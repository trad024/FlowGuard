"""Monitor agent (Step 6; DSO1, H1): detects incidents and proposes no action.

L0 reads only the frozen alert rules (D33), as Prometheus evaluated them. L1 and above add two
detectors (the ablation ladder is cumulative):

Runtime, per namespace and per pod, on the features of `flowguard.series`:
  1. a rolling z-score of each feature against that series' own last 10 minutes; the baseline
     is frozen while the entity looks abnormal, and a series with too little history (a new pod,
     the start of a replay) uses the reference profile fitted on normal traffic;
  2. when a z-score passes `z_threshold`, an Isolation Forest scores the entity's windowed
     z-scores (now, their mean over the last minute, and the largest of each); the entity is
     flagged once the score passes `if_threshold` for `persist` ticks in a row.
CI, from the run history: a job slower than its bound (median + k x robust spread of its past
successful durations; flagged while still running, as soon as it passes the bound) and a
failure streak (the same job failing `streak` runs in a row on one branch).

The Monitor signals an incident once per episode (a new alert name, a namespace with newly
flagged entities, a new CI finding) and posts evidence with references to the series behind
it. Release's P1 request gets a node or cluster stress claim. Thresholds come from a model file
fitted on dev recordings; without one the detectors are off, the Monitor behaves as at L0 and
its evidence says so.
"""

from __future__ import annotations

import json
import statistics
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from sklearn.ensemble import IsolationForest

from flowguard import series
from flowguard.agent_io import AgentInput, AgentOutput
from flowguard.contracts import AgentName, Event, EvidencePost

FEATURES = series.FEATURES
KINDS = ("namespace", "pod")
# Numerical guards, not tuned thresholds: the smallest spread a z-score divides by (one error in
# a hundred requests, one histogram step, 1% of the limit, one restart)
GUARD = {"error_ratio": 0.01, "p99_s": 0.05, "memory_ratio": 0.01, "restarts": 1.0}
MODEL_FILE = Path(__file__).resolve().parent / "models" / "monitor.json"
# P1 stress thresholds, frozen (agents/tests/test_monitor.py). Rule, fixed before the node series
# were seen: per recording, the median ratio against allocatable; the midpoint between the
# highest unstressed and the lowest stressed recording when they separate.
#   cpu: 0.507, from recordings-2 (D44 node stress): unstressed medians 0.022 and 0.023 (2),
#        stressed 0.990 to 0.992 (7), fully separated
#   memory: 0.90, final, a fixed choice, not set from data: every recorded node stress is CPU
#        stress, so the rule has no memory-stressed recording to separate (medians 0.154 to
#        0.165 throughout); untested on real memory stress (none recorded), a stated limitation
#   restarts: any restart of a non-canary pod in the last 5 minutes (none in any recording)
P1_CPU = 0.507
P1_MEMORY = 0.90
P1_RESTARTS = 1.0
FRESH_S = 60  # two polling cycles: an older snapshot does not describe "right now"


def kind_of(entity: str) -> str:
    return "pod" if "/" in entity else "namespace"


def features_of(snapshot: dict) -> dict[str, dict[str, float]]:
    """Flat snapshot -> entity -> feature -> value (node rows and unknown keys left out)."""
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for key, value in snapshot.items():
        parts = series.split_key(str(key))
        if parts and parts[0] != "node" and parts[1] in FEATURES:
            out[parts[0]][parts[1]] = float(value)
    return dict(out)


# ---------------------------------------------------------------- model file


@dataclass
class RuntimeModel:
    # kind -> feature -> [mean, std across entities, std within an entity] on normal traffic
    profile: dict[str, dict[str, list[float]]]
    z_threshold: float = 3.0
    if_threshold: float = 0.6
    persist: int = 2
    quantile: float = 0.95  # of normal-traffic scores, sets if_threshold
    train: list[list[float]] = field(default_factory=list)  # the forest's training vectors
    seed: int = 0
    history_s: float = 600.0
    min_history: int = 8
    window: int = 4  # ticks in the windowed mean (1 minute at 15 s)
    fitted_on: dict = field(default_factory=dict)

    def floor(self, kind: str, feature: str) -> float:
        return max(self.profile[kind][feature][2], GUARD[feature])

    def forest(self) -> IsolationForest | None:
        """Refit from the stored vectors: same data and seed, same trees (no pickle needed)."""
        if not self.train:
            return None
        rows = np.asarray(self.train, dtype=float)
        return IsolationForest(
            n_estimators=100, max_samples=min(256, len(rows)), random_state=self.seed
        ).fit(rows)


