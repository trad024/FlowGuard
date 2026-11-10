# Step 11 run summary: incidents and their effect (for the Threats section)

Source: the operator run log `state/run-log-step11.md` (entry numbers below; the log is kept on
laptop A, outside the repository), the decision log in `docs/blueprint-v2.md` and
`docs/step-11-results.md`. Dates are local time (UTC+1). No run, spec or scenario id, seed, run
order or key appears here. "Effect" says what each incident changes in the results, and where
nothing changes, why.

## Overview

| # | Date | Incident | Effect on the results | Recorded in |
|---|---|---|---|---|
| 1 | 11-08 | WinNAT reserved the cluster's host ports | none (fixed before any held-out run) | log 1, 5 |
| 2 | 11-08 | ci generator bug (D72) | none on results; the ci family is one run per registered variant | log 2, 3; D72; pre-registration 1.6 |
| 3 | 11-08 | laptop A restart killed the first normal-traffic window | none (window re-run in full) | log 5, 7, 15 |
| 4 | 11-08 | three-laptop split (D75) | per-laptop variation possible; reported as exploratory | log 6, 11, 12, 14, 23; D75 |
| 5 | 11-08 | first DSO5 run stopped by a harness bug (D76) | first run set aside; DSO5 re-run under D76 | log 8, 9, 10; D76; pre-registration 1.8 |
| 6 | 11-09 | operator saw one spec's fault details at share A start-up | blinding breach of one spec's inputs, no outcome seen | log 16 |
| 7 | 11-09 | HP SystemOptimizer crash loop on laptop A | no effect found; its tasks disabled during Step 11, re-enabled after | log 21, 22, 54, 55 |
| 8 | 11-09 | laptop C launch done through a helper | none; launch command and guard confirmed from C's transcript (reported by the laptop's operator, not verified from A) | log 23, 53 |
| 9 | 11-09 | laptop B: Modern Standby 03:00–03:15 on battery, driver ran on without Prometheus, Docker engine failure | one spec recorded across the freeze (invalid, re-run); later specs failed before recording and ran after recovery (reported by the laptop's operator, not verified from A; B's 2 invalid manifests: Prometheus timeouts, and no listed reason) | log 33, 38, 52, 56 |
| 10 | 11-09 | Diagnose lanes hit errors near the daily budget; L3 split over two days | 3 items retried next day; no item lost | log 26, 28, 34, 35 |
| 11 | 11-09 | DSO5 re-run stopped on a failed git push, resumed | the Optimizer mode ran in two time windows | log 29, 30, 31 |
| 12 | 11-09 | laptop C's results sent through WeTransfer unencrypted | confidentiality not guaranteed; integrity checked by hash | log 32 |
| 13 | 11-10 | the 01:05 scheduled resume did not run | none (started by hand after a passing dry run) | log 34, 37 |
| 14 | 11-10 | slow L3 and repeat calls; one repeat-2 item failed | one item retried; all repeats complete | log 35, 36, 37 |
| 15 | 11-10 | provenance check failed on 6 development cache files | none on held-out results; development provenance gap | log 37, 38, 39 |
| 16 | 11-10 | 5 Optimizer-mode pipelines failed to start (DSO5) | primary DSO5 metrics on 25 of 30 commits (post-hoc exclusion) plus sensitivity | log 40, 41; D78 |
| 17 | 11-10 | ci_levels stopped: wrong order and no key (D79), lane (D80) | none measured; inputs rebuilt | log 43–47; D79, D80 |
| 18 | 11-10 | report.py refused ambiguous repeat records (D81) | none (an empty proposal list added) | log 47, 48; D81 |
| 19 | 11-10 | 3 ci recordings captured while their run was queued | those 3 ci incidents have no L0–L4 decision | log 46, 48 |
| 20 | 11-10 | post-unblinding gaps in the toolkit (n = 0 metrics) | exploratory tables (D82), labelled so | log 49–51; D82 |

## Incidents in date order

### 1. WinNAT reserved the cluster's host ports (2026-11-08)

