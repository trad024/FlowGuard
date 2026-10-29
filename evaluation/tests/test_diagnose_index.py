"""L3 index build (Step 7): training and validation cases only, a deterministic manifest that pins
the embedding model, and one text representation for documents and queries."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import diagnose_dev  # noqa: E402
import diagnose_index  # noqa: E402
from flowguard import retrieval  # noqa: E402

LONG = "\n".join(
    f"step {i}: go: module example.com/x@v1.{i}.0: checksum mismatch" for i in range(400)
)


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """A stand-in corpus: every split variant with a log and its class (the real one is local)."""
    split = diagnose_dev.load_split()
    root = tmp_path / "corpus"
    for variant, meta in split["variants"].items():
        (root / variant).mkdir(parents=True)
        (root / variant / "unit.log").write_text(f"{LONG}\n{variant} failed\n", encoding="utf-8")
        cls = meta["template"].split("-")[0]
        (root / variant / "meta.json").write_text(json.dumps({"root_cause": cls}), encoding="utf-8")
    monkeypatch.setattr(diagnose_dev, "CORPUS", root)
    return split, root


class FakeCollection:
    def __init__(self):
        self.documents, self.queries = {}, []

    def upsert(self, ids, documents, metadatas):
        self.documents.update(zip(ids, documents, strict=True))

    def count(self):
        return len(self.documents)

    def query(self, query_texts, n_results, where=None):
        self.queries += query_texts
        return {"documents": [[]], "metadatas": [[]]}


def fake_retriever():
    r = object.__new__(retrieval.Retriever)  # no Chroma, no model download
    r.collection = FakeCollection()
    return r


def model(tmp_path):
    d = tmp_path / "model" / "all-MiniLM-L6-v2"
    (d / "onnx").mkdir(parents=True, exist_ok=True)
    (d / "onnx" / "model.onnx").write_bytes(b"weights")
    (d / "onnx" / "tokenizer.json").write_text("{}", encoding="utf-8")
    return lambda: diagnose_index.model_pin(d)


def test_only_training_and_validation_variants_are_indexed(corpus, tmp_path):
    split, _ = corpus
    indexed = diagnose_index.cases(split)
    test = {t for t, s in split["templates"].items() if s == "test"}
    assert indexed and {c["split"] for c in indexed} <= {"train", "validation"}
    assert not {c["template"] for c in indexed} & test
    m = diagnose_index.build(fake_retriever(), indexed, model(tmp_path), tmp_path / "m.json")
    assert m["templates"] == 25 and set(m["variants"]) == {c["variant"] for c in indexed}


def test_a_test_template_or_a_log_outside_the_corpus_is_refused(corpus, tmp_path):
    split, root = corpus
    test_variant = next(v for v, m in split["variants"].items()
                        if split["templates"][m["template"]] == "test")  # fmt: skip
    item = {"variant": test_variant, "template": split["variants"][test_variant]["template"],
            "log": root / test_variant / "unit.log"}  # fmt: skip
    with pytest.raises(ValueError, match="is test, not indexed"):
        diagnose_index.cases(split, [item])
    logchunks = tmp_path / "datasets" / "raw" / "logchunks" / "a.log"  # never in the corpus
    logchunks.parent.mkdir(parents=True)
    logchunks.write_text("x\n", encoding="utf-8")
    (logchunks.parent / "meta.json").write_text('{"root_cause": "code"}', encoding="utf-8")
    train_variant = next(v for v, m in split["variants"].items()
                         if split["templates"][m["template"]] == "train")  # fmt: skip
    item = {"variant": train_variant, "template": split["variants"][train_variant]["template"],
            "log": logchunks}  # fmt: skip
    with pytest.raises(ValueError, match="outside the corpus"):
        diagnose_index.cases(split, [item])


def test_the_same_inputs_give_the_same_manifest(corpus, tmp_path):
    split, _ = corpus
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    diagnose_index.build(fake_retriever(), diagnose_index.cases(split), model(tmp_path), a)
    diagnose_index.build(fake_retriever(), diagnose_index.cases(split), model(tmp_path), b)
    assert a.read_bytes() == b.read_bytes()
    m = json.loads(a.read_text(encoding="utf-8"))
    pin = m["embedding_model"]
    assert pin["archive_sha256"] and set(pin["files_sha256"]) == {
        "onnx/model.onnx",
        "onnx/tokenizer.json",
    }
    assert "no Hugging Face commit id" in pin["revision"]
    assert m["chunking"]["function"] == "flowguard.retrieval.document"
    assert "held-out time it does nothing" in m["own_template_exclusion"]


def test_a_query_is_embedded_as_the_same_text_as_its_document(corpus, tmp_path):
    """One representation: the query for a long log is its document, never the log's start."""
    split, _ = corpus
    r = fake_retriever()
    indexed = diagnose_index.cases(split)
    r.build(indexed)
    case = indexed[0]
    r.nearest(case["log"])
    (query,) = r.collection.queries
    assert query == r.collection.documents[case["variant"]] == retrieval.document(case["log"])
    assert not case["log"].startswith(query)  # the end of the log, not its beginning
