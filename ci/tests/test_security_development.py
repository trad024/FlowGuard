"""Synthetic scan and FIRST fixtures only; no network, Go invocation or real corpus."""

import gzip
import hashlib
import json
import sys
from pathlib import Path

import pytest

from flowguard.epss import load
from flowguard.orchestrator import level
from flowguard.security import Security, normalize
from flowguard.stubs import AgentInput

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "security"))
from prepare_development import verify_plan  # noqa: E402

from replay import counterfactual, finding_rows  # noqa: E402

CVE = "CVE-2000-10000"


def source(tmp_path, probability="0.005"):
    raw = (
        "#model_version:test,score_date:2026-10-28\n"
        f"cve,epss,percentile\n{CVE},{probability},0.50\n"
    ).encode()
    path = tmp_path / "2026-10-28.csv"
    path.write_bytes(raw)
    compressed = gzip.compress(raw)
    path.with_suffix(".csv.gz").write_bytes(compressed)
    metadata = {
        "format": 1,
        "date": "2026-10-28",
        "sample": False,
        "csv_sha256": hashlib.sha256(raw).hexdigest(),
        "gzip_sha256": hashlib.sha256(compressed).hexdigest(),
        "rows": 1,
    }
    path.with_suffix(".json").write_text(json.dumps(metadata))
    plan = {"epss": metadata, "cases": [{"id": "fake", "cve": CVE, "epss": float(probability)}]}
    return path, plan


def case(*, reachable=False, cvss=7.5, severity="HIGH", secret=False, cve=CVE):
    return {
        "id": "fake",
        "counts": {
            "trivy_high": int(severity == "HIGH"),
            "trivy_critical": int(severity == "CRITICAL"),
            "gitleaks": int(secret),
        },
        "reports": {
            "gitleaks": [{"RuleID": "fake"}] if secret else [],
            "trivy": {
                "Results": [
                    {
                        "Vulnerabilities": [
                            {
                                "VulnerabilityID": cve,
                                "Severity": severity,
                                "PkgName": "example.invalid/fake",
                                "InstalledVersion": "v1.0.0",
                                "CVSS": {"nvd": {"V3Score": cvss}},
                                "FixedVersion": "1.0.1",
                                "Reachable": reachable,
                            }
                        ]
                    }
                ],
            },
        },
    }


def actual(item, path):
    inp = AgentInput(
        incident_id="fake", scan_counts=item["counts"], params={"scan_reports": item["reports"]}
    )
    return Security(path).run(inp, level("L1")).proposals[0]


def test_explicit_unreachability_changes_whole_case_decision(tmp_path):
    path, _ = source(tmp_path)
    item = case()
    proposal = actual(item, path)
    assert proposal.action == "allow_with_findings"
    assert (
        counterfactual(
            normalize(item["counts"], item["reports"]),
            load(path),
            proposal,
            unknown_reachability=True,
        )
        == "request_security_review"
    )
    assert item["reports"]["trivy"]["Results"][0]["Vulnerabilities"][0]["Reachable"] is False


def test_epss_changes_score_only_decision_with_other_inputs_held_fixed(tmp_path):
    path, _ = source(tmp_path, "0.2")
    item = case()
    proposal = actual(item, path)
    assert proposal.action == "request_security_review"
    assert (
        counterfactual(
            normalize(item["counts"], item["reports"]), load(path), proposal, zero_epss=True
        )
        == "allow_with_findings"
    )


@pytest.mark.parametrize("secret,severity", [(True, "HIGH"), (False, "CRITICAL")])
def test_counterfactual_preserves_hard_rules(tmp_path, secret, severity):
    path, _ = source(tmp_path)
    item = case(secret=secret, severity=severity)
    proposal = actual(item, path)
    assert proposal.action == "block_deployment"
    assert (
        counterfactual(
            normalize(item["counts"], item["reports"]),
            load(path),
            proposal,
            zero_epss=True,
            unknown_reachability=True,
        )
        == "block_deployment"
    )


def test_missing_epss_still_requires_review_when_probabilities_zeroed(tmp_path):
    path, _ = source(tmp_path)
    item = case(cve="CVE-2000-20000")
    proposal = actual(item, path)
    assert (
        counterfactual(
            normalize(item["counts"], item["reports"]), load(path), proposal, zero_epss=True
        )
        == "request_security_review"
    )


def test_another_real_finding_can_mask_target_reachability_effect(tmp_path):
    path, _ = source(tmp_path)
    item = case()
    second = case(reachable=True)["reports"]["trivy"]["Results"][0]["Vulnerabilities"][0]
    item["reports"]["trivy"]["Results"][0]["Vulnerabilities"].append(second)
    item["counts"]["trivy_high"] = 2
    proposal = actual(item, path)
    assert proposal.action == "request_security_review"
    assert (
        counterfactual(
            normalize(item["counts"], item["reports"]),
            load(path),
            proposal,
            unknown_reachability=True,
        )
        == proposal.action
    )
    rows = finding_rows(item, load(path))
    assert len(rows) == 2 and rows[0]["fix_available"] is True


def test_plan_verifies_full_source_checksums_and_cve_membership(tmp_path):
    path, plan = source(tmp_path)
    assert verify_plan(plan, path).scores[CVE] == 0.005
    plan["cases"][0]["cve"] = "CVE-2000-20000"
    with pytest.raises(ValueError, match="CVE absent"):
        verify_plan(plan, path)


def test_plan_rejects_sample_and_corrupt_compressed_source(tmp_path):
    path, plan = source(tmp_path)
    path.with_suffix(".csv.gz").write_bytes(b"broken")
    with pytest.raises(ValueError, match="compressed"):
        verify_plan(plan, path)
    manifest = json.loads(path.with_suffix(".json").read_text())
    manifest["sample"] = True
    path.with_suffix(".json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="full dated"):
        verify_plan(plan, path)
