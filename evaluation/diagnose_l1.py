"""Train Diagnose L1 (TF-IDF + LR) on the corpus' training templates; score it on validation.

    python evaluation/diagnose_l1.py      # writes state/models/diagnose-l1.pkl and a score

Validation templates are never used for fitting; test templates are never read (D6, D39).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "evaluation"), str(ROOT / "scenarios")]

import diagnose_dev  # noqa: E402
import stats  # noqa: E402
from flowguard.classical import TfidfDiagnoser  # noqa: E402


def labelled(split_name: str) -> tuple[list[str], list[str], list[str]]:
    rows = [i for i in diagnose_dev.items(diagnose_dev.load_split()) if i["split"] == split_name]
    logs = [i["log"].read_text(encoding="utf-8", errors="replace") for i in rows]
    truth = [
        json.loads((diagnose_dev.CORPUS / i["variant"] / "meta.json").read_text("utf-8"))[
            "root_cause"
        ]
        for i in rows
    ]
    return [i["variant"] for i in rows], logs, truth


def main() -> int:
    _, train_logs, train_y = labelled("train")
    model = TfidfDiagnoser().fit(train_logs, train_y)
    path = model.save()
    names, val_logs, val_y = labelled("validation")
    pred = [model.predict(x) for x in val_logs]
    k = sum(p == y for p, y in zip(pred, val_y, strict=True))
    out = {
        "model": str(path.relative_to(ROOT)),
        "train": len(train_y),
        "validation": {"k": k, "n": len(val_y), "ci95": stats.wilson(k, len(val_y))},
        "confusion": dict(Counter(f"{y}->{p}" for y, p in zip(val_y, pred, strict=True))),
    }
    (diagnose_dev.OUT).mkdir(parents=True, exist_ok=True)
    (diagnose_dev.OUT / "L1-score.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
