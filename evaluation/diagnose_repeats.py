"""The D61 LLM repeats (Step 11; D28, D61, D69 stage A): 3 runs of L3 on 30 held-out Diagnose
items, the repeat index in the cache key.

    python evaluation/diagnose_repeats.py run --confirm-step-11

The subset (diagnose_heldout.repeat_subset): 30 items sampled from the sorted ids of every
held-out Diagnose item (the corpus' test-template variants and the 230 LogChunks logs), with
the held-out seed's own HMAC stream labelled "llm-repeat-subset" (heldout.stream). It needs the
seed in .env, checked against its committed hash, but not `heldout.py generate`, so it can run
in stage A. Repeat 1 is the item's main L3 call (diagnose_heldout, no repeat in its cache key,
its record flagged llm_repeat_subset); this runner makes repeats 2 and 3 (keys 2 and 3), so the
subset costs 60 calls beyond the main run, as D61 counts them.

The same guards, lanes, records and blind progress as diagnose_heldout: refuses without
--confirm-step-11, the Diagnose freeze and harness-diagnose-freeze; prints calls, cached,
failures and tokens per lane only. Outputs: L3-r2.jsonl and L3-r3.jsonl (replay records,
repeat 2 and 3, llm_repeat_subset true) and their -raw files, next to diagnose_heldout's.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "evaluation"), str(ROOT / "scenarios")]

import diagnose_dev  # noqa: E402
import diagnose_heldout as dh  # noqa: E402
import heldout  # noqa: E402

REPEATS = (2, 3)


def subset_items(items: list[dict], seed: str) -> list[dict]:
    """The D61 subset among `items`, drawn from every held-out Diagnose item id."""
    chosen = dh.repeat_subset(seed, [i["id"] for i in items])
    return [i for i in items if i["id"] in chosen]


def run(items, out, live, client_for, retriever, seed, progress=None) -> dict[int, int]:
    chosen = subset_items(items, seed)
    every = {i["id"] for i in chosen}
    return {
        r: dh.run_llm("L3", chosen, out, live, client_for, every, retriever, progress, repeat=r)
        for r in REPEATS
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--confirm-step-11", action="store_true")
    r.add_argument("--out", type=Path, default=dh.OUT)
    args = parser.parse_args(argv)
    dh.require(args.confirm_step_11)  # before anything is read
    items = dh.corpus_items(diagnose_dev.load_split()) + dh.logchunks_items()
    live = diagnose_dev.ready_lanes()
    if not live:
        raise SystemExit("no usable lane")
    from flowguard.retrieval import Retriever

    args.out.mkdir(parents=True, exist_ok=True)
    done = run(items, args.out, live, dh.lane_client, Retriever(), heldout.checked_seed())
    print(json.dumps({"written": done}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
