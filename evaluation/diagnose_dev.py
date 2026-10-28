"""Diagnose dev runs (Step 7) on the corpus' training and validation templates, one thread per lane.

    python evaluation/diagnose_dev.py run --level L2      # resumable: answers are cached
    python evaluation/diagnose_dev.py score --level L2
    python evaluation/diagnose_dev.py run --level L2 --repeat 2        # D28 repeat key
    python evaluation/diagnose_dev.py run --level L2 --provider openrouter --splits validation

Never runs test templates or LogChunks: those wait for Step 11 (D6, D32); `items()` refuses
them. .env is re-read at every start, so new GROQ_KEY_n lanes are picked up; the number of
independent lanes is printed and noted when it changes (D41). Each item is assigned to a lane
by a seeded hash, so all of its calls run on one lane; a lane that reaches its daily budget
stops and its items wait for the next UTC day. Diagnose sees only the log; the class is read
afterwards, for scoring.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "evaluation"), str(ROOT / "scenarios")]

import heldout  # noqa: E402
import stats  # noqa: E402
from flowguard import diagnose, lanes, llm  # noqa: E402
from flowguard.audit import AuditLog  # noqa: E402

CORPUS = ROOT / "evaluation" / "runs" / "corpus"
SPLIT = ROOT / "evaluation" / "datasets" / "corpus-split.json"
OUT = ROOT / "evaluation" / "runs" / "diagnose-dev"
LANE_SEED = "lanes-20261028"
DEV_SPLITS = ("train", "validation")
OPENROUTER_MODEL = "openai/gpt-oss-120b:free"


def items(split: dict, splits=DEV_SPLITS) -> list[dict]:
    """Verified variants of the given splits. Test templates need the D39 staged freeze:
    a frozen pre-registration and the Diagnose code unchanged since diagnose-freeze."""
    if any(s not in DEV_SPLITS for s in splits):
        heldout.diagnose_frozen()
    test = {t for t, s in split["templates"].items() if s == "test"}
    out = []
    for variant, meta in sorted(split["variants"].items()):
        template = meta["template"]
        s = split["templates"].get(template)
        if template in test or s not in splits:
            continue
        logs = sorted((CORPUS / variant).glob("*.log"))
        if logs:
            out.append({"variant": variant, "template": template, "split": s, "log": logs[0]})
    return out


def load_split() -> dict:
    return json.loads(SPLIT.read_text(encoding="utf-8"))


def ready_lanes() -> list[lanes.Lane]:
    found, notes = lanes.candidates(lanes.read_env())
    for n in notes:
        print(json.dumps({"lane_note": n}), flush=True)
    keep, report = lanes.independent(found, lanes.groq_probe())
    print(json.dumps({"lane_check": report}), flush=True)
    before = lanes.record_count(keep)
    if before is not None:
        print(json.dumps({"independent_lanes_changed": [before, len(keep)]}), flush=True)
    print(json.dumps({"independent_lanes": [lane.name for lane in keep]}), flush=True)
    return keep


def tag(level: str, repeat: int | None = None, provider: str = "groq") -> str:
    """The result set's name: one model and provider per set, never mixed (D40, D41)."""
    return (
        level + (f"-r{repeat}" if repeat else "") + ("" if provider == "groq" else f"-{provider}")
    )


def openrouter_lane() -> lanes.Lane:
    key = lanes.read_env().get("OPENROUTER_API_KEY", "")
    if not key:
        raise SystemExit("no OPENROUTER_API_KEY in .env")
    return lanes.Lane("OPENROUTER", "openrouter", key)


