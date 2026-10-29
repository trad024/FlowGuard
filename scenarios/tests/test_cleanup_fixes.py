"""The cleanup of what the fix handlers created: a fake transport only, no network."""

import urllib.parse

import cleanup_fixes

REPO = "acme/app"
MARK = [{"name": "flowguard-eval"}]


def pr(number, ref, labels=(), repo=REPO):
    return {"number": number, "title": f"PR {number}", "labels": list(labels),
            "head": {"ref": ref, "repo": {"full_name": repo}}}  # fmt: skip


class FakeGitHub:
    def __init__(self, issues, pulls, refs, refuse=()):
        self.issues, self.pulls, self.refs, self.refuse = issues, pulls, refs, set(refuse)
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        url = urllib.parse.urlsplit(path)
        query = dict(urllib.parse.parse_qsl(url.query))
        route = url.path.removeprefix(f"/repos/{REPO}/")
        if method != "GET":
            return (422, {"message": "no"}) if route in self.refuse else (200, {})
        if route == "git/matching-refs/heads/fix/flowguard-eval-":
            return 200, [{"ref": f"refs/heads/{r}"} for r in self.refs]
        items = {"issues": self.issues, "pulls": self.pulls}[route]
        if route == "issues":
            assert (query["labels"], query["state"]) == ("flowguard-eval", "open")
        size, page = int(query["per_page"]), int(query["page"])
        return 200, items[(page - 1) * size : page * size]

    def writes(self):
        return [(m, p) for m, p, _ in self.calls if m != "GET"]


def github(**kw):
    issues = [
        {"number": i, "title": f"issue {i}", "labels": MARK} for i in range(1, 101)
    ]  # a full first page: the second is read too
    issues += [
        {"number": 101, "title": "PR 101", "labels": MARK, "pull_request": {}},
        {"number": 102, "title": "unmarked", "labels": [{"name": "bug"}]},  # never touched
    ]
    pulls = [
        pr(101, "fix/flowguard-eval-p-1", MARK),
        pr(103, "fix/flowguard-eval-p-3"),  # its label did not stick
        pr(104, "fix/someone-else"),
        pr(105, "fix/flowguard-eval-p-5", repo="fork/app"),  # a fork's branch, unlabelled
        pr(106, "scenario/r-1"),
    ]
    refs = ["fix/flowguard-eval-p-1", "fix/flowguard-eval-p-3", "fix/flowguard-eval-a..b"]
    return FakeGitHub(issues, pulls, refs, **kw)


def test_only_marked_items_are_found_and_a_dry_run_changes_nothing(capsys):
    client = github()
    found = cleanup_fixes.find(client, REPO)
    assert [n for n, _ in found["pulls"]] == [101, 103]
    assert [n for n, _ in found["issues"]] == list(range(1, 101))
    assert found["branches"] == ["fix/flowguard-eval-p-1", "fix/flowguard-eval-p-3"]
    client = github()
    assert cleanup_fixes.main(["--repo", REPO], client=client) == 0
    assert client.writes() == []
    assert "104 found in acme/app; dry run, nothing changed" in capsys.readouterr().out


def test_apply_closes_prs_then_issues_then_deletes_branches_and_reports_refusals(capsys):
    client = github(refuse={"issues/7"})
    assert cleanup_fixes.main(["--repo", REPO, "--apply"], client=client) == 1
    writes = client.writes()
    assert writes[:2] == [("PATCH", f"/repos/{REPO}/pulls/{n}") for n in (101, 103)]
    assert writes[2:102] == [("PATCH", f"/repos/{REPO}/issues/{n}") for n in range(1, 101)]
    assert writes[102:] == [
        ("DELETE", f"/repos/{REPO}/git/refs/heads/fix/flowguard-eval-p-1"),
        ("DELETE", f"/repos/{REPO}/git/refs/heads/fix/flowguard-eval-p-3"),
    ]
    closing = next(b for m, p, b in client.calls if p.endswith("/issues/1") and m == "PATCH")
    assert closing == {"state": "closed", "state_reason": "not_planned"}
    out = capsys.readouterr().out
    assert "REFUSED close issue #7 (422)" in out and "103 of 104 cleaned up" in out
    assert not any(p.endswith(("/102", "/104", "/105", "/106")) for _, p in writes)
    assert not any(b in p for b in ("dev", "main", "scenario") for _, p in writes)
