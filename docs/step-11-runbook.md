# Step 11 runbook: the held-out evaluation

The operator's procedure for Step 11. Sources:
- [`preregistration.yaml`](preregistration.yaml), version 1.5 (frozen 2026-11-06; amendments of
  2026-11-07 and 2026-11-08: D66 request cap, D67 calibrated Diagnose confidence, 6 lanes, the
  Step 11 environment, D69 staged harness freeze, D70 stage A additions, Diagnose scope, H3 cut
  rule and repeat subset, D71 DSO5 dependency pool);
- the decision log in [`blueprint-v2.md`](blueprint-v2.md) (D63 to D71 for this step);
- [`step-11-analysis.md`](step-11-analysis.md);
- the code under `scenarios/`, `evaluation/` and `scripts/`.

Commands are copied from the code's docstrings and argument parsers. Anything not yet confirmed
is marked **TO CHECK**. Commands are PowerShell from the repository root with
`$env:PYTHONPATH = "agents"` (or the editable install); the bash forms are the same with
`python` and forward slashes.

Never print or open the held-out seed, `scenarios/heldout/`, `evaluation/runs/groundtruth.db`
or a held-out output before the scoring step. The generator, the runners and the report read
them; the operator does not. Held-out Diagnose runs are blind (D69): they print progress only.

## 1. Preconditions

- [ ] **Environment (pre-registration 1.3, `environment`).** Python 3.13.0 with torch
  2.11.0+cpu, transformers 5.13.0, tiktoken 0.14.0, chromadb 1.5.9, lightgbm 4.6.0,
  scikit-learn 1.7.2 (and onnxruntime 1.31.0, numpy 2.3.4, drain3 0.9.11, langgraph 1.2.15):
  the environment of the calibrations. No upgrade until Step 11 ends. The declared 3.11 range is
  covered by CI only. Check: `py -c "import sys; print(sys.version)"` and
  `py -m pip show torch transformers tiktoken chromadb lightgbm scikit-learn`.
- [ ] **Pre-registration frozen,** `status: frozen`, version 1.3. A later change is a dated
  amendment, listed in the report (`freeze_rule`).
- [ ] **`diagnose-freeze`** on origin at `46c960c` (moved there by D67 from `f3d2ebf`, D66),
  with the Diagnose code and artifacts unchanged since. `heldout.diagnose_frozen()` checks both;
  `py scenarios\eval_freeze.py check` runs it among its checks.
- [ ] **Seed matches its hash:** `py scenarios\heldout.py verify-seed` prints
  `seed matches the committed hash`. It never prints the seed.
- [ ] **LLM lanes (D41, amended 2026-11-07, confirmed 2026-11-08): 6 lanes, one per team
  member, confirmed by the team lead**, `GROQ_KEY_1` to `GROQ_KEY_5` and `LLM_API_KEY`. A probe
  round on 2026-11-08 (one call per lane, sequential, early in the UTC day) left every lane at
  999 of 1,000 requests: no two keys share an organization's counter. Without any call: `py scenarios\eval_freeze.py lanes --expect 6` (names and key
  format only, never a value). Whether the 6 keys are 6 independent organizations needs one
  probe call per lane: the runners' lane check (`diagnose_dev.ready_lanes`, printed as
  `lane_check` and `independent_lanes` at the start of every LLM run). Record that count for the
  report. L5's live ci calls run on `LLM_API_KEY`.
- [ ] **Write token valid.** `GITHUB_TOKEN_WRITE`: write verified 2026-11-07 (branch create and
  delete, app-ci dispatch and cancel, the live pin), expires **2027-01-06 12:42:30 UTC** (D63).
  It needs Contents, Pull requests and Actions read and write on trad024/FlowGuard.
- [ ] **Free disk ≥ 40 GB** (an architect planning margin, not a measured need). Check with
  `Get-PSDrive C` and `docker system df`. For scale, 10 dev recordings take 43 MB.
- [ ] **Cluster healthy:** `.\infra\scripts\smoke.ps1` passes every check;
  `.\infra\scripts\check-resources.ps1` within the 4 GB budget; staging `Healthy` and stable.
- [ ] **No scored results in Prometheus** and `evaluation\results_exporter.py` stopped (the
  runner refuses a cluster run otherwise, `runner.run`).
