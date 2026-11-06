# Step 11 runbook: the held-out evaluation

The operator's procedure for Step 11. Sources:
- [`preregistration.yaml`](preregistration.yaml), version 1.0, frozen 2026-11-06 (`04dcd41`);
- [`step-11-analysis.md`](step-11-analysis.md);
- the code under `scenarios/` and `evaluation/`.

Commands are copied from the code's docstrings and argument parsers. Anything the code does not
provide is marked **TO CHECK**: build or confirm it before relying on this runbook. Commands are
PowerShell from the repository root with the virtual environment active. The bash forms are the
same with `python` and forward slashes.

Never print or open the held-out seed, `scenarios/heldout/` or `evaluation/runs/groundtruth.db`
outside the scoring step. The generator and the report read them; the operator does not.

## 1. Preconditions

All of these must hold before `heldout.py generate`.

- [ ] **Pre-registration frozen.** `status: frozen`, `version: "1.0"`, `amendments: []`. A
  change after freezing is a dated amendment, listed in the report (`freeze_rule`).
- [ ] **`diagnose-freeze` tag** on origin (it exists, at `04dcd41`, `git ls-remote --tags
  origin`), and the Diagnose code and artifacts unchanged since it. `heldout.diagnose_frozen()` checks both, and
  `diagnose_dev.py` calls it for any split other than train and validation. TO CHECK: there is
  no standalone command for this check.
- [ ] **`eval-freeze` tag** on the commit that freezes all agent code, pushed. `generate` refuses
  without it, or when `agents/` differs from it or holds untracked files
  (`heldout.frozen_agents`). Creating the tag is not scripted: TO CHECK the exact commit and the
  `git tag eval-freeze <sha>` / `git push origin eval-freeze` commands with the integrator.
- [ ] **Seed matches its hash:** `py scenarios\heldout.py verify-seed` prints
  `seed matches the committed hash`. It never prints the seed.
- [ ] **LLM lanes confirmed (D41).** The lane check runs at the start of
  `py evaluation\diagnose_dev.py run`. It prints `lane_check`, `independent_lanes` and, when the
  count changed, `independent_lanes_changed`. Record the number of independent lanes for the
  report. TO CHECK: there is no lane check that runs without starting LLM calls.
- [ ] **Write token valid.** `GITHUB_TOKEN_WRITE` expires **2026-11-28 21:08 UTC** (D63). Every
  block that writes to GitHub (the live ci block, the DSO5 sequence, the cleanup) must finish
  before then. Its write scope is verified by the first live pin, the D64 acceptance test.
