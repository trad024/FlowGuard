"""D67: the gate's calibrators per level. One loader, fail closed (missing file, changed hash,
too few examples), every entry point uses it, the orchestrator refuses L1+ without a fitted
calibrator for each classified medium-tier agent; Diagnose's L4 and L5 requests are those of
L3; retrieved example ids are recorded, never sent to the model. No network."""

import hashlib
import json
import re
from pathlib import Path

import pytest
from conftest import fitted_all

from flowguard import calibrators, diagnose, llm
from flowguard.approval import SimulatedApprover
from flowguard.audit import AuditLog
from flowguard.calibration import ConformalCalibrator
from flowguard.contracts import AgentName
from flowguard.executor import Executor
from flowguard.incidents import IncidentRecord
from flowguard.monitor import ci_event
from flowguard.orchestrator import Orchestrator, level
from flowguard.stubs import Diagnose

ROOT = Path(__file__).resolve().parents[2]
LOG = "go: example.com/x@v9.9.9: invalid version: unknown revision v9.9.9\nFAIL\n"
RUN = {"id": 7, "status": "completed", "conclusion": "failure"}
JOBS = [{"id": 5, "name": "build", "conclusion": "failure"}]


def calibration(tmp_path, level_file="L3", content=None) -> tuple[Path, Path]:
    """A state/ root with one Diagnose calibration file, and a hash file naming it."""
    cal = ConformalCalibrator().fit([{"a": 0.6 + i / 100} for i in range(30)], ["a"] * 30)
    path = tmp_path / "state" / "models" / f"diagnose-calibration-{level_file}.json"
    cal.save(path)
    if content is not None:
        path.write_text(content, encoding="utf-8")
    real = hashlib.sha256(path.read_bytes()).hexdigest()
    hashes = tmp_path / "hashes.sha256"
    rel = f"state/models/diagnose-calibration-{level_file}.json"
    hashes.write_text(f"{real}  {rel}\n", encoding="utf-8")
    return tmp_path, hashes


# ---------------------------------------------------------------- the loader


def test_each_level_names_its_diagnose_calibration():
    assert calibrators.DIAGNOSE_CALIBRATION == {
        "L1": "L1", "L2": "L2", "L3": "L3", "L4": "L3", "L5": "L3",
    }  # fmt: skip


def test_a_frozen_calibration_loads(tmp_path):
    root, hashes = calibration(tmp_path)
    for name in ("L3", "L4", "L5"):  # L4 and L5 use L3's
        assert calibrators.diagnose(name, root, hashes).calibrated


def test_a_missing_calibration_refuses(tmp_path):
    root, hashes = calibration(tmp_path, "L3")
    with pytest.raises(calibrators.CalibrationRefused, match="L1.json is missing"):
        calibrators.diagnose("L1", root, hashes)


def test_a_changed_calibration_refuses(tmp_path):
    root, hashes = calibration(tmp_path)
    path = root / "state" / "models" / "diagnose-calibration-L3.json"
    path.write_text(path.read_text().replace("0.", "0.0", 1), encoding="utf-8")
    with pytest.raises(calibrators.CalibrationRefused, match="differs from its frozen hash"):
        calibrators.diagnose("L3", root, hashes)


def test_a_calibration_without_a_frozen_hash_or_examples_refuses(tmp_path):
    root, hashes = calibration(tmp_path)
    hashes.write_text("", encoding="utf-8")
    with pytest.raises(calibrators.CalibrationRefused, match="no frozen hash"):
        calibrators.diagnose("L3", root, hashes)
    root, hashes = calibration(tmp_path / "few", content=json.dumps({"alphas": [0.1] * 5}))
    with pytest.raises(calibrators.CalibrationRefused, match="too few examples"):
        calibrators.diagnose("L3", root, hashes)


def test_l0_needs_no_calibrator_and_an_unknown_level_refuses():
    assert calibrators.for_level("L0") == {}
    with pytest.raises(calibrators.CalibrationRefused):
        calibrators.diagnose_path("L9")


def test_the_loaders_hashes_are_the_frozen_ones():
    """The agents-side copy holds exactly the 3 Diagnose calibration lines (the freeze check
    compares them with the frozen artifact hashes)."""
    frozen = calibrators.frozen_hashes()
    assert sorted(frozen) == [f"state/models/diagnose-calibration-L{n}.json" for n in (1, 2, 3)]
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in frozen.values())