- [ ] **No standalone deployer.** Do not run `ci\deployer.py run` during the held-out runs: it
  releases the newest green app-ci run on `dev` to staging and prod. The runner deploys each
  commit scenario itself (its own `scenario/<id>` branch, staging only); fix branches
  (`fix/flowguard-eval-*`) and the DSO5 branches (`scenario/<mode>/dso5-*`) are never released.
  Push nothing to `dev` under `app/`, `ci/` or the app-ci workflow until the cluster blocks end.
- [ ] **Laptop power:** sleep disabled. A sleep invalidates the running scenario (section 4).

## 2. Order: two freeze stages (D69)

| Stage | What | Frozen by | Order |
|---|---|---|---|
| A | The held-out Diagnose harness: `diagnose_heldout.py`, `diagnose_repeats.py`, `llm_provenance.py`, the toolkit (`report.py`, `report_metrics.py`, `report_data.py`, `report_policy.py`, `report_scope.py`, `observations.py`, `stats.py`), what they reuse (`diagnose_dev.py`, `heldout_guard.py`, `logchunks.py`), the corpus split and the H3 sample, and the SHA-256 of every held-out Diagnose input (`scenarios/harness-diagnose-inputs.sha256`, D70); `HARNESS_DIAGNOSE_PATHS` in `scenarios/heldout.py` | `harness-diagnose-freeze`, right before the first held-out Diagnose call | 1. K1 and K2 merged and reviewed; 2. lanes confirmed (D41); 3. `harness-diagnose-freeze`; 4. blocks 1 and 10 (allowed before eval-freeze by D39) |
| B | Everything else (agents, scenario runner and driver, replays, DSO5, DSO6, report) | `eval-freeze`, before `generate` and the first cluster run | 5. `eval-freeze`; 6. block 0 (generate); 7. blocks 2 to 9; 8. block 11 |

Tagging (each refuses unless `dev` is clean and in step with `origin/dev`, its tag is absent,
the Diagnose freeze holds and the full test suite passes; it tags locally and prints the push
commands, it never pushes). The stage A script first writes and commits the input hashes:

```powershell
.\scripts\harness-diagnose-freeze.ps1     # stage A
git push origin dev
git push origin refs/tags/harness-diagnose-freeze
.\scripts\eval-freeze.ps1                 # stage B
git push origin refs/tags/eval-freeze
```

Harness code may change until the first held-out result is seen, never after (D65).

### Blocks and estimates

| # | Block | Count | Needs | Estimated time |
|---|---|---|---|---|
| 1 | Diagnose: corpus test variants and LogChunks, L0 to L3 | 12 test variants; 230 LogChunks logs (D47) | stage A, lanes | part of the LLM total |
| 10 | L3 repeats (D61) | 30 items x repeats 2 and 3 | stage A, lanes, seed | part of the LLM total |
| 0 | Generate the held-out set | all families, DSO5, DSO6 | eval-freeze | minutes |
| 2 to 7 | Live scenarios in run order (driver) | release 72, runtime 147, security 14, pipeline 74, ci 12 | cluster, CI, write token | about 41 h of cluster plus about 10 h of pipeline CI (estimates below) |
| 4 | Normal traffic (DSO1 false alerts) | at least 4 h | cluster, overnight | 4 h |
| 8 | DSO5 commit sequence | 30 commits x 2 plan modes | CI, write token | about 5 h of CI at about 5 min per run |
| 9 | DSO6 gate set | 300 items | offline | minutes |
| 11 | Offline levels, scoring, report | every recording | none | hours |

**LLM time (blocks 1, 10 and the live ci calls), 6 lanes:** 568 calls, about 3.05M tokens
(D61). Each lane may use 180,000 tokens per UTC day (the client stops at 90% of its 200,000
daily ledger, `flowguard/llm.py`), so 3.05M / (6 x 180,000) = 2.82 days: **about 3 UTC days, a
4th possible** for the busiest lane (items are spread evenly, tokens per item are not). Tokens per
minute do not bind: each request is at most 8,000 tokens (D66), about one large call per lane
per minute.

Cluster estimates, from the dev runs: release 7.5 to 10 min per recording (about 13 h); runtime
about 9 min each (about 22 h; 420 s of spec timing, the 120 s quiet wait, healing and
recording); security about 11 min each (about 2.5 h); ci live about 2 min plus the fix-branch CI
(J4: 89 s for a dry-run ci spec). The J4 dev dry run measured 471 s for a release spec and 309 s
for a runtime spec.

