"""LLM client (D27, D28): one pinned model, a response cache, a daily token budget.

- Pinned settings: model id, temperature 0 and reasoning effort, sent on every call and written
  to the audit log with the tokens used.
- Cache: every response is stored under a hash of (model, settings, messages, repeat) and a
  request is never sent twice. `repeat` (1 to 3) is set only for the D18 repeat subset, so each
  repeat is one real call, cached for reproduction (D28).
- Daily budget: Groq's headers give requests per day and tokens per minute, but not tokens per
  day, so the client keeps its own ledger of tokens per UTC day (from each response's usage) and
  stops at 90% of the configured daily limit; a run is resumable, as finished calls are cached.
  A 429 is retried after its retry-after delay.
- Lanes (D40, D41): a client is one lane, one person's or provider's account (`lane`, e.g.
  GROQ_KEY_1) with its own token ledger; every call records its lane, provider and, for
  OpenRouter, the backend that served it. The cache is shared: the key covers the model and
  settings, not the lane, so lanes with identical pinned settings reuse each other's answers
  and a different model never does. See flowguard.lanes for discovery and assignment.

Standard library only. Default state: state/llm-cache/ and state/llm-usage.json (gitignored).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "state"
API = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_API = "https://openrouter.ai/api/v1/chat/completions"
RATE_HEADERS = (
    "x-ratelimit-limit-requests",  # requests per day
    "x-ratelimit-remaining-requests",
    "x-ratelimit-limit-tokens",  # tokens per minute
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
)


class BudgetExhausted(RuntimeError):
    """90% of today's token budget is used: resume after the UTC day changes."""


@dataclass(frozen=True)
class Settings:
    model: str = "openai/gpt-oss-120b"
    temperature: float = 0.0
    reasoning_effort: str = "low"
    max_tokens: int = 1024


@dataclass
class LLMClient:
    api_key: str = field(repr=False)
    settings: Settings = field(default_factory=Settings)
    daily_token_limit: int = 200_000
    stop_at: float = 0.9
    cache_dir: Path = STATE / "llm-cache"
    ledger: Path = STATE / "llm-usage.json"
    api: str = API
    audit: object | None = None  # a flowguard.audit.AuditLog, if calls should be logged
    sleep: object = time.sleep
    lane: str = "default"
    provider: str = "groq"  # or "openrouter" (development only, D40)

    def __post_init__(self):
        if self.lane != "default" and self.ledger == STATE / "llm-usage.json":
            self.ledger = STATE / f"llm-usage-{self.lane}.json"

    def key(self, messages: list[dict], repeat: int | None) -> str:
        body = {"settings": self.settings.__dict__, "messages": messages, "repeat": repeat}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    # ------------------------------------------------------------ ledger

    def _today(self) -> str:
        return datetime.now(UTC).date().isoformat()

    def used_today(self) -> int:
        try:
            return int(json.loads(self.ledger.read_text(encoding="utf-8")).get(self._today(), 0))
        except (OSError, ValueError):
            return 0

    def _spend(self, tokens: int) -> None:
        try:
            data = json.loads(self.ledger.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[self._today()] = int(data.get(self._today(), 0)) + tokens
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")

    # ------------------------------------------------------------ calls

    def complete(self, messages: list[dict], repeat: int | None = None, retries: int = 5) -> dict:
        """{"content", "usage", "cached", "rate"}; from the cache when this exact request ran."""
        k = self.key(messages, repeat)
        path = self.cache_dir / f"{k}.json"
        if path.is_file():
            return {**json.loads(path.read_text(encoding="utf-8")), "cached": True}
        if self.used_today() >= self.stop_at * self.daily_token_limit:
            raise BudgetExhausted(f"{self.used_today()} tokens used today")
        body = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "reasoning_effort": self.settings.reasoning_effort,
            "max_tokens": self.settings.max_tokens,
            "response_format": {"type": "json_object"},
        }
        if self.provider == "openrouter":
            body["reasoning"] = {"effort": body.pop("reasoning_effort")}
        req = urllib.request.Request(
            self.api,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "flowguard",
            },
            method="POST",
        )
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = json.loads(r.read())
                    rate = {h: r.headers.get(h) for h in RATE_HEADERS}
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt == retries - 1:
                    raise
                self.sleep(float(exc.headers.get("retry-after") or 10))
        usage = data.get("usage", {})
        result = {
            "content": data["choices"][0]["message"].get("content") or "",
            "usage": usage,
            "model": data.get("model"),
            "rate": rate,
            "lane": self.lane,
            "provider": self.provider,
            "backend": data.get("provider"),  # OpenRouter's serving backend; None on Groq
        }
        self._spend(int(usage.get("total_tokens", 0)))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result), encoding="utf-8")
        if self.audit is not None:
            self.audit.event(
                "llm",
                "llm_call",
                model=self.settings.model,
                temperature=self.settings.temperature,
                reasoning_effort=self.settings.reasoning_effort,
                repeat=repeat,
                cache_key=k,
                lane=self.lane,
                provider=self.provider,
                backend=result["backend"],
                **{
                    f"tokens_{n}": usage.get(f"{n}_tokens")
                    for n in ("prompt", "completion", "total")
                },
            )
        return {**result, "cached": False}


def from_env(env: dict | None = None, **kw) -> LLMClient:
    env = env if env is not None else os.environ
    return LLMClient(api_key=env["LLM_API_KEY"], **kw)
