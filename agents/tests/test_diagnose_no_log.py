"""D60: without a log, every level takes one deterministic path: no LLM call, no guessed class,
"no diagnosis (no log)" in the incident record and the incident handed to a human."""

import pytest

from flowguard.agent_io import AgentInput
from flowguard.encoder import EncoderDiagnose
from flowguard.orchestrator import level
from flowguard.stubs import NO_LOG, Diagnose


class CountingLLM:
    def __init__(self):
        self.calls = 0

    def __call__(self, log):
        self.calls += 1
        return {"root_cause": "code", "evidence_lines": [1], "action": "open_fix_pr"}


def summary(out):
    return (
        [p.action for p in out.proposals],
        out.label_scores,
        [(e.agent, e.claim, e.refs) for e in out.evidence],
    )


@pytest.mark.parametrize("log", ["", "   \n\t\n"])
def test_no_log_gives_the_same_output_at_every_level_and_asks_no_model(log):
    llm = CountingLLM()
    agent = Diagnose(llm)
    outs = {}
    for name in ("L0", "L1", "L2", "L3", "L4", "L5"):
        inp = AgentInput(incident_id="i-1", environment="staging", target="podinfo", log=log)
        outs[name] = summary(agent.run(inp, level(name)))
    assert llm.calls == 0
    assert len(set(map(repr, outs.values()))) == 1  # one path for every level
    proposals, scores, evidence = outs["L3"]
    assert proposals == ["escalate_to_human"] and scores == {}  # no class, so no scores
    assert evidence == [("diagnose", NO_LOG, [])]


def test_an_absent_log_is_the_same_as_an_empty_one():
    llm = CountingLLM()
    inp = AgentInput(incident_id="i-1", environment="staging", target="podinfo")
    out = Diagnose(llm).run(inp, level("L2"))
    assert llm.calls == 0 and [e.claim for e in out.evidence] == [NO_LOG]


def test_the_encoder_slot_takes_the_same_path_without_a_log():
    class Unused:
        def predict_with_scores(self, log, incident_id):
            raise AssertionError("the encoder must not run without a log")

    inp = AgentInput(incident_id="i-1", environment="staging", target="podinfo", log="")
    out = EncoderDiagnose(Unused()).run(inp, level("L1"))
    assert [e.claim for e in out.evidence] == [NO_LOG]
    assert [p.action for p in out.proposals] == ["escalate_to_human"]


def test_a_log_still_reaches_the_model():
    llm = CountingLLM()
    inp = AgentInput(incident_id="i-1", environment="staging", target="podinfo", log="x\n")
    out = Diagnose(llm).run(inp, level("L2"))
    assert llm.calls == 1 and [p.action for p in out.proposals] == ["open_fix_pr"]
