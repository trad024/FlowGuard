"""Laptop deployer: the GitHub API is a local fake server, kubectl and the load generator are
fakes, so the tests run without a network, a cluster or a token."""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import deployer

IMAGE_REPO = "ghcr.io/trad024/flowguard-podinfo"
SHA_OLD = "a" * 40
SHA_NEW = "b" * 40


def gh_run(run_number, sha, conclusion="success", event="push", path=deployer.WORKFLOW_PATH):
    return {
        "id": 1000 + run_number,
        "run_number": run_number,
        "head_sha": sha,
        "status": "completed",
        "conclusion": conclusion,
        "event": event,
        "path": path,
        "html_url": f"https://github.com/x/runs/{run_number}",
    }


class FakeGitHub(BaseHTTPRequestHandler):
    """Serves /actions/... like GitHub: ETag + 304, rate limiting, auth header recorded."""

    runs: list = []
    requests: list = []
    rate_limited = False

    def do_GET(self):  # noqa: N802 (http.server API)
        FakeGitHub.requests.append({"path": self.path, "headers": dict(self.headers)})
        if FakeGitHub.rate_limited:
            self.send_response(403)
            self.send_header("X-RateLimit-Remaining", "0")
            self.send_header("X-RateLimit-Reset", "1999999999")
            self.end_headers()
            return
        if "head_sha=" in self.path:
            sha = self.path.split("head_sha=")[1].split("&")[0]
            runs = [r for r in FakeGitHub.runs if r["head_sha"] == sha]
        else:
            runs = list(FakeGitHub.runs)
        body = json.dumps({"total_count": len(runs), "workflow_runs": runs}).encode()
        etag = f'"{hash(body)}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def api():
    FakeGitHub.runs, FakeGitHub.requests, FakeGitHub.rate_limited = [], [], False
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def config(api_url, token=""):
    return deployer.Config(api_url=api_url, token=token, image=IMAGE_REPO, rollout_timeout=60)


def rollout(image, phase="Healthy", stable="h1", current="h1", abort=False, generation=1):
    return {
        "metadata": {"generation": generation},
        "spec": {"template": {"spec": {"containers": [{"name": "podinfo", "image": image}]}}},
        "status": {
            "phase": phase,
            "stableRS": stable,
            "currentPodHash": current,
            "abort": abort,
            "observedGeneration": str(generation),
        },
    }


class FakeCluster:
    """Starts on `image`; after deploy() it plays back the given Rollout states."""

    def __init__(self, image, after_deploy=(), fail=False):
        self.state = rollout(image)
        self.after_deploy = list(after_deploy)
        self.fail = fail
        self.deployed = []

    def rollout(self):
        if self.deployed and self.after_deploy:
            self.state = self.after_deploy.pop(0)
        return self.state

    def deploy(self, image, message):
        if self.fail:
            raise subprocess.CalledProcessError(1, "deploy-version", stderr="patch refused")
        self.deployed.append((image, message))


class FakeLoad:
    def __init__(self):
        self.calls = []

    def start(self, label):
        self.calls.append(("start", label))
        return Path(f"deploy-{label}.jsonl")

    def stop(self):
        self.calls.append(("stop",))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make(api_url, cluster, tmp_path, token=""):
    clock = Clock()
    events = deployer.EventLog(tmp_path / "events.jsonl")
    d = deployer.Deployer(
        config(api_url, token),
        deployer.GitHub(config(api_url, token)),
        cluster,
        FakeLoad(),
        events,
        sleep=clock.sleep,
        clock=clock,
    )
    return d


def events(tmp_path):
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_latest_success_ignores_failures_other_events_and_older_runs(api):
    FakeGitHub.runs = [
        gh_run(3, SHA_OLD),
        gh_run(5, "c" * 40, conclusion="failure"),
        gh_run(6, "d" * 40, event="pull_request"),
        gh_run(4, SHA_NEW),
    ]
    assert deployer.GitHub(config(api)).latest_success()["head_sha"] == SHA_NEW