# ---------------------------------------------------------------- the orchestrator's guard


def build(tmp_path, name="L3", **kw):
    db = tmp_path / f"{name}.db"
    audit = AuditLog(db, level=name)
    return Orchestrator(
        cfg=level(name),
        record=IncidentRecord(db),
        executor=Executor(audit=audit, dry_run=True),
        audit=audit,
        approver=SimulatedApprover(policy=lambda p: False),
        diagnose=Diagnose.without_logs(),
        **kw,
    )


@pytest.mark.parametrize("name", ["L1", "L2", "L3", "L4", "L5"])
def test_the_guard_refuses_l1_and_up_without_fitted_calibrators(tmp_path, name):
    with pytest.raises(calibrators.CalibrationRefused, match="no fitted calibrator"):
        build(tmp_path, name)
    empty = {a: ConformalCalibrator() for a in calibrators.CALIBRATED}  # not fitted
    with pytest.raises(calibrators.CalibrationRefused, match="no fitted calibrator"):
        build(tmp_path, name, calibrators=empty)
    build(tmp_path, name, calibrators=fitted_all())


def test_l0_is_exempt_from_the_guard(tmp_path):
    build(tmp_path, "L0")


def test_every_medium_tier_agent_is_classified():
    """Diagnose and Release calibrated; the Optimizer exempt (D67). A new medium-tier agent
    fails here until it is classified."""
    assert set(calibrators.CALIBRATED) == {AgentName.DIAGNOSE, AgentName.RELEASE}
    assert set(calibrators.EXEMPT) == {AgentName.OPTIMIZER}
    assert calibrators.medium_tier_agents() == set(calibrators.CALIBRATED) | set(calibrators.EXEMPT)


def test_an_unclassified_medium_tier_agent_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(calibrators, "EXEMPT", {})
    with pytest.raises(calibrators.CalibrationRefused, match="neither calibrated nor exempt"):
        build(tmp_path, "L3", calibrators=fitted_all())


# ---------------------------------------------------------------- confidence reaches the gate


def scored(cls, scores):
    def answer(log, examples=None):
        return {"root_cause": cls, "scores": scores, "action": "pin_dependency",
                "quotes": [log.splitlines()[0]], "evidence_lines": [1],
                "ungrounded": 0}  # fmt: skip

    return answer


class NoExamples:
    def nearest(self, log):
        return []


def test_a_pin_with_a_fitted_calibrator_gets_its_conformal_confidence(tmp_path):
    """Before D67 every Diagnose confidence was 0. Fitted on scores where the true class is
    clearly ahead, a confident pin passes rule 6; an ambiguous one does not."""
    cal = ConformalCalibrator().fit(
        [{"dependency": 0.9 - i / 200, "code": 0.05 + i / 400} for i in range(40)],
        ["dependency"] * 40,
    )
    cals = {**fitted_all(), AgentName.DIAGNOSE: cal}
    clear = {"dependency": 0.92, "code": 0.02, "environment": 0.02, "flaky_test": 0.02,
             "infrastructure": 0.02}  # fmt: skip
    vague = {"dependency": 0.4, "code": 0.35, "environment": 0.1, "flaky_test": 0.1,
             "infrastructure": 0.05}  # fmt: skip
    outcomes = {}
    for name, scores in (("clear", clear), ("vague", vague)):
        orch = build(tmp_path / name, "L3", calibrators=cals)
        orch.agents[AgentName.DIAGNOSE] = Diagnose(
            scored("dependency", scores), retriever=NoExamples()
        )
        (decision,) = orch.handle(ci_event(RUN, JOBS, 1.8e9, log=LOG))["decisions"]
        outcomes[name] = decision
    assert (
        outcomes["clear"].outcome == "execute"
    )  # medium tier, confident: no human (rule 6 passed)
    assert outcomes["vague"].outcome == "ask_human"
    rows = build(tmp_path / "rows", "L3", calibrators=cals)
    conf = rows.calibrators[AgentName.DIAGNOSE].confidence(clear)[1]
    assert conf >= 0.70 > rows.calibrators[AgentName.DIAGNOSE].confidence(vague)[1]


# ---------------------------------------------------------------- every entry point


