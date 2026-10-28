"""LogChunks evidence targets (D32, D43): where each human-labelled chunk sits in its log.

    python evaluation/logchunks.py locate     # all 797 logs -> datasets/logchunks-located.csv

A log's target is the block of its lines that holds the labelled chunk. Lines are compared after
removing ANSI codes and carriage-return overwrites (only the text after the last \\r is what a
terminal shows) and collapsing whitespace; a chunk line matches a log line that contains it.
Status per log:
  located        every non-blank chunk line matched, in order, on consecutive non-blank lines
  partly         some chunk lines matched, not the whole block
  not_located    no chunk line matched
Only `located` logs are used for H3 (D43); the others are excluded and counted in the report.
This prepares the targets only: nothing is run on LogChunks before Step 11.

Committed: identifiers, status, target line numbers and each log's SHA-256; no log text.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "evaluation" / "datasets" / "raw" / "LogChunks"
OUT = ROOT / "evaluation" / "datasets" / "logchunks-located.csv"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def norm(line: str) -> str:
    line = ANSI.sub("", line).rstrip("\r")
    if "\r" in line:
        line = line.rsplit("\r", 1)[1]
    return " ".join(line.split())


def locate(log: str, chunk: str) -> tuple[str, list[int]]:
    """(status, 1-based target line numbers in the log)."""
    lines = [norm(x) for x in log.split("\n")]
    want = [c for c in (norm(x) for x in chunk.split("\n")) if c]
    if not want:
        return "not_located", []
    filled = [i for i, line in enumerate(lines) if line]
    for start in range(len(filled)):
        if want[0] not in lines[filled[start]]:
            continue
        block = filled[start : start + len(want)]
        if len(block) == len(want) and all(w in lines[i] for w, i in zip(want, block, strict=True)):
            return "located", [i + 1 for i in block]
    hits = sorted({i + 1 for w in want for i in filled if w in lines[i]})
    return ("partly", hits) if hits else ("not_located", [])


def examples(raw: Path = RAW) -> list[dict]:
    """One entry per failed log with a labelled chunk, first label wins (as datasets.py)."""
    seen: dict[str, dict] = {}
    for xml in sorted((raw / "build-failure-reason").rglob("*.xml")):
        for ex in ET.parse(xml).getroot().iter("Example"):
            rel = (ex.findtext("Log") or "").strip()
            parts = rel.split("/")
            if len(parts) != 4 or parts[2] != "failed" or rel in seen:
                continue
            if (raw / "logs" / rel).is_file():
                seen[rel] = {"log_path": rel, "repository": parts[1], "chunk": ex.findtext("Chunk")}
    return [seen[k] for k in sorted(seen)]


def run(raw: Path = RAW, out: Path = OUT) -> dict:
    rows, counts = [], {"located": 0, "partly": 0, "not_located": 0}
    for ex in examples(raw):
        data = (raw / "logs" / ex["log_path"]).read_bytes()
        status, target = locate(data.decode("utf-8", errors="replace"), ex["chunk"] or "")
        counts[status] += 1
        rows.append(
            {
                "log_path": ex["log_path"],
                "repository": ex["repository"],
                "status": status,
                "target_lines": " ".join(map(str, target)),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    with out.open("w", encoding="utf-8", newline="\n") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    summary = {"logs": len(rows), **counts}
    print(json.dumps(summary))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cmd", choices=["locate"])
    parser.parse_args(argv)
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
