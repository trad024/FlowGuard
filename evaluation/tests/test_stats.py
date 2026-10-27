"""Statistics helpers against published reference values."""

import pytest

import stats


def test_wilson_matches_the_textbook_example():
    lo, hi = stats.wilson(81, 263)  # Newcombe (1998), method 3
    assert (round(lo, 4), round(hi, 4)) == (0.2553, 0.3662)
    assert stats.wilson(0, 0) == (0.0, 1.0)


@pytest.mark.parametrize(
    ("k", "expected"),
    [(0, (0.0, 3.689)), (1, (0.0253, 5.572)), (10, (4.795, 18.39))],  # Garwood tables
)
def test_poisson_exact_interval(k, expected):
    lo, hi = stats.poisson_exact(k)
    assert lo == pytest.approx(expected[0], abs=1e-3) and hi == pytest.approx(expected[1], abs=1e-2)


def test_mcnemar_sample_size_grows_as_the_effect_shrinks():
    big, small = stats.mcnemar_n(0.6, 0.8), stats.mcnemar_n(0.6, 0.7)
    assert 0 < big < small
    assert stats.mcnemar_n(0.6, 0.7, alpha=0.0125) > small  # Holm's strictest step
    with pytest.raises(ValueError):
        stats.mcnemar_n(0.7, 0.6)
