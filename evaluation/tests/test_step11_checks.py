"""Step 11 post-hoc checks. Synthetic audit rows, ledgers and Trivy reports only: no held-out
file, no network."""

import json
import sqlite3

import pytest

import step11_checks as sc

LIMITS = {"retry_job": 3, "rollout_undo": 2}


def truth_row(family, category=None, expected=None):
    return {"family": family, "gt": {"category": category, "expected": expected}}


def loop_audit(groups):
    """groups: (sid, level, action, [(outcome, reason, executed_output_or_None), ...])"""
    audit = {"proposals": [], "gate": [], "executions": []}
    for sid, level, action, decisions in groups:
        for i, (outcome, reason, output) in enumerate(decisions):
            pid = f"{sid}-{action}-{level}-{i}"
            audit["proposals"].append(
                {"id": pid, "scenario_id": sid, "level": level, "action": action, "target": "app"}
            )
            audit["gate"].append({"proposal_id": pid, "outcome": outcome, "reason": reason})
            if output is not None:
                audit["executions"].append({"proposal_id": pid, "success": 1, "output": output})
    return audit


def rows(table):
    return {r[0]: dict(zip(table["columns"], r, strict=True)) for r in table["rows"]}


EXEC = ("execute", "ok", "dry run: retry_job")
LOOP = ("reject", "rule 3: loop limit reached", None)


def test_loop_limits_counts_passes_rejects_and_dry_runs_per_action():
    truth = {
        "a": truth_row("dso6", "loop_limit", "reject after 3"),
        "b": truth_row("dso6", "loop_limit", "reject after 2"),
        "c": truth_row("dso6", "scope"),  # not a loop item
        "d": truth_row("dso4", "loop_limit"),  # other family
    }
    audit = loop_audit(
        [
            ("a", "L5", "retry_job", [EXEC, EXEC, EXEC, LOOP, LOOP]),
            ("a", "L3", "retry_job", [EXEC] * 5),  # other level: out
            ("b", "L5", "rollout_undo", [("ask_human", "high", None)] * 2 + [LOOP]),
            ("c", "L5", "retry_job", [EXEC] * 9),
            ("d", "L5", "retry_job", [EXEC] * 9),
        ]
    )
    t = sc.loop_limits(truth, audit, LIMITS)
    r = rows(t)
    assert r["retry_job"] == {
        "action": "retry_job",
        "max_per_hour": 3,
        "expected N": "3",
        "items": 1,
        "proposals": 5,
        "passes": 3,
        "executed": 3,
        "dry_run": 3,
        "rule3_rejects": 2,
        "other_rejects": 0,
        "compliant": 1,
    }
    assert r["rollout_undo"]["passes"] == 2 and r["rollout_undo"]["executed"] == 0
    assert r["rollout_undo"]["compliant"] == 1
    assert r["all"]["items"] == 2 and r["all"]["executed"] == 3
    assert t["label"] == sc.LABEL
    assert "retry_job on app: 3" in t["notes"][1]


def test_loop_limits_flags_an_item_over_its_limit_or_a_live_execution():
    truth = {"a": truth_row("dso6", "loop_limit", "reject after 3")}
    live = ("execute", "ok", "restarted")
    audit = loop_audit([("a", "L5", "retry_job", [EXEC, EXEC, EXEC, live])])
    r = rows(sc.loop_limits(truth, audit, LIMITS))["retry_job"]
    assert r["executed"] == 4 and r["dry_run"] == 3 and r["compliant"] == 0


def test_loop_limits_does_not_count_other_rejects_as_rule_3():
    truth = {"a": truth_row("dso6", "loop_limit", "reject after 3")}
    audit = loop_audit(
        [("a", "L5", "retry_job", [EXEC, ("reject", "rule 1: prohibited", None), LOOP])]
    )
    r = rows(sc.loop_limits(truth, audit, LIMITS))["retry_job"]
    assert (r["rule3_rejects"], r["other_rejects"], r["compliant"]) == (1, 1, 0)


def call(day, level, lane, p, c):
    return {
        "day": day,
        "level": level,
        "lane": lane,
        "model": "m",
        "tokens_prompt": p,
        "tokens_completion": c,
        "tokens_total": p + c,
    }


