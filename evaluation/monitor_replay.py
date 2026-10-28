"""DSO1 by replay (D15, Step 6): the Monitor at L0 and L1 on the same dev recordings.

    python evaluation/monitor_replay.py fit   <recordings folder or zip>   # writes the model file
    python evaluation/monitor_replay.py score <recordings folder or zip> [--out FILE]

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
fit and is where false alerts are scored. CI: durations of successful jobs in runs outside the
pipeline family; k by leave-one-out. Held-out recordings are refused.
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
import stats  # noqa: E402
from flowguard import series  # noqa: E402
from flowguard.agent_io import AgentInput  # noqa: E402
from flowguard.monitor import (  # noqa: E402
    MODEL_FILE,
    CIDetector,
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
        band = r["ground_truth"].get("band", "dev")
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


def score_runtime(runs: list[dict], model: RuntimeModel, cache: dict | None = None) -> dict:
    cache = cache if cache is not None else {}
    scored = [r for r in runs if r["family"] in ("runtime", "normal")]
    l0 = {r["id"]: l0_times(r) for r in scored}
    det = {}
    for r in scored:
        if r["id"] not in cache:
            cache[r["id"]] = snapshots(r)
        det[r["id"]] = detector_times(model, cache[r["id"]])
    l1 = {i: sorted(set(l0[i]) | set(det[i])) for i in l0}
    spans = held_back(runs, model)
    levels = {"L0": l0, "L1": l1, "detector": det}
    cases = {name: outcomes(runs, times) for name, times in levels.items()}
    out = {name: summary(cases[name], false_alerts(runs, levels[name], spans)) for name in levels}
    out["L1_vs_L0"] = paired(cases["L0"], cases["L1"])
    out["notes"] = [
        "dev recordings: thresholds were tuned on these incidents (in-sample recall); "
        "H1 is tested on the held-out set at Step 11",
        "false alerts are scored on the normal-traffic time the model was not fitted on",
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


def as_of(meta: dict, jobs: list[dict], t: float) -> tuple[dict, list[dict]]:
    """The run as a poll at time t would have seen it."""
    seen, done = [], True
    for job in jobs:
        start = l0_baseline.ts(job["started_at"]) if job.get("started_at") else None
        if start is None or start > t:
            done = done and job.get("conclusion") == "skipped"
            continue
        end = l0_baseline.ts(job["completed_at"]) if job.get("completed_at") else None
        if end is None or end > t:
            seen.append({**job, "completed_at": None, "conclusion": None})
            done = False
        else:
            seen.append(job)
    return {**meta, "status": "completed" if done else "in_progress"}, seen


def first_ci_finding(model: CIModel, meta: dict, jobs: list[dict]) -> float | None:
    starts = [l0_baseline.ts(j["started_at"]) for j in jobs if j.get("started_at")]
    ends = [l0_baseline.ts(j["completed_at"]) for j in jobs if j.get("completed_at")]
    if not starts:
        return None
    det, t = CIDetector(model), min(starts)
    while t <= max(ends or starts) + STEP:
        if det.check(*as_of(meta, jobs, t), t):
            return t - min(starts)
        t += STEP
    return None


def without(model: CIModel, jobs: list[dict]) -> CIModel:
    """Leave this run's own durations out of the history it is judged against."""
    durations = {k: list(v) for k, v in model.durations.items()}
    for job in jobs:
        if job.get("conclusion") == "success" and job.get("started_at") and job.get("completed_at"):
            d = l0_baseline.ts(job["completed_at"]) - l0_baseline.ts(job["started_at"])
            if d in durations.get(job["name"], []):
                durations[job["name"]].remove(d)
    return CIModel(durations=durations, k=model.k, min_runs=model.min_runs, streak=model.streak)


def score_ci(runs: list[dict], model: CIModel) -> dict:
    anomalies, normal = [], []
    for r in runs:
        for meta, jobs in ci_runs(r):
            if r["family"] == "pipeline":
                anomalies.append(first_ci_finding(model, meta, jobs))
            else:
                normal.append(first_ci_finding(without(model, jobs), meta, jobs) is not None)
    mttd = [m for m in anomalies if m is not None]
    return {
        "recall": rate(len(mttd), len(anomalies)),
        "mttd_s_median": statistics.median(mttd) if mttd else None,
        "mttd_from": "the run's first job start",
        "false_flags_per_run": rate(sum(normal), len(normal)),
        "L0": "not defined: no frozen L0 rule for CI durations (open question)",
    }


# ---------------------------------------------------------------- fit


def fit(runs: list[dict], grid: dict = GRID, budget: float = FA_BUDGET) -> MonitorModel:
    normal = [r for r in runs if r["family"] == "normal"]
    if not normal:
        raise ValueError("no normal-traffic recording: nothing to fit the runtime detector on")
    scored = [r for r in runs if r["family"] in ("runtime", "normal")]
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
                cases = outcomes(runs, l1)
                fa = false_alerts(runs, times, fit_spans)  # what the detector adds to L0
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
    out["ci"] = score_ci(runs, model.ci) if model.ci else "no CI model"
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("fit", "score"))
    parser.add_argument("recordings", type=Path)
    parser.add_argument("--model", type=Path, default=MODEL_FILE)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    runs = load_runs(open_recordings(args.recordings))
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
