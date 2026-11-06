"""Synthetic govulncheck protocol fixtures only; no tool execution or network."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "security"))
from map_govulncheck import VERSION, enrich, reachability  # noqa: E402

CVE = "CVE-2026-56851"


def stream(*, called=False, level="symbol", broken=False):
    items = [
        {
            "config": {
                "protocol_version": "v1.0.0",
                "scanner_version": VERSION,
                "scan_mode": "source",
                "scan_level": level,
            }
        },
        {"finding": {"osv": "GO-fixture", "trace": [{"module": "example.invalid/lib"}]}},
        {"osv": {"id": "GO-fixture", "aliases": [CVE]}},
    ]
    if called:
        items.append(
            {
                "finding": {
                    "osv": "GO-fixture",
                    "trace": [{"module": "example.invalid/lib", "function": "Vulnerable"}],
                }
            }
        )
    return "\n".join(json.dumps(item) for item in items) + ('\n{"finding":' if broken else "")


@pytest.mark.parametrize("called", [False, True])
@pytest.mark.parametrize("status", [0, 3])
def test_explicit_findings_map_aliases_regardless_of_message_order(called, status):
    mapped = reachability(stream(called=called), status)
    assert mapped["complete"] and mapped["reachable_by_cve"] == {CVE: called}
    trivy = {
        "Results": [
            {"Vulnerabilities": [{"VulnerabilityID": CVE}, {"VulnerabilityID": "CVE-2026-99999"}]}
        ]
    }
    result = enrich(trivy, mapped)["Results"][0]["Vulnerabilities"]
    assert result[0]["Reachable"] is called
    assert "Reachable" not in result[1]  # Absence from findings is unknown, not unreachable.
    assert "Reachable" not in trivy["Results"][0]["Vulnerabilities"][0]


@pytest.mark.parametrize(
    "text,status",
    [(stream(level="package"), 0), (stream(broken=True), 0), (stream(), 1), ("", 0), ("[]", 0)],
)
def test_incomplete_or_unsupported_evidence_preserves_unknown(text, status):
    result = reachability(text, status)
    assert not result["complete"] and result["reachable_by_cve"] == {}


def test_malformed_frames_or_wrong_version_never_prove_unreachability():
    for text in (
        stream().replace('"module": "example.invalid/lib"', '"module": null'),
        stream().replace(VERSION, "v1.1.4"),
    ):
        assert reachability(text, 0)["reachable_by_cve"] == {}


def test_an_osv_alias_without_an_affected_version_finding_stays_unknown():
    text = "\n".join(line for line in stream().splitlines() if '"finding"' not in line)
    result = reachability(text, 0)
    assert result["complete"] and result["reachable_by_cve"] == {}