@dataclass
class CIModel:
    durations: dict[str, list[float]]  # job name -> past successful durations, seconds
    k: float = 4.0
    min_runs: int = 5
    streak: int = 3
    fitted_on: dict = field(default_factory=dict)


@dataclass
class MonitorModel:
    runtime: RuntimeModel | None = None
    ci: CIModel | None = None

    def save(self, path: Path = MODEL_FILE) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"runtime": asdict(self.runtime) if self.runtime else None}
        body["ci"] = asdict(self.ci) if self.ci else None
        path.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path = MODEL_FILE) -> MonitorModel:
        body = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            runtime=RuntimeModel(**body["runtime"]) if body.get("runtime") else None,
            ci=CIModel(**body["ci"]) if body.get("ci") else None,
        )


# ---------------------------------------------------------------- runtime detector


@dataclass
class EntityState:
    entity: str
    t: float
    values: dict[str, float]
    z: dict[str, float]
    vector: list[float]
    score: float | None  # Isolation Forest anomaly score (higher is stranger); None if not run
    flagged: bool


class RuntimeDetector:
    def __init__(self, model: RuntimeModel, forest: IsolationForest | None | str = "fit"):
        self.model = model
        self.forest = model.forest() if forest == "fit" else forest
        self.history: dict[tuple[str, str], deque] = {}
        self.recent: dict[str, deque] = {}
        self.streak: dict[str, int] = {}
        self.frozen_since: dict[str, float] = {}
        self.last_seen: dict[str, float] = {}

    def _z(self, entity: str, feature: str, x: float | None, t: float) -> float:
        if x is None:
            return 0.0
        m, kind = self.model, kind_of(entity)
        past = [v for when, v in self.history.get((entity, feature), ()) if when >= t - m.history_s]
        if len(past) >= m.min_history:
            mu, sd = statistics.fmean(past), statistics.pstdev(past)
        else:
            mu, sd = m.profile[kind][feature][0], m.profile[kind][feature][1]
        return max(0.0, (x - mu) / max(sd, m.floor(kind, feature)))  # only rises are anomalies

    def step(self, snapshot: dict, t: float) -> list[EntityState]:
        m, states = self.model, []
        for entity, values in sorted(features_of(snapshot).items()):
            z = {f: self._z(entity, f, values.get(f), t) for f in FEATURES}
            zs = [z[f] for f in FEATURES]
            recent = self.recent.setdefault(entity, deque(maxlen=m.window))
            recent.append(zs)
            means = [sum(col) / len(recent) for col in zip(*recent, strict=True)]
            # The largest z-scores too: a feature that never moves on normal traffic (5xx,
            # restarts) gives the trees nothing to split on, so its rise alone would look normal
            vector = [round(v, 4) for v in [*zs, *means, max(zs), max(means)]]
            candidate = max(zs) > m.z_threshold
            score = None
            if candidate and self.forest is not None:
                score = float(-self.forest.score_samples(np.asarray([vector]))[0])
            hit = score is not None and score >= m.if_threshold
            self.streak[entity] = self.streak.get(entity, 0) + 1 if hit else 0
            if candidate:  # keep the anomaly out of the baseline, but accept a new level in time
                learn = t - self.frozen_since.setdefault(entity, t) >= m.history_s
            else:
                self.frozen_since.pop(entity, None)
                learn = True
            if learn:
                for f, v in values.items():
                    self.history.setdefault((entity, f), deque(maxlen=200)).append((t, v))
            self.last_seen[entity] = t
            flagged = self.streak[entity] >= m.persist
            states.append(EntityState(entity, t, values, z, vector, score, flagged))
        for entity in [e for e, seen in self.last_seen.items() if seen < t - m.history_s]:
            for store in (self.recent, self.streak, self.frozen_since, self.last_seen):
                store.pop(entity, None)
            for key in [k for k in self.history if k[0] == entity]:
                del self.history[key]
        return states


