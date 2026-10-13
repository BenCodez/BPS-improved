"""Narrow, authenticated phone GPS inventory and optional map-anchor limits."""
import math
import re
import time

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

MAX_LOCATIONS = 128
MAX_ANCHORS = 12
MAX_AGE_S = 60
MAX_PROPERTY_M = 5000
ENTITY_ID = re.compile(r"(?:device_tracker|person)\.[a-z0-9_]{1,128}\Z", re.ASCII)


def _number(value, minimum, maximum):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and minimum <= value <= maximum and math.isfinite(value))


def validate_georeference(layout):
    """Reject unbounded/malformed private anchors before the atomic layout save."""
    if not isinstance(layout, dict):
        return None
    floors = layout.get("floor", [])
    for floor in floors if isinstance(floors, list) else []:
        if not isinstance(floor, dict) or "georeference" not in floor:
            continue
        geo = floor["georeference"]
        if (not isinstance(geo, dict) or set(geo) != {"version", "anchors"}
                or isinstance(geo["version"], bool) or geo["version"] != 1
                or not isinstance(geo["anchors"], list) or len(geo["anchors"]) > MAX_ANCHORS):
            return "Georeference requires version 1 and at most 12 anchors"
        origin = None
        for anchor in geo["anchors"]:
            if (not isinstance(anchor, dict)
                    or set(anchor) - {"label", "latitude", "longitude", "x", "y", "accuracy_m"}
                    or not isinstance(anchor.get("label"), str) or len(anchor["label"]) > 80
                    or not _number(anchor.get("latitude"), -85, 85)
                    or not _number(anchor.get("longitude"), -180, 180)
                    or not _number(anchor.get("x"), 0, 1e6) or not _number(anchor.get("y"), 0, 1e6)
                    or not _number(anchor.get("accuracy_m"), 0, 1000)):
                return "Georeference anchors require valid GPS, map pixels, label and accuracy"
            if origin is None:
                origin = anchor
            # Shortest longitude difference also supports properties at the dateline.
            east = math.radians((anchor["longitude"] - origin["longitude"] + 180) % 360 - 180)
            east *= 6371008.8 * math.cos(math.radians(origin["latitude"]))
            north = math.radians(anchor["latitude"] - origin["latitude"]) * 6371008.8
            if math.hypot(east, north) > MAX_PROPERTY_M:
                return "Georeference anchors must lie within a 5 km property"
    return None


def gps_locations(hass, now=None):
    """Expose GPS fields from fresh HA states, never arbitrary attributes.

    last_updated is HA publication time; GPS measurement time is not known.
    """
    now = time.time() if now is None else now
    rows = []
    for domain in ("device_tracker", "person"):
        for state in hass.states.async_all(domain):
            eid = getattr(state, "entity_id", "")
            attrs = getattr(state, "attributes", {})
            if not isinstance(eid, str) or not ENTITY_ID.fullmatch(eid) or not isinstance(attrs, dict):
                continue
            if attrs.get("source_type") not in (None, "gps") or state.state in ("unknown", "unavailable"):
                continue
            lat, lon, accuracy = (attrs.get(k) for k in ("latitude", "longitude", "gps_accuracy"))
            if not (_number(lat, -85, 85) and _number(lon, -180, 180) and _number(accuracy, 0.1, 1000)):
                continue
            try:
                timestamp = state.last_updated.timestamp()
            except (AttributeError, ValueError, TypeError, OverflowError, OSError):
                continue
            if not _number(timestamp, 1, now + 5) or not 0 <= now - timestamp <= MAX_AGE_S:
                continue
            name = attrs.get("friendly_name")
            rows.append({"entity_id": eid, "name": name[:128] if isinstance(name, str) else eid,
                         "latitude": lat, "longitude": lon, "gps_accuracy": accuracy,
                         "last_updated": timestamp, "age_s": now - timestamp})
            if len(rows) >= MAX_LOCATIONS:
                return rows
    return rows


class BPSGPSLocationsAPI(HomeAssistantView):
    url = "/api/bps/gps_locations"
    name = "api:bps:gps_locations"
    requires_auth = True

    async def get(self, request):
        return web.json_response({"locations": gps_locations(request.app["hass"])})
