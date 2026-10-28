"""Small statistics for the evaluation, standard library only.

wilson(k, n)          95% Wilson score interval for a proportion
poisson_exact(k)      95% exact (Garwood) interval for a Poisson count
mcnemar_n(p0, p1)     paired sample size for McNemar's test (Connor 1987), assuming the two
                      levels' outcomes are independent given the item (a conservative choice)
"""

from __future__ import annotations

import math
import random
from statistics import NormalDist, median

Z = NormalDist()


def wilson(k: int, n: int, level: float = 0.95) -> tuple[float, float]:
    if not 0 <= k <= n or not 0 < level < 1:
        raise ValueError("require 0 <= k <= n and 0 < level < 1")
    if n == 0:
        return (0.0, 1.0)
    z = Z.inv_cdf(1 - (1 - level) / 2)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def _poisson_cdf(k: int, lam: float) -> float:
    if lam == 0:
        return 1.0
    # Log-sum-exp avoids underflow for counts in the thousands.
    terms = [-lam + i * math.log(lam) - math.lgamma(i + 1) for i in range(k + 1)]
    largest = max(terms)
    return min(1.0, math.exp(largest) * math.fsum(math.exp(v - largest) for v in terms))


def _solve(f, lo: float, hi: float) -> float:
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(mid):
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def poisson_exact(k: int, level: float = 0.95) -> tuple[float, float]:
    """Garwood interval for the mean of a Poisson count k."""
    if type(k) is not int or k < 0 or not 0 < level < 1:
        raise ValueError("require a nonnegative integer count and 0 < level < 1")
    a = (1 - level) / 2
    lower = 0.0 if k == 0 else _solve(lambda lam: 1 - _poisson_cdf(k - 1, lam) >= a, 0.0, k + 50)
    upper = _solve(lambda lam: _poisson_cdf(k, lam) <= a, 0.0, 10 * k + 50)
    return (lower, upper)


def mcnemar_n(p0: float, p1: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """Pairs needed to detect p0 -> p1 (success rates) with a two-sided McNemar test."""
    p01, p10 = (1 - p0) * p1, p0 * (1 - p1)  # success only at the new level, only at the old
    psi, delta = p01 + p10, p01 - p10
    if delta <= 0:
        raise ValueError("p1 must be larger than p0")
    za, zb = Z.inv_cdf(1 - alpha / 2), Z.inv_cdf(power)
    n = (za * math.sqrt(psi) + zb * math.sqrt(psi - delta * delta)) ** 2 / (delta * delta)
    return math.ceil(n)


def mcnemar_exact(b: int, c: int) -> float:
    """b, c: items right only at one level, only at the other. Binomial(b + c, 1/2), two-sided."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2**n
    return min(1.0, 2 * tail)


def bootstrap_median(
    diffs: list[float], resamples: int = 10000, seed: int = 20261028, level: float = 0.95
) -> tuple[float, float] | None:
    if not diffs:
        return None
    rng = random.Random(seed)
    meds = sorted(median(rng.choices(diffs, k=len(diffs))) for _ in range(resamples))
    a = (1 - level) / 2
    return (meds[int(a * resamples)], meds[min(resamples - 1, int((1 - a) * resamples))])


def mcnemar_paired(left: list[bool], right: list[bool]) -> dict:
    """Two-sided conditional binomial test on paired discordances, no continuity correction."""
    if len(left) != len(right):
        raise ValueError("paired outcomes must have equal lengths")
    b = sum(a and not c for a, c in zip(left, right, strict=True))
    c = sum(not a and d for a, d in zip(left, right, strict=True))
    p = mcnemar_exact(b, c)
    return {"n": len(left), "left_only": b, "right_only": c, "p_value": p if left else None}


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def bootstrap_interval(items, statistic, *, resamples=10000, seed=20261028) -> dict:
    """Percentile interval; resample whole items (e.g. incidents), not individual log lines."""
    if resamples < 1:
        raise ValueError("positive resample count required")
    if not items:
        return {"n": 0, "estimate": None, "ci95": None, "resamples": resamples, "seed": seed}
    rng = random.Random(seed)
    draws = [statistic(rng.choices(items, k=len(items))) for _ in range(resamples)]
    return {
        "n": len(items),
        "estimate": statistic(items),
        "ci95": [percentile(draws, 0.025), percentile(draws, 0.975)],
        "resamples": resamples,
        "seed": seed,
    }


def paired_bootstrap(left, right, statistic, *, resamples=10000, seed=20261028) -> dict:
    """Paired percentile difference CI and a two-sided, null-centred bootstrap p-value.

    The p-value uses (1 + extreme)/(B + 1), so finite Monte Carlo runs never report zero.
    Both methods see exactly the same sampled items, preserving the paired design.
    """
    if len(left) != len(right) or resamples < 1:
        raise ValueError("aligned pairs and positive resamples required")
    result = {"n": len(left), "resamples": resamples, "seed": seed}
    if not left:
        return {**result, "estimate": None, "ci95": None, "p_value": None}
    pairs = list(zip(left, right, strict=True))
    delta = statistic(left) - statistic(right)
    rng = random.Random(seed)
    draws = []
    for _ in range(resamples):
        sample = rng.choices(pairs, k=len(pairs))
        a, b = zip(*sample, strict=True)
        draws.append(statistic(list(a)) - statistic(list(b)))
    extreme = sum(abs(v - delta) >= abs(delta) - 1e-12 for v in draws)
    return {
        **result,
        "estimate": delta,
        "ci95": [percentile(draws, 0.025), percentile(draws, 0.975)],
        "p_value": (extreme + 1) / (resamples + 1),
    }


def holm(pvalues: dict[str, float | None], alpha=0.05) -> dict:
    """Step-down Holm adjustment; missing tests remain in the preregistered family size."""
    if not 0 < alpha < 1 or any(p is not None and not 0 <= p <= 1 for p in pvalues.values()):
        raise ValueError("invalid alpha or p-value")
    ordered = sorted(pvalues, key=lambda k: 1.0 if pvalues[k] is None else pvalues[k])
    result, previous = {}, 0.0
    for rank, name in enumerate(ordered):
        p = pvalues[name]
        adjusted = None if p is None else max(previous, min(1.0, (len(ordered) - rank) * p))
        if adjusted is not None:
            previous = adjusted
        result[name] = {
            "raw_p": p,
            "adjusted_p": adjusted,
            "reject": adjusted is not None and adjusted <= alpha,
        }
    return result
