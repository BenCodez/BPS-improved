"""Explicit map-trust tuning on synthetic paths and real weighted fixes."""
import copy
import math

import pytest

import bps
from bps.environment import (MIN_ENVIRONMENT_WEIGHT, MIN_RELIABILITY,
    compile_environment, measurement_reliability, outdoor_settings, path_reliability)
from bps.outdoor_config import validate_outdoor_layout
from bps.outdoor_tracking import solve_outdoor


def area(kind="dense_trees", points=((0, 0), (30, 0), (30, 40), (0, 40)), material="wood"):
    return {"id": "region", "name": "Synthetic obstruction", "type": kind,
            "material": material, "points": [{"x": x, "y": y} for x, y in points]}


def path(region, *, strength=2, scale=1, start=(-10, 20), end=(40, 20)):
    return path_reliability(compile_environment({"environment": [region]}), start, end,
        scale_px_per_m=scale, obstruction_strength=strength)


def test_tree_depth_and_strength_are_monotonic_and_keep_standard_weights():
    shallow = area(points=((0, 0), (5, 0), (5, 40), (0, 40)))
    deep = area()
    assert path(shallow, strength=1)["environmental_weight"] == 0.75
    assert path(deep, strength=1)["environmental_weight"] == 0.75
    assert path(deep)["environmental_weight"] < path(shallow)["environmental_weight"]
    weights = [path(deep, strength=s)["environmental_weight"] for s in (1, 1.1, 2, 3, 4)]
    assert weights == sorted(weights, reverse=True)
    assert all(MIN_ENVIRONMENT_WEIGHT <= w <= 1 for w in weights)
    info = path(deep)
    assert info["vegetation_depth_m"] == pytest.approx(30)
    assert info["mapped_environmental_weight"] == info["environmental_weight"]
    assert info["path_start_px"] == [-10, 20]
    assert info["path_end_px"] == [40, 20]
    assert info["intersections"][0]["path_length_m"] == pytest.approx(30)
    assert info["intersections"][0]["base_weight"] == 0.75
    assert info["intersections"][0]["depth_multiplier"] == pytest.approx(2)
    assert info["environmental_weight"] == pytest.approx(0.75 ** 4)


def test_tree_density_custom_meaning_and_physical_scale_are_distinct():
    dense = path(area())
    light = path(area("light_vegetation"))
    custom = path(area("custom"))
    assert dense["environmental_weight"] < light["environmental_weight"] < custom["environmental_weight"]
    assert custom["intersections"][0]["depth_multiplier"] == 1
    assert custom["vegetation_depth_m"] == 0
    # A resized map with the matching pixels/metre scale is physically identical.
    resized = area(points=((0, 0), (300, 0), (300, 400), (0, 400)))
    scaled = path(resized, scale=10, start=(-100, 200), end=(400, 200))
    assert scaled["vegetation_depth_m"] == pytest.approx(dense["vegetation_depth_m"])
    assert scaled["environmental_weight"] == pytest.approx(dense["environmental_weight"])


@pytest.mark.parametrize("scale", [None, 0, -1, True, "10", float("nan"), float("inf"), 1e7])
def test_missing_or_invalid_scale_never_guesses_tree_depth(scale):
    info = path(area(), scale=scale)
    assert info["vegetation_depth_m"] is None
    assert info["intersections"][0]["path_length_m"] is None
    assert info["environmental_weight"] == 0.75 ** 2


def test_strength_retains_each_wall_material_and_thin_fence_behavior_once():
    building = area("building")
    building["wall_materials"] = ["light", "metal", "wood", "wood"]
    info = path(building)
    assert info["building_crossings"] == 2
    assert info["intersections"][0]["base_weight"] == 0.9 * 0.5
    assert info["environmental_weight"] == pytest.approx((0.9 * 0.5) ** 2)
    assert [w["material"] for w in info["intersections"][0]["wall_crossings"]] == ["wood", "metal"]
    weights = [path(area("building", material=m), strength=2)["environmental_weight"]
               for m in ("light", "heavy", "metal")]
    assert weights == sorted(weights, reverse=True)
    fence = {"id": "f", "name": "Fence", "type": "fence", "material": "metal",
             "points": [{"x": 0, "y": 0}, {"x": 0, "y": 40}]}
    solid = path(fence)["environmental_weight"]
    fence["thickness"] = "thin"
    thin = path(fence)["environmental_weight"]
    assert solid == 0.5 ** 2
    assert thin == 0.875 ** 2
    assert thin > solid


