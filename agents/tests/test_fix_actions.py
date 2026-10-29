"""D57 fix actions (D62 module): the go.mod pin and its go.sum check, the fix request's content,
planning a fix after the diagnosis (Y from the last commit whose CI passed), the GitHub handlers
with their branch rule, and both actions through the gate and a dry-run executor. A fake GitHub
client only: no network, no git remote, no go toolchain."""

import base64
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from flowguard import fixes
from flowguard import github_actions as gha
from flowguard.audit import AuditLog
from flowguard.contracts import ActionProposal
from flowguard.executor import Executor
from flowguard.gate import RiskGate

REPO = "acme/app"
HEAD = "a" * 40  # the failing commit, on scenario/r-1
GREEN = "b" * 40  # the last commit whose app-ci passed, on dev
GO_MOD = """module example.com/app

go 1.26.0

require (
\tgithub.com/gorilla/websocket v1.5.3
\tgithub.com/spf13/viper v1.19.0 // indirect
\tgo.uber.org/zap v1.27.0
)

require github.com/fatih/color v1.17.0

replace golang.org/x/text => golang.org/x/text v0.42.0
"""
BROKEN = GO_MOD.replace("websocket v1.5.3", "websocket v1.9.9")
GO_SUM = (
    "github.com/gorilla/websocket v1.5.3 h1:SYNTHETIC1=\n"
    "github.com/gorilla/websocket v1.5.3/go.mod h1:SYNTHETIC2=\n"
    "go.uber.org/zap v1.27.0 h1:SYNTHETIC3=\n"
)
UNKNOWN = "go: github.com/gorilla/websocket@v1.9.9: invalid version: unknown revision v1.9.9"


# ---------------------------------------------------------------- go.mod and go.sum


def test_the_evidence_names_modules_at_the_versions_go_cannot_use():
    lines = [
        UNKNOWN,
        'go.mod:15:2: require github.com/spf13/viper: version "v2.0.0" invalid: should be v0 '
        "or v1, not v2",
        "verifying github.com/gorilla/mux@v1.8.1: checksum mismatch",
        UNKNOWN,  # repeated: listed once
        "reading https://proxy.golang.org/github.com/x/y/@v/v1.0.0.mod: 404 Not Found",
        "--- FAIL: TestHandler (0.00s)",
    ]
    assert fixes.named(lines) == [
        ("github.com/gorilla/websocket", "v1.9.9"),
        ("github.com/spf13/viper", "v2.0.0"),
        ("github.com/gorilla/mux", "v1.8.1"),
    ]


def test_set_version_edits_one_require_line_and_refuses_what_a_pin_cannot_fix():
    edited = fixes.set_version(BROKEN, "github.com/gorilla/websocket", "v1.5.3")
    assert edited == GO_MOD  # only that line changed
    viper = fixes.set_version(GO_MOD, "github.com/spf13/viper", "v1.18.2")
    assert "\tgithub.com/spf13/viper v1.18.2 // indirect\n" in viper
    color = fixes.set_version(GO_MOD, "github.com/fatih/color", "v1.16.0")
    assert "require github.com/fatih/color v1.16.0\n" in color
    refused = {
        ("golang.org/x/text", "v0.41.0"): "replaced",
        ("github.com/not/required", "v1.0.0"): "0 times",
        ("go.uber.org/zap", "v1.27.0"): "already at",
        ("go.uber.org/zap", "latest"): "not a module path and version",
        ("../../etc", "v1.0.0"): "not a module path and version",
    }
    for (module, version), why in refused.items():
        with pytest.raises(ValueError, match=why):
            fixes.set_version(GO_MOD, module, version)
    assert fixes.required(GO_MOD, "github.com/spf13/viper") == "v1.19.0"
    assert fixes.required(GO_MOD, "golang.org/x/text") is None  # replaced
    assert fixes.required(GO_MOD, "github.com/not/required") is None


def test_go_sum_must_hold_both_hashes_of_the_pinned_version():
    assert fixes.sum_has(GO_SUM, "github.com/gorilla/websocket", "v1.5.3")
    assert not fixes.sum_has(GO_SUM, "go.uber.org/zap", "v1.27.0")  # no /go.mod hash
    assert not fixes.sum_has(GO_SUM, "github.com/gorilla/websocket", "v1.5.2")


