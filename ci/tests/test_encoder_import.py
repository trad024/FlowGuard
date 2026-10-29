"""Labelled ZIP fixtures; reject invalid metadata before reading log bytes."""

import csv
import io
import json
import zipfile

import pytest

from encoder_data import canonical_hash, load_examples
from import_encoder import import_bundle


@pytest.mark.parametrize(
    "problem", [None, "optional_job", "excluded", "assignment", "extra", "incomplete"]
)
def test_labelled_import_validates_all_metadata_before_logs(tmp_path, monkeypatch, problem):
    record = {
        "templates": {"a": "train", "b": "validation", "c": "test"},
        "variants": {v: {"template": v, "root_cause": "code"} for v in "abc"},
        "unverified": [],
    }
    split = tmp_path / "split.json"
    split.write_text(json.dumps(record))
    rows = [
        dict(
            variant=v,
            template=v,
            split=s,
            **{"class": "code"},
            failing_job="unit",
            log=f"logs/{v}/unit.log",
        )
        for v, s in (("a", "train"), ("b", "validation"))
    ]
    if problem == "excluded":
        rows[1].update(variant="c", template="c", split="test", log="logs/c/unit.log")
    if problem == "assignment":
        rows[1]["class"] = "dependency"
    if problem == "optional_job":
        rows[1].update(failing_job="optional (integration)", log="logs/b/integration.log")
    if problem == "incomplete":
        rows.pop()
    labels = io.StringIO()
    writer = csv.DictWriter(labels, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    bundle = tmp_path / "source.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("labels.csv", labels.getvalue())
        archive.writestr("MANIFEST.txt", "synthetic development export")
        for row in rows:
            archive.writestr(row["log"], "\x1b[31mundefined symbol\x1b[0m")
        if problem == "extra":
            archive.writestr("logs/c/unit.log", "excluded fixture")
    original = zipfile.ZipFile.read

    def guarded(self, name, *args, **kwargs):
        if name.endswith(".log"):
            assert problem in (None, "optional_job"), "invalid export opened log bytes"
        return original(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", guarded)
    out = tmp_path / "export"
    digest = canonical_hash(record)
    if problem not in (None, "optional_job"):
        with pytest.raises(ValueError):
            import_bundle(bundle, split, out, digest)
        assert not out.exists()
    else:
        manifest = import_bundle(bundle, split, out, digest)
        assert manifest["counts"] == {"train": 1, "validation": 1}
        examples = load_examples(split, out / "index.jsonl", out, digest)
        assert [e.text for e in examples] == ["undefined symbol", "undefined symbol"]
