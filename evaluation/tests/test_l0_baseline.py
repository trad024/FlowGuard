"""DSO1 L0 baseline (D50): recordings without alert states are excluded, not counted as misses;
the sensitivity result leaves out incidents that started under leftover alerts."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import l0_baseline  # noqa: E402

START = "2026-10-28T10:00:00Z"
T0 = l0_baseline.ts(START)


def recording(tmp_path, name, firing_at=None):
    rec = tmp_path / name
    (rec / "prometheus").mkdir(parents=True)
    if firing_at is not None:
        series = [
            {
                "metric": {"alertname": "HighErrorRatio", "alertstate": "firing"},
                "values": [[T0 + s, "1"] for s in firing_at],
            }
        ]
        (rec / "prometheus" / "alerts.json").write_text(
            json.dumps({"result": series}), encoding="utf-8"
        )
    return {
        "family": "runtime",
        "recording": str(rec),
        "spec": {},
        "ground_truth": {"band": "near", "incident": {"start": START, "end": START}},
    }


def test_recordings_without_alert_states_are_excluded_and_leftovers_reported(tmp_path):
    runs = [
        recording(tmp_path, "r-old", firing_at=None),  # before the alert states were recorded
        recording(tmp_path, "r-hit", firing_at=[30]),
        recording(tmp_path, "r-miss", firing_at=[]),
        recording(tmp_path, "r-left", firing_at=[-30, 0, 30]),  # still firing at injection
    ]
    out = l0_baseline.dso1(runs)
    assert out["excluded_no_alert_states"] == ["r-old"]
    assert (out["recall"]["k"], out["recall"]["n"]) == (2, 3)
    sens = out["sensitivity_without_leftover_alerts"]
    assert sens["excluded"] == ["r-left"]
    assert (sens["recall"]["k"], sens["recall"]["n"]) == (1, 2)
