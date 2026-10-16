"""Conservative, instantaneous moving GPS references; never calibration input."""

import copy
import hashlib
import json
import math

from .gps_locations import _number
from .phone_survey import _fit, _local

SPEED_LIMITS = {"walk": 3.0, "ride": 10.0}
MAX_FIX_AGE_S = 3.0
GUIDE = (
    "The phone is a moving reference, not a measurement of the dog's exact location. "
    "Tolerance includes reported GPS accuracy, saved alignment uncertainty, the "
    "declared phone-to-dog allowance and motion during GPS age. BLE observations "
    "and filtered positions retain their own clocks and may lag. These captures "
    "must not be used as stationary calibration or independent GPS ground truth."
)


def prepare_reference(snapshot, marker, gps):
    """Project against this snapshot's saved alignment, retaining original clocks."""
    if not isinstance(marker, dict) or set(marker) - {"target", "floor", "label"}:
        raise ValueError("GPS marks require a target, saved floor and optional label")
    required = {"latitude", "longitude", "accuracy_m", "timestamp_ms", "mode",
                "dog_distance_m", "session_id", "alignment", "layout_revision"}
    if not isinstance(gps, dict) or set(gps) - required - {"speed_m_s", "heading_deg"} or not required <= gps.keys():
        raise ValueError("Supply a complete browser GPS reference")
    for field, lo, hi in (("latitude", -85, 85), ("longitude", -180, 180),
                          ("accuracy_m", 0.1, 20), ("dog_distance_m", 0, 30),
                          ("timestamp_ms", 1, 1e15)):
        if not _number(gps.get(field), lo, hi):
            raise ValueError(f"GPS {field} is invalid or outside the survey limit")
    mode = gps["mode"]
    if not isinstance(mode, str) or mode not in SPEED_LIMITS:
        raise ValueError("Select walking or a slow ride")
    session = gps["session_id"]
    if not isinstance(session, str) or not 16 <= len(session) <= 80 or not all(c.isascii() and (c.isalnum() or c in "_-") for c in session):
        raise ValueError("Supply a GPS survey session ID")
    age = snapshot["time"] - gps["timestamp_ms"] / 1000
    if not -1 <= age <= MAX_FIX_AGE_S:
        raise ValueError("GPS fix is stale or the phone/server clocks disagree; restart with a fresh fix")
    age = max(0, age)
    for field, hi in (("speed_m_s", 100), ("heading_deg", 360)):
        if gps.get(field) is not None and not _number(gps[field], 0, hi):
            raise ValueError(f"GPS {field} is invalid")
    speed_limit = SPEED_LIMITS[mode]
    if gps.get("speed_m_s") is not None and gps["speed_m_s"] > speed_limit:
        raise ValueError("Slow down before recording GPS survey points")
    context = snapshot.get("context", {})
    revision = gps["layout_revision"]
    if not isinstance(revision, str) or len(revision) != 64 or revision != context.get("layout_revision"):
        raise ValueError("Saved BPS settings changed; reload them before starting another survey")
    layout = context.get("layout", {})
    floors = [f for f in layout.get("floor", []) if f.get("name") == marker.get("floor")]
    if len(floors) != 1 or not _number(floors[0].get("scale"), 1e-6, 1e6):
        raise ValueError("Select a saved floor with a metric scale")
    floor = floors[0]
    alignment = {"scale": floor["scale"], "georeference": floor.get("georeference")}
    if gps["alignment"] != alignment:
        raise ValueError("Saved GPS alignment changed; reload it before starting another survey")
    transform = _fit(floor)
    x, y = _local(gps, transform["origin"])
    px = transform["a"] * x - transform["b"] * y + transform["tx"]
    py = transform["b"] * x + transform["a"] * y + transform["ty"]
    anchor_error = transform["anchor_accuracy_m"] * (
        1 + math.dist((x, y), transform["center"]) / transform["baseline_m"]
    ) + transform["residual_m"]
    # Convert the fit's GPS metres to the saved BPS metric frame, even when
    # the informational GPS scale and the floor's manually set scale differ.
    ratio = transform["scale"] / floor["scale"]
    components = {"gps_m": gps["accuracy_m"] * ratio,
                  "alignment_m": anchor_error * ratio,
                  "dog_distance_m": gps["dog_distance_m"] * ratio,
                  "gps_age_motion_m": speed_limit * age * ratio}
    tolerance = sum(components.values())
    if tolerance > 100:
        raise ValueError("GPS/alignment uncertainty exceeds 100 m; improve the alignment or GPS fix")
    resolved = {**marker, "x_m": px / floor["scale"], "y_m": py / floor["scale"],
                "accuracy_m": tolerance}
    reference = {"format": "bps-moving-gps-reference-v1", "guide": GUIDE,
                 "source": "browser_geolocation", "timestamp_kind": "browser_position_epoch_ms",
                 "request": copy.deepcopy({"marker": marker, "gps": gps}),
                 "gps_observed_at_epoch_s": gps["timestamp_ms"] / 1000,
                 "ble_snapshot_at_epoch_s": snapshot["time"], "gps_age_s": age,
                 "motion_speed_bound_m_s": speed_limit, "tolerance_components": components,
                 "accuracy_m": tolerance, "stationary_calibration_eligible": False,
                 "alignment": {"anchors": len(floor["georeference"]["anchors"]),
                               "independently_checked": len(floor["georeference"]["anchors"]) >= 3,
                               "fit_residual_m": transform["residual_m"],
                               "gps_to_bps_scale_ratio": ratio},
                 "alignment_sha256": hashlib.sha256(json.dumps(alignment, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()}
    return resolved, reference
