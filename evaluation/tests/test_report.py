"""Offline toolkit verified against small synthetic audit/truth databases only."""

import hashlib
import json
import sqlite3
import statistics

import pytest

import report
import stats
from flowguard.audit import AuditLog
from report_data import audit_outcomes, connect, load
from report_demo import create
from report_metrics import (
    analyze,
    autonomous,
    comparisons,
    detected,
    llm_variance,
    macro_f1,
    measurements,
    method_pairs,
    mttd,
    primary_method,
)
from report_policy import policy


@pytest.fixture
def inputs(tmp_path):
    return create(tmp_path / "synthetic")


def test_exact_statistics_and_poisson_underflow_reference():
    result = stats.mcnemar_paired([True] * 10, [False] * 10)
    assert result["p_value"] == 2 / 1024 and result["left_only"] == 10
    assert stats.mcnemar_paired([True, False], [True, False])["p_value"] == 1
    assert stats.mcnemar_paired([], [])["p_value"] is None
    # Garwood chi-square quantiles, independently checked with scipy.stats.chi2.
    assert stats.poisson_exact(1000) == pytest.approx(
        (938.9730184076952, 1063.952136016302), abs=1e-7
    )
    with pytest.raises(ValueError):
        stats.wilson(2, 1)


def test_bootstrap_pairs_seed_and_holm_family_are_preserved():
    a, b = [2, 4, 6], [0, 2, 4]
    result = stats.paired_bootstrap(a, b, statistics.mean)
    assert result["estimate"] == 2 and result["ci95"] == pytest.approx([2, 2])
    assert result == stats.paired_bootstrap(a, b, statistics.mean)
    assert result["resamples"] == 10000
    adjusted = stats.holm({"H1": 0.01, "H2a": 0.04, "H2b": None, "H3": 0.03, "H4": 0.8})
    assert adjusted["H1"]["adjusted_p"] == 0.05
    assert adjusted["H2a"]["adjusted_p"] == 0.12
    assert adjusted["H2b"]["adjusted_p"] is None
    assert adjusted["H1"]["reject"] and not adjusted["H3"]["reject"]


def test_join_preserves_valid_scenarios_and_does_not_write_databases(inputs):
    audit, truth = inputs
    hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs]
    rows, coverage = load(audit, truth)
    assert coverage["excluded"]["invalid_host_run"] == 6
    assert len({r["scenario_id"] for r in rows}) == 13
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs] == hashes
    db = connect(audit)
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM events")
    finally:
        db.close()


def test_unmatched_duplicate_and_ambiguous_results_are_refused(inputs, tmp_path):
    rows, _ = load(*inputs)
    row = {k: rows[0][k] for k in ("scenario_id", "level", "observed", "proposal_ids")}
    path = tmp_path / "replays.jsonl"
    path.write_text(json.dumps({**row, "scenario_id": "not-in-truth"}))
    with pytest.raises(ValueError, match="unmatched"):
        load(*inputs, path)
    path.write_text(json.dumps(row) + "\n" + json.dumps(row))
    with pytest.raises(ValueError, match="duplicate"):
        load(*inputs, path)
    row.pop("proposal_ids")
    path.write_text(json.dumps(row) + "\n" + json.dumps({**row, "repeat": 2}))
    with pytest.raises(ValueError, match="proposal_ids"):
        load(*inputs, path)


def test_ablation_paired_counts_band_tables_and_triplets(inputs):
    records, _ = load(*inputs)
    result = analyze(records)
    assert set(result["ablation"]) == {f"L{i}" for i in range(6)}
    assert result["ablation"]["L0"]["primary"]["H1_recall"]["estimate"] == 0
    assert result["ablation"]["L1"]["primary"]["H1_recall"]["estimate"] == 1
    assert result["ablation"]["L3"]["primary"]["H2_macro_f1"]["n"] == 5
    assert result["ablation"]["L3"]["primary"]["H3_localization_hit"]["n"] == 1
    assert result["comparisons"]["H1_L1_vs_L0"]["n"] == 3
    assert result["comparisons"]["H2_L3_vs_regex"]["n"] == 5
    assert result["comparisons"]["H3_L3_vs_L2"]["n"] == 1
    assert len(result["per_band"]) == 36
    variance = next(row for row in result["llm_variance"] if row["level"] == "L3")
    assert variance["complete_triplets"] == 5 and variance["expected_items"] == 60
    assert variance["summary"]["macro_f1"]["mean"] == 1
    incomplete = [r for r in records if not (r["level"] == "L3" and r["repeat"] == 3)]
    variance = next(row for row in llm_variance(incomplete) if row["level"] == "L3")
    assert variance["complete_triplets"] == 0 and variance["incomplete_items"] == 5


