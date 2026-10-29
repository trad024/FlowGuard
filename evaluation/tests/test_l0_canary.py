"""Offline L0 canary analysis (D54): the frozen template's settings and queries, Prometheus'
rate and histogram_quantile, Argo's measurement rules, and the reproduction of L0's live
outcome on the dev release recordings (when they are on this machine)."""

import math
import os
from pathlib import Path

import pytest

import l0_canary as l0
import release_replay as rr
from flowguard import series

T = 1_800_000_000.0
ROOT = Path(__file__).resolve().parents[2]


def test_settings_come_from_the_frozen_template_and_its_queries_are_the_ones_evaluated():
    metrics = {m.name: m for m in l0.load_template()}
    assert {(m.kind, m.interval_s, m.count, m.failure_limit) for m in metrics.values()} == {
        ("ratio", 30.0, 4, 1),
        ("p99", 30.0, 4, 1),
    }
    assert metrics["error-ratio-5xx"].threshold == 0.01
    assert metrics["latency-p99-seconds"].threshold == 0.5
    # If the frozen queries change, this evaluator no longer reproduces them
    text = " ".join(l0.TEMPLATE.read_text().split())
    canary = (
        'namespace="{{args.namespace}}",app="podinfo",path="root",'
        'rollouts_pod_template_hash="{{args.canary-hash}}"'
    )
    assert (
        f'( sum(rate(http_request_duration_seconds_count{{{canary},status=~"5.."}}[1m])) '
        f"or vector(0) ) / sum(rate(http_request_duration_seconds_count{{{canary}}}[1m]))"
    ) in text
    assert (
        "histogram_quantile(0.99, sum by (le) "
        f"(rate(http_request_duration_seconds_bucket{{{canary}}}[1m])))"
    ) in text


def counter(points, **labels):
    return series.Series(labels, [T + t for t, _ in points], [float(v) for _, v in points])


@pytest.mark.parametrize(
    ("points", "expected"),
    [
        # 4 samples 15 s apart, the first 15 s after the range start: extrapolated to 60 s
        ([(-45, 100), (-30, 115), (-15, 130), (0, 145)], 1.0),
        # a reset adds the value before it: 15 + 5 + 15 over 45 s, extrapolated
        ([(-45, 100), (-30, 115), (-15, 5), (0, 20)], 35 / 45 * 60 / 60),
        # a new counter is not extrapolated below zero: 3 s to its zero point, not 15
        ([(-45, 3), (-30, 18), (-15, 33), (0, 48)], 45 * (48 / 45) / 60),
        # a gap at the start beyond 1.1 average intervals: half an interval only
        ([(-15, 10), (0, 25)], 15 * (22.5 / 15) / 60),
        # the range is left-open: the sample at exactly t - 60 s is not in it
        ([(-60, 0), (0, 60)], None),
    ],
)
def test_rate_extrapolates_as_prometheus(points, expected):
    got = l0.rate(counter(points), T)
    assert got == pytest.approx(expected) if expected is not None else got is None


@pytest.mark.parametrize(
    ("buckets", "expected"),
    [
        ({0.1: 50, 0.5: 98, 1.0: 100, math.inf: 100}, 0.75),  # inside (0.5, 1]: interpolated
        ({0.005: 99.5, 0.5: 100, math.inf: 100}, 0.005 * (99 / 99.5)),  # lowest bucket from 0
        ({0.5: 50, math.inf: 100}, 0.5),  # in +Inf: the highest finite bound
        ({0.5: 50, 1.0: 100}, math.nan),  # no +Inf bucket
        ({0.5: 0, math.inf: 0}, math.nan),  # no observations
    ],
)
def test_histogram_quantile_as_prometheus(buckets, expected):
    got = l0.histogram_quantile(0.99, buckets)
    assert math.isnan(got) if math.isnan(expected) else got == pytest.approx(expected)


def requests(h, status, points, path="root"):
    labels = {"namespace": "staging", "app": "podinfo", "path": path, "status": status,
              "rollouts_pod_template_hash": h}  # fmt: skip
    return {"metric": labels, "values": [[T + t, str(v)] for t, v in points]}


def test_error_ratio_query_edge_cases():
    up = [(-45, 0), (-30, 15), (-15, 30), (0, 45)]
    clean = series.index({"podinfo_requests": [requests("new", "200", up)]})
    assert l0.error_ratio(clean, T, "staging", "new") == [0.0]  # no 5xx series: or vector(0)
    assert l0.error_ratio(clean, T, "staging", "other") == []  # no canary series: no data
    flat = series.index({"podinfo_requests": [requests("new", "200", [(-30, 5), (0, 5)])]})
    (nan,) = l0.error_ratio(flat, T, "staging", "new")  # no request: 0 / 0
    assert math.isnan(nan)
    metric = next(m for m in l0.load_template() if m.kind == "ratio")
    assert not l0.measure(metric, clean, T, "staging", "other")  # no data fails
    assert not l0.measure(metric, flat, T, "staging", "new")  # NaN fails
    errors = series.index(
        {
            "podinfo_requests": [
                requests("new", "200", up),
                requests("new", "500", [(-45, 0), (-30, 1), (-15, 1), (0, 2)]),
                requests("new", "200", [(-45, 0), (0, 900)], path="readyz"),  # not counted
            ]
        }
    )
    assert l0.error_ratio(errors, T, "staging", "new") == [pytest.approx(2 / 47)]


def test_the_second_failed_measurement_aborts(monkeypatch):
    metrics = l0.load_template()
    bad_from = {"value": T + 40}
    monkeypatch.setitem(
        l0.QUERIES, "ratio", lambda idx, t, ns, c: [0.05 if t >= bad_from["value"] else 0.0]
    )
    monkeypatch.setitem(l0.QUERIES, "p99", lambda idx, t, ns, c: [0.01])
    run = l0.analysis({}, T, "staging", "new", metrics=metrics)
    assert run["outcome"] == "failed" and run["t"] == T + 90  # failures at 60 and 90 s
    assert run["metric"] == "error-ratio-5xx"
    bad_from["value"] = T + 80  # one failure only (at 90 s): within failureLimit 1
    assert l0.analysis({}, T, "staging", "new", metrics=metrics)["outcome"] == "successful"
    bad_from["value"] = T  # would fail at 30 s, but the recording ends at 20 s
    assert l0.analysis({}, T, "staging", "new", T + 20, metrics)["outcome"] == "truncated"


def dev_recordings() -> Path | None:
    for candidate in (
        os.environ.get("FLOWGUARD_DEV_RECORDINGS"),
        ROOT / "evaluation" / "recordings",
    ):
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return None


@pytest.mark.skipif(dev_recordings() is None, reason="dev recordings are not on this machine")
def test_offline_l0_reproduces_the_live_outcome_on_the_dev_recordings():
    runs = rr.release_runs([dev_recordings()])
    metrics = l0.load_template()
    decided, truncated = [], []
    for r in runs:
        idx = rr.load_index(r)
        live = rr.l0_live(r, idx)
        if live is None:
            continue
        offline = rr.l0_offline(r, idx, metrics)
        if offline["outcome"] == "truncated":
            truncated.append(r["id"])  # the recording ends before the deciding measurement
        else:
            decided.append((r["id"], offline["outcome"], live["outcome"]))
    assert decided and all(o == lv for _, o, lv in decided), decided
    assert len(decided) >= 0.9 * (len(decided) + len(truncated))
