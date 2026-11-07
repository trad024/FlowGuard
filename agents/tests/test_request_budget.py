"""D66: a Diagnose request fits the lane's tokens per minute. The reduced log is cut from the
front, by whole lines, until counted prompt + margin + max_tokens fits; the instructions and
the examples never change; a 413 that still happens is on the record and escalated.
A counter of 1 token per 2 characters stands in for tiktoken; the API is a local fake server."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from flowguard import diagnose, llm, logreduce
from flowguard.agent_io import AgentInput
from flowguard.audit import AuditLog
from flowguard.orchestrator import level
from flowguard.stubs import Diagnose


def count(text: str) -> int:
    return -(-len(text) // 2)  # denser than any dev log, so a 14,000-character log overflows


def go_dependency_log(lines: int = 400) -> str:
    """A long synthetic Go module failure: module paths and hashes, no word shared between
    lines, so drain3 keeps them and the reduced log reaches its 14,000 characters."""
    body = [
        " ".join(f"{(i * 31 + j) * 7919:x}/mod{i}{j}@v{j}.{i}" for j in range(3 + i % 6))
        for i in range(lines)
    ]
    return "\n".join([*body, "verifying example.com/broken@v9.9.9: checksum mismatch", "FAIL"])


EXAMPLES = [
    diagnose.Example(f"example log {i} " * 80, "dependency", "pin_dependency") for i in range(3)
]


def client(**kw) -> llm.LLMClient:
    return llm.LLMClient(api_key="test-key", count=count, **kw)


def test_long_go_dependency_log_is_cut_to_fit():
    c = client()
    log = go_dependency_log()
    assert not c.fits(diagnose.messages(log, EXAMPLES))  # over budget as built before D66
    msgs, req = diagnose.request(log, EXAMPLES, c.fits, count)
    assert c.fits(msgs) and req["log_cut"] is True
    assert req["log_tokens_after"] < req["log_tokens_before"]
    shown = msgs[1]["content"].split("Failed job log:\n", 1)[1]
    assert shown.endswith("FAIL") and "checksum mismatch" in shown  # the failure is at the end
    kept = logreduce.reduce(log)
    numbers = [int(line.split(":", 1)[0][1:]) for line in shown.splitlines()]
    assert numbers == [n for n, _ in kept[-len(numbers) :]]  # whole lines, dropped from the front
    # one more line would not fit: the cut is the smallest that fits
    wider = diagnose._messages(kept[-len(numbers) - 1 :], EXAMPLES)
    assert not c.fits(wider)


def test_margin_is_overhead_plus_five_percent():
    assert llm.margin(0) == 110 and llm.margin(1000) == 160 and llm.margin(1001) == 161
    c = client(tpm_limit=1000)  # 400 left after max_tokens: 276 counted + 124 margin
    assert c.fits([{"content": "a" * 552}]) and not c.fits([{"content": "a" * 554}])


def test_short_log_is_untouched():
    c = client()
    log = "./main.go:3:2: undefined: foo\nFAIL\n"
    msgs, req = diagnose.request(log, EXAMPLES, c.fits, count)
    assert msgs == diagnose.messages(log, EXAMPLES)
    assert req["log_cut"] is False and req["log_tokens_before"] == req["log_tokens_after"]


def test_without_a_budget_the_request_is_the_old_one():
    log = go_dependency_log()
    msgs, req = diagnose.request(log, EXAMPLES)
    assert msgs == diagnose.messages(log, EXAMPLES) and req == {}


def test_cut_is_deterministic():
    c = client()
    log = go_dependency_log()
    first = diagnose.request(log, EXAMPLES, c.fits, count)
    assert all(diagnose.request(log, EXAMPLES, c.fits, count) == first for _ in range(3))


def test_system_prompt_and_examples_are_unchanged():
    c = client()
    log = go_dependency_log()
    msgs, req = diagnose.request(log, EXAMPLES, c.fits, count)
    full = diagnose.messages(log, EXAMPLES)
    assert req["log_cut"]
    assert msgs[0] == full[0] == {"role": "system", "content": diagnose.SYSTEM}
    head = full[1]["content"].split("Failed job log:\n", 1)[0]
    assert msgs[1]["content"].split("Failed job log:\n", 1)[0] == head
    for ex in EXAMPLES:
        assert ex.log[-diagnose.EXAMPLE_CHARS :] in head


# ---------------------------------------------------------------- 413


class Refuses(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):  # noqa: N802 (http.server API)
        self.rfile.read(int(self.headers["Content-Length"]))
        Refuses.calls += 1
        body = json.dumps(
            {"error": {"message": "Request too large", "code": "rate_limit_exceeded"}}
        )
        self.send_response(413)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def refusing(tmp_path):
    Refuses.calls = 0
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Refuses)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    audit = AuditLog(tmp_path / "audit.db", "s-1", "L3")
    c = client(
        cache_dir=tmp_path / "cache",
        ledger=tmp_path / "usage.json",
        api=f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions",
        audit=audit,
        sleep=lambda s: None,
    )
    yield c, audit
    httpd.shutdown()
    httpd.server_close()


class OneExample:
    def nearest(self, log):
        return EXAMPLES


@pytest.mark.parametrize("name", ["L2", "L3"])
def test_413_is_recorded_and_escalated(refusing, name):
    c, audit = refusing
    agent = Diagnose(llm=diagnose.answerer(c.complete), retriever=OneExample())
    inp = AgentInput(incident_id="i-1", environment="staging", target="podinfo",
                     log=go_dependency_log())  # fmt: skip
    (p,) = agent.run(inp, level(name)).proposals  # no exception
    assert p.action == "escalate_to_human"
    assert p.params["diagnose_root_cause"] == "unclear"
    assert p.params["diagnose_llm_failed"] == diagnose.FAILED
    assert p.params["diagnose_request"]["log_cut"] is True  # the cut was tried first
    assert Refuses.calls == 1  # never retried
    (failed,) = list(audit.events("llm_call_failed"))
    payload = (
        json.loads(failed["payload"]) if isinstance(failed["payload"], str) else failed["payload"]
    )
    assert payload["status"] == 413 and payload["log_cut"] is True
    assert "log_tokens_before" in payload and "log_tokens_after" in payload
    assert not list(audit.events("llm_call"))
    assert not list((c.cache_dir).glob("*.json"))  # a refused call is not cached


def test_413_raises_request_too_large_from_the_client(refusing):
    c, _ = refusing
    with pytest.raises(llm.RequestTooLarge):
        c.complete([{"role": "user", "content": "x"}])
