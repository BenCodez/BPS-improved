"""Optional map obstruction heuristics; coordinates are always floor pixels.

The factors below express relative trust, not RF loss or distance corrections.
They are intentionally conservative starting points for empirical tuning. No
input receiver, radius, or calibration factor is ever changed by this module.
"""
from dataclasses import dataclass
from functools import lru_cache
import math
from numbers import Real

from shapely.geometry import LineString, Point, Polygon
from shapely.prepared import prep

MAX_ENVIRONMENT_POLYGONS = 128
MAX_ENVIRONMENT_VERTICES = 256
MAX_COORDINATE_PX = 10_000_000.0
MIN_RELIABILITY = 0.05
MIN_ENVIRONMENT_WEIGHT = 0.15
BUILDING_BOUNDARY_WEIGHTS = {"unknown": 0.80, "light": 0.90,
                             "heavy": 0.65, "metal": 0.50}
VEGETATION_WEIGHTS = {"dense_trees": 0.75, "light_vegetation": 0.90,
                      "custom": 0.90}
ENVIRONMENT_TYPES = frozenset({"building", *VEGETATION_WEIGHTS})
RECEIVER_POLICIES = frozenset({"auto", "normal", "prefer", "deprioritize", "ignore"})


def finite_number(value, default=None, *, minimum=None, maximum=None):
    """Accept real finite numbers, rejecting booleans and numeric strings."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return default
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return default
    if not math.isfinite(value):
        return default
    if minimum is not None and value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


def clamp_reliability(value, default=1.0):
    """Keep contributing measurements in [0.05, 1]; exclusions are explicit."""
    number = finite_number(value, finite_number(default, 1.0))
    return max(MIN_RELIABILITY, min(1.0, number))


def outdoor_settings(layout):
    """Normalize optional configuration without adding fields to old layouts."""
    settings = layout.get("outdoor_tracking", {}) if isinstance(layout, dict) else {}
    if not isinstance(settings, dict):
        settings = {}
    return {"enabled": settings.get("enabled") is True,
            "show_uncertainty": settings.get("show_uncertainty", True) is not False,
            "hide_uncertainty_below_m": finite_number(
                settings.get("hide_uncertainty_below_m"), 0.0,
                minimum=0.0, maximum=10_000.0)}


def receiver_policy(value):
    return value if isinstance(value, str) and value in RECEIVER_POLICIES else "auto"


def _environment_signature(records):
    """Bound work and reject a whole polygon if any vertex is malformed.

    Polygon validity is checked during compilation. Oversized collections are
    rejected rather than silently giving partial obstruction coverage.
    """
    if not isinstance(records, list) or len(records) > MAX_ENVIRONMENT_POLYGONS:
        return ()
    signature = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        kind = record.get("type")
        if not isinstance(kind, str) or kind not in ENVIRONMENT_TYPES:
            continue
        points = record.get("points")
        if not isinstance(points, list) or not 3 <= len(points) <= MAX_ENVIRONMENT_VERTICES:
            continue
        coordinates = []
        for point in points:
            if not isinstance(point, dict):
                break
            x = finite_number(point.get("x"), maximum=MAX_COORDINATE_PX,
                              minimum=-MAX_COORDINATE_PX)
            y = finite_number(point.get("y"), maximum=MAX_COORDINATE_PX,
                              minimum=-MAX_COORDINATE_PX)
            if x is None or y is None:
                break
            coordinates.append((x, y))
        if len(coordinates) != len(points):
            continue
        material = record.get("material", "unknown")
        if not isinstance(material, str) or material not in BUILDING_BOUNDARY_WEIGHTS:
            material = "unknown"
        identifier = record.get("id", f"environment_{index}")
        name = record.get("name", identifier)
        if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
            identifier = f"environment_{index}"
        if not isinstance(name, str) or len(name) > 256:
            name = identifier
        signature.append((identifier, name, kind, material, tuple(coordinates)))
    return tuple(signature)


@dataclass(frozen=True)
class EnvironmentRegion:
    identifier: str
    name: str
    kind: str
    material: str
    coordinates: tuple
    polygon: object
    prepared: object


@dataclass(frozen=True)
class CompiledEnvironment:
    regions: tuple


@lru_cache(maxsize=64)
def _compile(signature):
    regions = []
    for identifier, name, kind, material, coordinates in signature:
        try:
            polygon = Polygon(coordinates)
            if not polygon.is_valid or polygon.is_empty or polygon.area <= 1e-9:
                continue
            regions.append(EnvironmentRegion(identifier, name, kind, material,
                                              coordinates, polygon, prep(polygon)))
        except (ValueError, TypeError):
            continue
    return CompiledEnvironment(tuple(regions))


def compile_environment(floor):
    """Return cached immutable geometry; call only for enabled outdoor mode."""
    records = floor.get("environment", []) if isinstance(floor, dict) else []
    return _compile(_environment_signature(records))


def normalize_environment(records):
    """JSON-safe validated polygons for storage, with invalid polygons omitted."""
    compiled = _compile(_environment_signature(records))
    return [{"id": r.identifier, "name": r.name, "type": r.kind,
             "material": r.material,
             "points": [{"x": x, "y": y} for x, y in r.coordinates]}
            for r in compiled.regions]


def _xy(value):
    if isinstance(value, dict):
        values = (value.get("x"), value.get("y"))
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        values = value[:2]
    else:
        return None
    result = tuple(finite_number(v, minimum=-MAX_COORDINATE_PX,
                                 maximum=MAX_COORDINATE_PX) for v in values)
    return result if all(v is not None for v in result) else None


def _boundary_positions(geometry, line):
    if geometry.is_empty:
        return []
    if hasattr(geometry, "geoms"):
        return [position for g in geometry.geoms for position in _boundary_positions(g, line)]
    if geometry.geom_type == "Point":
        return [line.project(geometry)]
    if geometry.geom_type in {"LineString", "LinearRing"}:
        return [line.project(Point(geometry.coords[0])),
                line.project(Point(geometry.coords[-1]))]
    return []


def _interior_transitions(region, line):
    """Count true wall transitions, excluding tangent touches and endpoints.

    Sampling the open intervals between boundary intersections distinguishes
    entry/exit from tangency. Boundary-only intervals do not invent a crossing.
    Endpoint points on a wall have no implied state beyond the measured path.
    """
    positions = sorted(set([0.0, line.length, *_boundary_positions(
        line.intersection(region.polygon.boundary), line)]))
    states = []
    length_inside = 0.0
    for start, end in zip(positions, positions[1:]):
        if end - start <= 1e-9:
            continue
        point = line.interpolate((start + end) / 2)
        if region.prepared.contains(point):
            states.append(True)
            length_inside += end - start
        elif not region.polygon.boundary.covers(point):
            states.append(False)
    return sum(a != b for a, b in zip(states, states[1:])), length_inside


def path_reliability(compiled, receiver_xy, fix_xy):
    """Analyze receiver-to-fix path without ever changing measured distances."""
    result = {"environmental_weight": 1.0, "classification": "clear",
              "building_crossings": 0, "building_paths": 0,
              "vegetation_paths": 0, "custom_paths": 0,
              "receiver_inside_building": False, "reflection_risk": False,
              "intersections": []}
    start, end = _xy(receiver_xy), _xy(fix_xy)
    if start is None or end is None:
        result.update(environmental_weight=MIN_ENVIRONMENT_WEIGHT,
                      classification="unknown", invalid_path=True)
        return result
    if not isinstance(compiled, CompiledEnvironment) or not compiled.regions:
        return result
    line = LineString([start, end])
    point = Point(start)
    kinds = set()
    for region in compiled.regions:
        inside = region.kind == "building" and region.prepared.contains(point)
        result["receiver_inside_building"] |= inside
        if line.length <= 1e-9 or not region.prepared.intersects(line):
            continue
        crossings, interior_length = _interior_transitions(region, line)
        if interior_length <= 1e-9:
            continue
        kinds.add(region.kind)
        if region.kind == "building":
            weight = BUILDING_BOUNDARY_WEIGHTS[region.material] ** crossings
            result["reflection_risk"] |= region.material == "metal"
            result["building_crossings"] += crossings
            result["building_paths"] += 1
        else:
            weight = VEGETATION_WEIGHTS[region.kind]
            key = "custom_paths" if region.kind == "custom" else "vegetation_paths"
            result[key] += 1
        result["environmental_weight"] *= weight
        result["intersections"].append({"id": region.identifier, "type": region.kind,
                                         "material": region.material,
                                         "boundary_crossings": crossings,
                                         "path_length_px": interior_length,
                                         "weight": weight})
    result["environmental_weight"] = max(MIN_ENVIRONMENT_WEIGHT,
                                           result["environmental_weight"])
    for kind in ("building", "dense_trees", "light_vegetation", "custom"):
        if kind in kinds:
            result["classification"] = kind
            break
    return result


def measurement_reliability(compiled, receiver_xy, fix_xy, *, policy="auto",
                            age_s=None, max_age_s=30.0, base_weight=1.0):
    """Combine optional policy/freshness with path trust, once per measurement.

    Missing age retains existing freshness behavior. Known readings past the
    caller's maximum age and the explicit ignore policy are excluded (zero);
    otherwise even heavily obstructed paths retain a safe nonzero weight.
    """
    result = path_reliability(compiled, receiver_xy, fix_xy)
    policy = receiver_policy(policy)
    age = finite_number(age_s, None, minimum=0.0)
    maximum = finite_number(max_age_s, 30.0, minimum=0.0)
    freshness_reference = maximum if maximum > 0 else 30.0
    freshness = 1.0 if age is None else max(0.1, min(1.0, 1.5 - age / freshness_reference))
    excluded = policy == "ignore" or (maximum > 0 and age is not None and age >= maximum)
    environment = 1.0 if policy == "normal" else result["environmental_weight"]
    preference = {"prefer": 1.15, "deprioritize": 0.5}.get(policy, 1.0)
    base = finite_number(base_weight, 1.0, minimum=0.0, maximum=1.0)
    # The safety floor must never inflate a pre-existing weak jump weight.
    # Explicit Prefer may increase it, but default clear paths stay identical.
    effective = min(1.0, max(min(base, MIN_RELIABILITY),
                             base * environment * freshness * preference))
    result.update(policy=policy, reading_age_s=age, freshness_weight=freshness,
                  policy_weight=preference, environmental_weight=environment,
                  reliability_weight=0.0 if excluded else effective,
                  used=not excluded, excluded=excluded,
                  status="excluded" if excluded else ("downweighted" if effective < 0.999 else "used"))
    return result