# ---------------------------------------------------------------- the D57 rule


def test_a_pin_only_where_the_evidence_supports_one_otherwise_an_issue():
    used = {"github.com/gorilla/websocket": "v1.5.3"}
    action, params = fixes.fix_action(
        "pin_dependency", "dependency", [UNKNOWN], "pin", used, GO_SUM
    )
    assert (action, params) == (
        "pin_dependency",
        {"file": fixes.GO_MOD, "module": "github.com/gorilla/websocket", "version": "v1.5.3"},
    )
    # go.sum without the hashes: the edit goes to a person as a draft PR
    action, params = fixes.fix_action("pin_dependency", "dependency", [UNKNOWN], "pin", used, "")
    assert action == "open_fix_pr" and params["kind"] == "draft_pr"
    assert params["patch"]["version"] == "v1.5.3" and "go mod tidy" in params["body"]
    cases = [
        ("pin_dependency", "dependency", [UNKNOWN], {}),  # X not found at the last green commit
        ("pin_dependency", "dependency", ["build failed"], used),  # evidence names no module
        ("pin_dependency", "code", [UNKNOWN], used),  # not a dependency diagnosis
        ("open_fix_pr", "environment", ["GOFLAGS=-mod=vendor"], used),
    ]
    for chosen, cause, evidence, known in cases:
        action, params = fixes.fix_action(chosen, cause, evidence, "fix it", known, GO_SUM)
        assert action == "open_fix_pr" and params["kind"] == "issue" and "patch" not in params
    for other in ("retry_job", "rerun_workflow", "escalate_to_human"):
        assert fixes.fix_action(other, "flaky_test", [], "", used, GO_SUM) == (other, {})


def test_the_fix_request_carries_the_diagnosis_and_renders_nothing_an_llm_wrote():
    params = fixes.request(
        "code",
        ["handler_test.go:42: expected 200, got ```500```", "  ", "L2"],
        "Return 200 from @maintainer's handler\n\nand [click](http://x)",
    )
    assert params["kind"] == "issue" and params["title"] == "CI failure, root cause code"
    body = params["body"]
    assert body.startswith("**Root cause:** code")
    assert "````text\nhandler_test.go:42: expected 200, got ```500```\nL2\n````" in body
    assert "```text\nReturn 200 from @maintainer's handler and [click](http://x)\n```" in body
    assert body.endswith("nothing is merged without a person.")
    many = fixes.request("dependency", [f"line {i}" for i in range(9)], "x" * 5000)
    assert many["body"].count("\nline ") == fixes.MAX_QUOTES and "x" * 1001 not in many["body"]


# ---------------------------------------------------------------- a fake GitHub


class FakeGitHub:
    """Answers the API paths the module uses; records every call."""

    def __init__(self, files=None, green=None, fail=()):
        self.files = files if files is not None else {}  # (path, ref) -> text
        self.green = green if green is not None else {}  # branch -> head SHA
        self.fail = set(fail)  # (method, path prefix) answered 422
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        url = urllib.parse.urlsplit(path)
        query = dict(urllib.parse.parse_qsl(url.query))
        route = urllib.parse.unquote(url.path).removeprefix(f"/repos/{REPO}/")
        if any(method == m and route.startswith(p) for m, p in self.fail):
            return 422, {"message": "Unprocessable"}
        if method == "GET" and route.startswith("contents/"):
            text = self.files.get((route.removeprefix("contents/"), query["ref"]))
            if text is None:
                return 404, {"message": "Not Found"}
            content = base64.encodebytes(text.encode()).decode()  # with newlines, as GitHub
            return 200, {"type": "file", "content": content, "sha": f"blob-{query['ref'][:4]}"}
        if method == "GET" and route == f"actions/workflows/{gha.WORKFLOW}/runs":
            assert query["status"] == "success" and query["per_page"] == "1"
            sha = self.green.get(query["branch"])
            return 200, {"workflow_runs": [{"head_sha": sha}] if sha else []}
        if method == "POST" and route == "git/refs":
            return 201, {"ref": body["ref"]}
        if method == "PUT" and route.startswith("contents/"):
            return 201, {"commit": {"sha": "c" * 40}}
        if method == "POST" and route == f"actions/workflows/{gha.WORKFLOW}/dispatches":
            return 204, None
        if method == "POST" and route == "issues":
            return 201, {"number": 7}
        if method == "POST" and route == "pulls":
            return 201, {"number": 8}
        if method == "POST" and route == "issues/8/labels":
            return 200, [{"name": body["labels"][0]}]
        return 404, {"message": f"no route {method} {route}"}

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


