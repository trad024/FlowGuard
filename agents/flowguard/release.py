"""Release agent (Step 8; DSO3, H4): decides each canary step from canary vs stable counts.

Data (D21; latency by D55): per-pod Prometheus counters summed per pod-template hash, cut into
30 s windows from the start of the canary's first step: requests to /, the 5xx among them, and
the requests slower than 500 ms (histogram buckets: the +Inf count minus the le="0.5" count).
`windows()` builds them from a series index, live and in replay alike (D15), and
`step_event()` turns them into the orchestrator's rollout_step event.

Sequential test (D4): one always-valid test per metric (5xx, slow). Within a window, given how
many errors (or slow requests) there were in all, the canary's share of them is binomial with
the canary's share of the window's requests when the canary is no worse than stable (exactly
hypergeometric, which only makes the test more conservative). The test is a mixture SPRT on
the odds ratio rho of that share: the e-value is the average over a fixed grid of rho > 1 of the
product over windows of rho^x / (rho * pi + 1 - pi)^k (x canary events of k, pi the canary's
traffic share). Under "no worse" its expectation is at most 1 at every look, so stopping at the
first look where it reaches 2 / alpha keeps the false rollback rate under alpha / 2 per metric,
however often it is looked at (Ville's inequality); the always-valid p-value is 1 / e-value.
It conditions on the counts, so it needs no normal approximation: it stays valid with the
handful of errors a 10 request/s canary shows, where a z-test is not.

Absolute floor (D4): a rollback also needs the canary's 5xx (or slow) share over the whole canary
to exceed stable's by at least FLOOR, so a significant but negligible excess never rolls back.

Decisions per look (rollout_step):
    test rejects, floor passed      roll back: rollout_abort (before 100%); with collaboration
                                    (L4+) P1 first: if Monitor or Diagnose says the cause is
                                    infrastructure, hold and escalate_to_human instead
    no rejection, LOOKS_PER_STEP    continue: rollout_promote (the next step)
    no rejection, fewer looks       hold: no proposal, the Rollout stays paused
    no canary requests at the end   escalate_to_human (an unmeasurable canary is not promoted)
    the Rollout is aborted          rollout_undo (back to the previous revision; high tier)
rollout_set_weight is never proposed: without a traffic router the weight is set by the steps.
A Monitor incident mid-canary reaches Release with the same windows (the service adds them to
its metrics_tick) and is decided by the same test, with the looks made so far in the step, so
it never promotes. One decision per step: once Release proposed for the step (`decided`), the
incident is only evidence and proposes nothing.
Below L1 (L0) the canary is judged alone on the frozen L0 thresholds over its last minute.

Label scores (D9): {"rollback": 1 - p, "continue": p} with p the smaller always-valid p-value
(times 2 for the two metrics, at most 1); the gate's confidence comes from a conformal
calibrator fitted on the dev release decisions (CALIBRATION_FILE), never from this agent.
"""

from __future__ import annotations

import math
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from flowguard import series
from flowguard.agent_io import AgentInput, AgentOutput
from flowguard.calibration import ConformalCalibrator
from flowguard.contracts import ActionProposal, AgentName, Event, EvidencePost

ALPHA = 0.05  # false rollbacks of a no-worse canary, over both metrics and every look
METRICS = ("5xx", "slow")
RHOS = tuple(2.0**k for k in range(1, 11))  # odds ratios 2 to 1024, equal weights
FLOOR = 0.001  # 0.1 percentage point: the smallest excess worth a rollback
SLOW_S = 0.5  # podinfo's histogram boundary at L0's p99 threshold
WINDOW_S = 30.0  # one window per L0 measurement interval
LOOKS_PER_STEP = 4  # L0 measures 4 times per step; Release looks as often before promoting
L0_RATIO = 0.01  # the frozen L0 thresholds: 5xx share 1%, p99 500 ms (> 1% slower)
CALIBRATION_FILE = Path(__file__).resolve().parent / "models" / "release-calibration.json"

# window fields: canary requests, 5xx, slow, then stable requests, 5xx, slow
FIELDS = (
    "canary_requests",
    "canary_5xx",
    "canary_slow",
    "stable_requests",
    "stable_5xx",
    "stable_slow",
)


# ---------------------------------------------------------------- counts per hash


def _increase(s: series.Series, lo: float, hi: float) -> float:
    """Counter increase over (lo, hi], from the last sample at or before lo, counting resets
    as Prometheus does; adjacent windows add up to the whole."""
    times, _ = s.between(float("-inf"), lo)
    start = times[-1] if times else lo
    _, values = s.between(start, hi)
    return sum(b - a if b >= a else b for a, b in zip(values, values[1:], strict=False))


