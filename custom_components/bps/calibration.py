"""Receiver auto-calibration from probe-to-probe BLE measurements.

Each ESPHome probe advertises an iBeacon from the same Bluetooth MAC its
scanner uses, so every sibling probe ranges it and Bermuda accumulates the
measurements on the scanner's own device — for all devices, tracked or not.
The bermuda.dump_devices service exposes those adverts with raw distances.

Combined with the receivers' known positions on the floor plan (and the
floor's pixels-per-meter scale) this yields a measured-vs-true matrix over
receiver pairs. A robust least-squares fit decomposes the log-space error
into a receive-side bias per receiver (rx) and a transmit-side bias per
beacon (tx):

    log10(measured_ij / true_ij) ~= rx_j + tx_i

The per-receiver correction factor 10^(-rx_j) multiplies every distance that
receiver reports (see update_receiver_radii). Because Bermuda's distance
model is exponential in RSSI, a multiplicative distance factor is exactly
equivalent to an additive per-scanner RSSI offset, so nothing is lost by
correcting in distance space. Corrections are normalized to a geometric mean
of 1: they encode only how receivers differ from each other. A bias shared by
the whole fleet is dominated by the beacons' TX power, which says nothing
about the phones and watches actually being tracked — applying it would
uniformly rescale every tracker distance and shift all positions (learned the
hard way: zones all went "unknown" the moment the first corrections landed).
"""

import asyncio
import json
import logging
import copy
import math
import re
import time
from collections import deque
from .receiver_sources import ReceiverSources
from .bermuda_diagnostics import MAC
from datetime import datetime, timezone
from pathlib import Path

import aiofiles
import numpy as np
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.util import slugify
from scipy.optimize import least_squares

from .calibration_auto import assess_update, WINDOW_S as AUTO_HISTORY_WINDOW
from .bermuda_calibration import build_preview, runtime_context, source_snapshot, source_for_floor
from .bermuda_direct import BermudaDirect
from .scanner_addresses import retained_mac, ble_mac

_LOGGER = logging.getLogger(__name__)

# Layout + calibration state now live in HA's Store (see storage.py). The
# shared write lock lives there too so both modules serialize on one lock
# without an import cycle.
from .storage import (
    BPS_FILE_LOCK,
    get_bps_data_for_edit,
    save_bps_data,
    load_calib_state,
    save_calib_state,
)

DOMAIN = "bps"
SAMPLE_INTERVAL = 10  # seconds between dump_devices calls (manual run)
DEFAULT_DURATION = 600  # seconds of sampling (manual run)
MIN_DURATION = 60
MAX_DURATION = 3600
MAX_CONSECUTIVE_FAILURES = 6
AUTO_SAMPLE_INTERVAL = 30  # seconds between dumps in continuous mode
AUTO_SOLVE_INTERVAL = 900  # seconds between re-solves in continuous mode
AUTO_MIN_WINDOW = 300  # seconds of data before the first auto solve
DUMP_DEVICES_TIMEOUT_S = 10  # cap on a single bermuda.dump_devices call
SAMPLES_MAXLEN = 720  # rolling window per pair (6 h at the auto interval)
STATE_SAMPLES_PER_PAIR = 200  # samples persisted per pair (enough for a solve)
STATE_MAX_AGE = SAMPLES_MAXLEN * AUTO_SAMPLE_INTERVAL  # drop older windows
STALE_ADVERT_SECS = 30  # ignore adverts older than this within a dump
MIN_SAMPLES_PER_PAIR = 5
MIN_TRUE_DISTANCE_M = 0.3  # closer pairs carry no path-loss information
MIN_PAIRS = 4
CORRECTION_MIN = 0.2
CORRECTION_MAX = 5.0
GAUGE_WEIGHT = 10.0
DIFF_WEIGHT = 2.0  # direction-difference equations are wall-free; trust them
# Asymmetric loss on absolute equations: walls only lengthen measurements, so
# a measurement above prediction is cheap to leave unexplained (positive
# side), while below prediction is physically impossible and expensive.
POS_SCALE = 0.12  # log10 units before wall-side residuals stop growing much
NEG_SLOPE = 3.0
NEG_SCALE = 0.4


def _normalize(value):
    return str(value or "").strip().lower()


# Mirrors __init__._scanner_token (calibration cannot import __init__ — it
# would be circular): the trailing MAC-derived hex group in a scanner slug,
# a stable hardware identity that survives renames.
_SCANNER_TOKEN_RE = re.compile(r"^[0-9a-f]{5,12}$")


def _scanner_token(slug):
    if not slug:
        return None
    last = str(slug).rsplit("_", 1)[-1].lower()
    return last if _SCANNER_TOKEN_RE.match(last) else None


def _address_tail(address):
    """Last 3 bytes of a MAC as 6 hex chars, e.g. 'aa:bb:cc:4e:88:d8' -> '4e88d8'."""
    hexonly = re.sub(r"[^0-9a-f]", "", str(address or "").lower())
    return hexonly[-6:] if len(hexonly) >= 6 else None


def _token_matches_address(token, address):
    """Whether a slug's hardware token plausibly names this Bluetooth MAC.

    ESPHome derives device-name suffixes from the base/WiFi MAC while the
    scanner advertises from its Bluetooth MAC; ESP-IDF derives both from the
    base MAC as +0..+3 in the last octet, wrapping uint8 (0xfe -> 0x00, no
    carry). That offset is strictly one-way — a derived MAC is never BELOW
    the base the token came from — so use a directional modular window.
    Espressif allocates base MACs in blocks of 4, so a symmetric window would
    admit the previous same-batch chip (its BT MAC sits 2 BELOW a neighbor's
    base): different hardware, and the reason this must stay directional.
    """
    tail = _address_tail(address)
    if not token or not tail:
        return False
    tok = str(token).lower()[-6:]
    if len(tok) != 6:
        return False
    try:
        return tok[:4] == tail[:4] and (int(tail[4:6], 16) - int(tok[4:6], 16)) % 256 <= 3
    except ValueError:
        return False


async def save_calibration_state(hass) -> None:
    """Persist the latest solves and the sample window across restarts.

    The applied corrections live in bpsdata.txt and always survive; this file
    only keeps the panel's matrix and the rolling window warm so a reboot does
    not blank the display and restart the window from zero.
    """
    cal = get_calibration_state(hass)
    payload = {
        "saved_at": time.time(),
        "results": cal["results"],
        "applied": cal["applied"],
        "samples": {
            key: list(values)[-STATE_SAMPLES_PER_PAIR:]
            for key, values in cal["samples"].items()
        },
        # Raw pair keys alone cannot identify the hardware or geometry that
        # produced them. Keep that observation baseline across restarts.
        "sample_geometry": copy.deepcopy(cal.get("_auto_geometry", cal.get("receivers", {}))),
    }
    try:
        await save_calib_state(hass, payload)
    except Exception as e:
        _LOGGER.warning("Could not persist calibration state: %s", e)


async def async_restore_calibration_state(hass) -> None:
    """Reload the persisted solves and sample window at startup."""
    cal = get_calibration_state(hass)
    if cal["mode"] != "off":
        return
    payload = await load_calib_state(hass)
    if not isinstance(payload, dict):
        return  # first run, or nothing persisted: start cold

    if isinstance(payload.get("results"), dict):
        cal["results"] = payload["results"]
    if isinstance(payload.get("applied"), dict):
        cal["applied"] = payload["applied"]

    saved_at = payload.get("saved_at")
    fresh = isinstance(saved_at, (int, float)) and time.time() - saved_at <= STATE_MAX_AGE
    if fresh and isinstance(payload.get("samples"), dict):
        for key, values in payload["samples"].items():
            if isinstance(values, list):
                cal["samples"][key] = deque(
                    (float(v) for v in values if isinstance(v, (int, float))),
                    maxlen=SAMPLES_MAXLEN,
                )
        geometry = payload.get("sample_geometry")
        # Legacy windows may remain visible until receivers are loaded, but
        # cannot be rebound to today's scanner hardware by an explicit solve.
        cal["_auto_geometry"] = copy.deepcopy(geometry) if isinstance(geometry, dict) else {}
    _LOGGER.info(
        "Restored calibration state (%d floor results, %d sample pairs%s)",
        len(cal["results"]),
        len(cal["samples"]),
        "" if fresh else ", window too old — dropped",
    )