def github(files=(), **kw):
    """The failing commit on scenario/r-1 (no green run there), the last green one on dev;
    `files` overrides some of their files."""
    known = {(fixes.GO_MOD, GREEN): GO_MOD, (fixes.GO_MOD, HEAD): BROKEN}
    known[(fixes.GO_SUM, HEAD)] = GO_SUM
    return FakeGitHub(files={**known, **dict(files)}, green={"dev": GREEN}, **kw)


# ---------------------------------------------------------------- planning (read-only)


def test_y_is_the_modules_version_at_the_last_commit_whose_ci_passed():
    client = github()
    action, params = gha.plan_fix(
        client, REPO, "pin_dependency", "dependency", [UNKNOWN], "pin", HEAD, "scenario/r-1"
    )
    assert action == "pin_dependency"
    assert params == {
        "file": fixes.GO_MOD, "module": "github.com/gorilla/websocket", "version": "v1.5.3",
        "head_sha": HEAD, "base_branch": "scenario/r-1",
    }  # fmt: skip
    assert client.writes() == []  # planning only reads
    runs = [urllib.parse.unquote(p) for m, p, _ in client.calls if "/runs?" in p]
    assert ["branch=scenario/r-1" in runs[0], "branch=dev" in runs[1]] == [True, True]
    reads = [urllib.parse.unquote(p).split("contents/")[1] for _, p, _ in client.calls[2:]]
    assert reads == [f"{fixes.GO_MOD}?ref={GREEN}", f"{fixes.GO_SUM}?ref={HEAD}"]


def test_without_a_known_good_version_the_plan_falls_back_to_an_issue():
    plans = {
        "no green run": FakeGitHub(),
        "module not in go.mod there": github(files={(fixes.GO_MOD, GREEN): "module x\n"}),
        "GitHub refuses the read": github(fail={("GET", "contents/")}),
    }
    for why, client in plans.items():
        action, params = gha.plan_fix(
            client, REPO, "pin_dependency", "dependency", [UNKNOWN], "pin", HEAD, "scenario/r-1"
        )
        assert (action, params["kind"]) == ("open_fix_pr", "issue"), why
        assert client.writes() == [], why
    # a pin that would need a draft PR into dev goes out as an issue instead
    client = github()
    client.files[(fixes.GO_SUM, HEAD)] = ""
    action, params = gha.plan_fix(
        client, REPO, "pin_dependency", "dependency", [UNKNOWN], "pin", HEAD, "dev"
    )
    assert (action, params["kind"]) == ("open_fix_pr", "issue") and "patch" not in params
    # no GitHub read at all for another class or another action
    client = FakeGitHub()
    assert gha.plan_fix(client, REPO, "open_fix_pr", "code", [UNKNOWN], "f", HEAD, "scenario/r-1")[
        1
    ]["kind"] == "issue"  # fmt: skip
    assert gha.plan_fix(client, REPO, "retry_job", "flaky_test", [], "", HEAD, "x") == (
        "retry_job", {}
    )  # fmt: skip
    assert client.calls == []


# ---------------------------------------------------------------- handlers (the executor's)


def test_the_branch_rule_allows_fix_and_scenario_branches_only():
    for ok in ("fix/p-1a2b", "scenario/r-1", "fix/a/b", "scenario/r-1.v2"):
        assert gha.check_branch(ok) == ok
    for bad in ("dev", "main", "refs/heads/main", "fix/", "fix/../dev", "fixes/x", "scenario",
                "fix/-x", "fix/a.lock", "fix/a b", "fix/a~1", None, "Fix/x"):  # fmt: skip
        with pytest.raises(gha.GitHubError):
            gha.check_branch(bad)


def pin_params(**kw):
    return {"file": fixes.GO_MOD, "module": "github.com/gorilla/websocket", "version": "v1.5.3",
            "head_sha": HEAD, "base_branch": "scenario/r-1", **kw}  # fmt: skip


