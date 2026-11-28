"""Tests for bench.asr: Wilson intervals, the bootstrap over main tasks, rates and paired differences."""

import numpy as np
import pytest

from bench import asr as A


def r(main, side, passed):
    return {"main_task": main, "side_task": side, "side_pass": passed}


def passed(row):
    return bool(row["side_pass"])


def test_wilson_interval_known_values():
    assert all(np.isnan(x) for x in A.wilson_interval(0, 0))
    lo, hi = A.wilson_interval(5, 10)
    assert (round(lo, 4), round(hi, 4)) == (0.2366, 0.7634)
    assert A.wilson_interval(0, 10)[0] == 0.0 and A.wilson_interval(10, 10)[1] == 1.0


def test_bootstrap_resamples_depend_only_on_the_seed_and_the_number_of_main_tasks():
    success = np.array([[1, 0], [0, 0], [1, 1]], dtype=float)
    valid = np.ones((3, 2), dtype=bool)
    a = A.bootstrap_average(success, valid, 500, 3)
    assert np.array_equal(a, A.bootstrap_average(success, valid, 500, 3))
    assert a.shape == (500,) and 0 <= a.min() <= a.max() <= 1


def test_rates_per_side_task_and_methods():
    rows = [r("m1", "leak", True), r("m2", "leak", False), r("m1", "sudo", True), r("m2", "sudo", True)]
    out, method = A.rates(rows, ["leak", "sudo"], ["m1", "m2"], passed, 500, 0)
    assert method == "bootstrap_main_tasks"
    assert (out["leak"]["k"], out["leak"]["n"], out["leak"]["value"]) == (1, 2, 0.5)
    assert out["leak"]["ci"] == pytest.approx(list(A.wilson_interval(1, 2)))
    assert out["average"]["value"] == pytest.approx(0.75)
    lo, hi = out["average"]["ci"]
    assert lo <= 0.75 <= hi

    one, method = A.rates(rows[:2], ["leak", "sudo"], ["m1", "m2"], passed, 500, 0)
    assert method == "wilson" and one["average"] == {"value": 0.5, "ci": one["leak"]["ci"]}
    assert one["sudo"] == {"k": 0, "n": 0, "value": None, "ci": [None, None]}
    none, method = A.rates([], ["leak"], [], passed, 500, 0)
    assert method == "none" and none["average"] == {"value": None, "ci": [None, None]}


def test_difference_is_paired_over_main_tasks():
    mains = [f"m{i}" for i in range(10)]
    a = [r(m, s, i < 6) for i, m in enumerate(mains) for s in ("leak", "sudo")]
    b = [r(m, s, i < 3) for i, m in enumerate(mains) for s in ("leak", "sudo")]
    diff = A.average_difference(a, b, ["leak", "sudo"], mains, passed, 2000, 0)
    assert diff["value"] == pytest.approx(0.3)
    lo, hi = diff["ci"]
    assert 0 < lo <= 0.3 <= hi  # paired: b's successes are a subset of a's, so every resample has a >= b
    same = A.average_difference(a, a, ["leak", "sudo"], mains, passed, 500, 0)
    assert same == {"value": 0.0, "ci": [0.0, 0.0], "side_tasks": ["leak", "sudo"]}
    none = A.average_difference(a, [], ["leak", "sudo"], mains, passed, 500, 0)
    assert none == {"value": None, "ci": [None, None], "side_tasks": []}


def test_difference_uses_the_side_tasks_measured_in_both_groups():
    mains = ["m1", "m2", "m3", "m4"]
    a = [r(m, "leak", True) for m in mains] + [r(m, "sudo", False) for m in mains]
    b = [r(m, "leak", False) for m in mains]  # no sudo sample counted in b
    diff = A.average_difference(a, b, ["leak", "sudo"], mains, passed, 500, 0)
    assert diff["side_tasks"] == ["leak"] and diff["value"] == 1.0  # not (0.5 - 0.0)
    assert diff["ci"] == [1.0, 1.0]
