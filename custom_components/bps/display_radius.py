"""A map-display limit, kept separate from tracking uncertainty and confidence."""
import math
from collections.abc import Mapping

from .environment import finite_number


def reporting_radius_limit(floor, point, diagnostics, now, max_age_s=30.0):
    """Use the third-nearest distinct, fresh, used receiver; fewer three has no cap.

    The raw diagnostic uncertainty is never clipped. This is a visual footprint
    of reporting stations, not an estimate of accuracy or a confidence interval.
    """
    if not isinstance(floor, Mapping) or not isinstance(point, (list, tuple)) or len(point) != 2:
        return {}
    scale = finite_number(floor.get("scale"), minimum=1e-6, maximum=1e6)
    xy = [finite_number(v, minimum=-1e9, maximum=1e9) for v in point]
    now = finite_number(now, minimum=0)
    age_limit = finite_number(max_age_s, 30.0, minimum=0, maximum=1e6) or 30.0
    if scale is None or now is None or any(v is None for v in xy):
        return {}
    if not isinstance(diagnostics, (list, tuple)):
        return {}
    used = set()
    for row in diagnostics:
        if not isinstance(row, Mapping) or row.get("used", True) is False or row.get("excluded") is True or row.get("status") == "excluded":
            continue
        observed = finite_number(row.get("observed"), minimum=0)
        name = row.get("receiver")
        weight = finite_number(row.get("reliability_weight"), minimum=0)
        if (isinstance(name, str) and observed is not None and 0 <= now - observed < age_limit
                and weight is not None and weight > 0):
            used.add(name)
    distances = {}
    for receiver in floor.get("receivers", []):
        if not isinstance(receiver, Mapping) or receiver.get("entity_id") not in used or receiver.get("outdoor_policy") == "ignore":
            continue
        cords = receiver.get("cords")
        if not isinstance(cords, Mapping):
            continue
        rx, ry = (finite_number(cords.get(k), minimum=-1e9, maximum=1e9) for k in ("x", "y"))
        if rx is None or ry is None:
            continue
        name = receiver["entity_id"]
        distances.setdefault(name, math.hypot(rx - xy[0], ry - xy[1]) / scale)
    if len(distances) < 3:
        return {}
    nearest = sorted(distances.items(), key=lambda item: (item[1], item[0]))[:3]
    radius = nearest[-1][1] * 1.25
    if not math.isfinite(radius):
        return {}
    return {"display_radius_limit_m": radius,
            "display_radius_limit_method": "third_nearest_reporting_receiver_1_25",
            "display_radius_receivers": [{"receiver": name, "distance_m": distance} for name, distance in nearest]}
