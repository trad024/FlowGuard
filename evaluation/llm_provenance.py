"""Which level made each LLM call (D64): the audit log's llm_call event, joined on cache_key.

A cached answer (state/llm-cache/<cache_key>.json) does not say who asked for it. The client
writes one llm_call event per real call, never for a cache hit, and every audit row carries the
level of the log that wrote it; so the level that made a cached answer is the level of the one
llm_call event with its cache key. A key with no such event, or with events from more than one
level, is an error, never a guess.

    python evaluation/llm_provenance.py [--audit state/audit.db] [--cache state/llm-cache]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "state" / "audit.db"
CACHE = ROOT / "state" / "llm-cache"


def making_levels(audit_db: Path = AUDIT, cache_dir: Path | None = CACHE) -> dict[str, str]:
    """cache key -> the level whose run made the call (read-only on the audit log)."""
    uri = Path(audit_db).resolve().as_uri() + "?mode=ro"
    levels: dict[str, set[str]] = defaultdict(set)
    with closing(sqlite3.connect(uri, uri=True)) as db:
        for level, payload in db.execute(
            "SELECT level, payload FROM events WHERE kind = 'llm_call'"
        ):
            key = json.loads(payload).get("cache_key")
            if key:
                levels[key].add(level)
    keys = set(levels)
    if cache_dir is not None and Path(cache_dir).is_dir():
        keys |= {p.stem for p in Path(cache_dir).glob("*.json")}
    out, problems = {}, []
    for key in sorted(keys):
        found = levels.get(key, set())
        if len(found) != 1:
            problems.append(f"{key}: {sorted(found) or 'no llm_call event'}")
        else:
            out[key] = next(iter(found))
    if problems:
        raise ValueError(f"{len(problems)} cache keys without one making level: {problems[:3]}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--audit", type=Path, default=AUDIT)
    parser.add_argument("--cache", type=Path, default=CACHE)
    args = parser.parse_args(argv)
    levels = making_levels(args.audit, args.cache)
    counts: dict[str, int] = defaultdict(int)
    for level in levels.values():
        counts[level] += 1
    print(json.dumps({"cache_keys": len(levels), "made_by_level": dict(sorted(counts.items()))}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
