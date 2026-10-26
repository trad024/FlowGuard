"""Laptop deployer: the GitHub API is a local fake server, kubectl and the load generator are
fakes, so the tests run without a network, a cluster or a token."""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import deployer
from flowguard import audit_metrics
from flowguard.audit import AuditLog

IMAGE_REPO = "ghcr.io/trad024/flowguard-podinfo"
SHA_OLD = "a" * 40
SHA_NEW = "b" * 40
CLEAN = "trivy_high=0 trivy_critical=0 gitleaks=0"


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
    """Serves the Actions API like GitHub: ETag + 304, rate limiting, jobs and annotations."""

    runs: list = []
    requests: list = []
    rate_limited = False
    scan_messages: dict = {}  # run id -> scan-counts message; None = no annotation

    def answer(self):
        path = self.path
        if "/check-runs/" in path:
            run_id = int(path.split("/check-runs/")[1].split("/")[0]) - 9000
            message = FakeGitHub.scan_messages.get(run_id, CLEAN)
            notes = [{"title": "plan", "message": "mode=optimizer"}]
            if message is not None:
                notes.append({"title": "scan-counts", "message": message})
            return notes
        if "/jobs" in path:
            run_id = int(path.split("/runs/")[1].split("/")[0])
            return {
                "jobs": [
                    {"id": 8000 + run_id, "name": "plan"},
                    {"id": 9000 + run_id, "name": "scans"},
                ]
            }
        if "head_sha=" in path:
            sha = path.split("head_sha=")[1].split("&")[0]
            runs = [r for r in FakeGitHub.runs if r["head_sha"] == sha]
        else:
            runs = list(FakeGitHub.runs)
        return {"total_count": len(runs), "workflow_runs": runs}

    def do_GET(self):  # noqa: N802 (http.server API)
        FakeGitHub.requests.append({"path": self.path, "headers": dict(self.headers)})
        if FakeGitHub.rate_limited:
            self.send_response(403)
            self.send_header("X-RateLimit-Remaining", "0")
            self.send_header("X-RateLimit-Reset", "1999999999")
            self.end_headers()
            return
        body = json.dumps(self.answer()).encode()
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
    FakeGitHub.scan_messages = {}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def config(api_url, token="", prod=False):
    return deployer.Config(
        api_url=api_url, token=token, image=IMAGE_REPO, rollout_timeout=60, release_to_prod=prod
    )


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


def promoted(image):
    return rollout(image, phase="Healthy", stable="h2", current="h2", generation=2)


class FakeCluster:
    """Starts on `image`; after deploy() it plays back the given Rollout states."""

    def __init__(self, image, after_deploy=(), fail=False):
        self.state = rollout(image) if isinstance(image, str) else image
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


def make(api_url, tmp_path, staging, prod=None, token="", release_to_prod=False):
    clock = Clock()
    cfg = config(api_url, token, release_to_prod)
    events = deployer.EventLog(tmp_path / "events.jsonl", AuditLog(tmp_path / "audit.db"))
    return deployer.Deployer(
        cfg,
        deployer.GitHub(cfg),
        {"staging": staging, "prod": prod or FakeCluster(f"{IMAGE_REPO}:{SHA_OLD}")},
        {"staging": FakeLoad(), "prod": FakeLoad()},
        events,
        sleep=clock.sleep,
        clock=clock,
    )


def events(tmp_path):
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


# ---------------------------------------------------------------- GitHub client


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


def test_scan_counts_come_from_the_scans_job_annotation(api):
    FakeGitHub.scan_messages = {7: "trivy_high=3 trivy_critical=1 gitleaks=0", 8: None}
    gh = deployer.GitHub(config(api))
    assert gh.scan_counts(7) == {"trivy_high": 3, "trivy_critical": 1, "gitleaks": 0}
    assert gh.scan_counts(8) is None


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (CLEAN, {"trivy_high": 0, "trivy_critical": 0, "gitleaks": 0}),
        (
            "gitleaks=2 trivy_critical=0 trivy_high=11",
            {"trivy_high": 11, "trivy_critical": 0, "gitleaks": 2},
        ),
        ("trivy_high=0 trivy_critical=0", None),  # incomplete
        ("", None),
    ],
)
def test_parse_scan_counts(message, expected):
    assert deployer.parse_scan_counts(message) == expected


