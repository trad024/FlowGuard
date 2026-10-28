"""Diagnose L1 (D7): TF-IDF + logistic regression learns from logs and round-trips to disk."""

from flowguard.classical import TfidfDiagnoser

LOGS = {
    "dependency": "go: verifying github.com/a/b@v1.2.3: checksum mismatch\nSECURITY ERROR",
    "code": "./main.go:12:3: undefined: handlerFunc\nFAIL build",
    "flaky_test": "--- FAIL: TestRace (0.01s)\n    timeout waiting for goroutine",
    "environment": "error: environment variable PODINFO_PORT is not set\nexit status 2",
    "infrastructure": "ERROR: failed to solve: dial tcp: lookup registry: no such host",
}


def model():
    logs, labels = [], []
    for label, log in LOGS.items():
        for i in range(4):
            logs.append(f"step {i}\n{log}")
            labels.append(label)
    return TfidfDiagnoser().fit(logs, labels)


def test_l1_separates_distinct_failures_and_scores_every_class():
    m = model()
    assert m.predict("verifying example.com/x@v0.1.0: checksum mismatch") == "dependency"
    scores = m.scores("lookup registry: no such host")
    assert set(scores) == set(LOGS) and abs(sum(scores.values()) - 1) < 1e-9


def test_l1_round_trips_through_state(tmp_path):
    m = model()
    path = m.save(tmp_path / "m.pkl")
    again = TfidfDiagnoser.load(path)
    log = "undefined: handlerFunc"
    assert again.scores(log) == m.scores(log)
