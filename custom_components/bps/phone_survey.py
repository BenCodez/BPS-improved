"""Opt-in phone GPS collection; every receiver move requires verification.

Range evidence and HA GPS are read by the server. Browser fixes retain their
original clock, aligned once at session start; polling never refreshes a fix.
Candidates live in private HA storage, independently of the saved floor plan.
"""

import asyncio
import copy
import json
import math
import re
import secrets
import time

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.storage import Store

from .gps_locations import ENTITY_ID, gps_locations, validate_georeference, _number
from .receiver_sources import ReceiverSources
from .storage import (
    BPS_FILE_LOCK,
    get_bps_data,
    get_bps_data_for_edit,
    layout_revision,
    save_verified_store,
    save_bps_data,
)
from .calibration import refresh_receivers_from_coords

EARTH_M = 6371008.8
MAX_CANDIDATES = 32
MAX_STORE_BYTES = 768000
MAX_SESSIONS = 8
MAX_SAMPLES = 180
MAX_CAPTURE_S = 60
MAX_GPS_AGE_S = 60
MAX_RANGE_AGE_S = 5
SESSION_IDLE_S = 90
NOTIFY_SERVICE = re.compile(r"mobile_app_[a-z0-9_]{1,128}\Z", re.ASCII)


def _floor(layout, name):
    floors = layout.get("floor", []) if isinstance(layout, dict) else []
    matches = [f for f in floors if isinstance(f, dict) and f.get("name") == name]
    if len(matches) != 1:
        raise ValueError("Select one saved floor")
    return matches[0]


def survey_context(floor):
    """Only coordinate/identity context; concurrent height/calibration edits survive."""
    return {
        "floor": {
            k: copy.deepcopy(v)
            for k, v in floor.items()
            if k not in ("receivers", "zones", "subzones", "environment")
        },
        "receivers": [
            {
                k: copy.deepcopy(r.get(k))
                for k in ("entity_id", "scanner_mac", "scanner_config_entry_id")
            }
            for r in floor.get("receivers", [])
        ],
    }


def _placement(floor, receiver):
    matches = [r for r in floor.get("receivers", []) if r.get("entity_id") == receiver]
    if len(matches) != 1:
        raise ValueError("Receiver placement changed or is ambiguous")
    return {k: matches[0].get("cords", {}).get(k) for k in ("x", "y")}


def _known_gps_entity(hass, entity_id):
    if not isinstance(entity_id, str) or not ENTITY_ID.fullmatch(entity_id):
        return False
    lookup = getattr(hass.states, "get", None)
    state = (
        lookup(entity_id)
        if lookup
        else next(
            (
                s
                for s in hass.states.async_all(entity_id.split(".", 1)[0])
                if s.entity_id == entity_id
            ),
            None,
        )
    )
    attrs = getattr(state, "attributes", {})
    return (
        isinstance(attrs, dict)
        and attrs.get("source_type") in (None, "gps")
        and _number(attrs.get("latitude"), -85, 85)
        and _number(attrs.get("longitude"), -180, 180)
        and _number(attrs.get("gps_accuracy"), 0.1, 1000)
    )


def _provenance(raw):
    """Retain source clocks/identity, never arbitrary or unbounded attributes."""
    result = {}
    for key in (
        "path",
        "entry_id",
        "device_id",
        "scanner_mac",
        "scanner_id",
        "status",
        "reason",
        "model_version",
    ):
        if isinstance(raw.get(key), str):
            result[key] = raw[key][:256]
    for key in ("version", "source_observed", "source_age_s", "accepted_age_s"):
        if _number(raw.get(key), 0, 1e15):
            result[key] = raw[key]
    quality = raw.get("quality")
    if isinstance(quality, dict):
        result["quality"] = {
            k: quality[k][:32]
            for k in ("mode", "status")
            if isinstance(quality.get(k), str)
        }
        result["quality"].update(
            {
                k: quality[k]
                for k in ("last_valid_stamp", "age")
                if _number(quality.get(k), 0, 1e15)
            }
        )
    return result