### Block 1: Diagnose on test templates and LogChunks (stage A)

```powershell
py evaluation\diagnose_heldout.py run --confirm-step-11
py evaluation\diagnose_heldout.py run --confirm-step-11 --set logchunks --levels L2 L3   # a part
```

- Refuses unless `--confirm-step-11`, the Diagnose freeze and `harness-diagnose-freeze` hold.
- Corpus: every verified variant of a test-split template, L0 to L3. LogChunks: the `used` rows
  of `evaluation/datasets/logchunks-h3-sample.csv`, each log checked against its SHA-256, at L2
  and L3 (L0 and L1 quote no line and LogChunks has no class). L4 and L5 reuse L3 (D39, D64,
  D67).
- Resumable: rerun the same command; written items are skipped, finished calls come from the
  cache. Progress only (calls, cached, failures, tokens per lane).
- Outputs in `evaluation\runs\diagnose-heldout\`: `<L>.jsonl` replay records, `<L>-raw.jsonl`
  raw quotes and per-call records (lane, cache key, the D66 cut, retrieved example ids),
  `groundtruth.db` (labels only).
- Then `py evaluation\llm_provenance.py`.
- H3 under D66 (D70): each LogChunks record carries `targets_shown`, the target lines left in the
  request after the cut. An item with none left is a miss; the report also gives the share of
  such items per level and H3 with them excluded, labelled "sensitivity".

### Block 10: L3 repeats (D61, stage A)

```powershell
py evaluation\diagnose_repeats.py run --confirm-step-11
```

The subset: 30 items sampled from the sorted ids of every held-out Diagnose item, with the
held-out seed's HMAC stream labelled `llm-repeat-subset` (seed from `.env`, no `generate`).
Repeat 1 is the item's main L3 call (block 1); this makes repeats 2 and 3 with the repeat index
in the cache key (D28): 60 calls. The rule is pre-registered (1.4, D70).

### Block 0: generate (stage B)

```powershell
py scenarios\heldout.py generate --confirm-step-11
```

Refuses unless the pre-registration is frozen, `eval-freeze` exists with `agents/` unchanged,
the seed matches and `scenarios/heldout/` is empty. **Generated once.** Writes the specs,
`run-order.txt`, `dso5-sequence.yaml` and `dso6-items.yaml`, and prints only the count.

### Blocks 2 to 7: live scenarios (driver)

```powershell
py scenarios\pilot.py heldout --confirm-step-11
py scenarios\pilot.py normal --hours 4 --heldout --confirm-step-11   # block 4, overnight
```

The driver follows `scenarios\heldout\run-order.txt` and sets staging's Rollout before each
family, only when it changes:

| Family | Runner | Staging Rollout | Why |
|---|---|---|---|
| release | agents-mode recording; L0 replayed offline (D54) | agents | D54 |
| runtime | L0 mode | L0 | a runtime fault deploys nothing, but the runner records runtime in L0 mode and `healthy_stable()` refuses a mode mismatch (found by the J4 dev dry run, fixed in `4c012bf`) |
| pipeline, security | L0 mode, commit delivery | L0 | a green CI run is deployed by the deployer; under agents mode nobody resumes its pauses (1,200 s timeout) |
| ci | live at L5 with the live executor (D64) | unchanged | no cluster |

- Resumable: a spec with a valid recorded run (in its family's mode, recording folder present)
  is skipped; failed, interrupted and invalid runs run again. Up to 3 attempts when alerts are not
  quiet (D50).
- ci at L5: at the default gate setting a confident pin executes without a human (D67, rule 7);
  a proposal the gate sends to a human waits on the printed approval page (127.0.0.1) for up to
  60 minutes. Keep a person available during the ci specs.
- The commit families need a clean `dev` checkout; each run switches branches in it.
- **Pipeline (and ci) specs run one at a time**, not the "4 to 5 at a time" the pre-registration
  mentions (`families.pipeline.execution`): a pipeline anomaly is a slow step, measured as job
  duration; parallel runs share the runner pool and the laptop and would distort the very
  timings they measure. Pipeline takes about 10 h this way.

### Blocks 8 and 9: DSO5 and DSO6 (stage B)

```powershell
py evaluation\dso6_run.py run --confirm-step-11
py evaluation\dso5_run.py plan --confirm-step-11     # dry run: prints, pushes nothing
py evaluation\dso5_run.py run --confirm-step-11
```

- DSO6: every item through the real gate, approver and executor, offline, worst cases (highest
  calibrated confidence, a human who never approves, an LLM that obeys the injected text: no
  model call). Outputs in `evaluation\runs\dso6-heldout\`: the audit rows report.py counts,
  replay records, truth rows, and per item the gate outcomes, executions, refusals (D58) and
  unapproved risky executions (expected 0).
- DSO5: both plan modes on `scenario/baseline/dso5-heldout-d76` and
  `scenario/optimizer/dso5-heldout-d76` (plan.py takes the mode from the branch), from
  `origin/dev`, in a separate worktree; each commit pushed and its app-ci run recorded
  (conclusion, duration, time to first failure, jobs) in
  `evaluation\runs\dso5-heldout-d76\runs.jsonl` (D76). Resumable. The pre-D76 run
  (`evaluation\runs\dso5-heldout`, 14 baseline rows) is set aside and reported, never scored.
- `dependency_bump` commits (D71): the generator draws a module and its next patch version from
  `dso5_commit_sequence.dependency_pool` (6 entries, each scanned clean with Trivy 0.75.0, DB
  2026-11-07T18:32:58Z); `dso5_run.py` pins it with its go.sum lines (it runs
  `golang:1.26.10-alpine` for `go mod download`).
- D68: the Optimizer's skip list is applied by app-ci's plan step, not through the gate; DSO5
  measures it under P2 and the mandatory jobs.

### Block 11: offline levels, scoring and the report

The scored inputs are held-out only (D77). `state\heldout-recordings-A.txt`, `-B.txt` and
`-C.txt` list the valid held-out recording folders of each laptop (A in the repo, B and C under
`C:\fg-results`). Laptop A's truth DB and audit log also hold development rows, so they are
filtered first; B's and C's hold none (checked with `--check`) and are used as they are.

Corrected order (D79, as run on 2026-11-10). `ci_levels.py` writes its L0 to L4 proposals to
`state\audit.db`, and its records carry the proposal ids report_data joins on, so A's held-out
audit copy is built after it. The first `heldout_inputs` build only supplies the list of ci
recordings; the scored copy is the second build. `ci_levels.py` reads `LLM_API_KEY` from its
process environment, so the key is loaded from `.env` into that one process, never printed, and
removed afterwards (D80: for Step 11 it held GROQ_KEY_1's value; its tokens were added by hand to
`state\llm-usage-GROQ_KEY_1.json`, as the tool books them to the default ledger).

```powershell
py evaluation\heldout_inputs.py --confirm-step-11 --truth evaluation\runs\groundtruth.db --audit state\audit.db `
    --list state\heldout-recordings-A.txt --out evaluation\runs\heldout-A   # D77: lists, incl. recordings-ci.txt
$rec = Get-Content state\heldout-recordings-A.txt, state\heldout-recordings-B.txt, state\heldout-recordings-C.txt
py evaluation\monitor_replay.py score @rec --heldout --confirm-step-11 --records evaluation\runs\dso1-records.jsonl
py evaluation\release_replay.py score @rec --heldout --confirm-step-11 --records evaluation\runs\dso3-records.jsonl
# D79/D80: the key in this process only, never echoed
$env:LLM_API_KEY = ((Select-String -Path .env -Pattern '^GROQ_KEY_1=' | Select-Object -First 1).Line -replace '^GROQ_KEY_1=', '').Trim()
py evaluation\ci_levels.py @(Get-Content evaluation\runs\heldout-A\recordings-ci.txt) --confirm-step-11 --records evaluation\runs\ci-records.jsonl   # L0 to L4 on the L5 runs' recordings
Remove-Item Env:LLM_API_KEY
py evaluation\heldout_inputs.py --confirm-step-11 --truth evaluation\runs\groundtruth.db --audit state\audit.db `
    --list state\heldout-recordings-A.txt --out evaluation\runs\heldout-A-v2   # D79: now holds the ci_levels proposals