def test_late_approval_cannot_authorize_an_earlier_high_risk_execution(inputs):
    audit, truth = inputs
    writer = AuditLog(audit, "synthetic-ci-0", "L5")
    pid = "synthetic-unsafe"
    writer.proposal(
        {
            "id": pid,
            "incident_id": "unsafe",
            "agent": "release",
            "action": "rollout_undo",
            "target": "demo",
            "environment": "prod",
            "confidence": 0,
            "rationale": "fixture",
            "params": {},
            "proposed_at": "2026-01-01T00:00:00Z",
        }
    )
    writer.gate_decision(
        {
            "proposal_id": pid,
            "tier": "high",
            "risk_score": 0.7,
            "outcome": "ask_human",
            "reason": "fixture",
            "decided_at": "2026-01-01T00:00:01Z",
        }
    )
    writer.execution(
        {
            "proposal_id": pid,
            "success": True,
            "validated": True,
            "finished_at": "2026-01-01T00:00:02Z",
        }
    )
    writer._insert(
        "approvals",
        {
            "ts": "2026-01-01T00:00:03Z",
            "proposal_id": pid,
            "verdict": "approved",
            "approver": "fixture",
            "wait_seconds": 2,
        },
    )
    writer.event(
        "offline_replay",
        "replay_result",
        setting="unsafe-setting",
        proposal_ids=[pid],
        observed={"resolved": True, "gate_threshold": 0.5},
    )
    records, _ = load(audit, truth)
    unsafe = [r for r in records if r["setting"] == "unsafe-setting"]
    result = measurements(unsafe, seed=42)
    assert result["safety"]["unapproved_high_risk"] == 1
    assert result["primary"]["H5_autonomy"]["estimate"] == 0


def test_missing_data_are_null_not_successful_zeros():
    result = measurements([], seed=42)
    assert all(
        v["n"] == 0 and v["estimate"] is None and v["ci95"] is None
        for v in result["primary"].values()
    )
    assert result["secondary"]["false_alerts_per_hour"]["estimate"] is None
    assert (
        macro_f1(
            [(c, c) for c in ("code", "dependency", "environment", "flaky_test", "infrastructure")]
        )
        == 1
    )


def test_pipeline_anomaly_and_incident_only_autonomy_denominators():
    record = {
        "truth": {"family": "pipeline", "ground_truth": {"anomaly": True}},
        "observed": {"detected": True, "resolved": True},
        "audit": {
            "validated_execution": True,
            "approvals": 0,
            "human_requests": 0,
            "unapproved_high_risk": 0,
            "other_unsafe_executions": 0,
        },
    }
    assert detected(record) is True and autonomous(record) is True
    record["truth"] = {"family": "normal", "ground_truth": {}}
    assert detected(record) is None and autonomous(record) is None
    record["truth"] = {"family": "release", "ground_truth": {"bad_release": False}}
    assert autonomous(record) is None


def test_preregistration_03_holm_scope_and_h4_use_only_dated_dev_amendment(inputs):
    specification = {
        "version": 0.3,
        "comparisons": {"H2": {"primary": "none"}},
        "amendments": [],
    }
    pending = policy(specification)
    assert not pending["h2_primary"] and pending["h4_mode"] == "pending_amendment"
    assert len(pending["holm_family"]) == 3
    records, _ = load(*inputs)
    result = comparisons(records, "default", 31, 0.05, pending)
    assert result["H4_L4_vs_L3"]["n"] == 3
    assert result["H4_L4_vs_L3"]["p_value"] is None
    assert result["H2_L3_vs_regex"]["p_value"] is None
    assert result["H2_L3_vs_regex"]["holm"] is None
    # Held-out wrong/right outcomes do not decide H4's mode. Only the prior dev amendment can.
    specification["amendments"] = [{"date": "2026-10-28", "H4": {"dev_wrong_rate": 0.09}}]
    descriptive = policy(specification)
    assert descriptive["h4_mode"] == "descriptive" and len(descriptive["holm_family"]) == 2
    specification["amendments"][0]["H4"]["dev_wrong_rate"] = 0.10
    assert policy(specification)["h4_mode"] == "test"
    specification["amendments"][0].pop("date")
    with pytest.raises(ValueError, match="dated"):
        policy(specification)


