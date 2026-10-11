"""The visual station footprint never substitutes for diagnostic uncertainty."""
import copy

import pytest

from bps.display_radius import reporting_radius_limit


def data():
    floor = {"name": "Yard", "scale": 10, "receivers": [
        {"entity_id": name, "cords": {"x": x, "y": 0}} for name, x in
        [("one", 30), ("two", 40), ("three", 80), ("four", 200), ("offline", 10)]]}
    rows = [{"receiver": name, "observed": 99, "reliability_weight": .3, "used": True}
            for name in ["one", "two", "three", "four"]]
    return floor, rows


def test_cap_uses_third_nearest_fresh_reporting_receiver_and_published_point():
    floor, rows = data()
    before = copy.deepcopy((floor, rows))
    result = reporting_radius_limit(floor, [0, 0], rows, 100)
    assert result["display_radius_limit_m"] == 10
    assert [r["receiver"] for r in result["display_radius_receivers"]] == ["one", "two", "three"]
    shifted = reporting_radius_limit(floor, [10, 0], rows, 100)
    assert shifted["display_radius_limit_m"] == 8.75
    assert (floor, rows) == before


def test_missing_duplicate_and_excluded_receivers_cannot_create_three_stations():
    floor, rows = data()
    for row in rows[2:]:
        row["used"] = False
    rows += [rows[0], rows[0], {"receiver": "missing", "observed": 99, "reliability_weight": 1}]
    assert reporting_radius_limit(floor, [0, 0], rows, 100) == {}
    floor["receivers"] += [floor["receivers"][0]]
    assert reporting_radius_limit(floor, [0, 0], rows, 100) == {}


@pytest.mark.parametrize("changes", [{"observed": 70}, {"observed": 101}, {"observed": None},
    {"used": False}, {"excluded": True}, {"status": "excluded"}, {"reliability_weight": 0},
    {"reliability_weight": float("nan")}, {"reliability_weight": None}])
def test_stale_future_unknown_or_unused_ranges_do_not_limit_display(changes):
    floor, rows = data()
    rows = rows[:3]
    rows[2].update(changes)
    assert reporting_radius_limit(floor, [0, 0], rows, 100) == {}


def test_ignored_receiver_does_not_count_even_if_diagnostics_are_inconsistent():
    floor, rows = data()
    floor["receivers"][2]["outdoor_policy"] = "ignore"
    assert reporting_radius_limit(floor, [0, 0], rows[:3], 100) == {}


@pytest.mark.parametrize("point", [None, [], [0], [0, float("nan")], [True, 0], [1e20, 0]])
def test_invalid_published_point_has_no_display_limit(point):
    floor, rows = data()
    assert reporting_radius_limit(floor, point, rows, 100) == {}


def test_disabling_reading_gate_does_not_count_indefinitely_old_ranges():
    floor, rows = data()
    assert reporting_radius_limit(floor, [0, 0], rows, 150, max_age_s=0) == {}