def counts(idx: series.Index, lo: float, hi: float, namespace: str) -> dict[str, list[float]]:
    """Pod-template hash -> [requests to /, 5xx, slower than SLOW_S] over (lo, hi]."""
    out: dict[str, list[float]] = {}
    for s in idx.get("podinfo_requests", []):
        m = s.labels
        h = m.get("rollouts_pod_template_hash")
        if m.get("path") != "root" or m.get("namespace") != namespace or not h:
            continue
        c = out.setdefault(h, [0.0, 0.0, 0.0])
        inc = _increase(s, lo, hi)
        c[0] += inc
        c[1] += inc if str(m.get("status", "")).startswith("5") else 0.0
    for s in idx.get("podinfo_latency_buckets", []):
        m = s.labels
        h = m.get("rollouts_pod_template_hash")
        if m.get("namespace") != namespace or not h or m.get("le") not in ("+Inf", str(SLOW_S)):
            continue
        sign = 1.0 if m["le"] == "+Inf" else -1.0  # slow = all - at most SLOW_S
        out.setdefault(h, [0.0, 0.0, 0.0])[2] += sign * _increase(s, lo, hi)
    for c in out.values():
        c[2] = max(0.0, c[2])
    return out


def windows(
    idx: series.Index, since: float, t: float, namespace: str, canary: str, stable: str
) -> list[list[float]]:
    """30 s windows from `since` to t (the last one may be shorter), FIELDS each."""
    out = []
    lo = since
    while lo < t:
        hi = min(lo + WINDOW_S, t)
        c = counts(idx, lo, hi, namespace)
        zero = [0.0, 0.0, 0.0]
        out.append([round(v, 3) for v in (*c.get(canary, zero), *c.get(stable, zero))])
        lo = hi
    return out


def step_event(
    idx: series.Index,
    t: float,
    namespace: str,
    canary: str,
    stable: str,
    since: float,
    looks_in_step: int,
    *,
    weight: int | None = None,
    aborted: bool = False,
    service: str = "podinfo",
) -> Event:
    """One look at a canary as the orchestrator's rollout_step event: the windows, their
    totals, and the node and restart rows the P1 answers read."""
    ws = windows(idx, since, t, namespace, canary, stable)
    totals = {f: round(sum(w[i] for w in ws), 3) for i, f in enumerate(FIELDS)}
    snap = series.snapshot(idx, t)
    context = {
        k: v
        for k, v in snap.items()
        if k.startswith("node:") or (k.startswith(f"{namespace}/") and k.endswith(":restarts"))
    }
    return Event(
        id=f"rollout-{namespace}-{canary}-{int(t)}",
        type="rollout_step",
        service=service,
        environment=namespace,
        payload={
            "metrics": {**totals, "windows": ws, **context},
            "in_canary": True,
            "params": {
                "ts": t,
                "canary_hash": canary,
                "looks_in_step": looks_in_step,
                "weight": weight,
                "aborted": aborted,
            },
        },
        received_at=datetime.fromtimestamp(t, UTC),
    )


# ---------------------------------------------------------------- the sequential test


def e_value(ws: list[list[float]], metric: str) -> float:
    """Mixture SPRT e-value that the canary's share of `metric` events is above its traffic
    share (see the module docstring); 1 when there is no evidence either way."""
    i = METRICS.index(metric) + 1
    logs = [0.0] * len(RHOS)
    for w in ws:
        nc, ns = w[0], w[3]
        x, k = w[i], w[i] + w[i + 3]
        if k <= 0 or nc + ns <= 0:
            continue
        pi = nc / (nc + ns)
        for j, rho in enumerate(RHOS):
            logs[j] += x * math.log(rho) - k * math.log(rho * pi + 1 - pi)
    top = max(logs)
    if top > 700:  # far past any threshold; avoid overflow
        return math.inf
    return sum(math.exp(v) for v in logs) / len(RHOS)


@dataclass(frozen=True)
class Verdict:
    outcome: str  # rollback, continue, hold, no_data
    e_values: dict[str, float]
    excess: dict[str, float]  # canary share - stable share over the whole canary
    p_value: float  # smaller always-valid p-value, Bonferroni over the two metrics
    reasons: tuple[str, ...]


def shares(ws: list[list[float]]) -> tuple[dict[str, float], float, float]:
    n_c = sum(w[0] for w in ws)
    n_s = sum(w[3] for w in ws)
    excess = {}
    for i, m in enumerate(METRICS, start=1):
        c = sum(w[i] for w in ws) / n_c if n_c else 0.0
        s = sum(w[i + 3] for w in ws) / n_s if n_s else 0.0
        excess[m] = c - s
    return excess, n_c, n_s


def judge(ws: list[list[float]], looks_in_step: int, alpha: float = ALPHA) -> Verdict:
    """The sequential test and the floor on the windows so far."""
    es = {m: e_value(ws, m) for m in METRICS}
    excess, n_c, _ = shares(ws)
    p = min(1.0, len(METRICS) * min(1.0 / e if e > 0 else 1.0 for e in es.values()))
    reasons = tuple(m for m in METRICS if es[m] >= len(METRICS) / alpha and excess[m] >= FLOOR)
    if reasons:
        outcome = "rollback"
    elif looks_in_step < LOOKS_PER_STEP:
        outcome = "hold"
    else:
        outcome = "continue" if n_c > 0 else "no_data"
    return Verdict(outcome, es, excess, p, reasons)


