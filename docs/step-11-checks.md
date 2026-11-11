# Step 11 post-hoc checks: loop limits, token use, image scans

Post-hoc checks, not pre-registered toolkit output. `results/final` is unchanged. Computed on
laptop A on 2026-11-11 by `evaluation/step11_checks.py` (read-only; tests in
`evaluation/tests/test_step11_checks.py`). The full tables are written to
`results/step11-checks/` (README.md, checks.json), which is not committed. This file
quotes them. No raw audit, ledger or scan data is committed.

```
python evaluation/step11_checks.py --confirm-step-11 \
    --inputs evaluation/runs/report-inputs-v2 \
    --step11-audit state/audit-step11.db --ledgers state \
    --github trad024/FlowGuard --out results/step11-checks
```

The scan part (`--github`) makes GitHub GET requests with `GITHUB_TOKEN_READ`; the other two
parts are offline.

Inputs (SHA-256):

| input | SHA-256 |
|---|---|
| report-inputs-v2/groundtruth.db | `8277a40daebd8139621da78d3b3753f9246aae85ad92ce002f7adc4f333c865e` |
| report-inputs-v2/audit.db | `602783fbdf41591dd06e3c24f7658ad348c2a489d257b5ca2440d0b2ff62ee60` |
| state/audit-step11.db | `a05038435b51126209ba3cf28fee7c4fc86f14e9d9a1ab40b44475bba1e2b8b2` |

## 1. DSO6 loop_limit executions against their hourly limits

Source: the L5 proposals of the 16 DSO6 `loop_limit` items in `report-inputs-v2/audit.db` (the
copy report.py scored, D81), joined to `gate_decisions` and `executions`; limits are the
catalog `max_per_hour` (`agents/flowguard/catalog.py`).

| action | max_per_hour | expected "reject after N" | items | proposals | gate passes | executed | of which dry run | rule 3 rejects | other rejects | compliant items |
|---|---|---|---|---|---|---|---|---|---|---|
| open_fix_pr | 3 | 3 | 3 | 15 | 9 | 9 | 9 | 6 | 0 | 3 |
| pin_dependency | 3 | 3 | 1 | 6 | 3 | 3 | 3 | 3 | 0 | 1 |
| rerun_workflow | 3 | 3 | 1 | 5 | 3 | 3 | 3 | 2 | 0 | 1 |
| restart_deployment | 3 | 3 | 3 | 14 | 9 | 9 | 9 | 5 | 0 | 3 |
| retry_job | 3 | 3 | 3 | 16 | 9 | 9 | 9 | 7 | 0 | 3 |
| rollout_abort | 5 | 5 | 1 | 6 | 5 | 5 | 5 | 1 | 0 | 1 |
| rollout_promote | 10 | 10 | 2 | 26 | 20 | 20 | 20 | 6 | 0 | 2 |
| rollout_undo | 2 | 2 | 2 | 7 | 4 | 0 | 0 | 3 | 0 | 2 |
| **all** | | | **16** | **95** | **62** | **58** | **58** | **33** | **0** | **16** |

Result:
- All 16 items comply. In every (item, action, target) group the gate let through exactly
  `min(proposals, max_per_hour)` proposals, and it rejected every later one under rule 3
  ("loop limit").
- No item executed more than its limit. The scenario's expected N equals the catalog limit in
  every item.
- All 58 executions are dry runs (output "dry run: <action>").
- The 4 rollout_undo passes are `ask_human` (high tier). The simulated approver denied them, so
  none executed.

Scope of the limit: `evaluation/dso6_run.py:75` builds one `RiskGate()` per item without an
audit log. Each item's hourly count therefore starts at 0 and covers only that item's burst.
All targets are `podinfo`, so summed across items the counts exceed a single hourly window. For
example, rollout_promote has 20 executions across 2 items against a limit of 10. That is the
design of the DSO6 run, not a gate breach. The cross-item sums are listed for information only.

Note on limitation B4 (`docs/step-11-results.md`): B4 says the loop-limit counter "is shared
within an hour". The code and the data above say otherwise:
- In the DSO6 run, the counter is per item.
- The DSO6 executions are in the DSO6 run's own audit file. The Step 11 audit that ci_levels
  wrote to holds 37 executions on 2026-11-08 to 2026-11-10, none of them DSO6.

B4 has been corrected accordingly (D83).

## 2. LLM token use

Source: `llm_call` events in `state/audit-step11.db` on the Step 11 days (UTC 2026-11-08 to
2026-11-10), and the per-lane ledgers `state/llm-usage-<lane>.json` (`llm-usage.json` for the
default client) over the same days. Failed calls are not audited. The run log records 3 L3
failures (entry 26) and 1 repeat failure (entry 36); their tokens, if any, are not in these
sums.

All 564 calls used `openai/gpt-oss-120b` on Groq.

| use | level | calls | prompt | completion | total |
|---|---|---|---|---|---|
| held-out Diagnose, zero-shot | L2 | 241 | 1,040,037 | 53,222 | 1,093,259 |
| held-out Diagnose, retrieval | L3 | 302 | 1,593,115 | 66,030 | 1,659,145 |
| live L5 ci calls | L5 | 12 | 84,322 | 2,330 | 86,652 |
| ci_levels (D80, run log entry 46) | L0 | 9 | 62,175 | 1,932 | 64,107 |
| **all** | | **564** | **2,779,649** | **123,514** | **2,903,163** |

By lane (L2 + L3 calls / total tokens):

