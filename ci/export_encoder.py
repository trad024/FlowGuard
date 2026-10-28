"""Export only verified train/validation corpus logs; never enumerate or open other variants."""

import argparse
import hashlib
import json
import zipfile
from collections import Counter
from pathlib import Path

from encoder_data import allowed_variants, canonical_hash, load_examples
from flowguard.encoder import CLASSES, prepare_text

BASE = Path(__file__).with_name("encoder") / "base-model.json"
JOBS = frozenset({"build", "unit", "lint", "integration", "sbom", "multiarch", "scans"})


def within(root, path):
    resolved = path.resolve(strict=True)
    if resolved != path or not resolved.is_relative_to(root):
        raise ValueError("corpus source must stay within its root without symlinks")
    return resolved


def export(split_path, corpus, out, expected_hash=None):
    split = json.loads(split_path.read_text(encoding="utf-8"))
    digest = canonical_hash(split)
    if expected_hash is not None and digest != expected_hash:
        raise ValueError("recorded split checksum mismatch")
    allowed = allowed_variants(split)
    root = corpus.resolve(strict=True)
    bundle = out.parent / (out.name + ".zip")
    if out.exists() or bundle.exists():
        raise ValueError("export output already exists; choose a fresh location")
    planned = []
    # Iterate the public allowlist, never glob the corpus or open excluded metadata/logs.
    for variant in sorted(allowed):
        if Path(variant).name != variant or variant in (".", ".."):
            raise ValueError("invalid variant path")
        folder = within(root, root / variant)
        meta_path = within(root, folder / "meta.json")
        meta_bytes = meta_path.read_bytes()
        meta = json.loads(meta_bytes)
        expected = split["variants"][variant]
        if (
            meta.get("verified") is not True
            or meta.get("variant") != variant
            or meta.get("template") != expected["template"]
            or meta.get("root_cause") != expected["root_cause"]
        ):
            raise ValueError(f"corpus metadata disagrees with the verified split: {variant}")
        job = meta.get("first_failed_job", "").removeprefix("optional (").removesuffix(")")
        if job not in JOBS:
            raise ValueError("unrecognized failed job in verified corpus metadata")
        log = within(root, folder / f"{job}.log")
        planned.append((variant, log, hashlib.sha256(meta_bytes).hexdigest()))
    texts, provenance = [], []
    for variant, log, meta_hash in planned:
        data = log.read_bytes()
        reduced = prepare_text(data.decode("utf-8")).encode("utf-8")
        texts.append((variant, reduced))
        provenance.append(
            {
                "variant_id": variant,
                "split": allowed[variant][0],
                "metadata_sha256": meta_hash,
                "source_log_sha256": hashlib.sha256(data).hexdigest(),
                "reduced_log_sha256": hashlib.sha256(reduced).hexdigest(),
            }
        )
    out.mkdir(parents=True)
    index = []
    for variant, data in texts:
        relative = f"{variant}/reduced.log"
        path = out / relative
        path.parent.mkdir()
        path.write_bytes(data)
        index.append({"variant_id": variant, "log_path": relative})
    index_path = out / "index.jsonl"
    index_path.write_text("".join(json.dumps(row) + "\n" for row in index), encoding="utf-8")
    # Verify the export with the very same complete strict loader used for training.
    load_examples(split_path, index_path, out, digest)
    manifest = {
        "split_sha256": digest,
        "counts": dict(Counter(value[0] for value in allowed.values())),
        "class_counts": dict(Counter(CLASSES[value[1]] for value in allowed.values())),
        "preprocessing": (
            "shared encoder prepare_text: ANSI removal and bounded 12000-character tail"
        ),
        "files": provenance,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in ["index.jsonl", "manifest.json", *(row["log_path"] for row in index)]:
            archive.write(out / relative, arcname=relative)
    with bundle.open("rb") as source:
        bundle_hash = hashlib.file_digest(source, "sha256").hexdigest()
    return {"bundle": str(bundle), "sha256": bundle_hash, "counts": manifest["counts"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", type=Path, default=Path("evaluation/datasets/corpus-split.json"))
    parser.add_argument("--corpus", type=Path, default=Path("evaluation/runs/corpus"))
    parser.add_argument("--out", type=Path, default=Path("state/encoder-corpus"))
    args = parser.parse_args()
    expected = json.loads(BASE.read_text(encoding="utf-8"))["split_sha256"]
    print(json.dumps(export(args.split, args.corpus, args.out, expected), indent=2))


if __name__ == "__main__":
    main()