def test_pin_dependency_commits_the_edit_on_a_fix_branch_and_starts_app_ci_there():
    client = github()
    out = gha.HANDLERS["pin_dependency"](client, REPO, "p-1a2b", pin_params())
    branch = "fix/flowguard-eval-p-1a2b"
    assert gha.fix_branch("p-1a2b") == branch
    assert out == f"set github.com/gorilla/websocket to v1.5.3 on {branch} ({'c' * 12}); " + (
        "app-ci started"
    )
    (ref, put, dispatch) = client.writes()
    assert ref == ("POST", f"/repos/{REPO}/git/refs", {"ref": f"refs/heads/{branch}", "sha": HEAD})
    method, path, body = put
    assert (method, urllib.parse.unquote(path)) == ("PUT", f"/repos/{REPO}/contents/{fixes.GO_MOD}")
    assert body["branch"] == branch and body["sha"] == "blob-aaaa"
    assert base64.b64decode(body["content"]).decode() == GO_MOD  # websocket back to v1.5.3
    assert dispatch == (
        "POST", f"/repos/{REPO}/actions/workflows/app-ci.yml/dispatches", {"ref": branch}
    )  # fmt: skip


def test_open_fix_pr_opens_an_issue_or_a_draft_pr_into_the_failing_branch():
    issue = fixes.request("code", ["boom"], "fix the handler")
    client = github()
    assert gha.HANDLERS["open_fix_pr"](client, REPO, "p-1", issue) == "opened issue #7"
    ((method, path, body),) = client.writes()
    assert (method, path) == ("POST", f"/repos/{REPO}/issues")
    assert body == {"title": issue["title"], "body": issue["body"], "labels": ["flowguard-eval"]}

    patch = {"file": fixes.GO_MOD, "module": "github.com/gorilla/websocket", "version": "v1.5.3"}
    draft = {**fixes.request("dependency", [UNKNOWN], "pin", patch), "head_sha": HEAD,
             "base_branch": "scenario/r-1"}  # fmt: skip
    client = github()
    out = gha.HANDLERS["open_fix_pr"](client, REPO, "p-2", draft)
    assert out == "opened draft PR #8 from fix/flowguard-eval-p-2 into scenario/r-1"
    ref, _put, pull, label = client.writes()
    assert ref[2]["ref"] == "refs/heads/fix/flowguard-eval-p-2"
    assert pull[1] == f"/repos/{REPO}/pulls"
    assert {k: pull[2][k] for k in ("head", "base", "draft")} == {
        "head": "fix/flowguard-eval-p-2", "base": "scenario/r-1", "draft": True
    }  # fmt: skip
    assert label == ("POST", f"/repos/{REPO}/issues/8/labels", {"labels": ["flowguard-eval"]})


def test_handlers_refuse_before_writing_anything_they_may_not_write():
    patch = {"file": fixes.GO_MOD, "module": "github.com/gorilla/websocket", "version": "v1.5.3"}
    draft = {**fixes.request("dependency", [UNKNOWN], "pin", patch), "head_sha": HEAD}
    refused = [
        ("open_fix_pr", "p-1", {**draft, "base_branch": "dev"}, "fix/\\* and scenario"),
        ("open_fix_pr", "p-1", {**draft, "base_branch": "main"}, "fix/\\* and scenario"),
        ("open_fix_pr", "p-1", {**draft, "base_branch": "fix/../main"}, "fix/\\* and scenario"),
        ("open_fix_pr", "p-1", {"kind": "pr", "title": "t", "body": "b"}, "unknown"),
        ("open_fix_pr", "p-1", {"kind": "issue", "title": "", "body": "b"}, "title"),
        ("pin_dependency", "p-1", pin_params(file="app/podinfo/main.go"), "edits"),
        ("pin_dependency", "p-1", pin_params(file="../.github/workflows/app-ci.yml"), "edits"),
        ("pin_dependency", "p-1", pin_params(head_sha="HEAD"), "not a commit SHA"),
        ("pin_dependency", "../main", pin_params(), "fix/\\* and scenario"),
        ("pin_dependency", "p-1", pin_params(version="v1.9.9"), "already at"),
    ]
    for action, pid, params, why in refused:
        client = github()
        with pytest.raises(gha.GitHubError, match=why):
            gha.HANDLERS[action](client, REPO, pid, params)
        assert client.writes() == [], (action, params)


