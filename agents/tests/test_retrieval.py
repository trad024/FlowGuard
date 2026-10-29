"""L3 retrieval (Step 7): a Chroma index of training cases, nearest of other templates only.
A bag-of-words embedding stands in for the sentence model, so nothing is downloaded."""

import hashlib

import numpy as np
import pytest
from chromadb.api.types import EmbeddingFunction

from flowguard import retrieval
from flowguard.diagnose import EXAMPLE_CHARS, messages


class Words(EmbeddingFunction):
    def __init__(self):
        pass

    def __call__(self, input):
        out = []
        for text in input:
            v = np.zeros(64, dtype=np.float32)
            for w in text.lower().split():
                v[int(hashlib.sha256(w.encode()).hexdigest(), 16) % 64] += 1.0
            out.append(v / (np.linalg.norm(v) or 1.0))
        return out

    @staticmethod
    def name():
        return "words-test"

    def get_config(self):
        return {}

    @staticmethod
    def build_from_config(config):
        return Words()


LOGS = {
    "code-01": ("code", "main.go:3:2: undefined: foo\nFAIL build"),
    "code-02": ("code", "handler.go:9:1: undefined: bar\nFAIL build"),
    "dep-01": ("dependency", "go: module example.com/x@v1.2.3: checksum mismatch"),
    "flaky-01": ("flaky_test", "--- FAIL: TestRace (0.01s) timeout waiting for lock"),
}


@pytest.fixture
def index(tmp_path):
    r = retrieval.Retriever(tmp_path / "chroma", embed=Words())
    cases = [
        {"variant": v, "template": v, "root_cause": c, "log": log} for v, (c, log) in LOGS.items()
    ]
    assert r.build(cases) == 4
    assert r.build(cases[:1]) == 4  # the same variant replaces its entry
    return r


def test_nearest_cases_come_from_other_templates(index):
    got = index.nearest("main.go:3:2: undefined: foo\nFAIL build", template="code-01")
    assert len(got) == 3
    assert got[0].root_cause == "code" and "bar" in got[0].log  # code-02, not itself
    assert all("foo" not in ex.log for ex in got)
    assert {ex.action for ex in got} <= set(retrieval.REFERENCE_ACTION.values())


def test_examples_are_cut_to_the_prompt_budget(index):
    long_log = "\n".join(f"step {i}: undefined: foo in main.go" for i in range(500))
    assert len(retrieval.document(long_log)) <= EXAMPLE_CHARS
    examples = index.nearest(long_log)
    user = messages(long_log, examples)[1]["content"]
    assert user.count("Similar past failure") == 3


def test_every_class_has_a_reference_action():
    from flowguard.diagnose import ALLOWED, CLASSES

    assert set(retrieval.REFERENCE_ACTION) == set(CLASSES)
    assert set(retrieval.REFERENCE_ACTION.values()) <= set(ALLOWED)
