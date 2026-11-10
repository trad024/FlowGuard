"""D82: exploratory held-out tables. Synthetic truth, replay and audit rows only: no held-out
file, no network."""

import json
import sqlite3

import pytest

import exploratory_heldout as ex


def run(family, valid=True, recording="", gt=None, spec=None):
    return {
        "family": family,
        "recording": recording,
        "valid": valid,
        "gt": gt or {},
        "spec": spec or {},
    }


def rec(sid, level, observed, **kw):
    r = {"scenario_id": sid, "level": level, "observed": observed, "repeat": 1}
    r.update(kw)
    r.setdefault("method", observed.get("method"))
    return r


def det(sid, level, detected, mttd=None):
    return rec(sid, level, {"detected": detected, "mttd_s": mttd})


def cells(table):
    return {tuple(r[:1]): r for r in table["rows"]}


def test_every_table_is_labelled():
    truth = {"x": run("runtime")}
    recs = [det("x", "L0", True, 10), det("x", "L1", True, 5)]
    audit = {k: [] for k in ("proposals", "gate", "approvals", "executions", "security")}
    tables = ex.build(truth, recs, audit, {}, {"B": "b"})
    assert len(tables) == 13
    assert all(t["label"] == "exploratory, not pre-registered toolkit output" for t in tables)
    assert ex.LABEL in ex.markdown({"metadata": {"inputs_sha256": {}}, "tables": tables})


def test_mttd_is_median_of_paired_differences_per_family():
    truth = {f"r{i}": run("runtime") for i in range(3)} | {"p0": run("pipeline")}
    truth["bad"] = run("runtime", valid=False)
    recs = []
    for i, (a, b) in enumerate([(100, 10), (50, 40), (30, 30)]):
        recs += [det(f"r{i}", "L0", True, a), det(f"r{i}", "L1", True, b)]
    recs += [det("p0", "L0", False), det("p0", "L1", True, 5)]  # not detected at both
    recs += [det("bad", "L0", True, 1), det("bad", "L1", True, 999)]  # invalid run
    recs.append(det("r0", "L1", True, 1) | {"repeat": 2})  # repeats never enter
    t = ex.mttd_per_family(truth, recs, {"runtime": {"L1_vs_L0": {"mttd_gain_s_median": 10}}})
    runtime, pipeline = t["rows"]
    assert runtime[:5] == ["runtime", 3, 50.0, 30.0, -10.0]  # diffs -90, -10, 0
    assert runtime[5][0] <= -10.0 <= runtime[5][1]
    assert pipeline[1] == 0 and pipeline[4] is None
    assert "mttd_gain_s_median" in t["notes"][0]


def test_h4_counts_false_rollbacks_on_node_stress_good_releases_only():
    stress = {"pod": "p"}
    truth = {
        "g1": run("release", gt={"bad_release": False, "node_stress": stress}),
        "g2": run("release", gt={"bad_release": False}, spec={"node_stress": stress}),
        "g3": run("release", gt={"bad_release": False}),  # no stress: out
        "b1": run("release", gt={"bad_release": True, "node_stress": stress}),
    }
    recs = []
    for level, outs in {
        "L3": ("aborted", "promoted", "aborted", "aborted"),
        "L4": ("promoted", "promoted", "aborted", "escalated"),
    }.items():
        for sid, o in zip(("g1", "g2", "g3", "b1"), outs, strict=True):
            recs.append(rec(sid, level, {"release_outcome": o}))
    rows = cells(ex.h4(truth, recs))
    assert rows[("L3",)][1]["k"] == 1 and rows[("L3",)][1]["n"] == 2
    assert rows[("L4",)][1]["k"] == 0
    assert rows[("L4",)][4] == {"escalated": 1}
    assert rows[("L0",)][1]["n"] == 0


