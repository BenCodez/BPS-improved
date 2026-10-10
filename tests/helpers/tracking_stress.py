"""Crash-contained numeric tracking stress with the real SciPy solver."""
import os

# Avoid an independent BLAS worker pool per executor thread in the test child.
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import math
from pathlib import Path
import runpy
import time

try:
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (55, 55))
    resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
except ImportError:
    pass

runpy.run_path(str(Path(__file__).resolve().parents[1] / "conftest.py"))
import bps
from bps.environment import _compile
from bps.outdoor_tracking import solve_outdoor
from bps.tracker_groups import fuse_group

WORKERS, ITERATIONS = 12, 2048
REGIONS = [{"id": "shop", "type": "building", "material": "metal",
            "wall_materials": ["wood", "metal", "wood", None],
            "points": [{"x": 10, "y": 0}, {"x": 40, "y": 0},
                       {"x": 40, "y": 100}, {"x": 10, "y": 100}]},
           {"id": "trees", "type": "dense_trees", "points": [
               {"x": 75, "y": 0}, {"x": 90, "y": 0},
               {"x": 90, "y": 100}, {"x": 75, "y": 100}]}]


def update(iteration):
    truth = (50 + 12 * math.sin(iteration / 30), 50 + 8 * math.cos(iteration / 25))
    positions = []
    for beacon in range(2):
        floor = {"receivers": [], "environment": copy.deepcopy(REGIONS)}
        # Churn the bounded map cache without mutating another job's snapshot.
        floor["environment"][0]["id"] = "shop-" + str(iteration % 128)
        if iteration % 127 == 0:
            _compile.cache_clear()
        weighted = []
        for receiver in range(8):
            angle = 2 * math.pi * receiver / 8
            x, y = 50 + 45 * math.cos(angle), 50 + 45 * math.sin(angle)
            r = math.dist((x, y), truth)
            age = 31 if iteration % 29 == 0 else (receiver % 4)
            floor["receivers"].append({"entity_id": str(receiver), "distance": r / 10,
                "cords": {"x": x, "y": y, "r": r}, "correction": 1.2,
                "_outdoor_reading": {"measured_distance_m": r / 10, "reading_age_s": age,
                                     "observed": 1000 - age}})
            weighted.append((x, y, r, 1.0, r))
        before = copy.deepcopy(floor)
        result = solve_outdoor(floor, weighted, (-20, -20, 120, 120),
                               10, 5, 30, None, bps.trilaterate)
        assert floor == before
        if iteration % 29 == 0:
            assert result is None
            continue
        assert result is not None
        assert math.dist(result["fix"], truth) < 1e-4
        assert all(pt[2] == original[2] for pt, original in zip(result["weighted"], weighted))
        positions.append({"ent": "beacon_" + str(beacon), "cords": list(result["fix"]),
                          "floor": "Yard", "updated": 1000, "rms_m": 0,
                          "outdoor": result["outdoor"]})
    if positions:
        group = fuse_group({"id": "dog", "name": "Dog", "enabled": True,
                            "beacons": ["beacon_0", "beacon_1"]}, positions, {"Yard": 10}, 1000)
        assert group["outdoor"]["beacons_used"] == 2
        assert math.dist(group["cords"], truth) < 1e-4
        assert math.isfinite(group["outdoor"]["estimated_uncertainty_m"])
        json.dumps(group, allow_nan=False)
    return len(positions)


if __name__ == "__main__":
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        solved = sum(executor.map(update, range(ITERATIONS)))
    print(json.dumps({"workers": WORKERS, "iterations": ITERATIONS,
                      "beacon_fixes": solved, "elapsed_s": round(time.perf_counter() - started, 3)}))
