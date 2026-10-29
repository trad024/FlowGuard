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
                    mode=None):  # fmt: skip
    """A 10-minute staging canary at 10 requests/s: 20% at 60 s, 50% at 210 s, 100% at 360 s,
    or the canary gone `abort_at` s into its 20% step. Counters grow by their expected values."""
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
    spec = {"family": "release", "environment": "staging", "fault": {"kind": "x"}}
    if node_stress:
        spec["node_stress"] = {"cpu_workers": 8, "cpu_request": 7.0}
    gt = {"bad_release": bad, **({"band": band} if band else {})}
    observed = {"l0": l0}
    if mode == "agents":  # D54: the canary was held and resumed to 100%; L0 is replayed
        observed = {"mode": "agents", "canary": "promoted", "l0": "offline"}
    (rec / "ground_truth.json").write_text(
        json.dumps({"spec": spec, "ground_truth": gt, "observed": observed, "valid": True})
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
    assert {w: t - T0 for w, t in good["steps"].items()} == {20: STEP_20, 50: STEP_50}
    assert good["end"] - T0 == FULL
    bad = rr.timeline(rr.load_index(runs["r-bad"]), "staging", *runs["r-bad"]["window"])
    assert bad["how"] == "aborted" and STEP_20 + 70 <= bad["end"] - T0 <= STEP_20 + 70 + 25


@pytest.fixture(scope="module")
def scored(tmp_path_factory):
    root = tmp_path_factory.mktemp("recordings")
    write_recording(root, "r-above", bad=True, band="above", l0="aborted", abort_at=70,
                    errors=0.1)  # fmt: skip
    write_recording(root, "r-invisible", bad=True, band="below", l0="aborted", abort_at=70)
    write_recording(root, "r-good", bad=False)
    write_recording(root, "r-slow", bad=True, band="near", slow=0.05)  # L0 promoted it
    return rr.score(rr.release_runs([root]))


def test_levels_are_scored_per_band_against_the_recorded_l0(scored):
    rec = scored["per_recording"]
    assert scored["label"] == "dev, in-sample" and scored["scored"] == 4
    assert rec["r-above"]["L3"]["outcome"] == "rollback" and rec["r-above"]["L3"]["t"] == 30
    assert rec["r-above"]["L0"]["outcome"] == "rollback" and rec["r-above"]["L0"]["t"] >= 70
    assert rec["r-slow"]["L3"]["outcome"] == "rollback"  # slow requests, invisible to L0's p99
    assert rec["r-good"]["L3"]["outcome"] == "promoted"
    assert [d["action"] for d in rec["r-good"]["L3"]["decisions"]] == (
        [None, None, None, "rollout_promote"] * 2  # holds, then a promotion per step
    )
    # L0 stopped the canary before Release's horizon on a canary that showed nothing:
    # truncated, counted as not caught
    assert rec["r-invisible"]["L3"]["outcome"] == "truncated"
    l0, l3 = scored["levels"]["L0"], scored["levels"]["L3"]
    assert (l0["bad_caught_before_100"]["k"], l3["bad_caught_before_100"]["k"]) == (2, 2)
    assert l3["bad_truncated"] == 1 and l3["false_rollbacks"]["k"] == 0
    assert l3["bad_caught_by_band"]["near"]["k"] == 1 and l0["bad_caught_by_band"]["near"]["k"] == 0
    assert (
        scored["L0_vs_L3"]["right_only_first"] == 1 and scored["L0_vs_L3"]["right_only_second"] == 1
    )
    assert scored["L0_vs_L3"]["both_rolled_back"] == 1
    assert scored["L0_vs_L3"]["rollback_time_gain_s_median"] > 0  # Release decides earlier


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


# ---------------------------------------------------------------- L0 replayed offline (D54)


def test_rate_extrapolates_as_prometheus_does():
    from flowguard.series import Series

    times = [T0 + 15 * k for k in range(9)]
    steady = Series({}, times, [15.0 * k for k in range(9)])  # 1 per second
    assert rr.prom_rate(steady, T0 + 120) == pytest.approx(1.0)
    assert rr.prom_rate(steady, T0 + 127) == pytest.approx(1.0)  # samples not on the range
    reset = Series({}, times, [100, 115, 130, 145, 160, 15, 30, 45, 60])  # restarted at 75 s
    assert rr.prom_rate(reset, T0 + 120) == pytest.approx(1.0)
    assert rr.prom_rate(steady, T0) is None  # one sample in (t - 60, t]: no output
    assert rr.prom_rate(steady, T0 + 15) == pytest.approx(0.25)  # no extrapolation below 0


def test_l0_is_replayed_offline_on_an_agents_mode_recording(tmp_path):
    # Under agents-mode every canary reaches 100%: the bad one is still on its pods
    write_recording(tmp_path, "r-bad", bad=True, band="above", errors=0.05, mode="agents")
    write_recording(tmp_path, "r-good", bad=False, mode="agents")
    write_recording(tmp_path, "r-edge", bad=True, band="near", errors=0.012, mode="agents")
    result = rr.score(rr.release_runs([tmp_path]))
    rec = result["per_recording"]
    assert result["agents_mode"] == 3 and result["l0_replay_vs_live"]["n"] == 0
    # second failed measurement of the 5xx ratio, 60 s into the 20% step
    assert rec["r-bad"]["L0"] == {"outcome": "rollback", "t": 60, "weight": 20, "failed": ["5xx"]}
    assert rec["r-edge"]["L0"]["outcome"] == "rollback"  # 1.2% is over 1% at every look
    assert rec["r-good"]["L0"]["outcome"] == "promoted"
    assert rec["r-bad"]["L3"]["outcome"] == "rollback"  # nothing truncates the other levels
    assert result["classes"] == {"bad / above": 1, "bad / near": 1, "good / dev": 1}


def test_no_canary_data_fails_an_l0_measurement():
    idx = {"podinfo_requests": [], "podinfo_latency_buckets": []}
    assert rr.l0_measure(idx, T0, "staging", "new") == {"5xx": False, "p99": False}


def test_the_replay_is_checked_against_l0s_live_decisions(scored):
    check = scored["l0_replay_vs_live"]
    # r-invisible: L0 aborted a canary that showed nothing, gone before a second failure
    assert check["n"] == 4 and check["agree"] == 3
    assert check["disagree"] == {"r-invisible": {"live": "rollback", "replay": "truncated"}}
