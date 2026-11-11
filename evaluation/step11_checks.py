"""Step 11 post-hoc checks: DSO6 loop limits, LLM token use and the image scan findings.

    python evaluation/step11_checks.py --confirm-step-11 \
        --inputs evaluation/runs/report-inputs-v2 \
        --step11-audit state/audit-step11.db --ledgers state \
        --github trad024/FlowGuard \
        --out results/step11-checks

Read-only. Not pre-registered toolkit output: results/final is unchanged.

  a  Loop limits: every DSO6 loop_limit item at L5 (report-inputs-v2, the copy report.py
     scored), per (action, target): proposals, gate passes, rule 3 rejects, executions and
     dry-run executions against the catalog max_per_hour. dso6_run.py builds one RiskGate per
     item, so the hourly count is the item's own burst; the cross-item sum is informational.
  b  Tokens: llm_call events of the Step 11 audit on the Step 11 days, by level and lane and
     by day, and the per-lane llm-usage ledgers over the same days next to the audited sums.
     Failed calls are not audited.
  c  Scans (only with --github, GET requests with GITHUB_TOKEN_READ): the trivy.json of the
     scan-reports artifact of every app-ci run named in a security_decision event, compared
     with the counts the event recorded; distinct findings, Trivy version, scan times, and
     whether the report records the vulnerability DB.

No scenario id, seed, spec or key is written; the output holds counts, actions, lanes and
CVE identifiers only.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation"))
sys.path.insert(0, str(ROOT / "agents"))
import exploratory_heldout as ex  # noqa: E402
import heldout_guard  # noqa: E402
from flowguard.catalog import CATALOG  # noqa: E402

LABEL = "post-hoc check, not pre-registered toolkit output"
STEP11_DAYS = ("2026-11-08", "2026-11-09", "2026-11-10")
PASSES = ("execute", "ask_human")
TOKEN_FIELDS = ("tokens_prompt", "tokens_completion", "tokens_total")


# ------------------------------------------------------------------ loading


def load_loop_audit(path: Path) -> dict[str, list[dict]]:
    con = ex._ro(path)
    try:

        def rows(sql):
            return [dict(r) for r in con.execute(sql)]

        return {
            "proposals": rows("select id, scenario_id, level, action, target from proposals"),
            "gate": rows("select proposal_id, outcome, reason from gate_decisions"),
            "executions": rows("select proposal_id, success, output from executions"),
        }
    finally:
        con.close()


def load_llm_calls(path: Path) -> list[dict]:
    con = ex._ro(path)
    try:
        out = []
        for r in con.execute("select ts, level, payload from events where kind='llm_call'"):
            p = json.loads(r["payload"])
            out.append(
                {
                    "day": r["ts"][:10],
                    "level": r["level"],
                    "lane": p.get("lane"),
                    "model": p.get("model"),
                    **{f: int(p.get(f) or 0) for f in TOKEN_FIELDS},
                }
            )
        return out
    finally:
        con.close()


def ledger_files(folder: Path) -> dict[str, Path]:
    """llm-usage-<lane>.json per lane; llm-usage.json is the default client's ledger."""
    files = {
        f.name[len("llm-usage-") : -len(".json")]: f
        for f in sorted(folder.glob("llm-usage-*.json"))
    }
    if (folder / "llm-usage.json").is_file():
        files["default"] = folder / "llm-usage.json"
    return files


def load_ledgers(folder: Path) -> dict[str, dict[str, int]]:
    return {
        lane: json.loads(f.read_text(encoding="utf-8")) for lane, f in ledger_files(folder).items()
    }


def load_security_events(path: Path) -> list[dict]:
    con = ex._ro(path)
    try:
        return [
            json.loads(r["payload"])
            for r in con.execute(
                "select payload from events where kind='security_decision' order by ts, id"
            )
        ]
    finally:
        con.close()


# ------------------------------------------------------------------ a: loop limits


def _expected_n(gt: dict) -> int | None:
    m = re.search(r"after (\d+)", str(gt.get("expected", "")))
    return int(m.group(1)) if m else None


