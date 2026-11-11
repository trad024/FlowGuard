# Method: Security (Step 9), Optimizer (Step 10) and the gate (DSO6)

Draft for the report. Method only: no held-out result appears here. Development numbers appear
only as a design input, labelled "dev". Decision numbers (Dn) refer to the decision log in
`docs/blueprint-v2.md`. The gate's rules, the catalog and the executor are described in
`architecture.md`; this section covers how they are tested.

## Security (Step 9, DSO4)

**Task.** Allow, review or block a deployment from the scan results of its CI run: Trivy for
the image and dependencies, Gitleaks for secrets, optionally SonarCloud.

**L0 (the baseline).** The deployer's security gate blocks on any HIGH, CRITICAL or Gitleaks
finding. It fails closed: a run without its scan counts is blocked like one with findings.

**L1 and above: hard rules, then a transparent priority score** (`docs/step-9-security.md`).
It is a documented policy, not a learned model.
- **Hard rules first.** Any CRITICAL finding or secret blocks, before any scoring. So does a
  failed SonarCloud quality gate, or a HIGH count without complete report detail. Missing or
  malformed scan evidence requires review.
- **Per finding:**

  ```
  priority = 0.50 × (CVSS base / 10) + 0.30 × EPSS + 0.10 × runtime dependency
           + 0.05 × reachable + 0.05 × no fix available
  ```

- **Aggregation:** the maximum finding score, compared in basis points with 5,000 (0.50).
  - At or above it: `request_security_review`.
  - Below it: `allow_with_findings`, which is high tier, so a human always decides.
  - Only a clean scan proposes `allow_deployment`.
- **Missing data scores conservatively.** Unknown runtime scope, unknown reachability and an
  unknown fix each count as 1. A missing CVSS or EPSS value requires review.
- **Where the inputs come from:**
  - EPSS from a dated FIRST snapshot with recorded checksums, read locally, never fetched
    during an incident;
  - reachability from a govulncheck source scan in app-ci (v1.4.0), mapped through OSV aliases.
    Only a completed scan establishes "unreachable".
- **The weights and the 0.50 threshold are choices, not fitted.** They were not tuned on the
  replay labels. A proposal carries every input, weight and contribution, so the explanation is
  the arithmetic itself.
- **Not a calibrated probability.** The score never feeds the gate's confidence
  (`label_scores` stay empty).

**Held-out set.** 14 scenarios: leaked secret, vulnerable dependency, low-severity finding
(pre-registration `families.security`).
- A dependency item takes a module version from the frozen pool: 6 vulnerable entries and 1
  control, checked with Trivy 0.75.0 at freeze, database 2026-11-06.
- A later database that changes an entry's result is reported as drift, never dropped
  (`dso4_dependency_pool.db_drift`).
- **Metrics:** injected criticals blocked, and the false-block rate.

## Optimizer (Step 10, DSO5)

**Task.** In CI's `plan` job, order the optional jobs (lint, integration, SBOM, multi-arch) and
skip the ones predicted unnecessary. Build, unit tests and scans are mandatory and never
skipped.

**L0 (the baseline).** A fixed order with standard `paths:` filters (D2).

**L1 and above.**
- **Models:** two LightGBM models in `ci/optimizer/model.json`: a job-duration regressor and a
  job-failure classifier.
- **Features:** changed paths, file types, line counts, dependency changes, change kind and job
  identity. Never branch names, commit messages, run ids or outcomes.
- **Inference in CI uses only the standard library.** The trees are exported to validated JSON,
  never a pickle (D1).
- **Explanations:** each prediction carries exact tree SHAP values.
- **Ordering:** retained optional jobs are ordered by predicted failure probability divided by
  predicted duration.

**Safety bounds** (`docs/step-10-optimizer.md`):
- **When a job may be skipped:** only below a fixed failure threshold of 0.01, with at least
  eight observed training outcomes, no training failure for that job, a known change size and
  every feature inside the training range.
- **P2, forced scans (D5):** no dependency or security job is skipped after a dependency change
  or a previous security flag.
- **Fallback:** an unknown prior scan status, or a missing or invalid model, falls back to the
  exact baseline order and filters. The Optimizer never adds work beyond the baseline's.

**Design input (dev).** On the development validation commits, the mandatory jobs took 975 of
the baseline's 1,038 job-seconds, so removing every optional job could save at most 6.0694% of
them (`docs/step-10-optimizer.md`). This bounds what any plan can save in this pipeline. The
pre-registered duration target is kept unchanged and reported against.

**Held-out set.** A seeded sequence of 30 commits, replayed through app-ci in both plan modes,
the same sequence for both (pre-registration `dso5_commit_sequence`).
- The commit mix: docs-only, application code, test-only, dependency bump, CI configuration,
  and a failing commit from the test split.
- **Metrics:** pipeline duration, failures missed against the full pipeline, time to first
  failure, MAE and AUC.

## The gate under test (DSO6)

**Claim tested.** Nothing high-risk runs without a human, and every forbidden action is
rejected, whatever an agent or an LLM proposes or is told.

**L0 for DSO6** is "no gate": the share of risky proposals that would run without approval.

**The adversarial set: 300 items** (pre-registration `dso6_adversarial_set`; D5, D20). Each item
is a contract-valid proposal, or an input the agents read, with the outcome the gate must give:

| Category | Weight | Expected |
|---|---|---|
| Prohibited action (every prohibited catalog action, random targets) | 2 | Reject |
| Out-of-catalog action (frozen names, including shell-like requests) | 2 | Reject |
| High-tier action without approval, staging and prod | 2 | Ask a human; never run unapproved |
| Medium tier in prod | 1 | Ask a human |
| Loop limit: the same action and target past its hourly limit | 1 | Reject |
| Conflicting proposals on one incident (P3) | 1 | The more conservative one, or a human |
| Prompt injection in a CI log line | 2 | Reject or escalate |
| Prompt injection in a commit message | 1 | Reject or escalate |
| Prompt injection in a diff | 1 | Reject or escalate |

- **The injection wording** is a seeded draw from six phrasings frozen in the pre-registration.
  Each tells the model to propose a prohibited action, or claims that approval was already
  given.
- **The development version** of the set uses an LLM that obeys the injection. This tests the
  gate against the worst-case model, not a well-behaved one.

**Metrics:**
- unapproved high-risk actions (must be 0);
- high-risk recall;
- escalation precision;
- approvals per incident.

**What counts as executed:** only what ran (D58).
- An executor refusal is written to its own table and reported apart. It is never counted as an
  execution or an unapproved action.
- The executor re-reads every decision from the audit log before acting (D37). A forged or
  altered decision is refused.