@pytest.mark.parametrize(
    ("counts", "decision"),
    [
        ({"trivy_high": 0, "trivy_critical": 0, "gitleaks": 0}, "allow"),
        ({"trivy_high": 1, "trivy_critical": 0, "gitleaks": 0}, "block"),
        ({"trivy_high": 0, "trivy_critical": 1, "gitleaks": 0}, "block"),
        ({"trivy_high": 0, "trivy_critical": 0, "gitleaks": 1}, "block"),
        (None, "block"),  # no counts: fail closed
    ],
)
def test_l0_security_verdict(counts, decision):
    assert deployer.security_verdict(counts)[0] == decision


# ---------------------------------------------------------------- staging


def test_nothing_to_do_when_staging_already_runs_the_newest_image(api, tmp_path):
    FakeGitHub.runs = [gh_run(1, SHA_NEW)]
    staging = FakeCluster(f"{IMAGE_REPO}:{SHA_NEW}")
    assert make(api, tmp_path, staging).poll_once() == {}
    assert staging.deployed == []


def test_nothing_to_do_without_a_successful_run(api, tmp_path):
    FakeGitHub.runs = [gh_run(1, SHA_NEW, conclusion="failure")]
    staging = FakeCluster("ghcr.io/stefanprodan/podinfo:6.7.1")
    assert make(api, tmp_path, staging).poll_once() == {}


def test_new_successful_run_is_deployed_under_load_until_promoted(api, tmp_path):
    FakeGitHub.runs = [gh_run(7, SHA_NEW)]
    new = f"{IMAGE_REPO}:{SHA_NEW}"
    staging = FakeCluster(
        f"{IMAGE_REPO}:{SHA_OLD}",
        after_deploy=[
            rollout(new, phase="Healthy", stable="h1", current="h1", generation=1),  # not observed
            rollout(new, phase="Progressing", stable="h1", current="h2", generation=2),
            promoted(new),
        ],
    )
    d = make(api, tmp_path, staging)
    assert d.poll_once() == {"staging": "promoted"}
    assert staging.deployed == [(new, f"FlowGuard {SHA_NEW[:7]}")]
    assert d.loadgens["staging"].calls == [("start", SHA_NEW[:12]), ("stop",)]
    names = [e["event"] for e in events(tmp_path)]
    assert names == ["security_decision", "run_detected", "deploy_started", "deploy_finished"]
    finished = events(tmp_path)[-1]
    assert (finished["outcome"], finished["sha"], finished["environment"]) == (
        "promoted",
        SHA_NEW,
        "staging",
    )


def test_every_deployer_event_also_reaches_the_audit_log(api, tmp_path):
    FakeGitHub.runs = [gh_run(7, SHA_NEW)]
    staging = FakeCluster(
        f"{IMAGE_REPO}:{SHA_OLD}", after_deploy=[promoted(f"{IMAGE_REPO}:{SHA_NEW}")]
    )
    make(api, tmp_path, staging).poll_once()
    log = AuditLog(tmp_path / "audit.db")
    kinds = [(e["source"], e["kind"]) for e in log.events()]
    assert kinds == [
        ("security_gate", "security_decision"),
        ("deployer", "run_detected"),
        ("deployer", "deploy_started"),
        ("deployer", "deploy_finished"),
    ]
    assert audit_metrics.deployment_outcomes(log, "staging") == {"promoted": 1}
    assert audit_metrics.security_decisions(log) == {"allow": 1}
    (finished,) = log.events("deploy_finished")
    assert finished["commit_sha"] == SHA_NEW and finished["environment"] == "staging"


def test_an_aborted_canary_is_reported_and_not_redeployed(api, tmp_path):
    FakeGitHub.runs = [gh_run(2, SHA_NEW)]
    new = f"{IMAGE_REPO}:{SHA_NEW}"
    staging = FakeCluster(
        f"{IMAGE_REPO}:{SHA_OLD}",
        after_deploy=[rollout(new, phase="Degraded", stable="h1", current="h2", abort=True)],
    )
    d = make(api, tmp_path, staging, release_to_prod=True)
    assert d.poll_once() == {"staging": "aborted"}  # no prod release after an abort
    # The Rollout spec now names the aborted commit: the next poll leaves it alone
    assert d.poll_once() == {}
    assert len(staging.deployed) == 1


def test_a_canary_that_never_finishes_times_out_and_stops_the_load(api, tmp_path):
    FakeGitHub.runs = [gh_run(1, SHA_NEW)]
    stuck = rollout(
        f"{IMAGE_REPO}:{SHA_NEW}", phase="Progressing", stable="h1", current="h2", generation=2
    )
    d = make(api, tmp_path, FakeCluster(f"{IMAGE_REPO}:{SHA_OLD}", after_deploy=[stuck] * 100))
    assert d.poll_once() == {"staging": "timeout"}
    assert d.loadgens["staging"].calls[-1] == ("stop",)