def test_policy_precedence_minimum_trust_and_raw_mapping_evidence():
    compiled = compile_environment({"environment": [area()]})
    def measurement(policy="auto", **kwargs):
        return measurement_reliability(compiled, (-10, 20), (40, 20), policy=policy,
            scale_px_per_m=1, obstruction_strength=4, **kwargs)
    auto, normal, prefer = [measurement(policy) for policy in ("auto", "normal", "prefer")]
    assert auto["mapped_environmental_weight"] == MIN_ENVIRONMENT_WEIGHT
    assert normal["mapped_environmental_weight"] == auto["mapped_environmental_weight"]
    assert normal["environmental_weight"] == normal["reliability_weight"] == 1
    assert prefer["reliability_weight"] > auto["reliability_weight"]
    assert measurement("ignore")["reliability_weight"] == 0
    assert measurement(age_s=30)["reliability_weight"] == 0
    assert measurement(base_weight=0.001)["reliability_weight"] <= 0.001
    assert measurement(base_weight=0.05)["reliability_weight"] == MIN_RELIABILITY


def test_deep_trees_saturate_and_clear_or_tangent_paths_remain_neutral():
    huge = area(points=((0, 0), (1_000_000, 0), (1_000_000, 40), (0, 40)))
    deep = path(huge, strength=4, start=(-10, 20), end=(1_000_010, 20))
    assert deep["intersections"][0]["depth_multiplier"] == 4
    assert deep["environmental_weight"] == MIN_ENVIRONMENT_WEIGHT
    assert math.isfinite(deep["vegetation_depth_m"])
    for start, end in [((-10, -1), (40, -1)), ((-10, 0), (40, 0))]:
        info = path(area(), strength=4, start=start, end=end)
        assert info["environmental_weight"] == 1
        assert info["vegetation_depth_m"] == 0
        assert info["intersections"] == []


def test_indoor_restoration_keeps_other_stronger_obstructions_and_normal_policy():
    own = area("building", points=((0, 0), (10, 0), (10, 10), (0, 10)), material="metal")
    trees = area(points=((15, 0), (25, 0), (25, 10), (15, 10)))
    trees["id"] = "trees"
    other = area("building", points=((28, 0), (32, 0), (32, 10), (28, 10)))
    other["id"] = "other"
    floor = {"name": "Synthetic", "scale": 1, "environment": [own, trees, other], "receivers": []}
    points = []
    for i, (xy, distance) in enumerate([((5, 5), 2), ((0, -20), 40), ((50, 10), 20), ((40, 30), 30)]):
        floor["receivers"].append({"entity_id": f"p{i}", "distance": distance,
            "cords": {"x": xy[0], "y": xy[1], "r": distance},
            "outdoor_policy": "normal" if i == 1 else "auto",
            "_outdoor_reading": {"measured_distance_m": distance, "reading_age_s": 0}})
        points.append((*xy, distance, 1.0, distance))
    result = solve_outdoor(floor, points, (-50, -50, 150, 150), 1, .5, 30,
        None, lambda *a, **kw: (35, 5), obstruction_strength=2)
    indoor, normal = result["outdoor"]["receiver_diagnostics"][:2]
    assert indoor["indoor_support_strength"] == 1
    assert indoor["mapped_environmental_weight"] == MIN_ENVIRONMENT_WEIGHT
    assert indoor["environmental_weight"] == pytest.approx(0.75 ** 2 * 0.9 ** 4)
    assert indoor["intersections"][0]["weight"] == 0.5 ** 2
    assert normal["mapped_environmental_weight"] < 1
    assert normal["environmental_weight"] == normal["reliability_weight"] == 1
    assert normal["indoor_range_weight"] == 1
    assert result["fix"] == (35, 5)