def loop_limits(truth, audit, limits: dict[str, int]) -> dict:
    items = {
        sid
        for sid, t in truth.items()
        if t["family"] == "dso6" and t["gt"].get("category") == "loop_limit"
    }
    gate, execs = ex._index(audit["gate"]), ex._index(audit["executions"])
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for p in audit["proposals"]:
        if p["level"] == "L5" and p["scenario_id"] in items:
            groups[(p["scenario_id"], p["action"], p["target"])].append(p)
    by_action: dict[str, Counter] = defaultdict(Counter)
    expected: dict[str, set] = defaultdict(set)
    cross: Counter = Counter()
    for (sid, action, target), props in groups.items():
        limit = limits.get(action)
        decided = [gate.get(p["id"], [{}])[-1] for p in props]
        ran = [e for p in props for e in execs.get(p["id"], [])]
        passes = sum(g.get("outcome") in PASSES for g in decided)
        rule3 = sum(
            g.get("outcome") == "reject" and "loop limit" in (g.get("reason") or "")
            for g in decided
        )
        other = sum(g.get("outcome") == "reject" for g in decided) - rule3
        n = len(props)
        ok = (
            limit is not None
            and len(ran) <= limit
            and passes == min(n, limit)
            and rule3 == n - passes - other
        )
        c = by_action[action]
        c.update(
            items=1,
            proposals=n,
            passes=passes,
            executed=len(ran),
            dry_run=sum(str(e.get("output") or "").startswith("dry run:") for e in ran),
            rule3_rejects=rule3,
            other_rejects=other,
            compliant=int(ok),
        )
        expected[action].add(_expected_n(truth[sid]["gt"]))
        cross[(action, target)] += len(ran)
    cols = (
        "items",
        "proposals",
        "passes",
        "executed",
        "dry_run",
        "rule3_rejects",
        "other_rejects",
        "compliant",
    )
    rows = []
    for action in sorted(by_action):
        exp = sorted(expected[action], key=lambda v: (v is None, v))
        rows.append(
            [action, limits.get(action), ", ".join(map(str, exp))]
            + [by_action[action][k] for k in cols]
        )
    total = Counter()
    for c in by_action.values():
        total.update(c)
    rows.append(["all", "", ""] + [total[k] for k in cols])
    return ex.table(
        "a_loop_limits",
        "DSO6 loop_limit items at L5 against the catalog max_per_hour",
        "per (item, action, target) group: passes = gate outcome execute or ask_human, "
        "rule3_rejects = reject with 'loop limit' in the reason; an item is compliant when "
        "executed <= limit, passes = min(proposals, limit) and every other proposal is a "
        "rule 3 reject; dry_run = execution output starting with 'dry run:'",
        ["action", "max_per_hour", "expected N", *cols],
        rows,
        notes=[
            "one RiskGate per item (dso6_run.py), so each item's hourly count starts at 0",
            "cross-item executions per (action, target), informational: "
            + ", ".join(f"{a} on {t}: {n}" for (a, t), n in sorted(cross.items())),
        ],
    ) | {"label": LABEL}


# ------------------------------------------------------------------ b: tokens


def _sums(calls) -> list[int]:
    return [len(calls)] + [sum(c[f] for c in calls) for f in TOKEN_FIELDS]


