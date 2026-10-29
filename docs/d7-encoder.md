# D7: measured CPU encoder development checkpoint

CPU training completed on 2026-10-29: three epochs, seed 31, two threads, batch size two,
with the classifier and last two DistilBERT blocks trainable. The actual training log,
validation predictions, corpus provenance, package versions and CPU benchmarks are committed
under `ci/encoder/results/`. No held-out test templates were used.

**This is a negative validation result:** macro-F1 0.2533, accuracy 5/14 (35.7%). It misses
three of five classes entirely. The checkpoint is delivered for review/comparison, not
adopted as a reliable deployment diagnosis model. No extra tuning run was performed.

| Class | Validation support | F1 |
|---|---:|---:|
| code | 2 | 0.0000 |
| dependency | 3 | 0.6000 |
| environment | 4 | 0.0000 |
| flaky_test | 2 | 0.6667 |
| infrastructure | 3 | 0.0000 |

| Epoch | Training loss | Validation macro-F1 | Accuracy |
|---|---:|---:|---:|
| 1 | 1.5966 | 0.0706 | 3/14 |
| 2 | 1.4235 | 0.2256 | 5/14 |
| 3 (selected) | 1.1912 | 0.2533 | 5/14 |

Measured training wall time: 155.94 seconds (excluding base-model download/load); peak
Windows process working set: 1137.24 MiB. Hardware: Intel i5-11300H, 4 physical cores,
8 logical processors, 7.7 GiB usable RAM. Training used only two CPU threads.

| CPU inference workload | Calls after 5 warmups | Median | p95 | Sampled peak process RSS |
|---|---:|---:|---:|---:|
| Fixed short-log smoke | 30 | 38.95 ms | 46.90 ms | 449.93 MiB |
| 14 validation logs, 3 cycles | 42 | 372.35 ms | 468.56 ms | 510.42 MiB |

RSS is sampled every 10 ms and includes checkpoint loading. This measures one encoder
process, not the entire FlowGuard/Docker footprint. The small validation workload does not
establish production latency or the whole-system 4 GB budget.

Weights remain outside git, on the public fork's
[development release](https://github.com/aziz696902/FlowGuard/releases/tag/encoder-d7-cpu-v1).
The actual URL, SHA-256 and size are pinned in `ci/encoder/artifact.json`:

```text
470d1b0fb40a106d1567e80c747329bf171bcbd45aac6409a77b93d6f90b964d
```

## Split and data boundary

The branch is based on `dev` commit `be5e4b2`, including accepted D39-D44 decisions and
the draft 0.3 pre-registration. The CI template assignment is populated.
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
python ci/train_encoder.py --index state/encoder-corpus/index.jsonl --log-root state/encoder-corpus --output state/encoder-training
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
`artifact.json` contains the ZIP's actual SHA-256 and size. After a real run, commit the
training log and metrics, publish the weight ZIP outside git (for example, on the fork's release),
and record its real public URL/checksum. The optional notebook remains cleared and unrun.

Download the published checkpoint with checksum verification:

```powershell
python ci/download_encoder.py --url https://github.com/aziz696902/FlowGuard/releases/download/encoder-d7-cpu-v1/encoder-weights.zip --sha256 470d1b0fb40a106d1567e80c747329bf171bcbd45aac6409a77b93d6f90b964d --destination state/encoder-downloaded
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
production effectiveness or the complete system's memory budget. No follow-up tuning was
performed after seeing validation. Twenty training templates is a small set; these results
must not be generalized to independent incidents.

Remaining: review and integration on the other machine, then the separately authorized final
evaluation. Frozen files remain unchanged. The checkpoint remains optional and should not
replace the stronger existing methods on the strength of this result.