async def _read_coords(hass):
    """The current layout as a fresh dict to mutate, or None if there is none.

    A deep copy from the store cache (never the live object) so the
    read-modify-write below can't leak a half-mutated layout to the tracking
    loop before the save lands.
    """
    return get_bps_data_for_edit(hass)


def _find_floor(coords, floor_name):
    for floor in coords.get("floor", []):
        if _normalize(floor.get("name")) == _normalize(floor_name):
            return floor
    return None


def get_calibration_state(hass) -> dict:
    hass.data.setdefault(DOMAIN, {})
    return hass.data[DOMAIN].setdefault(
        "calibration",
        {
            "state": "idle",  # idle | sampling | done | error
            "mode": "off",  # off | manual | auto
            "floor": None,  # manual runs are floor-scoped
            "started_at": None,
            "ends_at": None,
            "duration": None,
            "samples": {},  # "tx|rx" -> deque of raw distances (meters)
            "receivers": {},  # slug -> {"x", "y", "scale", "floor"}
            "results": {},  # floor name -> latest solve result
            "applied": {},  # floor name -> corrections last written to disk
            "last_solved_at": None,
            "error": None,
            "task": None,
            "auto_status": {},  # floor -> last apply/skip reason
        },
    )


def _build_receiver_map(coords, floor_name=None, *, hass=None, receiver_sources=None) -> dict:
    """Map receiver slug -> position/scale/floor, for one floor or all floors."""
    sources = receiver_sources if receiver_sources is not None else ReceiverSources(hass) if hass is not None else None
    if sources is not None:
        coords = sources.calibration_layout(coords)
    receivers = {}
    for floor in coords.get("floor", []):
        if floor_name is not None and _normalize(floor.get("name")) != _normalize(floor_name):
            continue
        scale = floor.get("scale")
        if not scale:
            continue
        for receiver in floor.get("receivers", []):
            cords = receiver.get("cords") or {}
            if receiver.get("entity_id") and cords.get("x") is not None and cords.get("y") is not None:
                uid = receiver.get("scanner_uid")
                height = receiver.get("height")
                receivers[str(receiver["entity_id"])] = {
                    "x": float(cords["x"]),
                    "y": float(cords["y"]),
                    "scale": float(scale),
                    "floor": floor.get("name"),
                    # Hardware identity stored by a panel re-link (issue #64);
                    # lets calibration match the scanner after a rename.
                    "uid": uid if isinstance(uid, str) and uid else None,
                    "scanner_mac": receiver.get("scanner_mac"),
                    "scanner_config_entry_id": receiver.get("scanner_config_entry_id"),
                    "scanner_owner_unique": receiver.get("scanner_owner_unique", False),
                    "strict_identity": sources is not None and sources.registry is not None,
                    # Mount height (m) set in the panel; makes the pair's
                    # ground-truth distance 3D (see _true_distance_m). The
                    # range guard also drops NaN/Infinity from a hand-edited
                    # file (NaN fails both comparisons), which would poison
                    # true_m and error out every solve on the floor.
                    "height": float(height)
                    if isinstance(height, (int, float)) and 0 <= height <= 10 else None,
                }
    return receivers


def _set_auto_receivers(hass, cal, coords):
    """Separate floor geometry from the shared physical observation keys."""
    sources = ReceiverSources(hass)
    floors = {floor.get("name"): _build_receiver_map({"floor": [floor]}, receiver_sources=sources)
              for floor in coords.get("floor", [])}
    claims = {}
    for receivers in floors.values():
        for slug, row in receivers.items():
            identity = ("physical", row["scanner_config_entry_id"], row["scanner_mac"]) if row.get("scanner_mac") else ("exact", slug)
            claims.setdefault(slug, set()).add(identity)
    # A reused label with contradictory hardware proof cannot route samples.
    ambiguous = {slug for slug, identities in claims.items() if len(identities) != 1}
    union, canonical, routes = {}, {}, {}
    for name, receivers in floors.items():
        for slug in ambiguous:
            receivers.pop(slug, None)
        routes[name] = {}
        for slug, row in receivers.items():
            identity = next(iter(claims[slug]))
            sample_slug = canonical.setdefault(identity, slug)
            union.setdefault(sample_slug, row)
            routes[name][slug] = sample_slug
    cal["receivers"] = union
    if cal.get("_auto_floor_receivers") != floors or cal.get("_auto_routes") != routes:
        cal.pop("_auto_sources", None)
    cal["_auto_floor_receivers"] = floors
    cal["_auto_routes"] = routes
    cal["_auto_ambiguous_slugs"] = ambiguous


def _floor_auto_snapshot(cal, floor_name, *, raw_samples=False):
    floor_name = next((name for name in cal["_auto_floor_receivers"]
                       if _normalize(name) == _normalize(floor_name)), floor_name)
    receivers = copy.deepcopy(cal["_auto_floor_receivers"].get(floor_name, {}))
    routes = cal["_auto_routes"].get(floor_name, {})
    inverse = {canonical: slug for slug, canonical in routes.items()}
    snapshot = {"receivers": receivers, "matched_placed": {
        inverse[s]: stamp for s, stamp in (cal.get("matched_placed") or {}).items() if s in inverse}}
    history = {}
    for pair, rows in cal.get("samples" if raw_samples else "_auto_samples", {}).items():
        tx, rx = pair.split("|", 1)
        if tx in inverse and rx in inverse:
            history[f"{inverse[tx]}|{inverse[rx]}"] = list(rows)
    return snapshot, history


def _equivalent_updates(hass, floor, updates, *, identities=None):
    """Expand from a pre-mutation snapshot, never repair conflicting aliases."""
    sources = ReceiverSources(hass)
    bindings = sources.floor_bindings(floor)
    proven = {}
    direct = set()
    placed = {str(rec.get("entity_id")) for rec in floor.get("receivers", [])}
    def captured(slug):
        row = (identities or {}).get(slug, {})
        mac, owner = row.get("scanner_mac"), row.get("scanner_config_entry_id")
        return (owner, mac.lower()) if isinstance(owner, str) and owner and isinstance(mac, str) and MAC.fullmatch(mac) else None
    # A current alias cannot prove which hardware produced an old solve. Only
    # the solve's captured owner/MAC may migrate a missing key to another slug.
    for slug, correction in updates.items():
        if slug in placed:
            continue
        identity = captured(slug)
        if identity in sources.by_identity:
            proven.setdefault(identity, set()).add(correction)
    for rec, binding in zip(floor.get("receivers", []), bindings):
        slug = str(rec.get("entity_id"))
        if slug not in updates or binding["status"] not in ("matched", "unmatched", "duplicate_scanner_alias"):
            continue
        identity = captured(slug)
        if identity is not None and identity != binding["identity"]:
            continue
        direct.add(slug)
        if binding["identity"]:
            proven.setdefault(binding["identity"], set()).add(updates[slug])
    expanded = {}
    for rec, binding in zip(floor.get("receivers", []), bindings):
        if binding["status"] not in ("matched", "unmatched", "duplicate_scanner_alias"):
            continue
        slug = str(rec.get("entity_id"))
        identity = captured(slug)
        if identity is not None and identity != binding["identity"]:
            continue
        values = proven.get(binding["identity"], set())
        if len(values) == 1:
            expanded[slug] = next(iter(values))
        elif not values and slug in direct:
            expanded[slug] = updates[slug]
    return expanded