def _local(p, origin):
    x = math.radians((p["longitude"] - origin["longitude"] + 180) % 360 - 180)
    x *= EARTH_M * math.cos(math.radians(origin["latitude"]))
    y = -math.radians(p["latitude"] - origin["latitude"]) * EARTH_M
    if math.hypot(x, y) > 5000:
        raise ValueError("GPS moved or jumped outside the property")
    return x, y


def _fit(floor):
    if validate_georeference({"floor": [floor]}):
        raise ValueError("Save valid GPS/map anchors first")
    anchors = floor.get("georeference", {}).get("anchors", [])
    if not 2 <= len(anchors) <= 12:
        raise ValueError("Save at least two separated GPS/map anchors first")
    points = [_local(a, anchors[0]) for a in anchors]
    for i, p in enumerate(points):
        for j in range(i):
            if (
                math.dist(p, points[j]) < 0.1
                or math.hypot(
                    anchors[i]["x"] - anchors[j]["x"], anchors[i]["y"] - anchors[j]["y"]
                )
                < 1
            ):
                raise ValueError("Anchors must identify distinct GPS and map points")
    cx, cy = (sum(p[k] for p in points) / len(points) for k in (0, 1))
    mx, my = (sum(a[k] for a in anchors) / len(anchors) for k in ("x", "y"))
    denom = sum((x - cx) ** 2 + (y - cy) ** 2 for x, y in points)
    variance = sum((a["x"] - mx) ** 2 + (a["y"] - my) ** 2 for a in anchors)
    if min(denom, variance) / len(anchors) < 1:
        raise ValueError("Separate anchors by several metres")
    a = (
        sum(
            (x - cx) * (p["x"] - mx) + (y - cy) * (p["y"] - my)
            for (x, y), p in zip(points, anchors)
        )
        / denom
    )
    b = (
        sum(
            (x - cx) * (p["y"] - my) - (y - cy) * (p["x"] - mx)
            for (x, y), p in zip(points, anchors)
        )
        / denom
    )
    scale = math.hypot(a, b)
    if not _number(scale, 0.01, 100000):
        raise ValueError("GPS/map anchors are ill-conditioned")
    tx, ty = mx - a * cx + b * cy, my - b * cx - a * cy
    residual = math.sqrt(
        sum(
            ((a * x - b * y + tx - p["x"]) ** 2 + (b * x + a * y + ty - p["y"]) ** 2)
            / scale**2
            for (x, y), p in zip(points, anchors)
        )
        / len(anchors)
    )
    accuracy = max(p["accuracy_m"] for p in anchors)
    if residual > max(5, accuracy * 2):
        raise ValueError("GPS/map anchors disagree")
    return {
        "origin": anchors[0],
        "a": a,
        "b": b,
        "tx": tx,
        "ty": ty,
        "scale": scale,
        "residual_m": residual,
        "anchor_accuracy_m": accuracy,
        "center": (cx, cy),
        "baseline_m": math.sqrt(denom / len(anchors)),
    }


def average_gps(samples, now):
    """Arithmetic mean, without claiming independent or calibrated GPS errors."""
    recent = [s for s in samples if 0 <= now - s["observed"] <= MAX_GPS_AGE_S]
    if not recent:
        raise ValueError("Need distinct GPS fixes spanning at least 20 seconds")
    points = [_local(s, recent[0]) for s in recent]
    x, y = (sum(p[k] for p in points) / len(points) for k in (0, 1))
    spread = max(math.hypot(px - x, py - y) for px, py in points)
    accuracy = max(s["accuracy_m"] for s in recent)
    if spread > max(5, accuracy * 2):
        raise ValueError("GPS moved or jumped; stand still and capture again")
    # Even an ineligible late source can contradict a stationary capture.
    if len(recent) < 2 or recent[-1]["observed"] - recent[0]["observed"] < 20:
        raise ValueError("Need distinct GPS fixes spanning at least 20 seconds")
    latitude = recent[0]["latitude"] - y / EARTH_M * 180 / math.pi
    longitude = (
        recent[0]["longitude"]
        + x / (EARTH_M * math.cos(math.radians(recent[0]["latitude"]))) * 180 / math.pi
        + 180
    ) % 360 - 180
    return {
        "latitude": latitude,
        "longitude": longitude,
        "accuracy_m": max(accuracy, spread),
        "reported_accuracy_m": accuracy,
        "spread_m": spread,
        "sample_count": len(recent),
        "observed": recent[-1]["observed"],
        "source": recent[0]["source"],
        "timestamp_kind": recent[0]["timestamp_kind"],
        "samples": copy.deepcopy(recent),
    }


