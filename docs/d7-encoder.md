# D7: CPU encoder preparation ? verified logs pending

The predictor, strict loader, CPU training script, train/validation exporter, cleared
notebook, verified artifact downloader and CPU benchmark are implemented. CPU training here
is now authorized and is the configured workflow. **No training has run yet** because the
verified corpus logs are absent from this machine and have not been published in the
repository, releases or Actions artifacts. The latest upstream documentation reports an
L0 development score on 74 logs, suggesting the corpus is available on its generating
machine; the split alone does not supply those log bytes.

No trained weights, validation F1, training log from an actual run, hosted weight checksum,
or measured trained-model CPU RAM/latency exist yet. These values are not fabricated. PR #3
remains a draft until the real CPU run, publication and benchmark complete.

## Split and data boundary

The branch is based on `dev` commit `be5e4b2`, including accepted D39?D44 decisions and
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
That export is not present in this clone. The index is JSON Lines containing metadata only:

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
From the checkout, after receiving the verified export:

```powershell
pip install -e .
pip install -r ci/encoder/requirements.txt
python ci/train_encoder.py --index state/encoder-corpus/index.jsonl --log-root state/encoder-corpus --output state/encoder-training
```

Training always selects CPU: seed 31, three epochs, two threads, batch size two, AdamW at
2e-5 and deterministic algorithms. By default only the classifier and last two DistilBERT
transformer blocks are updated; embeddings and earlier blocks are frozen to reduce CPU
memory/work on this tiny dataset. `--unfreeze-layers` records any explicit alternative.
Training and inference use the same preprocessing, left truncation and 512-token limit.
The best checkpoint is chosen by validation macro-F1; test templates are never opened.

Every epoch records training loss, elapsed time, validation macro-F1 and per-class
precision/recall/F1/support in `training-log.json`. Final metadata adds split/log checksums,
variant IDs, seed, CPU/platform, thread/batch counts, trainable parameters, wall time and
process memory. The script saves safetensors, validation predictions and `encoder-weights.zip`;
`artifact.json` contains the ZIP's actual SHA-256 and size. After a real run, commit the
training log and metrics, publish the weight ZIP outside git (for example, on the fork's release),
and record its real public URL/checksum. The optional notebook remains cleared and unrun.

After publication, use the actual artifact URL and checksum:

```powershell
python ci/download_encoder.py --url ACTUAL_HTTPS_URL --sha256 ACTUAL_SHA256 --destination state/encoder
python ci/benchmark_encoder.py --model state/encoder --output state/encoder-cpu.json
```

The downloader checks the SHA-256 before extraction and rejects unexpected paths, duplicate
files, symlinks and oversized artifacts. There is no invented download URL or weight checksum
in this branch. The benchmark records median/p95 CPU latency after five warmups, sampled
process RSS including model load, incremental RSS, platform and thread count. Run it on the
target laptop before deciding whether the complete system fits the 4 GB budget. Its fixed
short log is a smoke workload, not a representative CI-log latency distribution.

## Verification and remaining work

Verified with Python 3.11: deterministic
mocked five-class predictions; report schema; all scores sum to one; valid catalog proposals;
L0 preservation; invalid-output escalation; train/validation allowlist and pre-open rejection;
known-value F1 calculations; checksum/unsafe-archive handling with mocked networking;
cleared notebook; firewall; Python syntax compilation and all three CLI help commands.
The PR records final full-suite, Ruff 0.17.1 lint and formatting results.

Not verified: actual CPU training, training dependency installation, trained-model loading,
validation performance, hosted weight download, CPU RAM/latency or live integration. There
is no claimed model result from configuring a training command or testing a mocked backend.

Remaining blocker: obtain the verified export. Then run CPU training, commit its real log
and validation scores, publish/download the weights with their actual checksum, measure CPU
RAM/latency, and mark the PR ready. Frozen files remain unchanged.
