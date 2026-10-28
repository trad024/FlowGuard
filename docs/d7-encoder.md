# D7: encoder preparation — external training pending

This branch supplies the predictor, optional Diagnose adapter, data boundary, external GPU
training script, cleared notebook, artifact downloader and CPU benchmark script. **No encoder
was trained or downloaded on this machine**, following the operator's instruction. There are
no trained weights, validation F1 scores or trained-model CPU measurements to report yet.
The PR remains a draft until those external runs and artifact publication are complete.

## Split and data boundary

The branch starts from `dev` commit `0e3b8ca`, including the newly recorded corpus split and
approved D37/D38 changes. The pre-registration's CI template assignment is now populated.
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

Training requires an external export of only the verified train/validation reduced logs.
That export is not present in this clone. The index is JSON Lines containing metadata only:

```json
{"variant_id": "code-02", "log_path": "code-02/reduced.log"}
```

Alternatively `code-02.log` is accepted. Each log must correspond to its recorded variant;
the loader also records its SHA-256 for training provenance. No inline logs or label fields
are accepted in the index. Both training and validation must contain all five classes.

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

## External training and artifacts

The base is [DistilBERT](https://huggingface.co/distilbert/distilbert-base-uncased), pinned to
revision `12040accade4e8a0f71eabdb258fecc2e7e948be`, verified using the publisher's model
metadata. The code follows the [Transformers 4.46.3 DistilBERT interface](https://huggingface.co/docs/transformers/v4.46.3/en/model_doc/distilbert).
Training dependencies are isolated in `ci/encoder/requirements.txt`; ordinary tests do not
install Torch or download a model. Use the cleared `notebooks/d7-encoder.ipynb` on an external
free GPU runtime with Python 3.11, or run there:

```powershell
pip install -e .
pip install -r ci/encoder/requirements.txt
python ci/train_encoder.py --index /path/to/index.jsonl --log-root /path/to/logs --output /path/to/new-output
```

The script requires CUDA, never falls back to CPU training, uses seed 31, AdamW, three
epochs by default, and selects the checkpoint by validation macro-F1 only. It exports
per-class precision/recall/F1/support, aggregate macro-F1, validation per-class scores,
split/log provenance, a safetensors checkpoint and `encoder-weights.zip`. The ZIP's actual
SHA-256 and byte size are written to `artifact.json`. Store and publish weights outside git.
The notebook is unexecuted and every code-cell output and execution count is cleared.

After external publication, download using the **actual** URL and checksum from that run:

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

Verified locally with Python 3.11: updated-dev baseline 507 passed / 3 skipped; deterministic
mocked five-class predictions; report schema; all scores sum to one; valid catalog proposals;
L0 preservation; invalid-output escalation; train/validation allowlist and pre-open rejection;
known-value F1 calculations; checksum/unsafe-archive handling with mocked networking;
cleared notebook; firewall; Python syntax compilation and all three CLI help commands.
The PR records final full-suite, Ruff 0.17.1 lint and formatting results.

Not verified: external GPU execution, dependency installation in that GPU environment,
trained-model loading or inference, validation model quality, live orchestrator deployment,
CPU RAM/latency, hosted artifact download, or skipped integration tests.

Remaining: export the permitted logs; execute the notebook externally; retain real validation
metrics and metadata; publish the weight ZIP and checksum; download and benchmark on the
target laptop; then replace this pending status with measured results and make the PR ready.
