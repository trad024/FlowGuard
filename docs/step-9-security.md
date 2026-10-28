# Step 9: Security agent

The Security stub now selects the learned policy when `cfg.ml` is enabled (L1–L5).
L0 is unchanged. The orchestrator and all frozen files are unchanged. The policy returns
`ActionProposal`s and label scores, never executes a command or sets a risk tier.

Pass decoded `trivy` and `gitleaks` artifacts in
`Event.payload.params.scan_reports`, with an optional `sonarcloud` report. Existing
`payload.scan_counts` and `params.scan_annotation` are supported. The latter accepts the
existing `scan-counts` annotation. Any secret, any critical finding (including without a
fix), or a failed SonarCloud quality gate blocks before model loading. An unexplained HIGH
summary also blocks. Missing, malformed or incomplete evidence requests review. Raw scanner
payloads are removed from proposals so secret values cannot enter the audit trail there.

Grey-zone findings use a small scikit-learn logistic regression. Each explanation includes
the score, threshold, model identity, expected value, all feature contributions and the top
three features. These are exact interventional linear SHAP values in **log-odds**, checked
against `shap.LinearExplainer` during training. Runtime needs no scientific packages and loads
a packaged JSON model, with finite-value/schema checks. `allow_with_findings` uses the
existing high-risk catalog action: the existing gate always requires human approval.

## Development evidence

`ci/security/dev-cases.json` contains minimal sanitized scan evidence from these recorded
development runs (no secret values, descriptions or paths):

| Case | Actions run | Evidence |
|---|---|---|
| Leaked secret | 40383618943 | 1 Gitleaks finding |
| Low-severity dependency control | 40501662040 | 0 HIGH, 0 CRITICAL, 0 secrets |
| Vulnerable dependency | 40500780155 | 4 HIGH, 0 CRITICAL, 0 secrets |

Sources: `https://github.com/trad024/FlowGuard/actions/runs/<run-id>`, `scan-reports`
artifacts 12680645324, 12733287463 and 12733410878 respectively. The dependency commits
pin websocket and x/text respectively. The full artifacts were fetched read-only and kept
outside version control. Labels follow the three corresponding development scenario YAMLs.

Paired offline replay (`ci/security/dev-results.json`):

| Policy | Block | Review | Allow | False blocks / controls |
|---|---:|---:|---:|---:|
| L0 | 2 | 0 | 1 | 0/1 (0%; Wilson 95% CI 0–79.35%) |
| Learned | 1 | 1 | 1 | 0/1 (0%; Wilson 95% CI 0–79.35%) |

There are **no criticals** in these three runs, so measured critical recall is undefined.
The secret is blocked (1/1; Wilson 95% CI 20.65–100%). No deployment was executed in this
replay. Review is not counted as a block or as permission to deploy.

The model was fitted to 54 **synthetic development review-policy fixtures**, generated
explicitly in `ci/security/train.py` and recorded with a checksum. They express provisional
review urgency, not exploitability labels or observed safe deployments. They are separate
from the three replay cases. No outcome-model validation or improvement over L0 is claimed:
L0 already has zero false blocks on the single control. More independently reviewed dev
findings are needed before treating the model as empirically validated. The current CI
scanner only emits HIGH/CRITICAL vulnerabilities; MEDIUM and dev-only cases are exercised
in offline tests, not established by these live artifacts. Dev-only scope is used only when
an adapter explicitly supplies `RuntimeDependency: false`; it is never guessed from a path.

## Reproduction and verification

Use Python 3.11:

```powershell
pip install -e ".[dev]"
pip install -r ci/security/requirements.txt
python ci/security/train.py
python ci/security/replay.py
ruff check .
ruff format --check .
pytest
```

Development tooling follows the approved D38 pin, Ruff 0.17.1. Its formatting of the
existing Markdown example in `agents/README.md` keeps CI consistent with local checks.
Verified: initial suite 503 passed / 3 skipped; SHAP parity on all 54 policy rows; paired
development replay; hard rules, grey-zone scores, explanation additivity, valid proposals,
catalog membership, human routing, ablation switching, model failure fallback and firewall.
The branch's final full-suite result is recorded in its PR.

Not verified: live scanner execution, SonarCloud service integration, live deployment,
production model quality, or final evaluation. Integration tests remain skipped by default.
No frozen-file changes are proposed for this task. Collect reviewed dev findings and expose
MEDIUM findings in the scan artifact before a larger development comparison.