def _match_scanners(cal: dict, devices: dict) -> dict:
    """Map scanner MAC -> placed receiver slug (issue #63).

    Tier 1: slugify(current device name) == placed slug — names still in sync.
    Tier 2: hardware identity for placements no name matched: the stored
    scanner_uid (panel re-link, issue #64) or the MAC-derived hex tail in the
    placed slug, matched against a remaining device's name tail or Bluetooth
    MAC. Entity ids freeze at creation while device names follow renames, so
    tier 1 alone silently dropped renamed/moved probes from calibration —
    always the same ones, with tracking still fine. Only a UNIQUE candidate
    wins, so a rename can never quietly pair two different scanners.

    Placed slugs that matched anything are timestamped in cal["matched_placed"]
    so the report can tell "no matching scanner" apart from "matched but no
    adverts sampled".
    """
    scanners = []
    for dev in devices.values():
        if not isinstance(dev, dict) or dev.get("_is_scanner") is not True:
            continue
        address = ble_mac(dev)
        if address is None or retained_mac(dev) is None:
            continue
        scanners.append((address, slugify(str(dev.get("name") or ""))))

    scanner_slug_by_mac = {}
    claims = {}
    for slug, receiver in cal["receivers"].items():
        address = receiver.get("scanner_mac")
        if address:
            owner = receiver.get("scanner_config_entry_id")
            candidates = {ble_mac(dev) for dev in devices.values() if isinstance(dev, dict)
                and dev.get("_is_scanner") is True and retained_mac(dev) == address and ble_mac(dev)
                and (dev.get("_bps_config_entry_id") == owner or receiver.get("scanner_owner_unique"))}
            if len(candidates) == 1:
                claims.setdefault(next(iter(candidates)), []).append(slug)
        else:
            exact = {address for address, name_slug in scanners if name_slug == slug}
            # Exact current names remain compatible with older Bermuda entity
            # formats. This cannot expand aliases or override a stored MAC.
            if len(exact) == 1:
                claims.setdefault(next(iter(exact)), []).append(slug)
    for address, slugs in claims.items():
        if len(set(slugs)) == 1:
            scanner_slug_by_mac[address] = slugs[0]

    matched_slugs = set(scanner_slug_by_mac.values())
    # Tier-2 candidates: devices tier 1 didn't claim, excluding any whose
    # current name belongs to a placement on ANY floor — a manual run maps
    # only its own floor, so without the all-floors guard a name-synced
    # scanner placed elsewhere would look free here and could be captured
    # while a placement's true scanner happens to be offline.
    all_placed = cal.get("all_placed_slugs") or set(cal["receivers"])
    free_devs = [
        (a, n) for a, n in scanners
        if a not in scanner_slug_by_mac and n not in all_placed
    ]
    # Iterate to a fixpoint: consuming a device can turn a previously
    # ambiguous placement unique, and bpsdata receiver order must not decide
    # who gets matched.
    pending = [s for s in cal["receivers"] if s not in matched_slugs]
    progress = True
    while progress and pending:
        progress = False
        for slug in list(pending):
            if cal["receivers"][slug].get("strict_identity") or cal["receivers"][slug].get("scanner_mac"):
                pending.remove(slug)
                continue
            token = cal["receivers"][slug].get("uid") or _scanner_token(slug)
            if not token:
                pending.remove(slug)
                continue
            candidates = {
                a for a, n in free_devs
                if _scanner_token(n) == token or _token_matches_address(token, a)
            }
            if len(candidates) == 1:
                address = candidates.pop()
                scanner_slug_by_mac[address] = slug
                matched_slugs.add(slug)
                free_devs = [(a, n) for a, n in free_devs if a != address]
                pending.remove(slug)
                progress = True

    # slug -> when it last matched. Timestamps (not a grow-only set) so an
    # unbounded auto window doesn't keep reporting "matched" for a placement
    # whose match broke long ago.
    matched = cal.get("matched_placed")
    if not isinstance(matched, dict):
        matched = {}
        cal["matched_placed"] = matched
    now = time.time()
    for slug in matched_slugs:
        matched[slug] = now
    return scanner_slug_by_mac


def _all_placed_slugs(coords) -> set:
    """Every placed receiver slug across ALL floors — tier-2 matching must not
    treat another floor's (or any placed) scanner as a free candidate."""
    slugs = set()
    for floor in coords.get("floor", []):
        for receiver in floor.get("receivers", []):
            eid = receiver.get("entity_id")
            if isinstance(eid, str) and eid:
                slugs.add(eid)
    return slugs


def _ingest_dump(cal: dict, devices: dict) -> None:
    """Extract fresh probe-to-probe raw distances from a dump_devices payload."""
    if not isinstance(devices, dict):
        return
    _refresh_auto_history(cal)

    # Scanner MAC -> receiver slug, restricted to receivers on this floor.
    scanner_slug_by_mac = _match_scanners(cal, devices)
    # Provenance comes from the actual sampled adverts, never today's config
    # applied retrospectively to an old solve. Do not mix offset/model windows.
    source = source_snapshot(cal["receivers"], devices, scanner_slug_by_mac)
    known = all(row["scanner_mac"] and row["attenuation"] is not None
                and row["rssi_offset_db"] is not None and row["reference_power_dbm"] is not None
                and len(row["transmitter_reference_power_dbm"]) == 1
                for row in source["receivers"].values())
    previous = cal.get("_bermuda_source")
    if known and source["receivers"]:
        if previous != source and cal.get("samples"):
            cal["samples"] = {}
            cal["_auto_samples"] = {}
            cal["_advert_stamps"] = {}
        cal["_bermuda_source"] = source
        if cal.get("_auto_floor_receivers"):
            cal["_auto_sources"] = {name: source_snapshot(receivers, devices,
                _match_scanners({"receivers": receivers}, devices))
                for name, receivers in cal["_auto_floor_receivers"].items()}
    else:
        # Incomplete evidence cannot certify new samples against the old model.
        cal.pop("_bermuda_source", None)
        cal.pop("_auto_sources", None)

    # Monotonic "now": the freshest advert stamp in the payload.
    newest = 0.0
    for dev in devices.values():
        adverts = dev.get("adverts") if isinstance(dev, dict) else None
        if not isinstance(adverts, dict):
            continue
        for advert in adverts.values():
            stamp = advert.get("stamp") if isinstance(advert, dict) else None
            if isinstance(stamp, (int, float)) and math.isfinite(stamp) and stamp > newest:
                newest = stamp

    # A Bermuda restart can reset its monotonic advert stamps. Discard the
    # independent history before accepting observations from the new clock.
    previous_clock = cal.get("_advert_clock", 0.)
    if newest and newest < previous_clock:
        cal.pop("_advert_stamps", None)
        cal.pop("_auto_samples", None)
    if newest:
        cal["_advert_clock"] = newest
    stamps = cal.setdefault("_advert_stamps", {})
    history = cal.setdefault("_auto_samples", {})
    now = time.time()
    observations = {}
    # The beacon advertises from the scanner's own MAC, so the transmitter's
    # measurements live on the scanner devices themselves.
    for dev in devices.values():
        if not isinstance(dev, dict) or dev.get("_is_scanner") is not True:
            continue
        tx_slug = scanner_slug_by_mac.get(str(dev.get("address") or "").lower())
        if tx_slug is None:
            continue
        adverts = dev.get("adverts")
        if not isinstance(adverts, dict):
            continue
        for advert in adverts.values():
            if not isinstance(advert, dict):
                continue
            rx_slug = scanner_slug_by_mac.get(str(advert.get("scanner_address") or "").lower())
            if rx_slug is None or rx_slug == tx_slug:
                continue
            stamp = advert.get("stamp")
            if (not isinstance(stamp, (int, float)) or not math.isfinite(stamp)
                    or newest - stamp > STALE_ADVERT_SECS):
                continue
            if "_bps_direct_usable" in advert and (not advert["_bps_direct_usable"]
                    or advert.get("_bps_direct_age") is None or advert["_bps_direct_age"] > STALE_ADVERT_SECS):
                continue
            distance = advert.get("rssi_distance_raw")
            if not isinstance(distance, (int, float)) or not math.isfinite(distance) or distance <= 0:
                continue
            key = f"{tx_slug}|{rx_slug}"
            if stamp > observations.get(key, (-math.inf, 0))[0]:
                observations[key] = (stamp, float(distance))
    for key, (stamp, distance) in observations.items():
        if stamp <= stamps.get(key, -math.inf):
            continue  # polling the same cached advert is not new evidence
        stamps[key] = stamp
        cal["samples"].setdefault(key, deque(maxlen=SAMPLES_MAXLEN)).append(distance)
        if cal.get("mode") == "auto":
            rows = history.setdefault(key, deque(maxlen=SAMPLES_MAXLEN))
            rows.append((now, distance))
            while rows and rows[0][0] < now - AUTO_HISTORY_WINDOW:
                rows.popleft()


