"""Held-out scenario generator (D12 as changed by D26b).

    python scenarios/heldout.py commit-seed               # once, before any agent code
    python scenarios/heldout.py verify-seed
    python scenarios/heldout.py generate --confirm-step-11

A single developer cannot write a test set they do not know, so the held-out set is drawn by
this generator from the frozen pre-registration (docs/preregistration.yaml) with a secret seed.
`commit-seed` creates the seed, stores it in .env (never committed) and writes only its SHA-256
to scenarios/heldout-seed.sha256, which is committed: anyone can later check that the set was
drawn from the seed fixed before the agents existed. `generate` refuses to run unless the
pre-registration is frozen, the agent code is tagged eval-freeze (and agents/ has not changed
since), the seed matches the committed hash and Step 11 is confirmed; it writes the specs and
a seeded run order to scenarios/heldout/ (gitignored) and never prints them.

Every scenario is drawn from its own HMAC-derived random stream, so a scenario does not change
when another family's count changes. Release and runtime faults are drawn within an intensity
band (below, near or above the L0 thresholds), recorded in the ground truth. Ids are opaque.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import math
import random
import secrets
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "ci"), str(ROOT / "scenarios")]

import deployer  # noqa: E402  (.env parsing)
import runner  # noqa: E402  (spec validation)

PREREG = ROOT / "docs" / "preregistration.yaml"
HASH_FILE = ROOT / "scenarios" / "heldout-seed.sha256"
ENV_FILE = ROOT / ".env"
OUT = ROOT / "scenarios" / "heldout"
SEED_VAR = "FLOWGUARD_HELDOUT_SEED"
FREEZE_TAG = "eval-freeze"

# The frozen L0 thresholds (docs/blueprint-v2.md, "L0 canary baseline"), for expected_l0 only
L0_MAX_5XX = 0.01
L0_MAX_P99_MS = 500
BASE_MS = 20  # podinfo's own server time on /, added to a delay: an assumption, measured in 4b


# ---------------------------------------------------------------- seed commitment


def seed_hash(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def commit_seed(env_file: Path = ENV_FILE, hash_file: Path = HASH_FILE) -> str:
    """Create the secret seed once: the seed goes to .env, its SHA-256 to `hash_file`."""
    if hash_file.exists():
        raise SystemExit(
            f"{hash_file.name} already exists: the seed is committed, never replace it"
        )
    if deployer.read_env_file(env_file).get(SEED_VAR):
        raise SystemExit(f"{SEED_VAR} is already in {env_file.name}; refusing to overwrite it")
    seed = secrets.token_hex(32)
    text = env_file.read_text(encoding="utf-8") if env_file.is_file() else ""
    sep = "" if not text or text.endswith("\n") else "\n"
    env_file.write_text(f"{text}{sep}{SEED_VAR}={seed}\n", encoding="utf-8", newline="\n")
    digest = seed_hash(seed)
    hash_file.write_text(digest + "\n", encoding="utf-8", newline="\n")
    return digest


def checked_seed(env_file: Path = ENV_FILE, hash_file: Path = HASH_FILE) -> str:
    """The seed from .env, after checking it against the committed hash."""
    if not hash_file.is_file():
        raise SystemExit(f"no {hash_file.name}: run commit-seed first")
    seed = deployer.read_env_file(env_file).get(SEED_VAR)
    if not seed:
        raise SystemExit(f"{SEED_VAR} is not in {env_file.name}")
    if not hmac.compare_digest(seed_hash(seed), hash_file.read_text(encoding="utf-8").strip()):
        raise SystemExit(f"{SEED_VAR} does not match the committed hash")
    return seed


# ---------------------------------------------------------------- drawing


def stream(seed: str, label: str) -> random.Random:
    return random.Random(hmac.new(seed.encode(), label.encode(), hashlib.sha256).digest())


def draw(spec: dict, rng: random.Random) -> float | int:
    low, high = spec["low"], spec["high"]
    if spec["dist"] == "int":
        return rng.randint(int(low), int(high))
    if spec["dist"] == "uniform":
        return round(rng.uniform(low, high), 4)
    if spec["dist"] == "log_uniform":
        return round(math.exp(rng.uniform(math.log(low), math.log(high))), 4)
    raise ValueError(f"unknown distribution {spec['dist']!r}")


def weighted(kinds: dict, rng: random.Random) -> str:
    names = sorted(kinds)
    return rng.choices(names, weights=[kinds[k] for k in names])[0]


def predicted_l0(fault: dict) -> str:
    """The frozen L0 rule applied to a config release's injected settings."""
    kind, fraction = fault["kind"], float(fault.get("fraction", 1))
    if kind == "http_errors" and fault["error_rate"] * fraction > L0_MAX_5XX:
        return "abort"
    if kind == "latency" and fraction > 0.01:
        # p99 falls inside the affected requests; jitter is uniform, so take its p99 share
        share = 1 - 0.01 / fraction
        delay = fault["latency_ms"] + fault.get("jitter_ms", 0) * share
        if BASE_MS + delay > L0_MAX_P99_MS:
            return "abort"
    return "promote"


