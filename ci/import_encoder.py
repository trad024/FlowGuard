"""Validate a labelled development ZIP before reading any train/validation log."""

import argparse
import csv
import hashlib
import io
import json
import stat
import zipfile
from collections import Counter
from pathlib import Path

from encoder_data import allowed_variants, canonical_hash, load_examples
from export_encoder import BASE, JOBS
from flowguard.encoder import prepare_text

MAX_BYTES = 64 * 1024 * 1024
COLUMNS = {"variant", "template", "split", "class", "failing_job", "log"}


def import_bundle(bundle, split_path, out, expected_hash):
    record = json.loads(split_path.read_text(encoding="utf-8"))
    if canonical_hash(record) != expected_hash:
        raise ValueError("recorded split checksum mismatch")
    allowed = allowed_variants(record)
    if out.exists():
        raise ValueError("output already exists; choose a fresh location")
    with bundle.open("rb") as source:
        source_hash = hashlib.file_digest(source, "sha256").hexdigest()
    with zipfile.ZipFile(bundle) as archive:
        infos = archive.infolist()
        names = [i.filename for i in infos]
        if (
            len(names) != len(set(names))
            or sum(i.file_size for i in infos) > MAX_BYTES
            or any(stat.S_ISLNK(i.external_attr >> 16) for i in infos)
        ):
            raise ValueError("duplicate, oversized or symlinked archive entries")
        # Read only metadata until every variant, assignment and archive member is validated.
        reader = csv.DictReader(io.StringIO(archive.read("labels.csv").decode("utf-8-sig")))
        if set(reader.fieldnames or []) != COLUMNS or len(reader.fieldnames) != len(COLUMNS):
            raise ValueError("unexpected label columns")
        rows = list(reader)
        seen, planned = set(), []
        for row in rows:
            variant = row["variant"]
            if variant not in allowed or variant in seen:
                raise ValueError("excluded, unverified, unknown or duplicate variant")
            seen.add(variant)
            meta = record["variants"][variant]
            job = row["failing_job"].removeprefix("optional (").removesuffix(")")
            if (
                row["template"] != meta["template"]
                or row["class"] != meta["root_cause"]
                or row["split"] != allowed[variant][0]
                or job not in JOBS
                or row["log"] != f"logs/{variant}/{job}.log"
                or Path(variant).name != variant
                or variant in (".", "..")
            ):
                raise ValueError("label or path disagrees with recorded split")
            planned.append(row["log"])
        if seen != set(allowed):
            raise ValueError("incomplete verified train/validation export")
        if set(names) != {"MANIFEST.txt", "labels.csv", *planned}:
            raise ValueError("unexpected or missing archive members")
        provenance, texts = [], []
        for row in rows:
            data = archive.read(row["log"])
            reduced = prepare_text(data.decode("utf-8")).encode("utf-8")
            texts.append((row["variant"], reduced))
            provenance.append(
                {
                    "variant_id": row["variant"],
                    "split": row["split"],
                    "source_path": row["log"],
                    "source_log_sha256": hashlib.sha256(data).hexdigest(),
                    "reduced_log_sha256": hashlib.sha256(reduced).hexdigest(),
                }
            )
        source_manifest = archive.read("MANIFEST.txt").decode("utf-8")
    out.mkdir(parents=True)
    index = []
    for variant, data in texts:
        relative = f"{variant}/reduced.log"
        target = out / relative
        target.parent.mkdir()
        target.write_bytes(data)
        index.append({"variant_id": variant, "log_path": relative})
    index_path = out / "index.jsonl"
    index_path.write_text("".join(json.dumps(row) + "\n" for row in index), encoding="utf-8")
    load_examples(split_path, index_path, out, expected_hash)
    manifest = {
        "source_archive_sha256": source_hash,
        "source_manifest": source_manifest,
        "split_sha256": expected_hash,
        "counts": dict(Counter(row["split"] for row in rows)),
        "preprocessing": "shared prepare_text: ANSI removal and bounded 12000-character tail",
        "files": provenance,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--split", type=Path, default=Path("evaluation/datasets/corpus-split.json"))
    parser.add_argument("--out", type=Path, default=Path("state/encoder-corpus"))
    args = parser.parse_args()
    expected = json.loads(BASE.read_text(encoding="utf-8"))["split_sha256"]
    result = import_bundle(args.archive, args.split, args.out, expected)
    print(json.dumps({k: result[k] for k in ("source_archive_sha256", "counts")}, indent=2))


if __name__ == "__main__":
    main()