def _refresh_auto_history(cal):
    """Receiver geometry/identity changes invalidate old reference samples."""
    receivers = cal.get("receivers", {})
    floors = cal.get("_auto_floor_receivers", {})
    geometry = {"floors": floors, "routes": cal.get("_auto_routes")} if len(floors) > 1 else receivers
    previous = cal.get("_auto_geometry")
    if previous == geometry:
        return
    if previous is not None:
        unchanged = {s for s in receivers if receivers[s] == previous.get(s)} if geometry is receivers else set()
        cal["samples"] = {k: v for k, v in cal["samples"].items()
                          if all(s in unchanged for s in k.split("|", 1))}
        cal["matched_placed"] = {s: stamp for s, stamp in (cal.get("matched_placed") or {}).items()
                                 if s in unchanged}
        # A solve before the next successful dump must not certify retained
        # observations with provenance for the old receiver map.
        cal.pop("_bermuda_source", None)
        if cal.get("mode") == "manual":
            cal["_manual_solve_generation"] = object()
    cal["_auto_geometry"] = copy.deepcopy(geometry)
    cal["_auto_samples"] = {}
    cal["_advert_stamps"] = {}
    cal.pop("_auto_sources", None)


def _true_distance_m(cal: dict, slug_a: str, slug_b: str):
    a = cal["receivers"].get(slug_a)
    b = cal["receivers"].get(slug_b)
    if not a or not b or _normalize(a["floor"]) != _normalize(b["floor"]) or not a["scale"]:
        return None
    horizontal = math.hypot(a["x"] - b["x"], a["y"] - b["y"]) / a["scale"]
    # Known mount heights make the truth 3D: the probes' adverts travel the
    # slant path, so judging them against the flat map distance reads pure
    # geometry as RSSI bias — a 0.3 m vs 2.2 m pair 3 m apart on the map is
    # really 3.55 m apart, an 18% phantom error the fit would otherwise bake
    # into that receiver's correction. Applied only when BOTH heights are set;
    # a lone height says nothing about the pair's geometry.
    if a.get("height") is not None and b.get("height") is not None:
        return math.hypot(horizontal, a["height"] - b["height"])
    return horizontal


def solve_snapshot(cal: dict) -> dict:
    """A private copy of everything solve() reads, taken ON the event loop.

    solve() is handed to an executor (it is the single longest blocking call in
    the integration - tens of milliseconds on a desktop, hundreds on a Pi-class
    box, per floor). But cal["samples"] is a dict of deques the ingest path
    appends to on the loop, so iterating it off-thread races that ingest: a new
    pair key raises "dictionary changed size during iteration", and a deque
    growing under np.median silently returns a median of a moving set. Copying
    the two structures it reads is cheap next to the solve and removes the race
    entirely - the same discipline run_selftest already uses.
    """
    return {
        "samples": {k: list(v) for k, v in (cal.get("samples") or {}).items()},
        "receivers": copy.deepcopy(cal.get("receivers") or {}),
        "_bermuda_source": copy.deepcopy(cal.get("_bermuda_source")),
    }


async def async_solve(hass, cal: dict, floor_name: str):
    """solve() off the event loop, against a snapshot taken on it."""
    if cal.get("_auto_floor_receivers"):
        snap, samples = _floor_auto_snapshot(cal, floor_name, raw_samples=True)
        snap["samples"] = samples
        name = next((name for name in cal["_auto_floor_receivers"]
                     if _normalize(name) == _normalize(floor_name)), floor_name)
        snap["_bermuda_source"] = copy.deepcopy(cal.get("_auto_sources", {}).get(name))
    else:
        snap = solve_snapshot(cal)
    return await hass.async_add_executor_job(solve, snap, floor_name)


