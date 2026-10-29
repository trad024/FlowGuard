"""DSO3 replay (Step 8) on synthetic release recordings: the canary timeline from pods up per
hash, L0 from the recorded outcome, Release at L3 and L4 through the orchestrator, scoring per
band, truncation, P1 under node stress (H4's count) and the conformal calibration (D9)."""

import json
from datetime import UTC, datetime

import pytest

import release_replay as rr
from flowguard import release

T0 = 1_800_000_000.0
WINDOW = 600
STEP_20, STEP_50, FULL = 60, 210, 360  # seconds into the recording


def iso(t):
    return datetime.fromtimestamp(t, UTC).isoformat().replace("+00:00", "Z")


def canary_pods(s, abort_at):
    if abort_at is not None and s >= STEP_20 + abort_at:
        return 0
    return 10 if s >= FULL else 5 if s >= STEP_50 else 2 if s >= STEP_20 else 0


def write_recording(root, rid, *, bad, band=None, l0="promoted", abort_at=None, errors=0.0,
                    slow=0.0, stable_errors=0.0, node_cpu=None, node_stress=False,
                    steps=None):  # fmt: skip
    """A 10-minute staging canary at 10 requests/s: 20% at 60 s, 50% at 210 s, 100% at 360 s,
    or the canary gone `abort_at` s into its 20% step. Counters grow by their expected values.
    `steps`: the runner's Rollout step log rows, when the recording has one."""
    rec = root / rid
    (rec / "prometheus").mkdir(parents=True)
    times = [T0 + 15 * k for k in range(WINDOW // 15 + 1)]
    up, requests, buckets = [], [], []
    for h in ("old", "new"):
        for i in range(10):
            values = [
                [t, "1"]
                for t in times
                if i
                < (
                    canary_pods(t - T0, abort_at)
                    if h == "new"
                    else 10 - canary_pods(t - T0, abort_at)
                )  # fmt: skip
            ]
            labels = {"namespace": "staging", "pod": f"podinfo-{h}-{i}",
                      "rollouts_pod_template_hash": h, "app": "podinfo"}  # fmt: skip
            up.append({"metric": labels, "values": values})
        total = err = fast = 0.0
        series_req, series_err, series_inf, series_fast = [], [], [], []
        for a, b in zip(times, times[1:], strict=False):
            pods = canary_pods(a - T0, abort_at)
            pods = pods if h == "new" else 10 - pods
            n = pods * (b - a)  # one request/s per pod
            e = n * (errors if h == "new" else stable_errors)
            sl = n * (slow if h == "new" else 0.0)
            total, err, fast = total + n - e, err + e, fast + n - sl
            series_req.append([b, str(total)])
            series_err.append([b, str(err)])
            series_inf.append([b, str(total + err)])
            series_fast.append([b, str(fast)])
        base = {"namespace": "staging", "path": "root", "app": "podinfo",
                "rollouts_pod_template_hash": h, "pod": f"podinfo-{h}-0"}  # fmt: skip
        requests += [
            {"metric": {**base, "status": "200"}, "values": [[T0, "0"], *series_req]},
            {"metric": {**base, "status": "500"}, "values": [[T0, "0"], *series_err]},
        ]
        buckets += [
            {"metric": {**base, "le": "+Inf"}, "values": [[T0, "0"], *series_inf]},
            {"metric": {**base, "le": "0.5"}, "values": [[T0, "0"], *series_fast]},
        ]
    files = {"podinfo_up": up, "podinfo_requests": requests, "podinfo_latency_buckets": buckets}
    if node_cpu is not None:
        used = [[t, str(node_cpu * 8 * (t - T0))] for t in times]  # CPU seconds of 8 cores
        files["node_cpu"] = [{"metric": {"id": "/"}, "values": used}]
        files["node_allocatable"] = [
            {"metric": {"resource": "cpu"}, "values": [[t, "8"] for t in times]}
        ]
    for name, result in files.items():
        (rec / "prometheus" / f"{name}.json").write_text(json.dumps({"result": result}))
    if steps is not None:
        (rec / "rollout-events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in steps))
    spec = {"family": "release", "environment": "staging", "fault": {"kind": "x"}}
    if node_stress:
        spec["node_stress"] = {"cpu_workers": 8, "cpu_request": 7.0}
    gt = {"bad_release": bad, **({"band": band} if band else {})}
    (rec / "ground_truth.json").write_text(
        json.dumps({"spec": spec, "ground_truth": gt, "observed": {"l0": l0}, "valid": True})
    )
    (rec / "manifest.json").write_text(
        json.dumps(
            {
                "window": {"start": iso(T0), "end": iso(T0 + WINDOW)},
                "valid": True,
                "sources": {"prometheus": {n: {} for n in files}},
            }
        )
    )
    return rec


def test_timeline_reads_the_steps_and_how_the_canary_ended(tmp_path):
    write_recording(tmp_path, "r-good", bad=False)
    write_recording(tmp_path, "r-bad", bad=True, l0="aborted", abort_at=70, errors=0.1)
    runs = {r["id"]: r for r in rr.release_runs([tmp_path])}
    good = rr.timeline(rr.load_index(runs["r-good"]), "staging", *runs["r-good"]["window"])
    assert (good["stable"], good["canary"], good["how"]) == ("old", "new", "promoted")
    # Pods are Ready about half a scrape before their first `up` sample
    lag = rr.READY_LAG_S
    assert {w: t - T0 for w, t in good["steps"].items()} == {20: STEP_20 - lag, 50: STEP_50 - lag}
    assert good["end"] - T0 == FULL
    bad = rr.timeline(rr.load_index(runs["r-bad"]), "staging", *runs["r-bad"]["window"])
    assert bad["how"] == "aborted" and STEP_20 + 70 <= bad["end"] - T0 <= STEP_20 + 70 + 25


def step_log(pause_20, pause_50=None, end=None, aborted=False):
    """Step log rows as recorder.RolloutStepLog writes them (seconds into the recording)."""
    base = {"namespace": "staging", "canary_hash": "new", "stable_hash": "old", "abort": False,
            "aborted_at": None, "pause_start": None}  # fmt: skip
    rows = []
    for weight, pause in ((20, pause_20), (50, pause_50)):
        if pause is None:
            continue
        rows.append({**base, "ts": iso(T0 + pause - 4), "step": "setWeight", "set_weight": weight})
        rows.append(
            {
                **base,
                "ts": iso(T0 + pause + 2),
                "step": "pause",
                "set_weight": weight,
                "pause_start": iso(T0 + pause),
            }  # fmt: skip
        )
    if end is not None and aborted:
        rows.append({**base, "ts": iso(T0 + end + 3), "step": "pause", "set_weight": 20,
                     "abort": True, "aborted_at": iso(T0 + end)})  # fmt: skip
    elif end is not None:
        rows.append({**base, "ts": iso(T0 + end), "step": "setWeight", "set_weight": 100})
    return rows


def test_logged_step_times_replace_the_estimate_and_old_recordings_keep_it(tmp_path, monkeypatch):
    # Argo's own times, different from what the pods up suggest (52.5 s and 202.5 s)
    write_recording(tmp_path, "r-logged", bad=False, steps=step_log(57, 205, 357))
    write_recording(tmp_path, "r-old", bad=False)
    write_recording(tmp_path, "r-stopped", bad=True, l0="aborted", abort_at=70, errors=0.1,
                    steps=step_log(57, end=131, aborted=True))  # fmt: skip
    runs = {r["id"]: r for r in rr.release_runs([tmp_path])}
    idx = {k: rr.load_index(r) for k, r in runs.items()}
    logged = rr.run_timeline(runs["r-logged"], idx["r-logged"])
    assert logged["source"] == "argo" and (logged["canary"], logged["stable"]) == (
        "new", "old"
    )  # fmt: skip
    assert {w: t - T0 for w, t in logged["steps"].items()} == {20: 57, 50: 205}
    assert (logged["end"] - T0, logged["how"]) == (357, "promoted")
    old = rr.run_timeline(runs["r-old"], idx["r-old"])
    assert old["source"] == "pods_up_estimate"
    lag = rr.READY_LAG_S
    assert {w: t - T0 for w, t in old["steps"].items()} == {20: STEP_20 - lag, 50: STEP_50 - lag}
    stopped = rr.run_timeline(runs["r-stopped"], idx["r-stopped"])
    assert (stopped["end"] - T0, stopped["how"]) == (131, "aborted")  # Argo's abortedAt

    # The offline L0 starts each analysis 30 s after the logged pause start; Release looks
    # from the same times
    starts, looks = [], []
    analysis, step_event = rr.l0_canary.analysis, rr.release.step_event

    def spy_analysis(i, start, *a, **k):
        starts.append(start - T0)
        return analysis(i, start, *a, **k)

    def spy_step(i, t, *a, **k):
        looks.append(t - T0)
        return step_event(i, t, *a, **k)

    monkeypatch.setattr(rr.l0_canary, "analysis", spy_analysis)
    monkeypatch.setattr(rr.release, "step_event", spy_step)
    rr.l0_offline(runs["r-logged"], idx["r-logged"])
    rr.l0_offline(runs["r-old"], idx["r-old"])
    assert starts == [87, 235, STEP_20 - lag + 30, STEP_50 - lag + 30]
    rr.replay(runs["r-logged"], "L3", tmp_path, idx["r-logged"])
    assert looks[:4] == [87, 117, 147, 177] and looks[4] == 235
    result = rr.score(list(runs.values()))
    assert result["step_source"] == {"argo": 2, "pods_up_estimate": 1}
    assert result["step_source_fallback"] == ["r-old"]  # flagged in the results (D54)
    assert result["per_recording"]["r-logged"]["step_source"] == "argo"


@pytest.fixture(scope="module")
def scored(tmp_path_factory):
    root = tmp_path_factory.mktemp("recordings")
    write_recording(root, "r-above", bad=True, band="above", l0="aborted", abort_at=70,
                    errors=0.1)  # fmt: skip
    write_recording(root, "r-invisible", bad=True, band="below", l0="aborted", abort_at=70)
    write_recording(root, "r-good", bad=False)
    write_recording(root, "r-slow", bad=True, band="near", slow=0.05)  # L0 promoted it
    return rr.score(rr.release_runs([root]))


def test_every_level_is_scored_per_band_on_the_same_recording(scored):
    rec = scored["per_recording"]
    assert scored["label"] == "dev, in-sample" and scored["scored"] == 4
    # Release's first look and L0's (offline) first measurement are both 30 s into the step;
    # L0 needs a second failure
    assert rec["r-above"]["L3"]["outcome"] == "rollback" and rec["r-above"]["L3"]["t"] == 30
    assert rec["r-above"]["L0"]["outcome"] == "rollback" and rec["r-above"]["L0"]["t"] == 60
    assert [s["metric"] for s in rec["r-above"]["L0"]["steps"]] == ["error-ratio-5xx"]
    # Slow requests: the p99 L0 reads stays at the 0.5 s bucket bound, Release counts them
    assert rec["r-slow"]["L0"]["outcome"] == "promoted"
    assert rec["r-slow"]["L3"]["outcome"] == "rollback"
    assert rec["r-good"]["L0"]["outcome"] == rec["r-good"]["L3"]["outcome"] == "promoted"
    assert [d["action"] for d in rec["r-good"]["L3"]["decisions"]] == (
        [None, None, None, "rollout_promote"] * 2  # holds, then a promotion per step
    )
    # Recorded under the L0 Rollout and aborted live on a canary that showed nothing: no level
    # can decide on what is left of it
    assert rec["r-invisible"]["L0"]["outcome"] == rec["r-invisible"]["L3"]["outcome"] == "truncated"
    l0, l3 = scored["levels"]["L0"], scored["levels"]["L3"]
    assert (l0["bad_caught_before_100"]["k"], l3["bad_caught_before_100"]["k"]) == (1, 2)
    assert l3["bad_truncated"] == l0["bad_truncated"] == 1 and l3["false_rollbacks"]["k"] == 0
    assert l3["bad_caught_by_band"]["near"]["k"] == 1 and l0["bad_caught_by_band"]["near"]["k"] == 0
    paired = scored["L0_vs_L3"]
    assert (paired["right_only_first"], paired["right_only_second"]) == (0, 1)
    assert paired["both_rolled_back"] == 1 and paired["rollback_time_gain_s_median"] == 30


def test_offline_l0_is_checked_against_live_l0_and_short_recordings_are_listed(scored):
    agreement = scored["l0_live_agreement"]
    assert (agreement["agree"]["k"], agreement["agree"]["n"]) == (3, 4)
    assert agreement["differ"] == {"r-invisible": {"offline": "truncated", "live": "rollback"}}
    listed = {x["recording"]: x["reasons"] for x in scored["needs_rerecording"]}
    assert set(listed) == {"r-above", "r-invisible"}  # aborted live: no full window
    assert "a level is truncated" in listed["r-invisible"]


def test_p1_at_l4_under_node_stress_gives_h4_its_count(tmp_path):
    # A good release on a stressed node whose canary pods take the errors: L3 rolls back
    write_recording(tmp_path, "r-stress", bad=False, errors=0.05, node_cpu=0.97, node_stress=True)
    write_recording(tmp_path, "r-calm", bad=False)
    result = rr.score(rr.release_runs([tmp_path]))
    assert result["per_recording"]["r-stress"]["L3"]["outcome"] == "rollback"
    assert result["per_recording"]["r-stress"]["L4"]["outcome"] == "escalate"  # P1 holds
    assert (
        result["node_stress_L3"]["wrong"]["k"] == 1 and result["node_stress_L3"]["wrong"]["n"] == 1
    )
    assert result["node_stress_L4"]["wrong"]["k"] == 0
    assert result["node_stress_L3"]["with_node_series"] == 1
    assert result["levels"]["L4"]["good_escalated"] == 1
    assert result["L3_vs_L4"]["right_only_second"] == 1


def test_calibration_fits_release_scores_and_reports_coverage(tmp_path):
    root = tmp_path / "recordings"
    for i in range(8):
        write_recording(root, f"r-good-{i}", bad=False)
    for i in range(6):
        write_recording(root, f"r-bad-{i}", bad=True, band="above", errors=0.1)
    result = rr.score(rr.release_runs([root]))
    path = tmp_path / "release-calibration.json"
    report = rr.calibrate(result["per_recording"], path)
    assert report["decisions"] == 8 * 2 + 6 and report["calibrated"]
    assert report["coverage_leave_one_recording_out"]["n"] == 22
    assert report["rollbacks_at_confidence_0_70"] == 6  # clean separation: confident
    loaded = release.calibrator(path)
    assert loaded.calibrated and loaded.confidence({"rollback": 1.0, "continue": 0.0})[1] >= 0.7


def test_held_out_and_duplicate_recordings_are_refused(tmp_path):
    write_recording(tmp_path / "heldout", "r-1", bad=False)
    with pytest.raises(ValueError, match="held-out"):
        rr.release_runs([tmp_path / "heldout"])
    write_recording(tmp_path / "dev", "r-2", bad=False)
    with pytest.raises(ValueError, match="twice"):
        rr.release_runs([tmp_path / "dev", tmp_path / "dev"])
