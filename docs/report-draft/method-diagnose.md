# Method: Diagnose (Step 7, DSO2)

Draft for the report. Method only: no held-out result appears here. Development numbers
appear only where they are a calibration input or the reason for a design choice, and they are
labelled "dev". Decision numbers (Dn) refer to the decision log in `docs/blueprint-v2.md`.

## What it does

When a CI run fails, the Monitor opens the incident (D49). Diagnose then reads the failing
job's log and does three things:
1. it classifies the root cause into one of five classes: code, dependency, environment,
   flaky test or infrastructure;
2. it proposes one fix action from its allowed set: `retry_job`, `rerun_workflow`,
   `restart_deployment`, `pin_dependency`, `open_fix_pr` or `escalate_to_human`;
3. it posts its evidence to the incident record.

The gate, not Diagnose, decides whether the action runs. An action outside the allowed set
becomes `escalate_to_human`, and the change is flagged.

Diagnose sees only the log. It never sees the class, the template, the commit message or the
branch name (D25). The CI event's run JSON is cut to an allow-list (run id, states,
conclusions, timings, head commit). A branch that is not in the runner's opaque form reaches
it as a fixed placeholder.

## The failure corpus (D6, D34)

- **Templates:** 31 scripted CI failure templates, 6 or 7 per class. Each is applied to the
  demo application in several variants: different files, functions, modules, versions or
  hosts.
- **Runs:** each variant runs locally in app-ci's job order until the first failure. The
  failing job's log is kept.
- **Split by template, never by run:** 20 training templates, 5 for validation and 5 for test.
  No variant of a test template is ever seen in development (`corpus-split.json`; blueprint
  section 7). The 12 test variants are the pre-registered `ci` family.
- **Real logs:** LogChunks serves for evidence localization only (D32).

## Log reduction

Every level reads the same reduced log (`logreduce.py`):
1. cut at GitHub Actions' teardown;
2. keep the terminal view of each line, the text after its last carriage return, without ANSI
   codes, at most 300 characters;
3. group lines into drain3 templates and keep the first 2 lines of each;
4. keep the last 200 lines;
5. drop lines from the front until the text fits 14,000 characters.

Every kept line keeps its original line number, so evidence can be checked against the real
log. The 14,000-character budget keeps one request under the free tier's 8,000 tokens per minute
(D51).

## Levels

| Level | Method | Trained on |
|---|---|---|
| L0 | Regex rules per class in a fixed order (infrastructure, environment, dependency, flaky test, code) on the last 200 lines; no match is "unclear"; a fixed class-to-action playbook (D3). Written from the class definitions before any corpus log was scored | Nothing |
| L1 | TF-IDF and logistic regression on the reduced log's text, seeded (`classical.py`) | The 20 training templates |
| L2 | The pinned LLM, zero-shot: class definitions, allowed actions and a fixed JSON schema (class, a score per class, action, a one-sentence fix, up to 5 quoted lines) | Nothing |
| L3 | L2 plus up to 3 retrieved training cases and evidence grounding (D8) | Retrieval index of the training and validation cases |
| L4, L5 | L3's diagnosis. They reuse L3's cached answer for an identical input, so no extra LLM call (D39, D64) | |

A fine-tuned small encoder is compared as a fifth method, at L1's place in the ladder (D7):
- trained on a free GPU notebook, with CPU inference only;
- its final run and selection rule declared before it ran (`docs/d7-encoder.md`).

**LLM settings (D27, D41, D51):**
- Model `openai/gpt-oss-120b`, temperature 0, reasoning effort low, pinned and logged per call.
- Responses are cached by a hash of model, settings and prompt, and never re-sent.
- Each of L3's examples is cut to 1,000 characters and the answer to 600 tokens.
- Calls run on parallel lanes, one per team member's own free Groq account, with identical
  settings. Items are assigned to lanes by a seeded hash, all of an item's calls run on one
  lane, and keys are never rotated.

**Retrieval (L3).**
- A Chroma index holds one document per training or validation variant: the end of its reduced
  log, with its class and reference action.
- Embeddings come from all-MiniLM-L6-v2 as chromadb pins it, on the CPU.
- **Own-template exclusion (D45):** the nearest cases never include the query's own template.
  A development case is therefore diagnosed from other templates only, as a test case always
  is.

## Evidence grounding and the hallucination rate (D8)

- **A quote is found** if it is a whole log line, or part of one when it has at least 8
  characters (whitespace collapsed).
- **From L3 on, grounding decides:** an answer with no quote, or with any quote not found, is
  rejected. Its class becomes "unclear" and its action `escalate_to_human`.
- **At L2, grounding is measured but not applied.**
- **The raw counts are recorded the same way at L2 and L3:** the model's raw quotes and how
  many were not found, so the hallucination rate means the same at both levels.
- **The rate** (H3 secondary) is the sum of ungrounded quotes over the sum of quotes.
  - Its interval is a percentile bootstrap that resamples whole answers, because quotes from
    one answer are not independent.
  - The per-answer boolean (any ungrounded quote) is used only for the paired exact McNemar
    test between L2 and L3.

## No log (D60)

When the event carries no log, as with runtime incidents, every level takes one deterministic
path:
- no model is asked and no class is guessed;
- the record says "no diagnosis (no log)";
- the only proposal is `escalate_to_human`, without label scores.

## Calibration (D9, D45)

- **Where confidence comes from:** the gate reads a split-conformal confidence. It is 1 minus
  the second-largest label p-value, and 0 for an input unlike the calibration data.
- **The calibration set:** leave-one-template-out over the 25 training and validation templates.
  Each template's scores come from a model that never saw it:
  - L1 is refit without it;
  - L3's retrieval excludes its cases;
  - L2 has no training.
- **Validation-only fitting was dropped:** its 14 examples are under the minimum of 20, so every
  confidence would have been 0.
- **Calibration inputs (dev):** coverage at a 90% target and mean prediction set size.

  | Level | Coverage | Mean set size |
  |---|---|---|
  | L1 | 67/74 | 2.54 |
  | L2 | 67/74 | 3.35 |
  | L3 | 72/74 | 3.15 |

  Source: pre-registration `calibration.dev_coverage`.

## Fixes (D57, D62, D64)

- **`pin_dependency`:** "set module X to version Y" in `go.mod`.
  - It is proposed only for a dependency diagnosis whose quoted evidence names X at a version
    go cannot use.
  - Y is resolved after the diagnosis, read-only. It is X's version in `go.mod` at the last
    commit whose app-ci passed. It is never shown to Diagnose, so a `go.mod` diff cannot reveal
    the class to any level.
  - If no pin can be planned, the diagnosis goes out as `open_fix_pr` instead.
- **`open_fix_pr`:** opens an issue, or a draft PR when it carries a pin patch. It is counted as
  assisted, never as resolved without a human.
- **Validating a pin:** it is committed on its own fix branch, and app-ci runs there.
  - It counts as resolved without a human only if that run succeeds; a missing or failed run
    leaves it unresolved.
  - Everything the fix handlers create is marked for clean-up.
- **The live ci family (D64):** each scenario runs the full system once at L5 after its CI
  fails. L0 to L4 are compared offline on the same logs.

## Staged freeze (D39)

Once the pre-registration was frozen and Step 7 was done, the Diagnose code was tagged
`diagnose-freeze`. The SHA-256 of its untracked artifacts was committed with it: the L1 model,
the three calibrations, the index manifest and the encoder weights.

Its runs on the test templates and on LogChunks need that tag, with the code and artifacts
unchanged. The tag let them start while other agents were still being built: the LLM quota,
not the code, sets the schedule.