def solve(cal: dict, floor_name: str):
    """Fit per-receiver corrections for one floor from the collected samples.

    Returns a result dict, or raises ValueError when there is not enough data.
    """
    pairs = []  # (tx_slug, rx_slug, true_m, measured_m, n_samples)
    for key, values in cal["samples"].items():
        if len(values) < MIN_SAMPLES_PER_PAIR:
            continue
        tx_slug, rx_slug = key.split("|", 1)
        tx_info = cal["receivers"].get(tx_slug)
        if not tx_info or _normalize(tx_info["floor"]) != _normalize(floor_name):
            continue
        true_m = _true_distance_m(cal, tx_slug, rx_slug)
        if true_m is None or true_m < MIN_TRUE_DISTANCE_M:
            continue
        measured = float(np.median(values))
        if measured <= 0:
            continue
        pairs.append((tx_slug, rx_slug, true_m, measured, len(values)))

    # Placed receivers absent from the matrix, split by cause so the report
    # can say WHY (issue #63): never matched to any Bermuda scanner (identity
    # drift — a rename left the device name out of sync with the frozen
    # entity-id slug) vs matched but no usable adverts (not beaconing, or too
    # few/too-close samples). Computed BEFORE the too-few-pairs gate so the
    # strongest mismatch case (so few matches the solve can't even run) still
    # names its culprits. "Matched" counts only recent matches, so a match
    # that broke mid-window ages out instead of masking the drift.
    participating = {p[0] for p in pairs} | {p[1] for p in pairs}
    matched = cal.get("matched_placed")
    if not isinstance(matched, dict):
        matched = {}
    cutoff = time.time() - STATE_MAX_AGE
    matched_fresh = {
        s for s, ts in matched.items() if isinstance(ts, (int, float)) and ts >= cutoff
    }
    placed_on_floor = sorted(
        s for s, r in cal["receivers"].items()
        if _normalize(r["floor"]) == _normalize(floor_name)
    )
    missing_unmatched = [s for s in placed_on_floor if s not in participating and s not in matched_fresh]
    missing_no_data = [s for s in placed_on_floor if s not in participating and s in matched_fresh]

    if len(pairs) < MIN_PAIRS:
        hint = ""
        if missing_unmatched:
            hint = (
                " Placed receivers with no matching Bermuda scanner (renamed device?): "
                + ", ".join(missing_unmatched) + "."
            )
        raise ValueError(
            f"Only {len(pairs)} usable receiver pairs (need at least {MIN_PAIRS}). "
            "Sample longer, or check that the probes are advertising their iBeacon."
            + hint
        )

    participants = sorted({p[0] for p in pairs} | {p[1] for p in pairs})
    index = {slug: i for i, slug in enumerate(participants)}
    n = len(participants)

    y = np.array([math.log10(p[3] / p[2]) for p in pairs])
    # Distant pairs cross more walls; the exponential model amplifies their
    # error, so weight them down.
    weights = np.array([1.0 / (1.0 + p[2]) for p in pairs])
    tx_idx = np.array([index[p[0]] for p in pairs])
    rx_idx = np.array([index[p[1]] for p in pairs])

    # Wall attenuation is a property of the PATH, so it cancels exactly in
    # the difference between the two directions of a pair:
    #   y_ab - y_ba = (rx_b + tx_a) - (rx_a + tx_b)
    # These wall-free equations pin the receivers' relative biases.
    by_key = {(p[0], p[1]): k for k, p in enumerate(pairs)}
    diff_rows = []
    for (a, b), k_ab in by_key.items():
        k_ba = by_key.get((b, a))
        if k_ba is not None and a < b:
            diff_rows.append((k_ab, k_ba))
    diff_ab = np.array([d[0] for d in diff_rows], dtype=int)
    diff_ba = np.array([d[1] for d in diff_rows], dtype=int)

    def residuals(x):
        rx = x[:n]
        tx = x[n:]
        # Absolute equations, asymmetric: e > 0 means the pair measures long,
        # which a wall explains — compress it so walls stay cheap. e < 0 is
        # physically impossible (walls cannot shorten a path), so it stays
        # expensive; the node biases end up tracking each node's cleanest
        # (line-of-sight) paths instead of averaging its walls in.
        e = y - (rx[rx_idx] + tx[tx_idx])
        e_pos = np.clip(e, 0.0, None)
        e_neg = np.clip(-e, 0.0, None)
        res = (
            POS_SCALE * np.log1p(e_pos / POS_SCALE)
            - NEG_SLOPE * NEG_SCALE * np.log1p(e_neg / NEG_SCALE)
        ) * weights
        if len(diff_rows):
            pred_diff = (rx[rx_idx[diff_ab]] + tx[tx_idx[diff_ab]]) - (rx[rx_idx[diff_ba]] + tx[tx_idx[diff_ba]])
            meas_diff = y[diff_ab] - y[diff_ba]
            res = np.append(res, DIFF_WEIGHT * (pred_diff - meas_diff))
        return np.append(res, GAUGE_WEIGHT * np.mean(tx))

    fit = least_squares(
        residuals,
        np.zeros(2 * n),
        bounds=(-1.0, 1.0),  # each side capped at a 10x factor
    )
    rx = fit.x[:n]
    tx = fit.x[n:]

    # Corrections must encode RELATIVE receiver differences only. Any bias
    # shared by the whole fleet — typically the beacons' TX power differing
    # from the ref_power Bermuda's tracker calibration assumes — would rescale
    # every tracker distance at once and shift all trilaterated positions
    # (points drift out of their zones). Normalize to a geometric mean of 1
    # so the absolute scale stays with Bermuda's own calibration.
    # (Cast to plain floats: numpy scalars are not JSON serializable.)
    raw_factors = {slug: float(10 ** (-rx[i])) for slug, i in index.items()}
    log_mean = float(np.mean([math.log10(f) for f in raw_factors.values()]))
    corrections = {
        slug: round(min(CORRECTION_MAX, max(CORRECTION_MIN, f / (10 ** log_mean))), 4)
        for slug, f in raw_factors.items()
    }

    # A node with no line-of-sight path absorbs its walls into the fitted
    # bias, and that is locally invisible — the residuals look clean. The
    # honest observable warning sign is an aggressive correction (genuine
    # hardware gain differences are small) or hardly any pairs to fit from.
    pair_count = {}
    for p in pairs:
        pair_count[p[0]] = pair_count.get(p[0], 0) + 1
        pair_count[p[1]] = pair_count.get(p[1], 0) + 1
    low_confidence = sorted(
        slug
        for slug in corrections
        if not (0.5 <= corrections[slug] <= 2.0) or pair_count.get(slug, 0) < 3
    )

    # Median, not RMS: the asymmetric loss deliberately leaves through-wall
    # pairs unexplained, and their large residuals would dominate an RMS and
    # can even make "after" read worse than "before" while the fit is fine.
    def typical(values):
        return float(np.median(np.abs(values))) if len(values) else 0.0

    before = typical(y)
    after = typical(y - (rx[rx_idx] + tx[tx_idx]))

    matrix = [
        {
            "tx": p[0],
            "rx": p[1],
            "true_m": round(p[2], 2),
            "measured_m": round(p[3], 2),
            "corrected_m": round(p[3] * corrections[p[1]], 2),
            "error_pct": round((p[3] / p[2] - 1.0) * 100.0, 1),
            "samples": p[4],
        }
        for p in pairs
    ]

    return {
        "floor": floor_name,
        "bermuda_source": source_for_floor(cal.get("_bermuda_source"), floor_name),
        "solved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pairs_used": len(pairs),
        "bidirectional_pairs": len(diff_rows),
        "low_confidence": low_confidence,
        "receivers": corrections,
        "receiver_identities": {slug: {key: cal["receivers"][slug].get(key)
            for key in ("scanner_mac", "scanner_config_entry_id")} for slug in corrections},
        "rx_bias_db_equident": {
            # The equivalent Bermuda "Calibration 2" rssi_offset (attenuation 3).
            slug: round(-30.0 * math.log10(corrections[slug]), 1)
            for slug in corrections
        },
        # Typical multiplicative error before/after, e.g. 1.35 = 35% off.
        "error_factor_before": round(10 ** before, 3),
        "error_factor_after": round(10 ** after, 3),
        "matrix": matrix,
        "missing_unmatched": missing_unmatched,
        "missing_no_data": missing_no_data,
    }


async def _dump_devices(hass) -> dict:
    # Bounded so a hung/slow Bermuda service can't stall the sampling loop
    # indefinitely; the caller counts a timeout as a normal sampling failure.
    direct = BermudaDirect(hass, include_scanner_devices=True)
    if not direct.needs_legacy:
        return direct.dump_devices()
    try:
        response = await asyncio.wait_for(
            hass.services.async_call(
                "bermuda",
                "dump_devices",
                {"configured_devices": True},
                blocking=True,
                return_response=True,
            ),
            timeout=DUMP_DEVICES_TIMEOUT_S,
        )
    except Exception as err:
        if not direct.snapshots:
            raise
        _LOGGER.debug("Legacy Bermuda calibration dump unavailable (%s); retaining direct sources", type(err).__name__)
        response = {}
    if not direct.snapshots:
        return response or {}
    runtime = direct.legacy_scanners(hass)
    addresses = {row["address"] for row in runtime.values()}
    response = response if isinstance(response, dict) else {}
    service = {key: value for key, value in response.items() if isinstance(value, dict)
               and str(value.get("address") or "").lower() not in addresses}
    return direct.dump_devices({**service, **runtime})


async def _sample_loop(hass, cal: dict) -> None:
    """One-shot, floor-scoped sampling window (manual run)."""
    failures = 0
    try:
        while time.time() < cal["ends_at"]:
            try:
                _ingest_dump(cal, await _dump_devices(hass))
                failures = 0
            except Exception as e:  # service missing, timeout, bad payload
                failures += 1
                _LOGGER.warning("Calibration dump_devices call failed (%d): %s", failures, e)
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    cal["state"] = "error"
                    cal["mode"] = "off"
                    cal["error"] = (
                        "bermuda.dump_devices kept failing — is the Bermuda "
                        f"integration installed and current? Last error: {e}"
                    )
                    return
            await asyncio.sleep(SAMPLE_INTERVAL)

        # One final dump before solving so placements edited late in the
        # window (re-link + save inside the last sample gap) are matched and
        # classified against reality — mirrors the auto loop's pre-solve
        # ingest. A failed dump just falls back to what the window gathered.
        try:
            _ingest_dump(cal, await _dump_devices(hass))
        except Exception as e:
            _LOGGER.debug("Final calibration dump failed: %s", e)

        result = await async_solve(hass, cal, cal["floor"])
        cal["results"][result["floor"]] = result
        cal["last_solved_at"] = result["solved_at"]
        cal["state"] = "done"
        cal["mode"] = "off"
        await save_calibration_state(hass)
    except ValueError as e:
        cal["state"] = "error"
        cal["mode"] = "off"
        cal["error"] = str(e)
    except asyncio.CancelledError:
        cal["state"] = "idle"
        cal["mode"] = "off"
        raise
    except Exception as e:
        _LOGGER.exception("Calibration failed")
        cal["state"] = "error"
        cal["mode"] = "off"
        cal["error"] = str(e)