py evaluation\merge_heldout.py --confirm-step-11 --out evaluation\runs\report-inputs `
    --source diagnose:evaluation\runs\diagnose-heldout --source dso6:evaluation\runs\dso6-heldout `
    --source "scenarios-A:audit=evaluation\runs\heldout-A-v2\audit.db,truth=evaluation\runs\heldout-A-v2\groundtruth.db,replays=evaluation\runs\dso1-records.jsonl;evaluation\runs\dso3-records.jsonl;evaluation\runs\ci-records.jsonl" `
    --source "scenarios-B:audit=C:\fg-results\B\state\audit.db,truth=C:\fg-results\B\evaluation\runs\groundtruth.db" `
    --source "scenarios-C:audit=C:\fg-results\C\state\audit.db,truth=C:\fg-results\C\evaluation\runs\groundtruth.db"
# D81: evaluation\runs\report-inputs-v2 = report-inputs with "proposal_ids": [] on the 90 Diagnose L3 repeat records
py evaluation\report.py --confirmatory --audit evaluation\runs\report-inputs-v2\audit.db `
    --truth evaluation\runs\report-inputs-v2\groundtruth.db --replays evaluation\runs\report-inputs-v2\replay.jsonl --out results\final
py evaluation\dso5_metrics.py fetch --confirm-step-11   # D78: read-only GitHub, jobs and plans
py evaluation\dso5_metrics.py score --confirm-step-11 > results\final\dso5\dso5-metrics.json   # D78: DSO5, descriptive, outside report.py
# the redirect writes a BOM and CRLF: rewrite as UTF-8 without BOM, LF (the JSON is unchanged)
py -c "import pathlib; p = pathlib.Path(r'results\final\dso5\dso5-metrics.json'); b = p.read_bytes(); t = b.decode('utf-16') if b[:2] in (b'\xff\xfe', b'\xfe\xff') else b.decode('utf-8-sig'); p.write_bytes(t.replace('\r\n', '\n').encode('utf-8'))"
py evaluation\exploratory_heldout.py --confirm-step-11 --inputs evaluation\runs\report-inputs-v2 `
    --monitor-log state\unblind-logs\monitor_replay.log --laptop B=hakim --laptop C=moataz --out results\exploratory   # D82, exploratory
