"""J1: the held-out driver on synthetic specs. Run order, the mode per family, resumption
(valid recorded runs skipped, invalid or interrupted runs run again), refusal without
--confirm-step-11. Fake runners: no cluster, no network."""

import pytest
import yaml

import groundtruth
import pilot

FAMILIES = {"h-rel": "release", "h-ci": "ci", "h-run": "runtime", "h-pip": "pipeline",
            "h-sec": "security", "h-rel2": "release"}  # fmt: skip


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """Synthetic specs and a run order that is not their sorted order."""
    for sid, family in FAMILIES.items():
        spec = {"id": sid, "family": family, "fault": {"kind": "x", "delivery": "commit"},
                "ground_truth": {"expected_l0": "detect"}}  # fmt: skip
        (tmp_path / f"{sid}.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    order = ["h-run", "h-rel", "h-ci", "h-sec", "h-pip", "h-rel2"]
    (tmp_path / "run-order.txt").write_text("".join(i + "\n" for i in order), encoding="utf-8")
    monkeypatch.setattr(pilot.runner, "load_spec", lambda p: yaml.safe_load(p.read_text()))
    return tmp_path


class FakeRun:
    """Stands in for runner.Runner: records how it was built and what it would record."""

    made: list = []
    observe: dict = {}  # spec id -> list of observed dicts, one per attempt
    fail: set = set()

    def __init__(self, spec, mode="l0", ci_agents="off", recordings=None):
        self.spec, self.mode, self.ci_agents, self.recordings = spec, mode, ci_agents, recordings
        self.observed = {}
        FakeRun.made.append((spec["id"], mode, ci_agents))

    def run(self):
        if self.spec["id"] in FakeRun.fail:
            raise RuntimeError("cluster went away")
        queue = FakeRun.observe.get(self.spec["id"], [])
        self.observed = queue.pop(0) if queue else {}
        return self.recordings / f"rec-{self.spec['id']}"


@pytest.fixture
def fake(monkeypatch, tmp_path):
    FakeRun.made, FakeRun.observe, FakeRun.fail = [], {}, set()
    monkeypatch.setattr(pilot.runner, "Runner", FakeRun)
    modes = []
    truth = groundtruth.GroundTruthDB(tmp_path / "truth.db")
    return {"modes": modes, "truth": truth, "set_mode": modes.append, "rec": tmp_path / "rec"}


def drive(folder, fake, **kw):
    return pilot.run_heldout(
        pilot.run_order(folder), recordings=fake["rec"], truth=fake["truth"],
        set_mode=fake["set_mode"], say=lambda line: None, **kw,
    )  # fmt: skip


def test_specs_run_in_the_run_order(folder, fake):
    out = drive(folder, fake)
    assert [line["spec"] for line in out] == ["h-run", "h-rel", "h-ci", "h-sec", "h-pip", "h-rel2"]
    assert [m[0] for m in FakeRun.made] == ["h-run", "h-rel", "h-ci", "h-sec", "h-pip", "h-rel2"]


def test_each_family_runs_in_its_mode(folder, fake):
    drive(folder, fake)
    made = {sid: (mode, ci) for sid, mode, ci in FakeRun.made}
    assert made["h-rel"] == ("agents", "off") and made["h-rel2"] == ("agents", "off")  # D54
    assert made["h-ci"] == ("l0", "live")  # D64: live at L5
    assert made["h-run"] == made["h-sec"] == made["h-pip"] == ("l0", "off")
    # Staging's Rollout: agents for releases, L0 for the families the deployer deploys; set
    # only when it changes; runtime leaves it as it is
    assert fake["modes"] == ["agents", "l0", "agents"]


def test_the_canary_mode_per_family_is_classified():
    assert pilot.CANARY_MODE == {"release": "agents", "pipeline": "l0", "security": "l0",
                                 "runtime": None, "ci": None}  # fmt: skip


def record(truth, run_id, spec_id, family, valid, folder, mode=None):
    observed = {"mode": mode} if mode else {}
    truth.record_run(run_id, spec={"id": spec_id, "family": family}, ground_truth={},
                     observed=observed, l0_correct=None, valid=valid,
                     recording=str(folder))  # fmt: skip


def test_valid_recorded_runs_are_skipped_and_the_rest_run_again(folder, fake, tmp_path):
    there = tmp_path / "recorded"
    there.mkdir()
    truth = fake["truth"]
    record(truth, "r-1", "h-run", "runtime", True, there)  # valid: skipped
    record(truth, "r-2", "h-rel", "release", True, there, mode="agents")  # valid: skipped
    record(truth, "r-3", "h-sec", "security", False, there)  # invalid: run again
    record(truth, "r-4", "h-pip", "pipeline", True, tmp_path / "gone")  # folder lost: again
    record(truth, "r-5", "h-rel2", "release", True, there)  # not under agents mode: again
    out = drive(folder, fake)
    skipped = [line["spec"] for line in out if "skipped" in line]
    assert skipped == ["h-run", "h-rel"]
    assert [m[0] for m in FakeRun.made] == ["h-ci", "h-sec", "h-pip", "h-rel2"]


def test_a_failed_run_does_not_stop_the_set_and_runs_again_next_time(folder, fake):
    FakeRun.fail = {"h-ci"}
    out = drive(folder, fake)
    failed = [line for line in out if "failed" in line]
    assert [line["spec"] for line in failed] == ["h-ci"] and len(out) == 6
    FakeRun.fail, FakeRun.made = set(), []
    for sid, family in FAMILIES.items():  # every other spec now has a valid recording
        if sid != "h-ci":
            folder_ = fake["rec"] / f"rec-{sid}"
            folder_.mkdir(parents=True, exist_ok=True)
            mode = "agents" if family == "release" else None
            record(fake["truth"], f"r-{sid}", sid, family, True, folder_, mode)
    drive(folder, fake)
    assert [m[0] for m in FakeRun.made] == ["h-ci"]  # only the interrupted one


def test_alerts_not_quiet_is_retried_up_to_the_limit(folder, fake):
    FakeRun.observe = {"h-run": [{"alerts_not_quiet": ["HighErrorRatio"]}, {}]}
    out = drive(folder, fake)
    assert next(line for line in out if line["spec"] == "h-run")["attempt"] == 2
    assert [m[0] for m in FakeRun.made].count("h-run") == 2


def test_run_order_names_only_existing_specs(folder):
    (folder / "run-order.txt").write_text("h-run\nh-missing\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="missing"):
        pilot.run_order(folder)


def test_refused_without_confirmation_before_any_check(monkeypatch):
    import heldout

    called = []
    for name in ("frozen_agents", "diagnose_frozen", "checked_seed"):
        monkeypatch.setattr(heldout, name, lambda *a, n=name, **k: called.append(n))
    with pytest.raises(SystemExit, match="--confirm-step-11"):
        pilot.require_heldout(False)
    assert called == []
    pilot.require_heldout(True)  # confirmed: every frozen-state check runs
    assert called == ["frozen_agents", "diagnose_frozen", "checked_seed"]


def test_without_eval_freeze_the_confirmed_run_still_refuses(monkeypatch):
    import heldout

    def no_tag(*a, **k):
        raise SystemExit("tag the agent code eval-freeze before any held-out run")

    monkeypatch.setattr(heldout, "frozen_agents", no_tag)
    with pytest.raises(SystemExit, match="eval-freeze"):
        pilot.require_heldout(True)


def test_the_cli_refuses_without_confirmation(monkeypatch):
    monkeypatch.setattr(pilot, "run_heldout", lambda *a, **k: pytest.fail("ran"))
    with pytest.raises(SystemExit, match="--confirm-step-11"):
        pilot.main(["heldout"])