async def _auto_loop(hass, cal: dict) -> None:
    """Continuous mode: sample forever, re-solve and re-apply periodically."""
    started = time.time()
    last_solve = 0.0
    try:
        while True:
            try:
                _ingest_dump(cal, await _dump_devices(hass))
                if cal["error"] and cal["error"].startswith("bermuda.dump_devices"):
                    cal["error"] = None
            except Exception as e:
                _LOGGER.warning("Auto-calibration dump_devices call failed: %s", e)
                cal["error"] = f"bermuda.dump_devices failing: {e}"

            now = time.time()
            if now - started >= AUTO_MIN_WINDOW and now - last_solve >= AUTO_SOLVE_INTERVAL:
                last_solve = now
                try:
                    await _auto_solve_and_apply(hass, cal)
                    await save_calibration_state(hass)
                except Exception as e:
                    _LOGGER.exception("Auto-calibration solve failed")
                    cal["error"] = str(e)
            await asyncio.sleep(AUTO_SAMPLE_INTERVAL)
    except asyncio.CancelledError:
        raise


async def _auto_solve_and_apply(hass, cal: dict) -> None:
    """Re-solve every floor and persist corrections that changed enough."""
    # Refresh the receiver map and re-match BEFORE taking the file lock: every
    # ingest so far this cycle ran against the OLD map, so a receiver placed
    # or re-linked since would be misreported as "no matching scanner" (a
    # rename hint) for a whole solve interval despite matching perfectly. And
    # dump_devices is an external RPC with no timeout — awaiting it under
    # BPS_FILE_LOCK would let a wedged Bermuda hang every bpsdata writer
    # (panel saves included), where pre-lock it only delays this solve.
    coords = await _read_coords(hass)
    if not coords:
        return
    _set_auto_receivers(hass, cal, coords)
    cal["all_placed_slugs"] = _all_placed_slugs(coords)
    try:
        _ingest_dump(cal, await _dump_devices(hass))
    except Exception as e:
        _LOGGER.debug("Pre-solve dump failed; missing-receiver buckets may lag one cycle: %s", e)
    # Export outside the layout lock. Replay is optional: measured recordings
    # add a veto against actual beacon-position regressions, not a prerequisite.
    recording = hass.data.get(DOMAIN, {}).get("_diagnostics")
    tracking_recording = await recording.export() if recording and recording.frames else None
    async with BPS_FILE_LOCK:
        await _auto_solve_and_apply_locked(hass, cal, tracking_recording)


async def _auto_solve_and_apply_locked(hass, cal: dict, tracking_recording=None) -> None:
    # Re-read inside the lock so a concurrent writer is not clobbered; the
    # wrapper's pre-lock read was only to refresh the matching state.
    coords = await _read_coords(hass)
    if not coords:
        return
    # Floors and receivers may have been edited since the last cycle.
    _set_auto_receivers(hass, cal, coords)
    cal["all_placed_slugs"] = _all_placed_slugs(coords)
    _refresh_auto_history(cal)

    changed = False
    applied = {}
    accepted = {}
    pending_identities = {}
    statuses = cal.setdefault("auto_status", {})
    current_floors = {f.get("name") for f in coords.get("floor", [])}
    for name in list(statuses):
        if name not in current_floors:
            del statuses[name]
    for floor in coords.get("floor", []):
        floor_name = floor.get("name")
        snapshot, history = _floor_auto_snapshot(cal, floor_name)
        on_floor = set(snapshot["receivers"])
        if len(on_floor) < 4:
            ambiguous = sorted(set(str(r.get("entity_id")) for r in floor.get("receivers", [])) & cal["_auto_ambiguous_slugs"])
            reason = "Conflicting hardware identities share these scanner labels: " + ", ".join(ambiguous) + ". Match each placement to its correct scanner." if ambiguous else "Place at least four connected receivers for automatic updates"
            statuses[floor_name] = {"state": "skipped", "reason": reason}
            continue
        # The live layout is authoritative, including manual applies/resets.
        # Persisted cal['applied'] is just historical metadata.
        current = {str(r.get("entity_id")): r.get("correction", 1.)
                   for r in floor.get("receivers", []) if str(r.get("entity_id")) in on_floor}
        result_source = source_for_floor(cal.get("_auto_sources", {}).get(floor_name), floor_name)
        result, updates, decision = await hass.async_add_executor_job(
            assess_update, snapshot, history, current, floor_name, time.time(), solve, _true_distance_m)
        if not updates:
            statuses[floor_name] = decision
        if result is not None:
            result["bermuda_source"] = result_source
            cal["results"][floor_name] = result
            cal["last_solved_at"] = result["solved_at"]
        if not updates:
            continue
        solved_updates = copy.deepcopy(updates)
        updates = _equivalent_updates(hass, floor, updates, identities=snapshot["receivers"])
        current = _equivalent_updates(hass, floor, current, identities=snapshot["receivers"])
        if not updates:
            decision.update(state="skipped", updated_receivers=0,
                            reason="Receiver identities or equivalent placements changed; match conflicting placements before applying")
            statuses[floor_name] = decision
            continue
        if tracking_recording is not None:
            # Deferred import: __init__ imports this module before defining its
            # live solver, so it cannot be imported at module initialization.
            from . import trilaterate
            from .calibration_validation import compare_tracking_export
            try:
                report = await hass.async_add_executor_job(compare_tracking_export,
                    tracking_recording, floor_name, {**current, **updates},
                    copy.deepcopy(floor), trilaterate, current, copy.deepcopy(coords), set(on_floor))
            except ValueError as error:
                decision.update(state="skipped", updated_receivers=0,
                                reason=f"Tracking validation could not run: {error}")
                statuses[floor_name] = decision
                continue
            decision["tracking_validation"] = {k: report[k] for k in
                ("verdict", "baseline", "candidate", "locations", "candidate_failures",
                 "verification", "trackers") if k in report}
            if report["verdict"] == "worse":
                decision.update(state="skipped", updated_receivers=0,
                                reason="Measured beacon tracking would worsen; keeping current corrections")
                statuses[floor_name] = decision
                continue
        for receiver in floor.get("receivers", []):
            correction = updates.get(str(receiver.get("entity_id")))
            if correction is not None:
                receiver["correction"] = correction
        floor["calibration"] = {
            "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "auto": True,
            "pairs_used": result["pairs_used"],
            "error_factor_before": result["error_factor_before"],
            "error_factor_after": result["error_factor_after"],
            "auto_decision": decision,
        }
        applied[floor_name] = {**current, **updates}
        accepted[floor_name] = decision
        pending_identities[floor_name] = (solved_updates, snapshot["receivers"], updates)
        changed = True

    if changed:
        # Tracking replay and later floors await executors too. Recheck the
        # original canonical solve keys for every accepted floor before saving
        # the batch; expanded alias keys must not re-seed replacement hardware.
        for floor in coords.get("floor", []):
            name = floor.get("name")
            if name not in pending_identities:
                continue
            solved_updates, identities, expected = pending_identities[name]
            if _equivalent_updates(hass, floor, solved_updates, identities=identities) != expected:
                for accepted_name in accepted:
                    statuses[accepted_name] = {"state": "skipped", "reason": "Scanner identities changed while calibrating; current corrections retained"}
                return
        try:
            await save_bps_data(hass, coords)
        except BaseException:
            for name in applied:
                statuses[name] = {"state": "skipped", "reason": "Update was not saved; current corrections retained"}
            raise
        cal["applied"].update(applied)
        statuses.update(accepted)
        _LOGGER.info("Auto-calibration updated receiver corrections")


