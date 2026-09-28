"""Unbiased pass@k / pass^k estimators."""
import pytest

from app.evals.pass_k import pass_at_k, pass_hat_k, summarize


def test_estimators_match_the_combinatorial_definitions():
    assert pass_at_k(2, 1, 1) == 0.5 and pass_at_k(2, 1, 2) == 1.0
    assert pass_hat_k(2, 1, 2) == 0.0 and pass_hat_k(2, 2, 2) == 1.0
    assert pass_hat_k(4, 3, 2) == pytest.approx(3 / 6)          # C(3,2)/C(4,2)
    assert pass_at_k(4, 1, 2) == pytest.approx(1 - 3 / 6)       # 1 - C(3,2)/C(4,2)
    with pytest.raises(ValueError):
        pass_hat_k(2, 2, 3)


def test_summary_marks_flaky_cases_and_unestimable_k():
    s = summarize({"a": (2, 2), "b": (2, 1), "c": (2, 0)})
    assert s["pass@1"] == pytest.approx(0.5)
    assert s["pass@2"] == pytest.approx(2 / 3) and s["pass^2"] == pytest.approx(1 / 3)
    assert s["pass^3"] is None and s["flaky_cases"] == ["b"]
