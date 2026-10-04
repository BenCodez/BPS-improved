"""Real Shapely path geometry, input safety, cache behavior, and RF trust."""
import copy
import math

import pytest

from bps.environment import (
    MAX_ENVIRONMENT_POLYGONS, MAX_ENVIRONMENT_VERTICES, MIN_ENVIRONMENT_WEIGHT,
    MIN_RELIABILITY, _compile, clamp_reliability, compile_environment,
    measurement_reliability, normalize_environment, outdoor_settings,
    path_reliability,
)


def rectangle(kind="building", material="unknown", x=0, y=0, size=10, identifier="a"):
    return {"id": identifier, "name": "Region", "type": kind, "material": material,
            "points": [{"x": x, "y": y}, {"x": x + size, "y": y},
                       {"x": x + size, "y": y + size}, {"x": x, "y": y + size}]}


def compiled(*regions):
    return compile_environment({"environment": list(regions)})


@pytest.mark.parametrize("start,end,crossings", [
    ((-5, -5), (-1, -1), 0), ((5, 5), (15, 5), 1),
    ((-5, 5), (15, 5), 2), ((-5, 5), (5, -5), 0),
    ((-5, 0), (15, 0), 0), ((0, 5), (-5, 5), 0),
    ((0, 5), (15, 5), 1), ((-5, 5), (0, 5), 0),
    ((-5, 5), (10, 5), 1), ((0, 5), (10, 5), 0),
    ((5, 5), (5, 5), 0), ((5, 5), (7, 7), 0),
])
def test_true_boundary_transitions(start, end, crossings):
    info = path_reliability(compiled(rectangle()), start, end)
    assert info["building_crossings"] == crossings
    assert info["environmental_weight"] == pytest.approx(0.8 ** crossings)


def test_multiple_buildings_and_overlaps_are_bounded():
    geometry = compiled(rectangle(material="metal"), rectangle(x=20, material="metal", identifier="b"))
    info = path_reliability(geometry, (-5, 5), (35, 5))
    assert info["building_crossings"] == 4
    assert info["building_paths"] == 2
    assert info["environmental_weight"] == MIN_ENVIRONMENT_WEIGHT
    overlap = compiled(*[rectangle(material="metal", identifier=str(i)) for i in range(20)])
    assert path_reliability(overlap, (-5, 5), (15, 5))["environmental_weight"] == MIN_ENVIRONMENT_WEIGHT


def test_metal_reports_reflection_only_for_affected_paths():
    geometry = compiled(rectangle(material="metal"))
    assert path_reliability(geometry, (-5, 5), (15, 5))["reflection_risk"] is True
    assert path_reliability(geometry, (-5, -1), (15, -1))["reflection_risk"] is False


def test_inside_receiver_is_automatic_but_not_inherently_bad():
    geometry = compiled(rectangle())
    info = path_reliability(geometry, (5, 5), (6, 6))
    assert info["receiver_inside_building"] is True
    assert info["environmental_weight"] == 1.0
    assert path_reliability(geometry, (0, 5), (15, 5))["receiver_inside_building"] is False


@pytest.mark.parametrize("kind,weight", [("dense_trees", 0.75), ("light_vegetation", 0.9), ("custom", 0.9)])
def test_vegetation_distinct_from_buildings(kind, weight):
    info = path_reliability(compiled(rectangle(kind)), (-5, 5), (15, 5))
    assert info["classification"] == kind
    assert info["environmental_weight"] == weight
    assert info["building_crossings"] == 0


def test_combined_vegetation_and_building_paths():
    info = path_reliability(compiled(rectangle(), rectangle("dense_trees")), (-5, 5), (15, 5))
    assert info["classification"] == "building"
    assert info["building_paths"] == info["vegetation_paths"] == 1
    assert info["environmental_weight"] == pytest.approx(0.8 ** 2 * 0.75)


@pytest.mark.parametrize("value", [True, False, None, "12", float("inf"), float("nan"), {}, 10**1000])
def test_malformed_coordinates_reject_whole_polygon(value):
    region = rectangle()
    region["points"][1]["x"] = value
    assert not compiled(region).regions
    assert normalize_environment([region]) == []


