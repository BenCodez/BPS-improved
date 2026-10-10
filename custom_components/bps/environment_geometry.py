"""Bounded planar geometry for outdoor trust calculations, without GEOS.

Only immutable Python tuples/numbers leave compilation. Predicates use an
adaptive exact fallback for nearly collinear binary-float coordinates. Rings
are simple (holes are not part of the environment configuration format).
"""
from fractions import Fraction
import math


def _area(a, b, c):
    """Signed double triangle area; exact arithmetic near cancellation."""
    first = (b[0] - a[0]) * (c[1] - a[1])
    second = (b[1] - a[1]) * (c[0] - a[0])
    determinant = first - second
    if abs(determinant) > 8e-16 * (abs(first) + abs(second)):
        return determinant
    # Fraction(float) represents the input binary value exactly, unlike a
    # fixed decimal precision or an epsilon that can erase a narrow feature.
    ax, ay = map(Fraction, a)
    bx, by = map(Fraction, b)
    cx, cy = map(Fraction, c)
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _boxes_overlap(a, b, c, d):
    return (max(min(a[0], b[0]), min(c[0], d[0])) <= min(max(a[0], b[0]), max(c[0], d[0]))
            and max(min(a[1], b[1]), min(c[1], d[1])) <= min(max(a[1], b[1]), max(c[1], d[1])))


def _segments_meet(a, b, c, d):
    if not _boxes_overlap(a, b, c, d):
        return False
    p, q = _area(a, b, c), _area(a, b, d)
    r, s = _area(c, d, a), _area(c, d, b)
    return ((p <= 0 <= q or q <= 0 <= p)
            and (r <= 0 <= s or s <= 0 <= r))


def compile_ring(coordinates):
    """Return indexed edges and bounds, or None for a non-simple/empty ring.

    Keep original edge indexes for per-wall material compatibility, including
    consecutive duplicate vertices and an explicitly repeated closing vertex.
    Validation is O(vertices²), once per cached configuration, with box pruning.
    """
    edges = tuple((a, coordinates[(i + 1) % len(coordinates)], i)
                  for i, a in enumerate(coordinates)
                  if a != coordinates[(i + 1) % len(coordinates)])
    if len(edges) < 3:
        return None
    origin = coordinates[0]
    area = math.fsum(float(_area(origin, a, b)) for a, b, _ in edges)
    if abs(area) <= 2e-9:
        return None
    for i, (a, b, _) in enumerate(edges):
        for j in range(i + 1, len(edges)):
            c, d, _ = edges[j]
            if j == i + 1 or (i == 0 and j == len(edges) - 1):
                # Adjacent edges may share a vertex but cannot double back
                # along the same wall. Ordinary collinear forward edges pass.
                before, corner, after = (a, b, d) if j == i + 1 else (c, d, b)
                if _area(before, corner, after) == 0 and (
                        (corner[0] - before[0]) * (after[0] - corner[0])
                        + (corner[1] - before[1]) * (after[1] - corner[1]) < 0):
                    return None
            elif _segments_meet(a, b, c, d):
                return None
    xs, ys = zip(*coordinates)
    return edges, (min(xs), min(ys), max(xs), max(ys))


def point_relation(point, edges, bounds):
    """Return 1 inside, 0 on the boundary, -1 outside a simple ring."""
    x, y = point
    if not bounds[0] <= x <= bounds[2] or not bounds[1] <= y <= bounds[3]:
        return -1
    inside = False
    for a, b, _ in edges:
        area = _area(a, b, point)
        if area == 0 and min(a[0], b[0]) <= x <= max(a[0], b[0]) and min(a[1], b[1]) <= y <= max(a[1], b[1]):
            return 0
        if (a[1] > y) != (b[1] > y) and ((area > 0) == (b[1] > a[1])):
            inside = not inside
    return 1 if inside else -1


def interior_path(start, end, edges, bounds):
    """Return true wall transition points and length strictly inside a ring.

    Intersect the polygon with the infinite directed path using half-open
    vertex rules, then sweep crossing/collinear-overlap events in O(n log n).
    Boundary intervals have no interior state. Skipping them before comparing
    adjacent states preserves tangency, corner and wall-overlap behavior without
    a point-in-polygon test for every interval (which would be quadratic).
    """
    length = math.dist(start, end)
    if length <= 1e-9 or not _boxes_overlap(start, end, bounds[:2], bounds[2:]):
        return [], 0.0
    delta = (end[0] - start[0], end[1] - start[1])
    axis = 0 if abs(delta[0]) >= abs(delta[1]) else 1

    def parameter(point):
        return (point[axis] - start[axis]) / delta[axis]

    events = {0.0: [0, 0], 1.0: [0, 0]}
    inside = False
    for a, b, _ in edges:
        va, vb = _area(start, end, a), _area(start, end, b)
        if va == 0 and vb == 0:
            low, high = sorted((parameter(a), parameter(b)))
            low, high = max(0.0, low), min(1.0, high)
            if low < high:
                events.setdefault(low, [0, 0])[1] += 1
                events.setdefault(high, [0, 0])[1] -= 1
        elif (va > 0) != (vb > 0):
            # A vertex on the line belongs to exactly one half-plane. Two
            # touches at a tangent vertex cancel; a genuine crossing toggles.
            if va == 0:
                crossing = parameter(a)
            elif vb == 0:
                crossing = parameter(b)
            else:
                fraction = float(va / (va - vb))
                intersection = (a[axis] + fraction * (b[axis] - a[axis]))
                crossing = (intersection - start[axis]) / delta[axis]
            if crossing < 0:
                inside = not inside
            elif crossing <= 1:
                events.setdefault(crossing, [0, 0])[0] += 1
    positions = sorted(events)
    boundary = 0
    states = []
    interior = 0.0
    for low, high in zip(positions, positions[1:]):
        toggles, overlaps = events[low]
        if toggles % 2:
            inside = not inside
        boundary += overlaps
        interval_length = (high - low) * length
        if interval_length <= 1e-9 or boundary:
            continue
        states.append((inside, low, high))
        if inside:
            interior += interval_length
    crossings = []
    for previous, current in zip(states, states[1:]):
        if previous[0] != current[0]:
            t = current[1] if current[0] else previous[2]
            crossings.append((start[0] + t * delta[0], start[1] + t * delta[1]))
    return crossings, interior
