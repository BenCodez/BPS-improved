"""Private phone pressure inventory and bounded common-floor survey provenance."""
import math
import re
import time

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers import entity_registry as er

MAX_SOURCES = 128
MAX_AGE_S = 60
MAX_SESSION_S = 900
SENSOR_ID = re.compile(r"sensor\.[a-z0-9_]{1,128}\Z", re.ASCII)
PRESSURE_UNITS = {"hPa": 1, "mbar": 1, "Pa": .01, "kPa": 10}


def _number(value, low, high):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and low <= value <= high and math.isfinite(value))


def pressure_hpa(value, unit):
    """HA sensor states are strings; accept only supported pressure units."""
    if not isinstance(unit, str) or unit not in PRESSURE_UNITS or isinstance(value, bool):
        return None
    try:
        result = float(value) * PRESSURE_UNITS[unit]
    except (ValueError, TypeError, OverflowError):
        return None
    return result if _number(result, 300, 1100) else None


def height_sources(hass, now=None):
    """Discover mobile_app pressure sensors without guessing which phone is owned.

    HA publication age is not raw measurement age. Include unavailable devices
    so the user can distinguish an absent barometer from a disabled/stale one.
    No location, device identifiers or arbitrary attributes leave this boundary.
    """
    now = time.time() if now is None else now
    try:
        registry = er.async_get(hass)
    except (AttributeError, RuntimeError):
        return []
    rows = []
    for entry in registry.entities.values():
        eid = getattr(entry, "entity_id", "")
        if (getattr(entry, "platform", None) != "mobile_app"
                or not isinstance(eid, str) or not SENSOR_ID.fullmatch(eid)):
            continue
        state = hass.states.get(eid)
        attrs = getattr(state, "attributes", {})
        if not isinstance(attrs, dict):
            continue
        device_class = attrs.get("device_class") if state is not None else (
            getattr(entry, "device_class", None) or getattr(entry, "original_device_class", None))
        if device_class not in ("pressure", "atmospheric_pressure"):
            continue
        unit = attrs.get("unit_of_measurement") if state is not None else getattr(entry, "unit_of_measurement", None)
        if not isinstance(unit, str) or unit not in PRESSURE_UNITS:
            continue
        pressure = pressure_hpa(getattr(state, "state", None), unit)
        try:
            age = now - state.last_updated.timestamp()
        except (AttributeError, ValueError, TypeError, OverflowError, OSError):
            age = None
        available = pressure is not None and _number(age, 0, MAX_AGE_S)
        name = attrs.get("friendly_name") if state is not None else (
            getattr(entry, "name", None) or getattr(entry, "original_name", None))
        rows.append({"entity_id": eid, "name": name[:128] if isinstance(name, str) else eid,
                     "kind": "pressure", "available": available,
                     "pressure_hpa": pressure if available else None,
                     "age_s": age if available else None, "timestamp_kind": "ha_state_update"})
        if len(rows) >= MAX_SOURCES:
            break
    return rows


class BPSHeightSourcesAPI(HomeAssistantView):
    url = "/api/bps/height_sources"
    name = "api:bps:height_sources"
    requires_auth = True

    async def get(self, request):
        return web.json_response({"sources": height_sources(request.app["hass"])})


def _summary_valid(summary, method):
    keys = {"value", "sample_count", "span_s", "age_s", "spread_m", "reported_accuracy_m", "timestamp_kind"}
    if not isinstance(summary, dict) or set(summary) != keys:
        return False
    if (not isinstance(summary["sample_count"], int) or isinstance(summary["sample_count"], bool)
            or not 2 <= summary["sample_count"] <= 180
            or not _number(summary["span_s"], 20, 45)
            or not _number(summary["age_s"], 0, MAX_AGE_S)
            or not _number(summary["spread_m"], 0, .6 if method == "pressure" else 2)):
        return False
    if method == "pressure":
        return (_number(summary["value"], 300, 1100) and summary["reported_accuracy_m"] is None
                and summary["timestamp_kind"] == "ha_state_update")
    return (_number(summary["value"], -1000, 20000)
            and _number(summary["reported_accuracy_m"], .01, 2)
            and summary["timestamp_kind"] == "browser_position")


def validate_height_surveys(layout):
    """Validate retained metadata, including its common-floor conversion."""
    if not isinstance(layout, dict):
        return None
    floors = layout.get("floor", [])
    for floor in floors if isinstance(floors, list) else []:
        receivers = floor.get("receivers", []) if isinstance(floor, dict) else []
        for rec in receivers if isinstance(receivers, list) else []:
            if not isinstance(rec, dict) or "height_survey" not in rec:
                continue
            survey = rec["height_survey"]
            keys = {"version", "reference", "method", "source", "baseline_phone_height_m", "station_offset_m",
                    "delta_m", "estimate_m", "uncertainty_m", "baseline_age_s", "confirmed_at", "baseline", "station"}
            error = "Height survey requires bounded confirmed common-floor provenance"
            if (not isinstance(survey, dict) or set(survey) != keys
                    or type(survey["version"]) is not int or survey["version"] != 1
                    or survey["reference"] != "common_floor" or survey["method"] not in ("pressure", "gps")):
                return error
            method, source = survey["method"], survey["source"]
            if (not isinstance(source, str)
                    or (method == "gps" and source != "browser")
                    or (method == "pressure" and not SENSOR_ID.fullmatch(source))
                    or not _summary_valid(survey["baseline"], method) or not _summary_valid(survey["station"], method)):
                return error
            for field, low, high in (("baseline_phone_height_m", 0, 10), ("station_offset_m", -10, 10),
                    ("delta_m", -20, 20), ("estimate_m", 0, 10), ("uncertainty_m", 0, 2),
                    ("baseline_age_s", 0, MAX_SESSION_S), ("confirmed_at", 1, 1e11)):
                if not _number(survey[field], low, high):
                    return error
            if not _number(rec.get("height"), 0, 10):
                return error
            base, station = survey["baseline"], survey["station"]
            delta = (44330 * (1 - (station["value"] / base["value"]) ** .1903)
                     if method == "pressure" else station["value"] - base["value"])
            uncertainty = sum(max(.5 if method == "pressure" else s["reported_accuracy_m"], s["spread_m"])
                              for s in (base, station))
            estimate = survey["baseline_phone_height_m"] + delta + survey["station_offset_m"]
            if (abs(delta - survey["delta_m"]) > .002 or abs(estimate - survey["estimate_m"]) > .002
                    or abs(rec["height"] - survey["estimate_m"]) > .002
                    or abs(uncertainty - survey["uncertainty_m"]) > .002 or uncertainty > 2):
                return "Height survey conversion or vertical uncertainty disagrees with receiver height"
    return None