def fit_runtime(
    snapshots: list[tuple[float, dict]],
    *,
    z_threshold: float = 3.0,
    quantile: float = 0.95,
    persist: int = 2,
    seed: int = 0,
    max_rows: int = 4000,
    fitted_on: dict | None = None,
) -> RuntimeModel:
    """Fit on normal traffic only: the reference profile, then the forest on the windowed vectors
    the detector builds, then `if_threshold` as the `quantile` of their scores.

    The forest scores a point beyond the training range about like the most extreme normal
    point, however far out it is, so a very high quantile can never be reached; the forest's job
    here is to tell a sustained shift from a one-tick blip among stage-1 candidates."""
    per_entity: dict[str, dict[str, dict[str, list[float]]]] = {
        k: defaultdict(lambda: defaultdict(list)) for k in KINDS
    }
    for _, snap in snapshots:
        for entity, values in features_of(snap).items():
            for f, v in values.items():
                per_entity[kind_of(entity)][entity][f].append(v)
    profile: dict[str, dict[str, list[float]]] = {}
    for kind in KINDS:
        profile[kind] = {}
        for f in FEATURES:
            pooled = [v for e in per_entity[kind].values() for v in e.get(f, [])]
            within = [
                v - statistics.fmean(e[f])
                for e in per_entity[kind].values()
                if e.get(f)
                for v in e[f]
            ]
            profile[kind][f] = [
                round(statistics.fmean(pooled), 6) if pooled else 0.0,
                round(statistics.pstdev(pooled), 6) if len(pooled) > 1 else 0.0,
                round(statistics.pstdev(within), 6) if len(within) > 1 else 0.0,
            ]
    model = RuntimeModel(
        profile=profile,
        z_threshold=z_threshold,
        persist=persist,
        quantile=quantile,
        seed=seed,
        fitted_on=fitted_on or {},
    )
    det = RuntimeDetector(model, forest=None)
    vectors = [s.vector for t, snap in snapshots for s in det.step(snap, t)]
    if not vectors:
        raise ValueError("no runtime features in the normal-traffic snapshots")
    if len(vectors) > max_rows:
        pick = np.random.default_rng(seed).choice(len(vectors), max_rows, replace=False)
        vectors = [vectors[i] for i in sorted(pick)]
    model.train = vectors
    forest = model.forest()
    scores = -forest.score_samples(np.asarray(vectors, dtype=float))
    model.if_threshold = round(float(np.quantile(scores, quantile)), 6)
    return model


# ---------------------------------------------------------------- CI detector


def _ts(value) -> float | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def spread(durations: list[float]) -> tuple[float, float]:
    """(median, robust spread): 1.4826 x MAD, at least 5% of the median and 5 s (GitHub's
    timestamps have whole seconds and runners vary by a few seconds)."""
    med = statistics.median(durations)
    mad = statistics.median(abs(d - med) for d in durations)
    return med, max(1.4826 * mad, 0.05 * med, 5.0)


@dataclass
class CIFinding:
    kind: str  # "slow" or "streak"
    run_id: int
    job: str
    job_id: int | None
    t: float
    value: float  # seconds so far (slow) or failures in a row (streak)
    bound: float | None = None
    median: float | None = None
    n: int = 0
    run_ids: list[int] = field(default_factory=list)


class CIDetector:
    """Runs and jobs in GitHub's own JSON shapes (Actions API), live or as recorded."""

    def __init__(self, model: CIModel):
        self.model = model
        self.durations = {job: list(d) for job, d in model.durations.items()}
        self.failures: dict[tuple[str, str], list[int]] = {}
        self.done: set[int] = set()
        self.reported: set[tuple[str, int, str]] = set()

    def bound(self, job: str) -> tuple[float, float, int] | None:
        past = self.durations.get(job, [])[-50:]
        if len(past) < self.model.min_runs:
            return None
        med, s = spread(past)
        return med + self.model.k * s, med, len(past)

    def check(self, run: dict, jobs: list[dict], t: float) -> list[CIFinding]:
        """New findings for this run as seen at time t (jobs without completed_at still run)."""
        found, run_id = [], int(run.get("id", 0))
        for job in jobs:
            start = _ts(job.get("started_at"))
            if start is None or job.get("conclusion") == "skipped":
                continue
            elapsed = (_ts(job.get("completed_at")) or t) - start
            b = self.bound(job["name"])
            key = ("slow", run_id, job["name"])
            if b and elapsed > b[0] and key not in self.reported:
                self.reported.add(key)
                found.append(CIFinding("slow", run_id, job["name"], job.get("id"), t, elapsed, *b))
        if run.get("status") == "completed" and run_id not in self.done:
            self.done.add(run_id)
            found += self._learn(run, jobs, t)
        return found

    def _learn(self, run: dict, jobs: list[dict], t: float) -> list[CIFinding]:
        found, run_id = [], int(run.get("id", 0))
        for job in jobs:
            name, conclusion = job["name"], job.get("conclusion")
            start, end = _ts(job.get("started_at")), _ts(job.get("completed_at"))
            if (
                conclusion == "success"
                and start
                and end
                and ("slow", run_id, name) not in (self.reported)
            ):
                self.durations.setdefault(name, []).append(end - start)
            key = (str(run.get("head_branch", "")), name)
            if conclusion == "failure":
                streak = self.failures.setdefault(key, [])
                streak.append(run_id)
                if len(streak) == self.model.streak:
                    found.append(
                        CIFinding(
                            "streak", run_id, name, job.get("id"), t, len(streak), run_ids=streak[:]
                        )
                    )
            elif conclusion == "success":
                self.failures.pop(key, None)
        return found