def ci_audit():
    proposals = [
        {"id": "p1", "scenario_id": "c1", "level": "L5", "action": "open_fix_pr"},
        {"id": "p2", "scenario_id": "c2", "level": "L5", "action": "retry_job"},
        {"id": "p3", "scenario_id": "c3", "level": "L5", "action": "retry_job"},
        {"id": "p4", "scenario_id": "c4", "level": "L5", "action": "escalate_to_human"},
        {"id": "p5", "scenario_id": "c5", "level": "L5", "action": "rollback"},
        {"id": "p6", "scenario_id": "c1", "level": "L3", "action": "retry_job"},  # not L5
    ]
    gate = [{"proposal_id": f"p{i}", "tier": "low", "outcome": "execute"} for i in range(1, 5)]
    gate.append({"proposal_id": "p5", "tier": "high", "outcome": "ask_human"})
    executions = [
        {"proposal_id": "p1", "success": 1, "validated": None},
        {"proposal_id": "p2", "success": 1, "validated": None},
        {"proposal_id": "p3", "success": 1, "validated": 1},
        {"proposal_id": "p4", "success": 1, "validated": None},
    ]
    return {
        "proposals": proposals,
        "gate": gate,
        "approvals": [],
        "executions": executions,
        "security": [],
    }


def test_h5_classifies_live_ci_outcomes():
    truth = {f"c{i}": run("ci") for i in range(1, 6)}
    t = ex.h5_ci(truth, ci_audit())
    got = {(r[0], r[2]): r[3] for r in t["rows"][:-1]}
    assert got == {
        ("open_fix_pr", "assisted (issue or draft PR)"): 1,
        ("retry_job", "executed, not validated"): 1,
        ("retry_job", "resolved without a human"): 1,
        ("escalate_to_human", "escalated to a human"): 1,
        ("rollback", "not executed (gate ask_human)"): 1,
    }
    assert t["rows"][-1][3]["k"] == 1 and t["rows"][-1][3]["n"] == 5
    assert t["notes"] == ["unapproved high-risk actions: 0"]


def test_dso4_blocks_by_kind_and_baseline():
    def ev(i, sid, decision, high=2, gitleaks=0):
        payload = {"decision": decision, "trivy_high": high, "trivy_critical": 0}
        payload["gitleaks"] = gitleaks
        return {"id": i, "scenario_id": sid, "payload": json.dumps(payload)}

    truth = {
        "s1": run("security", gt={"bad_release": True}, spec={"fault": {"kind": "leaked_secret"}}),
        "s2": run(
            "security",
            gt={"bad_release": False},
            spec={"fault": {"kind": "low_severity_finding"}},
        ),
        "p1": run("pipeline"),
    }
    audit = {
        "security": [
            ev(1, "s1", "promote"),
            ev(2, "s1", "block", gitleaks=1),
            ev(3, "s2", "block"),
            ev(4, "p1", "block"),
        ]
    }
    kinds, summary, pipe = ex.dso4(truth, audit)
    rows = cells(kinds)
    assert rows[("leaked_secret",)][1:6] == [1, 1, 1, 1, 0]
    assert summary["rows"][0][1]["k"] == 1 and summary["rows"][1][1]["k"] == 1
    assert pipe["rows"] == [["block", 0, 2, 0, 1]]
    assert kinds["notes"] == ["scenarios with more than one security_decision: 1"]


def test_dso6_held_precision_and_approvals():
    truth = {
        "h": run("dso6", gt={"category": "high_risk_without_approval", "expected": "ask_human"}),
        "m": run("dso6", gt={"category": "medium_in_prod", "expected": "ask_human"}),
        "l": run("dso6", gt={"category": "loop_limit", "expected": "reject after 3 in an hour"}),
    }
    audit = {
        "proposals": [
            {"id": 1, "scenario_id": "h", "level": "L5", "action": "a"},
            {"id": 2, "scenario_id": "m", "level": "L5", "action": "a"},
            {"id": 3, "scenario_id": "l", "level": "L5", "action": "a"},
            {"id": 4, "scenario_id": "l", "level": "L5", "action": "a"},
        ],
        "gate": [
            {"proposal_id": 1, "tier": "high", "outcome": "ask_human"},
            {"proposal_id": 2, "tier": "medium", "outcome": "ask_human"},
            {"proposal_id": 3, "tier": "medium", "outcome": "ask_human"},
            {"proposal_id": 4, "tier": "high", "outcome": "execute"},
        ],
        "approvals": [
            {"proposal_id": 1, "verdict": "denied"},
            {"proposal_id": 2, "verdict": "denied"},
            {"proposal_id": 3, "verdict": "approved"},
        ],
        "executions": [{"proposal_id": 4, "success": 1, "validated": None}],
        "security": [],
    }
    summary, by_cat = ex.dso6(truth, audit)
    v = {r[0].split(" (")[0]: r[1] for r in summary["rows"]}
    assert (v["high-risk proposals held"]["k"], v["high-risk proposals held"]["n"]) == (1, 2)
    assert v["unapproved high-risk actions"] == 1
    assert (v["escalation precision"]["k"], v["escalation precision"]["n"]) == (2, 3)
    assert v["ask-human items escalated, none executed"]["k"] == 2
    assert v["approvals granted / approval requests"]["k"] == 1
    assert v["approval requests per dso6 incident"] == "3/3"
    assert cells(by_cat)[("loop_limit",)][1]["executed"] == 1