class PhoneSurvey:
    def __init__(
        self, hass, snapshot, inventory, *, clock=time.time, sources=ReceiverSources
    ):
        self.hass, self.snapshot, self.inventory = hass, snapshot, inventory
        self.clock, self.sources = clock, sources
        self.store = Store(hass, 1, "bps_phone_survey")
        self.candidates, self.sessions = [], {}
        self.lock = asyncio.Lock()
        self.loaded = False
        self.gps_requests = {}

    async def _load(self):
        if self.loaded:
            return
        payload = await self.store.async_load()
        if payload is not None:
            if (
                not isinstance(payload, dict)
                or payload.get("version") != 1
                or not isinstance(payload.get("candidates"), list)
                or len(payload["candidates"]) > MAX_CANDIDATES
                or len(json.dumps(payload, allow_nan=False).encode()) > MAX_STORE_BYTES
            ):
                raise ValueError(
                    "Saved phone survey data is invalid; no candidates were loaded"
                )
            for c in payload["candidates"]:
                if (
                    not isinstance(c, dict)
                    or not all(
                        isinstance(c.get(k), str)
                        for k in ("id", "owner", "floor", "phone", "receiver")
                    )
                    or not isinstance(c.get("context"), dict)
                    or not isinstance(c.get("scanner_identity"), list)
                    or len(c["scanner_identity"]) != 2
                    or not isinstance(c.get("position"), dict)
                    or not all(
                        _number(c["position"].get(k), 0, 1e6) for k in ("x", "y")
                    )
                    or not isinstance(c.get("original_position"), dict)
                    or not all(
                        _number(c["original_position"].get(k), 0, 1e6)
                        for k in ("x", "y")
                    )
                    or c.get("status") not in ("pending", "verified")
                    or not isinstance(c.get("gps"), dict)
                    or not all(
                        _number(c["gps"].get(k), lo, hi)
                        for k, lo, hi in (
                            ("latitude", -85, 85),
                            ("longitude", -180, 180),
                            ("accuracy_m", 0.1, 1000),
                            ("reported_accuracy_m", 0.1, 1000),
                            ("spread_m", 0, 2000),
                            ("sample_count", 2, MAX_SAMPLES),
                        )
                    )
                    or not isinstance(c["gps"].get("samples"), list)
                    or len(c["gps"]["samples"]) > MAX_SAMPLES
                ):
                    raise ValueError("Saved phone survey candidate is invalid")
            self.candidates = copy.deepcopy(payload["candidates"])
        self.loaded = True

    async def _persist(self, candidates):
        payload = {"version": 1, "candidates": candidates}
        if (
            len(candidates) > MAX_CANDIDATES
            or len(json.dumps(payload, allow_nan=False).encode()) > MAX_STORE_BYTES
        ):
            raise ValueError("Phone survey storage is full; remove an old candidate")
        await save_verified_store(
            self.hass, self.store, payload, key="bps_phone_survey"
        )
        self.candidates = candidates

    def _known(self):
        return [
            r
            for r in self.inventory(self.hass).get("trackers", [])
            if isinstance(r.get("key"), str) and r.get("group") is not True
        ][:128]

    def _notify_services(self):
        services = getattr(self.hass, "services", None)
        lookup = getattr(services, "async_services", None)
        rows = lookup().get("notify", {}) if lookup else {}
        return [
            {"key": key, "name": "notify." + key}
            for key in sorted(rows)
            if NOTIFY_SERVICE.fullmatch(key)
        ][:128]

    def _expire(self, now):
        self.sessions = {
            key: s
            for key, s in self.sessions.items()
            if now - s["touched"] <= SESSION_IDLE_S
        }

    def _session(self, token, owner, now):
        self._expire(now)
        s = self.sessions.get(token)
        if not s or s["owner"] != owner:
            raise ValueError("Phone survey stopped or expired; start again")
        if s["phone"] not in {r["key"] for r in self._known()}:
            self.sessions.pop(token, None)
            raise ValueError("Selected BLE phone is no longer available")
        floor = _floor(get_bps_data(self.hass), s["floor"])
        if survey_context(floor) != s["context"]:
            self.sessions.pop(token, None)
            raise ValueError(
                "Saved floor, anchors or receiver placements changed; start again"
            )
        if (
            s["capture"]
            and _placement(floor, s["capture"]["receiver"])
            != s["capture"]["original_position"]
        ):
            self.sessions.pop(token, None)
            raise ValueError("Receiver placement changed during capture; start again")
        return s, floor

    async def get(self, owner):
        async with self.lock:
            await self._load()
            return {
                "trackers": self._known(),
                "candidates": copy.deepcopy(
                    [c for c in self.candidates if c["owner"] == owner]
                ),
                "server_time": self.clock(),
                "notify_services": self._notify_services(),
            }

    async def action(self, owner, body):
        async with self.lock:
            await self._load()
            now = self.clock()
            action = body.get("action")
            if action == "start":
                self._expire(now)
                if len(self.sessions) >= MAX_SESSIONS:
                    raise ValueError("Too many active phone surveys")
                phone, name = body.get("phone"), body.get("floor")
                if phone not in {r["key"] for r in self._known()}:
                    raise ValueError("Select an actual Bermuda BLE phone tracker")
                if not isinstance(name, str) or not 1 <= len(name) <= 128:
                    raise ValueError("Select a saved floor")
                client_time = body.get("browser_time_ms")
                if not _number(client_time, 1, 1e15):
                    raise ValueError("Browser clock is unavailable")
                gps_entity = body.get("gps_entity", "")
                # A stale but real GPS source may be revived by the opted-in
                # Companion App command. It contributes no sample until fresh.
                if gps_entity and not _known_gps_entity(self.hass, gps_entity):
                    raise ValueError(
                        "Select an actual HA GPS entity or use browser GPS"
                    )
                floor = _floor(get_bps_data(self.hass), name)
                _fit(floor)
                receiver = body.get("receiver", "")
                if not isinstance(receiver, str) or (
                    receiver
                    and len(
                        [
                            r
                            for r in floor.get("receivers", [])
                            if r.get("entity_id") == receiver
                        ]
                    )
                    != 1
                ):
                    raise ValueError(
                        "Select an existing saved receiver or use automatic detection"
                    )
                request_gps = body.get("request_gps", False)
                if not isinstance(request_gps, bool):
                    raise ValueError("GPS request opt-in must be true or false")
                notify_service = body.get("notify_service", "")
                if request_gps and notify_service not in {
                    r["key"] for r in self._notify_services()
                }:
                    raise ValueError(
                        "Explicitly select your phone's Mobile App notification service"
                    )
                token = secrets.token_urlsafe(24)
                self.sessions[token] = {
                    "owner": owner,
                    "phone": phone,
                    "floor": name,
                    "context": survey_context(floor),
                    "touched": now,
                    "gps_entity": gps_entity,
                    "clock_offset": now - client_time / 1000,
                    "client_started": client_time,
                    "capture": None,
                    "completed": set(),
                    "request_gps": request_gps,
                    "notify_service": notify_service,
                }
                self.sessions[token]["receiver"] = receiver
                return {
                    "session": token,
                    "server_time": now,
                    "status": "Walk to a receiver and stand still",
                }
            if action == "stop":
                token = body.get("session")
                if token in self.sessions and self.sessions[token]["owner"] == owner:
                    self.sessions.pop(token)
                return {"stopped": True}
            if action == "poll":
                return await self._poll(owner, body, now)
            if action == "verify":
                return await self._verify(owner, body.get("candidate_id"), now)
            if action == "remove":
                wanted = body.get("candidate_id")
                await self._persist(
                    [
                        c
                        for c in self.candidates
                        if c["id"] != wanted or c["owner"] != owner
                    ]
                )
                return {"removed": True}
            raise ValueError("Unknown phone survey action")

    def _nearby(self, s, floor, now):
        snap = self.snapshot(self.hass, [s["phone"]], now)
        if snap.get("context", {}).get("target_members", {}).get(s["phone"]) != [
            s["phone"]
        ]:
            raise ValueError("Selected BLE phone membership changed")
        sources = self.sources(self.hass)
        near = []
        bindings = sources.floor_bindings(floor)
        for rec, binding in zip(floor.get("receivers", []), bindings):
            if s["receiver"] and rec.get("entity_id") != s["receiver"]:
                continue
            identity = binding.get("identity")
            if (
                binding.get("status") != "matched"
                or not identity
                or tuple(identity) in s["completed"]
            ):
                continue
            rows = [
                r
                for r in snap.get("readings", [])
                if r.get("tracker") == s["phone"]
                and r.get("floor") == s["floor"]
                and r.get("receiver") == rec.get("entity_id")
            ]
            if len(rows) != 1:
                continue
            row = rows[0]
            # Use the canonical source resolver again. Slug-only or mismatched
            # publication evidence cannot authorize a scanner move.
            source, state = sources.source(s["phone"], rec, binding, now=now)
            if state is None or source != row.get("entity_id"):
                continue
            if (
                row.get("status") != "current"
                or row.get("ignored")
                or row.get("assumed_unit_m")
                or row.get("unit") not in ("m", "ft")
                or not _number(row.get("measured_distance_m"), 0, 1)
                or not _number(row.get("age_s"), 0, MAX_RANGE_AGE_S)
                or not _number(row.get("observed"), 1, now)
                or now - row["observed"] > MAX_RANGE_AGE_S
            ):
                continue
            provenance = row.get("measurement_source", {})
            if provenance.get("path") == "direct" and (
                provenance.get("entry_id"),
                provenance.get("scanner_mac"),
            ) != tuple(identity):
                continue
            source_clock = (
                provenance.get("quality", {}).get("last_valid_stamp")
                if isinstance(provenance.get("quality"), dict)
                else None
            )
            if source_clock is None:
                source_clock = provenance.get("source_observed", row["observed"])
            if not _number(source_clock, 0, 1e15):
                continue
            near.append(
                {
                    "receiver": rec["entity_id"],
                    "identity": list(identity),
                    "observed": row["observed"],
                    "source_clock": source_clock,
                    "distance_m": row["measured_distance_m"],
                    "age_s": row["age_s"],
                    "source": source,
                    "measurement_source": _provenance(provenance),
                    "received": now,
                }
            )
        # Two nearby physical scanners are ambiguous. Do not pick one by order.
        return (near[0] if len(near) == 1 else None), len(near)

    def _add_sample(self, c, raw, source, observed, kind):
        if (
            not isinstance(raw, dict)
            or not _number(raw.get("latitude"), -85, 85)
            or not _number(raw.get("longitude"), -180, 180)
            or not _number(raw.get("accuracy_m"), 0.1, 1000)
            or not _number(observed, c["started"], self.clock())
            or self.clock() - observed > MAX_GPS_AGE_S
        ):
            return
        key = (
            (source, raw.get("timestamp"))
            if source == "browser"
            else (source, raw["latitude"], raw["longitude"], raw["accuracy_m"])
        )
        rows = c["samples"].setdefault(source, [])
        if key in c["seen"] or len(rows) >= MAX_SAMPLES:
            return
        c["seen"].add(key)
        rows.append(
            {
                "latitude": raw["latitude"],
                "longitude": raw["longitude"],
                "accuracy_m": raw["accuracy_m"],
                "timestamp": raw["timestamp"],
                "observed": observed,
                "source": source,
                "timestamp_kind": kind,
            }
        )
        rows.sort(key=lambda r: r["observed"])

    async def _poll(self, owner, body, now):
        s, floor = self._session(body.get("session"), owner, now)
        previous_check = s["touched"]
        gps_request = await self._request_gps(s, now)
        # A Mobile App service call can suspend this coroutine. Re-read saved
        # context and range on the current server clock after it completes.
        now = self.clock()
        s, floor = self._session(body.get("session"), owner, now)
        gap = now - previous_check
        s["touched"] = now
        near, nearby_count = self._nearby(s, floor, now)
        c = s["capture"]
        if near is None:
            s["capture"] = None
            return {
                "capturing": False,
                "gps_request": gps_request,
                "status": (
                    "Several receivers are within 1 m. Select an existing Receiver / anchor point, then restart auto survey."
                    if nearby_count > 1
                    else "Waiting for one proven receiver within 1 m of the selected BLE phone"
                ),
            }
        if c is None or gap > MAX_RANGE_AGE_S or c["identity"] != near["identity"]:
            c = s["capture"] = {
                "receiver": near["receiver"],
                "identity": near["identity"],
                "started": now,
                "proof": [],
                "samples": {},
                "seen": set(),
                "original_position": _placement(floor, near["receiver"]),
            }
        if c["proof"] and near["source_clock"] < c["proof"][-1]["source_clock"]:
            s["capture"] = None
            return {
                "capturing": False,
                "status": "BLE source clock changed; capture restarted",
            }
        if not c["proof"] or near["source_clock"] != c["proof"][-1]["source_clock"]:
            if len(c["proof"]) >= 64:
                c["proof"].pop(0)
            c["proof"].append(near)
        raw_samples = body.get("browser_samples", [])
        if not isinstance(raw_samples, list) or len(raw_samples) > MAX_SAMPLES:
            raise ValueError("Upload at most 180 browser GPS fixes")
        for raw in raw_samples:
            if isinstance(raw, dict) and _number(
                raw.get("timestamp"), s["client_started"], 1e15
            ):
                self._add_sample(
                    c,
                    raw,
                    "browser",
                    raw["timestamp"] / 1000 + s["clock_offset"],
                    "browser_position",
                )
        if s["gps_entity"]:
            for row in gps_locations(self.hass, now):
                if row["entity_id"] == s["gps_entity"]:
                    self._add_sample(
                        c,
                        {
                            **row,
                            "accuracy_m": row["gps_accuracy"],
                            "timestamp": row["last_updated"],
                        },
                        s["gps_entity"],
                        row["last_updated"],
                        "ha_state_update",
                    )
        elapsed = now - c["started"]
        result = {
            "capturing": True,
            "receiver": c["receiver"],
            "elapsed_s": elapsed,
            "sample_count": sum(len(rows) for rows in c["samples"].values()),
            "status": "Stand still; averaging GPS observations",
            "gps_request": gps_request,
        }
        if elapsed < 30:
            return result
        try:
            if (
                len(c["proof"]) < 2
                or c["proof"][-1]["observed"] - c["proof"][0]["observed"] < 20
            ):
                raise ValueError("Need fresh BLE observations throughout capture")
            fixes, failures = [], []
            for rows in c["samples"].values():
                try:
                    fixes.append(average_gps(rows, now))
                except ValueError as exc:
                    failures.append(str(exc))
            if any("moved or jumped" in e for e in failures):
                raise ValueError(next(e for e in failures if "moved or jumped" in e))
            if not fixes:
                raise ValueError(failures[0] if failures else "No fresh GPS fixes yet")
            if len(fixes) > 1 and math.hypot(*_local(fixes[0], fixes[1])) > max(
                5, fixes[0]["accuracy_m"] + fixes[1]["accuracy_m"]
            ):
                raise ValueError("Browser and selected HA GPS disagree")
            gps = min(fixes, key=lambda f: (f["accuracy_m"], -f["observed"]))
            transform = _fit(floor)
            x, y = _local(gps, transform["origin"])
            position = {
                "x": transform["a"] * x - transform["b"] * y + transform["tx"],
                "y": transform["b"] * x + transform["a"] * y + transform["ty"],
            }
            if not all(_number(v, 0, 1e6) for v in position.values()):
                raise ValueError("GPS candidate is outside the supported map")
            anchor_error = transform["anchor_accuracy_m"] * (
                1 + math.dist((x, y), transform["center"]) / transform["baseline_m"]
            )
            candidate = {
                "id": secrets.token_urlsafe(18),
                "owner": owner,
                "floor": s["floor"],
                "phone": s["phone"],
                "receiver": c["receiver"],
                "scanner_identity": c["identity"],
                "context": copy.deepcopy(s["context"]),
                "position": position,
                "gps": gps,
                "original_position": copy.deepcopy(c["original_position"]),
                "map_accuracy_m": gps["accuracy_m"]
                + anchor_error
                + transform["residual_m"],
                "ble_proof": copy.deepcopy(c["proof"]),
                "capture_started": c["started"],
                "captured_at": now,
                "status": "pending",
            }
            # One retained occurrence per receiver/phone/floor. Completed
            # identities prevent the same stationary session recapturing forever.
            candidates = [
                old
                for old in self.candidates
                if not (
                    old["owner"] == owner
                    and old["floor"] == s["floor"]
                    and old["receiver"] == c["receiver"]
                    and old["phone"] == s["phone"]
                )
            ]
            await self._persist(candidates + [candidate])
            s["completed"].add(tuple(c["identity"]))
            s["capture"] = None
            return {
                "capturing": False,
                "candidate": copy.deepcopy(candidate),
                "status": "GPS candidate ready; verify it before saving the receiver position",
                "gps_request": gps_request,
            }
        except ValueError as exc:
            result["status"] = str(exc)
            if (
                elapsed >= MAX_CAPTURE_S
                or "moved or jumped" in str(exc)
                or "disagree" in str(exc)
            ):
                s["capture"] = None
                result["capturing"] = False
            return result

    async def _request_gps(self, s, now):
        if not s["request_gps"]:
            return {"status": "disabled"}
        service = s["notify_service"]
        if service not in {r["key"] for r in self._notify_services()}:
            return {"status": "unavailable", "gps_response": "unknown"}
        previous = self.gps_requests.get(service)
        if previous is not None and now - previous < 60:
            return {
                "status": "rate_limited",
                "retry_after_s": 60 - (now - previous),
                "gps_response": "unknown",
            }
        # Reserve before awaiting; even a failed delivery must not trigger a
        # retry storm. A service acknowledgment does not prove a new GPS fix.
        if len(self.gps_requests) >= 128:
            self.gps_requests = {
                key: value
                for key, value in self.gps_requests.items()
                if now - value < 60
            }
        self.gps_requests[service] = now
        try:
            await asyncio.wait_for(
                self.hass.services.async_call(
                    "notify",
                    service,
                    {"message": "request_location_update"},
                    blocking=True,
                ),
                timeout=8,
            )
        except Exception:
            return {
                "status": "request_failed",
                "requested_at": now,
                "gps_response": "unknown",
            }
        return {"status": "requested", "requested_at": now, "gps_response": "unknown"}

    async def _verify(self, owner, candidate_id, now):
        candidate = next(
            (
                c
                for c in self.candidates
                if c["id"] == candidate_id and c["owner"] == owner
            ),
            None,
        )
        if candidate is None:
            raise ValueError("Phone survey candidate is unavailable")
        async with BPS_FILE_LOCK:
            layout = get_bps_data_for_edit(self.hass)
            floor = _floor(layout, candidate["floor"])
            if (
                candidate.get("status") != "verified"
                and survey_context(floor) != candidate["context"]
            ):
                raise ValueError(
                    "Saved floor, anchors or receiver placement changed; recapture before verifying"
                )
            if candidate.get("status") != "verified" and _placement(
                floor, candidate["receiver"]
            ) not in (candidate["original_position"], candidate["position"]):
                raise ValueError(
                    "Receiver placement changed; recapture before verifying"
                )
            sources = self.sources(self.hass)
            matches = [
                (rec, binding)
                for rec, binding in zip(
                    floor.get("receivers", []), sources.floor_bindings(floor)
                )
                if rec.get("entity_id") == candidate["receiver"]
                and binding.get("status") == "matched"
                and list(binding.get("identity") or []) == candidate["scanner_identity"]
            ]
            if len(matches) != 1:
                raise ValueError(
                    "Receiver hardware identity changed; recapture before verifying"
                )
            rec = matches[0][0]
            # The browser may adopt the revision only if its prior saved layout
            # differs from this one solely at these receiver coordinates. This
            # also safely reconciles lost responses and interrupted audit writes.
            saved_position = copy.deepcopy(rec.get("cords", {}))
            rec["cords"] = {**saved_position, **candidate["original_position"]}
            previous_revision = layout_revision(layout)
            rec["cords"] = saved_position
            if candidate.get("status") == "verified":
                # A retry acknowledges the committed occurrence while returning
                # current saved coordinates if a later map edit moved it again.
                return {
                    "saved": True,
                    "candidate": copy.deepcopy(candidate),
                    "position": _placement(floor, candidate["receiver"]),
                    "previous_layout_revision": previous_revision,
                    "layout_revision": layout_revision(layout),
                }
            # The layout can commit before an interrupted audit write. Retry
            # acknowledges that exact position without moving the receiver again.
            if _placement(floor, candidate["receiver"]) != candidate["position"]:
                rec["cords"] = {**rec.get("cords", {}), **candidate["position"]}
                await save_bps_data(self.hass, layout)
                refresh_receivers_from_coords(self.hass, json.dumps(layout))
            committed_revision = layout_revision(layout)
        verified = {**candidate, "status": "verified", "verified_at": now}
        candidates = [
            verified if c["id"] == candidate["id"] else c for c in self.candidates
        ]
        warning = None
        try:
            await self._persist(candidates)
        except Exception:
            # The layout commit already happened. Never report this as an
            # unsaved retryable move merely because audit persistence failed.
            self.candidates = candidates
            warning = "Position saved; phone survey verification record could not be persisted"
        return {
            "saved": True,
            "candidate": copy.deepcopy(verified),
            "position": copy.deepcopy(verified["position"]),
            "warning": warning,
            "previous_layout_revision": previous_revision,
            "layout_revision": committed_revision,
        }


class BPSPhoneSurveyAPI(HomeAssistantView):
    url = "/api/bps/phone_survey"
    name = "api:bps:phone_survey"
    requires_auth = True

    def __init__(self, snapshot, inventory):
        self.snapshot, self.inventory = snapshot, inventory

    def _manager(self, request):
        hass = request.app["hass"]
        bucket = hass.data.setdefault("bps", {})
        if "_phone_survey" not in bucket:
            bucket["_phone_survey"] = PhoneSurvey(hass, self.snapshot, self.inventory)
        return bucket["_phone_survey"]

    @staticmethod
    def _owner(request):
        user = request.get("hass_user")
        owner = getattr(user, "id", None)
        if not isinstance(owner, str) or not owner or len(owner) > 128:
            raise ValueError("Authenticated Home Assistant user is required")
        return owner

    async def get(self, request):
        try:
            return web.json_response(
                await self._manager(request).get(self._owner(request))
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def post(self, request):
        try:
            if request.content_length is None or request.content_length > 100000:
                raise ValueError("Phone survey request is too large")
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Phone survey request must be an object")
            return web.json_response(
                await self._manager(request).action(self._owner(request), body)
            )
        except (ValueError, TypeError, OverflowError, OSError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
