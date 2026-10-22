"""Load generator: response parsing, the JSONL log, the per-hash summary, and a short real run
against a local HTTP server that answers like podinfo."""

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import loadgen


class FakePodinfo(BaseHTTPRequestHandler):
    """Answers GET / like podinfo; GET /fail answers 500 with podinfo's empty error body."""

    def do_GET(self):  # noqa: N802 (http.server API)
        if self.path == "/fail":
            self.send_response(500)
            self.end_headers()
            return
        body = json.dumps({"hostname": "podinfo-6d8f7c9b5-x2x9k", "version": "6.7.1"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakePodinfo)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_pod_from_body_reads_podinfo_hostname():
    assert loadgen.pod_from_body(b'{"hostname": "podinfo-abc-1", "x": 1}') == "podinfo-abc-1"


@pytest.mark.parametrize("body", [b"", b"not json", b"[1, 2]", b'{"hostname": ""}', b"\xff\xfe"])
def test_pod_from_body_returns_none_for_anything_else(body):
    assert loadgen.pod_from_body(body) is None


@pytest.mark.parametrize(
    ("pod", "expected"),
    [
        ("podinfo-6d8f7c9b5-x2x9k", "6d8f7c9b5"),
        ("my-app-6d8f7c9b5-x2x9k", "6d8f7c9b5"),
        ("podinfo", None),
        (None, None),
    ],
)
def test_pod_template_hash(pod, expected):
    assert loadgen.pod_template_hash(pod) == expected


def test_probe_logs_status_latency_and_pod(server):
    record = loadgen.probe("staging", server + "/", timeout=2)
    assert record.status == 200
    assert record.pod == "podinfo-6d8f7c9b5-x2x9k"
    assert record.latency_ms >= 0
    assert record.error is None
    assert loadgen.parse_ts(record.ts).tzinfo is not None


def test_probe_keeps_http_errors_as_statuses(server):
    record = loadgen.probe("staging", server + "/fail", timeout=2)
    assert (record.status, record.pod, record.error) == (500, None, None)


def test_probe_logs_transport_errors_without_status():
    # Port 9 (discard) is closed on a normal laptop and CI runner
    record = loadgen.probe("prod", "http://127.0.0.1:9/", timeout=1)
    assert record.status is None
    assert record.error


def test_writer_appends_one_json_object_per_line(tmp_path):
    out = tmp_path / "runs" / "log.jsonl"
    writer = loadgen.JsonlWriter(out)
    writer.write(loadgen.Record("2026-10-22T10:00:00.000Z", "staging", 200, 1.5, "p-a-b"))
    writer.write(loadgen.Record("2026-10-22T10:00:01.000Z", "prod", None, 9.0, None, "boom"))
    writer.close()
    lines = out.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["namespace"] for line in lines] == ["staging", "prod"]
    assert set(json.loads(lines[0])) == {"ts", "namespace", "status", "latency_ms", "pod", "error"}


def test_run_sends_a_fixed_rate_to_every_target(server, tmp_path):
    out = tmp_path / "log.jsonl"
    targets = {"staging": server + "/", "prod": server + "/fail"}
    sent = loadgen.run(targets, rate=20, duration=1.0, out=out, timeout=2)
    records = list(loadgen.read_records(out.read_text(encoding="utf-8").splitlines()))
    assert sent == len(records)
    assert 30 <= len(records) <= 44  # 20/s x 2 targets x 1 s, with scheduling slack
    assert {r["namespace"] for r in records} == {"staging", "prod"}
    assert all(r["status"] == 500 for r in records if r["namespace"] == "prod")


def rec(ts, namespace, status, pod):
    return {"ts": ts, "namespace": namespace, "status": status, "latency_ms": 1.0, "pod": pod}


def test_summary_counts_per_hash_inside_the_window():
    records = [
        rec("2026-10-22T10:00:00.000Z", "staging", 200, "podinfo-stable1-a"),
        rec("2026-10-22T10:00:01.000Z", "staging", 500, "podinfo-canary1-b"),
        rec("2026-10-22T10:00:02.000Z", "staging", 409, "podinfo-canary1-b"),
        rec("2026-10-22T10:00:03.000Z", "staging", None, None),
        rec("2026-10-22T10:00:04.000Z", "prod", 200, "podinfo-stable9-c"),
        rec("2026-10-22T10:05:00.000Z", "staging", 200, "podinfo-stable1-a"),
    ]
    summary = loadgen.summarize(
        records,
        namespace="staging",
        since=datetime(2026, 10, 22, 10, 0, 0, tzinfo=UTC),
        until=datetime(2026, 10, 22, 10, 1, 0, tzinfo=UTC),
    )
    assert summary == {
        "requests": 4,
        "transport_errors": 1,
        "by_hash": {
            "stable1": {"requests": 1, "status_5xx": 0, "other": 0},
            "canary1": {"requests": 2, "status_5xx": 1, "other": 1},
        },
    }


def test_summary_cli_prints_json(tmp_path, capsys):
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps(rec("2026-10-22T10:00:00Z", "staging", 200, "p-h-x")) + "\n")
    assert loadgen.main(["summary", str(log), "--namespace", "staging"]) == 0
    assert json.loads(capsys.readouterr().out)["by_hash"] == {
        "h": {"requests": 1, "status_5xx": 0, "other": 0}
    }


def test_targets_must_be_namespace_equals_url():
    assert loadgen.parse_targets(["staging=http://x/"]) == {"staging": "http://x/"}
    assert loadgen.parse_targets(None) == loadgen.DEFAULT_TARGETS
    with pytest.raises(Exception, match="namespace=url"):
        loadgen.parse_targets(["http://x/"])
