"""Independent GEOS reference and process-contained outdoor geometry stress.

Shapely belongs only to the serial test oracle. The stress helper imports the
production geometry module alone and rejects any loaded Shapely module.
"""
import ast
import copy
import json
import math
import random
import subprocess
import sys
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point, Polygon

from bps import environment


WALL_WEIGHTS = {"unknown": 0.8, "light": 0.9, "wood": 0.9,
                "heavy": 0.65, "metal": 0.5}
TREE_WEIGHTS = {"dense_trees": 0.75, "light_vegetation": 0.9, "custom": 0.9}
SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]
CONCAVE = [(0, 0), (10, 0), (10, 10), (7, 10), (7, 3),
           (3, 3), (3, 10), (0, 10)]


def region(points=SQUARE, *, kind="building", material="unknown", walls=None,
           identifier="reference"):
    result = {"id": identifier, "name": identifier, "type": kind,
              "material": material, "points": [{"x": x, "y": y} for x, y in points]}
    if walls is not None:
        result["wall_materials"] = list(walls)
    return result


def _boundary_points(geometry):
    """Use GEOS-produced intersections, independent of production predicates."""
    if geometry.is_empty:
        return []
    if hasattr(geometry, "geoms"):
        return [point for part in geometry.geoms for point in _boundary_points(part)]
    if geometry.geom_type == "Point":
        return [geometry]
    return [Point(geometry.coords[0]), Point(geometry.coords[-1])]


def shapely_reference(records, start, end):
    """Established strict-interior RF semantics using independent GEOS geometry.

    Boundary-only intervals do not contribute attenuation. Crossings require a
    strict inside/outside transition within the path; endpoints have no implied
    extension. A corner selects the strongest attenuation of its incident walls.
    """
    output = {"environmental_weight": 1.0, "classification": "clear",
              "building_crossings": 0, "building_paths": 0,
              "vegetation_paths": 0, "custom_paths": 0,
              "receiver_inside_building": False, "reflection_risk": False,
              "intersections": []}
    path = LineString([start, end])
    receiver = Point(start)
    kinds = set()
    for record in records:
        points = [(p["x"], p["y"]) for p in record["points"]]
        polygon = Polygon(points)
        assert polygon.is_valid and polygon.area > 1e-9, "Reference fixtures must be valid"
        kind = record["type"]
        output["receiver_inside_building"] |= kind == "building" and polygon.contains(receiver)
        if path.length <= 1e-9:
            continue
        cuts = sorted({0.0, path.length, *(path.project(point) for point in
                       _boundary_points(path.intersection(polygon.boundary)))})
        intervals = []
        interior_length = 0.0
        for low, high in zip(cuts, cuts[1:]):
            if high - low <= 1e-9:
                continue
            midpoint = path.interpolate((low + high) / 2)
            if polygon.contains(midpoint):
                interior_length += high - low
                intervals.append((True, low, high))
            elif not polygon.boundary.covers(midpoint):
                intervals.append((False, low, high))
        if interior_length <= 1e-9:
            continue
        crossings = [path.interpolate(right[1] if right[0] else left[2])
                     for left, right in zip(intervals, intervals[1:])
                     if left[0] != right[0]]
        kinds.add(kind)
        material = record["material"]
        details = {"id": record["id"], "type": kind, "material": material,
                   "boundary_crossings": len(crossings),
                   "path_length_px": interior_length}
        if kind == "building":
            walls = []
            for crossing in crossings:
                hits = []
                for index, first in enumerate(points):
                    edge = LineString([first, points[(index + 1) % len(points)]])
                    if edge.length > 1e-9 and edge.distance(crossing) <= 1e-6:
                        override = record.get("wall_materials", [None] * len(points))[index]
                        hits.append((index + 1, override or material))
                chosen = min((m for _, m in hits), key=WALL_WEIGHTS.get) if hits else material
                walls.append({"walls": [i for i, _ in hits], "material": chosen,
                              "point": [crossing.x, crossing.y]})
            details["wall_crossings"] = walls
            weight = math.prod(WALL_WEIGHTS[wall["material"]] for wall in walls)
            output["building_crossings"] += len(crossings)
            output["building_paths"] += 1
            output["reflection_risk"] |= any(w["material"] == "metal" for w in walls) or (
                not walls and "metal" in [w or material for w in
                    record.get("wall_materials", [material])])
        else:
            weight = TREE_WEIGHTS[kind]
            output["custom_paths" if kind == "custom" else "vegetation_paths"] += 1
        details["weight"] = weight
        output["intersections"].append(details)
        output["environmental_weight"] *= weight
    output["environmental_weight"] = max(0.15, output["environmental_weight"])
    output["classification"] = next((kind for kind in
        ("building", "dense_trees", "light_vegetation", "custom") if kind in kinds), "clear")
    return output


