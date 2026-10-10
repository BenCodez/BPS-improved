"""Repeatable crash-contained stress: python tests/helpers/outdoor_stress.py.

Only standard-library imports precede the standalone production module import.
Tests launch this in a subprocess with a timeout; native geometry must never be
loaded. Resource limits contain regressions without affecting the pytest host.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import importlib
import json
import math
from pathlib import Path
import sys
import threading
import time
import types


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=12, choices=range(8, 17))
    parser.add_argument("--iterations", type=int, default=3072)
    args = parser.parse_args()
    if args.iterations < args.workers:
        parser.error("iterations must be at least workers")
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
        # glibc reserves virtual allocator arenas for each thread; allow those
        # mappings while bounding runaway memory use in this child process.
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except ImportError:
        pass
    source = Path(__file__).resolve().parents[2] / "custom_components" / "bps" / "environment.py"
    # Support relative pure-Python helpers without executing Home Assistant's
    # integration bootstrap (which imports unrelated native positioning code).
    package = types.ModuleType("bps")
    package.__path__ = [str(source.parent)]
    sys.modules["bps"] = package
    environment = importlib.import_module("bps.environment")
    assert not any(name == "shapely" or name.startswith("shapely.") for name in sys.modules), (
        "Live outdoor geometry loaded Shapely")

    def floor(version):
        x = version % 160
        square = [{"x": x, "y": 0}, {"x": x + 10, "y": 0},
                  {"x": x + 10, "y": 10}, {"x": x, "y": 10}]
        return {"environment": [
            {"id": f"shop-{version}", "type": "building", "material": "metal",
             "wall_materials": ["wood", None, "heavy", "wood"], "points": square},
            {"id": f"trees-{version}", "type": "dense_trees", "points": copy.deepcopy(square)}]}

    # Held snapshots outlive hundreds of replacement maps and cache eviction.
    held = [environment.compile_environment(floor(version)) for version in range(8)]
    held_results = [environment.path_reliability(snapshot, (index - 5, 5), (index + 15, 5))
                    for index, snapshot in enumerate(held)]
    barrier = threading.Barrier(args.workers)
    current_maps = [floor(index) for index in range(8)]  # Two beacons for four dogs.
    map_lock = threading.Lock()
    started = time.perf_counter()

    def worker(worker_id):
        completed = measurements = 0
        barrier.wait(timeout=10)
        for iteration in range(worker_id, args.iterations, args.workers):
            dog_beacon = iteration % 8
            version = iteration % 160
            replacement = floor(version)
            with map_lock:
                current_maps[dog_beacon] = replacement
                captured = current_maps[dog_beacon]
            if iteration % 127 == 0:
                # Reload-style cache resets must not invalidate calculations
                # already holding a compiled environment in another worker.
                environment._compile.cache_clear()
            geometry = environment.compile_environment(captured)
            # Original input is mutable; compiled snapshots must own immutable
            # values. Every worker has its own replacement map after capture.
            replacement["environment"][0]["points"][0]["x"] -= 500
            replacement["environment"][0]["wall_materials"][0] = "unknown"
            x = version % 160
            for receiver in range(8):
                start = (x - 5, 1 + receiver)
                fix = (x + 15, 1 + receiver)
                result = environment.measurement_reliability(
                    geometry, start, fix, age_s=iteration % 40, base_weight=0.8,
                    policy=("auto", "prefer", "deprioritize", "normal")[receiver % 4])
                assert result["building_crossings"] == 2
                assert result["building_paths"] == result["vegetation_paths"] == 1
                assert math.isfinite(result["reliability_weight"])
                assert 0.0 <= result["reliability_weight"] <= 1.0
                assert result["excluded"] == (iteration % 40 >= 30)
                json.dumps(result, allow_nan=False)
                measurements += 1
            old_index = iteration % len(held)
            assert environment.path_reliability(held[old_index],
                (old_index - 5, 5), (old_index + 15, 5)) == held_results[old_index]
            completed += 1
        return completed, measurements

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        totals = list(executor.map(worker, range(args.workers)))
    for index, snapshot in enumerate(held):
        assert environment.path_reliability(snapshot, (index - 5, 5), (index + 15, 5)) == held_results[index]
    native_loaded = any(name == "shapely" or name.startswith("shapely.") for name in sys.modules)
    assert not native_loaded
    print(json.dumps({"workers": args.workers, "iterations": sum(n for n, _ in totals),
                      "measurements": sum(n for _, n in totals),
                      "cache_size": environment._compile.cache_info().currsize,
                      "native_geometry_loaded": native_loaded,
                      "elapsed_s": round(time.perf_counter() - started, 3)}))


if __name__ == "__main__":
    main()
