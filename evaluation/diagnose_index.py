"""L3 retrieval index (Step 7): build state/chroma/ from the corpus' training and validation cases.

    python evaluation/diagnose_index.py build

Only training and validation variants are indexed: the build refuses a variant of a test
template and any log outside the corpus (LogChunks logs are never indexed). The text embedded
for a case is `flowguard.retrieval.document(log)`, the same function a query goes through, so the
model's 256-token cap cuts documents and queries the same way.

The manifest (state/chroma/index-manifest.json) is deterministic: the same corpus and model
give the same file. It holds the sorted variant ids with the SHA-256 of each embedded document,
the embedding model's pin (chromadb's archive SHA-256 and each extracted file's SHA-256; that
archive has no Hugging Face commit id), and the chunking. The diagnose-freeze (D39) hashes it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "evaluation")]

import diagnose_dev  # noqa: E402
from flowguard import retrieval  # noqa: E402
from flowguard.diagnose import EXAMPLE_CHARS  # noqa: E402

MANIFEST = retrieval.INDEX / "index-manifest.json"
DEV = ("train", "validation")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cases(split: dict, items: list[dict] | None = None, corpus: Path | None = None) -> list[dict]:
    """The cases to index: training and validation variants of the corpus, with their class.
    Anything else is refused, never skipped."""
    corpus = (corpus or diagnose_dev.CORPUS).resolve()
    items = diagnose_dev.items(split, DEV) if items is None else items
    out = []
    for it in items:
        s = split["templates"].get(it["template"])
        if s not in DEV:
            raise ValueError(f"{it['variant']}: template {it['template']} is {s}, not indexed")
        log = Path(it["log"]).resolve()
        if corpus not in log.parents:
            raise ValueError(f"{it['variant']}: {log} is outside the corpus, not indexed")
        meta = json.loads((log.parent / "meta.json").read_text(encoding="utf-8"))
        out.append(
            {
                "variant": it["variant"],
                "template": it["template"],
                "split": s,
                "root_cause": meta["root_cause"],
                "log": log.read_text(encoding="utf-8", errors="replace"),
            }
        )
    return sorted(out, key=lambda c: c["variant"])


def model_pin(directory: Path | None = None) -> dict:
    """The embedding model as it was downloaded: chromadb's pinned archive and its files."""
    from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

    directory = directory or retrieval.MODELS / ONNXMiniLM_L6_V2.MODEL_NAME
    files = {
        p.relative_to(directory).as_posix(): sha256(p.read_bytes())
        for p in sorted(directory.rglob("*"))
        if p.is_file() and p.name != ONNXMiniLM_L6_V2.ARCHIVE_FILENAME
    }
    if not files:
        raise FileNotFoundError(f"{directory}: the embedding model is not downloaded")
    return {
        "name": ONNXMiniLM_L6_V2.MODEL_NAME,
        "package": "chromadb==1.5.9",
        "source": ONNXMiniLM_L6_V2.MODEL_DOWNLOAD_URL,
        "archive_sha256": ONNXMiniLM_L6_V2._MODEL_SHA256,  # checked by chromadb on download
        "files_sha256": files,
        "revision": "no Hugging Face commit id is published with chromadb's archive; the "
        "archive SHA-256 and the file hashes pin it",
        "runtime": "onnxruntime==1.31.0, CPUExecutionProvider",
    }


def manifest(indexed: list[dict], model: dict) -> dict:
    return {
        "collection": retrieval.COLLECTION,
        "splits": list(DEV),
        "variants": {
            c["variant"]: {
                "template": c["template"],
                "split": c["split"],
                "document_sha256": sha256(retrieval.document(c["log"]).encode("utf-8")),
            }
            for c in indexed
        },
        "templates": len({c["template"] for c in indexed}),
        "embedding_model": model,
        "chunking": {
            "function": "flowguard.retrieval.document",
            "used_for": ["documents", "queries"],
            "text": f"the last {EXAMPLE_CHARS} characters of the reduced log (logreduce)",
            "model_cap": "chromadb truncates every text to 256 tokens",
            "one_document_per_variant": True,
        },
        "own_template_exclusion": (
            "for dev calibration only (D45): a dev query excludes cases of its own template; at "
            "held-out time it does nothing, as test templates are never in the index"
        ),
    }


def build(retriever, indexed: list[dict], model=model_pin, path: Path = MANIFEST) -> dict:
    """Index the cases, then pin the model as the build used it (the first build downloads
    it) and write the manifest."""
    retriever.build(indexed)
    m = manifest(indexed, model())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(m, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return m


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("build",))
    parser.parse_args(argv)
    indexed = cases(diagnose_dev.load_split())
    retriever = retrieval.Retriever()  # downloads the model on first use (state/models/)
    m = build(retriever, indexed)
    print(json.dumps({"variants": len(m["variants"]), "templates": m["templates"],
                      "manifest": str(MANIFEST)}))  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
