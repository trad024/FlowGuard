"""Calibrated confidence for the gate (D9): split conformal prediction, plain Python.

An agent's model gives a score per label (any scale where higher means more likely, e.g. class
probabilities). On held-apart validation data the calibrator stores each example's
nonconformity 1 - score(true label). For a new prediction, the p-value of a label is the share
of calibration examples at least as nonconforming as that label would be; the confidence of the
predicted label is 1 - the second-largest p-value (Vovk's conformal confidence): high only when
every other label is implausible. If even the best label's p-value (its credibility) is below
MIN_CREDIBILITY, the example looks unlike anything in the calibration data and the confidence
is 0: an out-of-distribution input goes to a human.

The gate never reads the confidence an agent or an LLM reports about itself. Without enough
calibration examples the confidence is 0, so a medium-tier action always goes to a human.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field

MIN_CALIBRATION = 20
MIN_CREDIBILITY = 0.05


@dataclass
class ConformalCalibrator:
    alphas: list[float] = field(default_factory=list)  # sorted nonconformity scores

    def fit(self, scores: list[dict[str, float]], labels: list[str]) -> ConformalCalibrator:
        if len(scores) != len(labels):
            raise ValueError("one label per score dict")
        self.alphas = sorted(1.0 - s.get(y, 0.0) for s, y in zip(scores, labels, strict=True))
        return self

    @property
    def calibrated(self) -> bool:
        return len(self.alphas) >= MIN_CALIBRATION

    def p_value(self, score: float) -> float:
        """Share of calibration examples at least as nonconforming as 1 - score."""
        alpha = 1.0 - score
        n = len(self.alphas)
        at_least = n - bisect_left(self.alphas, alpha)
        return (at_least + 1) / (n + 1)

    def p_values(self, scores: dict[str, float]) -> dict[str, float]:
        return {label: self.p_value(s) for label, s in scores.items()}

    def confidence(self, scores: dict[str, float]) -> tuple[str, float]:
        """(predicted label, calibrated confidence); confidence 0 when not calibrated."""
        if not scores:
            return "", 0.0
        predicted = max(scores, key=scores.get)
        if not self.calibrated:
            return predicted, 0.0
        ps = sorted(self.p_values(scores).values(), reverse=True)
        if ps[0] < MIN_CREDIBILITY:
            return predicted, 0.0
        second = ps[1] if len(ps) > 1 else 0.0
        return predicted, round(1.0 - second, 4)