def test_requests_name_the_workflow_branch_and_send_the_token_only_if_set(api):
    deployer.GitHub(config(api)).latest_success()
    deployer.GitHub(config(api, token="t0k")).latest_success()
    first, second = FakeGitHub.requests
    assert "/actions/workflows/app-ci.yml/runs?branch=dev&status=success" in first["path"]
    assert "Authorization" not in first["headers"]
    assert second["headers"]["Authorization"] == "Bearer t0k"


def test_unchanged_answers_are_revalidated_with_etag(api):
    FakeGitHub.runs = [gh_run(1, SHA_NEW)]
    gh = deployer.GitHub(config(api))
    assert gh.latest_success()["head_sha"] == SHA_NEW
    assert gh.latest_success()["head_sha"] == SHA_NEW  # served from cache after a 304
    assert "If-None-Match" in FakeGitHub.requests[1]["headers"]


def test_rate_limit_is_reported_with_its_reset_time(api):
    FakeGitHub.rate_limited = True
    with pytest.raises(deployer.RateLimited) as exc:
        deployer.GitHub(config(api)).latest_success()
    assert exc.value.reset_at == 1999999999


def test_run_for_commit_only_counts_the_app_workflow(api):
    FakeGitHub.runs = [gh_run(1, SHA_NEW, path=".github/workflows/checks.yml")]
    gh = deployer.GitHub(config(api))
    assert gh.run_for_commit(SHA_NEW) is None
    FakeGitHub.runs.append(gh_run(2, SHA_NEW))
    gh = deployer.GitHub(config(api))
    assert gh.run_for_commit(SHA_NEW)["run_number"] == 2


def test_nothing_to_do_when_staging_already_runs_the_newest_image(api, tmp_path):
    FakeGitHub.runs = [gh_run(1, SHA_NEW)]
    cluster = FakeCluster(f"{IMAGE_REPO}:{SHA_NEW}")
    assert make(api, cluster, tmp_path).poll_once() == "idle"
    assert cluster.deployed == []


def test_nothing_to_do_without_a_successful_run(api, tmp_path):
    FakeGitHub.runs = [gh_run(1, SHA_NEW, conclusion="failure")]
    cluster = FakeCluster("ghcr.io/stefanprodan/podinfo:6.7.1")
    assert make(api, cluster, tmp_path).poll_once() == "idle"


def test_new_successful_run_is_deployed_under_load_until_promoted(api, tmp_path):
    FakeGitHub.runs = [gh_run(7, SHA_NEW)]
    new = f"{IMAGE_REPO}:{SHA_NEW}"
    cluster = FakeCluster(
        f"{IMAGE_REPO}:{SHA_OLD}",
        after_deploy=[
            rollout(new, phase="Healthy", stable="h1", current="h1", generation=1),  # not observed
            rollout(new, phase="Progressing", stable="h1", current="h2", generation=2),
            rollout(new, phase="Healthy", stable="h2", current="h2", generation=2),
        ],
    )
    d = make(api, cluster, tmp_path)
    assert d.poll_once() == "promoted"
    assert cluster.deployed == [(new, f"FlowGuard {SHA_NEW[:7]}")]
    assert d.loadgen.calls == [("start", SHA_NEW[:12]), ("stop",)]
    names = [e["event"] for e in events(tmp_path)]
    assert names == ["run_detected", "deploy_started", "deploy_finished"]
    finished = events(tmp_path)[-1]
    assert (finished["outcome"], finished["sha"], finished["run_id"]) == ("promoted", SHA_NEW, 1007)


def test_an_aborted_canary_is_reported_and_not_redeployed(api, tmp_path):
    FakeGitHub.runs = [gh_run(2, SHA_NEW)]
    new = f"{IMAGE_REPO}:{SHA_NEW}"
    cluster = FakeCluster(
        f"{IMAGE_REPO}:{SHA_OLD}",
        after_deploy=[rollout(new, phase="Degraded", stable="h1", current="h2", abort=True)],
    )
    d = make(api, cluster, tmp_path)
    assert d.poll_once() == "aborted"
    # The Rollout spec now names the aborted commit: the next poll leaves it alone
    assert d.poll_once() == "idle"
    assert len(cluster.deployed) == 1