def run(level: str, repeat: int | None = None, provider: str = "groq", splits=DEV_SPLITS) -> None:
    if level not in ("L2", "L3"):
        raise SystemExit("only L2 is wired so far; L3 needs retrieval")
    if level == "L3":
        raise SystemExit("L3 retrieval is not built yet")
    todo = items(load_split(), splits)
    live = ready_lanes() if provider == "groq" else [openrouter_lane()]
    if not live:
        raise SystemExit("no usable lane")
    queues: dict[str, list[dict]] = {lane.name: [] for lane in live}
    for item in todo:
        queues[lanes.assign(item["variant"], live, LANE_SEED).name].append(item)
    OUT.mkdir(parents=True, exist_ok=True)
    name = tag(level, repeat, provider)
    out_path = OUT / f"{name}.jsonl"
    lock = threading.Lock()
    audit = AuditLog.open(level=level)

    def work(lane: lanes.Lane) -> None:
        extra = {}
        if lane.provider == "openrouter":  # D40: same settings, the free endpoint of the model
            extra = {"api": llm.OPENROUTER_API, "settings": llm.Settings(model=OPENROUTER_MODEL)}
        client = llm.LLMClient(
            api_key=lane.key, lane=lane.name, provider=lane.provider, audit=audit, **extra
        )
        for item in queues[lane.name]:
            log = item["log"].read_text(encoding="utf-8", errors="replace")
            try:
                res = client.complete(diagnose.messages(log), repeat=repeat)
            except llm.BudgetExhausted as exc:
                with lock:
                    print(json.dumps({"lane": lane.name, "stopped": str(exc)}), flush=True)
                return
            except Exception as exc:  # one failed call must not stop the lane
                with lock:
                    print(json.dumps({"variant": item["variant"], "error": str(exc)[:200]}))
                continue
            d = diagnose.parse(res["content"], log, apply_grounding=(level != "L2"))
            row = {
                "variant": item["variant"],
                "template": item["template"],
                "split": item["split"],
                "level": level,
                "lane": res["lane"],
                "provider": res["provider"],
                "backend": res.get("backend"),
                "model": res.get("model"),
                "repeat": repeat,
                "cached": res["cached"],
                "tokens": (res.get("usage") or {}).get("total_tokens"),
                "root_cause": d.root_cause,
                "scores": d.scores,
                "action": d.action,
                "fix": d.fix,
                "quotes": d.quotes,
                "grounded": d.grounded,
                "ungrounded": d.ungrounded,
                "rejected": d.rejected,
                "flags": d.flags,
            }
            with lock, out_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")

    print(json.dumps({"items": len(todo), "per_lane": {k: len(v) for k, v in queues.items()}}))
    threads = [threading.Thread(target=work, args=(lane,)) for lane in live]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    score(name)


def score(level: str) -> dict:
    rows = {}
    for line in (OUT / f"{level}.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        rows[r["variant"]] = r  # the last answer per variant (reruns are cached)
    truth = {
        v: json.loads((CORPUS / v / "meta.json").read_text(encoding="utf-8"))["root_cause"]
        for v in rows
    }
    out = {"level": level, "n": len(rows)}
    for s in DEV_SPLITS:
        sub = [r for r in rows.values() if r["split"] == s]
        k = sum(r["root_cause"] == truth[r["variant"]] for r in sub)
        out[s] = {"k": k, "n": len(sub), "ci95": stats.wilson(k, len(sub))}
    quotes = sum(len(r["quotes"]) for r in rows.values())
    bad = sum(r["ungrounded"] for r in rows.values())
    out["hallucination_rate"] = {"ungrounded": bad, "quotes": quotes}
    out["by_lane"] = dict(Counter(r["lane"] for r in rows.values()))
    out["confusion"] = dict(Counter(f"{truth[v]}->{r['root_cause']}" for v, r in rows.items()))
    (OUT / f"{level}-score.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cmd", choices=["run", "score"])
    parser.add_argument("--level", default="L2")
    parser.add_argument("--repeat", type=int, choices=[2, 3])
    parser.add_argument("--provider", default="groq", choices=["groq", "openrouter"])
    parser.add_argument("--splits", nargs="+", default=list(DEV_SPLITS))
    args = parser.parse_args(argv)
    if args.cmd == "run":
        run(args.level, args.repeat, args.provider, tuple(args.splits))
    else:
        score(tag(args.level, args.repeat, args.provider))
    return 0


if __name__ == "__main__":
    sys.exit(main())