def test_the_service_asks_the_loader_for_its_level(fitted_levels, tmp_path):
    from flowguard.service import make_orchestrator

    orch = make_orchestrator("L3", AuditLog(tmp_path / "a.db", level="L3"), approver=None)
    assert fitted_levels == ["L3"] and orch.calibrators[AgentName.DIAGNOSE].calibrated


# ---------------------------------------------------------------- L4 and L5 ask what L3 asks


def test_diagnoses_request_is_the_same_at_l3_l4_and_l5(tmp_path):
    """D67: L4 and L5 use L3's calibration because their Diagnose requests are L3's."""
    client = llm.LLMClient(api_key="unused", count=lambda s: -(-len(s) // 3))
    examples = [diagnose.Example("x" * 50, "dependency", "pin_dependency", id="dependency-02")]

    class Retriever:
        def nearest(self, log):
            return examples

    sent = {}
    for name in ("L3", "L4", "L5"):
        seen = []

        def complete(messages, request=None, _seen=seen):
            _seen.append(messages)
            return {
                "content": json.dumps({"root_cause": "dependency", "evidence": [], "scores": {}})
            }

        complete.__self__ = client
        orch = build(tmp_path / name, name, calibrators=fitted_all())
        orch.agents[AgentName.DIAGNOSE] = Diagnose(
            diagnose.answerer(complete), retriever=Retriever()
        )
        orch.handle(ci_event(RUN, JOBS, 1.8e9, log=LOG))
        (msgs,) = seen
        sent[name] = (json.dumps(msgs, sort_keys=True).encode(), client.key(msgs, None))
    assert sent["L3"] == sent["L4"] == sent["L5"]


# ---------------------------------------------------------------- example ids


def test_example_ids_are_recorded_and_never_sent():
    with_ids = [
        diagnose.Example("case one", "code", "open_fix_pr", id="code-01", distance=0.0662291),
        diagnose.Example(
            "case two", "dependency", "pin_dependency", id="dependency-02.v2", distance=0.2
        ),
    ]
    without = [diagnose.Example(e.log, e.root_cause, e.action) for e in with_ids]
    fits = llm.LLMClient(api_key="unused", count=len).fits
    msgs, req = diagnose.request(LOG, with_ids, fits, len)
    plain, _ = diagnose.request(LOG, without, fits, len)
    assert (
        json.dumps(msgs).encode()
        == json.dumps(plain).encode()
        == json.dumps(diagnose.messages(LOG, without)).encode()
    )
    assert "code-01" not in json.dumps(msgs) and "dependency-02" not in json.dumps(msgs)
    assert req["examples"] == [
        {"id": "code-01", "distance": 0.066229},
        {"id": "dependency-02.v2", "distance": 0.2},
    ]


def test_the_index_returns_its_variant_ids_and_distances():
    from flowguard.retrieval import Retriever

    class Collection:
        def query(self, query_texts, n_results, where):
            return {"ids": [["code-01", "env-02"]], "documents": [["a", "b"]],
                    "metadatas": [[{"root_cause": "code", "action": "open_fix_pr"},
                                   {"root_cause": "environment", "action": "open_fix_pr"}]],
                    "distances": [[0.1, 0.3]]}  # fmt: skip

    r = Retriever.__new__(Retriever)
    r.collection = Collection()
    got = r.nearest("log", template="t")
    assert [(e.id, e.distance) for e in got] == [("code-01", 0.1), ("env-02", 0.3)]


def test_the_call_records_its_example_ids_in_the_audit_row(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Groq(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 (http.server API)
            self.rfile.read(int(self.headers["Content-Length"]))
            body = {"choices": [{"message": {"content": '{"root_cause": "code"}'}}],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 1,
                              "total_tokens": 10}}  # fmt: skip
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Groq)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        audit = AuditLog(tmp_path / "audit.db", "s-1", "L3")
        client = llm.LLMClient(
            api_key="k", count=len, audit=audit, cache_dir=tmp_path / "c",
            ledger=tmp_path / "u.json",
            api=f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions",
        )  # fmt: skip
        answer = diagnose.answerer(client.complete)
        answer(LOG, [diagnose.Example("case", "code", "open_fix_pr", id="code-01", distance=0.1)])
    finally:
        httpd.shutdown()
        httpd.server_close()
    (row,) = list(audit.events("llm_call"))
    payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    assert payload["examples"] == [{"id": "code-01", "distance": 0.1}]
