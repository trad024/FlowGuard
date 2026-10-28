"""Diagnose L1 (D7): TF-IDF + logistic regression over the reduced log, seeded.

Trained on the corpus' training templates only, by the scoring side's training script, and saved
under state/models/ (D25), so the agent loads a fitted model and never reads the corpus.
Input: the reduced log's text without line numbers (logreduce), so L1 sees what L2 and L3 see.
Output: a probability per class, for the conformal calibration (D9).
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass, field
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from flowguard import logreduce
from flowguard.llm import STATE

MODEL = STATE / "models" / "diagnose-l1.pkl"
SEED = 20261028


def text_of(log: str) -> str:
    return "\n".join(t for _, t in logreduce.reduce(log))


@dataclass
class TfidfDiagnoser:
    pipeline: Pipeline = field(
        default_factory=lambda: Pipeline(
            [
                (
                    "tfidf",
                    TfidfVectorizer(
                        ngram_range=(1, 2),
                        sublinear_tf=True,
                        token_pattern=r"(?u)\b[A-Za-z_][A-Za-z0-9_.\-/]+\b",
                    ),
                ),
                (
                    "lr",
                    LogisticRegression(
                        C=1.0, max_iter=2000, class_weight="balanced", random_state=SEED
                    ),
                ),
            ]
        )
    )

    def fit(self, logs: list[str], labels: list[str]) -> TfidfDiagnoser:
        self.pipeline.fit([text_of(x) for x in logs], labels)
        return self

    def scores(self, log: str) -> dict[str, float]:
        probs = self.pipeline.predict_proba([text_of(log)])[0]
        return {c: float(p) for c, p in zip(self.pipeline.classes_, probs, strict=True)}

    def predict(self, log: str) -> str:
        s = self.scores(log)
        return max(s, key=s.get)

    def save(self, path: Path = MODEL) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pickle.dumps(self.pipeline))
        return path

    @classmethod
    def load(cls, path: Path = MODEL) -> TfidfDiagnoser:
        # only a model this project trained and saved under state/ is ever loaded
        return cls(pipeline=pickle.loads(path.read_bytes()))  # noqa: S301
