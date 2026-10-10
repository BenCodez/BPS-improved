# Outdoor geometry and Home Assistant process stability

The reported fatal Python stacks terminate in Shapely prepared `intersects`,
called by BPS `path_reliability` in executor workers. The old 64-entry environment
cache stored `Polygon` and `PreparedGeometry` objects and returned the same native
objects to different tracking workers. Frozen dataclasses did not establish
native object ownership or thread safety.

This confirms exposure to shared native outdoor geometry. It does **not** prove
which GEOS/Shapely operation, version, object lifetime, or concurrency condition
caused the native fault. The supplied dumps have no timestamps; the separate
October 10 restart and unclean SQLite shutdown cannot be attributed to these
dumps. The installed versions on that Home Assistant host were not supplied.

## Mitigation

`environment.py` and `environment_geometry.py` use no Shapely/GEOS. Their bounded
cache holds frozen dataclasses containing only strings, floats and tuples. Cache
eviction, map replacement and retained worker snapshots therefore cannot expose
a shared GEOS pointer on this path. Catching an exception or adding a GEOS lock
would not provide this structural protection against a segmentation fault.

The pure Python implementation validates simple polygon rings, rejects
self-intersections/backtracking and uses adaptive exact orientation predicates
near collinearity. Consecutive duplicate/closing vertices preserve original wall
indexes. A half-open sweep of path intersections handles corners, tangencies,
endpoint touches and boundary overlaps without inventing walls. Strict interior
length, mixed wall materials, vegetation weights and reflection flags retain the
existing public JSON semantics. There are no data migrations or new dependencies.

Validation costs at most O(vertices²) per polygon, only on a cache miss. A path
sweep costs O(vertices log vertices); wall identification is linear per crossing.
The existing limits remain 128 polygons, 256 vertices each, 64 cached signatures,
and coordinates within ±10,000,000 pixels. Configuration validation and outdoor
solving remain executor work. No expensive calculation was moved onto HA's loop.

Unexpected recoverable geometry errors use **neutral obstruction weighting**,
while preserving receiver policies, stale exclusions and original measured
distances. Diagnostics disclose the fallback and mark the source/group confidence
poor. Warnings are limited to one per minute. Invalid path coordinates retain the
previous conservative invalid-path behavior. A fallback never changes calibration
factors or fabricates a corrected BLE distance.

## Scheduling and ownership audit

| Work | Ownership and scheduling |
| --- | --- |
| Tracker refresh | One batch per HA instance; at most eight trackers active. Physical executor admission survives cancellation/reload until the private jobs finish. Failed siblings settle before groups or another cycle. Detached per-tracker layouts go to workers. |
| Map edits / reload / unload | Layout identity and lifecycle checks reject obsolete post-executor results before election, Kalman state, history or sensors change. Tracking is invalidated, cancelled and awaited before teardown; failed unload resumes a fresh generation. Deferred startup cannot resurrect an unloaded integration. |
| Group zone assignment | Each executor job receives an unpublished fused fix and detached layout. Results are checked against layout/lifecycle before publication. |
| Calibration | Numeric samples/receiver records are detached. Auto/manual sampling loops await their own solves and stop on cancellation. Concurrent manual API solves are rejected; obsolete results cannot replace newer candidates after session, map or lifecycle changes. |
| History / diagnostic capture | Existing async locks and snapshots retain their ordering. Diagnostic packing runs off the loop. |
| Self-tests / zone-adjust previews / exports | Demand-driven executor work uses private data/geometry. These requests are separate from the periodic tracker limit. |

Shapely remains a dependency for **legacy zone/no-go geometry and zone-adjustment
tools**. Those objects are created per call, rather than stored in the outdoor
environment cache. The existing indoor solver/zone path is unchanged. This fix
removes the reported shared outdoor GEOS execution path; it cannot guarantee that
every native dependency in Home Assistant is incapable of crashing.

## Verification

Reference tests use Shapely only as a serial oracle, covering convex/concave rings,
multiple regions, walls/corners/endpoints, collinear runs, duplicate vertices,
extreme valid coordinates and malformed inputs. No intentional RF behavior change
was found. Separate lifecycle tests suspend actual executor work and verify that
map replacement, cancellation and reload cannot publish stale results.

Repeat crash-contained stress tests from the repository root:

```sh
python tests/helpers/outdoor_stress.py --workers 16 --iterations 4096
python tests/helpers/tracking_stress.py
```

Both children have CPU/memory/core-dump limits; their pytest launchers impose wall
timeouts. The outdoor helper performs 32,768 measurements with concurrent map
replacement, cache eviction/reset and retained snapshots, and verifies that no
Shapely module loaded. The tracking helper performs 2,048 moving-dog updates with
12 workers, 3,954 valid beacon fixes, stale exclusions, real SciPy solves and
two-beacon fusion. Coordinates, ranges and corrections remain unchanged by trust
processing.

The original shared-GEOS implementation also completed the isolated 16-worker
stress run on Python 3.14.7 / Shapely 2.2.0 / GEOS 3.14.1. Its crash was **not
reproduced** in this environment. Structural removal of GEOS from outdoor geometry
is the mitigation evidence; passing tests alone are not proof of a historical
native fault's root cause.

Python 3.12.14 and 3.14.7 were tested using actual NumPy/SciPy/Shapely libraries and
the existing HA runtime stubs. CI runs both Python versions. The 3.14 environment
uses NumPy 2.5.4, SciPy 1.18.1, Shapely 2.2.0 and GEOS 3.14.1. These checks do not
substitute for testing the user's live Home Assistant/Bermuda installation.

## Performance

Local Python 3.12.14 comparison against `main`'s original implementation
(Shapely 2.1.2 / GEOS 3.13.1). Each path crosses every overlapping polygon; figures
are elapsed time, not a guarantee on a Home Assistant host.

| Polygons × vertices | GEOS compile | Python compile | GEOS per path | Python per path |
| --- | ---: | ---: | ---: | ---: |
| 8 × 4 | 0.47 ms | 0.38 ms | 1.43 ms | 0.11 ms |
| 32 × 64 | 3.75 ms | 38.25 ms | 39.96 ms | 2.92 ms |
| 128 × 256 | 51.71 ms | 2,258.95 ms | 535.11 ms | 42.87 ms |

Repeated path calculations improved in these fixtures. Cold compilation of a
maximum-size map is slower, but occurs off the loop and is cached. The 16-worker
geometry stress finished in 1.80 seconds on Python 3.14; the real-solver tracking
stress finished in 14.14 seconds. Hardware, geometry and concurrency affect these
figures; do not infer RF accuracy from this benchmark.

## Manual validation after installation

1. Install a release containing the fix and restart Home Assistant. Confirm the
   existing maps, groups, individual entities and calibration settings remain.
2. In BPS, track both dogs with their groups. Check indoor and outdoor movement,
   small member icons, one uncertainty circle per displayed tracker and stale
   behavior when a beacon stops reporting.
3. Test a building with wood and metal walls plus a tree area. Tracking diagnostics
   should show the expected crossed wall materials and bounded trust weights;
   measured distances and calibration factors should not change when drawing areas.
4. Edit/save the map during tracking and reload BPS. Deleted/old map coordinates
   must not return from a delayed calculation. Calibration Solve must refuse a
   duplicate in-flight request and discard work made obsolete by Cancel or edits.
5. Record known-position diagnostics and monitor Core uptime/logs. If a fatal dump
   recurs, retain its complete stack and record Python, Shapely and GEOS versions,
   Core version, and the restart time. New outdoor stacks should contain no
   Shapely/GEOS calls. Report any fallback warning along with the recording.