- **What happened.** After a laptop restart, Docker Desktop was down. Once it started, the k3d server could not publish host ports 9898 and 9899: Windows had reserved the dynamic TCP range 9850–9949 (WinNAT). The fix was to stop Docker and WSL, reserve the cluster's six host ports as administered exclusions, restart WinNAT, Docker and the containers. The exclusions survived the next reboot (entry 5).
- **Effect.** None. It happened before any held-out run. Smoke 9/9, resources 1,331 MiB of 4,096, both Rollouts Healthy.
- **Recorded.** Log entries 1 and 5.

### 2. ci generator bug, D72 (2026-11-08)

- **What happened.** The first `heldout.py generate` stopped at spec validation and wrote nothing. 7 of the 12 pre-registered ci variants were unknown to the validator, and the ci draw was random instead of one spec per variant. The fix was in `scenarios/` only, under a new harness tag, `eval-freeze-d72`. Generate then produced 319 specs, as pre-registered.
- **Effect.** No result existed yet (D65), so nothing was re-drawn after a result was seen. The ci family is one run per registered variant (12), with the registered kind mix. `families.ci.kinds` does not apply to ci, a dated amendment (pre-registration 1.6).
- **Recorded.** Log 2 and 3; decision D72; pre-registration amendment D72.

### 3. Laptop A restart killed the first normal-traffic window (2026-11-08)

- **What happened.** Laptop A restarted about an hour into the 4-hour normal-traffic window. The window recorded nothing.
- **Effect.** None on the results. The window was run again in full (20:26 to 00:27) and its recording is valid. During part of it, DSO5's short `go mod download` container shared the laptop (stated in log 7).
- **Recorded.** Log 5, 7 and 15.

### 4. Three-laptop split, D75 (2026-11-08)

- **What happened.** The run order was split by a fixed rule, before any recording:
  - laptop A ran every ci, pipeline and security spec;
  - release and runtime specs alternated between laptops B and C.

  B and C ran the harness at `eval-freeze-d75`, A at `eval-freeze-d76`. The two differ only in DSO5 code, its tests and the pre-registration version; no runner, recorder, agent or cluster file differs. The laptops were matched on 8 CPUs, the same Docker memory limit and the same Python stack. Parity was checked:
  - node allocatable memory differs by -604 Ki (B) and +8 Ki (C);
  - WSL is 2.7.10 on B and 2.5.10 on C vs 2.6.1 on A (B and C as reported);
  - Docker Engine is 29.9.2 on B vs 29.8.2 on A.

  The node-stress image (alpine:3.24) was pulled with crictl on B and C, with the same image digest on both.
- **Effect.** Release and runtime results come from two machines. The exploratory per-laptop breakdown (results, section 6, table h) shows runtime L1 recall 73/74 on B and 64/73 on C. That difference is untested and its intervals overlap.
- **Recorded.** Log 6, 11, 12, 14 and 23; decision D75; pre-registration amendment D75.

### 5. First DSO5 run stopped by a harness bug, D76 (2026-11-08)

- **What happened.** The first held-out DSO5 run stopped after 14 baseline commits. The runner stacked every commit on one branch per mode, so a failing-commit template could no longer find the line it edits. The run log first named the wrong cause and corrected it (entry 9).
- **Fix (D76).** Each commit is applied to the sequence's base tree and restored afterwards with a `[skip ci]` commit, which was verified to start no workflow. Committed and tagged `eval-freeze-d76`.
- **Effect.**
  - The 14-row first run is set aside and never scored.
  - DSO5 was re-run in full under D76.
  - Stated limitations: the Optimizer's "previous scan" refers to the previous measured commit, whose tree was restored; the first commit has no previous run.
- **Recorded.** Log 8, 9 and 10; decision D76; pre-registration amendment D76 (version 1.8).

### 6. One spec's fault details seen at share A start-up (2026-11-09)

- **What happened.** To confirm share A had started, the operator printed the driver's first 3 output lines. They showed the first spec's family and its fault details: kind, delivery, added time, and the injected commit's file and message. No score, result or ground truth was shown. From then on the watcher printed only counts and keywords.
- **Effect.** A blinding breach limited to one spec's inputs. The operator did not change, select or re-run anything because of it, and no outcome was visible. The spec was scored like every other.
- **Recorded.** Log 16.