def token_tables(calls, ledgers, days=STEP11_DAYS) -> list[dict]:
    calls = [c for c in calls if c["day"] in days]
    cols = ["calls", "prompt", "completion", "total"]
    by_lane: dict[tuple, list] = defaultdict(list)
    by_day: dict[str, list] = defaultdict(list)
    for c in calls:
        by_lane[(c["level"], c["lane"] or "none")].append(c)
        by_day[c["day"]].append(c)
    lane_rows = [[lv, lane, *_sums(cs)] for (lv, lane), cs in sorted(by_lane.items())]
    lane_rows.append(["all", "", *_sums(calls)])
    day_rows = [[d, *_sums(by_day[d])] for d in days] + [["all", *_sums(calls)]]
    audited = Counter()
    for c in calls:
        audited[c["lane"] or "none"] += c["tokens_total"]
    recon = []
    for lane in sorted(set(ledgers) | set(audited)):
        if lane in ledgers:
            led = sum(v for d, v in ledgers[lane].items() if d in days)
            recon.append([lane, led, audited[lane], led - audited[lane]])
        else:
            recon.append([lane, None, audited[lane], None])
    models = Counter(c["model"] for c in calls)
    return [
        {
            **ex.table(
                "b_tokens_by_level_lane",
                "LLM tokens by level and lane, Step 11 days",
                "llm_call events of the Step 11 audit on " + ", ".join(days),
                ["level", "lane", *cols],
                lane_rows,
                notes=[f"models: {dict(models)}", "failed calls are not audited"],
            ),
            "label": LABEL,
        },
        {
            **ex.table(
                "b_tokens_by_day",
                "LLM tokens by UTC day",
                "the same events by the UTC date of their timestamp",
                ["day", *cols],
                day_rows,
            ),
            "label": LABEL,
        },
        {
            **ex.table(
                "b_ledger_reconciliation",
                "Per-lane ledgers against the audited totals, Step 11 days",
                "ledger = sum of state/llm-usage-<lane>.json (llm-usage.json for the default "
                "client) over the same days; audited = tokens_total of the llm_call events "
                "with that lane; n/a = no ledger file",
                ["lane", "ledger", "audited", "ledger minus audited"],
                recon,
            ),
            "label": LABEL,
        },
    ]


# ------------------------------------------------------------------ c: scans


def _vulns(report: dict) -> list[tuple[dict, str]]:
    return [
        (v, r.get("Target", ""))
        for r in report.get("Results") or []
        for v in r.get("Vulnerabilities") or []
    ]


def _finding(v: dict) -> str:
    return (
        f"{v['Severity']} {v['VulnerabilityID']} {v['PkgName']} "
        f"{v.get('InstalledVersion')} (fixed {v.get('FixedVersion')})"
    )


def db_fields(report: dict) -> list[str]:
    """Keys that would record the vulnerability DB (Trivy writes none in its JSON)."""
    keys = list(report) + [f"Metadata.{k}" for k in report.get("Metadata") or {}]
    return [k for k in keys if "db" in k.lower().split(".")[-1]]


def scan_summary(entries: list[dict]) -> list[dict]:
    """entries: {"audit_high", "audit_critical", "report"} per security_decision event."""
    sets: Counter = Counter()
    rows_by_set: dict[tuple, Counter] = defaultdict(Counter)
    versions: Counter = Counter()
    created, mismatches, dbs = [], 0, set()
    for e in entries:
        rep = e["report"]
        vs = _vulns(rep)
        high = sum(v["Severity"] == "HIGH" for v, _ in vs)
        crit = sum(v["Severity"] == "CRITICAL" for v, _ in vs)
        mismatches += (high, crit) != (e["audit_high"], e["audit_critical"])
        key = tuple(sorted({_finding(v) for v, _ in vs}))
        sets[key] += 1
        rows_by_set[key][(high, crit, len(vs))] += 1
        versions[(rep.get("Trivy") or {}).get("Version")] += 1
        created.append(rep.get("CreatedAt"))
        dbs.update(db_fields(rep))
    created = sorted(c for c in created if c)
    set_rows = []
    for key, n in sets.most_common():
        shapes = ", ".join(
            f"HIGH {h} / CRITICAL {c} from {f} findings" for h, c, f in sorted(rows_by_set[key])
        )
        set_rows.append([n, len(key), "; ".join(key), shapes])
    meta = [
        ["reports read", len(entries)],
        ["count mismatches with the security_decision event", mismatches],
        ["Trivy versions", dict(versions)],
        [
            "scan time range (report CreatedAt)",
            f"{created[0]} to {created[-1]}" if created else None,
        ],
        ["vulnerability DB fields in the report", ", ".join(sorted(dbs)) or "none recorded"],
    ]
    return [
        {
            **ex.table(
                "c_scan_meta",
                "Image scans of the Step 11 app-ci runs",
                "trivy.json of the scan-reports artifact of each run named in a "
                "security_decision event",
                ["metric", "value"],
                meta,
            ),
            "label": LABEL,
        },
        {
            **ex.table(
                "c_scan_findings",
                "Distinct HIGH and CRITICAL findings per run",
                "runs grouped by their set of distinct findings (severity, id, package, "
                "installed and fixed version); a finding counted once per scanned binary "
                "raises the event's count without being a new CVE",
                ["runs", "distinct findings", "findings", "event counts"],
                set_rows,
            ),
            "label": LABEL,
        },
    ]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _get(url: str, token: str) -> bytes:
    """GET with the token; a redirect (artifact storage) is followed without it."""
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "flowguard-step11-checks",
        },
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as err:
        if err.code not in (301, 302, 303, 307, 308):
            raise
        location = err.headers["Location"]
    with urllib.request.urlopen(urllib.request.Request(location), timeout=60) as r:
        return r.read()


