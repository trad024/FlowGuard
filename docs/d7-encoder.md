# D7: final pre-declared CPU encoder comparison

Two runs were completed, with no further runs after the architect's pre-declared run 2.
Its plan was committed and pushed as `fe0496e` before execution. Both used the same seed 31,
base revision, split checksum, exact log index, reduction, tokenizer, AdamW settings,
512-token limit, batch size two and two CPU threads. Embeddings remain frozen in both.

| Run | Unfrozen blocks | Max epochs | Class-weighted loss | Patience | Best epoch | Macro-F1 | Accuracy |
|---|---:|---:|---|---:|---:|---:|---:|
| 1 | 2 | 3 | No | Disabled | 3 | 0.2533 | 5/14 (35.7%) |
| 2 (selected) | 6 | 10 | N/(5*n_class), training labels only | 3 | 7 | 0.7200 | 10/14 (71.4%) |

Run 2 stopped at epoch 10 after three non-improving epochs. Selection follows the declared
higher-validation-macro-F1 rule; ties would retain run 1. Both runs' full logs, predictions
and benchmarks are under `ci/encoder/results/run-1` and `run-2`; root result files mirror
the selected run. Run 2 used 43,121,669 trainable parameters. Training took 1050.49 s versus
155.94 s for run 1, excluding base loading/download. Peak training process working set was
1640.41 MiB versus 1137.24 MiB. Weighted and unweighted training losses are different
objectives and should not be compared directly across runs.

| Class | Validation support | Run 1 F1 | Run 2 F1 |
|---|---:|---:|---:|
| code | 2 | 0.0000 | 1.0000 |
| dependency | 3 | 0.6000 | 0.6000 |
| environment | 4 | 0.0000 | 0.0000 |
| flaky_test | 2 | 0.6667 | 1.0000 |
| infrastructure | 3 | 0.0000 | 1.0000 |

The selected model remains below macro-F1 0.75 and accuracy 0.80, and misses the environment
class. Twenty training templates is a small set: its 60 variants are not independent
templates, and validation has only five templates/14 variants. This is the final experimental
comparison checkpoint, not evidence of reliable production diagnosis. No held-out logs used.

Hardware: i5-11300H, four cores/eight logical processors, 7.7 GiB usable RAM; two CPU threads.
Selected run CPU inference after five warmups:

| Workload | Calls | Median | p95 | Sampled peak process RSS |
|---|---:|---:|---:|---:|
| Short-log smoke | 30 | 38.06 ms | 49.60 ms | 517.72 MiB |
| 14 validation logs, three cycles | 42 | 385.97 ms | 437.88 ms | 578.22 MiB |

RSS is sampled every 10 ms and includes checkpoint loading. It is one process, not the
whole FlowGuard/Docker budget. Full per-call measurements and the run-1 comparison remain
committed. The small workload does not establish production performance.

