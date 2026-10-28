"""LLM lanes (D40, D41): team members' own Groq accounts, run in parallel with identical settings.

A lane is one GROQ_KEY_n from .env, i.e. one person's free-tier account. `.env` is re-read at
the start of every run, so a key added later becomes a lane without a restart. Before use:

- identical values count once, and keys Groq rejects (401) are dropped;
- keys of one organization share its quota, so they count as one lane. `independent` calls
  every key twice, in the same order: between a key's two calls every key ran once, so its
  remaining-requests-per-day counter drops by 1 when its organization is its own and by 1 + m
  when m other keys share it.

Items are assigned to lanes by a seeded hash of the item id, so all of an item's calls run on
one lane; no key is ever rotated to get around a limit. Keys never appear in a repr or a log.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from flowguard.llm import API, ROOT, STATE, Settings

ENV_PATH = ROOT / ".env"
LANES_STATE = STATE / "llm-lanes.json"


class InvalidKey(RuntimeError):
    """The provider rejected the key (401)."""


@dataclass(frozen=True)
class Lane:
    name: str
    provider: str
    key: str = field(repr=False)


def read_env(path: Path = ENV_PATH) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("\"'")
    return out


def candidates(env: dict[str, str]) -> tuple[list[Lane], list[str]]:
    """GROQ_KEY_n with a value, by n; a value already seen is noted and skipped."""
    names = sorted(
        (k for k in env if k.startswith("GROQ_KEY_") and k[9:].isdigit() and env[k]),
        key=lambda k: int(k[9:]),
    )
    lanes, notes, seen = [], [], {}
    for name in names:
        if env[name] in seen:
            notes.append(f"{name} has the same value as {seen[env[name]]}: skipped")
            continue
        seen[env[name]] = name
        lanes.append(Lane(name, "groq", env[name]))
    return lanes, notes


def independent(
    lanes: list[Lane], probe: Callable[[Lane], int]
) -> tuple[list[Lane], dict[str, dict]]:
    """One lane per organization. `probe` makes one tiny call and returns the remaining
    requests per day; it raises InvalidKey for a rejected key."""
    rounds: list[dict[str, int | None]] = [{}, {}]
    for r in rounds:
        for lane in lanes:
            try:
                r[lane.name] = probe(lane)
            except InvalidKey:
                r[lane.name] = None
    report, shared = {}, []
    for lane in lanes:
        a, b = rounds[0][lane.name], rounds[1][lane.name]
        if a is None or b is None:
            report[lane.name] = {"status": "invalid key"}
            continue
        drop = a - b
        report[lane.name] = {"status": "ok", "drop": drop}
        if drop > 1:
            shared.append((drop, b, lane))
    keep = [lane for lane in lanes if report[lane.name].get("drop") == 1]
    # keys sharing an organization: groups of `drop` keys, one lane kept per group
    shared.sort(key=lambda s: (s[0], s[1]))
    i = 0
    while i < len(shared):
        size = shared[i][0]
        group = sorted((g[2] for g in shared[i : i + size]), key=lanes.index)
        keep.append(group[0])
        for lane in group:
            report[lane.name]["organization_shared_with"] = [g.name for g in group]
        i += size
    keep.sort(key=lambda lane: lanes.index(lane))
    return keep, report


PINNED = Settings()


def groq_probe(api: str = API, settings: Settings = PINNED) -> Callable[[Lane], int]:
    def probe(lane: Lane) -> int:
        body = {
            "model": settings.model,
            "messages": [{"role": "user", "content": "Reply with the word ok."}],
            "temperature": settings.temperature,
            "reasoning_effort": settings.reasoning_effort,
            "max_tokens": 32,
        }
        req = urllib.request.Request(
            api,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {lane.key}",
                "Content-Type": "application/json",
                "User-Agent": "flowguard",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return int(r.headers.get("x-ratelimit-remaining-requests") or 0)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise InvalidKey(lane.name) from None
            raise

    return probe


def assign(item_id: str, lanes: list[Lane], seed: str) -> Lane:
    """The item's lane: a seeded hash over the lanes sorted by name."""
    ordered = sorted(lanes, key=lambda lane: lane.name)
    h = int(hashlib.sha256(f"{seed}:{item_id}".encode()).hexdigest(), 16)
    return ordered[h % len(ordered)]


def record_count(lanes: list[Lane], path: Path = LANES_STATE) -> int | None:
    """Store today's lane names; returns the previous count if it changed, else None."""
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    names = [lane.name for lane in lanes]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"lanes": names, "checked_at": datetime.now(UTC).isoformat()}, indent=1),
        encoding="utf-8",
    )
    before = len(previous.get("lanes", [])) if previous else 0
    return before if before != len(names) else None
