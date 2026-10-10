"""Optional map obstruction heuristics; coordinates are always floor pixels.

The factors below express relative trust, not RF loss or distance corrections.
They are intentionally conservative starting points for empirical tuning. No
input receiver, radius, or calibration factor is ever changed by this module.
"""
from dataclasses import dataclass
from functools import lru_cache
import logging
import math
from numbers import Real
from threading import Lock
import time

from .environment_geometry import compile_ring, interior_path, point_relation

_LOGGER = logging.getLogger(__name__)
_WARNING_LOCK = Lock()
_last_geometry_warning = -math.inf
# Bounded lock stripes coalesce identical cold misses without retaining keys
# or serializing path calculations. Hash collisions only delay compilation.
_COMPILE_LOCKS = tuple(Lock() for _ in range(64))

MAX_ENVIRONMENT_POLYGONS = 128
MAX_ENVIRONMENT_VERTICES = 256
MAX_COORDINATE_PX = 10_000_000.0
MIN_RELIABILITY = 0.05
MIN_ENVIRONMENT_WEIGHT = 0.15
BUILDING_BOUNDARY_WEIGHTS = {"unknown": 0.80, "light": 0.90,
                             "wood": 0.90, "heavy": 0.65, "metal": 0.50}
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


def reading_max_age(layout, default=30.0):
    """Shared reading gate: finite nonnegative numbers, zero disables aging."""
    layout = layout if isinstance(layout, dict) else {}
    default = finite_number(default, 30.0, minimum=0.0)
    # Match the existing outdoor guard without narrowing valid legacy limits.
    outdoor = layout.get("outdoor_tracking")
    maximum = 1e6 if isinstance(outdoor, dict) and outdoor.get("enabled") is True else None
    return finite_number(layout.get("reading_max_age"), default, minimum=0.0, maximum=maximum)


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
        walls = record.get("wall_materials")
        if (not isinstance(walls, list) or len(walls) != len(coordinates)
                or any(w is not None and (not isinstance(w, str)
                       or w not in BUILDING_BOUNDARY_WEIGHTS) for w in walls)):
            walls = None
        signature.append((identifier, name, kind, material, tuple(coordinates),
                          tuple(walls) if walls is not None else None))
    return tuple(signature)


@dataclass(frozen=True)
class EnvironmentRegion:
    identifier: str
    name: str
    kind: str
    material: str
    coordinates: tuple
    wall_materials: tuple | None
    edges: tuple
    bounds: tuple


@dataclass(frozen=True)
class CompiledEnvironment:
    regions: tuple


@lru_cache(maxsize=64)
def _compile_cached(signature):
    regions = []
    for identifier, name, kind, material, coordinates, walls in signature:
        ring = compile_ring(coordinates)
        if ring is None:
            continue
        edges, bounds = ring
        regions.append(EnvironmentRegion(identifier, name, kind, material,
                                          coordinates, walls, edges, bounds))
    return CompiledEnvironment(tuple(regions))


def _compile(signature):
    # lru_cache alone can run its body repeatedly for simultaneous misses.
    # Recheck the cache while holding the signature's stripe so one worker
    # validates a new map, and its waiting peers reuse the completed snapshot.
    with _COMPILE_LOCKS[hash(signature) % len(_COMPILE_LOCKS)]:
        return _compile_cached(signature)


_compile.cache_info = _compile_cached.cache_info
_compile.cache_clear = _compile_cached.cache_clear


def compile_environment(floor):
    """Return cached immutable numeric geometry, safe to share across workers."""
    records = floor.get("environment", []) if isinstance(floor, dict) else []
    return _compile(_environment_signature(records))


def normalize_environment(records):
    """JSON-safe validated polygons for storage, with invalid polygons omitted."""
    compiled = _compile(_environment_signature(records))
    return [{"id": r.identifier, "name": r.name, "type": r.kind,
             "material": r.material,
             "points": [{"x": x, "y": y} for x, y in r.coordinates],
             **({"wall_materials": list(r.wall_materials)} if r.wall_materials is not None else {})}
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


def _crossed_wall(region, point):
    """At a corner, count one crossing using the less trusted adjacent wall."""
    matches = []
    coords = region.coordinates
    for i, (ax, ay) in enumerate(coords):
        bx, by = coords[(i + 1) % len(coords)]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        if length2 <= 1e-18:
            continue
        t = max(0.0, min(1.0, ((point[0] - ax) * dx + (point[1] - ay) * dy) / length2))
        if math.hypot(point[0] - ax - t * dx, point[1] - ay - t * dy) <= 1e-6:
            material = (region.wall_materials[i] if region.wall_materials else None) or region.material
            matches.append((i + 1, material))
    material = min((m for _, m in matches), key=BUILDING_BOUNDARY_WEIGHTS.get) if matches else region.material
    return {"walls": [i for i, _ in matches], "material": material,
            "point": list(point)}


def _path_result():
    return {"environmental_weight": 1.0, "classification": "clear",
              "building_crossings": 0, "building_paths": 0,
              "vegetation_paths": 0, "custom_paths": 0,
              "receiver_inside_building": False, "reflection_risk": False,
              "intersections": []}


def report_geometry_failure(error):
    """Bound unexpected-error logging across workers; never log observations."""
    global _last_geometry_warning
    with _WARNING_LOCK:
        now = time.monotonic()
        if now - _last_geometry_warning < 60:
            return
        _last_geometry_warning = now
    _LOGGER.warning("Outdoor geometry failed; using neutral environmental trust (%s)",
                    type(error).__name__, exc_info=True)


def path_reliability(compiled, receiver_xy, fix_xy):
    """Analyze paths; an unexpected geometry failure removes only obstruction trust."""
    try:
        return _path_reliability(compiled, receiver_xy, fix_xy)
    except Exception as error:
        report_geometry_failure(error)
        result = _path_result()
        result.update(classification="unknown", environment_error=type(error).__name__)
        return result


def _path_reliability(compiled, receiver_xy, fix_xy):
    result = _path_result()
    start, end = _xy(receiver_xy), _xy(fix_xy)
    if start is None or end is None:
        result.update(environmental_weight=MIN_ENVIRONMENT_WEIGHT,
                      classification="unknown", invalid_path=True)
        return result
    if not isinstance(compiled, CompiledEnvironment) or not compiled.regions:
        return result
    kinds = set()
    for region in compiled.regions:
        inside = region.kind == "building" and point_relation(start, region.edges, region.bounds) == 1
        result["receiver_inside_building"] |= inside
        crossing_points, interior_length = interior_path(start, end, region.edges, region.bounds)
        crossings = len(crossing_points)
        if interior_length <= 1e-9:
            continue
        kinds.add(region.kind)
        if region.kind == "building":
            walls = [_crossed_wall(region, p) for p in crossing_points]
            weight = math.prod(BUILDING_BOUNDARY_WEIGHTS[w["material"]] for w in walls)
            result["reflection_risk"] |= any(w["material"] == "metal" for w in walls) or (
                not walls and "metal" in tuple(m or region.material for m in
                    (region.wall_materials or (region.material,))))
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
                                         **({"wall_crossings": walls} if region.kind == "building" else {}),
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
