"""L3 retrieval (Step 7): the training cases nearest a failed job's log, for Diagnose's prompt.

Index: a persistent Chroma collection under state/chroma/ (gitignored), one document per
training variant: the last EXAMPLE_CHARS characters of its reduced log (the part the prompt
shows, D51), with its template, class and reference action as metadata. Embeddings: the
all-MiniLM-L6-v2 sentence model as chromadb 1.5.9 ships it (ONNX, file pinned by SHA-256 in
chromadb), run on the CPU only and cached under state/models/. Model and index are built on
first use; nothing is downloaded at import.

`nearest` never returns a case of the query's own template (D45), so a training case is
diagnosed from other templates only, as a test case always is.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from flowguard import logreduce
from flowguard.diagnose import EXAMPLE_CHARS, MAX_EXAMPLES, Example

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "state"
INDEX = STATE / "chroma"
MODELS = STATE / "models"
COLLECTION = "diagnose-train"

# The fix an example shows for its class (D57: a pin only where a pin fixes the failure, a fix
# PR is assisted). Proposal, used only to label retrieved examples.
REFERENCE_ACTION = {
    "code": "open_fix_pr",
    "dependency": "pin_dependency",
    "environment": "open_fix_pr",
    "flaky_test": "retry_job",
    "infrastructure": "retry_job",
}


def document(log: str) -> str:
    """What is embedded and shown: the end of the reduced log, where failures are reported."""
    return logreduce.render(logreduce.reduce(log))[-EXAMPLE_CHARS:]


def embedder():
    """all-MiniLM-L6-v2 (ONNX) on the CPU, cached under state/models/."""
    from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

    class Local(ONNXMiniLM_L6_V2):
        DOWNLOAD_PATH = MODELS / ONNXMiniLM_L6_V2.MODEL_NAME

    return Local(preferred_providers=["CPUExecutionProvider"])


class Retriever:
    def __init__(self, path: Path = INDEX, collection: str = COLLECTION, embed=None):
        import chromadb
        from chromadb.config import Settings

        client = chromadb.PersistentClient(
            path=str(path), settings=Settings(anonymized_telemetry=False)
        )
        self.collection = client.get_or_create_collection(
            collection, embedding_function=embed or embedder(), metadata={"hnsw:space": "cosine"}
        )

    def build(self, cases: Iterable[dict]) -> int:
        """Index cases {"variant", "template", "root_cause", "log"}; the same variant again
        replaces its entry."""
        cases = list(cases)
        if cases:
            self.collection.upsert(
                ids=[c["variant"] for c in cases],
                documents=[document(c["log"]) for c in cases],
                metadatas=[
                    {
                        "template": c["template"],
                        "root_cause": c["root_cause"],
                        "action": REFERENCE_ACTION[c["root_cause"]],
                    }
                    for c in cases
                ],
            )
        return self.collection.count()

    def nearest(
        self, log: str, template: str | None = None, k: int = MAX_EXAMPLES
    ) -> list[Example]:
        """The k nearest cases of other templates, nearest first."""
        where = {"template": {"$ne": template}} if template else None
        res = self.collection.query(query_texts=[document(log)], n_results=k, where=where)
        return [
            Example(log=doc, root_cause=meta["root_cause"], action=meta["action"])
            for doc, meta in zip(res["documents"][0], res["metadatas"][0], strict=True)
        ]
