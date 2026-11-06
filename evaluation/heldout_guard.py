"""The one gate for held-out Diagnose runs (D39, D65): test templates, LogChunks and held-out
recordings are touched only with an explicit --confirm-step-11 AND the staged freeze.

The confirmation is checked first and is never implied: a run without it is refused even when
the pre-registration is frozen and diagnose-freeze holds. With it, heldout.diagnose_frozen()
must pass too (frozen pre-registration, Diagnose code and artifacts unchanged since the tag).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scenarios"), str(ROOT / "ci"), str(ROOT / "agents")]

import heldout  # noqa: E402

FLAG = "--confirm-step-11"
SPLIT = ROOT / "evaluation" / "datasets" / "corpus-split.json"


def require_step_11(confirmed: bool, what: str) -> None:
    """Refuse a held-out run unless it is confirmed and the Diagnose freeze holds."""
    if not confirmed:
        raise SystemExit(
            f"{what} touches held-out material (test templates, LogChunks or held-out "
            f"recordings): Step 11 only, with {FLAG}"
        )
    heldout.diagnose_frozen()


def test_templates(split_path: Path = SPLIT) -> set[str]:
    split = json.loads(split_path.read_text(encoding="utf-8"))
    return {t for t, s in split["templates"].items() if s == "test"}


def is_heldout_recording(rec: Path, split_path: Path = SPLIT) -> bool:
    """A recording of the held-out set: in a held-out folder, a held-out spec id (h-...), or a
    ci scenario built from a test-split template."""
    if "heldout" in str(rec).lower():
        return True
    truth = rec / "ground_truth.json"
    if not truth.is_file():
        return False
    gt = json.loads(truth.read_text(encoding="utf-8"))
    spec, labels = gt.get("spec") or {}, gt.get("ground_truth") or {}
    if str(spec.get("id", "")).startswith("h-"):
        return True
    template = labels.get("template_id") or spec.get("template")
    return bool(template) and template in test_templates(split_path)
