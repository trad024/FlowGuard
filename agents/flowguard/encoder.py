"""Optional CPU encoder predictor with the existing Diagnose contracts and score channel."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from flowguard.contracts import DiagnosisReport
from flowguard.stubs import CLASSES, AgentOutput, Diagnose, proposal

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def prepare_text(log: str) -> str:
    """Shared train/inference preprocessing; bounded tail, no labels or metadata appended."""
    if not isinstance(log, str):
        raise ValueError("a reduced log must be text")
    lines = [line.rstrip() for line in ANSI.sub("", log).replace("\r", "\n").splitlines()]
    text = "\n".join(line for line in lines if line.strip())[-12000:]
    if not text:
        raise ValueError("empty reduced log")
    return text


@dataclass(frozen=True)
class EncoderPrediction:
    report: DiagnosisReport
    scores: dict[str, float]


class EncoderPredictor:
    """Backend injection keeps schema and deterministic tests independent of model downloads."""

    def __init__(self, logits: Callable[[str], Sequence[float]], model_id: str):
        self.logits = logits
        self.model_id = model_id

    def predict_with_scores(self, log: str, incident_id: str) -> EncoderPrediction:
        logits = [float(v) for v in self.logits(prepare_text(log))]
        if len(logits) != len(CLASSES) or not all(math.isfinite(v) for v in logits):
            raise ValueError("encoder must return five finite logits")
        largest = max(logits)
        exp = [math.exp(v - largest) for v in logits]
        scores = dict(zip(CLASSES, (v / sum(exp) for v in exp), strict=True))
        cause = max(scores, key=scores.get)
        report = DiagnosisReport(
            incident_id=incident_id,
            root_cause=cause,
            confidence=scores[cause],
            summary=f"Most likely root cause: {cause}.",
            evidence=[],
            similar_cases=[],
            llm_model=self.model_id,
            llm_tokens=0,
            llm_cost_usd=0,
        )
        return EncoderPrediction(report, scores)

    def predict(self, log: str, incident_id: str) -> DiagnosisReport:
        return self.predict_with_scores(log, incident_id).report

    @classmethod
    def from_directory(cls, directory: Path):
        """Load only local safetensors; never fetch a model during incident handling."""
        import torch
        from transformers import AutoConfig, AutoModelForSequenceClassification, AutoTokenizer

        directory = Path(directory)
        if not (directory / "model.safetensors").is_file():
            raise ValueError("trained safetensors checkpoint unavailable")
        config = AutoConfig.from_pretrained(
            directory, local_files_only=True, trust_remote_code=False
        )
        if config.model_type != "distilbert" or config.num_labels != len(CLASSES):
            raise ValueError("unsupported encoder configuration")
        if [config.id2label.get(i) for i in range(len(CLASSES))] != list(CLASSES):
            raise ValueError("encoder label order differs from Diagnose")
        tokenizer = AutoTokenizer.from_pretrained(
            directory, local_files_only=True, trust_remote_code=False
        )
        tokenizer.truncation_side = "left"
        model = (
            AutoModelForSequenceClassification.from_pretrained(
                directory,
                local_files_only=True,
                trust_remote_code=False,
                use_safetensors=True,
                attn_implementation="eager",
            )
            .to("cpu")
            .eval()
        )

        def logits(text):
            encoded = tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
            with torch.inference_mode():
                return model(**encoded).logits[0].tolist()

        return cls(logits, "distilbert-root-cause-v1")


class EncoderDiagnose(Diagnose):
    """Explicit optional Diagnose implementation; L0 remains the existing regex baseline."""

    def __init__(self, predictor: EncoderPredictor):
        super().__init__()
        self.predictor = predictor

    def method(self, cfg) -> str:
        return "encoder" if cfg.ml else "regex"

    def missing(self, cfg) -> str | None:
        return None  # the predictor is given; a failing one escalates (run below)

    def classify(self, inp, cfg):
        if not cfg.ml:
            return super().classify(inp, cfg)
        result = self.predictor.predict_with_scores(inp.log, inp.incident_id)
        return result.report.root_cause, result.scores, [], ""

    def run(self, inp, cfg):
        try:
            return super().run(inp, cfg)
        except (OSError, ValueError, RuntimeError, TypeError, OverflowError):
            p = proposal(
                self.name, "escalate_to_human", inp, "encoder unavailable or invalid output"
            )
            return AgentOutput(proposals=[p])
