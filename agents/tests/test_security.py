"""Security hard rules, evidence handling, model explanations and real gate routing."""

import json
import math

import pytest

from flowguard.catalog import AGENT_ACTIONS
from flowguard.contracts import ActionProposal, AgentName, Finding
from flowguard.gate import RiskGate
from flowguard.orchestrator import level
from flowguard.security import MODEL, Security, features, normalize, score
from flowguard.stubs import AgentInput
from flowguard.stubs import Security as ConfiguredSecurity


def reports(severity=None, runtime=None, fixed="1.2.3", leaks=None):
    vulns = []
    if severity:
        vulns = [{"Severity": severity, "RuntimeDependency": runtime, "FixedVersion": fixed}]
    return {"trivy": {"Results": [{"Vulnerabilities": vulns}]}, "gitleaks": leaks or []}


def run(report=None, counts=None, model=MODEL, **params):
    inp = AgentInput(
        incident_id="i-1",
        scan_counts=counts or {},
        params={"scan_reports": report, **params},
    )
    return Security(model).run(inp, level("L1"))


@pytest.mark.parametrize("fixed", [None, "", "2.0"])
def test_critical_always_blocks_without_model(tmp_path, fixed):
    p = run(reports("CRITICAL", fixed=fixed), model=tmp_path / "absent").proposals[0]
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
    assert features(scan.findings)[4] == 1


@pytest.mark.parametrize("severity,scope,fixed", [("HIGH", True, "2"), ("LOW", False, "")])
def test_grey_zone_scores_and_exact_shap_additivity(severity, scope, fixed):
    out = run(reports(severity, scope, fixed))
    p = out.proposals[0]
    assert p.action == ("request_security_review" if severity == "HIGH" else "allow_with_findings")
    explanation = p.params["shap"]
    logit = explanation["base_value"] + sum(explanation["values"].values())
    assert math.isclose(1 / (1 + math.exp(-logit)), p.params["security_score"])
    assert len(explanation["top_features"]) == 3 and "SHAP" in p.rationale
    assert math.isclose(sum(out.label_scores[p.id].values()), 1)
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
        {**reports(), "sonarcloud": {"projectStatus": None}},
        {**reports(), "sonarcloud": {"issues": None}},
    ],
)
def test_malformed_reports_request_review(report):
    assert run(report).proposals[0].action == "request_security_review"


@pytest.mark.parametrize("content", ["bad", "{}", '{"format": 2}', "null"])
def test_model_failure_escalates_without_allowing(tmp_path, content):
    path = tmp_path / "bad.json"
    path.write_text(content)
    assert (
        run(reports("HIGH", False, ""), model=path).proposals[0].action == "request_security_review"
    )


def test_nonfinite_model_is_rejected(tmp_path):
    model = json.loads(MODEL.read_text())
    model["weights"][0] = float("nan")
    path = tmp_path / "nan.json"
    path.write_text(json.dumps(model))
    with pytest.raises(ValueError):
        score([Finding(tool="trivy", rule_id="v", severity="high")], path)
