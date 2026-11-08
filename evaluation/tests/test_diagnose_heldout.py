"""K1, K2 (D69 stage A): the held-out Diagnose runner and the D61 repeats, on synthetic data.
Refusal without each condition; test templates included; LogChunks items built from the
sample's targets; resumption through outputs and the cache; no score field; the records and
truth rows accepted by the report toolkit's loader; blind progress. A fake client, no network,
no real seed, nothing from scenarios/heldout/."""

import csv
import hashlib
import json
import threading

import pytest

import diagnose_dev
import diagnose_heldout as dh
import diagnose_repeats
import groundtruth
import report_data
from flowguard import diagnose, lanes
from flowguard.audit import AuditLog

LANES = [lanes.Lane("GROQ_KEY_1", "groq", "k1"), lanes.Lane("LLM_API_KEY", "groq", "k2")]


# ---------------------------------------------------------------- fakes and fixtures


class FakeClient:
    """LLMClient's surface the runners use: fits, count, key, complete; a shared cache."""

    cache: dict = {}
    sent: list = []

    def __init__(self, lane, level):
        self.lane, self.provider = lane.name, "groq"
        self.count = len

    def fits(self, messages):
        return True

    def key(self, messages, repeat):
        body = json.dumps({"messages": messages, "repeat": repeat}, sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()

    def complete(self, messages, repeat=None, request=None):
        k = self.key(messages, repeat)
        if k in FakeClient.cache:
            return {**FakeClient.cache[k], "cached": True}
        FakeClient.sent.append((messages, repeat, request))
        first = messages[1]["content"].split("Failed job log:\n", 1)[1].splitlines()[0]
        quote = first.split(": ", 1)[1]
        answer = {"root_cause": "dependency", "action": "pin_dependency", "fix": "pin it",
                  "evidence": [quote, "a line that is not in the log"],
                  "scores": {"dependency": 0.9, "code": 0.1}}  # fmt: skip
        res = {"content": json.dumps(answer), "lane": self.lane, "provider": "groq",
               "model": "openai/gpt-oss-120b",
               "usage": {"total_tokens": 100}}  # fmt: skip
        FakeClient.cache[k] = res
        return {**res, "cached": False}


class Retriever:
    def nearest(self, log, template=None):
        return [diagnose.Example("an example log", "code", "open_fix_pr", id="code-01",
                                 distance=0.1)]  # fmt: skip


class L1:
    def scores(self, log):
        return {"code": 0.2, "dependency": 0.8}


LOG = "go: downloading example.com/x v1.0.0\ngo: example.com/x@v9.9.9: invalid version\nFAIL\n"


@pytest.fixture
def data(tmp_path):
    """A split with train and test templates, their corpus logs, and a LogChunks sample."""
    FakeClient.cache, FakeClient.sent = {}, []
    split = {"templates": {"t-train": "train", "t-test": "test", "u-test": "test"},
             "variants": {"a": {"template": "t-train", "root_cause": "code"},
                          "b": {"template": "t-test", "root_cause": "dependency"},
                          "b.v2": {"template": "t-test", "root_cause": "dependency"},
                          "c": {"template": "u-test", "root_cause": "environment"},
                          "d": {"template": "u-test", "root_cause": "environment"}}}  # fmt: skip
    corpus = tmp_path / "corpus"
    for v in ("a", "b", "b.v2", "c"):  # d has no verified log
        (corpus / v).mkdir(parents=True)
        (corpus / v / "run.log").write_text(LOG + f"variant {v}\n", encoding="utf-8")
    raw = tmp_path / "LogChunks"
    rows = []
    for n, status in enumerate(["used", "not_visible", "used"]):
        text = f"step {n}\nerror: something broke {n}\nexit 1\n"
        path = raw / "logs" / f"repo{n}" / "log.txt"
        path.parent.mkdir(parents=True)
        path.write_bytes(text.encode())
        rows.append({"rank": n + 1, "log_path": f"repo{n}/log.txt", "status": status,
                     "target_lines": "2 3", "visible_target_lines": "2 3",
                     "l2_request_chars": 10,
                     "sha256": hashlib.sha256(text.encode()).hexdigest()})  # fmt: skip
    sample = tmp_path / "sample.csv"
    with sample.open("w", encoding="utf-8", newline="\n") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    items = dh.corpus_items(split, corpus) + dh.logchunks_items(sample, raw)
    out = tmp_path / "out"
    out.mkdir()
    return {"split": split, "corpus": corpus, "raw": raw, "sample": sample, "items": items,
            "out": out, "rows": rows}  # fmt: skip


# ---------------------------------------------------------------- guards


@pytest.fixture
def checks(monkeypatch):
    called = []
    monkeypatch.setattr(dh.heldout_guard.heldout, "diagnose_frozen",
                        lambda *a: called.append("diagnose_frozen"))  # fmt: skip
    monkeypatch.setattr(dh.heldout, "harness_diagnose_frozen",
                        lambda *a: called.append("harness"))  # fmt: skip
    return called


def test_refused_without_confirmation_before_any_freeze_check(checks):
    with pytest.raises(SystemExit, match="--confirm-step-11"):
        dh.require(False)
    assert checks == []


def test_refused_without_the_diagnose_freeze(checks, monkeypatch):
    def thawed(*a):
        raise SystemExit("agents/flowguard/diagnose.py changed since diagnose-freeze")

    monkeypatch.setattr(dh.heldout_guard.heldout, "diagnose_frozen", thawed)
    with pytest.raises(SystemExit, match="diagnose-freeze"):
        dh.require(True)
    assert "harness" not in checks


def test_refused_without_the_harness_freeze(checks, monkeypatch):
    def untagged(*a):
        raise SystemExit("tag the agent code harness-diagnose-freeze before any held-out run")

    monkeypatch.setattr(dh.heldout, "harness_diagnose_frozen", untagged)
    with pytest.raises(SystemExit, match="harness-diagnose-freeze"):
        dh.require(True)


def test_all_three_conditions_let_it_start(checks):
    dh.require(True)
    assert checks == ["diagnose_frozen", "harness"]


def test_the_clis_refuse_before_reading_anything(monkeypatch):
    monkeypatch.setattr(dh, "corpus_items", lambda *a: pytest.fail("read the corpus"))
    for main in (dh.main, diagnose_repeats.main):
        with pytest.raises(SystemExit, match="--confirm-step-11"):
            main(["run"])


# ---------------------------------------------------------------- items


def test_test_templates_are_included_and_training_ones_are_not(data):
    corpus = [i for i in data["items"] if i["set"] == "corpus"]
    assert [i["id"] for i in corpus] == ["b", "b.v2", "c"]  # d has no verified log
    assert {i["template"] for i in corpus} == {"t-test", "u-test"}
    assert corpus[0]["truth"] == {"root_cause": "dependency"}


def test_logchunks_items_come_from_the_samples_targets(data):
    chunks = [i for i in data["items"] if i["set"] == "logchunks"]
    assert len(chunks) == 2  # only `used` rows
    used = [r for r in data["rows"] if r["status"] == "used"]
    assert [i["id"] for i in chunks] == [f"lc-{r['sha256'][:12]}" for r in used]
    assert all(i["truth"] == {"localizable": True, "relevant_lines": [2, 3]} for i in chunks)
    assert chunks[0]["template"] is None


def test_a_changed_logchunks_log_refuses(data):
    (data["raw"] / "logs" / "repo0" / "log.txt").write_text("edited\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="changed since the sample was drawn"):
        dh.logchunks_items(data["sample"], data["raw"])


# ---------------------------------------------------------------- the run


def run_level(data, level, subset=frozenset(), repeat=None, progress=None):
    return dh.run_llm(level, data["items"], data["out"], LANES, FakeClient, set(subset),
                      Retriever(), progress, repeat=repeat)  # fmt: skip


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x]


def test_resume_skips_written_items_and_the_cache_answers_again(data):
    assert run_level(data, "L2") == 5
    first = len(FakeClient.sent)
    assert first == 5
    assert run_level(data, "L2") == 0  # every item written: nothing asked, nothing added
    assert len(FakeClient.sent) == first and len(lines(data["out"] / "L2.jsonl")) == 5
    (data["out"] / "L2.jsonl").unlink()  # interrupted before writing: the cache answers
    assert run_level(data, "L2") == 5
    assert len(FakeClient.sent) == first
    assert all(r["cached"] for r in lines(data["out"] / "L2-raw.jsonl")[5:])


def test_each_item_stays_on_its_seeded_lane(data):
    run_level(data, "L2")
    raw = {r["scenario_id"]: r["lane"] for r in lines(data["out"] / "L2-raw.jsonl")}
    for item in data["items"]:
        assert raw[item["id"]] == lanes.assign(item["id"], LANES, dh.LANE_SEED).name


def test_each_call_records_lane_cache_key_cut_and_example_ids(data):
    run_level(data, "L3")
    for r in lines(data["out"] / "L3-raw.jsonl"):
        assert r["lane"] and len(r["cache_key"]) == 64 and r["repeat"] is None
        assert r["request"]["log_cut"] is False
        assert r["request"]["examples"] == [{"id": "code-01", "distance": 0.1}]
        assert r["diagnose_quotes"]  # the model's raw quotes, kept


FORBIDDEN = ("score", "correct", "accuracy", "coverage", "f1", "rate")


def keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from keys(v)


def test_no_score_field_is_written(data):
    run_level(data, "L2")
    run_level(data, "L3")
    dh.run_classical("L0", data["items"], data["out"])
    dh.run_classical("L1", data["items"], data["out"], L1())
    for path in data["out"].glob("*.jsonl"):
        for row in lines(path):
            bad = [k for k in keys(row) if any(w in k.lower() for w in FORBIDDEN)]
            assert bad == [], (path.name, bad)


def test_progress_is_blind(data):
    said = []
    progress = dh.Progress(said.append)
    run_level(data, "L2", progress=progress)
    shown = json.loads(said[-1])
    assert set(shown) == {"level", "progress"}
    for lane in shown["progress"].values():
        assert set(lane) <= {"calls", "cached", "failed", "tokens"}
    assert "dependency" not in said[-1]


def test_the_toolkit_loads_the_records_and_truth_rows(data, tmp_path):
    run_level(data, "L2")
    run_level(data, "L3", subset={"b"})
    dh.run_classical("L0", data["items"], data["out"])
    dh.run_classical("L1", data["items"], data["out"], L1())
    truth = groundtruth.GroundTruthDB(data["out"] / "groundtruth.db")
    dh.record_truth(truth, data["items"])
    dh.record_truth(truth, data["items"])  # idempotent on resume
    replay = tmp_path / "replay.jsonl"
    replay.write_text("".join(p.read_text() for p in sorted(data["out"].glob("L?.jsonl"))))
    audit = tmp_path / "audit.db"
    AuditLog(audit)  # an empty audit log: no live outcomes for offline items
    joined, summary = report_data.load(audit, data["out"] / "groundtruth.db", replay)
    assert summary["replay_records"] == 2 * 5 + 2 * 3  # L2, L3 on both sets; L0, L1 corpus only
    assert summary["truth_without_replay"] == []
    methods = {(r["level"], r["method"]) for r in joined}
    assert methods == {("L0", "regex"), ("L1", "tfidf_lr"), ("L2", "llm_zero_shot"),
                       ("L3", "llm_retrieval")}  # fmt: skip
    l2 = [r for r in joined if r["level"] == "L2"]
    assert all(r["observed"]["diagnose_quotes"] == 2 for r in l2)
    assert all(r["observed"]["diagnose_ungrounded"] == 1 for r in l2)
    subset = [r for r in joined if r["observed"].get("llm_repeat_subset")]
    assert [(r["scenario_id"], r["level"]) for r in subset] == [("b", "L3")]


def test_the_requests_are_the_dev_runners(data):
    """The held-out runner sends exactly what diagnose_dev.ask builds for the same input."""
    run_level(data, "L3")
    sent = {json.dumps(m) for m, _, _ in FakeClient.sent}
    client = FakeClient(LANES[0], "L3")
    for item in data["items"]:
        log = item["log"].read_text(encoding="utf-8", errors="replace")
        msgs, _ = diagnose.request(log, Retriever().nearest(log), client.fits, client.count)
        assert json.dumps(msgs) in sent


# ---------------------------------------------------------------- K2: the D61 repeats


def many(data, n):
    """n synthetic held-out items with distinct logs (the subset needs at least 30)."""
    out = []
    for i in range(n):
        log = data["out"].parent / "many" / f"x-{i:03d}.log"
        log.parent.mkdir(exist_ok=True)
        log.write_text(LOG + f"item {i}\n", encoding="utf-8")
        out.append({"id": f"x-{i:03d}", "set": "corpus", "template": "t-test", "log": log,
                    "truth": {"root_cause": "dependency"}})  # fmt: skip
    return out


def test_the_subset_is_seeded_sized_and_order_independent():
    ids = [f"x-{i:03d}" for i in range(242)]
    a = dh.repeat_subset("seed-a", ids)
    assert len(a) == 30 and a == dh.repeat_subset("seed-a", list(reversed(ids)))
    assert a != dh.repeat_subset("seed-b", ids)


def test_repeats_two_and_three_run_on_the_subset_with_their_own_keys(data):
    items = many(data, 40)
    out = diagnose_repeats.run(items, data["out"], LANES, FakeClient, Retriever(), "seed-a")
    assert out == {2: 30, 3: 30}
    chosen = dh.repeat_subset("seed-a", [i["id"] for i in items])
    for r in (2, 3):
        rows = lines(data["out"] / f"L3-r{r}.jsonl")
        assert {row["scenario_id"] for row in rows} == chosen
        assert all(row["repeat"] == r and row["observed"]["llm_repeat_subset"] for row in rows)
        raw = lines(data["out"] / f"L3-r{r}-raw.jsonl")
        assert all(row["repeat"] == r for row in raw)
    # the repeat index is in the cache key: 60 real calls, none answered from repeat 1's key
    assert sorted({rep for _, rep, _ in FakeClient.sent}) == [2, 3]
    assert len(FakeClient.sent) == 60
    assert diagnose_repeats.run(items, data["out"], LANES, FakeClient, Retriever(), "seed-a") == {
        2: 0, 3: 0}  # fmt: skip


def test_lanes_run_in_parallel_threads(data, monkeypatch):
    seen = set()
    real = FakeClient.complete

    def complete(self, *a, **k):
        seen.add(threading.current_thread().name)
        return real(self, *a, **k)

    monkeypatch.setattr(FakeClient, "complete", complete)
    run_level(data, "L2")
    assert len(seen) == len(
        {lanes.assign(i["id"], LANES, dh.LANE_SEED).name for i in data["items"]}
    )


def test_diagnose_dev_keeps_its_dev_only_items():
    split = {"templates": {"t": "test"}, "variants": {"v": {"template": "t", "root_cause": "code"}}}
    assert diagnose_dev.items(split) == []  # test templates stay out of the dev runner


# ---------------------------------------------------------------- R3: the D66 cut rule for H3

import report_metrics  # noqa: E402


class CuttingClient(FakeClient):
    """A lane whose budget nothing fits: the D66 cut keeps only the log's last line."""

    def fits(self, messages):
        return False


def test_targets_shown_counts_the_target_lines_the_request_kept(data):
    chunks = [i for i in data["items"] if i["set"] == "logchunks"]
    cut = {**chunks[0], "id": "lc-cut", "truth": {"localizable": True, "relevant_lines": [1, 2]}}
    kept = chunks[1]  # targets 2 and 3
    dh.run_llm("L2", [cut, kept], data["out"], LANES, CuttingClient, set(), None,
               dh.Progress(lambda s: None))  # fmt: skip
    rows = {r["scenario_id"]: r["observed"] for r in lines(data["out"] / "L2.jsonl")}
    assert rows["lc-cut"]["targets_shown"] == 0  # only line 3 survived the cut
    assert rows[kept["id"]]["targets_shown"] == 1  # line 3 of [2, 3]
    corpus = [i for i in data["items"] if i["set"] == "corpus"][:1]
    dh.run_llm("L3", corpus, data["out"], LANES, FakeClient, set(), Retriever(),
               dh.Progress(lambda s: None))  # fmt: skip
    (row,) = lines(data["out"] / "L3.jsonl")
    assert "targets_shown" not in row["observed"]  # no target lines: nothing to count


def h3_record(level, shown, evidence, sid):
    truth = {"family": "diagnose", "spec": {}, "valid": True,
             "ground_truth": {"localizable": True, "relevant_lines": [2, 3]}}  # fmt: skip
    observed = {"evidence_lines": evidence, "targets_shown": shown}
    return {"scenario_id": sid, "level": level, "observed": observed, "truth": truth}


def test_a_cut_item_is_a_miss_and_the_sensitivity_excludes_it():
    cut = h3_record("L3", 0, [2], "a")  # would hit, but its targets were never shown
    hit = h3_record("L3", 2, [3], "b")
    miss = h3_record("L3", 1, [9], "c")
    assert report_metrics.localization(cut) is False
    assert report_metrics.localization(hit) is True and report_metrics.localization(miss) is False
    assert [report_metrics.targets_cut(r) for r in (cut, hit, miss)] == [True, False, False]
    assert report_metrics.localization_sensitivity(cut) is None
    assert report_metrics.localization_sensitivity(hit) is True


def test_the_report_gives_the_cut_share_and_the_sensitivity_per_level(data, tmp_path):
    chunks = [i for i in data["items"] if i["set"] == "logchunks"]
    cut = {**chunks[0], "id": "lc-cut", "truth": {"localizable": True, "relevant_lines": [1, 2]}}
    items = [cut, chunks[1]]
    dh.run_llm("L2", items, data["out"], LANES, CuttingClient, set(), None,
               dh.Progress(lambda s: None))  # fmt: skip
    truth = groundtruth.GroundTruthDB(data["out"] / "groundtruth.db")
    dh.record_truth(truth, items)
    AuditLog(tmp_path / "audit.db")
    joined, _ = report_data.load(tmp_path / "audit.db", data["out"] / "groundtruth.db",
                                 data["out"] / "L2.jsonl")  # fmt: skip
    m = report_metrics.measurements([r for r in joined if r["level"] == "L2"], 1)
    secondary = m.get("secondary", m)
    assert secondary["H3_targets_cut"]["k"] == 1 and secondary["H3_targets_cut"]["n"] == 2
    sens = secondary["H3_localization_hit_sensitivity"]
    assert sens["n"] == 1 and sens["label"].startswith("sensitivity")
    assert m["primary"]["H3_localization_hit"]["n"] == 2  # the cut item counts, as a miss