def test_encoder_and_tfidf_pair_at_same_level_without_triple_counting_retrieval(inputs):
    records, _ = load(*inputs)
    tfidf = [r for r in records if r["method"] == "tfidf_lr" and r["truth"]["family"] == "ci"]
    records.extend({**r, "method": "encoder"} for r in tfidf)
    a, b = method_pairs(records, "encoder", "tfidf_lr", "default")
    assert len(a) == len(b) == 5 and a == b
    a, b = method_pairs(records, "encoder", "llm_retrieval", "default")
    assert len(a) == len(b) == 5  # L4/L5 cached copies do not add 10 extra incidents.
    incomplete = {**tfidf[0], "method": "encoder", "observed": {}}
    assert not primary_method(incomplete)


def test_timestamp_mttd_is_shared_by_summaries_and_paired_comparisons():
    record = {
        "truth": {
            "family": "runtime",
            "ground_truth": {"incident": {"start": "2026-01-01T00:00:00Z"}},
        },
        "observed": {"detected": True, "detected_at": "2026-01-01T00:00:05Z"},
    }
    assert mttd(record) == 5
    record["observed"]["detected"] = False
    assert mttd(record) is None
    record["observed"].update(detected=True, detected_at="2025-12-31T23:59:59Z")
    with pytest.raises(ValueError, match="precedes"):
        mttd(record)


def test_an_unsafe_execution_is_counted_once_even_with_multiple_missing_authorizations(inputs):
    from flowguard.catalog import AGENT_ACTIONS, CATALOG

    action = next(name for name, entry in CATALOG.items() if str(entry.tier) == "medium")
    agent = next(agent for agent, actions in AGENT_ACTIONS.items() if action in actions)
    audit, _ = inputs
    writer = AuditLog(audit, "synthetic-ci-0", "L5")
    pid = "synthetic-missing-medium-authorization"
    writer.proposal(
        {
            "id": pid,
            "incident_id": "fixture",
            "agent": agent.value,
            "action": action,
            "target": "demo",
            "environment": "prod",
            "confidence": 0,
            "rationale": "synthetic",
            "params": {},
            "proposed_at": "2026-01-01T00:00:00Z",
        }
    )
    writer.execution(
        {
            "proposal_id": pid,
            "success": True,
            "validated": True,
            "finished_at": "2026-01-01T00:00:01Z",
        }
    )
    db = connect(audit)
    try:
        result = audit_outcomes(
            db,
            {
                "scenario_id": "synthetic-ci-0",
                "level": "L5",
                "proposal_ids": [pid],
            },
            False,
        )
    finally:
        db.close()
    assert result["executions"] == result["other_unsafe_executions"] == 1


def test_one_command_writes_all_outputs_from_synthetic_inputs(inputs, tmp_path):
    audit, truth = inputs
    out = tmp_path / "out"
    assert report.main(["--audit", str(audit), "--truth", str(truth), "--out", str(out)]) == 0
    result = json.loads((out / "results.json").read_text())
    assert result["metadata"]["bootstrap_resamples"] == 10000
    assert not result["metadata"]["confirmatory"]  # repository preregistration is still draft
    assert len(result["figures"]) == 8
    assert len((out / "ablation.csv").read_text().splitlines()) == (
        6 * len(result["metadata"]["primary_metrics"]) + 1
    )
    for name in result["figures"]:
        path = out / name
        assert path.stat().st_size > 1000
        if name.endswith("png"):
            assert path.read_bytes().startswith(b"\x89PNG")
        else:
            assert "<svg" in path.read_text()
    again = report.build(audit, truth, tmp_path / "again", make_plots=False)
    assert result["ablation"] == again["ablation"]
    assert result["comparisons"] == again["comparisons"]
