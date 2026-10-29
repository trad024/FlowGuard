"""DSO1 by replay (D15, Step 6): the Monitor at L0 and L1 on the same dev recordings.

    python evaluation/monitor_replay.py fit   <folder or zip> [...]   # writes the model file
    python evaluation/monitor_replay.py score <folder or zip> [...] [--out FILE]

Several folders are read as one set (the dev recordings and the normal-traffic window arrive
separately); nothing is copied.

Scoring side: reads each recording's ground_truth.json (family, incident window, band). The
Monitor sees only ticks built from the recorded series by `flowguard.series`, one every 15 s
(the rate Prometheus evaluated the L0 rules at), starting cold at the recording's start.

    L0         the frozen alert rules (D33), as Prometheus evaluated them live (ALERTS series),
               read exactly as l0_baseline.py reads them
    L1         L0 plus the runtime detector (the ablation ladder is cumulative)
    detector   the runtime detector alone (exploratory)

Rules of l0_baseline.dso1: an incident is detected when an alert is up in [start, end + 60 s];
MTTD runs from the incident's start; false alerts are alerting episodes on normal traffic (a gap
over 60 s starts a new one) per hour, with an exact Poisson interval. Rates have their count and
a 95% Wilson interval; L0 vs L1 is paired: exact McNemar on detected / missed and a bootstrap
interval for the median MTTD gain on incidents both detect. Every result also per band.

fit: the first half of each normal-traffic recording fits the reference profile and the forest;
a small grid (z threshold, forest quantile, persistence) is tuned for L1 recall on the dev
runtime incidents, keeping the detector's own false alerts on that same first half under a
budget (0.5 per hour). The second half is never used to
fit and is where false alerts are scored.

Runtime recordings without an ALERTS series (recorded before D33) are left out and listed: L0
cannot be measured on them. Incidents with an L0 alert already firing before the fault (left
over from the previous run) stay in the primary result and are left out of a sensitivity
result and of the tuning.

CI: the pipeline family's runs are the anomalies, every other successful run is normal and is
judged against the history of the others (leave one out). A run is judged when its
ci_run_completed event arrives (D49); the Monitor sees the event as replay.py builds it. L0 is
the rule frozen 2026-10-28 (CI_L0): a job longer than 3x its dev median, or 3 failed runs in a
row on a branch. L1 adds the duration detector (median + k x robust spread, k by leave-one-out).
Failed runs are incidents at every level (D49) and are only counted. Held-out recordings are
refused.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "evaluation")]

import l0_baseline  # noqa: E402
import replay  # noqa: E402
import stats  # noqa: E402
from flowguard import series  # noqa: E402
from flowguard.agent_io import AgentInput  # noqa: E402
from flowguard.contracts import Event  # noqa: E402
from flowguard.monitor import (  # noqa: E402
    MODEL_FILE,
    CIModel,
    Monitor,
    MonitorModel,
    RuntimeModel,
    fit_ci,
    fit_runtime,
)
from flowguard.orchestrator import level  # noqa: E402

STEP = 15.0
EPISODE_GAP = 60.0
FA_BUDGET = 0.5  # detector false alerts per hour on the fit half; DSO1's target is 1 for L1
GRID = {"z_threshold": (3.0, 4.0, 5.0), "quantile": (0.9, 0.95, 0.99), "persist": (1, 2, 3)}
OUT = ROOT / "evaluation" / "runs" / "dso1-replay.json"


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def open_recordings(path: Path) -> Path:
    """A folder as is; a zip extracted (zip-slip safe) into a temporary folder."""
    if "heldout" in str(path).lower():
        raise ValueError("held-out material is never replayed before Step 11")
    if path.suffix.lower() != ".zip":
        return path
    import datasets

    dest = Path(tempfile.mkdtemp(prefix="dso1-recordings-"))
    datasets.safe_extract(path, dest)
    return dest


def load_runs(root: Path) -> list[dict]:
    """Valid recordings under root, with what scoring needs (and only scoring reads)."""
    runs = []
    for manifest_path in sorted(root.rglob("manifest.json")):
        rec = manifest_path.parent
        if "heldout" in str(rec.relative_to(root)).lower():
            raise ValueError(f"held-out recording refused: {rec}")
        truth, manifest = _json(rec / "ground_truth.json"), _json(manifest_path)
        if truth is None or not truth.get("valid", True) or not manifest.get("valid", True):
            continue
        spec = truth.get("spec") or {}
        window = manifest.get("window") or {}
        runs.append(
            {
                "id": rec.name,
                "family": spec.get("family"),
                "recording": str(rec),
                "ground_truth": truth.get("ground_truth") or {},
                "spec": spec,
                "window": (l0_baseline.ts(window["start"]), l0_baseline.ts(window["end"])),
                "manifest": manifest,
            }
        )
    return runs


# ---------------------------------------------------------------- what the Monitor sees


def snapshots(run: dict) -> list[tuple[float, dict]]:
    rec = Path(run["recording"])
    raw = {p.stem: _json(p) for p in sorted((rec / "prometheus").glob("*.json"))}
    idx = series.index(raw)
    t0, t1 = run["window"]
    n = int((t1 - t0) // STEP)
    return [(t, series.snapshot(idx, t)) for t in (t0 + k * STEP for k in range(1, n + 1))]


def detector_times(model: RuntimeModel, snaps: list[tuple[float, dict]]) -> list[float]:
    """Tick times at which the Monitor (L1, alerts withheld) has a flagged entity."""
    mon, cfg, times = Monitor(MonitorModel(runtime=model)), level("L1"), []
    for t, snap in snaps:
        mon.run(AgentInput(incident_id="replay", metrics=snap, params={"ts": t}), cfg)
        if mon.flagged:
            times.append(t)
    return times


def l0_times(run: dict) -> list[float]:
    return [t for _, t in l0_baseline.firing(Path(run["recording"]))]


# ---------------------------------------------------------------- scoring


def episodes(times: list[float]) -> int:
    times = sorted(times)
    return sum(1 for a, b in zip([None, *times], times, strict=False) if a is None or b - a > 60)


def rate(k: int, n: int) -> dict:
    return l0_baseline.rate(k, n)


def per_hour(k: int, hours: float) -> dict:
    lo, hi = stats.poisson_exact(k)
    return {
        "episodes": k,
        "hours": round(hours, 3),
        "rate": round(k / hours, 3) if hours else None,
        "ci95": [round(lo / hours, 3), round(hi / hours, 3)] if hours else None,
    }


def outcomes(runs: list[dict], times: dict[str, list[float]]) -> dict[str, tuple]:
    """Runtime incident id -> (band, detected, MTTD or None)."""
    out = {}
    for r in runs:
        incident = r["ground_truth"].get("incident") or {}
        if r["family"] != "runtime" or not incident.get("start") or not incident.get("end"):
            continue
        start = l0_baseline.ts(incident["start"])
        end = l0_baseline.ts(incident["end"]) + 60  # the cooldown counts, as at L0
        hits = [t for t in times.get(r["id"], []) if start <= t <= end]
        band = r["ground_truth"].get("band") or "dev"  # the first dev scenarios have none
        out[r["id"]] = (band, bool(hits), (min(hits) - start) if hits else None)
    return out


def false_alerts(runs: list[dict], times: dict[str, list[float]], spans: dict) -> dict:
    k, hours = 0, 0.0
    for r in runs:
        if r["family"] != "normal" or r["id"] not in spans:
            continue
        lo, hi = spans[r["id"]]
        k += episodes([t for t in times.get(r["id"], []) if lo <= t <= hi])
        hours += (hi - lo) / 3600
    return per_hour(k, hours)


def summary(cases: dict[str, tuple], fa: dict) -> dict:
    by_band: dict[str, list] = defaultdict(list)
    for band, hit, mttd in cases.values():
        by_band[band].append((hit, mttd))

    def block(items):
        mttd = [m for _, m in items if m is not None]
        return {
            "recall": rate(sum(h for h, _ in items), len(items)),
            "mttd_s_median": statistics.median(mttd) if mttd else None,
        }

    return {
        **block([c for items in by_band.values() for c in items]),
        "by_band": {b: block(items) for b, items in sorted(by_band.items())},
        "false_alerts_per_hour": fa,
    }


def paired(l0: dict[str, tuple], l1: dict[str, tuple]) -> dict:
    """Exact McNemar on detected / missed; bootstrap interval of the median MTTD gain."""
    b = sum(l1[i][1] and not l0[i][1] for i in l0)
    c = sum(l0[i][1] and not l1[i][1] for i in l0)
    gains = [l0[i][2] - l1[i][2] for i in l0 if l0[i][1] and l1[i][1]]
    ci = stats.bootstrap_median(gains)
    return {
        "detected_only_l1": b,
        "detected_only_l0": c,
        "mcnemar_exact_p": stats.mcnemar_exact(b, c),
        "mttd_gain_s_median": statistics.median(gains) if gains else None,
        "mttd_gain_s_ci95": list(ci) if ci else None,
        "both_detected": len(gains),
    }


def held_back(runs: list[dict], model: RuntimeModel | None) -> dict[str, tuple[float, float]]:
    """Normal-traffic spans to score false alerts on: each window minus what fitted the model."""
    fitted = {f["recording"]: f for f in (model.fitted_on.get("normal", []) if model else [])}
    spans = {}
    for r in runs:
        if r["family"] != "normal":
            continue
        lo, hi = r["window"]
        if r["id"] in fitted:
            lo = max(lo, fitted[r["id"]]["end"])
        if hi > lo:
            spans[r["id"]] = (lo, hi)
    return spans


def alerts_recorded(run: dict) -> bool:
    """L0 can only be scored where the ALERTS series was recorded (from 2026-10-27, D33)."""
    return (Path(run["recording"]) / "prometheus" / "alerts.json").is_file()


def carry_over(run: dict) -> bool:
    """An L0 alert already firing before the fault started: left over from an earlier run."""
    incident = run["ground_truth"].get("incident") or {}
    if run["family"] != "runtime" or not incident.get("start"):
        return False
    start = l0_baseline.ts(incident["start"])
    return any(run["window"][0] <= t < start for t in l0_times(run))


def runtime_set(runs: list[dict]) -> tuple[list[dict], list[str]]:
    """Runtime and normal recordings L0 can be scored on, and the ids left out."""
    scored = [r for r in runs if r["family"] in ("runtime", "normal")]
    return [r for r in scored if alerts_recorded(r)], [
        r["id"] for r in scored if not alerts_recorded(r)
    ]


def score_runtime(runs: list[dict], model: RuntimeModel, cache: dict | None = None) -> dict:
    cache = cache if cache is not None else {}
    scored, unmeasured = runtime_set(runs)
    l0 = {r["id"]: l0_times(r) for r in scored}
    det = {}
    for r in scored:
        if r["id"] not in cache:
            cache[r["id"]] = snapshots(r)
        det[r["id"]] = detector_times(model, cache[r["id"]])
    l1 = {i: sorted(set(l0[i]) | set(det[i])) for i in l0}
    spans = held_back(scored, model)
    levels = {"L0": l0, "L1": l1, "detector": det}
    cases = {name: outcomes(scored, times) for name, times in levels.items()}
    out = {"label": "dev, in-sample"}
    out.update(
        {name: summary(cases[name], false_alerts(scored, levels[name], spans)) for name in levels}
    )
    out["L1_vs_L0"] = paired(cases["L0"], cases["L1"])
    tainted = sorted(r["id"] for r in scored if carry_over(r))
    clean = {name: {i: c for i, c in v.items() if i not in tainted} for name, v in cases.items()}
    out["without_carry_over"] = {
        "excluded": tainted,
        **{name: summary(clean[name], {}) for name in ("L0", "L1")},
        "L1_vs_L0": paired(clean["L0"], clean["L1"]),
    }
    out["not_scored"] = {"no ALERTS series recorded (L0 unmeasurable)": unmeasured}
    out["notes"] = [
        "dev, in-sample: the grid was tuned on these incidents; H1 is tested on the held-out "
        "set at Step 11",
        "false alerts are scored only on normal-traffic time the model was not fitted on",
        "without_carry_over: sensitivity, leaves out incidents with an L0 alert already firing "
        "before the fault (left over from the previous run); the primary result keeps them",
        "detector: exploratory (not a level of the ladder)",
    ]
    return out


# ---------------------------------------------------------------- CI


def ci_runs(run: dict) -> list[tuple[dict, list[dict]]]:
    rec, out = Path(run["recording"]), []
    for entry in run["manifest"].get("sources", {}).get("ci") or []:
        folder = rec / "ci" / str(entry.get("run_id"))
        meta, jobs = _json(folder / "run.json"), _json(folder / "jobs.json")
        if meta and jobs:
            out.append((meta, jobs))
    return out


def span(jobs: list[dict]) -> tuple[float, float] | None:
    """(first job start, last job end): when the run began and when ci_run_completed arrives."""
    starts = [l0_baseline.ts(j["started_at"]) for j in jobs if j.get("started_at")]
    ends = [l0_baseline.ts(j["completed_at"]) for j in jobs if j.get("completed_at")]
    return (min(starts), max(ends)) if starts and ends else None


def detector_flags(model: CIModel, history: dict[str, list[float]], event: Event) -> bool:
    """The Monitor at L1, given only this run's ci_run_completed event as the system shows it
    (D49), judged against `history`: does it report a CI incident?"""
    ci = CIModel(durations=history, k=model.k, min_runs=model.min_runs, streak=model.streak)
    p = event.payload
    inp = AgentInput(incident_id="replay", log=p.get("log", ""), params=dict(p.get("params", {})))
    out = Monitor(MonitorModel(ci=ci)).run(inp, level("L1"))
    return out.incident and out.detector == "ci"


# CI L0, decided 2026-10-28 and frozen before any CI scoring: a job alerts when its duration
# exceeds 3x its median over the dev history, or when 3 consecutive runs fail (one branch).
CI_L0 = {"duration_factor": 3.0, "failure_streak": 3}


def job_durations(ci: list[tuple[dict, list[dict]]]) -> dict[str, list[float]]:
    """Durations of successful jobs, by job name."""
    out: dict[str, list[float]] = defaultdict(list)
    for _, jobs in ci:
        for job in jobs:
            if (
                job.get("conclusion") == "success"
                and job.get("started_at")
                and job.get("completed_at")
            ):
                d = l0_baseline.ts(job["completed_at"]) - l0_baseline.ts(job["started_at"])
                out[job["name"]].append(d)
    return dict(out)


def without(history: dict[str, list[float]], jobs: list[dict]) -> dict[str, list[float]]:
    """Leave this run's own durations out of the history it is judged against."""
    left = {k: list(v) for k, v in history.items()}
    for name, ds in job_durations([({}, jobs)]).items():
        for d in ds:
            if d in left.get(name, []):
                left[name].remove(d)
    return left


