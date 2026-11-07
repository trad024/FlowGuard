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
- Request size (D66): Groq refuses (413) a request whose prompt plus max_tokens exceeds the
  lane's tokens per minute (`tpm_limit`). `fits` counts the prompt with tiktoken's o200k_base
  encoding plus a margin (Groq counted 102 tokens more than o200k_base on every one of 147
  dev calls, the chat format: 110, plus 5% of the count); callers cut the log until it fits
  (flowguard.diagnose.request). A 413 that still happens is written to the audit log as a
  failed call and raised as RequestTooLarge, for the agent to escalate.
- Lanes (D41): a client is one lane, one person's Groq account (`lane`, e.g. GROQ_KEY_1)
  with its own token ledger; every call records its lane and provider. The cache is shared:
  the key covers the model and settings, not the lane, so lanes with identical pinned settings
  reuse each other's answers and a different model never does.
  See flowguard.lanes for discovery and assignment.

Standard library, plus tiktoken for counting (its encoding file is cached under
state/tiktoken/ and checked against tiktoken's pinned hash). Default state: state/llm-cache/
and state/llm-usage.json (gitignored).
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
# D66: the request budget. TPM_LIMIT is the free tier's tokens per minute per lane, the one
# place it is set; a client's tpm_limit defaults to it.
TPM_LIMIT = 8_000
COUNT_ENCODING = "o200k_base"
OVERHEAD_TOKENS = 110  # Groq's chat format: 102 tokens over o200k_base on all 147 dev calls
MARGIN_SHARE = 0.05
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


class RequestTooLarge(RuntimeError):
    """Groq refused the request (413): prompt plus max_tokens over the tokens per minute."""


_encoding = None


def count_tokens(text: str) -> int:
    """Tokens of `text` in o200k_base (gpt-oss's encoding family), the counter of D66."""
    global _encoding
    if _encoding is None:
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(STATE / "tiktoken"))
        import tiktoken

        _encoding = tiktoken.get_encoding(COUNT_ENCODING)
    return len(_encoding.encode(text, disallowed_special=()))


def prompt_tokens(messages: list[dict], count=count_tokens) -> int:
    """The counted prompt: every message's content."""
    return sum(count(m["content"]) for m in messages)


def margin(counted: int) -> int:
    """D66: the worst undercount seen (102, rounded up to 110) plus 5% of the count."""
    return OVERHEAD_TOKENS + -(-counted * 5 // 100)


@dataclass(frozen=True)
class Settings:
    model: str = "openai/gpt-oss-120b"
    temperature: float = 0.0
    reasoning_effort: str = "low"
    max_tokens: int = 600  # D51: answers measured so far stay under 360 tokens


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
    provider: str = "groq"  # the only provider (D46 dropped OpenRouter)
    tpm_limit: int = TPM_LIMIT  # D66; not in Settings, so it never changes a cache key
    count: object = count_tokens

    def fits(self, messages: list[dict]) -> bool:
        """D66: counted prompt + margin + max_tokens within the lane's tokens per minute."""
        counted = prompt_tokens(messages, self.count)
        return counted + margin(counted) + self.settings.max_tokens <= self.tpm_limit

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

    def complete(
        self,
        messages: list[dict],
        repeat: int | None = None,
        retries: int = 5,
        request: dict | None = None,
    ) -> dict:
        """{"content", "usage", "cached", "rate"}; from the cache when this exact request ran.
        `request` (D66: log tokens before and after the cut, whether it was cut) goes to the
        audit log with the call; it is not part of the cache key."""
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
                if exc.code == 413:  # D66: on the record as a failed call, never retried
                    if self.audit is not None:
                        self.audit.event(
                            "llm",
                            "llm_call_failed",
                            model=self.settings.model,
                            repeat=repeat,
                            cache_key=k,
                            lane=self.lane,
                            provider=self.provider,
                            status=413,
                            reason="request too large",
                            **(request or {}),
                        )
                    raise RequestTooLarge(f"413 on lane {self.lane}") from None
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
                **{
                    f"tokens_{n}": usage.get(f"{n}_tokens")
                    for n in ("prompt", "completion", "total")
                },
                **(request or {}),
            )
        return {**result, "cached": False}


def from_env(env: dict | None = None, **kw) -> LLMClient:
    env = env if env is not None else os.environ
    return LLMClient(api_key=env["LLM_API_KEY"], **kw)
