"""LLM client (D27, D28): pinned settings, cache, repeat key, daily budget, 429 retry.
The API is a local fake server; nothing leaves the machine."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from flowguard import llm
from flowguard.audit import AuditLog


class FakeGroq(BaseHTTPRequestHandler):
    bodies: list = []
    throttle = 0  # answer 429 this many times first

    def do_POST(self):  # noqa: N802 (http.server API)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeGroq.bodies.append(body)
        if FakeGroq.throttle:
            FakeGroq.throttle -= 1
            self.send_response(429)
            self.send_header("retry-after", "2")
            self.end_headers()
            return
        out = {
            "model": body["model"],
            "choices": [{"message": {"content": '{"root_cause": "code"}'}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 100, "total_tokens": 1000},
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("x-ratelimit-limit-requests", "1000")
        self.send_header("x-ratelimit-limit-tokens", "8000")
        self.end_headers()
        self.wfile.write(json.dumps(out).encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def client(tmp_path):
    FakeGroq.bodies, FakeGroq.throttle = [], 0
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeGroq)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    waits = []
    c = llm.LLMClient(
        api_key="test-key",
        daily_token_limit=10_000,
        cache_dir=tmp_path / "cache",
        ledger=tmp_path / "usage.json",
        api=f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions",
        audit=AuditLog(tmp_path / "audit.db"),
        sleep=waits.append,
    )
    c.waits = waits
    yield c
    httpd.shutdown()
    httpd.server_close()


MESSAGES = [{"role": "user", "content": "classify this log"}]


def test_pinned_settings_are_sent_and_logged(client):
    out = client.complete(MESSAGES)
    (body,) = FakeGroq.bodies
    assert (body["model"], body["temperature"], body["reasoning_effort"]) == (
        "openai/gpt-oss-120b",
        0.0,
        "low",
    )
    assert out["usage"]["total_tokens"] == 1000 and out["cached"] is False
    assert out["rate"]["x-ratelimit-limit-requests"] == "1000"
    (event,) = client.audit.events("llm_call")
    assert event["payload"]["model"] == "openai/gpt-oss-120b"
    assert event["payload"]["tokens_total"] == 1000


def test_a_request_is_never_sent_twice_but_each_repeat_is_one_call(client):
    client.complete(MESSAGES)
    again = client.complete(MESSAGES)
    assert again["cached"] is True and len(FakeGroq.bodies) == 1
    for repeat in (1, 2, 3):  # D28: the repeat index is part of the key
        client.complete(MESSAGES, repeat=repeat)
    assert len(FakeGroq.bodies) == 4
    client.complete(MESSAGES, repeat=2)
    assert len(FakeGroq.bodies) == 4
    assert client.used_today() == 4000


def test_stops_at_90_percent_of_the_daily_budget(client):
    for i in range(9):
        client.complete([{"role": "user", "content": f"log {i}"}])
    assert client.used_today() == 9000
    with pytest.raises(llm.BudgetExhausted):
        client.complete([{"role": "user", "content": "one more"}])
    assert client.complete([{"role": "user", "content": "log 3"}])["cached"]  # cache still works


def test_a_429_is_retried_after_its_delay(client):
    FakeGroq.throttle = 2
    out = client.complete(MESSAGES)
    assert out["content"] and client.waits == [2.0, 2.0] and len(FakeGroq.bodies) == 3
