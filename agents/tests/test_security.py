"""Security hard rules, evidence handling, score explanations and real gate routing."""

from pathlib import Path

import pytest

from flowguard.catalog import AGENT_ACTIONS
from flowguard.contracts import ActionProposal, AgentName
from flowguard.gate import RiskGate
from flowguard.orchestrator import level
from flowguard.security import WEIGHTS, Security, cvss_base, normalize
from flowguard.stubs import AgentInput
from flowguard.stubs import Security as ConfiguredSecurity

SAMPLE = Path(__file__).resolve().parents[2] / "ci/security/epss-sample.csv"


def reports(severity=None, runtime=None, fixed="1.2.3", leaks=None):
    vulns = []
    if severity:
        vulns = [
            {
                "Severity": severity,
                "RuntimeDependency": runtime,
                "FixedVersion": fixed,
                "VulnerabilityID": "CVE-2026-56851",
                "CVSS": {"redhat": {"V3Score": 7.5 if severity == "HIGH" else 3.3}},
            }
        ]
    return {"trivy": {"Results": [{"Vulnerabilities": vulns}]}, "gitleaks": leaks or []}


def run(report=None, counts=None, epss=SAMPLE, **params):
    inp = AgentInput(
        incident_id="i-1",
        scan_counts=counts or {},
        params={"scan_reports": report, **params},
    )
    return Security(epss).run(inp, level("L1"))


@pytest.mark.parametrize("fixed", [None, "", "2.0"])
def test_critical_always_blocks_without_model(tmp_path, fixed):
    p = run(reports("CRITICAL", fixed=fixed), epss=tmp_path / "absent").proposals[0]
    assert p.action == "block_deployment" and "hard rule" in p.rationale


def test_any_secret_blocks_even_unverified_and_trivy_broken():
    p = run({"trivy": None, "gitleaks": [{"Secret": "do-not-copy", "Verified": False}]}).proposals[
        0
    ]
    assert p.action == "block_deployment"
    assert "do-not-copy" not in p.model_dump_json()


@pytest.mark.parametrize("key", ["gitleaks", "trivy_critical", "trivy_high"])
def test_positive_summary_cannot_be_hidden_by_clean_artifact(key):
    p = run(reports(), {"trivy_high": 0, "trivy_critical": 0, "gitleaks": 0, key: 1}).proposals[0]
    assert p.action == "block_deployment"


@pytest.mark.parametrize("bad", [None, {}, {"trivy_high": -1}, {"trivy_high": "x"}])
def test_missing_or_invalid_evidence_requests_review(bad):
    assert run(counts=bad).proposals[0].action == "request_security_review"


def test_annotation_and_raw_reports():
    out = run(
        scan_annotation="::notice title=scan-counts::trivy_high=0 trivy_critical=0 gitleaks=0"
    )
    assert out.proposals[0].action == "allow_deployment"
    scan = normalize(reports=reports("MEDIUM", False, ""))
    assert not scan.errors and scan.findings[0].runtime_dependency is False
    assert scan.findings[0].fix_available is False
    assert scan.evidence[0]["cvss"] == 3.3


@pytest.mark.parametrize("severity,scope,fixed", [("HIGH", True, "2"), ("LOW", False, "")])
def test_grey_zone_transparent_additivity(severity, scope, fixed):
    out = run(reports(severity, scope, fixed))
    p = out.proposals[0]
    assert p.action == ("request_security_review" if severity == "HIGH" else "allow_with_findings")
    explanation = p.params["score_explanation"]
    assert explanation["weights"] == WEIGHTS and sum(WEIGHTS.values()) == pytest.approx(1)
    assert sum(explanation["worst_contributions"].values()) == pytest.approx(
        p.params["security_score"]
    )
    assert explanation["method"] == "transparent_weighted_sum" and "weighted" in p.rationale
    assert not out.label_scores  # A policy score is not a calibrated label probability.
    assert ActionProposal.model_validate(p.model_dump()) == p
    assert p.action in AGENT_ACTIONS[AgentName.SECURITY]


def test_allow_with_findings_requires_human():
    p = run(reports("LOW", False, "")).proposals[0]
    assert RiskGate().decide(p, confidence=1.0).outcome == "ask_human"


