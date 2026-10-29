"""Tiny mocked model: real contracts, stable probabilities, proposals and calibration scores."""

import math

import pytest
from pydantic import ValidationError

from flowguard.catalog import AGENT_ACTIONS
from flowguard.contracts import AgentName, DiagnosisReport
from flowguard.encoder import CLASSES, EncoderDiagnose, EncoderPredictor, prepare_text
from flowguard.orchestrator import level
from flowguard.stubs import AgentInput


class TinyModel:
    def __call__(self, text):
        assert "checksum mismatch" in text
        return [-1.0, 3.0, 0.0, -2.0, 1.0]


def test_predictor_returns_existing_schema_and_deterministic_five_class_scores():
    predictor = EncoderPredictor(TinyModel(), "tiny-fixture")
    a = predictor.predict_with_scores("checksum mismatch", "i-1")
    b = predictor.predict_with_scores("checksum mismatch", "i-1")
    assert a == b and a.report.root_cause == "dependency"
    assert set(a.scores) == set(CLASSES) and math.isclose(sum(a.scores.values()), 1)
    assert all(0 <= v <= 1 for v in a.scores.values())
    assert DiagnosisReport.model_validate(a.report.model_dump()) == a.report
    assert predictor.predict("checksum mismatch", "i-1") == a.report
    assert a.report.llm_tokens == 0 and a.report.llm_cost_usd == 0
    assert not a.report.evidence
    with pytest.raises(ValidationError):
        DiagnosisReport.model_validate({**a.report.model_dump(), "class_scores": a.scores})


def test_adapter_uses_existing_label_score_channel_and_only_catalog_proposals():
    predictor = EncoderPredictor(TinyModel(), "tiny-fixture")
    agent = EncoderDiagnose(predictor)
    inp = AgentInput(incident_id="i", log="checksum mismatch")
    output = agent.run(inp, level("L1"))
    p = output.proposals[0]
    assert p.agent == AgentName.DIAGNOSE and p.action in AGENT_ACTIONS[AgentName.DIAGNOSE]
    assert output.label_scores[p.id] == predictor.predict_with_scores(inp.log, "i").scores
    assert p.confidence == 0  # the gate obtains confidence separately from D9


def test_l0_uses_regex_without_loading_or_calling_encoder():
    def unavailable(text):
        raise AssertionError("L0 must not call encoder")

    agent = EncoderDiagnose(EncoderPredictor(unavailable, "absent"))
    output = agent.run(AgentInput(incident_id="i", log="checksum mismatch"), level("L0"))
    assert output.proposals[0].action in AGENT_ACTIONS[AgentName.DIAGNOSE]


@pytest.mark.parametrize("logits", [[0] * 4, [float("nan")] * 5, [float("inf")] * 5, None])
def test_invalid_model_output_escalates(logits):
    agent = EncoderDiagnose(EncoderPredictor(lambda text: logits, "broken"))
    output = agent.run(AgentInput(incident_id="i", log="real failure"), level("L1"))
    assert output.proposals[0].action == "escalate_to_human"
    assert not output.label_scores


def test_shared_text_preprocessing_is_bounded_and_rejects_empty():
    assert prepare_text("\x1b[31mchecksum mismatch\x1b[0m\r\n") == "checksum mismatch"
    assert len(prepare_text("a" * 13000)) == 12000
    with pytest.raises(ValueError):
        prepare_text(" \n")
