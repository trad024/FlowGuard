"""Held-out Diagnose runs (Step 11; D39, D69 stage A): the corpus' test templates and the
LogChunks H3 sample, at L0 to L3. L4 and L5 reuse L3's answers (D39, D64, D67).

    python evaluation/diagnose_heldout.py run --confirm-step-11     # every level, both sets
    python evaluation/diagnose_heldout.py run --confirm-step-11 --set logchunks --levels L2 L3

Refuses unless --confirm-step-11 is given, the Diagnose freeze holds (heldout.diagnose_frozen)
and this harness is tagged harness-diagnose-freeze and unchanged since
(heldout.harness_diagnose_frozen). The checks run in that order, before anything is read.

Items:
  corpus     every verified variant of a test-split template (corpus-split.json), with its class
  logchunks  the `used` rows of datasets/logchunks-h3-sample.csv (230 logs, D47), each log
             checked against its SHA-256, with its target lines as the relevant lines (D32)
Levels: L0 regex and L1 TF-IDF + LR on the corpus only (LogChunks has no class and these
methods quote no line, so nothing of theirs is scorable there); L2 and L3 on both sets.

L2 and L3 make the same calls as the dev runner (diagnose_dev.ask: the D66 cut, grounding from
L3), with L3's examples retrieved excluding the item's own template (a no-op for held-out
items, which are not in the index). Each item is assigned to a lane by a seeded hash, so all of
its calls run on one lane (D41); every call goes to the audit log with its lane, cache key,
level, the cut and the retrieved example ids and distances (D66, D67). Resumable: an item and
level already written is skipped, and a call already made comes from the cache.

Blind (D69): while running it prints progress only (calls, cached, failed and tokens per lane).
It never compares an answer with a label, and writes no accuracy, coverage or hallucination
figure. Outputs, under evaluation/runs/diagnose-heldout/ by default:
  <L>.jsonl       replay records as report_data.load reads them: scenario_id, level, method,
                  repeat, setting and `observed` (root_cause, evidence_lines, tokens, and at L2
                  and L3 the counts diagnose_quotes / diagnose_ungrounded / hallucinated built by
                  observations.hallucination); L3 records of the D61 subset carry
                  llm_repeat_subset (their repeat 1; diagnose_repeats.py adds 2 and 3)
  <L>-raw.jsonl   per call: the model's raw quotes, lane, provider, model, cache key, cached,
                  the request record (cut, example ids and distances), and a failure if any
  groundtruth.db  one run per item (family "diagnose"): its class, or its relevant lines with
                  localizable true, for the report's join; labels only, never a comparison
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import threading
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "evaluation"), str(ROOT / "scenarios")]

import diagnose_dev  # noqa: E402
import groundtruth  # noqa: E402
import heldout  # noqa: E402
import heldout_guard  # noqa: E402
import logchunks  # noqa: E402
import observations  # noqa: E402
from flowguard import baselines, lanes, llm  # noqa: E402
from flowguard.audit import AuditLog  # noqa: E402

OUT = ROOT / "evaluation" / "runs" / "diagnose-heldout"
LEVELS = ("L0", "L1", "L2", "L3")
METHOD = {"L0": "regex", "L1": "tfidf_lr", "L2": "llm_zero_shot", "L3": "llm_retrieval"}
LLM_LEVELS = ("L2", "L3")
LANE_SEED = "lanes-heldout-20261108"
REPEAT_SUBSET = 30  # D61: 30 items x 3 runs at L3
SUBSET_LABEL = "llm-repeat-subset"


# ---------------------------------------------------------------- guards and items


def require(confirmed: bool) -> None:
    """--confirm-step-11, then the Diagnose freeze, then this harness's own freeze (D69)."""
    heldout_guard.require_step_11(confirmed, "diagnose_heldout")
    heldout.harness_diagnose_frozen()


def corpus_items(split: dict, corpus: Path = diagnose_dev.CORPUS) -> list[dict]:
    """Every verified variant of a test-split template, with its class."""
    test = {t for t, s in split["templates"].items() if s == "test"}
    out = []
    for variant, meta in sorted(split["variants"].items()):
        if meta["template"] not in test:
            continue
        logs = sorted((corpus / variant).glob("*.log"))
        if logs:
            out.append({"id": variant, "set": "corpus", "template": meta["template"],
                        "log": logs[0], "truth": {"root_cause": meta["root_cause"]}})  # fmt: skip
    return out