def assert_json_matches(actual, expected):
    """Compare all public fields without depending on compiled-region internals."""
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys()
        for key in expected:
            assert_json_matches(actual[key], expected[key])
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for a, e in zip(actual, expected):
            assert_json_matches(a, e)
    elif isinstance(expected, float):
        assert math.isfinite(actual)
        assert actual == pytest.approx(expected, rel=1e-9, abs=1e-7)
    else:
        assert actual == expected


PATHS = [
    ((-5, 5), (15, 5)),       # Horizontal two/four-wall crossing.
    ((5, -5), (5, 15)),       # Vertical crossing or concave notch.
    ((5, 5), (15, 5)),        # Receiver strictly inside.
    ((2, 2), (8, 2)),         # Entirely interior.
    ((-5, -5), (-1, -1)),     # Entirely outside.
    ((-5, -5), (15, 15)),     # Vertex entry and exit.
    ((-5, 5), (5, -5)),       # Tangent at a corner.
    ((-5, 0), (15, 0)),       # Collinear boundary overlap.
    ((0, 0), (10, 0)),        # Exact full wall.
    ((-5, 10), (15, 10)),     # Several collinear concave wall spans.
    ((0, 5), (15, 5)),        # Starts on wall; no invented entry.
    ((-5, 5), (10, 5)),       # Ends on wall; no invented exit.
    ((0, 5), (10, 5)),        # Both endpoints on wall.
    ((0, 0), (10, 10)),       # Both endpoints at vertices.
    ((5, 5), (5, 5)),         # Zero-length strictly inside/notch.
    ((0, 0), (0, 0)),         # Zero-length boundary.
    ((-1, -1), (-1, -1)),     # Zero-length outside.
    ((3, 5), (7, 5)),         # Concave notch with wall endpoints.
    ((-2, 3), (12, 3)),       # Boundary interval between two interior spans.
    ((3, -2), (3, 12)),       # Concave wall overlap after interior segment.
]
SHAPES = [
    region(),
    region(list(reversed(SQUARE)), material="heavy"),
    region(CONCAVE, material="wood"),
    region(SQUARE + [SQUARE[0]], material="metal"),
    region([SQUARE[0], SQUARE[0], *SQUARE[1:]], material="light"),
    region([(0, 0), (5, 0), (10, 0), (10, 10), (0, 10)],
           walls=["wood", "metal", "light", "heavy", None]),
    region(walls=["wood", None, "metal", "heavy"]),
    region(kind="dense_trees"),
    region(CONCAVE, kind="light_vegetation"),
    region(kind="custom"),
]


@pytest.mark.parametrize("record", SHAPES, ids=[
    "convex", "clockwise", "concave", "closed-ring", "duplicate-vertex",
    "collinear-vertex-materials", "per-wall-materials", "trees", "vegetation", "custom"])
@pytest.mark.parametrize("start,end", PATHS)
def test_geos_reference_boundary_and_material_regressions(record, start, end):
    records = [record]
    actual = environment.path_reliability(environment.compile_environment({"environment": records}), start, end)
    assert_json_matches(actual, shapely_reference(records, start, end))
    json.dumps(actual, allow_nan=False)


@pytest.mark.parametrize("start,end", PATHS)
def test_geos_reference_overlapping_regions(start, end):
    records = [region(material="metal", identifier="shop"),
               region([(5, -2), (12, -2), (12, 12), (5, 12)],
                      material="wood", identifier="shed"),
               region(CONCAVE, kind="dense_trees", identifier="trees"),
               region(kind="custom", identifier="custom")]
    actual = environment.path_reliability(environment.compile_environment({"environment": records}), start, end)
    assert_json_matches(actual, shapely_reference(records, start, end))


