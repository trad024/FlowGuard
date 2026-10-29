"""DSO1 replay (Step 6): L0 read as l0_baseline reads it, fit on normal traffic only, L0 vs L1
scoring per band, CI scoring, and the held-out refusal. Recordings here are small synthetic
ones in the recorder's format (manifest, ground truth, prometheus/*.json, ci/<run>/)."""

import json
import math
import random
import zipfile
from datetime import UTC, datetime

import pytest

import l0_baseline
import leakage
import monitor_replay as mr
import stats
from flowguard.monitor import Monitor, MonitorModel

T0 = 1_800_000_000.0
STEP = 15.0
LE = ["0.005", "0.01", "0.025", "0.05", "0.1", "0.25", "0.5", "1", "2.5", "5", "10", "+Inf"]
PODS = ("podinfo-aaa-1", "podinfo-aaa-2", "podinfo-aaa-3", "podinfo-aaa-4")
TINY_GRID = {"z_threshold": (3.0,), "quantile": (0.9, 0.95), "persist": (2,)}


def iso(t):
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def prometheus(seconds, *, start, seed, error=None):
    """Request counters, latency buckets, working set, restarts; error(pod, t) -> 5xx share."""
    rng = random.Random(seed)
    counters, buckets, out = {}, {}, {}
    names = ("podinfo_requests", "podinfo_latency_buckets", "container_memory", "pod_restarts")
    out = {n: {} for n in names}

    def sample(name, key, labels, t, v):
        out[name].setdefault(key, {"metric": labels, "values": []})["values"].append([t, str(v)])

    for k in range(int(seconds // STEP) + 1):
        t = start + k * STEP
        for pod in PODS:
            base = {"namespace": "staging", "pod": pod, "app": "podinfo"}
            share = error(pod, t) if error else 0.0
            for _ in range(15):
                status = "500" if rng.random() < share else "200"
                latency = rng.lognormvariate(math.log(0.004), 0.5)
                counters[(pod, status)] = counters.get((pod, status), 0) + 1
                for le in LE:
                    buckets[(pod, le)] = buckets.get((pod, le), 0) + (latency <= float(le))
            for (p, status), n in counters.items():
                if p == pod:
                    labels = {**base, "path": "root", "status": status}
                    sample("podinfo_requests", (p, status), labels, t, n)
            for le in LE:
                labels = {**base, "path": "root", "le": le}
                sample("podinfo_latency_buckets", (pod, le), labels, t, buckets.get((pod, le), 0))
            working = 24 * 2**20 + rng.uniform(-0.2, 0.2) * 2**20
            sample("container_memory", pod, {**base, "container": "podinfo"}, t, working)
            sample("pod_restarts", pod, {**base, "container": "podinfo"}, t, 0)
    return {name: list(v.values()) for name, v in out.items()}


def write_recording(
    root, rid, *, family, seconds, start=T0, seed=1, error=None, alerts=(), truth=None, ci=()
):
    rec = root / rid
    (rec / "prometheus").mkdir(parents=True)
    for name, result in prometheus(seconds, start=start, seed=seed, error=error).items():
        (rec / "prometheus" / f"{name}.json").write_text(json.dumps({"result": result}))
    firing = [
        {"metric": {"alertname": name, "alertstate": "firing"}, "values": [[t, "1"] for t in ts]}
        for name, ts in alerts
    ]
    (rec / "prometheus" / "alerts.json").write_text(json.dumps({"result": firing}))
    for run, jobs in ci:
        (rec / "ci" / str(run["id"])).mkdir(parents=True)
        (rec / "ci" / str(run["id"]) / "run.json").write_text(json.dumps(run))
        (rec / "ci" / str(run["id"]) / "jobs.json").write_text(json.dumps(jobs))
    manifest = {
        "recording_id": rid,
        "window": {"start": iso(start), "end": iso(start + seconds)},
        "sources": {"ci": [{"run_id": run["id"]} for run, _ in ci]},
        "valid": True,
    }
    (rec / "manifest.json").write_text(json.dumps(manifest))
    spec = {"id": rid, "family": family, "hours": seconds / 3600}
    body = {"ground_truth": truth or {}, "spec": spec, "valid": True}
    (rec / "ground_truth.json").write_text(json.dumps(body))
    return rec


def incident(start, band, pod_share):
    """A 300 s error burst on one pod of four, the runtime family's shape."""

    def error(pod, t):
        return pod_share if pod == PODS[0] and start <= t < start + 300 else 0.0

    truth = {"incident": {"start": iso(start), "end": iso(start + 300)}, "band": band}
    return error, truth


@pytest.fixture(scope="module")
def dev(tmp_path_factory):
    root = tmp_path_factory.mktemp("recordings")
    write_recording(
        root,
        "normal",
        family="normal",
        seconds=7200,
        seed=3,
        alerts=[("HighLatencyP99", [T0 + 5000])],
    )
    s1 = T0 + 10_000
    err, truth = incident(s1 + 60, "below", 0.2)  # 5% of the service: L0 stays silent
    write_recording(
        root, "rt-below", family="runtime", start=s1, seconds=420, seed=4, error=err, truth=truth
    )
    s2 = T0 + 20_000
    err, truth = incident(s2 + 60, "above", 0.9)
    write_recording(
        root,
        "rt-above",
        family="runtime",
        start=s2,
        seconds=420,
        seed=5,
        error=err,
        truth=truth,
        alerts=[("HighErrorRatio", [s2 + 60 + 135, s2 + 60 + 150])],
    )
    return root


def test_l0_scoring_is_l0_baselines(dev):
    runs = mr.load_runs(dev)
    times = {r["id"]: mr.l0_times(r) for r in runs}
    spans = {r["id"]: r["window"] for r in runs if r["family"] == "normal"}
    mine = mr.summary(mr.outcomes(runs, times), mr.false_alerts(runs, times, spans))
    frozen = l0_baseline.dso1(runs)
    assert mine["recall"] == frozen["recall"] and mine["mttd_s_median"] == frozen["mttd_s_median"]
    assert {b: v["recall"] for b, v in mine["by_band"].items()} == frozen["recall_by_band"]
    fa = mine["false_alerts_per_hour"]
    assert (fa["episodes"], fa["rate"], fa["ci95"]) == (
        frozen["false_alerts_per_hour"]["episodes"],
        frozen["false_alerts_per_hour"]["rate"],
        frozen["false_alerts_per_hour"]["ci95"],
    )


def test_fit_on_normal_then_score_l0_vs_l1_per_band(dev, tmp_path):
    runs = mr.load_runs(dev)
    model = mr.fit(runs, grid=TINY_GRID)
    (fitted,) = model.runtime.fitted_on["normal"]
    assert fitted["recording"] == "normal" and fitted["end"] < T0 + 7200
    assert len(model.runtime.fitted_on["grid"]) == 2
    model = MonitorModel.load(model.save(tmp_path / "monitor.json"))
    out = mr.score(runs, model)["runtime"]
    assert out["L0"]["by_band"]["below"]["recall"]["k"] == 0
    assert out["L1"]["by_band"]["below"]["recall"]["k"] == 1  # the detector adds this one
    assert out["L1"]["recall"]["k"] == 2 and out["L1"]["recall"]["n"] == 2
    assert out["L1"]["mttd_s_median"] <= out["L0"]["mttd_s_median"]
    assert out["L1_vs_L0"]["detected_only_l1"] == 1 and out["L1_vs_L0"]["detected_only_l0"] == 0
    fa = out["L1"]["false_alerts_per_hour"]
    assert fa["hours"] == pytest.approx(1.0, abs=0.01)  # only the held-back half
    assert fa["ci95"][1] > 0  # exact Poisson upper bound even at 0 episodes
    assert out["L0"]["false_alerts_per_hour"]["episodes"] == 1  # the firing at T0 + 5000


def test_unmeasured_l0_is_left_out_and_carry_over_is_a_sensitivity(tmp_path):
    write_recording(tmp_path, "normal", family="normal", seconds=1800, seed=3)
    s1 = T0 + 10_000
    err, truth = incident(s1 + 60, "near", 0.9)
    write_recording(  # an alert left over from the previous run fires before the fault
        tmp_path, "carried", family="runtime", start=s1, seconds=420, seed=4, error=err,
        truth=truth, alerts=[("ContainerOOMKilled", [s1 + 10, s1 + 25, s1 + 70])],
    )  # fmt: skip
    s2 = T0 + 20_000
    err, truth = incident(s2 + 60, "near", 0.9)
    rec = write_recording(
        tmp_path, "old", family="runtime", start=s2, seconds=420, seed=5, error=err, truth=truth
    )
    (rec / "prometheus" / "alerts.json").unlink()  # recorded before the L0 rules were
    runs = mr.load_runs(tmp_path)
    assert [mr.carry_over(r) for r in runs if r["family"] == "runtime"] == [True, False]
    model = mr.fit(runs, grid=TINY_GRID)
    out = mr.score(runs, model)["runtime"]
    assert out["label"] == "dev, in-sample"
    assert out["not_scored"] == {"no ALERTS series recorded (L0 unmeasurable)": ["old"]}
    assert out["L0"]["recall"] == mr.rate(1, 1)  # the leftover alert counts, as l0_baseline does
    assert out["without_carry_over"]["excluded"] == ["carried"]
    assert out["without_carry_over"]["L0"]["recall"]["n"] == 0


def test_replayed_inputs_carry_no_experiment_words(dev):
    runs = {r["id"]: r for r in mr.load_runs(dev)}
    snaps = mr.snapshots(runs["rt-above"])
    keys = {k for _, snap in snaps for k in snap}
    assert keys and not [k for k in keys if leakage.hits(leakage.masked(k))]
    model = mr.fit(list(runs.values()), grid=TINY_GRID)
    mon = Monitor(MonitorModel(runtime=model.runtime))
    from flowguard.agent_io import AgentInput
    from flowguard.orchestrator import level

    claims = []
    for t, snap in mr.snapshots(runs["rt-below"]):
        out = mon.run(AgentInput(incident_id="x", metrics=snap, params={"ts": t}), level("L1"))
        claims += [p.claim for p in out.evidence]
    assert claims and not [c for c in claims if leakage.hits(leakage.masked(c))]


def gh(t):
    return iso(t)


def ci_run(run_id, unit_s, start, branch="dev", conclusion="success"):
    run = {
        "id": run_id,
        "head_branch": branch,
        "status": "completed",
        "conclusion": conclusion,
        "created_at": gh(start),
    }
    jobs = [
        {"id": run_id * 10 + 1, "name": "build", "conclusion": "success",
         "started_at": gh(start), "completed_at": gh(start + 60)},
        {"id": run_id * 10 + 2, "name": "unit", "conclusion": conclusion,
         "started_at": gh(start + 60), "completed_at": gh(start + 60 + unit_s)},
    ]  # fmt: skip
    return run, jobs


def test_ci_scoring_l0_vs_l1_on_a_slow_run_and_normal_runs(tmp_path):
    for i, d in enumerate([98, 104, 100, 97, 103, 101, 99, 102]):
        write_recording(
            tmp_path,
            f"sec-{i}",
            family="security",
            seconds=60,
            ci=[ci_run(i + 1, d, T0 + i * 1000)],
        )
    write_recording(tmp_path, "slow", family="pipeline", seconds=60, ci=[ci_run(99, 220, T0 + 9e4)])
    runs = mr.load_runs(tmp_path)
    model = mr.fit_ci([rj for r in runs if r["family"] != "pipeline" for rj in mr.ci_runs(r)])
    out = mr.score_ci(runs, model)
    assert out["L0"]["recall"]["k"] == 0  # 220 s is under 3 x the 100 s median
    assert (
        out["L1"]["recall"]["k"] == 1
        and out["L1"]["false_flags_per_run"] == out["L0"]["false_flags_per_run"]
    )
    assert out["L1"]["false_flags_per_run"]["k"] == 0 and out["L1"]["false_flags_per_run"]["n"] == 8
    assert out["L1"]["mttd_s_median"] == 60 + 220  # judged on ci_run_completed (D49)
    assert out["history"] == {"build": 8, "unit": 8} and out["failed_runs"] == 0


def test_ci_failed_runs_are_counted_apart_and_l0_catches_a_3x_job(tmp_path):
    for i, d in enumerate([98, 104, 100]):
        write_recording(tmp_path, f"sec-{i}", family="security", seconds=60,
                        ci=[ci_run(i + 1, d, T0 + i * 1000)])  # fmt: skip
    write_recording(tmp_path, "fail", family="ci", seconds=60,
                    ci=[ci_run(7, 30, T0 + 5000, conclusion="failure")])  # fmt: skip
    write_recording(tmp_path, "slow", family="pipeline", seconds=60, ci=[ci_run(99, 400, T0 + 9e4)])
    runs = mr.load_runs(tmp_path)
    out = mr.score_ci(runs, mr.fit_ci([rj for r in runs for rj in mr.ci_runs(r)]))
    assert out["failed_runs"] == 1 and out["L0"]["false_flags_per_run"]["n"] == 3
    assert out["L0"]["recall"]["k"] == 1  # 400 s > 3 x the 100 s median
    assert out["detector"]["recall"]["k"] == 0  # 3 runs of history: under min_runs


def test_ci_l0_rule_is_frozen():
    assert mr.CI_L0 == {"duration_factor": 3.0, "failure_streak": 3}  # decided 2026-10-28
    check = mr.l0_duration_check({"unit": [90.0, 100.0, 110.0]})
    run, jobs = ci_run(1, 300, T0)
    assert not check(run, jobs, T0 + 1e4)  # exactly 3 x the median: no alert
    run, jobs = ci_run(1, 301, T0)
    assert check(run, jobs, T0 + 1e4)
    running = [{**j, "completed_at": None} for j in jobs]
    assert not check(run, running, T0 + 60 + 300) and check(run, running, T0 + 60 + 301)
    failed = [ci_run(i, 30, T0 + i, conclusion="failure") for i in range(1, 4)]
    assert mr.streak_runs(failed, by_job=False, streak=3) == {3}
    assert mr.streak_runs(failed[:2], by_job=False, streak=3) == set()
    reset = [failed[0], failed[1], ci_run(9, 30, T0 + 2.5), failed[2]]
    assert mr.streak_runs(reset, by_job=False, streak=3) == set()
    other = [failed[0], failed[1], ci_run(4, 30, T0 + 3, branch="x", conclusion="failure")]
    assert mr.streak_runs(other, by_job=False, streak=3) == set()  # per branch


def test_held_out_material_is_refused(tmp_path):
    with pytest.raises(ValueError, match="held-out"):
        mr.open_recordings(tmp_path / "heldout" / "x.zip")
    write_recording(tmp_path / "heldout", "h-1", family="runtime", seconds=60)
    with pytest.raises(ValueError, match="held-out"):
        mr.load_runs(tmp_path)


def test_a_zip_of_recordings_opens(tmp_path):
    write_recording(tmp_path / "src", "normal", family="normal", seconds=120)
    archive = tmp_path / "dev-recordings.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for p in (tmp_path / "src").rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(tmp_path / "src").as_posix())
    (run,) = mr.load_runs(mr.open_recordings(archive))
    assert run["family"] == "normal" and run["window"] == (T0, T0 + 120)


def test_exact_mcnemar_and_bootstrap_median():
    assert stats.mcnemar_exact(0, 0) == 1.0
    assert stats.mcnemar_exact(5, 0) == pytest.approx(2 / 32)
    assert stats.mcnemar_exact(6, 1) == pytest.approx(2 * (1 + 7) / 128)
    assert stats.mcnemar_exact(3, 3) == 1.0
    lo, hi = stats.bootstrap_median([10.0, 20.0, 30.0, 40.0, 50.0], resamples=2000)
    assert lo <= 30.0 <= hi and stats.bootstrap_median([]) is None
    assert stats.bootstrap_median([1.0, 2.0, 3.0], resamples=500) == stats.bootstrap_median(
        [1.0, 2.0, 3.0], resamples=500
    )