def fit_ci(
    runs: list[tuple[dict, list[dict]]], *, k_grid=(3.0, 4.0, 5.0, 6.0), fitted_on=None
) -> CIModel:
    """Durations of successful jobs in normal runs; k is the smallest in the grid that flags none
    of those jobs when each is checked against the others (leave one out)."""
    durations: dict[str, list[float]] = defaultdict(list)
    for _, jobs in runs:
        for job in jobs:
            start, end = _ts(job.get("started_at")), _ts(job.get("completed_at"))
            if job.get("conclusion") == "success" and start and end:
                durations[job["name"]].append(end - start)
    model = CIModel(durations=dict(durations), fitted_on=fitted_on or {})
    for k in k_grid:
        flags = 0
        for ds in durations.values():
            for i, d in enumerate(ds):
                rest = ds[:i] + ds[i + 1 :]
                if len(rest) >= model.min_runs:
                    med, s = spread(rest)
                    flags += d > med + k * s
        if flags == 0:
            model.k = k
            break
    else:
        model.k = k_grid[-1]
    return model


# ---------------------------------------------------------------- the agent


def tick_time(inp: AgentInput) -> float:
    ts = inp.params.get("ts")
    return float(ts) if isinstance(ts, (int, float)) else time.time()


def _fmt(feature: str, value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.0f}" if feature == "restarts" else f"{value:.3f}"