### 7. HP SystemOptimizer crash loop on laptop A (2026-11-09)

- **What happened.** HP SystemOptimizer crashed every 1–4 minutes during share A, leaving 26 crashed processes. The team lead disabled its three scheduled tasks at 02:24. The 22 leftover processes could not be killed and held about 1 MB in all. An hourly check watched for new ones and for free RAM under 1 GB.
- **Effect.** No effect on a run was found. Share A finished 100/100 valid with empty stderr. A phone-alert helper died once near a crash, but the link was not established and no alert was pending. The three tasks were re-enabled after Step 11 by the team lead in an administrator shell and verified Ready on 2026-11-10 (log 54, 55).
- **Recorded.** Log 21 and 22.

### 8. Laptop C launch (2026-11-09)

- **What happened.** Share C was set up and started on laptop C by its operator with a helper
  tool. Setup facts as reported (log 23): WSL 2.5.10.0, 8 CPUs, pip freeze identical to A's,
  parity passed, the same node-stress image.
- **Confirmed later from C's PowerShell transcript (reported by the laptop's operator, not verified from A):**
  - HEAD at `eval-freeze-d75`;
  - launched at 02:13:22 with `py -3.13 -u scenarios\pilot.py heldout --share C --confirm-step-11`;
  - the driver's own `require_heldout` guard (frozen agents, frozen Diagnose, committed seed) must
    pass before the driver records anything, and the driver started and recorded;
  - all 109 manifests carry the `eval-freeze-d75` harness commit.
- **Effect.** None. C ran the frozen harness. Its 109 run-order ids are each valid exactly once
  in the right mode (log 38). Closed.
- **Recorded.** Log 23, 38 and 53.

### 9. Laptop B: Modern Standby, a broken WSL link, recovery and resume (2026-11-09)

All of this is reported by the laptop's operator, not verified from A (B's operator, 2026-11-09 about 08:55).

- **What happened.**
  - Share B started at 00:03:01. At 02:56 the charger was unplugged. On battery the laptop
    entered Modern Standby from 03:00:43 to 03:15:44: network disconnected, WSL VM frozen.
  - The spec running at that moment was recorded across the freeze, flagged `host_suspended` and
    marked invalid, as designed.
  - After resume, the WSL vsock link to Windows stayed broken and `localhost:9090` (Prometheus)
    stopped answering. Each following spec failed its 30 s `results_in_prometheus` check
    **before** taking the run lock: no recording folder was written and the cluster was not
    changed. The driver went through the rest of its list this way and exited 0 at 04:01:53.
  - Docker Desktop's WSL engine failed at 05:04.
- **Recovery.**
  - Power set to never sleep on AC and on battery; Docker and WSL restarted.
  - `docker info` showed 8 CPUs and 10,296,619,008 bytes; all pods Running, both Rollouts
    Healthy, the node-stress image present.
  - `smoke.ps1` 9/9; `check-resources.ps1` 1,547 of 4,096 MiB; parity matching A apart from the
    accepted memory and per-cluster lines.
  - Share B resumed with its start command at 05:41:34 and finished at 19:32:03. The driver skips
    recorded specs, so the specs that failed before recording ran after the recovery.
- **Also reported.**
  - Two short standbys under the 60 s threshold: 01:47 for 47 s (on AC, screen off) and 02:56 for
    27 s. Runs spanning them are valid under the pre-registered rule.
  - During the night B's operator closed Edge and stopped the Oracle XE services (memory committed
    27.4 of 29.1 GB).
  - B's WSL is 2.7.10.0 (A 2.6.1.0, C 2.5.10.0).
