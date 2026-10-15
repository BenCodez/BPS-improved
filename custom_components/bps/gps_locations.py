"""Narrow, authenticated phone GPS inventory and optional map-anchor limits."""
import math
import re
import time

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers import entity_registry as er

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


def gps_sources(hass, now=None):
    """List GPS identities for selection, including those without a fresh fix.

    Inventory is intentionally separate from ``gps_locations``: an identity
    being selectable never makes its last coordinates usable for a survey.
    """
    now = time.time() if now is None else now
    states = {}
    for domain in ("device_tracker", "person"):
        for state in hass.states.async_all(domain):
            eid = getattr(state, "entity_id", None)
            if isinstance(eid, str) and ENTITY_ID.fullmatch(eid):
                states[eid] = state

    try:
        registry = er.async_get(hass)
        entries = getattr(registry, "entities", {})
        entries = entries.values() if hasattr(entries, "values") else ()
    except (AttributeError, KeyError, TypeError):
        entries = ()
    mobile_ids = {
        entry.entity_id for entry in entries
        if isinstance(getattr(entry, "entity_id", None), str)
        and ENTITY_ID.fullmatch(entry.entity_id)
        and entry.entity_id.startswith("device_tracker.")
        and getattr(entry, "platform", None) == "mobile_app"
    }

    def gps_evidence(eid, state):
        attrs = getattr(state, "attributes", {}) if state is not None else {}
        if not isinstance(attrs, dict):
            attrs = {}
        source_type = attrs.get("source_type")
        # An explicit router/BLE source cannot become a GPS phone merely
        # because another integration supplied coordinates or registry data.
        if source_type not in (None, "gps"):
            return False
        if source_type == "gps" or eid in mobile_ids:
            return True
        return all(_number(attrs.get(key), low, high) for key, low, high in (
            ("latitude", -85, 85), ("longitude", -180, 180), ("gps_accuracy", 0.1, 1000)))

    tracker_ids = {eid for eid, state in states.items()
                   if eid.startswith("device_tracker.") and gps_evidence(eid, state)}
    tracker_ids.update(eid for eid in mobile_ids if gps_evidence(eid, states.get(eid)))
    person_ids = set()
    for eid, state in states.items():
        if not eid.startswith("person."):
            continue
        attrs = getattr(state, "attributes", {})
        if isinstance(attrs, dict) and attrs.get("source_type") not in (None, "gps"):
            continue
        if (isinstance(attrs, dict) and isinstance(attrs.get("source"), str)
                and attrs["source"].startswith("device_tracker.")
                and attrs["source"] not in tracker_ids):
            continue
        if gps_evidence(eid, state):
            person_ids.add(eid)
        elif isinstance(attrs, dict):
            refs = attrs.get("device_trackers")
            if isinstance(refs, (list, tuple)) and any(ref in tracker_ids for ref in refs if isinstance(ref, str)):
                person_ids.add(eid)
            elif isinstance(attrs.get("source"), str) and attrs["source"] in tracker_ids:
                person_ids.add(eid)

    rows = []
    for eid in sorted(tracker_ids | person_ids)[:MAX_LOCATIONS]:
        state = states.get(eid)
        attrs = getattr(state, "attributes", {}) if state is not None else {}
        attrs = attrs if isinstance(attrs, dict) else {}
        name = attrs.get("friendly_name")
        name = name[:128] if isinstance(name, str) and name else eid
        try:
            timestamp = state.last_updated.timestamp()
        except (AttributeError, ValueError, TypeError, OverflowError, OSError):
            timestamp = None
        age = now - timestamp if _number(timestamp, 1, now + 5) and timestamp <= now else None
        status, reason = "unavailable", "no_valid_fix"
        if state is not None and state.state in ("unknown", "unavailable"):
            reason = state.state
        elif state is None:
            reason = "no_state"
        elif age is None:
            reason = "missing_timestamp"
        elif not all(_number(attrs.get(key), low, high) for key, low, high in (
                ("latitude", -85, 85), ("longitude", -180, 180), ("gps_accuracy", 0.1, 1000))):
            reason = "invalid_location"
        elif age > MAX_AGE_S:
            status, reason = "stale", "state_older_than_60s"
        else:
            status, reason = "fresh", "fresh_state"
        rows.append({"entity_id": eid, "name": name, "status": status,
                     "reason": reason, "age_s": age})
    return rows


class BPSGPSLocationsAPI(HomeAssistantView):
    url = "/api/bps/gps_locations"
    name = "api:bps:gps_locations"
    requires_auth = True

    async def get(self, request):
        hass, now = request.app["hass"], time.time()
        return web.json_response({"locations": gps_locations(hass, now),
                                  "sources": gps_sources(hass, now)})