async def start_calibration(hass, floor_name: str, duration: int) -> dict:
    cal = get_calibration_state(hass)
    if cal["mode"] == "auto":
        raise ValueError("Auto calibration is running; turn it off for a manual run.")
    if cal["state"] == "sampling":
        raise ValueError("A calibration is already running.")
    # A finished task object may still be mid-teardown; make sure it is gone
    # before its state fields are reused.
    await _stop_task(cal)

    coords = await _read_coords(hass)
    if not coords:
        raise ValueError("No BPS data saved yet.")
    floor = _find_floor(coords, floor_name)
    if floor is None:
        raise ValueError(f'No floor named "{floor_name}".')
    if not floor.get("scale"):
        raise ValueError("The floor has no scale; set it before calibrating.")

    receivers = _build_receiver_map(coords, floor_name=floor_name, hass=hass)
    if len(receivers) < 3:
        raise ValueError("At least three placed receivers are needed to calibrate.")

    duration = max(MIN_DURATION, min(MAX_DURATION, int(duration or DEFAULT_DURATION)))
    now = time.time()
    cal.update(
        {
            "state": "sampling",
            "mode": "manual",
            "floor": floor.get("name"),
            "started_at": now,
            "ends_at": now + duration,
            "duration": duration,
            "samples": {},
            "_bermuda_source": None,
            "receivers": receivers,
            "matched_placed": {},  # fresh window: re-learn which placements match
            "all_placed_slugs": _all_placed_slugs(coords),
            "error": None,
        }
    )
    for key in ("_auto_floor_receivers", "_auto_routes", "_auto_sources", "_auto_ambiguous_slugs", "_auto_geometry"):
        cal.pop(key, None)
    _refresh_auto_history(cal)
    cal["task"] = hass.async_create_task(_sample_loop(hass, cal))
    return cal


async def _stop_task(cal: dict) -> None:
    # HTTP solves have their own awaiters. Changing sessions must invalidate
    # those results even when no sampling/auto task is currently running.
    cal["_manual_solve_generation"] = object()
    task = cal.get("task")
    cal["task"] = None
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def set_auto_calibration(hass, enabled: bool) -> None:
    """Enable/disable continuous calibration and persist the flag."""
    cal = get_calibration_state(hass)
    coords = await _read_coords(hass)
    if not coords:
        raise ValueError("No BPS data saved yet.")

    if bool(coords.get("auto_calibration")) != bool(enabled):
        async with BPS_FILE_LOCK:
            # Re-read inside the lock so a concurrent writer is not clobbered.
            coords = await _read_coords(hass) or coords
            coords["auto_calibration"] = bool(enabled)
            await save_bps_data(hass, coords)

    await _stop_task(cal)
    if enabled:
        if "_auto_geometry" not in cal:
            cal["_auto_geometry"] = copy.deepcopy(cal.get("receivers", {}))
        # Keep raw samples/results for the existing matrix. Automatic updates
        # nevertheless require fresh independent evidence from this session.
        cal.update(
            {
                "state": "sampling",
                "mode": "auto",
                "floor": None,
                "started_at": time.time(),
                "ends_at": None,
                "duration": None,
                "receivers": _build_receiver_map(coords, hass=hass),
                "matched_placed": {},  # fresh window: re-learn which placements match
                "all_placed_slugs": _all_placed_slugs(coords),
                "error": None,
            }
        )
        _set_auto_receivers(hass, cal, coords)
        cal.pop("_auto_sources", None)
        # Persisted raw windows keep the matrix warm, but independent evidence
        # must be observed afresh after restart or a new automatic session.
        cal["_auto_samples"] = {}
        _refresh_auto_history(cal)
        cal["_advert_stamps"] = {}
        cal["auto_status"] = {}
        cal["task"] = hass.async_create_task(_auto_loop(hass, cal))
    else:
        cal["state"] = "idle"
        cal["mode"] = "off"


def refresh_receivers_from_coords(hass, coordinates_json) -> None:
    """Keep an ACTIVE calibration window tracking placements edited mid-run.

    Called by the panel-save endpoint right after bpsdata.txt is written: a
    re-linked, added, or removed receiver takes effect on the next dump
    instead of waiting for the next manual window or auto solve cycle (up to
    15 minutes of ingesting — and reporting missing — against stale slugs).
    """
    cal = hass.data.get(DOMAIN, {}).get("calibration")
    if not cal or cal.get("state") != "sampling":
        return
    try:
        coords = json.loads(coordinates_json) if coordinates_json else None
        if not isinstance(coords, dict):
            return
        floor = cal.get("floor") if cal.get("mode") == "manual" else None
        new_map = _build_receiver_map(coords, floor_name=floor, hass=hass)
        if floor is not None and not new_map:
            # The floor this manual window is calibrating vanished from the
            # save (deleted or de-scaled mid-run). Keep the start-of-run
            # snapshot rather than blinding the window — an empty map would
            # end the run with a misleading "0 usable pairs / check iBeacon"
            # error instead of a coherent (if now moot) report.
            return
        if cal.get("mode") == "auto":
            _set_auto_receivers(hass, cal, coords)
        else:
            # Establish the old map as the baseline even for a window that
            # predates geometry bookkeeping. The first edit must not baseline
            # the replacement hardware against observations from the old map.
            _refresh_auto_history(cal)
            cal["receivers"] = new_map
        _refresh_auto_history(cal)
        cal["all_placed_slugs"] = _all_placed_slugs(coords)
    except Exception as e:
        # A save must never fail because of calibration bookkeeping.
        _LOGGER.debug("Could not refresh calibration receivers from save: %s", e)


async def async_start_auto_if_enabled(hass) -> None:
    """Resume continuous calibration after a restart when the flag is set."""
    coords = await _read_coords(hass)
    if coords and coords.get("auto_calibration"):
        try:
            await set_auto_calibration(hass, True)
            _LOGGER.info("Auto-calibration resumed")
        except Exception as e:
            _LOGGER.warning("Could not resume auto-calibration: %s", e)


async def async_shutdown_calibration(hass) -> None:
    """Stop any running calibration task (integration unload/shutdown)."""
    state = hass.data.get(DOMAIN, {}).get("calibration")
    if state:
        await _stop_task(state)
        state["state"] = "idle"
        state["mode"] = "off"
        if state["samples"] or state["results"]:
            await save_calibration_state(hass)


async def apply_corrections(hass, cal: dict, floor_name: str) -> int:
    """Write the solved corrections into bpsdata.txt. Returns receivers updated."""
    result = None
    for name, res in cal["results"].items():
        if _normalize(name) == _normalize(floor_name):
            result = res
            break
    if not result:
        raise ValueError("No calibration result to apply for this floor.")
    async with BPS_FILE_LOCK:
        return await _apply_result_locked(hass, cal, result)


async def _apply_result_locked(hass, cal: dict, result: dict) -> int:
    coords = await _read_coords(hass)
    if not coords:
        raise ValueError("No BPS data saved.")
    floor = _find_floor(coords, result["floor"])
    if floor is None:
        raise ValueError(f'Floor "{result["floor"]}" no longer exists.')

    updated = 0
    corrections = _equivalent_updates(hass, floor, result["receivers"], identities=result.get("receiver_identities"))
    if not corrections:
        raise ValueError("No eligible receiver placements remain; match conflicting placements before applying.")
    for receiver in floor.get("receivers", []):
        correction = corrections.get(str(receiver.get("entity_id")))
        if correction is not None:
            receiver["correction"] = correction
            updated += 1
    floor["calibration"] = {
        "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "auto": False,
        "pairs_used": result["pairs_used"],
        "error_factor_before": result["error_factor_before"],
        "error_factor_after": result["error_factor_after"],
    }
    await save_bps_data(hass, coords)
    cal["applied"][floor.get("name")] = corrections
    return updated


