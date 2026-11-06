"""Map complete pinned govulncheck source/symbol JSON evidence to Trivy CVEs, offline."""

import argparse
import copy
import json
import re
from pathlib import Path

VERSION = "v1.4.0"


def messages(text):
    decoder = json.JSONDecoder()
    result = []
    while text.strip():
        item, end = decoder.raw_decode(text.lstrip())
        if not isinstance(item, dict):
            raise ValueError("govulncheck messages must be objects")
        result.append(item)
        text = text.lstrip()[end:]
    return result


def reachability(text, exit_code):
    unknown = {"complete": False, "reachable_by_cve": {}, "scanner_version": VERSION}
    if exit_code not in (0, 3):
        return {**unknown, "reason": "scan did not complete successfully"}
    try:
        stream = messages(text)
        configurations = [m["config"] for m in stream if "config" in m]
        if len(configurations) != 1 or not stream or "config" not in stream[0]:
            raise ValueError("missing or duplicate configuration")
        config = configurations[0]
        if (
            config.get("protocol_version") != "v1.0.0"
            or config.get("scanner_version") != VERSION
            or config.get("scan_mode") != "source"
            or config.get("scan_level") != "symbol"
        ):
            raise ValueError("requires the pinned complete source/symbol scan")
        aliases = {}
        for message in stream:
            if "osv" not in message:
                continue
            entry = message["osv"]
            values = entry.get("aliases", [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ValueError("invalid OSV aliases")
            aliases[entry["id"]] = [v for v in values if re.fullmatch(r"CVE-\d{4}-\d{4,}", v)]
        evidence = {}
        for message in stream:
            if "finding" not in message:
                continue
            finding = message["finding"]
            trace = finding.get("trace")
            if (
                not isinstance(trace, list)
                or not trace
                or not all(isinstance(f, dict) for f in trace)
            ):
                raise ValueError("invalid finding trace")
            if any(
                not isinstance(frame.get("module"), str)
                or not frame["module"]
                or ("function" in frame and not isinstance(frame["function"], str))
                for frame in trace
            ):
                raise ValueError("invalid trace frame")
            called = any(bool(frame.get("function")) for frame in trace)
            for cve in aliases.get(finding.get("osv"), []):
                evidence[cve] = evidence.get(cve, False) or called
        return {
            "complete": True,
            "scanner_version": VERSION,
            "reachable_by_cve": evidence,
            "reason": "explicit findings only; absence of a CVE leaves reachability unknown",
        }
    except (ValueError, TypeError, KeyError, AttributeError):
        return {**unknown, "reason": "malformed, truncated or unsupported JSON evidence"}


def enrich(trivy, mapped):
    result = copy.deepcopy(trivy)
    for target in result.get("Results", []):
        for vuln in target.get("Vulnerabilities") or []:
            cve = vuln.get("VulnerabilityID")
            if cve in mapped["reachable_by_cve"]:
                vuln["Reachable"] = mapped["reachable_by_cve"][cve]
                vuln["ReachabilitySource"] = f"govulncheck:{VERSION}:source-symbol"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--trivy", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    mapped = reachability(args.input.read_text(encoding="utf-8"), args.exit_code)
    trivy = json.loads(args.trivy.read_text(encoding="utf-8"))
    args.trivy.write_text(json.dumps(enrich(trivy, mapped), indent=2) + "\n", encoding="utf-8")
    args.out.write_text(json.dumps(mapped, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
