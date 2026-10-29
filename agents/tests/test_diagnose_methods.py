"""Diagnose: one method per level, required, recorded on every proposal; the orchestrator refuses to
start without the level's method, and Diagnose itself refuses a log it cannot handle."""

import json
import re
from types import SimpleNamespace

import pytest

from flowguard import diagnose
from flowguard.agent_io import AgentInput
from flowguard.audit import AuditLog
from flowguard.encoder import EncoderDiagnose
from flowguard.executor import Executor
from flowguard.incidents import IncidentRecord
from flowguard.monitor import ci_event
from flowguard.orchestrator import Orchestrator, level
from flowguard.stubs import Diagnose, MethodUnavailable

LOG = "./main.go:3:2: undefined: foo\nFAIL\n"  # the regex calls this a code failure


class TfidfStub:
    def scores(self, log):
        return {"code": 0.1, "dependency": 0.7, "environment": 0.1, "flaky_test": 0.05,
                "infrastructure": 0.05}  # fmt: skip


class LLMStub:
    """Answers environment, quoting `quotes` (by default the log's first line, so grounded)."""

    def __init__(self, quotes=None, ungrounded=0):
        self.examples = []
        self.quotes, self.ungrounded = quotes, ungrounded

    def __call__(self, log, examples=None):
        self.examples.append(examples)
        quotes = log.splitlines()[:1] if self.quotes is None else self.quotes
        return {"root_cause": "environment", "evidence_lines": [1], "action": "open_fix_pr",
                "quotes": quotes, "ungrounded": self.ungrounded}  # fmt: skip


class EmptyRetriever:
    def nearest(self, log):
        return []


class Retriever:
    def nearest(self, log):
        return [diagnose.Example("x", "code", "open_fix_pr")]


class Encoder:
    def predict_with_scores(self, log, incident_id):
        report = SimpleNamespace(root_cause="flaky_test")  # only what EncoderDiagnose reads
        return SimpleNamespace(report=report, scores={"flaky_test": 0.9})


def full():
    return Diagnose(LLMStub(), l1=TfidfStub(), retriever=Retriever())


def inp(log=LOG):
    return AgentInput(incident_id="i-1", environment="staging", target="podinfo", log=log)


@pytest.mark.parametrize(
    "name,method,cls",
    [
        ("L0", "regex", "code"),
        ("L1", "tfidf_lr", "dependency"),  # TF-IDF, not the regex (the old fallback)
        ("L2", "llm_zero_shot", "environment"),
        ("L3", "llm_retrieval", "environment"),
        ("L4", "llm_retrieval", "environment"),
        ("L5", "llm_retrieval", "environment"),
    ],
)
def test_each_level_uses_and_records_its_own_method(name, method, cls):
    agent = full()
    out = agent.run(inp(), level(name))
    (p,) = out.proposals
    assert p.params["diagnose_method"] == method
    assert f"root cause {cls}" in p.rationale
    if method == "llm_retrieval":
        assert agent.llm.examples == [Retriever().nearest(LOG)]  # examples reach the model
    elif method == "llm_zero_shot":
        assert agent.llm.examples == [None]


def test_the_encoder_and_the_no_log_path_record_their_method():
    out = EncoderDiagnose(Encoder()).run(inp(), level("L1"))
    assert out.proposals[0].params["diagnose_method"] == "encoder"
    out = full().run(inp(""), level("L3"))
    assert out.proposals[0].params["diagnose_method"] == "no_log"


def make(tmp_path, name, **kw):
    audit = AuditLog(tmp_path / "a.db")
    return Orchestrator(
        cfg=level(name),
        record=IncidentRecord(tmp_path / "a.db"),
        executor=Executor(audit=audit),
        audit=audit,
        **kw,
    )


@pytest.mark.parametrize(
    "name,agent,needs",
    [
        ("L1", Diagnose(), "TF-IDF + LR model"),
        ("L2", Diagnose(l1=TfidfStub()), "LLM client"),
        ("L3", Diagnose(LLMStub()), "retrieval index"),
        ("L4", Diagnose(LLMStub()), "retrieval index"),
    ],
)
def test_the_orchestrator_refuses_to_start_without_the_levels_method(tmp_path, name, agent, needs):
    with pytest.raises(ValueError, match=f"refusing to start: {name}: .*{re.escape(needs)}"):
        make(tmp_path, name, diagnose=agent)


def test_levels_with_their_method_or_without_logs_start(tmp_path):
    make(tmp_path / "l0", "L0", diagnose=Diagnose())  # the regex needs nothing
    for name in ("L1", "L2", "L3", "L4", "L5"):
        make(tmp_path / name, name, diagnose=full())
        make(tmp_path / f"{name}-n", name, diagnose=Diagnose.without_logs())


