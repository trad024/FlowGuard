"""Held-out generator (D26b): seed commitment, refusals, reproducible and valid draws.
Temporary files only: no real seed is ever created here."""

import copy
import hashlib

import pytest
import yaml

import heldout
import runner

SEED = "ab" * 32


@pytest.fixture
def prereg():
    """The draft pre-registration with test counts and test-split templates filled in."""
    p = yaml.safe_load(heldout.PREREG.read_text(encoding="utf-8"))
    for family in p["families"].values():
        family["count"] = 12
    p["families"]["ci"]["templates"] = [
        "code-01",
        "dependency-02",
        "environment-03",
        "flaky_test-01",
        "infrastructure-04",
    ]
    return p


def test_the_committed_draft_is_not_frozen_and_has_no_counts():
    p = yaml.safe_load(heldout.PREREG.read_text(encoding="utf-8"))
    assert p["status"] == "draft"
    assert all(f["count"] is None for f in p["families"].values())
    assert set(p["families"]) == set(runner.FAMILIES)


def test_commit_seed_writes_only_the_hash_and_refuses_twice(tmp_path):
    env, digest_file = tmp_path / ".env", tmp_path / "seed.sha256"
    env.write_text("GITHUB_TOKEN_READ=x", encoding="utf-8")  # no trailing newline
    digest = heldout.commit_seed(env, digest_file)
    seed = heldout.deployer.read_env_file(env)[heldout.SEED_VAR]
    assert len(seed) == 64 and seed not in digest_file.read_text()
    assert digest == hashlib.sha256(seed.encode()).hexdigest() == digest_file.read_text().strip()
    assert env.read_text().startswith("GITHUB_TOKEN_READ=x\n")
    assert heldout.checked_seed(env, digest_file) == seed
    with pytest.raises(SystemExit):
        heldout.commit_seed(env, digest_file)
    digest_file.unlink()
    with pytest.raises(SystemExit):  # a seed already in .env is never overwritten either
        heldout.commit_seed(env, digest_file)


def test_a_seed_that_does_not_match_the_hash_is_refused(tmp_path):
    env, digest_file = tmp_path / ".env", tmp_path / "seed.sha256"
    heldout.commit_seed(env, digest_file)
    env.write_text(f"{heldout.SEED_VAR}={SEED}\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="does not match"):
        heldout.checked_seed(env, digest_file)


def test_generate_refuses_a_draft_and_needs_step_11(monkeypatch, tmp_path):
    monkeypatch.setattr(heldout, "OUT", tmp_path / "heldout")
    with pytest.raises(SystemExit, match="not frozen"):
        heldout.main(["generate", "--confirm-step-11"])
    frozen = tmp_path / "prereg.yaml"
    frozen.write_text("status: frozen\nfamilies: {}\n", encoding="utf-8")
    monkeypatch.setattr(heldout, "PREREG", frozen)
    with pytest.raises(SystemExit, match="Step 11"):
        heldout.main(["generate"])
    assert not (tmp_path / "heldout").exists()


def test_draws_are_valid_reproducible_and_independent_per_family(prereg):
    specs = heldout.generate(prereg, SEED)
    assert len(specs) == 12 * len(prereg["families"])
    assert specs == heldout.generate(prereg, SEED)
    assert len({s["id"] for s in specs}) == len(specs)
    assert all(s["id"].startswith("h-") and runner.validate_spec(s) == [] for s in specs)
    assert heldout.generate(prereg, "cd" * 32) != specs
    # More release scenarios leave every other family's draws unchanged
    more = copy.deepcopy(prereg)
    more["families"]["release"]["count"] = 20
    other = [s for s in heldout.generate(more, SEED) if s["family"] != "release"]
    assert other == [s for s in specs if s["family"] != "release"]


def test_drawn_parameters_stay_in_the_registered_ranges(prereg):
    for s in heldout.generate(prereg, SEED):
        params = prereg["families"][s["family"]].get("parameters") or {}
        for name, dist in params.get(s["fault"]["kind"], {}).items():
            assert dist["low"] <= s["fault"][name] <= dist["high"], (s["id"], name)
        if s["family"] == "ci":
            assert s["fault"]["template"] in prereg["families"]["ci"]["templates"]


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ({"kind": "none"}, "promote"),
        ({"kind": "http_errors", "error_rate": 0.005}, "promote"),
        ({"kind": "http_errors", "error_rate": 0.05}, "abort"),
        ({"kind": "http_errors", "error_rate": 0.05, "fraction": 0.1}, "promote"),
        ({"kind": "latency", "latency_ms": 800, "jitter_ms": 0, "fraction": 1.0}, "abort"),
        ({"kind": "latency", "latency_ms": 300, "jitter_ms": 300, "fraction": 0.03}, "abort"),
        ({"kind": "latency", "latency_ms": 800, "jitter_ms": 0, "fraction": 0.005}, "promote"),
    ],
)
def test_expected_l0_follows_the_frozen_thresholds(fault, expected):
    assert heldout.predicted_l0(fault) == expected