def test_invalid_self_intersecting_and_zero_area_polygons_skipped():
    bowtie = rectangle()
    bowtie["points"] = [{"x": 0, "y": 0}, {"x": 10, "y": 10},
                        {"x": 0, "y": 10}, {"x": 10, "y": 0}]
    flat = rectangle(size=0)
    assert len(compiled(bowtie, flat, rectangle()).regions) == 1


def test_limits_and_malformed_enums_fail_safe():
    assert not compile_environment({"environment": [rectangle()] * (MAX_ENVIRONMENT_POLYGONS + 1)}).regions
    region = rectangle()
    region["points"] *= MAX_ENVIRONMENT_VERTICES
    assert not compiled(region).regions
    region = rectangle()
    region["type"] = []
    assert not compiled(region).regions
    region = rectangle()
    region["material"] = []
    assert compiled(region).regions[0].material == "unknown"


def test_geometry_cache_reused_until_immutable_signature_changes():
    floor = {"environment": [rectangle()]}
    first = compile_environment(floor)
    assert first is compile_environment(copy.deepcopy(floor))
    for _ in range(20):
        path_reliability(first, (-5, 5), (15, 5))
    floor["environment"][0]["points"][0]["x"] = -1
    assert compile_environment(floor) is not first
    assert _compile.cache_info().maxsize == 64


def test_concave_polygon_multiple_true_crossings():
    region = rectangle()
    region["points"] = [{"x": x, "y": y} for x, y in
                        [(0, 0), (10, 0), (10, 10), (7, 10), (7, 3),
                         (3, 3), (3, 10), (0, 10)]]
    assert path_reliability(compiled(region), (-5, 5), (15, 5))["building_crossings"] == 4


def test_settings_opt_in_and_no_layout_mutation():
    old = {"floor": []}
    assert outdoor_settings(old) == {"enabled": False, "show_uncertainty": True,
                                    "hide_uncertainty_below_m": 0.0}
    assert old == {"floor": []}
    assert outdoor_settings({"outdoor_tracking": {"enabled": "yes"}})["enabled"] is False
    assert outdoor_settings({"outdoor_tracking": {"hide_uncertainty_below_m": True}})["hide_uncertainty_below_m"] == 0


def test_measurement_modifiers_do_not_mutate_distances_or_calibration():
    receiver = {"cords": {"x": -5, "y": 5, "r": 999}, "correction": 1.7, "distance": 12.3}
    saved = copy.deepcopy(receiver)
    result = measurement_reliability(compiled(rectangle()), receiver["cords"], (15, 5), base_weight=0.5)
    assert result["reliability_weight"] == pytest.approx(0.5 * 0.8 ** 2)
    assert receiver == saved
    assert result["status"] == "downweighted"


def test_policies_and_explicit_stale_exclusion():
    geometry = compiled(rectangle())
    def measurement(**kwargs):
        return measurement_reliability(geometry, (-5, 5), (15, 5), **kwargs)
    assert measurement(policy="normal")["reliability_weight"] == 1
    assert measurement(policy="prefer")["reliability_weight"] > measurement()["reliability_weight"]
    assert measurement(policy="deprioritize")["reliability_weight"] < measurement()["reliability_weight"]
    assert measurement(policy="ignore")["reliability_weight"] == 0
    assert measurement(age_s=30)["status"] == "excluded"
    assert measurement(age_s=300, max_age_s=0)["used"] is True
    assert measurement(age_s=None)["freshness_weight"] == 1


def test_weak_existing_jump_weight_is_never_inflated_by_safe_floor():
    clear = measurement_reliability(compiled(), (0, 0), (15, 5), base_weight=0.001)
    obstructed = measurement_reliability(compiled(rectangle()), (-5, 5), (15, 5), base_weight=0.001)
    assert clear["reliability_weight"] == 0.001
    assert obstructed["reliability_weight"] <= 0.001


@pytest.mark.parametrize("value", [-10, 0, 1e100, True, float("nan"), float("inf")])
def test_reliability_always_finite_and_bounded(value):
    assert MIN_RELIABILITY <= clamp_reliability(value) <= 1
    assert math.isfinite(clamp_reliability(value))


def test_invalid_path_is_conservative_finite():
    info = path_reliability(compiled(rectangle()), (float("nan"), 2), (1, 2))
    assert info["invalid_path"] is True
    assert info["environmental_weight"] == MIN_ENVIRONMENT_WEIGHT