def test_live_pipeline_passes_strength_and_physical_scale(hass, monkeypatch):
    import asyncio
    import datetime
    from types import SimpleNamespace
    floor, _points, _truth = outlier_case(area())
    # A clean current reading set is sufficient to verify layout propagation;
    # it does not depend on previous tracker histories or user station data.
    readings = {r["entity_id"]: r["distance"] for r in floor["receivers"]}
    for receiver in floor["receivers"]:
        receiver.pop("distance")
        receiver.pop("_outdoor_reading")
        receiver["cords"].pop("r")
        receiver["correction"] = 1
    monkeypatch.setattr(bps, "apitricords", [])
    for name in ("_kf_position_state", "_floor_probability", "_floor_challenge", "_floor_dark_cycles"):
        monkeypatch.setattr(bps, name, {})
    for name in ("last_floor", "last_r_values"):
        monkeypatch.setattr(bps.update_trilateration_and_zone, name, {}, raising=False)
    floor["zones"] = []
    now = datetime.datetime.now(datetime.timezone.utc)
    hass.states = SimpleNamespace(get=lambda eid: SimpleNamespace(state=str(readings[eid.rsplit("_to_", 1)[-1]]),
        attributes={"unit_of_measurement": "m"}, last_updated=now))
    item = {"entity": "obstruction_synthetic", "data": {"floor": [floor],
        "outdoor_tracking": {"enabled": True, "obstruction_strength": 2}}}
    asyncio.run(bps.process_single_entity(hass, [item], item))
    quality = bps.apitricords[0]["outdoor"]
    assert quality["obstruction_strength"] == 2
    obstructed = next(d for d in quality["receiver_diagnostics"] if d["receiver"] == "p0")
    assert obstructed["obstruction_strength"] == 2
    assert obstructed["path_scale_px_per_m"] == 1
    assert obstructed["vegetation_depth_m"] > 10
    assert obstructed["intersections"][0]["depth_multiplier"] > 1


@pytest.mark.parametrize("value", [0, 4.1, True, "2", None, float("nan"), float("inf")])
def test_strength_configuration_rejects_invalid_saves_and_normalizes_runtime(value):
    layout = {"outdoor_tracking": {"enabled": True, "obstruction_strength": value}}
    before = copy.deepcopy(layout)
    assert validate_outdoor_layout(layout)
    assert outdoor_settings(layout)["obstruction_strength"] == 1
    assert path(area(), strength=value)["obstruction_strength"] == 1
    # NaN cannot compare equal to itself; both readers must leave its identity intact.
    assert layout["outdoor_tracking"]["obstruction_strength"] is value
    assert layout.keys() == before.keys()


def outlier_case(region):
    truth = (40, 50)
    positions = [(0, 0), (0, 100), (100, 100), (100, 0)]
    floor = {"name": "Synthetic", "scale": 1, "environment": [region], "receivers": []}
    points = []
    for i, xy in enumerate(positions):
        distance = 100.0 if i == 0 else math.dist(xy, truth)
        floor["receivers"].append({"entity_id": f"p{i}", "distance": distance,
            "cords": {"x": xy[0], "y": xy[1], "r": distance},
            "_outdoor_reading": {"measured_distance_m": distance, "reading_age_s": 0}})
        points.append((*xy, distance, 1.0, distance))
    return floor, points, truth


@pytest.mark.parametrize("kind", ["dense_trees", "building"])
def test_stronger_mapped_outlier_path_improves_real_fix_without_range_correction(kind):
    floor, points, truth = outlier_case(area(kind))
    saved = copy.deepcopy(floor)
    results = [solve_outdoor(floor, points, (-50, -50, 150, 150), 1, .5, 30,
        None, bps.trilaterate, obstruction_strength=s) for s in (1, 2)]
    standard, stronger = results
    assert math.dist(stronger["fix"], truth) < math.dist(standard["fix"], truth)
    assert stronger["outdoor"]["obstruction_strength"] == 2
    assert stronger["weighted"][0][3] < standard["weighted"][0][3]
    assert all(a[:3] == b[:3] and a[4] == b[4] for a, b in zip(stronger["weighted"], points))
    assert all(p[3] > 0 for p in stronger["weighted"])
    assert floor == saved
    # Mapped obstruction is a trust hint: penalizing an accurate bearing instead
    # does not magically identify the outlier or guarantee a better position.
    wrong_region = area(kind, points=((0, 60), (30, 60), (30, 100), (0, 100)))
    wrong_floor, wrong_points, _ = outlier_case(wrong_region)
    wrong = solve_outdoor(wrong_floor, wrong_points, (-50, -50, 150, 150), 1, .5,
        30, None, bps.trilaterate, obstruction_strength=2)
    assert math.dist(wrong["fix"], truth) > math.dist(stronger["fix"], truth)
