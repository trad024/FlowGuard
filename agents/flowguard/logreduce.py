"""CI log reduction for Diagnose (Step 7): what the model sees, with original line numbers.

1. Cut at "Post job cleanup" (GitHub Actions' teardown, after the failure).
2. Split on newlines only and keep what a terminal shows, the text after a line's last
   carriage return (progress bars overwrite themselves); strip ANSI codes; cut each line
   to 300 characters. Line numbers thus match the LogChunks targets (D48).
3. drain3 groups lines into templates (variable parts masked); only the first 2 lines of each
   template are kept, so repeated progress or download lines collapse.
4. If more than 200 lines remain, the last 200 are kept: failures are at the end.
5. If the rendered lines exceed 14,000 characters (about 3,500 tokens), lines are dropped
   from the front until they fit: one request must stay under the free tier's 8,000
   tokens per minute with the instructions, L3's examples and the answer.

Every kept line keeps its number in the original log ("L<n>: text"), so evidence can be checked
against the real log (D8).
"""

from __future__ import annotations

import re

from drain3 import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig

STOP = "Post job cleanup"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
MAX_LINE = 300
PER_TEMPLATE = 2
MAX_LINES = 200
MAX_CHARS = 14_000


def original_lines(log: str) -> list[str]:
    """The log as the model's line numbers refer to it: cut at STOP, terminal view, no ANSI."""
    lines = []
    for line in log.split("\n"):
        line = line.rstrip("\r")
        if "\r" in line:
            line = line.rsplit("\r", 1)[1]
        if STOP in line:
            break
        lines.append(ANSI.sub("", line).rstrip())
    return lines


def _miner() -> TemplateMiner:
    config = TemplateMinerConfig()  # defaults; never reads a drain3.ini from the working dir
    config.profiling_enabled = False
    config.masking_instructions = []
    return TemplateMiner(config=config)


def reduce(log: str) -> list[tuple[int, str]]:
    """(original line number, text) for the lines the model sees."""
    miner, seen, kept = _miner(), {}, []
    for n, line in enumerate(original_lines(log), start=1):
        text = line[:MAX_LINE]
        if not text.strip():
            continue
        cluster = miner.add_log_message(text)["cluster_id"]
        seen[cluster] = seen.get(cluster, 0) + 1
        if seen[cluster] <= PER_TEMPLATE:
            kept.append((n, text))
    kept = kept[-MAX_LINES:]
    while len(kept) > 1 and len(render(kept)) > MAX_CHARS:
        kept = kept[1:]
    return kept


def render(lines: list[tuple[int, str]]) -> str:
    return "\n".join(f"L{n}: {text}" for n, text in lines)
