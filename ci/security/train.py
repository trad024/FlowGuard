"""Fit a small, explicit development policy fixture set; this is not outcome data.

Run from the repository root: python ci/security/train.py
No experimental recordings or final evaluation data are used here.
"""

import hashlib
import itertools
import json
from pathlib import Path

import numpy as np
import shap
from sklearn.linear_model import LogisticRegression

from flowguard.contracts import Finding
from flowguard.security import FEATURES, MODEL, features


def policy_fixtures():
    """Exploratory reviewer policy: runtime/unknown HIGHs merit review; dev-only less so.

    No fix reduces urgency, never the hard-rule critical/secret block. Labels encode a
    provisional human review policy, not whether a vulnerability is exploitable or safe.
    """
    rows = []
    for severity, scope, fixed, count in itertools.product(
        ("low", "medium", "high"), (True, False, None), (True, False), (1, 2, 4)
    ):
        finding = Finding(
            tool="trivy",
            rule_id="policy-fixture",
            severity=severity,
            runtime_dependency=scope,
            fix_available=fixed,
        )
        review = severity == "high" and (scope is not False or fixed and count >= 2)
        review |= severity == "medium" and scope is not False and fixed and count >= 2
        rows.append({"features": features([finding] * count), "review": int(review)})
    return rows


def main():
    rows = policy_fixtures()
    x = np.asarray([r["features"] for r in rows])
    y = np.asarray([r["review"] for r in rows])
    model = LogisticRegression(C=1.0, random_state=17, max_iter=1000).fit(x, y)
    explainer = shap.LinearExplainer(model, x)
    values = explainer(x)
    expected = (x - x.mean(axis=0)) * model.coef_[0]
    np.testing.assert_allclose(values.values, expected, atol=1e-10)
    np.testing.assert_allclose(
        values.base_values + values.values.sum(axis=1), model.decision_function(x), atol=1e-10
    )
    payload = {
        "format": 1,
        "type": "logistic-shap",
        "model": "security-dev-policy-v1",
        "features": list(FEATURES),
        "weights": model.coef_[0].tolist(),
        "intercept": float(model.intercept_[0]),
        "background_mean": x.mean(axis=0).tolist(),
        "threshold": 0.5,
        "training": {
            "source": "explicit synthetic development review-policy fixtures, not outcomes",
            "rows": len(rows),
            "sha256": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
            "sklearn": "1.7.2",
            "shap": "0.49.1",
            "seed": 17,
        },
    }
    MODEL.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    Path("ci/security/policy-fixtures.json").write_text(
        json.dumps(rows, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Trained {len(rows)} policy fixtures; SHAP log-odds parity verified")


if __name__ == "__main__":
    main()