- [ ] **Free disk ≥ 40 GB** (the architect's threshold; no committed source). Check with
  `Get-PSDrive C` and `docker system df`. For scale, the 10 recordings on the development
  workstation take 43 MB (`du -sh evaluation/recordings`, 2026-11-06).
- [ ] **Cluster healthy:**
  1. `.\infra\scripts\smoke.ps1` passes every check;
  2. `.\infra\scripts\check-resources.ps1` reports within the 4 GB budget;
  3. staging is `Healthy` with its stable ReplicaSet serving. `pilot.py` heals it once with
     load before each scenario (`pilot.healthy`).
- [ ] **No scored results in Prometheus,** and `evaluation\results_exporter.py` stopped. The
  runner refuses a cluster run while any `flowguard_result` series exists (`runner.run`).
- [ ] **Laptop power:** sleep disabled for the run. A sleep invalidates the running scenario
  (section 4).

## 2. Run order

Counts from the pre-registration. Times are estimates, marked with their source.

| # | Block | Count | Needs | Estimated time |
|---|---|---|---|---|
| 0 | Generate the held-out set | all families, DSO5, DSO6 | eval-freeze | minutes |
| 1 | Diagnose: corpus test templates and LogChunks (D39) | 12 test variants (`families.ci.templates`); 230 LogChunks logs (D47) | LLM lanes, diagnose-freeze | part of the LLM total below |
| 2 | Release (agents-mode canary, D54) | 72 (36 bad, 36 good, a quarter under node stress) | cluster | about 13 h (pre-registration `families.release.count_rule`: 7.5 to 10 min per recording) |
| 3 | Runtime | 147 | cluster | about 22 h, my estimate: 420 s of spec timing (`families.runtime.timing`) plus the 120 s quiet wait (`runner.QUIET_S`) plus healing and recording, about 9 min each. TO CHECK on the first runs |
| 4 | Normal traffic (DSO1 false alerts) | at least 4 h (`normal_traffic.hours`) | cluster, overnight | 4 h |
| 5 | Security | 14 | cluster, CI | about 2.5 h, my estimate: app-ci about 5 min and a canary about 6 min (blueprint, Step 3) |
| 6 | Pipeline (slow CI step) | 74 | CI, no cluster | about 10 h one at a time; 2 to 2.5 h at the pre-registered 4 to 5 in parallel |
| 7 | ci, live at L5 (D64) | 12 | CI, LLM, write token, **a person at the approval page** | about 4 h attended, my estimate: CI, one L5 decision and one fix-branch CI run each (the CI wait is capped at 2,400 s, `runner.CI_TIMEOUT_S`) |
| 8 | DSO5 commit sequence, baseline and optimizer plan modes | 30 commits x 2 modes (`dso5_commit_sequence.count`) | CI, write token | about 5 h of CI at about 5 min per run. TO CHECK |
| 9 | DSO6 gate set | 300 items (`dso6_adversarial_set.count`) | offline | minutes. TO CHECK |
| 10 | L3 repeats (D61) | 30 seeded items x 3 runs at L3 | LLM lanes | the whole LLM plan: 568 calls, about 3.05M tokens, about 2.4 days on 7 lanes (D61) |
| 11 | Offline levels, scoring, report | every recording | none | hours |

Cluster time, blocks 2 to 5: about 41 h of estimate, inside 35 to 55 h. The LLM blocks (1 and 10)
run in parallel with the cluster: they need only the lanes.

### Block 0: generate

```powershell
py scenarios\heldout.py generate --confirm-step-11
```

It refuses unless:
- the pre-registration is frozen;
- `eval-freeze` exists and `agents/` is unchanged since it;
- the seed matches its hash;
- `scenarios/heldout/` holds no specs yet. **The held-out set is generated once.**

It writes the specs, `run-order.txt`, `dso5-sequence.yaml` and `dso6-items.yaml` to
`scenarios/heldout/` (gitignored), and prints only their count.

### Block 1: Diagnose on test templates and LogChunks (D39)

- **TO CHECK: no runner exists for either.**
  - `diagnose_dev.py run --splits test` passes the freeze check (`items()`), but then skips
    every test template, so it would run nothing.
  - `logchunks.py` only prepares targets and the H3 sample: "nothing is sent to a model on
    LogChunks before Step 11".
- A runner must call the frozen Diagnose at L2 and L3 with the pinned settings. It must record
  the raw quotes through `observations.hallucination`, and write its calls to the audit log so
  `llm_provenance.py` can attribute them (D64).
- Once it exists, check provenance with `py evaluation\llm_provenance.py`.

### Blocks 2 to 7: live scenarios

The pre-registration requires the seeded order in `scenarios/heldout/run-order.txt`
(`run_order`).

**TO CHECK: no driver takes held-out specs in that order.**
- `py scenarios\pilot.py scenarios --dir <folder>` runs a folder's specs in sorted file-name
  order. It skips specs that already have a valid run, and retries a run started under firing
  alerts up to 3 times (`pilot.QUIET_ATTEMPTS`).
- In its default (`l0`) mode it would also record release specs under the inline L0 analysis.
  That is against D54.
- It never passes `--ci-agents`.

A driver needs to follow `run-order.txt`, set the canary mode per family and pass `--ci-agents`.
Until it exists, the commands below are the building blocks it would call.

**Release, agents mode:**

```powershell
py scenarios\pilot.py mode agents
py scenarios\pilot.py scenarios --agents --dir <folder of release specs>
```

**Runtime and security,** with staging back on the inline L0 analysis:

```powershell
py scenarios\pilot.py mode l0
```

TO CHECK: runtime does not use the canary, so its mode may not matter. Security releases do go
through the canary.

**Normal traffic:**

```powershell
py scenarios\pilot.py normal --hours 4
```

**ci, live at L5 (D64).** One spec at a time. Approve on the printed page:

```powershell
py scenarios\runner.py --ci-agents dry-run <spec>   # first spec only: writes nothing to GitHub
py scenarios\runner.py --ci-agents live <spec>
```

- The commit families (`ci`, `pipeline`, `security`) need a clean `dev` checkout. Each run
  switches branches in it (`run_commit`).
- TO CHECK: the pre-registered "4 to 5 at a time" for ci and pipeline needs separate checkouts;
  one checkout runs one commit scenario at a time.

### Blocks 8 and 9: DSO5 and DSO6

**TO CHECK: no code reads `dso5-sequence.yaml` or `dso6-items.yaml`.**
- The dev DSO6 set is scored by `evaluation/l0_baseline.py` (L0 only).
- The DSO5 development results come from `ci/collect_optimizer.py` on existing runs
  (`docs/step-10-optimizer.md`).
- Neither replays a held-out sequence or item set.

### Block 10: L3 repeats (D61)

**TO CHECK: no runner for the held-out repeat subset.** `diagnose_dev.py --repeat 2|3` repeats
only dev items.

### Block 11: offline levels, scoring and the report

- **DSO1:** `py evaluation\monitor_replay.py score <recordings>`.
- **DSO3:** `py evaluation\release_replay.py score <recordings>`.
- **ci family, L0 to L4 on the recording of the L5 run (D64):**
  `py evaluation\ci_levels.py <recording>`.
- **Diagnose observations:** `py evaluation\observations.py state\audit.db <out.jsonl>`.
- **The report:** `py evaluation\report.py --confirmatory --out results\final` (section 3).

TO CHECK:
- Both replay scorers label their output "dev, in-sample".
- `monitor_replay.py` refuses any path containing "heldout".
- No code turns their JSON into the `replay_result` records `report.py` reads
  (`step-11-analysis.md`, "Replay observations and joins").

## 3. Checkpoints

Run after every block. Stop at the first failure.

| Check | How | Pass |
|---|---|---|
| Recording count | `py -c "import json,pathlib; m=[json.loads(p.read_text()) for p in pathlib.Path('evaluation/recordings').glob('*/manifest.json')]; print(len(m), sum(x.get('valid', True) for x in m))"` (reads manifests only, never ground truth) | valid recordings went up by the block's count; every invalid one is re-run (section 4) |
| Sources present | the same manifests' `sources`: the recorder writes `{"error": ...}` for a source that failed and still marks the run valid (`recorder.record`) | no source with an `error`. TO CHECK: whether such a run should be re-run |
| No leaked ground truth (D25) | `py -m pytest evaluation\tests\test_agent_inputs.py`; it replays every local recording through the orchestrator and scans every agent input | all pass, none skipped for "no local recordings" |
| Disk | `Get-PSDrive C`; `docker system df` | still ≥ 40 GB free |
| LLM budget per lane | `state\llm-usage-<lane>.json`: tokens per UTC day per lane, never the key. The client stops a lane at 90% of its daily limit (`flowguard/llm.py`) | each lane below its limit, or stopped and resuming the next UTC day |
| LLM provenance (D64) | `py evaluation\llm_provenance.py` | no error: every cached answer has exactly one `llm_call` level |
| Fix-branch leftovers | `py scenarios\cleanup_fixes.py` (dry run) | only this block's fix branches; `--apply` after scoring |
| **Final report** | `py evaluation\report.py --confirmatory --out results\final` | the printed summary and `results.json` metadata say `"confirmatory": true` |

**The final checkpoint.** If `confirmatory` is false, read `reason` (the first check that
failed) and stop. Do not re-label, re-run selectively or edit inputs. `report_scope.py` checks
these four, in this order:
1. the pre-registration is frozen;
2. the `eval-freeze` tag exists and `agents/` is unchanged since it;
3. `run-order.txt` exists and the seed in `.env` matches `scenarios/heldout-seed.sha256`;
4. every scenario id scored in the truth database is in the run order: no dev, synthetic or
   unknown id.

`report.py` also blocks on:
- an H4 amendment that is pending;
- any pre-registered count left unset.

With `--confirmatory`, a failed check refuses the report outright (`confirmatory output
refused: …`). No flag can make a report confirmatory.

## 4. Failure recovery

The rule, from the pre-registration (`exclusions`): a run recorded with `valid: false` is
re-run with the same spec and seed, never dropped silently. The report gives the number of
re-runs.

| Failure | What the code does | Safe to resume? | Action |
|---|---|---|---|
| Laptop sleep | The load log shows a gap over 60 s, so the runner sets `host_suspended` and records the run with `valid: false` (`runner.suspend_gaps`) | Yes | Rerun `pilot.py`: it skips only specs with a valid run (`pilot.done`), so the invalid one runs again |
| Alerts not quiet before a fault | No fault is injected; the run is recorded with `valid: false`; `pilot.py` retries up to 3 times (D50) | Yes | None; after 3 failures, check what keeps firing |
| Canary not at 100% within 15 min (agents mode) | `canary_incomplete`, recorded with `valid: false` (D54) | Yes | Check staging, then rerun |
| Cluster crash mid-scenario | An exception leaves `Runner._run` before `recorder.record`, so no recording and no truth row are written for the spec; `pilot.py` logs `failed` and continues. Cleanup runs in `finally` blocks (restore, stress pods, load, the scenario branch) | Yes, after the cluster is back | `setup.ps1`, `smoke.ps1`, then rerun `pilot.py`. TO CHECK: cleanup when the cluster itself is gone (`kubectl` fails inside `finally`) |
| Docker disk full | Not handled. Prometheus or the cluster stops; the recorder writes an `error` for each source it cannot read and may still mark the run valid | **No** | Discard every recording since the last checkpoint whose sources show errors, free disk, and re-run those specs. TO CHECK: the recorder does not invalidate such runs itself |
| Groq rate limit or quota | The client retries 429 after `retry-after` and stops a lane at 90% of its daily limit; finished calls are cached and never re-sent, so a rerun continues (D27, `flowguard/llm.py`) | Yes | Rerun the same command after the UTC reset |
| A lane goes down | A key Groq rejects (401) is dropped at the next start (`lanes.candidates` and `independent`); `diagnose_dev.py` prints the changed lane count | Yes, with care | Note the lane change for the report (D41). TO CHECK: items are assigned by a seeded hash over the current lane list (`lanes.assign`), so a smaller list may move an unstarted item to another lane |
| Write token expired | Live pins and dispatches fail and are recorded as failed executions | **No** for blocks 7 and 8 | Renew before 2026-11-28 21:08 UTC (D63); a pin that failed this way is a failed execution, reported as is |
| An agent or LLM failure in the live ci block | Recorded in the runner's `observed.agents`; the L0 recording is still made (`runner.run_commit`) | Yes | Report it; do not re-run to get a different answer |

Never re-run a valid run because its result looks wrong. That would break the freeze rule.

## 5. Day-by-day plan

The plan assumes eval-freeze can be tagged early. The cluster runs unattended overnight on the
blocks that need no person. The write token's expiry (2026-11-28) leaves about three weeks; the
plan needs about six days plus margin. The TO CHECK runners above must exist before their day.

| Day | Daytime (attended) | Overnight (unattended) |
|---|---|---|
| 1 | Preconditions (section 1); tag eval-freeze; block 0 (generate); start the LLM blocks 1 and 10 on the lanes; block 7 (ci live at L5, 12 specs, approvals at the page; first spec in dry run) | Block 2: release, agents mode (about 13 h) |
| 2 | Checkpoints for days 1 and 2; switch to `l0` mode; block 5 (security, 14) | Block 3: runtime, first part |
| 3 | Block 3 continues (runtime totals about 22 h); block 6 (pipeline, 74) on CI while the cluster runs runtime | Block 3: runtime, rest |
| 4 | Block 8 (DSO5, 30 x 2 on CI); block 9 (DSO6, offline) | Block 4: normal traffic, 4 h; LLM blocks finishing (about 2.4 days from day 1) |
| 5 | Re-runs of every invalid run (section 4); full checkpoint pass; `cleanup_fixes.py --apply` | Spare |
| 6 | Block 11: offline levels, observations, report. Final checkpoint: `confirmatory: true` | |

Spare days before 2026-11-28 cover re-runs and a lost lane day. If the cluster falls behind,
move runtime overnights forward; never shorten a pre-registered timing or count.