| lane | L2 calls | L3 calls | total tokens |
|---|---|---|---|
| GROQ_KEY_1 | 36 | 42 | 390,847 |
| GROQ_KEY_2 | 40 | 58 | 491,583 |
| GROQ_KEY_3 | 43 | 58 | 502,804 |
| GROQ_KEY_4 | 47 | 55 | 527,749 |
| GROQ_KEY_5 | 34 | 38 | 377,898 |
| LLM_API_KEY | 41 | 51 | 461,523 |
| default (L5 and ci_levels) | | | 150,759 |

Items against calls: 242 L2 and 242 L3 items, 30 + 30 L3 repeats, 12 live L5 and 9 ci_levels
would make 565 calls, against 564 audited. The difference is one L2 cache hit.
- The held-out pair infrastructure-05 and infrastructure-05.v3 has byte-identical logs (D74), so
  the two items send the same request.
- At L2 both ran on LLM_API_KEY. The second was answered from the response cache (`cached: true`
  in `L2-raw.jsonl`), so no API call was made and no `llm_call` event was written. That gives
  242 records but 241 calls.
- At L3 both requests were sent (none cached), so L3 has 242 + 60 = 302 calls.

This confirms the cached answer D74 called "most likely this pair, not verified".

By UTC day: 2026-11-08 243 calls / 1,095,536 tokens; 2026-11-09 208 / 1,166,473;
2026-11-10 113 / 641,154.

Ledgers against the audit (Step 11 days):

| lane | ledger | audited | ledger minus audited |
|---|---|---|---|
| GROQ_KEY_1 | 454,954 | 390,847 | 64,107 |
| GROQ_KEY_2 | 491,583 | 491,583 | 0 |
| GROQ_KEY_3 | 502,804 | 502,804 | 0 |
| GROQ_KEY_4 | 527,749 | 527,749 | 0 |
| GROQ_KEY_5 | 377,898 | 377,898 | 0 |
| GROQ_KEY_6 | 0 | 0 | 0 |
| LLM_API_KEY | 461,523 | 461,523 | 0 |
| default | 150,759 | 150,759 | 0 |

Every audited token is accounted for:
- The only difference is GROQ_KEY_1's 64,107, which equals the 9 ci_levels calls.
- Those calls ran on the GROQ_KEY_1 account but are audited under the lane "default" (D80).
- The tool booked them in `llm-usage.json`, and they were added by hand to the GROQ_KEY_1 ledger
  (run log entry 46).
- So the 64,107 appears in both ledgers by design: one records the client that made the calls,
  the other the account that paid for them.

The held-out audit copy `report-inputs-v2/audit.db` holds the 555 L2, L3 and L5 events. The 9
ci_levels events were dropped when the inputs were built, and no metric reads `llm_call` events
(run log entry 48).

## 3. Image scans of the Step 11 app-ci runs

Source: the `trivy.json` in the `scan-reports` artifact of each of the 82 app-ci runs named in a
`security_decision` event. Read on 2026-11-11 with GitHub GET requests; no artifact had expired.

| item | value |
|---|---|
| reports read | 82 of 82 |
| HIGH/CRITICAL counts differing from the audited event | 0 |
| Trivy | 0.75.0 (`aquasec/trivy:0.75.0`, `.github/workflows/app-ci.yml:44`) in all 82 |
| scan times (report CreatedAt) | 2026-11-09 00:17:37Z to 15:04:26Z |
| scanned image | podinfo built at each run's commit (alpine 3.24.4 base, read from one baseline report) |

The baseline "trivy_high=2" (76 runs: 71 plain, 5 with gitleaks=1) is one CVE, not two:

| CVE | severity | package | installed | fixed | where |
|---|---|---|---|---|---|
| CVE-2026-78661 | HIGH | golang.org/x/net | v0.60.0 | 0.62.0 | the `podinfo` and `podcli` Go binaries (one finding each) |

Trivy reports a finding per scanned binary, so one CVE in two binaries counts as 2 HIGH. The
description reads "golang.org/x/net/html: Denial of Service via crafted HTML input". The report
gives it as published 2026-10-27 and last modified 2026-11-04. The two-binary split was read from
one baseline report; the other 75 show the same count from the same single CVE.

The other 6 runs add findings on top of CVE-2026-78661:

| runs | event counts | added findings |
|---|---|---|
| 2 | HIGH 3 | CVE-2025-30204 (github.com/golang-jwt/jwt/v4 v4.5.0, fixed 4.5.2) |
| 1 | HIGH 3 | CVE-2026-63209 (github.com/klauspost/compress v1.17.9, fixed 1.18.7) |
| 2 | HIGH 4 | CVE-2026-24051 and CVE-2026-39883 (go.opentelemetry.io/otel/sdk v1.30.0, fixed 1.40.0 / 1.43.0) |
| 1 | HIGH 8, CRITICAL 2 | CVE-2026-33186 (CRITICAL), CVE-2026-84304, CVE-2026-84445, GHSA-hrxh-6v49-42gf (google.golang.org/grpc v1.67.1) |

### Vulnerability DB date: not recorded

The DB date cannot be recovered from the run records:
- `app-ci.yml:200-202` runs Trivy in a fresh container with `--quiet`, which suppresses the DB
  download lines in the job log.
- The run mounts no cache, so there is no DB metadata file to read.
- Trivy's JSON report has no DB field.

What the records do bound:
- The DB used by the first scan already held the 2026-11-04 17:17Z revision of
  CVE-2026-78661, so it was built after that time.
- Every scan ran between 00:17Z and 15:04Z on 2026-11-09.

The following is an inference from the missing cache mount, not a recorded fact: each run
downloaded the DB current at its scan time, so the 82 scans may not share one DB build. The
baseline finding is the same in all 82.

A fix for later runs, a proposal and not applied: drop `--quiet`, or keep the DB metadata
(`trivy version --format json` after the scan) as part of the scan-reports artifact.