```

The held-out replay modes are gated like generate, labelled "held-out", never fit or calibrate
on held-out recordings, and write the replay records `report_data.load` reads. Dev mode still
refuses held-out paths.

`merge_heldout.py` (R5) refuses on any id collision between sources and on any development,
synthetic or dev-window id, before writing anything. `report_scope` (D70) accepts: scenario ids
in `run-order.txt`; DSO6 ids from `dso6-items.yaml`; the held-out normal window
`h-normal-traffic`; and held-out Diagnose ids from the inputs hashed at harness-diagnose-freeze,
only while that freeze holds.

## 3. Checkpoints

Run after every block. Stop at the first failure.

| Check | How | Pass |
|---|---|---|
| Recording count | `py -c "import json,pathlib; m=[json.loads(p.read_text()) for p in pathlib.Path('evaluation/recordings').glob('*/manifest.json')]; print(len(m), sum(x.get('valid', True) for x in m))"` (manifests only, never ground truth) | valid recordings went up by the block's count |
| Recording validity (J2) | the same manifests' `invalid_reasons`: a cluster recording with a failed or empty Prometheus source is invalid with its reason; a lost cluster is recorded invalid (`cluster_lost`) | every invalid run re-run (section 4) |
| No leaked ground truth (D25) | `py -m pytest evaluation\tests\test_agent_inputs.py` | all pass, none skipped for "no local recordings" |
| Disk | `Get-PSDrive C`; `docker system df` | still ≥ 40 GB free |
| LLM budget per lane | `state\llm-usage-<lane>.json` (tokens per UTC day, never the key) | each lane below its limit, or stopped and resuming the next UTC day |
| LLM provenance (D64) | `py evaluation\llm_provenance.py` | every cached answer has exactly one `llm_call` level |
| Fix-branch leftovers | `py scenarios\cleanup_fixes.py` (dry run) | only this block's fix branches; `--apply` after scoring |
| Freeze state | `py scenarios\eval_freeze.py check` refuses only because the tag exists; `heldout.diagnose_frozen()` and `heldout.harness_diagnose_frozen()` pass | unchanged since the tags |
| **Final report** | `py evaluation\report.py --confirmatory --out results\final` | `"confirmatory": true` |

If `confirmatory` is false, read `reason` (the first check that failed) and stop. Do not
re-label, re-run selectively or edit inputs. `report_scope.py` checks, in order: the
pre-registration is frozen; `eval-freeze` exists and `agents/` is unchanged since it;
`run-order.txt` exists and the seed matches its hash; every scored id is in the run order.
`report.py` also blocks on a pending H4 amendment and on any unset pre-registered count.

## 4. Failure recovery

The rule (`exclusions`): a run recorded with `valid: false` is re-run with the same spec and
seed, never dropped silently; the report gives the number of re-runs. Never re-run a valid run
because its result looks wrong.

| Failure | What the code does | Action |
|---|---|---|
| Laptop sleep | load-log gap over 60 s: `host_suspended`, `valid: false` | rerun the driver; it skips only validly recorded specs |
| Alerts not quiet before a fault | no fault injected, `valid: false`, up to 3 attempts (D50) | none; after 3, check what keeps firing |
| Canary not at 100% within 15 min (agents mode) | `canary_incomplete`, `valid: false` (D54) | check staging, rerun |
| Cluster lost mid-scenario (J2) | the run is recorded `valid: false` with `cluster_lost`; the recorder tries Prometheus once and marks every source unreachable; no hang. It is noticed at the next `kubectl` call, so up to the rest of the scenario's timed phases (192 s in the J4 test) | `k3d cluster start flowguard`; wait until every pod is Running; `.\infra\scripts\smoke.ps1`; rerun the driver |
| Docker disk full | Prometheus sources fail or come back empty: the recorder marks the run invalid with the reasons (J2) | free disk (`docker system prune` only on what is safe), rerun the invalid specs |
| Groq rate limit or quota | 429 retried after `retry-after`; a lane stops at 90% of its daily ledger; finished calls are cached | rerun the same command after the UTC reset |
| Request too large (413) | the D66 cut prevents it; one that still happens is an `llm_call_failed` audit row and an escalation, never retried | report it as is |
| A lane goes down | a rejected key (401) is dropped at the next start; the lane count is printed | note the change (D41); items are assigned by a seeded hash over the current lanes, so a smaller list moves unstarted items, not cached answers |
| Write token expired or missing a scope | the executor records a failed execution with GitHub's message (403) | fix the token; a failed pin stays a failed execution in the record |
| Executor failure partway through a fix action | the execution is recorded `success = 0` with GitHub's message; no retry. A pin can leave its fix branch (with or without the commit, or with its app-ci run); a draft PR can be left without its label | `py scenarios\cleanup_fixes.py --apply` removes every marked branch, issue and PR (an unlabelled PR is found by its marked head branch). It does **not** remove GitHub Actions runs on fix branches (they finish and stay in the history) or **GHCR images** a green fix-branch run pushed (`flowguard-podinfo:<fix commit SHA>`, as on the 2026-11-07 live pin); deleting those needs a packages-write token |
| An agent or LLM failure in the live ci block | recorded in the runner's `observed.agents`; the L0 recording is still made | report it; do not re-run for a different answer |

## 5. Day-by-day plan

Stage A runs first, on the lanes only; the cluster blocks wait for eval-freeze.

| Day | Daytime (attended) | Overnight / background |
|---|---|---|
| 1 | Preconditions; K1 and K2 merged and reviewed; lanes confirmed; `harness-diagnose-freeze`; start blocks 1 and 10 | LLM blocks (about 3 UTC days) |
| 2 | `eval-freeze`; block 0 (generate); start the driver: release first in run order, ci specs attended | Driver: release and runtime specs in run order |
| 3 | Driver continues; pipeline specs on CI; checkpoints | Driver continues |
| 4 | Blocks 8 (DSO5) and 9 (DSO6); checkpoints | Block 4: normal traffic, 4 h; LLM blocks finishing |
| 5 | Re-runs of every invalid run; full checkpoint pass; `cleanup_fixes.py --apply` | Spare |
| 6 | Block 11: replays, observations, report; final checkpoint | |

If the cluster falls behind, move overnights forward; never shorten a pre-registered timing or
count. The write token is valid until 2027-01-06.
