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
The decision compares integer basis points against **5,000** (0.50): calculate the
weighted total in decimal, then round once to the nearest basis point, half up. With
CVSS 7.0, EPSS 0, unknown runtime/reachability and an available fix, the total is
exactly 0.500 = 5,000 bp and requests review. A score at least **0.50** proposes `request_security_review`; a lower score proposes
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
historical Trivy reports contain no such evidence: it remains unknown, scored 1. New dev
cases have real govulncheck evidence, described below. This adapter
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
and never use the network. The expanded development replay requires the full dated snapshot
with both pinned checksums. The three-row sample is used only for offline test fixtures.

## Govulncheck reachability in app-ci

The scans job installs **govulncheck v1.4.0** and runs a source/symbol scan with JSON output
on the same checked-out app commit. This version uses Go 1.25 or newer, compatible with the
app's Go 1.26 toolchain. Raw JSON, mapping status and enriched Trivy JSON are retained in
scan-reports. Vulnerability findings do not themselves fail CI; the Security gate still
decides. A failed/unsupported scan leaves reachability unknown.

The scan uses `CGO_ENABLED=0`, `GOOS=linux`, `GOARCH=amd64` and the two shipped entry points
`./cmd/podinfo ./cmd/podcli`, matching the Docker image's build configuration and binaries.

The offline adapter maps each finding's Go advisory ID through OSV CVE aliases. In a
completed source/symbol scan, a function trace establishes reachable; explicit module or
package findings without a called function establish unreachable. Any positive trace wins
across repeated findings. A CVE absent from findings, missing aliases, a package-only scan,
wrong scanner version, malformed/truncated data or a failed scan never establishes false.
Only mapped CVEs receive the `Reachable` boolean; other Trivy findings retain unknown.
Runtime dependency scope is not inferred from reachability.

Sources: [pinned module requirements](https://go.googlesource.com/vuln/+/refs/tags/v1.4.0/go.mod)
and [Go's JSON protocol documentation](https://pkg.go.dev/golang.org/x/vuln/internal/govulncheck).
The mapper does not run govulncheck or the network. Tests use small synthetic protocol messages.
The new CI step has now completed on eight real development scenario commits; see the
[per-case report](security-dev-cases.md) for all run links and measured reachability.

## Development replay and limitations

There are now **22 real recorded development scan cases**: the original 14 below and eight
deliberately selected HIGH-finding cases with real app-ci scans. The new eight have six
reachable and two unreachable targets, six with fixed versions and two with no known fixed
version, six low-EPSS and two high-EPSS cases. Reachability changes two whole-case L1 decisions;
EPSS changes none. Two high-EPSS cases also have a CRITICAL compatibility dependency and
therefore block under the hard rule. These are development diagnostics, not held-out results.
The [expanded report](security-dev-cases.md) records every requested field and the limitations.

The **original 14 historical cases** include 12 security-negative
controls. Nine are dev-branch CI runs, three are the previously recorded development
security cases, and two are named development release recordings. Unknown scenario IDs
were excluded. Source branch, commit, run/artifact IDs and raw-report SHA-256 are recorded
per case in `ci/security/dev-cases.json`. Raw artifacts and secrets stay outside git.
Repeated clean builds are not independent vulnerability examples.

The vulnerable report has CVSS 7.5 for CVE-2026-56851/56852 and real pinned EPSS values
0.00466/0.00475. Its maximum score is 0.526425 (5,264 bp), requiring review. The score is
retrospective on these historical artifacts using the documented 2026-10-28 snapshot.

```powershell
python ci/security/report_development.py
```

The historical 14 alone produced:

| Policy | Block | Review | Allow | False blocks / controls |
|---|---:|---:|---:|---:|
| L0 | 2 | 0 | 12 | 0/12 |
| Transparent score | 1 | 1 | 12 | 0/12 |

Both false-block Wilson 95% intervals are 0-24.25%. The secret is blocked by both. There
were no critical cases in that historical set. The expanded set now includes two real
critical-containing cases, both blocked by L0 and L1. No deployment executes during this
replay. Review is not permission to deploy or a counted block.

**No observed development case changes decision because of EPSS:** setting EPSS to zero
leaves every decision unchanged in all 22 cases. **Two new cases change decision because
of measured unreachability**, moving from review to allow with findings (still human-gated).
The original 14 reports predate govulncheck. Unknown runtime/reachability score 1.0; thus HIGH
CVSS >= 7.0 requests review even at EPSS zero unless explicit exposure evidence reduces it.

Verified: 22-case offline paired replay, eight successful real app-ci scenario runs,
exact 5,000-bp boundary, hard rules, conservative
missing data, malformed govulncheck protocol handling, alias/trace precedence, human routing,
firewall, mocked EPSS acquisition and real FIRST cache verification from the earlier run.
The workflow YAML is parsed and the tool is pinned; CI tests never invoke scans/network.
Not verified: live deployment, production effectiveness,
SonarCloud service integration or held-out evaluation. Frozen files remain unchanged.
