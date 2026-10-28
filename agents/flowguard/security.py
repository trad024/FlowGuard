"""Read-only Security policy. Learned scores never override criticals or secrets.

The portable logistic model uses exact interventional linear SHAP in log-odds space.
Training checks these contributions against shap.LinearExplainer; inference needs only
the standard library. Inputs are decoded reports, never paths or scanner commands.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from flowguard.contracts import AgentName, Finding

FEATURES = (
    "high_runtime",
    "high_dev",
    "high_unknown",
    "medium_runtime",
    "medium_dev",
    "medium_unknown",
    "low",
    "no_fix",
    "unknown_fix",
)
MODEL = Path(__file__).with_name("security_model.json")
COUNT_KEYS = ("trivy_high", "trivy_critical", "gitleaks")


@dataclass
class Scan:
    findings: list[Finding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    quality_failed: bool = False


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
            vulns = result.get("Vulnerabilities") or []
            if not isinstance(vulns, list):
                scan.errors.append("invalid vulnerability list")
                continue
            for vuln in vulns:
                try:
                    severity = vuln["Severity"].lower()
                    # Image artifacts do not establish whether a package is dev-only.
                    # Only an explicit boolean from the evidence adapter may establish it.
                    runtime = vuln.get("RuntimeDependency")
                    if runtime is not None and type(runtime) is not bool:
                        scan.errors.append("invalid dependency scope")
                        runtime = None
                    fixed = vuln.get("FixedVersion")
                    scan.findings.append(
                        Finding(
                            tool="trivy",
                            rule_id=str(vuln.get("VulnerabilityID", "unknown")),
                            severity=severity,
                            runtime_dependency=runtime,
                            fix_available=bool(fixed) if fixed is not None else None,
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
                    scan.findings.append(
                        Finding(tool="sonarcloud", rule_id=str(issue["rule"]), severity=severity)
                    )
                except (KeyError, TypeError, ValueError):
                    scan.errors.append("invalid SonarCloud issue")
    return scan


def features(findings: list[Finding]) -> list[float]:
    values = dict.fromkeys(FEATURES, 0.0)
    for finding in findings:
        if finding.tool == "gitleaks":
            continue
        if finding.severity in ("high", "medium"):
            scope = {True: "runtime", False: "dev", None: "unknown"}[finding.runtime_dependency]
            values[f"{finding.severity}_{scope}"] += 1
        elif finding.severity == "low":
            values["low"] += 1
        if finding.fix_available is False:
            values["no_fix"] += 1
        elif finding.fix_available is None:
            values["unknown_fix"] += 1
    return [min(10.0, values[name]) for name in FEATURES]


def sigmoid(value: float) -> float:
    return 1 / (1 + math.exp(-max(-700.0, min(700.0, value))))


def score(findings: list[Finding], path: Path = MODEL) -> tuple[float, dict]:
    model = json.loads(path.read_text(encoding="utf-8"))
    if (
        model["format"] != 1
        or model["type"] != "logistic-shap"
        or model["features"] != list(FEATURES)
    ):
        raise ValueError("unsupported security model")
    weights, background = model["weights"], model["background_mean"]
    if len(weights) != len(FEATURES) or len(background) != len(FEATURES):
        raise ValueError("invalid security model dimensions")
    numbers = [*weights, *background, model["intercept"], model["threshold"]]
    if not all(type(v) in (int, float) and math.isfinite(v) for v in numbers):
        raise ValueError("non-finite security model")
    if not 0 < model["threshold"] < 1:
        raise ValueError("invalid threshold")
    x = features(findings)
    contributions = {
        name: w * (v - mean)
        for name, w, v, mean in zip(FEATURES, weights, x, background, strict=True)
    }
    base = model["intercept"] + sum(w * mean for w, mean in zip(weights, background, strict=True))
    if not all(math.isfinite(v) for v in [base, *contributions.values()]):
        raise ValueError("security model arithmetic overflow")
    probability = sigmoid(base + sum(contributions.values()))
    top = sorted(contributions, key=lambda name: abs(contributions[name]), reverse=True)[:3]
    return probability, {
        "method": "interventional_linear_shap",
        "space": "log_odds",
        "base_value": base,
        "values": contributions,
        "top_features": [{"feature": name, "value": contributions[name]} for name in top],
        "threshold": model["threshold"],
        "model": model["model"],
    }


class Security:
    name = AgentName.SECURITY

    def __init__(self, model_path: Path = MODEL):
        self.model_path = model_path

    def run(self, inp, cfg):
        from flowguard.stubs import AgentOutput, proposal

        scan = normalize(
            inp.scan_counts,
            inp.params.get("scan_reports"),
            inp.params.get("scan_annotation"),
        )
        critical = scan.counts.get("trivy_critical", 0) or any(
            f.severity == "critical" for f in scan.findings
        )
        secret = scan.counts.get("gitleaks", 0) or any(f.tool == "gitleaks" for f in scan.findings)
        params = {}
        scores = {}
        if secret or critical or scan.quality_failed:
            action = "block_deployment"
            reason = "hard rule: " + (
                "secret finding"
                if secret
                else "critical finding"
                if critical
                else "quality gate failed"
            )
            scores = {"block": 1.0, "allow": 0.0}
        elif scan.counts.get("trivy_high", 0) > sum(
            f.tool == "trivy" and f.severity == "high" for f in scan.findings
        ):
            action, reason = "block_deployment", "HIGH summary findings lack complete report detail"
            scores = {"block": 1.0, "allow": 0.0}
        elif scan.errors:
            action, reason = "request_security_review", "; ".join(scan.errors)
        elif not scan.findings:
            action, reason = "allow_deployment", "no findings in the supplied scan evidence"
            scores = {"allow": 1.0, "block": 0.0}
        else:
            try:
                probability, explanation = score(scan.findings, self.model_path)
                action = (
                    "request_security_review"
                    if probability >= explanation["threshold"]
                    else "allow_with_findings"
                )
                top = ", ".join(
                    f"{v['feature']}={v['value']:+.3f}" for v in explanation["top_features"]
                )
                reason = f"grey-zone score={probability:.3f}; SHAP log-odds: {top}"
                params = {"security_score": probability, "shap": explanation}
                scores = {"review": probability, "allow": 1 - probability}
            except (OSError, ValueError, KeyError, TypeError, OverflowError):
                action, reason = "request_security_review", "security model unavailable or invalid"
        # Scanner payloads may contain a secret even in a report labelled redacted. Keep only
        # the executor's run identifiers and our numeric explanation in the proposed action.
        p = proposal(self.name, action, inp, reason)
        p.params = {
            **{key: inp.params[key] for key in ("run_id", "commit_sha") if key in inp.params},
            **params,
        }
        return AgentOutput(proposals=[p], label_scores={p.id: scores} if scores else {})