async def reset_corrections(hass, cal: dict, floor_name: str) -> int:
    async with BPS_FILE_LOCK:
        coords = await _read_coords(hass)
        if not coords:
            raise ValueError("No BPS data saved.")
        floor = _find_floor(coords, floor_name)
        if floor is None:
            raise ValueError(f'No floor named "{floor_name}".')

        removed = 0
        for receiver in floor.get("receivers", []):
            if receiver.pop("correction", None) is not None:
                removed += 1
        floor.pop("calibration", None)
        cal["applied"].pop(floor.get("name"), None)

        await save_bps_data(hass, coords)
        return removed


def _status_payload(cal: dict) -> dict:
    pair_counts = {key: len(values) for key, values in cal["samples"].items()}
    payload = {
        "state": cal["state"],
        "mode": cal["mode"],
        "floor": cal["floor"],
        "error": cal["error"],
        "results": cal["results"],
        "last_solved_at": cal["last_solved_at"],
        "pair_counts": pair_counts,
        "receiver_count": len(cal["receivers"]),
        "auto_status": cal.get("auto_status", {}),
    }
    if cal["mode"] == "manual" and cal["state"] == "sampling" and cal["ends_at"]:
        payload["seconds_left"] = max(0, int(cal["ends_at"] - time.time()))
        payload["duration"] = cal["duration"]
    return payload


class BPSCalibrationAPI(HomeAssistantView):
    """Start, watch, apply, and reset receiver calibration."""

    url = "/api/bps/calibration"
    name = "api:bps:calibration"
    requires_auth = True

    def __init__(self, validate_tracking=None):
        self.validate_tracking = validate_tracking

    async def get(self, request):
        hass = request.app["hass"]
        return web.json_response(_status_payload(get_calibration_state(hass)))

    async def post(self, request):
        hass = request.app["hass"]
        cal = get_calibration_state(hass)
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        action = data.get("action")

        try:
            if action == "start":
                await start_calibration(hass, data.get("floor"), data.get("duration"))
            elif action == "auto":
                await set_auto_calibration(hass, bool(data.get("enabled")))
            elif action == "cancel":
                if cal["mode"] == "auto":
                    await set_auto_calibration(hass, False)
                else:
                    await _stop_task(cal)
                    cal["state"] = "idle"
                    cal["mode"] = "off"
                    cal["error"] = None
            elif action == "solve":
                # Re-solve from the samples already collected (e.g. after an
                # early cancel, or to inspect before the window ends).
                lock = cal.setdefault("_manual_solve_lock", asyncio.Lock())
                pending = cal.get("_manual_solve_pending")
                if lock.locked() or (pending is not None and not pending.done()):
                    return web.json_response({**_status_payload(cal),
                        "error": "A calibration solve is already running"}, status=409)
                async with lock:
                    bucket = hass.data[DOMAIN]
                    if bucket.get("_tracking_active") is False:
                        return web.json_response({**_status_payload(cal),
                            "error": "BPS is stopped; calibration solve is unavailable"}, status=409)
                    generation = cal.setdefault("_manual_solve_generation", object())
                    layout = bucket.get("layout")
                    lifecycle = bucket.get("_tracking_lifecycle")
                    session = (cal.get("mode"), cal.get("task"), cal.get("floor"),
                               cal.get("started_at"), cal.get("last_solved_at"))
                    floor_name = data.get("floor") or cal.get("floor")
                    # Request cancellation cannot stop a native executor
                    # thread. Retain admission until its private solve finishes;
                    # only this awaiting handler is allowed to publish a result.
                    async def solve_privately():
                        try:
                            return await async_solve(hass, cal, floor_name), None
                        except Exception as error:
                            # Python 3.14 reports an abandoned shielded future's
                            # exception even when the source task consumes it.
                            # Carry failures as values until a live handler can
                            # raise them through its existing HTTP error path.
                            return None, error

                    pending = asyncio.create_task(solve_privately())
                    cal["_manual_solve_pending"] = pending

                    def solve_finished(done):
                        if cal.get("_manual_solve_pending") is done:
                            cal.pop("_manual_solve_pending", None)
                        if not done.cancelled():
                            done.exception()  # also consume abandoned failures

                    pending.add_done_callback(solve_finished)
                    result, error = await asyncio.shield(pending)
                    if error is not None:
                        raise error
                    bucket = hass.data.get(DOMAIN, {})
                    current_session = (cal.get("mode"), cal.get("task"), cal.get("floor"),
                                       cal.get("started_at"), cal.get("last_solved_at"))
                    if (bucket.get("calibration") is not cal
                            or cal.get("_manual_solve_generation") is not generation
                            or current_session != session
                            or bucket.get("layout") is not layout
                            or bucket.get("_tracking_lifecycle") is not lifecycle
                            or bucket.get("_tracking_active") is False):
                        return web.json_response({**_status_payload(cal),
                            "error": "Calibration changed while solving; retry with current data"}, status=409)
                    cal["results"][result["floor"]] = result
                    cal["last_solved_at"] = result["solved_at"]
                    cal["error"] = None
            elif action == "validate_tracking":
                if self.validate_tracking is None:
                    raise ValueError("Tracking validation is unavailable")
                return web.json_response(await self.validate_tracking(hass, data.get("floor") or cal.get("floor")))
            elif action == "preview_bermuda":
                # Read-only: no services, writes, solves, or state publication.
                coords = await _read_coords(hass)
                if not coords:
                    raise ValueError("No BPS data saved yet")
                name = data.get("floor") or cal.get("floor")
                result = next((r for n, r in cal.get("results", {}).items()
                    if _normalize(n) == _normalize(name)), None)
                source = runtime_context(hass, coords, name)
                tracking = None
                if result is not None and self.validate_tracking is not None:
                    validation_coords = copy.deepcopy(coords)
                    validation_source = copy.deepcopy(source)
                    try:
                        tracking = await self.validate_tracking(hass, name)
                    except ValueError:
                        pass  # Export still explains missing measured evidence.
                    # Validation awaits work; bind the export to current settings
                    # and the exact result rather than a replaced candidate.
                    coords = await _read_coords(hass)
                    if not coords:
                        raise ValueError("BPS layout changed during preview; refresh it")
                    cal = hass.data.get(DOMAIN, {}).get("calibration", {})
                    result = next((r for n, r in cal.get("results", {}).items()
                        if _normalize(n) == _normalize(name)), None)
                    source = runtime_context(hass, coords, name)
                    if coords != validation_coords or source != validation_source:
                        tracking = None
                if cal.get("mode") == "auto":
                    coords["auto_calibration"] = True
                return web.json_response(build_preview(coords, name, result, source, tracking, hass=hass))
            elif action == "apply":
                updated = await apply_corrections(hass, cal, data.get("floor") or cal.get("floor"))
                await save_calibration_state(hass)
                return web.json_response({"applied": updated, **_status_payload(cal)})
            elif action == "reset":
                removed = await reset_corrections(hass, cal, data.get("floor") or cal.get("floor"))
                await save_calibration_state(hass)
                return web.json_response({"reset": removed, **_status_payload(cal)})
            else:
                return web.json_response({"error": f"Unknown action {action!r}"}, status=400)
        except ValueError as e:
            return web.json_response({**_status_payload(cal), "error": str(e)}, status=400)

        return web.json_response(_status_payload(cal))