def test_a_log_never_falls_back_to_the_regex():
    """The second guard: a Diagnose without its method refuses a log, at the agent."""
    with pytest.raises(MethodUnavailable, match="TF-IDF"):
        Diagnose().run(inp(), level("L1"))
    with pytest.raises(MethodUnavailable, match="without log diagnosis"):
        Diagnose.without_logs().run(inp(), level("L2"))
    no_log = Diagnose.without_logs().run(inp(""), level("L2"))  # D60 still answers
    assert no_log.proposals[0].params["diagnose_method"] == "no_log"


def test_the_method_is_in_the_audit_row(tmp_path):
    orch = make(tmp_path, "L1", diagnose=full())
    run = {"id": 7, "status": "completed", "conclusion": "failure"}
    jobs = [{"id": 5, "name": "unit", "conclusion": "failure"}]
    orch.handle(ci_event(run, jobs, 1.8e9, log=LOG))
    rows = orch.audit.query("SELECT params FROM proposals WHERE agent = 'diagnose'")
    assert [json.loads(r["params"])["diagnose_method"] for r in rows] == ["tfidf_lr"]


def test_the_llm_callable_shares_the_dev_runs_prompt_and_grounds_only_with_examples():
    sent = []

    def complete(messages):
        sent.append(messages)
        answer = {"root_cause": "code", "action": "open_fix_pr", "evidence": ["not in the log"],
                  "scores": {"code": 1}}  # fmt: skip
        return {"content": json.dumps(answer)}

    answer = diagnose.answerer(complete)
    raw = answer(LOG)  # the model's own answer: grounding is the agent's, by level
    assert raw["root_cause"] == "code" and sent[0] == diagnose.messages(LOG)
    assert raw["quotes"] == ["not in the log"] and raw["ungrounded"] == 1
    examples = Retriever().nearest(LOG)
    assert answer(LOG, examples)["root_cause"] == "code"
    assert sent[1] == diagnose.messages(LOG, examples)


def ungrounded_agent(retriever):
    return Diagnose(LLMStub(quotes=["a line the log does not have"], ungrounded=1),
                    retriever=retriever)  # fmt: skip


@pytest.mark.parametrize("name", ["L3", "L4", "L5"])
def test_grounding_is_on_from_l3_even_when_retrieval_returns_nothing(name):
    """D8 depends on the level, not on the examples: an L3 query with none is still grounded."""
    for retriever in (Retriever(), EmptyRetriever()):
        (p,) = ungrounded_agent(retriever).run(inp(), level(name)).proposals
        assert p.action == "escalate_to_human" and "root cause unclear" in p.rationale
        assert p.params["diagnose_grounding"] is True and p.params["diagnose_rejected"] is True


def test_grounding_is_off_at_l2():
    (p,) = ungrounded_agent(None).run(inp(), level("L2")).proposals
    assert p.action == "open_fix_pr" and "root cause environment" in p.rationale
    assert p.params["diagnose_grounding"] is False and p.params["diagnose_rejected"] is False


@pytest.mark.parametrize("name", ["L2", "L3"])
def test_the_raw_quotes_are_recorded_at_l2_and_l3_whatever_grounding_decides(name):
    """The hallucination rate is counted on what the model wrote, at both levels; grounding
    changes the decision only (otherwise H3's secondary claim holds by construction)."""
    (p,) = ungrounded_agent(EmptyRetriever()).run(inp(), level(name)).proposals
    assert p.params["diagnose_quotes"] == ["a line the log does not have"]
    assert p.params["diagnose_ungrounded"] == 1


class FixingLLM:
    """An answer with a fix sentence, quoting `quotes`."""

    def __init__(self, quotes):
        self.quotes = quotes

    def __call__(self, log, examples=None):
        return {"root_cause": "dependency", "evidence_lines": [1], "action": "pin_dependency",
                "quotes": self.quotes, "ungrounded": 0,
                "fix": "Pin golang.org/x/text to v0.20.0."}  # fmt: skip


@pytest.mark.parametrize("name", ["L2", "L3"])
def test_the_class_and_the_models_fix_reach_the_fix_planner(name):
    agent = Diagnose(FixingLLM(LOG.splitlines()[:1]), retriever=EmptyRetriever())
    (p,) = agent.run(inp(), level(name)).proposals
    assert p.params["diagnose_root_cause"] == "dependency"
    assert p.params["diagnose_fix"] == "Pin golang.org/x/text to v0.20.0."


def test_a_rejected_answer_passes_no_class_and_no_fix_to_the_planner():
    agent = Diagnose(FixingLLM([]), retriever=EmptyRetriever())  # no quotes: rejected at L3
    (p,) = agent.run(inp(), level("L3")).proposals
    assert (p.params["diagnose_root_cause"], p.params["diagnose_fix"]) == ("unclear", "")


def test_the_regex_and_tfidf_record_their_class_without_a_fix():
    for name, cls in (("L0", "code"), ("L1", "dependency")):
        (p,) = full().run(inp(), level(name)).proposals
        assert p.params["diagnose_root_cause"] == cls and "diagnose_fix" not in p.params