Weights remain outside git on the
[personal-fork release](https://github.com/aziz696902/FlowGuard/releases/tag/encoder-d7-cpu-final-run2).
`ci/encoder/artifact.json` pins the actual URL, 247,316,554-byte size and SHA-256:

```text
c9e2930e587eddabd0bff79d82c7e9348e76f5173f03078d483a4719b75396db
```

The source is the personal fork `aziz696902/FlowGuard`. Project owner `trad024` may re-host
the **identical ZIP**, retaining exactly the same SHA-256. Run 1's original release remains
available with SHA-256 `470d1b0fb40a106d1567e80c747329bf171bcbd45aac6409a77b93d6f90b964d`.

## Split and data boundary

Training used the pinned split on the earlier `be5e4b2` development base. The delivery
branch is updated onto `dev` commit `72935a1` (draft pre-registration 0.5); the recorded
train/validation split and its checksum are unchanged. The CI template assignment is populated.
`evaluation/datasets/corpus-split.json` assigns 20 training templates and 5 validation
templates. Excluding its explicitly unverified variants leaves 60 training and 14 validation
variants, with every root-cause class represented. No test-template contents were opened.

The canonical JSON SHA-256 of that public split record is pinned in
`ci/encoder/base-model.json`:

```text
a1003ffa6cfefe6f80588ce22f7cbac3231e4cf070580c893b74dcb9ccaa2beb
```

This is a **split-record checksum**, not a trained-weight checksum. The training script
refuses a changed split, unknown or duplicate variants, test assignments and unverified
variants. It validates the entire metadata index before opening any log. Paths must identify
the assigned variant, remain within the declared log root and not redirect through symlinks.
Labels come from the recorded split metadata, never from filenames or user-supplied labels.
No agent code imports or references experiment-data directories.

Training requires an export of only the verified train/validation reduced logs.
The supplied `flowguard-corpus-dev.zip` was validated and imported locally; raw logs remain
outside git. Its SHA-256 is recorded in `results/corpus-provenance.json`. The index is JSON Lines containing metadata only:

```json
{"variant_id": "code-02", "log_path": "code-02/reduced.log"}
```

Alternatively `code-02.log` is accepted. Each log must correspond to its recorded variant;
the loader also records its SHA-256 for training provenance. No inline logs or label fields
are accepted in the index. Both training and validation must contain all five classes. The index must include every
verified train/validation variant, not an arbitrary subset.

**Twenty training templates is a small set.** Its 60 variants share templates; they are not
60 independent templates. Validation contains only five templates and 14 logs, with two to
four examples per class. Any eventual F1 will need that limitation stated plainly.

On the machine holding `evaluation/runs/corpus/`, export without enumerating or opening
excluded directories:

```powershell
python ci/export_encoder.py --corpus evaluation/runs/corpus --out state/encoder-corpus
```

The exporter validates the recorded split checksum, reads only allowlisted metadata, checks
`verified`, variant/template/class and the failed-job log, then applies the same encoder
preprocessing used at inference. It validates the completed export with the strict loader.
It writes `index.jsonl`, reduced logs, source/reduced-log checksums and metadata checksums,
and `state/encoder-corpus.zip` with an actual printed SHA-256. It neither opens excluded
metadata/logs nor trains a model or uploads anything. Transfer that **train/validation-only**
export to this machine to unblock training. Synthetic tests verify the exact ZIP membership
and that excluded directories are never opened.

## Predictor and calibration

`EncoderPredictor.predict(log, incident_id)` returns the existing `DiagnosisReport`.
`predict_with_scores` returns an `EncoderPrediction(report, scores)` with a probability for
each of `code`, `dependency`, `environment`, `flaky_test` and `infrastructure`.
The frozen report contract forbids extra fields and has no per-class-score field. The
separate result preserves that contract, and `EncoderDiagnose` routes its scores through the
existing `AgentOutput.label_scores` channel for D9. Report confidence is raw softmax, not
calibrated gate confidence; proposal confidence remains zero and the gate calibrates it.
No evidence lines are invented from a classifier prediction.

The adapter retains the regex method at L0, proposes only the existing Diagnose playbook
actions, and escalates invalid/unavailable model output. Select it explicitly for the encoder
comparison after downloading a trained checkpoint:

```python
from pathlib import Path
from flowguard.contracts import AgentName
from flowguard.encoder import EncoderDiagnose, EncoderPredictor

predictor = EncoderPredictor.from_directory(Path("state/encoder"))
orchestrator.agents[AgentName.DIAGNOSE] = EncoderDiagnose(predictor)
```

The factory accepts only local safetensors and the exact five-class label order. It performs
CPU inference in eval/inference mode, uses a 512-token limit and never downloads a model
while handling an incident. Train and inference share ANSI removal and a bounded log tail.

No frozen-file change is made. If scores must literally be serialized inside
`DiagnosisReport` itself, the integration proposal is an optional validated `class_scores`
field in `agents/flowguard/contracts.py`; that requires a separate approved contract change.
The existing side channel already supplies D9 without it.

## CPU training and artifacts

The base is [DistilBERT](https://huggingface.co/distilbert/distilbert-base-uncased), pinned to
revision `12040accade4e8a0f71eabdb258fecc2e7e948be`. Dependencies are pinned separately in
`ci/encoder/requirements.txt`; ordinary tests neither install Torch nor download a model.
From the checkout, after importing the verified export:

```powershell
pip install -e .
pip install -r ci/encoder/requirements.txt
python ci/import_encoder.py --archive PATH_TO/flowguard-corpus-dev.zip --out state/encoder-corpus
python ci/train_encoder.py --index state/encoder-corpus/index.jsonl --log-root state/encoder-corpus --output state/encoder-training --unfreeze-layers 6 --epochs 10 --patience 3 --class-weighted
```

Training always selects CPU: seed 31, three epochs, two threads, batch size two, AdamW at
2e-5 and deterministic algorithms. By default only the classifier and last two DistilBERT
transformer blocks are updated; embeddings and earlier blocks are frozen to reduce CPU
memory/work on this tiny dataset. `--unfreeze-layers` records any explicit alternative.
Training and inference use the same preprocessing, left truncation and 512-token limit.
The best checkpoint is chosen by validation macro-F1; test templates are never opened.
Loading phases and batch loss/elapsed time are printed live. The importer validates all CSV
labels and ZIP membership before reading log bytes, accepts the source optional-job naming
convention, and rejects excluded, incomplete, mislabeled or unsafe exports.

Every epoch records training loss, elapsed time, validation macro-F1 and per-class
precision/recall/F1/support in `training-log.json`. Final metadata adds split/log checksums,
variant IDs, seed, CPU/platform, thread/batch counts, trainable parameters, wall time and
process memory. The script saves safetensors, validation predictions and `encoder-weights.zip`;
`artifact.json` contains the ZIP's actual SHA-256 and size. The measured training log and
metrics are committed; the weight ZIP is stored on the public fork release. The optional
notebook remains cleared and unrun.

Download the published checkpoint with checksum verification:

```powershell
python ci/download_encoder.py --url https://github.com/aziz696902/FlowGuard/releases/download/encoder-d7-cpu-final-run2/encoder-weights.zip --sha256 c9e2930e587eddabd0bff79d82c7e9348e76f5173f03078d483a4719b75396db --destination state/encoder-downloaded
python ci/benchmark_encoder.py --model state/encoder-downloaded --output state/encoder-cpu.json
python ci/benchmark_encoder.py --model state/encoder-downloaded --index state/encoder-corpus/index.jsonl --log-root state/encoder-corpus --iterations 42 --output state/encoder-cpu-validation.json
```

The downloader verifies SHA-256 before extraction and rejects unexpected paths, duplicate
files, symlinks and oversized artifacts. Validation-log benchmarking uses the same complete
strict loader and selects only validation examples. The artifact is a development checkpoint;
its poor accuracy is not repaired by low latency or passing interface tests.

## Verification and remaining work

Verified with Python 3.11: deterministic
mocked five-class predictions; report schema; all scores sum to one; valid catalog proposals;
L0 preservation; invalid-output escalation; train/validation allowlist and pre-open rejection;
known-value F1 calculations; checksum/unsafe-archive handling with mocked networking;
cleared notebook; firewall; Python syntax compilation and all three CLI help commands.
The PR records final full-suite, Ruff 0.17.1 lint and formatting results.

Also verified on the real verified training/validation export: CPU training, local
safetensors loading, independently recomputed validation metrics, artifact checksum and both
CPU workloads above. Committed JSON files record the measurements, including per-class
support and all epoch losses. The source archive manifest's provenance is retained; no
held-out file was opened or new corpus generated here.

Not verified: held-out accuracy, calibrated encoder coverage, live orchestrator integration,
production effectiveness or the complete system's memory budget. The only follow-up was the pre-declared architect-requested run 2; no further runs follow. Twenty training templates is a small set; these results
must not be generalized to independent incidents.

Remaining: review and integration on the other machine, then the separately authorized final
evaluation. Frozen files remain unchanged. The checkpoint remains optional and should not
replace the stronger existing methods on the strength of this result.

## Architect review: one additional pre-declared run

The immutable plan in `ci/encoder/run-2-plan.json` declares one run with six unfrozen
transformer blocks, at most ten epochs, training-only inverse-frequency class weights,
and early stopping on validation macro-F1 after three non-improving epochs. Seed, splits,
reduction, tokenizer, optimizer and other settings stay unchanged. Selection is the higher
validation macro-F1 across the two runs (ties retain run 1). No further runs follow.
