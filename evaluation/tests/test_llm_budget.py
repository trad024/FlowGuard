"""LLM budget measurement (D27): log reduction and the quota projection. No network."""

import llm_budget


def test_reduction_keeps_errors_drops_cleanup_and_fits_the_budget():
    lines = [f"2026-10-27T20:31:{i % 60:02d}.0000000Z step {i}" for i in range(2000)]
    lines[1500] = "2026-10-27T20:31:00.0000000Z ERROR: cannot find module providing package x"
    lines[1990] = "2026-10-27T20:31:00.0000000Z Post job cleanup."
    out = llm_budget.reduce_log("\n".join(lines))
    assert len(out) <= llm_budget.LOG_BUDGET_CHARS
    assert "1501: ERROR: cannot find module" in out  # numbered, timestamp removed
    assert "Post job cleanup" not in out and "step 1995" not in out


def test_projection_is_limited_by_tokens_or_requests():
    p = llm_budget.project(1000, 1269, tokens_per_day=200_000, requests_per_day=1000)
    assert p == {"calls": 1000, "days_by_tokens": 7.0, "days_by_requests": 1.1, "days": 8}
    q = llm_budget.project(1000, 100, tokens_per_day=200_000, requests_per_day=100)
    assert q["days"] == 12  # requests bind when calls are small