def l0_duration_check(history: dict[str, list[float]]):
    medians = {k: statistics.median(v) for k, v in history.items() if v}

    def check(meta: dict, jobs: list[dict], t: float) -> bool:
        for job in jobs:
            if job.get("started_at") and job["name"] in medians:
                end = l0_baseline.ts(job["completed_at"]) if job.get("completed_at") else t
                elapsed = end - l0_baseline.ts(job["started_at"])
                if elapsed > CI_L0["duration_factor"] * medians[job["name"]]:
                    return True
        return False

    return check


def streak_runs(ci: list[tuple[dict, list[dict]]], by_job: bool, streak: int) -> set[int]:
    """Runs that complete a failure streak, in time order: L0 counts failed runs per branch,
    the detector failed jobs per branch and job."""
    flagged, count = set(), defaultdict(int)
    for meta, jobs in sorted(ci, key=lambda rj: rj[0].get("created_at") or ""):
        branch = meta.get("head_branch", "")
        keys = (
            [((branch, j["name"]), j.get("conclusion")) for j in jobs]
            if by_job
            else [((branch,), meta.get("conclusion"))]
        )
        for key, conclusion in keys:
            if conclusion == "failure":
                count[key] += 1
                if count[key] == streak:
                    flagged.add(meta.get("id"))
            elif conclusion == "success":
                count[key] = 0
    return flagged


