"""The observation builder's hallucination fields and the per-quote rate, on a synthetic audit
log only: hallucinated comes from diagnose_ungrounded, never from diagnose_rejected or grounded
line numbers, at L2 and L3 alike."""

import json

import pytest

import observations
import stats
from flowguard.audit import AuditLog
from report_data import connect, validate_observed
from report_metrics import hallucination_rate, measurements, pairs_for


def answer(quotes, ungrounded, rejected, method="llm_zero_shot", **extra):
    return {
        "diagnose_method": method,
        "diagnose_quotes": [f"quoted line {i}" for i in range(quotes)],
        "diagnose_ungrounded": ungrounded,
        "diagnose_grounding": method == "llm_retrieval",
        "diagnose_rejected": rejected,
        **extra,
    }


# (scenario, level, agent, params). Rejection and grounded lines disagree with the raw count
# on purpose: the builder must follow diagnose_ungrounded only.
PROPOSALS = [
    ("s1", "L2", "diagnose", answer(4, 1, False, evidence_lines=[3, 4, 5, 6])),
    ("s1", "L3", "diagnose", answer(3, 0, True, "llm_retrieval", evidence_lines=[])),
    ("s2", "L2", "diagnose", answer(5, 2, False)),
    ("s2", "L3", "diagnose", answer(2, 2, True, "llm_retrieval", evidence_lines=[7])),
    ("s3", "L1", "diagnose", {"diagnose_method": "tfidf_lr"}),  # no LLM answer
    ("s4", "L2", "diagnose", {"diagnose_method": "no_log"}),  # D60: no model asked
    ("s1", "L2", "security", {"diagnose_ungrounded": 9}),  # another agent: ignored
]


@pytest.fixture
def audit(tmp_path):
    path = tmp_path / "audit.db"
    for i, (scenario, level, agent, params) in enumerate(PROPOSALS):
        AuditLog(path, scenario, level).proposal(
            {"id": f"p-{i}", "incident_id": f"i-{i}", "agent": agent, "action": "escalate_to_human",
             "target": "podinfo", "environment": "staging", "confidence": 0,
             "rationale": "synthetic", "params": params,
             "proposed_at": "2026-01-01T00:00:00Z"}
        )  # fmt: skip
    return path


def test_the_builder_takes_hallucinated_from_ungrounded_quotes_only(audit, tmp_path):
    out = tmp_path / "records.jsonl"
    assert observations.build(audit, out) == 6
    built = [json.loads(line) for line in out.read_text().splitlines()]
    by_key = {(r["scenario_id"], r["level"]): r for r in built}
    assert by_key[("s1", "L2")]["observed"] == {
        "diagnose_quotes": 4, "diagnose_ungrounded": 1, "hallucinated": True
    }  # fmt: skip
    # rejected at L3, but none of its raw quotes is missing from the log: not hallucinated
    assert by_key[("s1", "L3")]["observed"] == {
        "diagnose_quotes": 3, "diagnose_ungrounded": 0, "hallucinated": False
    }  # fmt: skip
    # not rejected and with grounded lines at L2, yet two raw quotes are not in the log
    assert by_key[("s2", "L2")]["observed"]["hallucinated"] is True
    assert by_key[("s2", "L3")]["observed"]["hallucinated"] is True
    assert by_key[("s3", "L1")] == {
        "scenario_id": "s3", "level": "L1", "observed": {}, "method": "tfidf_lr"
    }  # fmt: skip
    assert "method" not in by_key[("s4", "L2")] and by_key[("s4", "L2")]["observed"] == {}
    assert {r["method"] for r in built if r["level"] == "L3"} == {"llm_retrieval"}


def test_the_rejection_flag_alone_never_changes_the_fields():
    for rejected in (True, False):
        assert observations.hallucination(answer(3, 0, rejected)) == {
            "diagnose_quotes": 3, "diagnose_ungrounded": 0, "hallucinated": False
        }  # fmt: skip
        assert observations.hallucination(answer(3, 1, rejected))["hallucinated"] is True
    assert observations.hallucination({"diagnose_rejected": True}) == {}
    assert observations.hallucination(answer(0, 0, True)) == {
        "diagnose_quotes": 0,
        "diagnose_ungrounded": 0,
        "hallucinated": False,
    }  # fmt: skip  (no quotes: rejected at L3, but nothing hallucinated)


def test_inconsistent_raw_fields_are_refused(audit):
    for bad in (answer(2, 3, False), {**answer(2, 1, False), "diagnose_ungrounded": "1"}):
        with pytest.raises(ValueError):
            observations.hallucination(bad)
    for observed in (
        {"diagnose_quotes": 2},
        {"diagnose_quotes": 2, "diagnose_ungrounded": 3},
        {"diagnose_quotes": 2, "diagnose_ungrounded": 1, "hallucinated": False},
    ):
        with pytest.raises(ValueError):
            validate_observed(observed)
    AuditLog(audit, "s1", "L2").proposal(
        {"id": "p-repeat", "incident_id": "i", "agent": "diagnose", "action": "retry_job",
         "target": "podinfo", "environment": "staging", "confidence": 0, "rationale": "r",
         "params": answer(1, 0, False), "proposed_at": "2026-01-01T00:00:00Z"}
    )  # fmt: skip
    db = connect(audit)
    try:
        with pytest.raises(ValueError, match="2 Diagnose answers"):
            observations.records(db)
    finally:
        db.close()


def test_the_rate_is_per_quote_and_the_boolean_feeds_mcnemar(audit):
    db = connect(audit)
    try:
        built = observations.records(db)
    finally:
        db.close()
    records = []
    for r in built:
        truth = {"family": "ci", "ground_truth": {"root_cause": "dependency"}, "spec": {}}
        audited = dict.fromkeys(
            ("proposals", "approvals", "human_requests", "executions", "high_risk_executions",
             "unapproved_high_risk", "other_unsafe_executions"), 0
        )  # fmt: skip
        audited.update(actions=[], validated_execution=False)
        records.append({**r, "setting": "default", "repeat": 1, "truth": truth, "audit": audited})
        records[-1].setdefault("method", "default")
    l2 = [r for r in records if r["level"] == "L2"]
    l3 = [r for r in records if r["level"] == "L3"]
    # L2: (1 + 2) of (4 + 5) quotes; L3: (0 + 2) of (3 + 2); the boolean would say 2/2 and 1/2
    rate = hallucination_rate(l2)
    assert (rate["k"], rate["n"], rate["items"], rate["unit"]) == (3, 9, 2, "quote")
    assert rate["ci95"] == list(stats.wilson(3, 9)) and rate["interval"] == "Wilson"
    assert (hallucination_rate(l3)["k"], hallucination_rate(l3)["n"]) == (2, 5)
    assert measurements(l3, seed=1)["secondary"]["hallucination_rate"]["n"] == 5
    assert hallucination_rate([r for r in records if r["level"] == "L1"])["n"] == 0
    # exact McNemar on the per-item boolean, paired by scenario (s1, s2)
    left, right = pairs_for(records, "L3", "L2", lambda r: r["observed"].get("hallucinated"),
                            "default")  # fmt: skip
    assert (left, right) == ([False, True], [True, True])
    assert stats.mcnemar_paired(left, right)["right_only"] == 1
