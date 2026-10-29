"""Observation builder: audit rows -> the replay records report_data.load reads.

    python evaluation/observations.py <audit.db> <out.jsonl>

One record per (scenario, level) that has a Diagnose proposal, with the observed fields taken
from what Diagnose recorded on its proposal (flowguard.stubs.Diagnose): the method it used and
the model's raw quotes. Hallucination (H3 secondary, pre-registration metrics.H3.hallucination),
at L2 and L3 alike:

    diagnose_quotes      raw quotes the model wrote, before any grounding
    diagnose_ungrounded  raw quotes not found in the log
    hallucinated         diagnose_ungrounded > 0: the per-item boolean, for exact McNemar only

The rate (report_metrics) is sum of diagnose_ungrounded over sum of diagnose_quotes. Never from
diagnose_rejected or the grounded line numbers: grounding (L3) changes the decision, not the
count, so L2 and L3 are counted the same way. A proposal without the raw fields (no LLM answer:
L0, L1, no log) gets no hallucination fields. The LogChunks H3 runner takes the same fields
through `hallucination`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from report_data import connect, validate_observed
from report_metrics import METHODS

DIAGNOSE = "diagnose"


def hallucination(params: dict) -> dict:
    """The hallucination fields from one Diagnose answer's raw fields; {} without them."""
    if "diagnose_quotes" not in params or "diagnose_ungrounded" not in params:
        return {}
    quotes, ungrounded = params["diagnose_quotes"], params["diagnose_ungrounded"]
    if not isinstance(quotes, list) or type(ungrounded) is not int:
        raise ValueError("diagnose_quotes must be a list and diagnose_ungrounded an integer")
    if not 0 <= ungrounded <= len(quotes):
        raise ValueError("diagnose_ungrounded must be between 0 and the number of quotes")
    return {
        "diagnose_quotes": len(quotes),
        "diagnose_ungrounded": ungrounded,
        "hallucinated": ungrounded > 0,
    }


def records(db) -> list[dict]:
    """One record per (scenario, level) with a Diagnose proposal, in the audit log's order. More
    than one Diagnose answer for a scenario and level (repeats, settings) is refused: those
    need records of their own."""
    found: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in db.execute(
        "SELECT scenario_id, level, id, params FROM proposals WHERE agent = ? ORDER BY rowid",
        (DIAGNOSE,),
    ):
        if row["scenario_id"] is None:
            continue
        params = json.loads(row["params"] or "{}")
        if not isinstance(params, dict):
            raise ValueError(f"{row['id']}: params must be an object")
        found[(row["scenario_id"], row["level"])].append({"id": row["id"], **params})
    out = []
    for (scenario, level), answers in found.items():
        if len(answers) > 1:
            raise ValueError(
                f"{scenario} at {level}: {len(answers)} Diagnose answers, expected one per record"
            )
        (params,) = answers
        observed = hallucination(params)
        validate_observed(observed)
        record = {"scenario_id": scenario, "level": level, "observed": observed}
        if params.get("diagnose_method") in METHODS:  # not "no_log": that stays "default"
            record["method"] = params["diagnose_method"]
        out.append(record)
    return out


def build(audit: Path, out: Path) -> int:
    with closing(connect(audit)) as db:
        built = records(db)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r) + "\n" for r in built), encoding="utf-8")
    return len(built)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("audit", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps({"records": build(args.audit, args.out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
