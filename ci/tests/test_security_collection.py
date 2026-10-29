"""Synthetic CI metadata and scanner artifacts only; never invoke hosted CI in tests."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "security"))
from collect_development import collect_case  # noqa: E402
from map_govulncheck import reachability  # noqa: E402


@pytest.fixture
def artifacts(tmp_path):
    cve = "CVE-2000-10000"
    case = {
        "id": "fake",
        "cve": cve,
        "module": "example.invalid/fake",
        "version": "v1.0.0",
        "epss": 0.005,
        "advisory": "https://example.invalid/advisory",
    }
    publication = {"sha": "abc", "branch": "scenario/security-dev-31-fake", "development_seed": 31}
    run = {
        "head_sha": "abc",
        "head_branch": publication["branch"],
        "name": "app-ci",
        "id": 1,
        "html_url": "https://example.invalid/run/1",
        "created_at": "2026-10-29T00:00:00Z",
        "conclusion": "success",
    }
    jobs = {
        "jobs": [{"name": name, "conclusion": "success"} for name in ("build", "unit", "scans")]
    }
    stream = "\n".join(
        json.dumps(message)
        for message in [
            {
                "config": {
                    "protocol_version": "v1.0.0",
                    "scanner_version": "v1.4.0",
                    "scan_mode": "source",
                    "scan_level": "symbol",
                }
            },
            {"osv": {"id": "fake", "aliases": [cve]}},
            {"finding": {"osv": "fake", "trace": [{"module": "example.invalid/fake"}]}},
        ]
    )
    (tmp_path / "govulncheck.json").write_text(stream)
    (tmp_path / "govulncheck-reachability.json").write_text(json.dumps(reachability(stream, 0)))
    (tmp_path / "gitleaks.json").write_text(json.dumps([{"Secret": "do-not-retain-this-content"}]))
    (tmp_path / "trivy.json").write_text(
        json.dumps(
            {
                "Results": [
                    {
                        "Vulnerabilities": [
                            {
                                "VulnerabilityID": cve,
                                "Severity": "HIGH",
                                "PkgName": case["module"],
                                "InstalledVersion": "1.0.0",
                                "Reachable": False,
                                "FixedVersion": "1.0.1",
                                "CVSS": {"nvd": {"V3Score": 7.5}},
                                "Description": "discard-unneeded-field",
                            }
                        ]
                    }
                ]
            }
        )
    )
    return case, publication, run, jobs, tmp_path


def test_collection_validates_evidence_and_discards_secret_contents(artifacts):
    record = collect_case(*artifacts)
    assert record["counts"] == {"trivy_high": 1, "trivy_critical": 0, "gitleaks": 1}
    assert len(record["source"]["artifact_sha256"]) == 4
    serialized = json.dumps(record)
    assert "do-not-retain-this-content" not in serialized
    assert "discard-unneeded-field" not in serialized


def test_failed_build_is_invalid_case_not_security_block(artifacts):
    artifacts[3]["jobs"][0]["conclusion"] = "failure"
    with pytest.raises(ValueError, match="invalid case"):
        collect_case(*artifacts)


def test_wrong_commit_is_rejected(artifacts):
    artifacts[2]["head_sha"] = "another-commit"
    with pytest.raises(ValueError, match="does not match"):
        collect_case(*artifacts)


def test_missing_requested_high_module_version_is_rejected(artifacts):
    artifacts[0]["version"] = "v9.0.0"
    with pytest.raises(ValueError, match="not observed"):
        collect_case(*artifacts)


def test_incomplete_reachability_scan_is_not_accepted_as_unreachable(artifacts):
    (artifacts[4] / "govulncheck-reachability.json").write_text('{"complete": false}')
    with pytest.raises(ValueError, match="complete pinned"):
        collect_case(*artifacts)