def test_l0_preserved_and_l1_replaces_stub():
    inp = AgentInput(incident_id="i", params={"scan_reports": reports("CRITICAL")})
    assert ConfiguredSecurity().run(inp, level("L0")).proposals[0].action == "allow_deployment"
    assert ConfiguredSecurity().run(inp, level("L1")).proposals[0].action == "block_deployment"


def test_quality_gate_failure_blocks():
    report = reports()
    report["sonarcloud"] = {"projectStatus": {"status": "ERROR"}}
    assert run(report).proposals[0].action == "block_deployment"


def test_sonar_critical_and_malformed_scope_do_not_hide_criticals():
    report = reports()
    report["sonarcloud"] = {"issues": [{"rule": "vulnerability", "severity": "CRITICAL"}]}
    assert run(report).proposals[0].action == "block_deployment"
    assert run(reports("CRITICAL", runtime="unknown")).proposals[0].action == "block_deployment"


@pytest.mark.parametrize(
    "report",
    [
        {"trivy": {"Results": [None]}, "gitleaks": []},
        {"trivy": {"Results": [{"Vulnerabilities": "bad"}]}, "gitleaks": []},
        {"trivy": {"Results": [{"Vulnerabilities": {}}]}, "gitleaks": []},
        {**reports(), "sonarcloud": {"projectStatus": None}},
        {**reports(), "sonarcloud": {"issues": None}},
    ],
)
def test_malformed_reports_request_review(report):
    assert run(report).proposals[0].action == "request_security_review"


@pytest.mark.parametrize("content", ["bad", "{}", '{"format": 2}', "null"])
def test_invalid_snapshot_escalates_without_allowing(tmp_path, content):
    path = tmp_path / "bad.csv"
    path.with_suffix(".json").write_text(content)
    assert (
        run(reports("HIGH", False, ""), epss=path).proposals[0].action == "request_security_review"
    )


def test_unknown_cvss_or_epss_forces_review_and_scope_is_not_guessed():
    report = reports("LOW", False, "2")
    del report["trivy"]["Results"][0]["Vulnerabilities"][0]["CVSS"]
    p = run(report).proposals[0]
    assert p.action == "request_security_review"
    assert p.params["score_explanation"]["missing_required"]
    report = reports("LOW", None, "2")
    v = report["trivy"]["Results"][0]["Vulnerabilities"][0]
    v["VulnerabilityID"] = "CVE-2099-99999"
    assert run(report).proposals[0].action == "request_security_review"
    v["VulnerabilityID"] = "CVE-2026-56851"
    assert normalize(reports=report).findings[0].runtime_dependency is None


def test_reachability_and_dependency_scope_are_explicit_contributions():
    report = reports("HIGH", True, "2")
    v = report["trivy"]["Results"][0]["Vulnerabilities"][0]
    v["Reachability"] = "reachable"
    original = run(report).proposals[0].params["security_score"]
    v["Reachability"] = "unreachable"
    v["RuntimeDependency"] = False
    p = run(report).proposals[0]
    assert p.params["security_score"] == pytest.approx(original - 0.15)
    assert p.action == "allow_with_findings"
    assert RiskGate().decide(p, confidence=1).outcome == "ask_human"


def test_cvss_preference_validation_and_hard_rules_with_bad_context():
    assert cvss_base({"nvd": {"V2Score": 10}, "redhat": {"V3Score": 7.5}}) == (
        7.5,
        "redhat:V3Score",
    )
    with pytest.raises(ValueError):
        cvss_base({"nvd": {"V3Score": float("nan")}})
    report = reports("CRITICAL")
    report["trivy"]["Results"][0]["Vulnerabilities"][0]["CVSS"] = "broken"
    assert run(report).proposals[0].action == "block_deployment"


def test_checksum_corruption_escalates(tmp_path):
    path = tmp_path / "corrupt.csv"
    path.write_bytes(SAMPLE.read_bytes() + b"\n")
    path.with_suffix(".json").write_text(SAMPLE.with_suffix(".json").read_text())
    assert run(reports("LOW", False), epss=path).proposals[0].action == "request_security_review"