class Monitor:
    name = AgentName.MONITOR

    def __init__(self, model: MonitorModel | None | str = "default"):
        if model == "default":
            model = MonitorModel.load() if MODEL_FILE.is_file() else None
        self.model = model
        self.runtime = RuntimeDetector(model.runtime) if model and model.runtime else None
        self.ci = CIDetector(model.ci) if model and model.ci else None
        # Per environment: the live service sends one tick per namespace
        self.alerts: dict[str, set[str]] = defaultdict(set)
        self.flagged: set[str] = set()  # entities, namespace first
        self.last: dict[str, tuple[float, dict]] = {}
        self.failed_runs: set[int] = set()
        self.hash_seen: dict[str, float] = {}

    @property
    def alerting(self) -> bool:
        """An L0 alert or a flagged entity right now (an episode is under way)."""
        return any(self.alerts.values()) or bool(self.flagged)

    def run(self, inp: AgentInput, cfg) -> AgentOutput:
        t = tick_time(inp)
        self.last[inp.environment] = (t, dict(inp.metrics))
        for key in inp.metrics:
            parts = series.split_key(str(key))
            if parts and "/" in parts[0] and (h := series.pod_hash(parts[0].split("/", 1)[1])):
                self.hash_seen.setdefault(h, t)
        found: list[tuple[str, EvidencePost]] = [
            ("alert_rule", self._alert_post(inp, a, t))
            for a in sorted(set(inp.alerts) - self.alerts[inp.environment])
        ]
        self.alerts[inp.environment] = set(inp.alerts)
        found += [("ci", p) for p in self._failed_run(inp)]  # at every level, as L0
        score = None
        if cfg.ml:
            detected, score = self._detect(inp, t)
            found += detected
            off = [n for n, d in (("runtime", self.runtime), ("ci", self.ci)) if d is None]
            if found and off:
                note = self._post(inp, f"detectors off, no fitted model: {', '.join(off)}", [])
                found.append(("", note))
        detector = next((k for k, _ in found if k), None)
        return AgentOutput(
            incident=bool(found),
            evidence=[p for _, p in found],
            detector=detector,
            anomaly_score=score if detector == "runtime" else None,
        )

    def _failed_run(self, inp: AgentInput) -> list[EvidencePost]:
        """A completed CI run with a failed job is an incident (L0's CI detection)."""
        ci = inp.params.get("ci_run")
        run = ci.get("run", {}) if isinstance(ci, dict) else {}
        run_id = int(run.get("id", 0) or 0)
        if run.get("conclusion") != "failure" or run_id in self.failed_runs:
            return []
        self.failed_runs.add(run_id)
        failed = [j for j in ci.get("jobs", []) if j.get("conclusion") == "failure"]
        names = ", ".join(j["name"] for j in failed) or "unknown job"
        refs = [f"ci:run/{run_id}/job/{j.get('id')}" for j in failed] or [f"ci:run/{run_id}"]
        return [self._post(inp, f"CI run {run_id} failed: {names}", refs)]

    def _detect(
        self, inp: AgentInput, t: float
    ) -> tuple[list[tuple[str, EvidencePost]], float | None]:
        """(detector, evidence) per finding, and the highest isolation score flagged now."""
        posts: list[tuple[str, EvidencePost]] = []
        score = None
        if self.runtime is not None:
            flagged = {s.entity: s for s in self.runtime.step(inp.metrics, t) if s.flagged}
            # One episode per namespace: its pods and the namespace row are one incident
            known = {e.split("/")[0] for e in self.flagged}
            new = sorted(e for e in flagged if e.split("/")[0] not in known)
            posts += [("runtime", self._runtime_post(inp, flagged[e])) for e in new]
            # A tick speaks for the namespaces in its snapshot; the others keep their episodes
            here = {e.split("/")[0] for e in features_of(inp.metrics)}
            self.flagged = {e for e in self.flagged if e.split("/")[0] not in here} | set(flagged)
            if new:
                score = min(1.0, max(0.0, max(flagged[e].score or 0.0 for e in new)))
        run = inp.params.get("ci_run")
        if self.ci is not None and isinstance(run, dict):
            for f in self.ci.check(run.get("run", {}), run.get("jobs", []), t):
                posts.append(("ci", self._ci_post(inp, f)))
        return posts, score

    # ------------------------------------------------------------ evidence

    def _post(self, inp: AgentInput, claim: str, refs: list[str]) -> EvidencePost:
        return EvidencePost(incident_id=inp.incident_id, agent=self.name, claim=claim, refs=refs)

    def _alert_post(self, inp: AgentInput, alert: str, t: float) -> EvidencePost:
        ref = series.series_ref("ALERTS", {"alertname": alert, "alertstate": "firing"}, t - 15, t)
        return self._post(inp, f"L0 alert {alert} firing ({inp.environment})", [ref])

    def _runtime_post(self, inp: AgentInput, s: EntityState) -> EvidencePost:
        m = self.runtime.model
        lead = max(FEATURES, key=lambda f: s.z[f])
        strong = [f for f in FEATURES if s.z[f] > m.z_threshold]
        refs = [r for f in strong for r in series.refs(s.entity, f, s.t)]
        claim = (
            f"anomaly on {s.entity}: {lead} {_fmt(lead, s.values.get(lead))} (z {s.z[lead]:.1f});"
            f" isolation score {s.score:.2f} >= {m.if_threshold:.2f} for {m.persist} ticks"
        )
        return self._post(inp, claim, refs)

    def _ci_post(self, inp: AgentInput, f: CIFinding) -> EvidencePost:
        if f.kind == "slow":
            claim = (
                f"CI job {f.job} at {f.value:.0f} s, above its {f.bound:.0f} s bound"
                f" (median {f.median:.0f} s over {f.n} runs)"
            )
            refs = [f"ci:run/{f.run_id}/job/{f.job_id}"]
        else:
            claim = f"CI job {f.job} failed {f.value:.0f} runs in a row"
            refs = [f"ci:run/{r}" for r in f.run_ids]
        return self._post(inp, claim, refs)

    # ------------------------------------------------------------ P1

    def answer(self, inp: AgentInput) -> EvidencePost:
        """P1: is the node or cluster under stress right now? CPU and memory against allocatable,
        and container restarts on pods that are not the canary's."""
        t = tick_time(inp)
        last = self.last.get(inp.environment)  # the canary's namespace
        view = dict(last[1]) if last and t - last[0] <= FRESH_S else {}
        view.update(inp.metrics)
        cpu = view.get("node:cpu_ratio", view.get("node_cpu_ratio"))
        mem = view.get("node:memory_ratio", view.get("node_memory_ratio"))
        canary = inp.params.get("canary_hash") or self._canary(view)
        stress, refs, missing = [], [], []
        for label, value, limit, feature in (
            ("cpu", cpu, P1_CPU, "cpu_ratio"),
            ("memory", mem, P1_MEMORY, "memory_ratio"),
        ):
            if value is None:
                missing.append(label)
                continue
            refs += series.refs("node", feature, t)
            if float(value) >= limit:
                stress.append(f"{label} {float(value):.2f} >= {limit:.2f} of allocatable")
        for entity, n in sorted(self._restarts(view, canary).items()):
            stress.append(f"{n:.0f} restarts on {entity}")
            refs += series.refs(entity, "restarts", t)
        if stress:
            claim = "node under stress: " + "; ".join(stress)
        elif missing:
            claim = f"node stress unknown: no {' or '.join(missing)} series"
        else:
            claim = (
                f"node resources normal: cpu {float(cpu):.2f}, memory {float(mem):.2f} of"
                " allocatable, no restarts on non-canary pods"
            )
        return self._post(inp, claim, refs)

    def _canary(self, view: dict) -> str | None:
        """The newest pod-template hash on screen, when more than one is: the canary's."""
        present = {
            h
            for key in view
            if (p := series.split_key(str(key))) and "/" in p[0]
            if (h := series.pod_hash(p[0].split("/", 1)[1]))
        }
        if len(present) < 2:
            return None
        seen = [self.hash_seen.get(h) for h in present]
        if None in seen or len(set(seen)) < len(seen):
            return None  # cannot tell which is newer: every restart counts
        return max(present, key=self.hash_seen.get)

    @staticmethod
    def _restarts(view: dict, canary: str | None) -> dict[str, float]:
        out = {}
        for key, value in view.items():
            parts = series.split_key(str(key))
            if not parts or parts[1] != "restarts" or "/" not in parts[0]:
                continue
            if float(value) < P1_RESTARTS:
                continue
            pod = parts[0].split("/", 1)[1]
            if canary is None or series.pod_hash(pod) != canary:
                out[parts[0]] = float(value)
        return out