- **Effect.**
  - No invalid run is scored. B holds 112 runs, 2 invalid and both re-run valid (log 38); the
    report's coverage counts 2 invalid truth runs. Only the `invalid_reasons` field of B's 2
    invalid manifests was read on A (log 56):
    - one lists Prometheus timeouts: the first metric query timed out, and the other 12 were
      not queried because Prometheus was unreachable;
    - the other has an empty `invalid_reasons` list.

    Neither says `host_suspended`. Which of the two is the run recorded across the freeze, and
    why one is invalid with no listed reason, cannot be told from that field alone. Both runs are
    excluded from scoring either way.
  - The specs that failed before recording left no data and ran later, in a different time
    window and after a Docker and WSL restart.
  - Runs that spanned the two short standbys are kept under the pre-registered rule.
  - Limitation (harness): when Prometheus is unreachable, the driver records a failure and moves
    on instead of stopping. That is why it ran through the rest of its list in about 45 minutes.
  - Laptop B ran with background load (Edge, Oracle XE) until the operator stopped it. Its memory
    commit was high; the cluster stayed within its Docker limit.
- **Recorded.** Log 33, 38, 52 and 56.

### 10. Diagnose lanes near the daily budget; L3 over two days (2026-11-09/10)

- **What happened.** As the provider lanes approached the 180,000-token daily stop, 3 L3 calls failed on two lanes with an HTTP error. The status is not logged; the cause is inferred to be rate or budget, not a bad key or an oversized request. Lanes then stopped on their budgets with L3 at 198 of 242. The repeats were held back that night, on the team lead's rule.
- **Effect.** No item was lost. Failed calls write no record and no cache entry, so the 3 items ran again after the reset. L3 completed 242/242 the next night, with 0 failures in that session. The L3 calls are split over two UTC days, with the same model and settings.
- **Recorded.** Log 26, 27, 28, 34 and 35.

### 11. DSO5 re-run stopped on a failed git push (2026-11-09)

- **What happened.** The D76 re-run stopped at the 8th Optimizer commit: `git push` failed, and the harness does not capture git's own message. A push dry run showed that authentication worked and nothing had been rejected. The run was resumed on the team lead's go and finished 30/30 per mode.
- **Effect.** The Optimizer mode ran in two time windows (commits 1–7, then 8–30), while the baseline mode ran in one. GitHub-hosted runner load can differ between windows, which can shift pipeline time and time to first failure. This is a stated threat to validity. The cause of the failed push is unknown, a stated limitation.
- **Recorded.** Log 29, 30 and 31.

### 12. Laptop C's results sent through WeTransfer unencrypted (2026-11-09)

- **What happened.** Share C's results reached A as a plain, unencrypted zip through WeTransfer, after the runs had ended. They were extracted outside the repository without being opened.
  - Counts matched C's: 109 recording folders, 2,071 recording files, 116 run files.
  - Both databases' SHA-256 matched C's on the first 12 characters.
- **Effect.** Integrity of the two databases is supported by the hash match; the other files were checked by count only. Confidentiality during transfer is not guaranteed, a stated threat. Share B was sent AES-256 encrypted with hidden file names and a full-hash match, so this does not apply to B.
- **Recorded.** Log 32 and 33.

### 13. Scheduled 01:05 resume did not run (2026-11-10)

- **What happened.** The one-shot scheduled task fired at 01:05, but its script did not run (Task Scheduler result 0xFFFD0000). The same script passed a dry run from the operator session and was started by hand at 01:05:44. The task was deleted afterwards.
- **Effect.** None. L3 resumed 44 seconds later than planned, with the same arguments.
- **Recorded.** Log 34 and 37.

### 14. Slow L3 and repeat calls; one repeat-2 item failed (2026-11-10)

- **What happened.** From about 01:43 to 04:21, and again from about 04:50 to 07:00, calls slowed to about one item every 40 minutes. The process was idle, consistent with waiting on rate-limit retry delays (not confirmed). One repeat-2 call failed and was retried on the team lead's go: 1 call, 0 failed.
- **Effect.** All held-out Diagnose outputs are complete: L2 242, L3 242, repeats 30 + 30. Latency is not a reported measure (n = 0), so the delays do not bias any result.
- **Recorded.** Log 35, 36 and 37.

### 15. Provenance check failed on 6 development cache files (2026-11-10)