def logchunks_items(sample: Path = logchunks.SAMPLE_OUT, raw: Path = logchunks.RAW) -> list[dict]:
    """The H3 sample's used logs, each checked against its SHA-256, with its target lines."""
    out = []
    with sample.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["status"] != "used":
                continue
            path = raw / "logs" / row["log_path"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                raise SystemExit(f"{row['log_path']}: log changed since the sample was drawn")
            target = [int(x) for x in row["target_lines"].split()]
            out.append({"id": f"lc-{row['sha256'][:12]}", "set": "logchunks", "template": None,
                        "log": path,
                        "truth": {"localizable": True, "relevant_lines": target}})  # fmt: skip
    return out


def repeat_subset(seed: str, ids: list[str], size: int = REPEAT_SUBSET) -> set[str]:
    """D61's 30 items: a sample from the sorted held-out Diagnose item ids, drawn from the
    held-out seed's own HMAC stream (heldout.stream), so it is fixed before any run."""
    return set(heldout.stream(seed, SUBSET_LABEL).sample(sorted(ids), size))


def record_truth(truth: groundtruth.GroundTruthDB, items: list[dict]) -> None:
    for item in items:
        if truth.run(item["id"]) is None:
            truth.record_run(item["id"], spec={"id": item["id"], "family": "diagnose",
                             "set": item["set"]}, ground_truth=item["truth"], observed={},
                             l0_correct=None, valid=True, recording=None)  # fmt: skip


# ---------------------------------------------------------------- outputs


def tag(level: str, repeat: int | None = None) -> str:
    """The output name: L3 for the main run, L3-r2 and L3-r3 for the D61 repeats."""
    return level + (f"-r{repeat}" if repeat else "")


def written(out: Path, level: str, repeat: int | None = None) -> set[str]:
    path = out / f"{tag(level, repeat)}.jsonl"
    if not path.is_file():
        return set()
    return {json.loads(line)["scenario_id"] for line in path.read_text().splitlines() if line}


def record(item: dict, level: str, observed: dict, repeat: int = 1) -> dict:
    return {"scenario_id": item["id"], "level": level, "method": METHOD[level],
            "repeat": repeat, "setting": "default", "observed": observed}  # fmt: skip


def llm_observed(d, res: dict, in_subset: bool, level: str) -> dict:
    """What the toolkit reads from one LLM answer: the hallucination counts come from
    observations.hallucination, the same function the audit-log path uses."""
    params = {"diagnose_quotes": list(d.quotes), "diagnose_ungrounded": int(d.ungrounded)}
    observed = {"root_cause": d.root_cause, "evidence_lines": list(d.grounded),
                "tokens": int((res.get("usage") or {}).get("total_tokens") or 0),
                **observations.hallucination(params)}  # fmt: skip
    if in_subset and level == "L3":
        observed["llm_repeat_subset"] = True
    return observed


def raw_line(item, level, res, d, req, msgs, key, repeat=None) -> dict:
    line = {"scenario_id": item["id"], "level": level, "repeat": repeat,
            "diagnose_quotes": list(d.quotes), "lane": res.get("lane"),
            "provider": res.get("provider"), "model": res.get("model"), "cache_key": key,
            "cached": res.get("cached"), "request": req}  # fmt: skip
    if res.get("failed"):
        line["failed"] = res["failed"]
    return line


# ---------------------------------------------------------------- the run


class Progress:
    """Blind progress (D69): calls, cached, failed and tokens per lane; nothing else."""

    def __init__(self, say=print):
        self.lock, self.say, self.lanes = threading.Lock(), say, {}

    def add(self, lane: str, cached: bool, failed: bool, tokens: int) -> None:
        with self.lock:
            c = self.lanes.setdefault(lane, Counter())
            c["calls"] += 1
            c["cached"] += cached
            c["failed"] += failed
            c["tokens"] += 0 if cached else tokens

    def show(self, level: str) -> None:
        self.say(
            json.dumps({"level": level, "progress": {k: dict(v) for k, v in self.lanes.items()}})
        )


def run_classical(level: str, items: list[dict], out: Path, l1=None) -> int:
    """L0 or L1 on the corpus items not yet written."""
    done, rows = written(out, level), []
    for item in items:
        if item["set"] != "corpus" or item["id"] in done:
            continue
        log = item["log"].read_text(encoding="utf-8", errors="replace")
        if level == "L0":
            cls, lines = baselines.classify(log)
            observed = {"root_cause": cls, "evidence_lines": [n for n in lines if n >= 1]}
        else:
            scores = l1.scores(log)
            observed = {"root_cause": max(scores, key=scores.get)}
        rows.append(record(item, level, observed))
    with (out / f"{level}.jsonl").open("a", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    return len(rows)


def run_llm(
    level: str,
    items: list[dict],
    out: Path,
    live: list[lanes.Lane],
    client_for,
    subset: set[str],
    retriever=None,
    progress: Progress | None = None,
    repeat: int | None = None,
) -> int:
    """L2 or L3 on the items not yet written, one thread per lane. `repeat` (2 or 3, D61) is
    part of the cache key (D28); the main run is repeat 1 in the records, no repeat in the key."""
    progress = progress or Progress()
    name = tag(level, repeat)
    done = written(out, level, repeat)
    todo = [i for i in items if i["id"] not in done]
    shown = {}
    if level == "L3":  # retrieved before the lanes start: the index is read by one thread
        for item in todo:
            log = item["log"].read_text(encoding="utf-8", errors="replace")
            shown[item["id"]] = retriever.nearest(log, template=item["template"])
    queues = {lane.name: [] for lane in live}
    for item in todo:
        queues[lanes.assign(item["id"], live, LANE_SEED).name].append(item)
    lock, count = threading.Lock(), Counter()

    def work(lane: lanes.Lane) -> None:
        client = client_for(lane, level)
        for item in queues[lane.name]:
            log = item["log"].read_text(encoding="utf-8", errors="replace")
            try:
                res, d, req, msgs = diagnose_dev.ask(
                    client, log, shown.get(item["id"], []), level, repeat
                )
            except llm.BudgetExhausted:
                progress.say(json.dumps({"lane": lane.name, "stopped": "daily budget"}))
                return
            except Exception as exc:  # one failed call must not stop the lane
                progress.add(lane.name, False, True, 0)
                progress.say(json.dumps({"lane": lane.name, "error": type(exc).__name__}))
                continue
            failed = bool(res.get("failed"))
            tokens = int((res.get("usage") or {}).get("total_tokens") or 0)
            progress.add(lane.name, bool(res.get("cached")), failed, tokens)
            observed = llm_observed(d, res, item["id"] in subset or bool(repeat), level)
            row = record(item, level, observed, repeat or 1)
            raw = raw_line(item, level, res, d, req, msgs, client.key(msgs, repeat), repeat)
            with lock:
                with (out / f"{name}.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
                with (out / f"{name}-raw.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(raw) + "\n")
                count[name] += 1

    threads = [threading.Thread(target=work, args=(lane,)) for lane in live if queues[lane.name]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    progress.show(name)
    return count[name]


def lane_client(lane: lanes.Lane, level: str) -> llm.LLMClient:
    audit = AuditLog.open(level=level)
    return llm.LLMClient(api_key=lane.key, lane=lane.name, provider=lane.provider, audit=audit)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--confirm-step-11", action="store_true")
    r.add_argument("--set", choices=("corpus", "logchunks", "all"), default="all")
    r.add_argument("--levels", nargs="+", choices=LEVELS, default=list(LEVELS))
    r.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    require(args.confirm_step_11)  # before anything is read
    items = []
    if args.set in ("corpus", "all"):
        items += corpus_items(diagnose_dev.load_split())
    if args.set in ("logchunks", "all"):
        items += logchunks_items()
    args.out.mkdir(parents=True, exist_ok=True)
    record_truth(groundtruth.GroundTruthDB(args.out / "groundtruth.db"), items)
    all_ids = [i["id"] for i in corpus_items(diagnose_dev.load_split()) + logchunks_items()]
    subset = repeat_subset(heldout.checked_seed(), all_ids)
    print(json.dumps({"items": len(items), "levels": args.levels}), flush=True)
    for level in args.levels:
        if level in LLM_LEVELS:
            from flowguard.retrieval import Retriever

            live = diagnose_dev.ready_lanes()  # D41: independent lanes, reported
            if not live:
                raise SystemExit("no usable lane")
            retriever = Retriever() if level == "L3" else None
            run_llm(level, items, args.out, live, lane_client, subset, retriever)
        else:
            from flowguard.classical import TfidfDiagnoser

            n = run_classical(
                level, items, args.out, TfidfDiagnoser.load() if level == "L1" else None
            )
            print(json.dumps({"level": level, "written": n}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