def test_h2_corpus_only_and_paired_difference():
    classes = ["code", "dependency", "environment", "flaky_test", "infrastructure"]
    truth = {f"d{i}": run("diagnose", gt={"root_cause": c}) for i, c in enumerate(classes)}
    truth["ci1"] = run("ci", gt={"root_cause": "code"})
    truth["lc"] = run("diagnose", gt={"localizable": True})  # LogChunks: no root_cause
    recs = []
    for i, c in enumerate(classes):
        recs.append(rec(f"d{i}", "L3", {"root_cause": c}, method="llm_retrieval"))
        recs.append(rec(f"d{i}", "L0", {"root_cause": "code"}, method="regex"))
        recs.append(rec(f"d{i}", "L2", {"root_cause": c}, method="llm_zero_shot"))
    recs.append(rec("ci1", "L3", {"root_cause": "dependency"}, method="llm_retrieval"))
    recs.append(rec("lc", "L3", {"root_cause": "code"}, method="llm_retrieval"))
    rows = {r[0]: r for r in ex.h2_corpus(truth, recs)["rows"]}
    assert rows["L3 llm_retrieval"][1] == 5 and rows["L3 llm_retrieval"][3] == 1.0
    assert rows["L0 regex"][2]["k"] == 1
    assert rows["L1 tfidf_lr"][1] == 0 and rows["L1 tfidf_lr"][4] is None
    assert rows["L2 llm_zero_shot"][4] == 0.0
    assert rows["L0 regex"][4] == pytest.approx(1.0 - ex.macro_f1([(c, "code") for c in classes]))


def test_dso1_variants():
    truth = {
        "a": run("runtime"),
        "b": run("runtime"),
        "c": run("runtime"),
        "p": run("pipeline"),
        "q": run("pipeline"),  # valid, no replay: a miss at both levels
        "z": run("pipeline", valid=False),
    }
    recs = [det("a", "L0", False), det("a", "L1", True)]
    recs += [det("b", "L0", True), det("b", "L1", True)]
    recs += [det("c", "L0", False), det("c", "L1", True)]
    recs += [det("p", "L0", True), det("p", "L1", True)]
    t = ex.dso1(truth, recs, ["c", "elsewhere"])
    rows = {r[0]: r for r in t["rows"]}
    base = rows["as report.py (paired replays)"]
    assert base[1] == 4 and base[2]["k"] == 2 and base[3]["k"] == 4 and base[4:6] == [2, 0]
    miss = rows["valid runs without a replay counted as misses"]
    assert miss[1] == 5 and miss[2]["k"] == 2 and miss[3]["k"] == 4
    without = rows["carry-over incidents excluded"]
    assert without[1] == 3 and without[3]["k"] == 3
    assert rows["runtime, carry-over excluded"][1] == 2
    assert t["notes"][1] == "carry-over incidents listed: 2, of them in the paired set: 1"


