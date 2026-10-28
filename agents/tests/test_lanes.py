"""LLM lanes (D41): discovery from .env, one lane per organization, seeded assignment,
lane recorded per call. No network: probes are fakes and the API a local server."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flowguard import lanes, llm
from flowguard.audit import AuditLog


def test_candidates_skip_empty_and_duplicate_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "GROQ_KEY_2=bbb\nGROQ_KEY_1=aaa\nGROQ_KEY_3=\nGROQ_KEY_10=aaa\nOTHER=x\n", encoding="utf-8"
    )
    found, notes = lanes.candidates(lanes.read_env(env))
    assert [lane.name for lane in found] == ["GROQ_KEY_1", "GROQ_KEY_2"]
    assert notes == ["GROQ_KEY_10 has the same value as GROQ_KEY_1: skipped"]
    assert "aaa" not in repr(found)  # keys never appear in a repr


def test_the_project_leads_key_is_a_candidate_after_the_groq_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LLM_API_KEY=ccc\nGROQ_KEY_1=aaa\nGROQ_KEY_2=bbb\n", encoding="utf-8")
    found, _ = lanes.candidates(lanes.read_env(env))
    assert [lane.name for lane in found] == ["GROQ_KEY_1", "GROQ_KEY_2", "LLM_API_KEY"]
    env.write_text("LLM_API_KEY=aaa\nGROQ_KEY_1=aaa\n", encoding="utf-8")
    found, notes = lanes.candidates(lanes.read_env(env))
    assert [lane.name for lane in found] == ["GROQ_KEY_1"]
    assert notes == ["LLM_API_KEY has the same value as GROQ_KEY_1: skipped"]


def test_the_project_leads_account_counts_once_when_it_shares_an_organization():
    found = [
        lanes.Lane("GROQ_KEY_1", "groq", "k1"),
        lanes.Lane("GROQ_KEY_2", "groq", "k2"),
        lanes.Lane("LLM_API_KEY", "groq", "k9"),
    ]
    keep, report = lanes.independent(found, Orgs({"k1": "a", "k2": "b", "k9": "b"}))
    assert [lane.name for lane in keep] == ["GROQ_KEY_1", "GROQ_KEY_2"]
    assert report["LLM_API_KEY"]["organization_shared_with"] == ["GROQ_KEY_2", "LLM_API_KEY"]


class Orgs:
    """Fake provider: keys map to organizations; each call spends one of the org's requests."""

    def __init__(self, org_of):
        self.org_of, self.remaining = org_of, {o: 1000 for o in set(org_of.values()) if o}

    def __call__(self, lane):
        org = self.org_of[lane.key]
        if org is None:
            raise lanes.InvalidKey(lane.name)
        self.remaining[org] -= 1
        return self.remaining[org]


def test_one_lane_per_organization_and_invalid_keys_dropped():
    found = [lanes.Lane(f"GROQ_KEY_{i}", "groq", f"k{i}") for i in range(1, 7)]
    orgs = Orgs({"k1": "a", "k2": "b", "k3": "c", "k4": "c", "k5": None, "k6": "d"})
    keep, report = lanes.independent(found, orgs)
    assert [lane.name for lane in keep] == ["GROQ_KEY_1", "GROQ_KEY_2", "GROQ_KEY_3", "GROQ_KEY_6"]
    assert report["GROQ_KEY_5"] == {"status": "invalid key"}
    assert report["GROQ_KEY_4"]["organization_shared_with"] == ["GROQ_KEY_3", "GROQ_KEY_4"]


def test_assignment_is_seeded_and_stable():
    two = [lanes.Lane("GROQ_KEY_1", "groq", "a"), lanes.Lane("GROQ_KEY_2", "groq", "b")]
    picks = [lanes.assign(f"item-{i}", two, "s").name for i in range(200)]
    assert picks == [lanes.assign(f"item-{i}", list(reversed(two)), "s").name for i in range(200)]
    assert 70 < picks.count("GROQ_KEY_1") < 130  # roughly balanced
    assert picks != [lanes.assign(f"item-{i}", two, "other").name for i in range(200)]


def test_lane_count_changes_are_reported(tmp_path):
    state = tmp_path / "lanes.json"
    one = [lanes.Lane("GROQ_KEY_1", "groq", "a")]
    assert lanes.record_count(one, state) == 0  # first run: from nothing to one
    assert lanes.record_count(one, state) is None
    assert lanes.record_count([*one, lanes.Lane("GROQ_KEY_2", "groq", "b")], state) == 1


class Fake(BaseHTTPRequestHandler):
    bodies: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.bodies.append(body)
        out = {
            "model": body["model"],
            "provider": "SomeBackend",
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"total_tokens": 10},
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(out).encode())

    def log_message(self, *args):
        pass


def test_each_call_records_lane_and_provider(tmp_path):
    Fake.bodies = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        c = llm.LLMClient(
            api_key="secret-key",
            cache_dir=tmp_path / "cache",
            ledger=tmp_path / "usage.json",
            api=f"http://127.0.0.1:{httpd.server_address[1]}/v1",
            audit=AuditLog(tmp_path / "audit.db"),
            lane="GROQ_KEY_2",
        )
        out = c.complete([{"role": "user", "content": "x"}])
        assert (out["lane"], out["provider"]) == ("GROQ_KEY_2", "groq")
        assert Fake.bodies[0]["reasoning_effort"] == "low"
        (event,) = c.audit.events("llm_call")
        assert event["payload"]["lane"] == "GROQ_KEY_2"
        assert event["payload"]["provider"] == "groq"
        assert "secret-key" not in repr(c)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_named_lane_keeps_its_own_ledger():
    assert llm.LLMClient(api_key="k", lane="GROQ_KEY_2").ledger.name == "llm-usage-GROQ_KEY_2.json"