def test_a_failed_deploy_script_is_logged_and_stops_the_load(api, tmp_path):
    FakeGitHub.runs = [gh_run(1, SHA_NEW)]
    d = make(api, tmp_path, FakeCluster(f"{IMAGE_REPO}:{SHA_OLD}", fail=True))
    assert d.poll_once() == {"staging": "error"}
    assert d.loadgens["staging"].calls[-1] == ("stop",)
    error = next(e for e in events(tmp_path) if e["event"] == "deploy_error")
    assert "patch refused" in error["detail"]


# ---------------------------------------------------------------- L0 security gate


@pytest.mark.parametrize(
    "message",
    ["trivy_high=1 trivy_critical=0 gitleaks=0", "trivy_high=0 trivy_critical=0 gitleaks=2", None],
)
def test_security_gate_blocks_findings_and_missing_counts_without_deploying(api, tmp_path, message):
    FakeGitHub.runs = [gh_run(4, SHA_NEW)]
    FakeGitHub.scan_messages = {1004: message}
    staging = FakeCluster(f"{IMAGE_REPO}:{SHA_OLD}")
    d = make(api, tmp_path, staging, release_to_prod=True)
    assert d.poll_once() == {"staging": "blocked"}
    assert staging.deployed == [] and d.loadgens["staging"].calls == []
    (decision,) = events(tmp_path)
    assert (decision["event"], decision["decision"], decision["sha"]) == (
        "security_decision",
        "block",
        SHA_NEW,
    )
    # Logged once: later polls do not repeat the decision for the same commit
    assert d.poll_once() == {}
    assert len(events(tmp_path)) == 1
    assert audit_metrics.security_decisions(AuditLog(tmp_path / "audit.db")) == {"block": 1}


# ---------------------------------------------------------------- prod (D23, L0)


def test_a_promoted_staging_canary_is_released_to_prod(api, tmp_path):
    FakeGitHub.runs = [gh_run(9, SHA_NEW)]
    new = f"{IMAGE_REPO}:{SHA_NEW}"
    staging = FakeCluster(f"{IMAGE_REPO}:{SHA_OLD}", after_deploy=[promoted(new)])
    prod = FakeCluster(f"{IMAGE_REPO}:{SHA_OLD}", after_deploy=[promoted(new)])
    d = make(api, tmp_path, staging, prod, release_to_prod=True)
    assert d.poll_once() == {"staging": "promoted", "prod": "promoted"}
    assert prod.deployed == [(new, f"FlowGuard {SHA_NEW[:7]}")]
    assert d.loadgens["prod"].calls == [("start", SHA_NEW[:12]), ("stop",)]
    finished = [e for e in events(tmp_path) if e["event"] == "deploy_finished"]
    assert [(e["environment"], e["outcome"]) for e in finished] == [
        ("staging", "promoted"),
        ("prod", "promoted"),
    ]
    assert d.poll_once() == {}  # both environments now run the commit


def test_a_pending_prod_release_is_picked_up_after_a_restart(api, tmp_path):
    FakeGitHub.runs = [gh_run(9, SHA_NEW)]
    new = f"{IMAGE_REPO}:{SHA_NEW}"
    staging = FakeCluster(promoted(new))  # promoted before the restart
    prod = FakeCluster(f"{IMAGE_REPO}:{SHA_OLD}", after_deploy=[promoted(new)])
    d = make(api, tmp_path, staging, prod, release_to_prod=True)
    assert d.poll_once() == {"prod": "promoted"}
    assert staging.deployed == []


def test_no_prod_release_when_disabled(api, tmp_path):
    FakeGitHub.runs = [gh_run(9, SHA_NEW)]
    new = f"{IMAGE_REPO}:{SHA_NEW}"
    prod = FakeCluster(f"{IMAGE_REPO}:{SHA_OLD}")
    d = make(
        api, tmp_path, FakeCluster(f"{IMAGE_REPO}:{SHA_OLD}", after_deploy=[promoted(new)]), prod
    )
    assert d.poll_once() == {"staging": "promoted"}
    assert d.poll_once() == {}
    assert prod.deployed == []


# ---------------------------------------------------------------- config and CLI


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


def test_gate_command_prints_counts_and_verdict(api, monkeypatch, capsys):
    FakeGitHub.runs = [gh_run(3, SHA_NEW)]
    FakeGitHub.scan_messages = {1003: "trivy_high=0 trivy_critical=2 gitleaks=0"}
    monkeypatch.setattr(deployer, "load_config", lambda env_file: config(api))
    assert deployer.main(["gate", "--sha", SHA_NEW]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["trivy_critical"] == 2 and out["decision"] == "block"