def score_ci(runs: list[dict], model: CIModel | None) -> dict:
    """Pipeline-family runs are the anomalies (a slow step, the run succeeds); other successful
    runs are normal, each judged against the history without itself. Every run is judged once,
    when its ci_run_completed event arrives (D49), so MTTD is the run's length. Failed runs are
    incidents at every level (D49) and are counted apart. L1 = L0 plus the detector."""
    every = [(r, rj) for r in runs for rj in ci_runs(r)]
    events = {
        e.payload["params"].get("run_id"): e
        for r in {r["id"]: r for r, _ in every}.values()
        for e in replay.ci_events(Path(r["recording"]), r["manifest"])
        if e.type == "ci_run_completed"
    }
    history = job_durations([rj for r, rj in every if r["family"] != "pipeline"])
    l0_streak = streak_runs([rj for _, rj in every], False, CI_L0["failure_streak"])
    l1_streak = streak_runs([rj for _, rj in every], True, model.streak) if model else set()
    times: dict[str, list] = {"L0": [], "L1": [], "detector": []}
    labels, failed = [], 0
    for r, (meta, jobs) in every:
        when = span(jobs)
        if meta.get("conclusion") == "failure" or when is None or meta.get("id") not in events:
            failed += meta.get("conclusion") == "failure"
            continue
        hist = history if r["family"] == "pipeline" else without(history, jobs)
        mttd = when[1] - when[0]
        hit0 = l0_duration_check(hist)(meta, jobs, when[1]) or meta.get("id") in l0_streak
        hit1 = bool(model) and (
            detector_flags(model, hist, events[meta["id"]]) or meta.get("id") in l1_streak
        )
        labels.append(r["family"] == "pipeline")
        times["L0"].append(mttd if hit0 else None)
        times["detector"].append(mttd if hit1 else None)
        times["L1"].append(mttd if hit0 or hit1 else None)

    def block(found: list) -> dict:
        hits = [m for m, bad in zip(found, labels, strict=True) if bad and m is not None]
        return {
            "recall": rate(len(hits), sum(labels)),
            "mttd_s_median": statistics.median(hits) if hits else None,
            "false_flags_per_run": rate(
                sum(m is not None for m, bad in zip(found, labels, strict=True) if not bad),
                len(labels) - sum(labels),
            ),
        }

    return {
        **{name: block(found) for name, found in times.items()},
        "streak_runs": {"L0": sorted(l0_streak), "detector": sorted(l1_streak)},
        "L0_rule": CI_L0,
        "history": {name: len(ds) for name, ds in sorted(history.items())},
        "detector_min_runs": model.min_runs if model else None,
        "failed_runs": failed,
        "mttd_from": "the run's first job start to its completion (ci_run_completed, D49)",
        "notes": ["detector: exploratory (not a level of the ladder)"],
    }


