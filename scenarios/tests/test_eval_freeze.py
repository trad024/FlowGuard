"""J3: the eval-freeze checks refuse unless dev is clean, in step with origin, untagged and
the Diagnose freeze holds; the lane check counts lanes and checks key formats without a call
and without printing a key. Fake git answers and a fake .env; nothing is tagged."""

import subprocess
from pathlib import Path

import eval_freeze

ROOT = Path(__file__).resolve().parents[2]


def fake_git(branch="dev", dirty="", head="a" * 40, remote="a" * 40, tagged=False):
    def run(*args, root=None):
        out, code = "", 0
        if args[:2] == ("rev-parse", "--abbrev-ref"):
            out = branch
        elif args[0] == "status":
            out = dirty
        elif args == ("rev-parse", "HEAD"):
            out = head
        elif args == ("rev-parse", "origin/dev"):
            out = remote
        elif args[:3] == ("rev-parse", "-q", "--verify"):
            code = 0 if tagged else 1
        return subprocess.CompletedProcess(args, code, out, "")

    return run


def frozen(root):
    return None


def thawed(root):
    raise SystemExit("agents/flowguard/diagnose.py changed since diagnose-freeze")


def test_ready_when_every_check_holds():
    assert eval_freeze.problems(run=fake_git(), diagnose_frozen=frozen) == []


def test_every_reason_is_reported():
    found = eval_freeze.problems(
        run=fake_git(branch="main", dirty=" M x.py", remote="b" * 40, tagged=True),
        diagnose_frozen=thawed,
    )
    assert found == [
        "on main, not dev",
        "the working tree is not clean",
        "dev is not in step with origin/dev (push or pull first)",
        "eval-freeze already exists: it is created once",
        "Diagnose freeze: agents/flowguard/diagnose.py changed since diagnose-freeze",
    ]


def test_the_cli_refuses_with_a_nonzero_exit(monkeypatch, capsys):
    monkeypatch.setattr(eval_freeze, "problems", lambda: ["the working tree is not clean"])
    assert eval_freeze.main(["check"]) == 1
    assert "refused: the working tree is not clean" in capsys.readouterr().out


KEY = "gsk_" + "A1" * 15


def test_lanes_are_counted_by_name_and_values_never_printed(monkeypatch, capsys):
    env = {f"GROQ_KEY_{n}": KEY + str(n) for n in range(1, 6)}
    env |= {"LLM_API_KEY": KEY + "x", "GITHUB_TOKEN_WRITE": "github_pat_secret"}
    monkeypatch.setattr(eval_freeze.lanes, "read_env", lambda: env)
    assert eval_freeze.main(["lanes", "--expect", "6"]) == 0
    out = capsys.readouterr().out
    assert (
        "lanes (6): GROQ_KEY_1, GROQ_KEY_2, GROQ_KEY_3, GROQ_KEY_4, GROQ_KEY_5, LLM_API_KEY" in out
    )
    assert "gsk_" not in out and "github_pat" not in out


def test_a_wrong_count_a_duplicate_or_a_malformed_key_fails(monkeypatch, capsys):
    env = {"GROQ_KEY_1": KEY + "1", "GROQ_KEY_2": KEY + "1", "GROQ_KEY_3": "not-a-key"}
    monkeypatch.setattr(eval_freeze.lanes, "read_env", lambda: env)
    assert eval_freeze.main(["lanes", "--expect", "6"]) == 1
    out = capsys.readouterr().out
    assert "lanes (2)" in out and "same value as GROQ_KEY_1" in out
    assert "malformed keys: GROQ_KEY_3" in out and "not-a-key" not in out


def test_the_scripts_check_test_then_tag_and_never_push():
    for name in ("eval-freeze.sh", "eval-freeze.ps1"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        check, tests, tag = (text.index(s) for s in ("eval_freeze.py check", "pytest", "git tag"))
        assert check < tests < tag
        assert "git push origin refs/tags/eval-freeze" in text  # printed, not run
        assert not any(line.strip().startswith("git push") for line in text.splitlines()), name
