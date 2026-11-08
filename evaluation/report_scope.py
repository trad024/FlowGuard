"""Whether a report may be confirmatory: proven held-out inputs, never a status flag alone.

A report is confirmatory only if all of these hold, checked here in this order; the first that
fails is the reason it is not:

    1. the pre-registration is frozen;
    2. the eval-freeze tag exists and agents/ is unchanged since it (heldout.frozen_agents);
    3. the held-out run order written by `heldout.py generate` exists, and the seed in .env still
       matches scenarios/heldout-seed.sha256 (heldout.checked_seed; the seed is never printed);
    4. every scenario id scored in the truth database is in that run order: no dev, synthetic
       or demo id.

Held-out Diagnose items (truth family "diagnose": corpus test-template variants and LogChunks
logs, run before generate by D39 and D69) are never in the run order. They are confirmatory only
if the pre-registration is frozen, harness-diagnose-freeze holds with its inputs unchanged
(heldout.harness_diagnose_frozen), and every scored Diagnose id is in the held-out Diagnose set
derived from those hashed inputs (`heldout_diagnose_ids`). DSO6 items (family "dso6") are
accepted from the generated scenarios/heldout/dso6-items.yaml alongside the run order, and the
held-out normal-traffic window under heldout.NORMAL_TRAFFIC_ID (family "normal").

The input labels come from the inputs, not from a flag: synthetic when the fixture marker
(report_demo) sits next to the truth database, development when a development bundle is given
or a scored id is a dev spec id (scenarios/dev/).
"""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scenarios"), str(ROOT / "ci"), str(ROOT / "agents")]

import heldout  # noqa: E402

SYNTHETIC_MARKER = ("SYNTHETIC.txt", "FlowGuard synthetic fixtures v1\n")  # report_demo writes it
DEV_IDS = {"normal-traffic"}  # dev ids that have no spec file (pilot.run_normal)


def scored_ids(truth_path: Path) -> list[str]:
    """The scenario (spec) ids of the runs in the truth database, read-only."""
    uri = Path(truth_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        return sorted({r[0] for r in db.execute("SELECT spec_id FROM runs") if r[0]})


def scored_families(truth_path: Path) -> dict[str, str]:
    """Scored spec id -> its family (from the spec stored with each run), read-only."""
    uri = Path(truth_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        rows = db.execute("SELECT spec_id, family FROM runs").fetchall()
    return {sid: fam or "" for sid, fam in rows if sid}


def heldout_diagnose_ids(root: Path = ROOT) -> set[str]:
    """The held-out Diagnose set, from the inputs hashed at harness-diagnose-freeze: test-split
    variant ids with a hashed corpus log, and lc-<sha12> for each sampled LogChunks log hashed."""
    hashed = set(heldout.frozen_harness_inputs(root))
    split = json.loads((root / heldout.SPLIT).read_text(encoding="utf-8"))
    test = {t for t, s in split["templates"].items() if s == "test"}
    corpus = heldout.CORPUS.as_posix()
    ids = {
        v
        for v, meta in split["variants"].items()
        if meta["template"] in test and any(h.startswith(f"{corpus}/{v}/") for h in hashed)
    }
    logs = (heldout.LOGCHUNKS / "logs").as_posix()
    with (root / heldout.H3_SAMPLE).open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["status"] == "used" and f"{logs}/{row['log_path']}" in hashed:
                ids.add(f"lc-{row['sha256'][:12]}")
    return ids


def dev_diagnose_ids(root: Path = ROOT) -> set[str]:
    split = json.loads((root / heldout.SPLIT).read_text(encoding="utf-8"))
    dev = {t for t, s in split["templates"].items() if s in ("train", "validation")}
    return {v for v, meta in split["variants"].items() if meta["template"] in dev}


def diagnose_scope(ids: set[str], root: Path = ROOT) -> str | None:
    """None when every scored Diagnose id is a frozen held-out Diagnose item (R2)."""
    try:
        heldout.harness_diagnose_frozen(root)
    except SystemExit as exc:
        return f"held-out Diagnose harness not frozen: {exc}"
    allowed, dev = heldout_diagnose_ids(root), dev_diagnose_ids(root)
    for sid in sorted(ids):
        if sid not in allowed:
            kind = "development" if sid in dev else "unknown"
            return f"scored Diagnose id not in the held-out Diagnose set ({kind}): {sid}"
    return None


def dso6_ids(root: Path = ROOT) -> set[str]:
    path = root / "scenarios" / "heldout" / "dso6-items.yaml"
    if not path.is_file():
        return set()
    return {str(i["id"]) for i in yaml.safe_load(path.read_text(encoding="utf-8")) or []}


def dev_spec_ids(root: Path = ROOT) -> set[str]:
    ids = set(DEV_IDS)
    for path in (root / "scenarios" / "dev").rglob("*.yaml"):
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(spec, dict) and spec.get("id"):
            ids.add(str(spec["id"]))
    return ids


def labels(truth_path: Path, dev_bundle=None, root: Path = ROOT) -> dict[str, bool]:
    marker = Path(truth_path).parent / SYNTHETIC_MARKER[0]
    synthetic = marker.is_file() and marker.read_text(encoding="utf-8") == SYNTHETIC_MARKER[1]
    development = dev_bundle is not None or bool(set(scored_ids(truth_path)) & dev_spec_ids(root))
    return {"synthetic": synthetic, "development": development}


def confirmatory_scope(specification: dict, truth_path: Path, root: Path = ROOT) -> str | None:
    """None when the inputs are proven held-out runs; else the first check that fails."""
    if specification.get("status") != "frozen":
        return "pre-registration is not frozen"
    families = scored_families(truth_path)
    diagnose = {i for i, f in families.items() if f == "diagnose"}
    if diagnose and (reason := diagnose_scope(diagnose, root)) is not None:
        return reason
    if diagnose and not set(families) - diagnose:
        return None  # a Diagnose-only report: stage A alone decides (D69)
    try:
        heldout.frozen_agents(root)
    except SystemExit as exc:
        return (
            "eval-freeze tag missing"
            if "tag the agent code" in str(exc)
            else f"agents/ changed since {heldout.FREEZE_TAG}"
        )
    order_path = root / "scenarios" / "heldout" / "run-order.txt"
    if not order_path.is_file():
        return "held-out run order missing (heldout.py generate has not run)"
    try:
        heldout.checked_seed(root / ".env", root / "scenarios" / "heldout-seed.sha256")
    except SystemExit as exc:
        return f"held-out seed check failed: {exc}"
    order = set(order_path.read_text(encoding="utf-8").split())
    order |= {i for i in dso6_ids(root) if families.get(i) == "dso6"}
    if families.get(heldout.NORMAL_TRAFFIC_ID) == "normal":  # the held-out normal window (R5)
        order.add(heldout.NORMAL_TRAFFIC_ID)
    ids = [i for i in scored_ids(truth_path) if i not in diagnose]
    if not ids:
        return "no scored scenario in the truth database"
    dev = dev_spec_ids(root)
    for sid in ids:
        if sid not in order:
            kind = (
                "development"
                if sid in dev
                else "synthetic"
                if sid.startswith("synthetic-")
                else "unknown"
            )
            return f"scored id not in the held-out run order ({kind}): {sid}"
    return None