def test_laptop_mapping_and_breakdown():
    mapping = {"B": "Hakim", "C": "mo"}
    assert ex.laptop_of("C:\\Users\\hakim\\FlowGuard\\r", mapping) == "B"
    assert ex.laptop_of("C:/Users/Mo/x", mapping) == "C"
    assert ex.laptop_of("C:/Users/hp/x", mapping) is None
    truth = {
        "r1": run("runtime", recording="C:/Users/hakim/r1"),
        "r2": run("runtime", recording="C:/Users/mo/r2"),
        "e1": run("release", recording="C:/Users/hakim/e1", gt={"bad_release": True}),
        "e2": run("release", recording="C:/Users/mo/e2", gt={"bad_release": False}),
    }
    recs = [det("r1", "L0", True, 50), det("r1", "L1", True, 10)]
    recs += [det("r2", "L0", False), det("r2", "L1", True, 20)]
    for level in ex.RELEASE_LEVELS:
        recs.append(rec("e1", level, {"release_outcome": "aborted", "caught_before_full": True}))
        recs.append(rec("e2", level, {"release_outcome": "aborted", "caught_before_full": False}))
    rt, rel = ex.per_laptop(truth, recs, mapping)
    assert rt["rows"][0][:2] == ["B", 1] and rt["rows"][0][3] == 50
    assert rt["rows"][1][2]["k"] == 0 and rt["rows"][1][5] == 20
    b_l3 = [r for r in rel["rows"] if r[0] == "B" and r[1] == "L3"][0]
    c_l3 = [r for r in rel["rows"] if r[0] == "C" and r[1] == "L3"][0]
    assert b_l3[3]["k"] == 1 and c_l3[4]["k"] == 1


def test_dso3_change_failure_rate():
    truth = {
        "b1": run("release", gt={"bad_release": True}),
        "b2": run("release", gt={"bad_release": True}),
        "g1": run("release", gt={"bad_release": False}),
        "g2": run("release", gt={"bad_release": False}),
    }
    obs = {
        "b1": {"release_outcome": "aborted", "caught_before_full": True, "time_to_rollback_s": 60},
        "b2": {"release_outcome": "promoted", "caught_before_full": False},
        "g1": {"release_outcome": "promoted", "caught_before_full": False},
        "g2": {"release_outcome": "escalated", "caught_before_full": False},
    }
    recs = [rec(s, "L4", o) for s, o in obs.items()]
    row = cells(ex.dso3(truth, recs))[("L4",)]
    assert row[1]["k"] == 1 and row[2] == 1 and row[3] == 1
    assert row[4]["k"] == 0 and row[5] == 60
    assert (row[6]["k"], row[6]["n"]) == (1, 2)


def test_loaders_and_log_encodings(tmp_path):
    truth_db = tmp_path / "groundtruth.db"
    con = sqlite3.connect(truth_db)
    con.execute(
        "create table runs (run_id, spec_id, family, recorded_at, recording, spec, "
        "ground_truth, observed, l0_correct, valid)"
    )
    con.execute(
        "insert into runs values ('r', 's', 'runtime', 't', 'x', '{}', '{\"band\": 1}', '{}', 1, 1)"
    )
    con.commit()
    con.close()
    assert ex.load_truth(truth_db)["r"]["gt"] == {"band": 1}
    replay = tmp_path / "replay.jsonl"
    replay.write_text(json.dumps({"scenario_id": "r", "level": "L0", "observed": {}}) + "\n\n")
    assert ex.load_replay(replay)[0]["repeat"] == 1
    for enc in ("utf-16", "utf-8-sig", "utf-8"):
        log = tmp_path / f"{enc}.log"
        log.write_bytes('noise\n{"a": 1}'.encode(enc))
        assert ex.read_log_json(log) == {"a": 1}


def test_main_refuses_without_the_step_11_flag(tmp_path):
    with pytest.raises(SystemExit, match="Step 11 only"):
        ex.main(["--inputs", str(tmp_path), "--monitor-log", "x", "--out", str(tmp_path / "o")])
    assert not (tmp_path / "o").exists()


def test_parse_mapping_rejects_malformed():
    assert ex.parse_mapping(["B=x", "C=y"]) == {"B": "x", "C": "y"}
    with pytest.raises(SystemExit):
        ex.parse_mapping(["B"])
