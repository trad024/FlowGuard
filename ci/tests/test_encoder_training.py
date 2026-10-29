"""Data allowlists, metrics and artifact checks, without real models or external calls."""

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from download_encoder import download
from encoder_data import canonical_hash, classification_metrics, load_examples
from train_encoder import EarlyStopping, class_weights


def test_early_stopping_counts_ties_and_resets_only_on_improvement():
    stopping = EarlyStopping(3)
    assert not stopping.update(0.2)
    assert not stopping.update(0.2)
    assert not stopping.update(0.1)
    assert not stopping.update(0.3)
    assert not stopping.update(0.3)
    assert not stopping.update(0.2)
    assert stopping.update(0.3)


def test_weights_balance_training_class_mass():
    labels = [0] + [1] * 2 + [2] * 3 + [3] * 4 + [4] * 5
    weights = class_weights(labels)
    assert [weights[i] * labels.count(i) for i in range(5)] == pytest.approx([3] * 5)
    with pytest.raises(ValueError):
        class_weights([0, 1])


@pytest.fixture
def data(tmp_path):
    split = {
        "templates": {"a": "train", "b": "validation", "c": "test"},
        "variants": {
            "a": {"template": "a", "root_cause": "code"},
            "b": {"template": "b", "root_cause": "dependency"},
            "c": {"template": "c", "root_cause": "environment"},
            "a.v2": {"template": "a", "root_cause": "code"},
        },
        "unverified": ["a.v2"],
    }
    split_path, index = tmp_path / "split.json", tmp_path / "index.jsonl"
    split_path.write_text(json.dumps(split))
    (tmp_path / "a.log").write_text("undefined: Missing")
    (tmp_path / "b.log").write_text("checksum mismatch")
    index.write_text(
        "\n".join(json.dumps({"variant_id": v, "log_path": f"{v}.log"}) for v in ("a", "b"))
    )
    return tmp_path, split_path, index, canonical_hash(split)


def test_only_recorded_train_validation_variants_are_loaded(data):
    root, split, index, digest = data
    examples = load_examples(split, index, root, digest)
    assert [e.split for e in examples] == ["train", "validation"]
    assert [e.label for e in examples] == [0, 1]
    assert [e.text for e in examples] == ["undefined: Missing", "checksum mismatch"]
    assert all(len(e.sha256) == 64 for e in examples)


@pytest.mark.parametrize("variant", ["c", "a.v2", "unknown"])
def test_excluded_variants_are_rejected_before_any_log_is_opened(data, monkeypatch, variant):
    root, split, index, digest = data
    index.write_text(json.dumps({"variant_id": variant, "log_path": f"{variant}.log"}))

    def refuse(self):
        raise AssertionError("a log was opened before metadata validation")

    monkeypatch.setattr(Path, "read_bytes", refuse)
    with pytest.raises(ValueError, match="excluded"):
        load_examples(split, index, root, digest)


def test_a_valid_variant_cannot_point_at_another_variants_file(data):
    root, split, index, digest = data
    index.write_text(json.dumps({"variant_id": "a", "log_path": "c.log"}))
    with pytest.raises(ValueError, match="identify its recorded variant"):
        load_examples(split, index, root, digest)
    with pytest.raises(ValueError, match="checksum"):
        load_examples(split, index, root, "0" * 64)


def test_f1_metrics_have_known_values_without_training():
    result = classification_metrics([0, 1, 2, 3, 4], [0, 1, 2, 3, 0])
    assert result["correct"] == 4 and result["accuracy"] == 0.8
    assert result["per_class"]["code"]["f1"] == pytest.approx(2 / 3)
    assert result["macro_f1"] == pytest.approx((2 / 3 + 3) / 5)


def test_incomplete_index_fails_before_any_log_is_read(data, monkeypatch):
    root, split, index, digest = data
    index.write_text(json.dumps({"variant_id": "a", "log_path": "a.log"}))

    def refuse(self):
        raise AssertionError("an incomplete index opened a log")

    monkeypatch.setattr(Path, "read_bytes", refuse)
    with pytest.raises(ValueError, match="every verified"):
        load_examples(split, index, root, digest)


class Response(io.BytesIO):
    def geturl(self):
        return "https://example.invalid/checkpoint.zip"


def bundle(extra=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in ("config.json", "tokenizer_config.json", "tokenizer.json", "model.safetensors"):
            archive.writestr(name, b"tiny mocked fixture")
        if extra:
            archive.writestr(extra, b"unsafe")
    return buffer.getvalue()


def test_download_verifies_checksum_with_mocked_network(tmp_path):
    payload = bundle()
    target = tmp_path / "model"
    result = download(
        "https://example.invalid/model.zip",
        hashlib.sha256(payload).hexdigest(),
        target,
        open_url=lambda *a, **kw: Response(payload),
    )
    assert (result / "model.safetensors").read_bytes() == b"tiny mocked fixture"


@pytest.mark.parametrize("bad_archive", [False, True])
def test_checksum_or_unsafe_archive_never_extracts(tmp_path, bad_archive):
    payload = bundle("../escape" if bad_archive else None)
    checksum = hashlib.sha256(payload).hexdigest() if bad_archive else "0" * 64
    with pytest.raises(ValueError):
        download(
            "https://example.invalid/model.zip",
            checksum,
            tmp_path / "model",
            open_url=lambda *a, **kw: Response(payload),
        )
    assert not (tmp_path / "model").exists() and not (tmp_path / "escape").exists()


def test_committed_notebook_outputs_are_cleared():
    root = Path(__file__).resolve().parents[2]
    notebook = json.loads((root / "notebooks/d7-encoder.ipynb").read_text(encoding="utf-8"))
    assert notebook["nbformat"] == 4
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            assert cell["outputs"] == [] and cell["execution_count"] is None
