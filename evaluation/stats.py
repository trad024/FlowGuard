"""Small statistics for the evaluation, standard library only.

wilson(k, n)          95% Wilson score interval for a proportion
poisson_exact(k)      95% exact (Garwood) interval for a Poisson count
mcnemar_n(p0, p1)     paired sample size for McNemar's test (Connor 1987), assuming the two
                      levels' outcomes are independent given the item (a conservative choice)
"""

from __future__ import annotations

import math
from statistics import NormalDist

Z = NormalDist()


def wilson(k: int, n: int, level: float = 0.95) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    z = Z.inv_cdf(1 - (1 - level) / 2)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def _poisson_cdf(k: int, lam: float) -> float:
    term = total = math.exp(-lam)
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return total


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