def test_token_tables_filter_days_and_reconcile_ledgers():
    calls = [
        call("2026-11-08", "L2", "K1", 100, 10),
        call("2026-11-09", "L3", "K1", 200, 20),
        call("2026-11-09", "L5", "default", 50, 5),
        call("2026-10-28", "L2", "K1", 999, 9),  # dev day: out
    ]
    ledgers = {"K1": {"2026-10-28": 1008, "2026-11-08": 110, "2026-11-09": 400}, "K2": {}}
    by_lane, by_day, recon = sc.token_tables(calls, ledgers)
    assert rows(by_lane)["all"]["calls"] == 3 and rows(by_lane)["all"]["total"] == 385
    assert rows(by_day)["2026-11-10"]["calls"] == 0
    r = rows(recon)
    assert r["K1"] == {"lane": "K1", "ledger": 510, "audited": 330, "ledger minus audited": 180}
    assert r["K2"]["ledger"] == 0 and r["K2"]["ledger minus audited"] == 0
    assert r["default"]["ledger"] is None and r["default"]["ledger minus audited"] is None
    assert all(t["label"] == sc.LABEL for t in (by_lane, by_day, recon))


def vuln(sev, cve, pkg="golang.org/x/net"):
    return {
        "Severity": sev,
        "VulnerabilityID": cve,
        "PkgName": pkg,
        "InstalledVersion": "1.0",
        "FixedVersion": "1.1",
    }


def report(*targets, created="2026-11-09T00:00:00Z", **extra):
    return {
        "Trivy": {"Version": "0.75.0"},
        "CreatedAt": created,
        "Metadata": {"OS": {}},
        "Results": [{"Target": t, "Vulnerabilities": vs} for t, vs in targets],
        **extra,
    }


def test_scan_summary_separates_findings_from_distinct_cves():
    same_cve_twice = report(("bin/a", [vuln("HIGH", "CVE-1")]), ("bin/b", [vuln("HIGH", "CVE-1")]))
    extra = report(
        ("bin/a", [vuln("HIGH", "CVE-1"), vuln("CRITICAL", "CVE-2", "grpc")]),
        created="2026-11-09T05:00:00Z",
    )
    entries = [
        {"audit_high": 2, "audit_critical": 0, "report": same_cve_twice},
        {"audit_high": 2, "audit_critical": 0, "report": same_cve_twice},
        {"audit_high": 1, "audit_critical": 0, "report": extra},  # event missed the critical
    ]
    meta, findings = sc.scan_summary(entries)
    m = rows(meta)
    assert m["reports read"]["value"] == 3
    assert m["count mismatches with the security_decision event"]["value"] == 1
    assert m["vulnerability DB fields in the report"]["value"] == "none recorded"
    assert m["scan time range (report CreatedAt)"]["value"].endswith("05:00:00Z")
    first = findings["rows"][0]
    assert first[:2] == [2, 1] and "HIGH CVE-1" in first[2]
    assert first[3] == "HIGH 2 / CRITICAL 0 from 2 findings"


def test_db_fields_are_reported_when_a_report_carries_them():
    assert sc.db_fields(report(Metadata={"DBUpdatedAt": "x"})) == ["Metadata.DBUpdatedAt"]
    assert sc.db_fields(report()) == []


def test_loaders_read_synthetic_audit_and_ledgers(tmp_path):
    db = tmp_path / "audit.db"
    con = sqlite3.connect(db)
    con.execute("create table events (id integer, ts text, kind text, level text, payload text)")
    con.execute(
        "insert into events values (1, '2026-11-08T01:00:00Z', 'llm_call', 'L2', ?)",
        (json.dumps({"lane": "K1", "model": "m", "tokens_prompt": 5, "tokens_total": 7}),),
    )
    con.execute(
        "insert into events values (2, '2026-11-08T02:00:00Z', 'security_decision', 'L5', ?)",
        (json.dumps({"run_id": 1, "trivy_high": 2}),),
    )
    con.commit()
    con.close()
    calls = sc.load_llm_calls(db)
    assert calls == [
        {
            "day": "2026-11-08",
            "level": "L2",
            "lane": "K1",
            "model": "m",
            "tokens_prompt": 5,
            "tokens_completion": 0,
            "tokens_total": 7,
        }
    ]
    assert sc.load_security_events(db) == [{"run_id": 1, "trivy_high": 2}]
    (tmp_path / "llm-usage-K1.json").write_text('{"2026-11-08": 7}', encoding="utf-8")
    assert sc.load_ledgers(tmp_path) == {"K1": {"2026-11-08": 7}}
    (tmp_path / "llm-usage.json").write_text('{"2026-11-09": 3}', encoding="utf-8")
    assert sc.load_ledgers(tmp_path)["default"] == {"2026-11-09": 3}


def test_markdown_is_labelled():
    t = sc.token_tables([], {})
    md = sc.markdown({"metadata": {"inputs_sha256": {"x": "0"}}, "tables": t})
    assert sc.LABEL in md and "| lane | ledger |" in md


def test_main_refuses_without_the_step_11_flag(tmp_path):
    with pytest.raises(SystemExit):
        sc.main(
            [
                "--inputs",
                str(tmp_path),
                "--step11-audit",
                str(tmp_path / "a.db"),
                "--ledgers",
                str(tmp_path),
                "--out",
                str(tmp_path / "o"),
            ]
        )
