"""Diagnose core (Step 7): log reduction, prompt, answer parsing, grounding (D8)."""

import json

from flowguard import diagnose, logreduce

LOG = "\n".join(
    [
        "\x1b[32m#1 [internal] load build definition\x1b[0m",
        *[f"go: downloading example.com/mod{i} v1.{i}.0" for i in range(30)],
        "verifying github.com/gorilla/mux@v1.8.1: checksum mismatch",
        "ERROR: failed to build: process did not complete successfully: exit code: 1",
        "Post job cleanup.",
        "this line comes after the cleanup marker",
    ]
)


def test_reduction_cuts_collapses_and_keeps_line_numbers():
    kept = logreduce.reduce(LOG)
    texts = [t for _, t in kept]
    assert not any("cleanup" in t or "\x1b" in t for t in texts)
    assert sum("downloading" in t for t in texts) == logreduce.PER_TEMPLATE
    assert kept[-1] == (33, LOG.splitlines()[32])
    assert logreduce.render(kept[-1:]).startswith("L33: ERROR")


def test_the_prompt_has_the_classes_actions_and_at_most_three_examples():
    ex = diagnose.Example("L1: boom", "code", "open_fix_pr")
    msgs = diagnose.messages(LOG, [ex] * 5)
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert all(c in system for c in diagnose.CLASSES)
    assert all(a in system for a in diagnose.ALLOWED)
    assert user.count("Similar past failure") == 3
    assert "L32: verifying github.com/gorilla/mux" in user
    assert diagnose.messages(LOG)[1]["content"].count("Similar past failure") == 0


def answer(**kw):
    base = {
        "root_cause": "dependency",
        "scores": {"dependency": 8, "code": 2},
        "action": "pin_dependency",
        "fix": "pin mux",
        "evidence": ["L32: verifying github.com/gorilla/mux@v1.8.1: checksum mismatch"],
    }
    return json.dumps({**base, **kw})


def test_a_grounded_answer_is_kept():
    d = diagnose.parse(answer(), LOG, apply_grounding=True)
    assert (d.root_cause, d.action, d.grounded, d.ungrounded, d.rejected) == (
        "dependency",
        "pin_dependency",
        [32],
        0,
        False,
    )
    assert abs(sum(d.scores.values()) - 1) < 1e-9 and d.scores["dependency"] == 0.8
    assert d.hallucination_rate == 0.0


def test_an_invented_line_is_measured_at_l2_and_rejected_at_l3():
    made_up = answer(evidence=["L5: go: module not found in the proxy", "checksum mismatch"])
    l2 = diagnose.parse(made_up, LOG, apply_grounding=False)
    assert l2.root_cause == "dependency" and l2.ungrounded == 1 and l2.hallucination_rate == 0.5
    l3 = diagnose.parse(made_up, LOG, apply_grounding=True)
    assert (l3.root_cause, l3.action, l3.rejected) == ("unclear", "escalate_to_human", True)
    assert diagnose.parse(answer(evidence=[]), LOG, apply_grounding=True).rejected


def test_bad_answers_degrade_safely():
    d = diagnose.parse("not json", LOG, apply_grounding=False)
    assert d.root_cause == "unclear" and "unparseable answer" in d.flags
    d = diagnose.parse(answer(root_cause="cosmic_rays", action="delete_namespace"), LOG, False)
    assert (d.root_cause, d.action) == ("unclear", "escalate_to_human")
    assert any("not allowed" in f for f in d.flags)
    fenced = "```json\n" + answer() + "\n```"
    assert diagnose.parse(fenced, LOG, apply_grounding=True).root_cause == "dependency"


def test_a_long_log_is_cut_to_the_character_budget_keeping_the_end():
    long = "\n".join(
        f"step {i}: " + "x" * (i % 7) + f" value={i * 7919 % 1000}" for i in range(5000)
    )
    long += "\nFATAL: the real failure"
    text = logreduce.render(logreduce.reduce(long))
    assert len(text) <= logreduce.MAX_CHARS and text.endswith("FATAL: the real failure")


def test_example_logs_are_cut_too():
    ex = diagnose.Example("A" * 10_000 + "TAIL", "code", "open_fix_pr")
    user = diagnose.messages("boom", [ex])[1]["content"]
    assert "TAIL" in user and "A" * (diagnose.EXAMPLE_CHARS + 1) not in user


def test_a_short_quote_counts_only_as_a_whole_line():
    log = "=== RUN TestX\ngot\n  want  \nFAIL\nsomething forgotten"
    assert diagnose.ground(["got", "want", "FAIL"], log) == ([2, 3, 4], 0)
    assert diagnose.ground(["forgot", "RUN"], log) == ([], 2)  # parts of lines, too short