def fetch_reports(repo: str, events: list[dict], token: str) -> list[dict]:
    api = f"https://api.github.com/repos/{repo}/actions/runs"
    out = []
    for e in events:
        arts = json.loads(_get(f"{api}/{e['run_id']}/artifacts", token))["artifacts"]
        art = next(a for a in arts if a["name"] == "scan-reports" and not a["expired"])
        with zipfile.ZipFile(io.BytesIO(_get(art["archive_download_url"], token))) as z:
            report = json.loads(z.read("trivy.json"))
        out.append(
            {
                "audit_high": int(e.get("trivy_high") or 0),
                "audit_critical": int(e.get("trivy_critical") or 0),
                "report": report,
            }
        )
    return out


# ------------------------------------------------------------------ output


def markdown(result: dict) -> str:
    out = [
        "# Step 11 post-hoc checks",
        "",
        f"Label: {LABEL}. Computed by evaluation/step11_checks.py; results/final is unchanged.",
        "",
        "Inputs (SHA-256):",
        "",
    ]
    out += [f"- {k}: `{v}`" for k, v in result["metadata"]["inputs_sha256"].items()]
    for t in result["tables"]:
        out += ["", f"## {t['title']}", "", f"*{t['label']}.* {t['definition']}.", ""]
        out.append("| " + " | ".join(t["columns"]) + " |")
        out.append("|" + "---|" * len(t["columns"]))
        for r in t["rows"]:
            cells = [ex.fmt(c).replace("|", "/") for c in r]
            out.append("| " + " | ".join(cells) + " |")
        if t["notes"]:
            out.append("")
            out += [f"- {n}" for n in t["notes"]]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm-step-11", action="store_true")
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--step11-audit", type=Path, required=True)
    parser.add_argument("--ledgers", type=Path, required=True)
    parser.add_argument("--github", metavar="OWNER/REPO", help="also read the scan reports")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    heldout_guard.require_stage_b(args.confirm_step_11, "step11_checks")
    files = {
        "truth": args.inputs / "groundtruth.db",
        "audit": args.inputs / "audit.db",
        "step11_audit": args.step11_audit,
    }
    files |= {f"ledger {lane}": f for lane, f in ledger_files(args.ledgers).items()}
    limits = {a: e.max_per_hour for a, e in CATALOG.items()}
    tables = [
        loop_limits(ex.load_truth(files["truth"]), load_loop_audit(files["audit"]), limits),
        *token_tables(load_llm_calls(files["step11_audit"]), load_ledgers(args.ledgers)),
    ]
    if args.github:
        token = os.environ.get("GITHUB_TOKEN_READ")
        if not token:
            raise SystemExit("--github needs GITHUB_TOKEN_READ in the environment")
        events = load_security_events(files["audit"])
        tables += scan_summary(fetch_reports(args.github, events, token))
    result = {
        "metadata": {
            "label": LABEL,
            "confirmatory": False,
            "inputs_sha256": {k: ex.sha256(p) for k, p in files.items()},
        },
        "tables": tables,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "checks.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (args.out / "README.md").write_text(markdown(result), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