def test_a_github_refusal_fails_the_handler():
    client = github(fail={("POST", "git/refs")})
    with pytest.raises(gha.GitHubError, match="422 to create fix/flowguard-eval-p-1: Unproc"):
        gha.pin_dependency(client, REPO, "p-1", pin_params())
    client = github(fail={("POST", "actions/workflows")})
    with pytest.raises(gha.GitHubError, match="start app-ci.yml"):
        gha.pin_dependency(client, REPO, "p-1", pin_params())
    # the PR is open but unlabelled: a failed execution, and the cleanup still finds the PR
    # by its marked head branch
    patch = {"file": fixes.GO_MOD, "module": "github.com/gorilla/websocket", "version": "v1.5.3"}
    draft = {**fixes.request("dependency", [UNKNOWN], "pin", patch), "head_sha": HEAD,
             "base_branch": "scenario/r-1"}  # fmt: skip
    client = github(fail={("POST", "issues/8/labels")})
    with pytest.raises(gha.GitHubError, match="label PR #8"):
        gha.open_fix_pr(client, REPO, "p-2", draft)


# ---------------------------------------------------------------- through the gate and executor


@pytest.mark.parametrize(
    ("action", "params"),
    [("pin_dependency", pin_params()), ("open_fix_pr", fixes.request("code", ["boom"], "fix"))],
)
def test_both_actions_go_through_the_gate_and_a_dry_run_executor(tmp_path, action, params):
    audit = AuditLog(tmp_path / "a.db")
    gate = RiskGate(audit=audit)
    outcomes = {}
    for env in ("staging", "prod"):
        p = ActionProposal(
            id=f"p-{env}",
            agent="diagnose",
            action=action,
            target="podinfo",
            environment=env,
            params=params,
            confidence=0.0,
            rationale="synthetic",
        )
        d = gate.decide(p, confidence=0.9)
        audit.proposal(p)
        audit.gate_decision(d)
        outcomes[env] = d.outcome
        if d.outcome == "execute":
            result = Executor(audit=audit, dry_run=True).execute(p, d)
            assert result.success and result.output == f"dry run: {action}"
    assert outcomes == {"staging": "execute", "prod": "ask_human"}  # medium tier, unchanged
    logged = audit.query("SELECT params FROM proposals WHERE id = 'p-staging'")[0]["params"]
    assert json.loads(logged) == params  # what the executor re-reads and compares (D37)


def test_another_agent_may_not_propose_them():
    p = ActionProposal(id="p-r", agent="release", action="pin_dependency", target="podinfo",
                       environment="staging", params=pin_params(), confidence=0.0,
                       rationale="synthetic")  # fmt: skip
    assert RiskGate().decide(p, confidence=1.0).outcome == "reject"


# ---------------------------------------------------------------- the JSON transport


class FakeAPI(BaseHTTPRequestHandler):
    seen: list = []

    def _answer(self):
        size = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(size)) if size else None
        FakeAPI.seen.append((self.command, self.path, body, self.headers.get("Authorization")))
        status, answer = (404, {"message": "Not Found"}) if "missing" in self.path else (201, {})
        if status == 201:
            answer = {"number": 9, "echo": body}
        data = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = _answer  # noqa: N815 (http.server API)

    def log_message(self, *args):
        pass


def test_the_transport_sends_json_and_returns_the_parsed_answer():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        call = gha.urllib_transport("t0ken", api=f"http://127.0.0.1:{server.server_port}")
        assert call("POST", "/repos/a/b/issues", {"title": "x"}) == (
            201, {"number": 9, "echo": {"title": "x"}}
        )  # fmt: skip
        assert call("GET", "/repos/a/b/missing", None) == (404, {"message": "Not Found"})
        assert FakeAPI.seen[0] == ("POST", "/repos/a/b/issues", {"title": "x"}, "Bearer t0ken")
        assert FakeAPI.seen[1][2] is None  # no body on a GET
    finally:
        server.shutdown()
    with pytest.raises(gha.GitHubError, match="no GitHub token"):
        gha.urllib_transport("")("GET", "/x", None)
    with pytest.raises(gha.GitHubError, match="not an API path"):
        gha.urllib_transport("t")("GET", "https://evil.example/x", None)
