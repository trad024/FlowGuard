"""D64: the level that made each cached LLM answer comes from the audit log's llm_call event."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agents"))

import llm_provenance  # noqa: E402
from flowguard.audit import AuditLog  # noqa: E402


def call(audit, key):
    audit.event("llm", "llm_call", cache_key=key, lane="GROQ_KEY_1", tokens_total=100)


def cache(tmp_path, *keys):
    d = tmp_path / "llm-cache"
    d.mkdir(exist_ok=True)
    for k in keys:
        (d / f"{k}.json").write_text(json.dumps({"content": "{}"}), encoding="utf-8")
    return d


def test_each_cache_key_has_exactly_one_making_level(tmp_path):
    db = tmp_path / "audit.db"
    call(AuditLog(db, level="L5"), "k-live")  # the live L5 run made this call first
    call(AuditLog(db, level="L2"), "k-zero")
    call(AuditLog(db, level="L3"), "k-retrieval")
    # L3 offline on the live log hit the cache: no second event for k-live
    levels = llm_provenance.making_levels(db, cache(tmp_path, "k-live", "k-zero", "k-retrieval"))
    assert levels == {"k-live": "L5", "k-zero": "L2", "k-retrieval": "L3"}


def test_a_cached_answer_without_its_call_is_an_error_not_a_guess(tmp_path):
    db = tmp_path / "audit.db"
    call(AuditLog(db, level="L2"), "k-1")
    with pytest.raises(ValueError, match="no llm_call event"):
        llm_provenance.making_levels(db, cache(tmp_path, "k-1", "k-orphan"))


def test_a_key_called_from_two_levels_is_an_error(tmp_path):
    db = tmp_path / "audit.db"
    call(AuditLog(db, level="L2"), "k-1")
    call(AuditLog(db, level="L3"), "k-1")
    with pytest.raises(ValueError, match=r"\['L2', 'L3'\]"):
        llm_provenance.making_levels(db, cache(tmp_path, "k-1"))