def test_a_canary_that_never_finishes_times_out_and_stops_the_load(api, tmp_path):
    FakeGitHub.runs = [gh_run(1, SHA_NEW)]
    new = f"{IMAGE_REPO}:{SHA_NEW}"
    stuck = rollout(new, phase="Progressing", stable="h1", current="h2", generation=2)
    cluster = FakeCluster(f"{IMAGE_REPO}:{SHA_OLD}", after_deploy=[stuck] * 100)
    d = make(api, cluster, tmp_path)
    assert d.poll_once() == "timeout"
    assert d.loadgen.calls[-1] == ("stop",)


def test_a_failed_deploy_script_is_logged_and_stops_the_load(api, tmp_path):
    FakeGitHub.runs = [gh_run(1, SHA_NEW)]
    cluster = FakeCluster(f"{IMAGE_REPO}:{SHA_OLD}", fail=True)
    d = make(api, cluster, tmp_path)
    assert d.poll_once() == "error"
    assert d.loadgen.calls[-1] == ("stop",)
    assert "patch refused" in events(tmp_path)[1]["detail"]


def test_deployed_sha_only_trusts_images_from_the_ci_registry():
    assert deployer.deployed_sha(rollout(f"{IMAGE_REPO}:{SHA_NEW}"), IMAGE_REPO) == SHA_NEW
    assert deployer.deployed_sha(rollout("ghcr.io/stefanprodan/podinfo:6.7.1"), IMAGE_REPO) is None


def test_config_reads_env_file_and_lets_the_environment_win(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# local settings\nGITHUB_TOKEN_READ='abc'\nFLOWGUARD_BRANCH=feature\n\nLLM_API_KEY=\n"
    )
    monkeypatch.setenv("FLOWGUARD_BRANCH", "dev")
    for key in ("GITHUB_TOKEN_READ", "FLOWGUARD_POLL_SECONDS", "FLOWGUARD_REPO"):
        monkeypatch.delenv(key, raising=False)
    cfg = deployer.load_config(env_file)
    assert (cfg.token, cfg.branch, cfg.repo, cfg.poll_seconds) == (
        "abc",
        "dev",
        "trad024/FlowGuard",
        30,
    )


def test_without_a_token_polling_slows_to_stay_under_60_requests_an_hour(tmp_path, monkeypatch):
    for key in ("GITHUB_TOKEN_READ", "FLOWGUARD_POLL_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    assert deployer.load_config(tmp_path / "absent.env").poll_seconds == 60


def test_env_file_is_gitignored():
    ignored = (deployer.ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in ignored


def test_scenario_branches_are_url_encoded_in_the_runs_query(api):
    cfg = config(api)
    cfg.branch = "scenario/step3-check-1"
    deployer.GitHub(cfg).latest_success()
    assert "?branch=scenario%2Fstep3-check-1&status=success" in FakeGitHub.requests[0]["path"]


def test_branch_option_overrides_the_configured_branch(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("FLOWGUARD_BRANCH", "dev")
    seen = []

    class RecordingGitHub:
        def __init__(self, cfg):
            seen.append(cfg.branch)

        def run_for_commit(self, sha):
            return None

    monkeypatch.setattr(deployer, "GitHub", RecordingGitHub)
    env_file = str(tmp_path / "absent.env")
    deployer.main(["--env-file", env_file, "status", "--sha", SHA_NEW])
    deployer.main(["--env-file", env_file, "--branch", "scenario/x", "status", "--sha", SHA_NEW])
    assert seen == ["dev", "scenario/x"]
    assert capsys.readouterr().out.split() == ["null", "null"]