# ---------------------------------------------------------------- polling


def tick_event(
    snapshot: dict,
    alerts: list[str],
    t: float,
    *,
    environment: str = "staging",
    service: str = "podinfo",
    in_canary: bool = False,
) -> Event:
    """One Monitor poll as the orchestrator's metrics_tick event (CI results come as
    ci_run_completed events, see ci_event)."""
    params: dict = {"ts": t}
    return Event(
        id=f"tick-{environment}-{int(t)}",
        type="metrics_tick",
        service=service,
        environment=environment,
        payload={"metrics": snapshot, "alerts": alerts, "params": params, "in_canary": in_canary},
        received_at=datetime.fromtimestamp(t, UTC),
    )


def ci_event(
    run: dict,
    jobs: list[dict],
    t: float,
    *,
    log: str = "",
    environment: str = "staging",
    service: str = "podinfo",
) -> Event:
    """A finished CI run (GitHub's run and jobs JSON) and, if a job failed, its log (D49). The
    run's head commit and branch, when given, are what a fix starts from (D57, D64)."""
    failed = next((j for j in jobs if j.get("conclusion") == "failure"), None)
    params: dict = {"ts": t, "ci_run": {"run": run, "jobs": jobs}, "run_id": run.get("id")}
    for key in ("head_sha", "head_branch"):
        if run.get(key):
            params[key] = run[key]
    if failed:
        params["job_id"] = failed.get("id")
    return Event(
        id=f"ci-{run.get('id')}",
        type="ci_run_completed",
        service=service,
        environment=environment,
        payload={"log": log, "params": params},
        received_at=datetime.fromtimestamp(t, UTC),
    )


def poll(source: series.PrometheusSource, t: float | None = None, **kw) -> Event:
    """Live: read Prometheus at t (now by default) and build the tick event."""
    t = time.time() if t is None else t
    idx = source.fetch(t)
    return tick_event(series.snapshot(idx, t), series.firing(idx, t), t, **kw)
