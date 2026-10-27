"""DSO2 L0 (D3): regex root-cause classifier and fixed playbook, on hand-written log lines."""

import pytest

from flowguard import baselines
from flowguard.catalog import CATALOG


@pytest.mark.parametrize(
    ("log", "expected"),
    [
        (
            "Step 4/9\nError: dial tcp: lookup mirror.example on 127.0.0.53:53: no such host",
            "infrastructure",
        ),
        (
            "Loaded image\ncurl: (28) Connection timed out after 20001 milliseconds",
            "infrastructure",
        ),
        ("go: go.mod requires go >= 1.26 (running go 1.22; GOTOOLCHAIN=local)", "environment"),
        (
            "ERROR: failed to resolve source metadata for docker.io/library/alpine:3.99",
            "environment",
        ),
        ("verifying github.com/x/y@v1.2.3: checksum mismatch", "dependency"),
        ("go: github.com/x/y@v1.9.9: invalid version: unknown revision v1.9.9", "dependency"),
        (
            "--- FAIL: TestRefresh (0.02s)\n    x_test.go:20: not done after 12ms (budget 10ms)",
            "flaky_test",
        ),
        ("--- FAIL: TestVersionHandler (0.00s)\n    handler returned unexpected body", "code"),
        ("./info.go:34:16: too many arguments in call to strconv.Itoa", "code"),
        ("all good, nothing to see", "unclear"),
    ],
)
def test_rules_classify_typical_failure_lines(log, expected):
    cls, lines = baselines.classify(log)
    assert cls == expected
    assert (lines == []) == (expected == "unclear")


def test_only_the_tail_of_the_log_is_read():
    noise = ["go: downloading toolchain go1.26.10"] + ["ok step"] * baselines.TAIL
    cls, _ = baselines.classify("\n".join(noise + ["./a.go:3:2: undefined: foo"]))
    assert cls == "code"  # the early toolchain line is outside the window


def test_playbook_covers_every_class_with_catalog_actions():
    assert set(baselines.PLAYBOOK) == {
        "code",
        "dependency",
        "environment",
        "flaky_test",
        "infrastructure",
        "unclear",
    }
    assert all(action in CATALOG for action in baselines.PLAYBOOK.values())