# ---------------------------------------------------------------- fit


def fit(runs: list[dict], grid: dict = GRID, budget: float = FA_BUDGET) -> MonitorModel:
    measured, _ = runtime_set(runs)
    normal = [r for r in measured if r["family"] == "normal"]
    if not normal:
        raise ValueError("no normal-traffic recording: nothing to fit the runtime detector on")
    scored = [r for r in measured if not carry_over(r)]  # tuned on clean incidents only
    cache = {r["id"]: snapshots(r) for r in scored}
    fit_snaps, fit_spans = [], {}
    for r in normal:
        snaps = cache[r["id"]]
        mid = snaps[len(snaps) // 2][0]
        fit_snaps += [s for s in snaps if s[0] < mid]
        fit_spans[r["id"]] = (r["window"][0], mid)
    fitted_on = {
        "normal": [{"recording": i, "start": a, "end": b} for i, (a, b) in fit_spans.items()]
    }
    tried, best = [], None
    for z in grid["z_threshold"]:
        for q in grid["quantile"]:
            for persist in grid["persist"]:
                m = fit_runtime(
                    fit_snaps, z_threshold=z, quantile=q, persist=persist, fitted_on=fitted_on
                )
                times = {i: detector_times(m, cache[i]) for i in cache}
                l1 = {r["id"]: sorted(set(l0_times(r)) | set(times[r["id"]])) for r in scored}
                cases = outcomes(scored, l1)
                fa = false_alerts(scored, times, fit_spans)  # what the detector adds to L0
                mttd = [c[2] for c in cases.values() if c[2] is not None]
                row = {
                    "z_threshold": z,
                    "quantile": q,
                    "persist": persist,
                    "recall": sum(c[1] for c in cases.values()),
                    "incidents": len(cases),
                    "detector_fa_per_hour": fa["rate"],
                    "mttd_s_median": statistics.median(mttd) if mttd else None,
                }
                tried.append(row)
                if fa["rate"] is None or fa["rate"] > budget:
                    continue
                # most recall, then fastest, then the most conservative setting
                key = (row["recall"], -(row["mttd_s_median"] or 1e9), z, q, persist)
                if best is None or key > best[0]:
                    best = (key, m)
    if best is None:
        raise ValueError(f"no setting stays under {budget} false alerts per hour: {tried}")
    runtime = best[1]
    runtime.fitted_on = {**fitted_on, "budget_per_hour": budget, "grid": tried}
    ci_hist = [rj for r in runs if r["family"] != "pipeline" for rj in ci_runs(r)]
    ci = fit_ci(ci_hist, fitted_on={"runs": [m.get("id") for m, _ in ci_hist]}) if ci_hist else None
    return MonitorModel(runtime=runtime, ci=ci)


def score(runs: list[dict], model: MonitorModel) -> dict:
    out = {"recordings": len(runs)}
    out["runtime"] = score_runtime(runs, model.runtime) if model.runtime else "no runtime model"
    out["ci"] = score_ci(runs, model.ci)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("fit", "score"))
    parser.add_argument("recordings", type=Path, nargs="+", help="folders or zips")
    parser.add_argument("--model", type=Path, default=MODEL_FILE)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    runs = [r for path in args.recordings for r in load_runs(open_recordings(path))]
    if len({r["id"] for r in runs}) != len(runs):
        raise ValueError("the same recording id appears twice")
    if args.command == "fit":
        print(fit(runs).save(args.model))
        return 0
    result = score(runs, MonitorModel.load(args.model))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