def judge_l0(ws: list[list[float]], looks_in_step: int) -> Verdict:
    """L0's frozen thresholds on the canary alone over its last minute (two windows)."""
    last = ws[-2:]
    n = sum(w[0] for w in last)
    ratios = {m: (sum(w[i] for w in last) / n if n else 0.0) for i, m in enumerate(METRICS, 1)}
    reasons = tuple(m for m in METRICS if ratios[m] > L0_RATIO)
    if reasons:
        outcome = "rollback"
    elif looks_in_step < LOOKS_PER_STEP:
        outcome = "hold"
    else:
        outcome = "continue" if sum(w[0] for w in ws) > 0 else "no_data"
    return Verdict(outcome, {}, ratios, 1.0, reasons)


def calibrator(path: Path = CALIBRATION_FILE) -> ConformalCalibrator:
    """The conformal calibrator for Release's proposals (empty, so confidence 0, if absent)."""
    return ConformalCalibrator.load(path) if path.is_file() else ConformalCalibrator()


# ---------------------------------------------------------------- the agent


def infrastructure(post: EvidencePost) -> bool:
    """P1: does an answer say the cause is the node or the cluster, not the release?"""
    claim = post.claim.lower()
    return claim.startswith("node under stress") or "infrastructure-caused" in claim


class Release:
    name = AgentName.RELEASE

    def run(self, inp: AgentInput, cfg, ask=None) -> AgentOutput:
        if inp.params.get("aborted"):
            return self._out(inp, "rollout_undo", "aborted canary: back to the previous revision")
        if inp.params.get("decided"):  # one decision per step
            claim = "Release already proposed for this canary step: the incident is evidence only"
            post = EvidencePost(incident_id=inp.incident_id, agent=self.name, claim=claim)
            return AgentOutput(evidence=[post])
        ws = inp.metrics.get("windows")
        if not isinstance(ws, list) or not ws:
            return self._out(inp, "escalate_to_human", "no per-hash canary counts to test")
        looks = inp.params.get("looks_in_step")
        looks = LOOKS_PER_STEP if looks is None else int(looks)  # 0 before the step's 1st look
        verdict = judge(ws, looks) if cfg.ml else judge_l0(ws, looks)
        evidence = [self._post(inp, verdict, ws)]
        if verdict.outcome == "hold":
            return AgentOutput(evidence=evidence)
        if verdict.outcome == "no_data":
            return self._out(inp, "escalate_to_human", "no canary requests", verdict, evidence)
        if verdict.outcome == "continue":
            return self._out(inp, "rollout_promote", "canary no worse", verdict, evidence)
        why = ", ".join(verdict.reasons)
        if cfg.collaboration and ask is not None:  # P1: ask before rolling back
            answers = [
                ask(AgentName.MONITOR, "Is the node or cluster under stress?"),
                ask(AgentName.DIAGNOSE, "Does the error look release- or infrastructure-caused?"),
            ]
            evidence += answers
            if any(infrastructure(a) for a in answers):
                rationale = f"canary worse ({why}), but the evidence says infrastructure: hold"
                return self._out(inp, "escalate_to_human", rationale, verdict, evidence)
        return self._out(inp, "rollout_abort", f"canary worse: {why}", verdict, evidence)

    def _out(self, inp, action, rationale, verdict=None, evidence=()) -> AgentOutput:
        p = ActionProposal(
            id=f"p-{secrets.token_hex(5)}",
            incident_id=inp.incident_id,
            agent=self.name,
            action=action,
            target=inp.target,
            environment=inp.environment,
            params={k: v for k, v in inp.params.items() if k in ("canary_hash", "weight")},
            confidence=0.0,  # never used by the gate (D9)
            rationale=rationale[:500],
        )
        scores = {}
        if verdict is not None:
            scores = {p.id: {"rollback": 1.0 - verdict.p_value, "continue": verdict.p_value}}
        return AgentOutput(proposals=[p], label_scores=scores, evidence=list(evidence))

    def _post(self, inp: AgentInput, v: Verdict, ws: list[list[float]]) -> EvidencePost:
        tot = [sum(w[i] for w in ws) for i in range(len(FIELDS))]
        claim = (
            f"canary 5xx {tot[1]:.0f}/{tot[0]:.0f}, slow {tot[2]:.0f}/{tot[0]:.0f}; stable 5xx"
            f" {tot[4]:.0f}/{tot[3]:.0f}, slow {tot[5]:.0f}/{tot[3]:.0f}; "
            + (
                "e-values " + ", ".join(f"{m} {v.e_values[m]:.3g}" for m in METRICS)
                if v.e_values
                else "L0 thresholds"
            )
            + f"; {v.outcome}"
        )
        t = float(inp.params.get("ts") or 0.0)
        canary = inp.params.get("canary_hash")
        refs = (
            [
                series.series_ref(
                    "http_request_duration_seconds_count",
                    {"namespace": inp.environment, "rollouts_pod_template_hash": canary},
                    t - WINDOW_S * len(ws),
                    t,
                )
            ]
            if t and canary
            else []
        )
        return EvidencePost(incident_id=inp.incident_id, agent=self.name, claim=claim, refs=refs)
