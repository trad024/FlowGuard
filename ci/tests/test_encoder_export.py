"""Synthetic source corpus only; excluded directories must never be opened by the exporter."""

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from encoder_data import canonical_hash, load_examples
from export_encoder import export


@pytest.fixture
def corpus(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    record = {
        "templates": {"a": "train", "b": "validation", "c": "test"},
        "variants": {
            v: {"template": v, "root_cause": cls}
            for v, cls in (("a", "code"), ("b", "dependency"), ("c", "environment"))
        },
        "unverified": [],
    }
    for variant, meta in record["variants"].items():
        folder = source / variant
        folder.mkdir()
        (folder / "meta.json").write_text(
            json.dumps(
                {
                    **meta,
                    "variant": variant,
                    "verified": True,
                    "first_failed_job": "unit",
                }
            )
        )
        (folder / "unit.log").write_text(f"fixture log for {variant}")
    split = tmp_path / "split.json"
    split.write_text(json.dumps(record))
    return split, source, canonical_hash(record)


def test_export_includes_exact_allowlist_and_checksums_without_opening_excluded_files(
    corpus, tmp_path, monkeypatch
):
    split, source, digest = corpus
    original = Path.read_bytes

    def guarded(path):
        assert source / "c" not in path.parents
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    target = tmp_path / "export"
    result = export(split, source, target, digest)
    assert result["counts"] == {"train": 1, "validation": 1}
    assert [e.variant for e in load_examples(split, target / "index.jsonl", target, digest)] == [
        "a",
        "b",
    ]
    bundle = Path(result["bundle"])
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == result["sha256"]
    with zipfile.ZipFile(bundle) as archive:
        assert set(archive.namelist()) == {
            "a/reduced.log",
            "b/reduced.log",
            "index.jsonl",
            "manifest.json",
        }


def test_unverified_source_metadata_aborts_before_export(corpus, tmp_path):
    split, source, digest = corpus
    meta = source / "a/meta.json"
    data = json.loads(meta.read_text())
    meta.write_text(json.dumps({**data, "verified": False}))
    target = tmp_path / "export"
    with pytest.raises(ValueError, match="metadata disagrees"):
        export(split, source, target, digest)
    assert not target.exists()
