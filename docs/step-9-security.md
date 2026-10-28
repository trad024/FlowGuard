# Step 9: transparent Security policy

Security selects deterministic hard rules plus an explicit vulnerability-priority score
when `cfg.ml` is enabled (L1?L5). The existing flag selects the implementation; this policy
is **not a learned model**. L0 and all frozen files remain unchanged. The 54 invented policy
fixtures, fitted logistic model and training dependencies have been removed. There is no
SHAP claim: explanations are the actual arithmetic contributions of the documented policy.

The agent only proposes catalog actions. It never sets a tier, executes, or supplies a
policy score as a calibrated class probability. `allow_with_findings` retains the existing
high-risk action and always needs human approval. Passing the score threshold cannot
bypass the gate.

## Evidence and hard rules

Supply decoded `trivy` and `gitleaks` reports in `params.scan_reports`, optionally with
SonarCloud. Scan-count dictionaries and the existing complete `scan-counts` annotation
remain supported. Any CRITICAL or secret blocks before loading EPSS, even with missing or
malformed scoring context. A failed SonarCloud quality gate also blocks. A positive HIGH
summary without complete report detail blocks conservatively. Missing or malformed scan
evidence requires review. Scanner payloads and secret contents never enter the proposal.

## Score: explicit weights and missing-data behavior

For each finding:

```text
priority = 0.50 ? (CVSS base / 10)
         + 0.30 ? EPSS probability
         + 0.10 ? runtime dependency
         + 0.05 ? reachable
         + 0.05 ? no fix available
```

Aggregate by the **maximum** finding score, not the sum of duplicated image-layer entries.
A score at least **0.50** proposes `request_security_review`; a lower score proposes
`allow_with_findings`. Only a clean scan can propose `allow_deployment`.

CVSS supplies the largest impact term; EPSS adds independently published exploitation
likelihood rather than learning deployment decisions from invented labels. Runtime scope
and reachability give limited exposure adjustments. The smaller no-fix term makes unresolved
remediation more urgent. These weights and the 0.50 threshold are transparent policy choices,
not fitted parameters, empirically calibrated probabilities or evidence of safe deployment.
They have not been optimized on the replay labels.

Trivy's actual vendor CVSS entries are read: prefer valid v3/v4 base scores, otherwise v2;
use the maximum provided score and report its vendor/version. Nonfinite/out-of-range scores
are invalid evidence. EPSS uses the CSV `epss` probability, never its percentile rank.
Missing CVSS or an unscored CVE always requires review; missing terms are displayed with
conservative upper values, not silently treated as zero risk. A missing or corrupt snapshot
also requires review. SonarCloud issues without these numeric inputs require review unless
a hard rule blocks them.

Only explicit boolean `RuntimeDependency: false` establishes dev-only scope. Unknown scope
receives the runtime value 1. Optional adapter fields `Reachable` (boolean) or `Reachability`
(`reachable` / `unreachable`) carry explicit reachability evidence if available. The actual
recorded Trivy reports contain no such evidence: it remains unknown, scored 1. This adapter
does not claim Trivy performs reachability analysis, infer reachability from dependency
paths, or interpret `Status: fixed` as proof of unreachability. A missing/unknown fix receives
no-fix value 1. Critical/secret rules are unaffected by any of these reductions.

Proposals contain every finding's input values, normalized values, weighted contributions,
maximum score, threshold, missing-required flags, and EPSS date/model/checksum. Their
rationale summarizes the maximum finding's contributions. `AgentOutput.label_scores` stays
empty because this policy score is not a class probability for D9.

## FIRST daily CSV: acquisition, provenance and cache

[FIRST's data documentation](https://www.first.org/epss/data.html) publishes the daily CSV
and historical archive. The acquisition script uses its provider's dated daily URL:

```powershell
python ci/security/download_epss.py --date 2026-10-28
# With a previously recorded compressed-file checksum:
python ci/security/download_epss.py --date 2026-10-28 --sha256 aa9eefe4155babd88985c26528087bba5053405d4d64aed85ea5dc5c2e16c4e9
```

The script saves `state/epss/YYYY-MM-DD.csv.gz`, decoded `.csv`, and a JSON manifest with
the source URL, publication date, model version and both SHA-256 hashes. It validates the
CSV header's date, columns, CVE IDs and probability ranges. A cached date is verified and
reused without network access; a checksum mismatch fails rather than silently replacing it.
A known `--sha256` also checks the downloaded/cached compressed source. Checksums detect
byte changes; they are not a signature or independent attestation of FIRST's model.

`current.csv` and `current.json` select the verified snapshot for incident handling.
`FLOWGUARD_EPSS_CSV` can instead pin a dated local CSV. The agent only reads local files;
it never fetches the network. Operators must run the acquisition script daily and retain
the manifests used for a replay. The agent records the selected publication date and does
not silently relabel an older pinned snapshot as current; no age-based refresh occurs inside
an incident handler.

On 2026-10-28, the actual downloaded file had 396,966 CVEs, EPSS model `v2026.06.15`:

- Compressed SHA-256: `aa9eefe4155babd88985c26528087bba5053405d4d64aed85ea5dc5c2e16c4e9`.
- Decoded SHA-256: `16ff6e9063ce2e2b31efbfa9bf98b574d501f84f22056861d31f3610665b5d06`.

The full files stay outside git. `ci/security/epss-sample.csv` commits three unmodified real
rows, with a manifest recording source-file and sample checksums, date and selection. This
is a tiny offline fixture, not a complete operational feed. Unit tests inject download bytes
and never use the network. The development replay explicitly selects the committed sample
so it reproduces offline.

## Development replay and limitations

The three sanitized development cases come from Actions runs 40383618943 (secret),
40501662040 (low-severity dependency control), and 40500780155 (vulnerable dependency).
Original scan-reports artifact IDs are 12680645324, 12733287463 and 12733410878. Full raw
artifacts remain outside git; only CVE/severity/fix/CVSS values and counts are retained.
The original vulnerable-dependency report gives CVSS 7.5 for CVE-2026-56851 and
CVE-2026-56852. Their real 2026-10-28 EPSS values are 0.00466 and 0.00475. Unknown scope
and reachability remain explicit. The maximum score is 0.526425, requesting review.

```powershell
python ci/security/replay.py
```

| Policy | Block | Review | Allow | False blocks / controls |
|---|---:|---:|---:|---:|
| L0 | 2 | 0 | 1 | 0/1 |
| Transparent score | 1 | 1 | 1 | 0/1 |

Both have zero measured false blocks on one control (Wilson 95% interval 0?79.35%). The
single secret is blocked by both. No critical occurs in these three runs, so measured
critical recall is undefined; hard-rule coverage comes from tests. Review does not mean
permission to deploy, and is not counted as a block. No deployment executes in this replay.
No improvement in false-block rate, calibrated risk prediction, or generalization is claimed.
The original scanner emitted HIGH/CRITICAL findings only: MEDIUM, dev-only and explicit
reachability cases are synthetic unit checks, not additional real validation examples.

Verified: real FIRST daily download and checksum, verified cache reuse, real CVSS preservation,
offline paired replay, hard rules with corrupt context, missing-data escalation, arithmetic
contributions, explicit scope/reachability, human routing, ablation switching and firewall.
The PR records the final full-suite count. Not verified: live deployment, production policy
quality, SonarCloud service integration or final evaluation. No frozen-file change is needed.