- **What happened.** `llm_provenance.py` failed on 6 cache files that have no audit event. All 6 were written on 2026-11-07 by development work, before the freeze, through the default client, and no held-out record uses them.
  - Restricted to held-out keys, by a read-only operator script applying the same rule plus a Step 11 date check: **pass**. That covers 554 keys, each with `llm_call` events from exactly one level, all from 2026-11-08 on.
  - One key is shared by two held-out Diagnose items with byte-identical requests: the duplicate held-out log, D74.
- **Effect.** None on held-out results. Development provenance for 2026-11-07 cannot be reconstructed, a stated limitation. The 6 files and the frozen tool were left unchanged.
- **Recorded.** Log 37, 38 and 39; D74.

### 16. Five Optimizer-mode pipelines failed to start, DSO5 (2026-11-10, found while reading DSO5)

- **What happened.** In 5 of 30 commits, the Optimizer-mode app-ci run concluded failure in about 50–56 s with no job, check run or annotation. The workflow file was identical to the base, and GitHub shows no cause. All 5 baseline counterparts ran 6–9 jobs.
- **Effect.**
  - DSO5 reports a reliability line: Optimizer 5/30 failed to start, baseline 0/30.
  - Primary metrics use the other 25 commits, a post-hoc exclusion labelled as such and set before any score.
  - A sensitivity block counts the 5 against the Optimizer: failures missed 5 of 21 vs 1 of 17.
  - Of the 5, 4 had a failing baseline run.
- **Recorded.** Log 40, 41 and 49; decision D78; results, section 1.

### 17. ci_levels: wrong order and no key (D79); lane (D80) (2026-11-10, after unblinding)

- **What happened.** Block 11 stopped at `ci_levels.py` (KeyError: no `LLM_API_KEY` in the process). This exposed an ordering slip from the D77 runbook: A's held-out audit copy was built before ci_levels writes its proposals.
  - **D79:** run ci_levels first, with the key loaded into that process only, then rebuild A's inputs (`heldout-A-v2`).
  - **D80:** LLM_API_KEY's remaining budget could not fit about 12 calls, so the 9 new calls (64,107 tokens) ran on the GROQ_KEY_1 account. Its ledger was updated by hand.
- **Effect.** No measured value changes. Same model and settings; the cache key holds no lane. The runbook is corrected.
- **Recorded.** Log 43–47; decisions D79 and D80.

### 18. report.py refused ambiguous repeat records, D81 (2026-11-10)

- **What happened.** The frozen report refused to join the 90 Diagnose L3 repeat records, because they had no `proposal_ids`. Those items have no proposal.
- **Effect.** None on values. `report-inputs-v2` adds an empty `proposal_ids` list to those 90 records only; the audit and truth databases are byte-identical. report.py then ran with confirmatory true.
- **Recorded.** Log 47 and 48; decision D81.

### 19. Three ci recordings captured while their run was queued (2026-11-10)

- **What happened.** 3 of the 12 ci recordings were captured before their GitHub run finished: status queued, no failed job. The offline levels L0–L4 therefore saw no CI event and made no proposal. The live L5 run proposed `retry_job`, which executed but was never validated.
- **Effect.**
  - These 3 incidents have no L0–L4 decision; ci level comparisons rest on 9.
  - They are among the 21 truth runs without a replay record.
  - In H5 they are "executed, not validated" and never count as resolved.
- **Recorded.** Log 46 and 48; results, limitation B3.

### 20. Post-unblinding toolkit gaps (2026-11-10)

- **What happened.** The independent review found pre-registered metrics that the frozen toolkit reports with n = 0 or pools, although the inputs measure them: H4, H5, DSO4 and per-family MTTD.
- **Effect.** `results/final` is unchanged. The measurable answers are computed by `evaluation/exploratory_heldout.py` (D82), labelled exploratory, and checked by a second independent reviewer (no numeric mismatch). The frozen encoder was not run on held-out items, because no existing command does so.
- **Recorded.** Log 49–51; decision D82; `docs/step-11-results.md`, sections 2–6.

## Open items

1. Laptop B's standby (item 9): closed from B's operator report (log 52).
2. Laptop C's launch (item 8): closed from C's transcript (log 53).
3. HP SystemOptimizer scheduled tasks on laptop A (item 7): closed, re-enabled and verified Ready (log 55).