def test_seeded_irregular_convex_and_concave_geos_reference():
    generator = random.Random(20261010)
    for trial in range(80):
        # Sorted angles around a center produce simple radial polygons, including
        # concave ones, without depending on production validity checks.
        count = generator.randint(4, 14)
        angles = [2 * math.pi * (index + generator.uniform(-0.2, 0.2)) / count
                  for index in range(count)]
        points = [(radius * math.cos(angle), radius * math.sin(angle))
                  for angle, radius in zip(angles, [generator.uniform(5, 20) for _ in angles])]
        records = [region(points, material="heavy", walls=[
            generator.choice([None, "wood", "metal", "heavy"]) for _ in points])]
        geometry = environment.compile_environment({"environment": records})
        for _ in range(4):
            start = tuple(generator.uniform(-25, 25) for _ in range(2))
            end = tuple(generator.uniform(-25, 25) for _ in range(2))
            actual = environment.path_reliability(geometry, start, end)
            try:
                assert_json_matches(actual, shapely_reference(records, start, end))
            except AssertionError as error:
                raise AssertionError(f"Reference disagreement for trial {trial}: {records}, {start}, {end}") from error


@pytest.mark.parametrize("points,start,end", [
    ([(9_999_980, 9_999_980), (9_999_990, 9_999_980),
      (9_999_990, 9_999_990), (9_999_980, 9_999_990)],
     (9_999_975, 9_999_985), (9_999_995, 9_999_985)),
    ([(-9_999_990, -9_999_990), (-9_999_980, -9_999_990),
      (-9_999_980, -9_999_980), (-9_999_990, -9_999_980)],
     (-9_999_995, -9_999_985), (-9_999_975, -9_999_985)),
    ([(-9_999_990, -9_999_990), (9_999_990, -9_999_990),
      (9_999_990, 9_999_990), (-9_999_990, 9_999_990)],
     (-9_999_995, 0), (9_999_995, 0)),
    ([(0, 0), (0.0001, 0), (0.0001, 0.0001), (0, 0.0001)],
     (-0.0001, 0.00005), (0.0002, 0.00005)),
    ([(0, 0), (0.0001, 0), (0.0001, 0.0001), (0, 0.0001)],
     (-9_999_995, 0.00005), (9_999_995, 0.00005)),
    ([(9_999_990, 9_999_990), (9_999_990.0001, 9_999_990),
      (9_999_990.0001, 9_999_990.0001), (9_999_990, 9_999_990.0001)],
     (9_999_989.9999, 9_999_990.00005), (9_999_990.0002, 9_999_990.00005)),
])
def test_extreme_valid_coordinates_and_small_translated_polygons(points, start, end):
    records = [region(points)]
    normalized = environment.normalize_environment(records)
    assert len(normalized) == 1
    actual = environment.path_reliability(environment.compile_environment({"environment": records}), start, end)
    assert_json_matches(actual, shapely_reference(records, start, end))


@pytest.mark.parametrize("points", [
    [(0, 0), (10, 10), (0, 10), (10, 0)],  # Self-intersection.
    [(0, 0), (10, 0), (10, 10), (5, 10), (5, 5), (5, 10), (0, 10)],  # Spike.
    [(0, 0), (10, 0), (10, 10), (0, 0), (-10, 10), (-10, 0)],  # Touching rings.
    [(0, 0), (1, 0), (2, 0)],
    [(0, 0), (0, 0), (0, 0)],
    [(0, 0), (1e-6, 0), (0, 1e-6)],  # Below the existing area threshold.
])
def test_invalid_topology_is_omitted_without_affecting_valid_neighbors(points):
    invalid = region(points, identifier="invalid")
    polygon = Polygon(points)
    assert not polygon.is_valid or polygon.area <= 1e-9
    valid = region(identifier="valid")
    assert environment.normalize_environment([invalid]) == []
    assert environment.normalize_environment([invalid, valid]) == [valid]
    actual = environment.path_reliability(environment.compile_environment({"environment": [invalid, valid]}),
                                          (-5, 5), (15, 5))
    assert_json_matches(actual, shapely_reference([valid], (-5, 5), (15, 5)))