def ground_truth(family: str, fault: dict, band: str | None = None) -> dict:
    kind = fault["kind"]
    banded = {"band": band} if band else {}
    if family == "release":
        return {"bad_release": kind != "none", "expected_l0": predicted_l0(fault), **banded}
    if family == "runtime":
        return {"incident": True, "expected_l0": "detect", **banded}
    if family == "ci":
        return {"root_cause": kind, "expected_l0": "ci_failure"}
    if family == "security":
        bad = kind != "low_severity_finding"
        return {"bad_release": bad, "expected_l0": "block" if bad else "promote"}
    if family == "pipeline":
        return {"anomaly": True, "bad_release": False, "expected_l0": "detect"}
    raise ValueError(family)


def fault_params(cfg: dict, kind: str, rng: random.Random) -> tuple[dict, str | None]:
    """Parameters for one fault, drawn within a band when the family has bands."""
    bands = (cfg.get("bands") or {}).get(kind)
    band = weighted({b: v["weight"] for b, v in bands.items()}, rng) if bands else None
    dists = bands[band]["params"] if bands else (cfg.get("parameters") or {}).get(kind, {})
    params = {name: draw(dist, rng) for name, dist in dists.items()}
    if "effective_error_rate" in params:  # the service-wide share, spread over pods_fraction
        effective = params.pop("effective_error_rate")
        params["error_rate"] = round(min(1.0, effective / params["pods_fraction"]), 4)
    return params, band


def scenario(seed: str, family: str, index: int, cfg: dict) -> dict:
    rng = stream(seed, f"{family}:{index}")
    kind = weighted(cfg["kinds"], rng)
    fault = {"kind": kind, "delivery": cfg["delivery"]}
    params, band = fault_params(cfg, kind, rng)
    fault.update(params)
    if family == "ci":
        templates = [t for t in cfg.get("templates") or [] if t.startswith(f"{kind}-")]
        if not templates:
            raise SystemExit(f"no test-split templates for {kind} in the pre-registration")
        fault["template"] = rng.choice(sorted(templates))
    spec = {
        "id": "h-"
        + hmac.new(seed.encode(), f"id:{family}:{index}".encode(), "sha256").hexdigest()[:10],
        "family": family,
        "description": "held-out",
        "environment": "staging",
        "fault": fault,
        "intensity": 1,
        "ground_truth": ground_truth(family, fault, band),
    }
    stress = cfg.get("node_stress")
    if stress:  # good releases under stress must be promoted, bad ones still rolled back
        share = stress["share_of_none"] if kind == "none" else stress["share_of_bad"]
        if rng.random() < share:
            spec["node_stress"] = {"cpu_workers": draw(stress["cpu_workers"], rng)}
    for key in ("timing", "load"):
        if key in cfg:
            spec[key] = dict(cfg[key])
    return spec


def generate(prereg: dict, seed: str) -> list[dict]:
    specs = []
    for family in sorted(prereg["families"]):
        cfg = prereg["families"][family]
        if cfg.get("count") is None:
            raise SystemExit(f"{family}: no count in the pre-registration")
        specs += [scenario(seed, family, i, cfg) for i in range(int(cfg["count"]))]
    for s in specs:
        if errors := runner.validate_spec(s):
            raise SystemExit(f"{s['id']}: {errors}")
    return specs


def run_order(seed: str, specs: list[dict]) -> list[str]:
    """The seeded random order in which the live scenarios run (and every level replays)."""
    ids = sorted(s["id"] for s in specs)
    stream(seed, "run-order").shuffle(ids)
    return ids


def frozen_agents(root: Path = ROOT) -> None:
    """Refuse unless the eval-freeze tag exists and agents/ is unchanged since it."""

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)

    if git("rev-parse", "-q", "--verify", f"refs/tags/{FREEZE_TAG}").returncode != 0:
        raise SystemExit(f"tag the agent code {FREEZE_TAG} before generating the held-out set")
    changed = git("diff", "--quiet", FREEZE_TAG, "--", "agents").returncode
    untracked = git("ls-files", "--others", "--exclude-standard", "--", "agents").stdout.strip()
    if changed != 0 or untracked:
        raise SystemExit(f"agents/ has changed since {FREEZE_TAG}")


# ---------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("commit-seed", help="create the secret seed and write its SHA-256")
    sub.add_parser("verify-seed", help="check the seed in .env against the committed hash")
    gen = sub.add_parser("generate", help="write the held-out specs (Step 11 only)")
    gen.add_argument("--confirm-step-11", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "commit-seed":
        print(
            f"seed stored in .env; commit {HASH_FILE.relative_to(ROOT).as_posix()}: {commit_seed()}"
        )
        return 0
    if args.cmd == "verify-seed":
        checked_seed()
        print("seed matches the committed hash")
        return 0
    prereg = yaml.safe_load(PREREG.read_text(encoding="utf-8"))
    if prereg.get("status") != "frozen":
        raise SystemExit("the pre-registration is not frozen")
    if not args.confirm_step_11:
        raise SystemExit("held-out scenarios are generated only at Step 11 (--confirm-step-11)")
    frozen_agents()
    if OUT.is_dir() and any(OUT.glob("*.yaml")):
        raise SystemExit(f"{OUT} already holds specs; the held-out set is generated once")
    seed = checked_seed()
    specs = generate(prereg, seed)
    OUT.mkdir(parents=True, exist_ok=True)
    for s in specs:
        (OUT / f"{s['id']}.yaml").write_text(yaml.safe_dump(s, sort_keys=False), encoding="utf-8")
    order = "".join(i + "\n" for i in run_order(seed, specs))
    (OUT / "run-order.txt").write_text(order, encoding="utf-8", newline="\n")
    print(f"{len(specs)} held-out specs written to {OUT.relative_to(ROOT).as_posix()}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
