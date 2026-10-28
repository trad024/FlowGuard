"""Transparent vulnerability-priority score; critical/secret hard rules always win.

The weighted sum is an explicit policy, not a learned model or calibrated probability.
Only locally cached, checksummed FIRST EPSS data is read during incident handling.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from flowguard.contracts import AgentName, Finding
from flowguard.epss import load as load_epss

WEIGHTS = {"cvss": 0.50, "epss": 0.30, "runtime": 0.10, "reachable": 0.05, "no_fix": 0.05}
THRESHOLD = 0.50
EPSS = Path(__file__).resolve().parents[2] / "state/epss/current.csv"

COUNT_KEYS = ("trivy_high", "trivy_critical", "gitleaks")


@dataclass
class Scan:
    findings: list[Finding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    quality_failed: bool = False
    critical_found: bool = False
    evidence: list[dict] = field(default_factory=list)


def normalize(counts=None, reports=None, annotation=None) -> Scan:
    """Accept app-ci's JSON artifacts or its complete scan-counts annotation.

    Summary counts supplement reports: a positive summary must not be erased by an empty
    or stale artifact. Report completeness and disagreements are checked before allowing.
    Gitleaks contents are never copied into a proposal.
    """
    scan = Scan()
    if annotation is not None:
        if not isinstance(annotation, str):
            scan.errors.append("invalid annotation")
        else:
            counts = dict(re.findall(r"\b(trivy_high|trivy_critical|gitleaks)=(\d+)\b", annotation))
    if counts:
        if not isinstance(counts, dict):
            scan.errors.append("invalid counts")
        else:
            for key in COUNT_KEYS:
                value = counts.get(key)
                if isinstance(value, str) and value.isdecimal():
                    value = int(value)
                if type(value) is not int or value < 0:
                    scan.errors.append(f"missing or invalid {key}")
                else:
                    scan.counts[key] = value
    if reports is None:
        if not scan.counts:
            scan.errors.append("no scan evidence")
        return scan
    if not isinstance(reports, dict):
        scan.errors.append("invalid reports")
        return scan

    # Inspect secret evidence independently, even if the vulnerability report is broken.
    leaks = reports.get("gitleaks")
    if isinstance(leaks, list):
        scan.findings += [
            Finding(tool="gitleaks", rule_id="secret", severity="critical") for _ in leaks
        ]
    else:
        scan.errors.append("missing or invalid Gitleaks report")
    trivy = reports.get("trivy")
    if not isinstance(trivy, dict) or not isinstance(trivy.get("Results"), list):
        scan.errors.append("missing or invalid Trivy report")
    else:
        for result in trivy["Results"]:
            if not isinstance(result, dict):
                scan.errors.append("invalid Trivy result")
                continue
            vulns = result.get("Vulnerabilities")
            if vulns is None:
                vulns = []
            if not isinstance(vulns, list):
                scan.errors.append("invalid vulnerability list")
                continue
            for vuln in vulns:
                try:
                    severity = vuln["Severity"].lower()
                    scan.critical_found |= severity == "critical"
                    # Image artifacts do not establish whether a package is dev-only.
                    # Only an explicit boolean from the evidence adapter may establish it.
                    runtime = vuln.get("RuntimeDependency")
                    if runtime is not None and type(runtime) is not bool:
                        scan.errors.append("invalid dependency scope")
                        runtime = None
                    fixed = vuln.get("FixedVersion")
                    if fixed is not None and not isinstance(fixed, str):
                        raise ValueError("invalid fixed version")
                    cvss, cvss_source = cvss_base(vuln.get("CVSS"))
                    reachable = vuln.get("Reachable")
                    if reachable is None:
                        reachable = {"reachable": True, "unreachable": False}.get(
                            vuln.get("Reachability")
                        )
                    if reachable is not None and type(reachable) is not bool:
                        scan.errors.append("invalid reachability evidence")
                        reachable = None
                    scan.evidence.append(
                        {
                            "cvss": cvss,
                            "cvss_source": cvss_source,
                            "reachable": reachable,
                        }
                    )
                    scan.findings.append(
                        Finding(
                            tool="trivy",
                            rule_id=str(vuln.get("VulnerabilityID", "unknown")),
                            severity=severity,
                            runtime_dependency=runtime,
                            fix_available=bool(fixed.strip()) if fixed is not None else None,
                        )
                    )
                except (KeyError, TypeError, ValueError, AttributeError):
                    scan.errors.append("invalid vulnerability")
    sonar = reports.get("sonarcloud")
    if sonar is not None:
        if not isinstance(sonar, dict):
            scan.errors.append("invalid SonarCloud report")
        else:
            status = sonar.get("projectStatus", {})
            if not isinstance(status, dict):
                scan.errors.append("invalid SonarCloud quality status")
                status = {}
            gate = status.get("status")
            if gate == "ERROR":
                scan.quality_failed = True
            elif gate not in (None, "OK"):
                scan.errors.append("unknown SonarCloud quality gate")
            issues = sonar.get("issues", [])
            if not isinstance(issues, list):
                scan.errors.append("invalid SonarCloud issues")
                issues = []
            for issue in issues:
                try:
                    severity = {
                        "BLOCKER": "critical",
                        "CRITICAL": "critical",
                        "MAJOR": "medium",
                        "MINOR": "low",
                        "INFO": "info",
                    }[issue["severity"]]
                    scan.critical_found |= severity == "critical"
                    scan.evidence.append({"cvss": None, "cvss_source": None, "reachable": None})
                    scan.findings.append(
                        Finding(tool="sonarcloud", rule_id=str(issue["rule"]), severity=severity)
                    )
                except (KeyError, TypeError, ValueError):
                    scan.errors.append("invalid SonarCloud issue")
    return scan


def cvss_base(raw):
    """Prefer valid v3/v4 scores; otherwise v2. Take the maximum supplied vendor score."""
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        raise ValueError("invalid CVSS data")
    modern, legacy = [], []
    for vendor, versions in raw.items():
        if not isinstance(versions, dict):
            raise ValueError("invalid CVSS vendor entry")
        for version in ("V4Score", "V3Score", "V2Score"):
            if version not in versions:
                continue
            value = versions[version]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10:
                raise ValueError("invalid CVSS base score")
            source = (
                f"{vendor}:{version}" if re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", vendor) else version
            )
            (legacy if version == "V2Score" else modern).append((float(value), source))
    return max(modern or legacy, default=(None, None))


def score(scan: Scan, snapshot):
    findings = [finding for finding in scan.findings if finding.tool != "gitleaks"]
    if len(findings) != len(scan.evidence) or not findings:
        raise ValueError("incomplete vulnerability score evidence")
    items = []
    for finding, evidence in zip(findings, scan.evidence, strict=True):
        cve = finding.rule_id if re.fullmatch(r"CVE-[0-9]{4}-[0-9]{4,}", finding.rule_id) else None
        cvss, epss = evidence["cvss"], snapshot.scores.get(cve)
        missing = [name for name, value in (("cvss", cvss), ("epss", epss)) if value is None]
        values = {
            "cvss": cvss / 10 if cvss is not None else 1.0,
            "epss": epss if epss is not None else 1.0,
            "runtime": 0.0 if finding.runtime_dependency is False else 1.0,
            "reachable": 0.0 if evidence["reachable"] is False else 1.0,
            "no_fix": 0.0 if finding.fix_available is True else 1.0,
        }
        contributions = {name: WEIGHTS[name] * value for name, value in values.items()}
        items.append(
            {
                "cve": cve or "unmapped finding",
                "cvss_base": cvss,
                "cvss_source": evidence["cvss_source"],
                "epss_probability": epss,
                "runtime_dependency": finding.runtime_dependency,
                "reachable": evidence["reachable"],
                "fix_available": finding.fix_available,
                "normalized_values": values,
                "contributions": contributions,
                "score": sum(contributions.values()),
                "missing_required": missing,
            }
        )
    worst = max(items, key=lambda item: item["score"])
    return worst["score"], {
        "method": "transparent_weighted_sum",
        "weights": WEIGHTS,
        "threshold": THRESHOLD,
        "aggregate": "maximum finding score",
        "findings": items,
        "worst_contributions": worst["contributions"],
        "missing_required": any(item["missing_required"] for item in items),
        "epss_date": snapshot.date,
        "epss_csv_sha256": snapshot.sha256,
        "epss_model_version": snapshot.model_version,
        "meaning": "policy priority score; not an exploit or safe-deployment probability",
    }


class Security:
    name = AgentName.SECURITY

    def __init__(self, epss_path: Path | None = None):
        self.epss_path = epss_path or Path(os.environ.get("FLOWGUARD_EPSS_CSV", EPSS))

    def run(self, inp, cfg):
        from flowguard.stubs import AgentOutput, proposal

        scan = normalize(
            inp.scan_counts,
            inp.params.get("scan_reports"),
            inp.params.get("scan_annotation"),
        )
        critical = (
            scan.critical_found
            or scan.counts.get("trivy_critical", 0)
            or any(f.severity == "critical" for f in scan.findings)
        )
        secret = scan.counts.get("gitleaks", 0) or any(f.tool == "gitleaks" for f in scan.findings)
        params = {}
        if secret or critical or scan.quality_failed:
            action = "block_deployment"
            reason = "hard rule: " + (
                "secret finding"
                if secret
                else "critical finding"
                if critical
                else "quality gate failed"
            )
        elif scan.counts.get("trivy_high", 0) > sum(
            f.tool == "trivy" and f.severity == "high" for f in scan.findings
        ):
            action, reason = "block_deployment", "HIGH summary findings lack complete report detail"
        elif scan.errors:
            action, reason = "request_security_review", "; ".join(scan.errors)
        elif not scan.findings:
            action, reason = "allow_deployment", "no findings in the supplied scan evidence"
        else:
            try:
                priority, explanation = score(scan, load_epss(self.epss_path))
                action = (
                    "request_security_review"
                    if priority >= THRESHOLD or explanation["missing_required"]
                    else "allow_with_findings"
                )
                top = ", ".join(
                    f"{name}={value:.3f}"
                    for name, value in explanation["worst_contributions"].items()
                )
                reason = f"transparent priority={priority:.3f}; weighted contributions: {top}"
                if explanation["missing_required"]:
                    reason += "; missing CVSS or EPSS requires review"
                params = {"security_score": priority, "score_explanation": explanation}
            except (OSError, ValueError, KeyError, TypeError, OverflowError, IndexError):
                action, reason = "request_security_review", "EPSS snapshot unavailable or invalid"
        # Scanner payloads may contain a secret even in a report labelled redacted. Keep only
        # the executor's run identifiers and our numeric explanation in the proposed action.
        p = proposal(self.name, action, inp, reason)
        p.params = {
            **{key: inp.params[key] for key in ("run_id", "commit_sha") if key in inp.params},
            **params,
        }
        return AgentOutput(proposals=[p])
