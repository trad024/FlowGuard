"""Recorder: raw Prometheus samples, window filtering of the JSONL sources, CI metadata with
logs marked unavailable when the token may not read them, and the manifest."""

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

import groundtruth
import recorder

START = datetime(2026, 10, 26, 18, 0, 0, tzinfo=UTC)
END = datetime(2026, 10, 26, 18, 10, 0, tzinfo=UTC)


class Fake(BaseHTTPRequestHandler):
    """Prometheus /api/v1/query and the GitHub Actions endpoints the recorder reads."""

    queries: list = []

    def reply(self, code, body):
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 (http.server API)
        url = urlparse(self.path)
        if url.path == "/api/v1/query":
            q = parse_qs(url.query)
            Fake.queries.append((q["query"][0], q["time"][0]))
            series = {"metric": {"pod": "podinfo-h1-a"}, "values": [[1, "3"], [16, "5"]]}
            return self.reply(200, {"status": "success", "data": {"result": [series]}})
        if url.path.endswith("/logs"):
            if not self.headers.get("Authorization"):
                return self.reply(403, {"message": "Must have admin rights to Repository."})
            # Like GitHub: a redirect to log storage on another host, which rejects the token
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_address[1]}/blob")
            self.end_headers()
            return None
        if url.path == "/blob":
            if self.headers.get("Authorization"):
                return self.reply(401, {"message": "unexpected Authorization header"})
            return self.reply(200, b"2026-10-27T10:00:00Z ##[group]Run go test ./...\n")
        if url.path.endswith("/annotations"):
            return self.reply(200, [{"title": "scan-counts", "message": "trivy_high=0"}])
        if url.path.endswith("/jobs"):
            return self.reply(200, {"jobs": [{"id": 5, "name": "unit", "conclusion": "failure"}]})
        if "/actions/runs/" in url.path:
            return self.reply(
                200,
                {"id": 77, "conclusion": "failure", "head_sha": "abc", "head_branch": "scenario/x"},
            )
        return self.reply(404, {})

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Fake.queries = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def write_lines(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_filter_jsonl_keeps_only_the_window(tmp_path):
    f = tmp_path / "load.jsonl"
    write_lines(
        f,
        [
            {"ts": "2026-10-26T17:59:59Z"},
            {"ts": "2026-10-26T18:00:00.500Z"},
            {"ts": "2026-10-26T18:10:00Z"},
            {"ts": "2026-10-26T18:10:01Z"},
            {"x": 1},
        ],
    )
    f.write_text(f.read_text() + "not json\n", encoding="utf-8")
    kept = list(recorder.filter_jsonl([f, tmp_path / "missing.jsonl"], START, END))
    assert [r["ts"] for r in kept] == ["2026-10-26T18:00:00.500Z", "2026-10-26T18:10:00Z"]


def test_prometheus_samples_are_raw_over_the_whole_window(server, tmp_path):
    summary = recorder.record_prometheus(tmp_path, START, END, server)
    assert set(summary) == set(recorder.SELECTORS)
    assert summary["podinfo_requests"] == {"series": 1, "samples": 2}
    query, at = Fake.queries[0]
    assert query == recorder.SELECTORS["podinfo_requests"] + "[600s]"  # a range selector
    assert float(at) == END.timestamp()
    saved = json.loads((tmp_path / "prometheus" / "podinfo_requests.json").read_text())
    assert saved["result"][0]["values"] == [[1, "3"], [16, "5"]]


def test_prometheus_errors_are_recorded_not_raised(tmp_path):
    summary = recorder.record_prometheus(tmp_path, START, END, "http://127.0.0.1:9")
    assert all("error" in v for v in summary.values())


def test_job_logs_follow_the_redirect_without_the_token(server):
    gh = recorder.GitHubReader("trad024/FlowGuard", token="read-token", api=server)
    text, why = gh.job_log(5)
    assert why is None and "Run go test" in text


def test_ci_metadata_is_saved_and_unreadable_logs_are_marked(server, tmp_path):
    gh = recorder.GitHubReader("trad024/FlowGuard", token="", api=server)
    (entry,) = recorder.record_ci(tmp_path, [77], gh)
    assert entry["conclusion"] == "failure" and entry["jobs"] == {"unit": "failure"}
    assert entry["logs"]["unit"].startswith("unavailable (HTTP 403")
    assert (tmp_path / "ci" / "77" / "jobs.json").is_file()
    notes = json.loads((tmp_path / "ci" / "77" / "annotations.json").read_text())
    assert notes["unit"][0]["title"] == "scan-counts"


def test_record_writes_manifest_ground_truth_and_sources(server, tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(recorder, "RUNS_DIR", runs)
    write_lines(
        runs / "deployer-events.jsonl",
        [
            {"ts": "2026-10-26T18:05:00Z", "event": "deploy_finished"},
            {"ts": "2026-10-26T19:00:00Z", "event": "later"},
        ],
    )
    load = tmp_path / "load.jsonl"
    write_lines(load, [{"ts": "2026-10-26T18:01:00.000Z", "status": 500}])
    monkeypatch.setattr(recorder, "audit_events", lambda s, e: [{"ts": "2026-10-26T18:02:00Z"}])
    truth_db = groundtruth.GroundTruthDB(tmp_path / "groundtruth.db")
    out = recorder.record(
        "dev-001",
        START,
        END,
        loadgen_files=[load],
        ci_runs=[77],
        ground_truth={"bad_release": True, "expected_l0": "abort"},
        evaluation={
            "spec": {"id": "x-1", "family": "release"},
            "observed": {"l0": "aborted"},
            "l0_correct": True,
        },
        truth_db=truth_db,
        github=recorder.GitHubReader("trad024/FlowGuard", api=server),
        prometheus=server,
        out_root=tmp_path / "recordings",
    )
    assert out.name == "dev-001-20261026T180000Z"
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["window"] == {"start": "2026-10-26T18:00:00Z", "end": "2026-10-26T18:10:00Z"}
    assert manifest["sources"]["loadgen"]["lines"] == 1
    assert manifest["sources"]["deployer_events"]["lines"] == 1
    assert manifest["sources"]["audit_events"]["lines"] == 1
    assert manifest["sources"]["ci"][0]["run_id"] == 77
    # D25: nothing about the injection in the manifest; all of it in ground_truth.json and the DB
    assert manifest["valid"] is True
    assert not {"spec", "observed", "l0_correct", "ground_truth"} & set(manifest)
    truth = json.loads((out / "ground_truth.json").read_text())
    assert truth["ground_truth"]["expected_l0"] == "abort"
    assert truth["observed"] == {"l0": "aborted"} and truth["spec"]["id"] == "x-1"
    row = truth_db.run(out.name)
    assert row["family"] == "release" and row["l0_correct"] is True and row["valid"] is True
    assert row["ground_truth"]["bad_release"] is True and row["recording"] == str(out)


def test_recordings_and_heldout_scenarios_are_gitignored():
    ignored = (recorder.ROOT / ".gitignore").read_text().splitlines()
    assert "evaluation/recordings/" in ignored
    assert "scenarios/heldout/" in ignored