@pytest.mark.parametrize("value", [None, True, "1", [], {}, math.nan, math.inf, -math.inf,
                                 10**1000, 10_000_001, -10_000_001])
def test_malformed_polygon_coordinate_is_rejected_as_whole(value):
    record = region()
    record["points"][2]["x"] = value
    assert environment.normalize_environment([record]) == []
    assert environment.path_reliability(environment.compile_environment({"environment": [record]}),
                                        (-5, 5), (15, 5))["classification"] == "clear"


@pytest.mark.parametrize("points", [None, {}, "bad", [], [{"x": 0, "y": 0}] * 2,
                                  [{"x": 0, "y": 0}, None, {"x": 1, "y": 1}],
                                  [{"x": 0, "y": 0}, {"x": 1}, {"x": 1, "y": 1}]])
def test_malformed_point_collections_fail_safely(points):
    record = region()
    record["points"] = points
    assert environment.normalize_environment([record]) == []


@pytest.mark.parametrize("walls", [[], ["wood"], ["wood"] * 5,
                                  ["wood", "bogus", None, "metal"],
                                  ["wood", [], None, "metal"], "metal", True])
def test_malformed_wall_material_overrides_inherit_building_default(walls):
    record = region(material="heavy")
    record["wall_materials"] = walls
    normalized = environment.normalize_environment([record])
    assert "wall_materials" not in normalized[0]
    actual = environment.path_reliability(environment.compile_environment({"environment": [record]}),
                                          (-5, 5), (15, 5))
    assert_json_matches(actual, shapely_reference([region(material="heavy")], (-5, 5), (15, 5)))


@pytest.mark.parametrize("start", [None, True, "bad", {}, [], [1], [math.nan, 0],
                                 [0, math.inf], [True, 1], ["1", 2], [10_000_001, 0]])
def test_malformed_paths_produce_finite_conservative_json(start):
    geometry = environment.compile_environment({"environment": [region()]})
    for receiver, fix in [(start, (5, 5)), ((5, 5), start)]:
        output = environment.path_reliability(geometry, receiver, fix)
        assert output["invalid_path"] is True
        assert output["classification"] == "unknown"
        assert output["environmental_weight"] == 0.15
        assert output["intersections"] == []
        json.dumps(output, allow_nan=False)


def test_map_replacement_does_not_mutate_held_geometry_or_normalized_configuration():
    records = [region(walls=["wood", None, "metal", None])]
    floor = {"environment": records}
    snapshot = environment.compile_environment(floor)
    expected = copy.deepcopy(environment.path_reliability(snapshot, (-5, 5), (15, 5)))
    normalized = environment.normalize_environment(records)
    for update in range(90):  # Exceed the 64-entry cache while retaining snapshot.
        floor["environment"][0]["id"] = f"replacement-{update}"
        floor["environment"][0]["points"][0]["x"] = -update - 1
        floor["environment"][0]["wall_materials"][0] = "metal"
        replacement = environment.compile_environment(floor)
        json.dumps(environment.path_reliability(replacement, (-5, 5), (15, 5)), allow_nan=False)
    assert environment.path_reliability(snapshot, (-5, 5), (15, 5)) == expected
    assert normalized == [region(walls=["wood", None, "metal", None])]


def test_live_environment_source_has_no_shapely_imports():
    tree = ast.parse(Path(environment.__file__).read_text())
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    imported = [name.name for node in imports if isinstance(node, ast.Import) for name in node.names]
    imported.extend(node.module or "" for node in imports if isinstance(node, ast.ImportFrom))
    assert not any(name == "shapely" or name.startswith("shapely.") for name in imported)


def test_concurrent_map_replacement_and_cache_turnover_in_subprocess():
    helper = Path(__file__).parent / "helpers" / "outdoor_stress.py"
    completed = subprocess.run([sys.executable, str(helper), "--workers", "12", "--iterations", "3072"],
                               capture_output=True, text=True, timeout=70, check=False)
    assert completed.returncode == 0, (
        f"Stress process failed ({completed.returncode}); stdout={completed.stdout}; stderr={completed.stderr}")
    report = json.loads(completed.stdout)
    assert report["workers"] == 12
    assert report["iterations"] == 3072
    assert report["measurements"] == 3072 * 8
    assert report["cache_size"] <= 64
    assert report["native_geometry_loaded"] is False
