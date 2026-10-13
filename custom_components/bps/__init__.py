import aiofiles
import aiofiles.os
import time
from pathlib import Path
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.frontend import async_remove_panel
from homeassistant.components import panel_custom
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.const import UnitOfLength
from homeassistant.util.unit_conversion import DistanceConverter
from homeassistant.util import slugify
import numpy as np
from scipy.optimize import least_squares
import voluptuous as vol
import logging
import asyncio
import math
import os
import json
import re
import copy
from functools import partial
from contextvars import ContextVar
from collections.abc import Mapping
from shapely.geometry import Point, Polygon
from shapely.ops import nearest_points, unary_union
try:
    from shapely.validation import make_valid as shapely_make_valid
except ImportError:  # very old shapely
    shapely_make_valid = None
from asyncio import Lock, Queue, wait_for, TimeoutError

from .calibration import (
    BPSCalibrationAPI,
    async_restore_calibration_state,
    async_shutdown_calibration,
    async_start_auto_if_enabled,
    get_calibration_state,
    refresh_receivers_from_coords,
)
from .storage import (
    BPS_FILE_LOCK,
    get_bps_data,
    get_bps_data_for_edit,
    load_bps_data,
    migrate_legacy,
    save_bps_data,
)
from .const import ACCURACY_ENTITY_ID
from . import history as history_mod
from .zone_adjust import adjust_zones, adjust_subzones
from .environment import outdoor_settings, finite_number, reading_max_age
from .outdoor_tracking import solve_outdoor, account_for_published_position
from .display_radius import reporting_radius_limit
from .sparse_tracking import solve_sparse_detection
from .solver_settings import MIN_WEIGHT_RADIUS_M, SOLVER_ROBUST_F_SCALE
from .tracker_groups import fuse_groups, normalize_groups
from .outdoor_config import validate_outdoor_layout
from .diagnostics import BPSDiagnosticsAPI, activate as activate_diagnostics, shutdown as shutdown_diagnostics
from .receiver_matching import inventory as receiver_inventory
from .receiver_sources import ReceiverSources
from .gps_locations import BPSGPSLocationsAPI, validate_georeference
from .height_sources import BPSHeightSourcesAPI, validate_height_surveys
from . import bermuda_diagnostics
from . import recent_tracking

_LOGGER = logging.getLogger(__name__)

DOMAIN = "bps"
OPTION_SHOW_SIDEBAR_PANEL = "show_sidebar_panel"
FRONTEND_PATH = Path(__file__).parent / "frontend"
LEGACY_BPS_ENTITY_PATTERN = re.compile(r"^sensor\.(.+)_\1_bps_(zone|floor)$")

# Global data (the layout now lives in the Store; see storage.get_bps_data)
state_change_lock = Lock()
state_change_counter = {}
update_queue = Queue()
tracked_listeners = {}
tracked_entities = []
new_global_data = {}
secToUpdate = 1
# A scanner Bermuda hasn't heard for this long is treated as offline; the
# liveness is polled from dump_devices every RECEIVER_DUMP_INTERVAL seconds.
RECEIVER_OFFLINE_SECS = 30
RECEIVER_DUMP_INTERVAL = 15
apitricords = []
# A tracker not detected by any receiver for this long disappears from the
# map and its zone/floor sensors go to unknown. Override with a top-level
# "position_timeout" (seconds) in bpsdata.txt.
STALE_POSITION_SECS = 300

# One awaited batch per installation, with bounded parallel tracker work. The
# context follows child tasks so every post-executor publication uses the same
# layout and lifecycle that supplied the cycle's measurements.
MAX_CONCURRENT_TRACKERS = 8
_tracking_context = ContextVar("bps_tracking_context", default=None)
_receiver_sources_context = ContextVar("bps_receiver_sources", default=None)


def _direct_tracker_keys(hass):
    sources = _receiver_sources_context.get()
    if sources is None or sources.hass is not hass:
        sources = ReceiverSources(hass)
    return sources.direct_trackers()


def _capture_tracking_context(hass):
    inherited = _tracking_context.get()
    if inherited is not None and inherited[0] is hass:
        return inherited
    bucket = hass.data.get(DOMAIN, {})
    return hass, bucket.get("layout"), bucket.get("_tracking_lifecycle")


def _tracking_context_current(context):
    hass, layout, lifecycle = context
    bucket = hass.data.get(DOMAIN, {})
    return (bucket.get("_tracking_active") is not False
            and bucket.get("layout") is layout
            and bucket.get("_tracking_lifecycle") is lifecycle)


async def _tracking_executor_job(hass, function, *args):
    """Keep physical worker admission until completion, including after reload.

    Cancelling an awaiter cannot stop its executor thread. The semaphore and
    owned tasks survive lifecycle changes in the per-HA bucket, so new batches
    cannot queue more native jobs while abandoned private calculations run.
    """
    bucket = hass.data.setdefault(DOMAIN, {})
    slots = bucket.setdefault("_tracking_executor_slots", asyncio.Semaphore(MAX_CONCURRENT_TRACKERS))
    jobs = bucket.setdefault("_tracking_executor_jobs", set())
    await slots.acquire()

    async def owned():
        try:
            return await hass.async_add_executor_job(function, *args), None
        except Exception as error:
            # Python 3.14 reports exceptions in abandoned shielded tasks even
            # if a callback retrieves them. Transfer errors as outcome values.
            return None, error

    try:
        task = asyncio.create_task(owned())
    except BaseException:
        slots.release()
        raise
    jobs.add(task)

    def finished(done):
        jobs.discard(done)
        slots.release()
        if not done.cancelled():
            done.exception()

    task.add_done_callback(finished)
    result, error = await asyncio.shield(task)
    if error is not None:
        raise error
    return result


async def _stop_tracking(hass):
    """Invalidate publications before cancelling and draining background work.

    Cancelling an executor future cannot stop its thread. Workers own private
    snapshots; invalidation also rejects results from any independently awaited
    batch that finishes during integration teardown or a later reload.
    """
    bucket = hass.data.setdefault(DOMAIN, {})
    bucket["_tracking_active"] = False
    bucket["_tracking_lifecycle"] = object()
    bucket.pop("_tracking_setup", None)
    bucket.pop("_tracking_initialize", None)
    task = hass.data.pop("bps_update_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as error:
            _LOGGER.warning("BPS tracking task stopped after failure: %.240s", error)

# --- Stale distance readings (per receiver, per tracker) ----------------------
# A distance_to sensor keeps its last value when its scanner stops hearing the
# tracker: the reading goes STUCK rather than unavailable (most visible on
# Bermuda's unfiltered distance entities, which have no timeout of their own).
# Fed to the solver, a stuck radius anchors the fix to a receiver that can no
# longer see the device. Readings older than this are dropped from the solve;
# override with a top-level "reading_max_age" (seconds) in the layout, or set it
# to 0 to disable the gate entirely.
READING_MAX_AGE_SECS = 30

# --- Output-position smoothing (constant-velocity Kalman filter) -------------
# The published position is smoothed with a constant-velocity 2D Kalman filter
# (state [x, y, vx, vy]) instead of a fixed-length moving average. Unlike the
# old 3-sample mean, the filter carries a motion model, so it lags less while a
# tracker is walking and settles more while it is still, and it adapts its gain
# to the estimated uncertainty rather than weighting every past fix equally.
#
# The noise parameters are defined in METRES (and metres/second) and converted
# into each floor's pixel space via the floor scale, so the filter behaves the
# same on maps of any resolution. They are deliberately "trusting": BPS already
# reads Bermuda's smoothed rssi_distance (20-sample average + velocity gate), so
# the trilateration fixes fed in here are not raw-RSSI noisy. Tune KF_MEAS_NOISE_M
# up for more smoothing, or KF_ACCEL_NOISE_MS2 up for a snappier response.
KF_MEAS_NOISE_M = 1.5        # per-fix position uncertainty (m); larger = smoother
KF_ACCEL_NOISE_MS2 = 0.5     # expected acceleration (m/s^2); larger = more responsive
KF_INIT_VEL_UNC_MS = 1.0     # initial velocity uncertainty (m/s) at (re)init
KF_MAX_DT_S = 10.0           # cap the prediction step so a gap can't blow up P
KF_MAX_GAP_S = 30.0          # gap beyond which state is reset (tracker was away)
# Soft-gate scale for spiky per-receiver distances: a reading whose radius
# changed by this fraction versus the previous update is down-weighted to 0.5
# (was a hard 50% discard). Nothing is dropped, so the solver keeps enough
# points to fix a position even while every distance is legitimately changing
# during movement.
RADIUS_JUMP_TOL = 0.5
# Uploaded floor-plan maps: accepted image extensions and a size cap. Client
# filenames are never trusted for filesystem paths (see _safe_maps_child).
# .jfif/.jpe are ordinary JPEG variants a browser's image/jpeg picker yields.
_ALLOWED_MAP_EXTS = {
    ".png", ".jpg", ".jpeg", ".jfif", ".jpe", ".gif", ".webp", ".bmp", ".svg", ".avif",
}
MAX_MAP_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB
# Non-map files that live in www/bps_maps and must never be deletable via the
# save_text "remove" field (that field is only meant to drop an old map image).
_PROTECTED_MAPS_FILES = {"bpsdata.txt", "bps_calibration_state.json"}
# Longest to wait on Bermuda's dump_devices before treating it as unavailable,
# so a hung/slow service can't stall the liveness or calibration loops.
DUMP_DEVICES_TIMEOUT_S = 10.0


def _safe_maps_child(maps_path, raw_name, allowed_exts=None):
    """Resolve raw_name to a path guaranteed to sit directly inside maps_path.

    Returns the resolved Path, or None if raw_name is unsafe. Path(name).name
    strips any directory component (so '../x' and '/etc/x' both collapse to a
    bare filename), and the resolved result is re-checked against maps_path as
    belt-and-suspenders. Client-supplied filenames must never reach the
    filesystem unfiltered (unauthenticated write endpoints, issue: audit #2).
    """
    if not raw_name:
        return None
    name = Path(str(raw_name)).name
    if not name or name in (".", ".."):
        return None
    if allowed_exts is not None and Path(name).suffix.lower() not in allowed_exts:
        return None
    base = Path(maps_path).resolve()
    target = (base / name).resolve()
    try:
        if not target.is_relative_to(base):
            return None
    except AttributeError:  # Python < 3.9 (HA is 3.12+; defensive only)
        if base != target and base not in target.parents:
            return None
    return target

# --- Receiver mount heights (optional per-receiver "height", metres) ---------
# Bermuda's distance estimates are line-of-sight SLANT ranges, but the map
# solve is 2D: a ceiling probe reading 2.3 m to a tracker right below it is
# really ~0.6 m away horizontally. When a receiver's mount height is set in
# the panel, the vertical leg is removed before trilateration
# (horizontal = sqrt(slant^2 - dz^2)). Trackers are assumed to be carried at
# TRACKER_HEIGHT_M above the floor; override with a top-level
# "tracker_height" (metres) in bpsdata.txt.
TRACKER_HEIGHT_M = 1.0

# --- Per-tracker reference-power trim (issue #92) -----------------------------
# Bermuda turns RSSI into distance with an exponential path-loss model,
# d = 10 ** ((ref_power - rssi) / (10 * attenuation)). Cheap beacons vary in
# transmit power, so one Bermuda ref_power can read consistently long or short
# for a given tag. BPS can't change Bermuda's config, but an offset of `delta`
# dB on ref_power is exactly a MULTIPLICATIVE scale on every distance from that
# tracker: 10 ** (delta / (10 * attenuation)). So a per-tracker offset (stored
# in metres-free dB under the top-level "tracker_ref_offsets" map) is applied
# here as a distance factor — tunable live from the panel while watching the
# map, and portable back into Bermuda's own ref_power once a value is found.
# The exponent below is Bermuda's default attenuation; a user whose Bermuda
# uses a different one still gets a monotonic trim, just on a slightly
# different dB scale (this is a relative knob, not a calibrated instrument).
PATH_LOSS_EXPONENT = 3.0
TRACKER_REF_OFFSET_MAX_DB = 20.0  # +/- range accepted from the panel/API
# --- Floor election by hypothesis competition ---------------------------------
# The floor used to be elected by the single nearest receiver — one noisy
# reading through a ceiling could steal the tracker for a cycle (kitchen <->
# bedroom flapping, issue #94). Now every plausible floor is SOLVED and
# SCORED: the fit's agreement with all of that floor's receivers feeds a
# smoothed per-floor probability, and the elected floor only changes when a
# challenger clearly and persistently outscores the incumbent.
FLOOR_CANDIDATES = 3         # solve at most this many floors per cycle
FLOOR_PROB_SMOOTHING = 0.7   # EMA weight on the previous probability
FLOOR_SWITCH_MARGIN = 0.05   # probability lead that starts/keeps a challenge
FLOOR_SWITCH_CYCLES = 3      # consecutive leading cycles before the floor switches
FLOOR_DARK_GRACE_CYCLES = 3  # cycles a dark incumbent holds everything frozen
FLOOR_RESIDUAL_SCALE_M = 2.0 # weighted RMS residual (m) at which fit quality = 0.5
COVERAGE_TARGET_N = 5.0      # heard receivers at which the coverage term saturates

# No-go zones (issue #60): areas a tracker can't physically be — the upper
# footprint of a double-height foyer/great room open to the floor below.
# When a floor's fit lands in one of its no-go zones the fit is impossible on
# THAT floor, so its election confidence is multiplied down: the competition
# then prefers the floor where the same spot is a real room (the open space
# means that floor's receivers already hear the tracker and solve it as a
# candidate). The penalty only DOWN-WEIGHTS — a no-go floor that is the sole
# candidate still wins and its position is snapped out to the nearest allowed
# zone — so a tracker is never left position-less.
NO_GO_CONF_PENALTY = 0.15
# When snapping a fix out of dead space, grow the no-go footprint by this many
# pixels before subtracting it from the allowed region, so the snap target's
# boundary sits clear of the (boundary-inclusive) no-go edge rather than
# exactly on it — otherwise the snapped point still reads as "in the no-go
# zone" to covers()-based tests.
NO_GO_SNAP_MARGIN_PX = 3.0

# Per-tracker election state, all reset when the tracker is pruned:
# smoothed floor probabilities (entity -> {floor name: P}), the pending
# challenge (a floor out-scoring the incumbent, counted per cycle: entity ->
# {"floor": name, "count": n}), and how many consecutive cycles the incumbent
# floor has been dark (unsolvable) while a competitor solved.
_floor_probability = {}
_floor_challenge = {}
_floor_dark_cycles = {}

# Per-tracker Kalman state: entity -> {"x": np.array(4), "P": np.array(4,4),
# "ts": float, "floor": str}. Reset on floor change, long gap, or prune.
_kf_position_state = {}


def _tracker_height(data, entity=None):
    """Assumed tracker height above the floor (m) for slant correction.

    Precedence: the device's own entry in "tracker_heights" (set per tracker
    in the panel — an ankle beacon at 0.1 m and a phone at 1.0 m need
    different vertical legs), then the top-level "tracker_height" override,
    then the 1.0 m default. Out-of-range/garbage values fall through.
    """
    if isinstance(data, dict):
        per_tracker = data.get("tracker_heights")
        if entity is not None and isinstance(per_tracker, dict):
            configured = per_tracker.get(entity)
            # not-bool: isinstance(True, int) holds in Python, so a hand-edited
            # true/false would otherwise read as a valid 1.0/0.0 m height.
            if isinstance(configured, (int, float)) and not isinstance(configured, bool) \
                    and 0 <= configured <= 5:
                return float(configured)
        configured = data.get("tracker_height")
        if isinstance(configured, (int, float)) and not isinstance(configured, bool) \
                and 0 <= configured <= 5:
            return float(configured)
    return TRACKER_HEIGHT_M


HISTORY_FLUSH_INTERVAL = 60  # s between appends of buffered history to disk


def get_position_history(hass):
    """The per-install position history, created on first use."""
    bucket = hass.data.setdefault(DOMAIN, {})
    hist = bucket.get("_history")
    if hist is None:
        hist = bucket["_history"] = history_mod.PositionHistory(
            history_mod.history_config(get_bps_data(hass)))
    return hist


def history_dir(hass):
    """Where the NDJSON day segments live (under .storage, never web-served)."""
    return hass.config.path(".storage", history_mod.HISTORY_DIRNAME)


def _history_lock(hass):
    """Serialises every history disk operation (flush, prune, restore, clear).

    Without it a Clear can be undone: the periodic flush drains the queue and
    hands it to the executor, the Clear deletes the segments, and then the
    append lands - putting the forgotten positions back on disk, where the next
    restart reads them in again. The per-tracker rewrite has the mirror problem
    (read-filter-replace losing rows an append wrote in the meantime).
    """
    bucket = hass.data.setdefault(DOMAIN, {})
    lock = bucket.get("_history_lock")
    if lock is None:
        lock = bucket["_history_lock"] = asyncio.Lock()
    return lock


def _history_prunable_max_age(hass, hist):
    """The retention to prune against, or None when it can't be trusted.

    Pruning DELETES days of record irreversibly, so it must never run on a
    guessed window: if the layout cache isn't a dict (a fresh install, or a
    store that failed to load this boot) history_config falls back to the 6 h
    default, which would take a configured 7-day record down to six hours.
    """
    layout = get_bps_data(hass)
    if not isinstance(layout, dict):
        return None
    return hist.cfg["max_age"]


async def flush_position_history(hass, prune=False):
    """Append buffered points to today's segment; optionally prune old days.

    All file work happens in the executor. Failing here loses at most the
    buffered points (the in-memory ring is untouched), so it must never
    propagate into the tracking loop.
    """
    hist = get_position_history(hass)
    hist.configure(history_mod.history_config(get_bps_data(hass)))
    # Age every track, not just the ones that recorded this cycle: a tracker
    # that went silent (or a history since switched off) would otherwise keep
    # serving points past the configured window.
    hist.evict_all()
    if not hist.pending_count() and not prune:
        return
    dirpath = history_dir(hass)
    max_age = _history_prunable_max_age(hass, hist) if prune else None

    async with _history_lock(hass):
        # Drain INSIDE the lock so a Clear cannot slip between the drain and
        # the write and be overwritten by it.
        grouped = hist.drain_pending()
        if not grouped and max_age is None:
            return

        def _work():
            if grouped:
                history_mod.append_segments(dirpath, grouped)
            if max_age is not None:
                history_mod.prune_segments(dirpath, max_age)

        try:
            await hass.async_add_executor_job(_work)
        except Exception:
            # The write failed; put the rows back rather than dropping them on
            # the floor. requeue() honours the pending cap, so a disk that
            # stays broken cannot grow this without bound.
            hist.requeue(grouped)
            raise


async def restore_position_history(hass):
    """Reload the retained window from disk at startup.

    Every restored track is gap-marked afterwards: the integration was down
    for an unknown span, so the first new fix must start a fresh polyline
    rather than draw a straight line across the outage.
    """
    hist = get_position_history(hass)
    # A config-entry reload re-enters setup with the SAME PositionHistory:
    # async_unload_entry cancels the tracking task but leaves hass.data[DOMAIN]
    # alone, so re-reading the segments would append a second copy of every
    # point already held and leave the arrays unsorted, breaking every bisect
    # in query() and evict(). There is nothing to restore into a live ring.
    if hist.entities() or hist.pending_count():
        hist.mark_all_gaps()
        return
    dirpath = history_dir(hass)
    cfg = dict(hist.cfg)
    # Same rule as the periodic flush: pruning DELETES days irreversibly, so it
    # must never run on a guessed window. At startup the layout may not have
    # loaded (or may be unreadable this boot), in which case history_config
    # hands back the 6 h default - and pruning with that would take a
    # configured 7-day record down to six hours on every restart.
    prune_max_age = _history_prunable_max_age(hass, hist)

    # Parse AND build the tracks off the loop: the retained window can be
    # hundreds of thousands of rows and this runs during setup.
    def _work():
        if prune_max_age is not None:
            history_mod.prune_segments(dirpath, prune_max_age)
        loaded = history_mod.PositionHistory(cfg)
        loaded.load_rows(history_mod.restore_recent(dirpath, cfg))
        # The rows came FROM disk; nothing here needs writing back out.
        loaded.drain_pending()
        return loaded

    try:
        async with _history_lock(hass):
            loaded = await hass.async_add_executor_job(_work)
    except Exception as e:
        _LOGGER.warning("BPS position history could not be restored: %s", e)
        return
    hist.adopt(loaded)
    hist.mark_all_gaps()
    ents = hist.entities()
    _LOGGER.info("BPS position history restored: %d points across %d trackers",
                 sum((hist.retained(e) or {}).get("points", 0) for e in ents), len(ents))


def _reading_max_age(data):
    """Seconds after which a distance reading is ignored (0 = never)."""
    return reading_max_age(data, READING_MAX_AGE_SECS)


def _reading_age_secs(state, *, now=None):
    """Age of a state in seconds, or None when it can't be determined.

    Uses ``last_updated`` (falling back to ``last_changed``) rather than
    ``last_reported``: Home Assistant only bumps ``last_updated`` when the
    value actually changes, so a sensor that keeps re-reporting the SAME stale
    distance still ages out — which is exactly the stuck-reading case. Returns
    None (i.e. "don't gate") if the timestamps are missing or unusable, so an
    unexpected state object can never blank out the whole map.
    """
    ts = getattr(state, "last_updated", None) or getattr(state, "last_changed", None)
    if ts is None:
        return None
    try:
        return max(0.0, (time.time() if now is None else now) - ts.timestamp())
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        return None


def _tracker_ref_offset(data, entity):
    """This tracker's ref-power trim in dB (0.0 when unset). See issue #92."""
    if not isinstance(data, dict) or entity is None:
        return 0.0
    offsets = data.get("tracker_ref_offsets")
    if not isinstance(offsets, dict):
        return 0.0
    value = offsets.get(entity)
    # not-bool: isinstance(True, int) holds in Python, so a hand-edited
    # true/false would otherwise read as a valid +1 dB trim.
    if isinstance(value, (int, float)) and not isinstance(value, bool) \
            and abs(value) <= TRACKER_REF_OFFSET_MAX_DB:
        return float(value)
    return 0.0


def _tracker_distance_factor(data, entity):
    """Multiplicative distance scale from this tracker's ref-power trim.

    A ref_power offset of `delta` dB scales every distance by
    10 ** (delta / (10 * attenuation)) in Bermuda's path-loss model, so a
    positive trim reads the tracker as FARTHER and a negative one as nearer.
    Returns 1.0 (no-op) when no trim is configured.
    """
    offset = _tracker_ref_offset(data, entity)
    if offset == 0.0:
        return 1.0
    return 10.0 ** (offset / (10.0 * PATH_LOSS_EXPONENT))


def _floor_scale(data, entity, floor_name):
    """Pixels-per-metre for a tracker's elected floor (None if unknown)."""
    for ent in data:
        if ent.get("entity") == entity:
            for floor in ent["data"]["floor"]:
                if floor["name"] == floor_name:
                    return floor.get("scale")
    return None


def _jump_weight(r, prev_r, min_wr):
    """Soft down-weight for a radius that jumped since the previous update.

    Radii are clamped to the physical minimum for the comparison: a tracker
    genuinely next to a receiver bounces between sub-clamp readings from pure
    RSSI noise (0.1 <-> 0.3 m is noise, not motion), and an unclamped relative
    gate would penalize that — its most informative receiver — every tick.
    The slant-collapse pathology needs no gate help: the projection floor
    keeps a collapsed radius constant at the clamp (rel = 1 either way) and
    the measured-slant weight radius bounds its influence in the solve.
    """
    if prev_r is None:
        return 1.0  # first sighting on this floor: no basis to distrust it
    r_eff, prev_eff = max(r, min_wr), max(prev_r, min_wr)
    rel = max(r_eff / prev_eff, prev_eff / r_eff)  # symmetric relative change, >= 1
    return 1.0 / (1.0 + ((rel - 1.0) / RADIUS_JUMP_TOL) ** 2)


def _kalman_position_update(entity, floor_name, meas, scale, bounds):
    """Constant-velocity Kalman filter on the trilaterated pixel position.

    ``meas`` is the newest ``(x, y)`` fix in this floor's pixel space. Noise is
    specified in metres via the module KF_* constants and scaled to pixels with
    ``scale`` (pixels per metre) so the smoothing is resolution-independent. The
    state is (re)initialised at the measurement with zero velocity whenever there
    is no prior state, the elected floor changed (coordinates live in a different
    pixel space), or the gap since the last fix exceeds KF_MAX_GAP_S (the tracker
    was out of range, so its velocity is meaningless). Returns the filtered
    ``(x, y)`` clipped to ``bounds``; the raw fix is what feeds the filter, so the
    estimate is never biased by zone snapping applied downstream.
    """
    s = scale if isinstance(scale, (int, float)) and scale > 0 else 1.0
    r_var = (KF_MEAS_NOISE_M * s) ** 2          # measurement variance (px^2)
    a_var = (KF_ACCEL_NOISE_MS2 * s) ** 2       # accel variance (px^2/s^4)
    zx, zy = float(meas[0]), float(meas[1])
    now = time.time()
    st = _kf_position_state.get(entity)

    def _clip(px, py):
        if bounds is None:
            return px, py
        minx, miny, maxx, maxy = bounds
        return min(max(px, minx), maxx), min(max(py, miny), maxy)

    if st is None or st["floor"] != floor_name or now - st["ts"] > KF_MAX_GAP_S:
        v_var = (KF_INIT_VEL_UNC_MS * s) ** 2
        _kf_position_state[entity] = {
            "x": np.array([zx, zy, 0.0, 0.0], dtype=float),
            "P": np.diag([r_var, r_var, v_var, v_var]).astype(float),
            "ts": now,
            "floor": floor_name,
        }
        return _clip(zx, zy)

    dt = min(max(now - st["ts"], 1e-3), KF_MAX_DT_S)
    x, P = st["x"], st["P"]
    F = np.array(
        [[1, 0, dt, 0], [0, 1, 0, dt], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=float
    )
    # Piecewise white-noise-acceleration process covariance, per axis.
    dt2 = dt * dt
    dt3 = dt2 * dt
    dt4 = dt3 * dt
    q_axis = np.array([[dt4 / 4.0, dt3 / 2.0], [dt3 / 2.0, dt2]]) * a_var
    Q = np.zeros((4, 4))
    Q[np.ix_([0, 2], [0, 2])] = q_axis  # x, vx
    Q[np.ix_([1, 3], [1, 3])] = q_axis  # y, vy

    # Predict.
    x = F @ x
    P = F @ P @ F.T + Q
    # Update with the position measurement.
    H = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=float)
    R = np.diag([r_var, r_var]).astype(float)
    z = np.array([zx, zy], dtype=float)
    S = H @ P @ H.T + R
    K = P @ H.T @ np.linalg.inv(S)
    x = x + K @ (z - H @ x)
    P = (np.eye(4) - K @ H) @ P

    st["x"], st["P"], st["ts"], st["floor"] = x, P, now, floor_name
    return _clip(float(x[0]), float(x[1]))


def cleanup_legacy_bps_registry_and_states(hass: HomeAssistant):
    """Remove legacy duplicated BPS ids from entity registry and state machine."""
    entity_registry = er.async_get(hass)
    legacy_registry_ids = [
        entry.entity_id
        for entry in entity_registry.entities.values()
        if LEGACY_BPS_ENTITY_PATTERN.match(entry.entity_id)
    ]
    for entity_id in legacy_registry_ids:
        _LOGGER.info("Removing legacy BPS registry entity: %s", entity_id)
        entity_registry.async_remove(entity_id)

    legacy_state_ids = [
        state.entity_id
        for state in hass.states.async_all()
        if LEGACY_BPS_ENTITY_PATTERN.match(state.entity_id)
    ]
    for entity_id in legacy_state_ids:
        _LOGGER.info("Removing legacy BPS state: %s", entity_id)
        hass.states.async_remove(entity_id)

async def update_tracked_entities(hass):
    """Update tracked_entities with the result of the Jinja code once per second."""
    global tracked_entities, tracked_listeners, new_global_data
    global secToUpdate
    # Reloads may be requested by callbacks originating inside a tracker task;
    # the new background loop must not inherit that old batch's ContextVar.
    _tracking_context.set(None)
    _receiver_sources_context.set(None)
    while True:
        # Receiver liveness and the self-localization accuracy sensor are
        # receiver-side diagnostics, independent of how many beacons are being
        # tracked — so they run on their own slow cadence at the TOP of the loop,
        # BEFORE the "too few trackers" early-continues below (otherwise a fresh
        # deploy, or any time nobody is home, would never publish them).
        now_ts = time.time()
        if now_ts - getattr(update_tracked_entities, "last_liveness", 0.0) >= RECEIVER_DUMP_INTERVAL:
            update_tracked_entities.last_liveness = now_ts
            await update_receiver_liveness(hass)

        # Runs on the first tick too, so a fresh deploy shows a value quickly.
        # The scipy solves run in the executor so the loop is never blocked; the
        # bindings, geometry and calibration deques are snapshotted here on the
        # loop thread first, before calibration ingest or layout edits can run.
        if now_ts - getattr(update_tracked_entities, "last_selftest", 0.0) >= SELFTEST_SENSOR_INTERVAL:
            update_tracked_entities.last_selftest = now_ts
            try:
                snapshot = _selftest_snapshot(hass)
                result = await hass.async_add_executor_job(run_selftest, None, None, snapshot)
                state, attrs = _selftest_summary(result)
                update_bps_sensor_state(hass, ACCURACY_ENTITY_ID, state, attrs)
            except Exception as e:  # never let the diagnostic sensor stall tracking
                _LOGGER.warning("BPS self-test sensor update failed: %s", e)

        # Persist the position history at a slow cadence (and prune expired day
        # segments hourly), so a restart does not lose the scrubback window.
        if now_ts - getattr(update_tracked_entities, "last_history_flush", 0.0) >= HISTORY_FLUSH_INTERVAL:
            update_tracked_entities.last_history_flush = now_ts
            prune_due = now_ts - getattr(update_tracked_entities, "last_history_prune", 0.0) >= 3600
            if prune_due:
                update_tracked_entities.last_history_prune = now_ts
            try:
                await flush_position_history(hass, prune=prune_due)
            except Exception as e:  # disk trouble must not stop tracking
                _LOGGER.warning("BPS position history flush failed: %s", e)

        sources_token = None
        try:
            # This used to render a Jinja template that selected every
            # "sensor.*_distance_to_*" and then intersect it with the Bermuda
            # ones. The intersection was provably the second list: both walk
            # hass.states.async_all("sensor") and both require "_distance_to_"
            # in the id, so the template only ever added look-alikes from other
            # integrations for the next line to discard again. Rendering a
            # template over every sensor state, once a second, to compute a
            # superset of a list we already have was the single cost in this
            # loop that grew with the size of the user's Home Assistant rather
            # than with the number of beacons.
            tracked_entities = _bermuda_distance_sensor_ids(hass)
            receiver_sources = ReceiverSources(hass)
            sources_token = _receiver_sources_context.set(receiver_sources)
            direct_trackers = receiver_sources.direct_trackers()
            discover_sensors = hass.data.get(DOMAIN, {}).get("resume_sensor_discovery")
            if discover_sensors is not None and direct_trackers:
                discover_sensors(direct_trackers)

            await prune_stale_positions(hass)

            # Legacy admission still follows published distance entities. A
            # proved direct tracker must also run when its adverts disappear,
            # so its normal unavailable handling clears nearest-zone promptly.
            num_points = len(tracked_entities)
            if num_points == 0 and not direct_trackers:
                await update_tracker_groups(hass)
                await _capture_recent_tracking(hass)
                _LOGGER.info("There are no devices present to track, sleep 10 seconds")
                await asyncio.sleep(10)
                continue  # Skip and start over
            layout = get_bps_data(hass)
            legacy_admitted = num_points >= 3 or outdoor_settings(layout)["enabled"]
            if not legacy_admitted and not direct_trackers:
                await update_tracker_groups(hass)
                await _capture_recent_tracking(hass)
                _LOGGER.info("There are not enough trackers with available data to track, sleep 10 seconds")
                await asyncio.sleep(10)
                continue  # Skip and start over

            cleaned = [item.split("_distance_to_")[0].replace("sensor.", "") for item in tracked_entities] if legacy_admitted else []
            unique_values = list(set(cleaned) | direct_trackers)
            # Use a separate copy per entity to avoid cross-entity mutation side effects.
            new_global_data = [{"entity": ent, "data": copy.deepcopy(layout)} for ent in unique_values]

            await process_entities(hass, new_global_data, receiver_sources)

        except Exception as e:
            _LOGGER.info(f"Error executing Jinja code: {e}")
        finally:
            if sources_token is not None:
                _receiver_sources_context.reset(sources_token)

        await asyncio.sleep(secToUpdate)  # Run every X seconds, set timer in global variables


# State strings that mean "not working" when a status/availability entity is read.
_OFFLINE_STATES = {
    "", "unavailable", "unknown", "none", "off", "false",
    "not_home", "offline", "disconnected", "no",
}


def _state_looks_online(state):
    """Whether a status/availability entity's state reads as online."""
    if state is None:
        return False
    return str(state).strip().lower() not in _OFFLINE_STATES


def _bermuda_distance_sensor_ids(hass):
    """Entity ids of the ``sensor.*_distance_to_*`` sensors that actually belong
    to the ``bermuda`` integration.

    Other integrations expose look-alike distance sensors — e.g. an ESPHome
    mmWave presence sensor's ``..._distance_to_detection_object`` — which are not
    tracker-to-scanner distances and must never feed BPS. Every place that
    enumerates distance sensors (device tracking, the receiver/beacon debug
    views, the receiver picker) goes through this, mirroring the same
    ``platform == "bermuda"`` guard ``sensor.get_filtered_entities`` already
    applies to the sensor-creation path.
    """
    ent_reg = er.async_get(hass)
    ids = []
    for st in hass.states.async_all("sensor"):
        eid = st.entity_id
        if "_distance_to_" not in eid:
            continue
        entry = ent_reg.async_get(eid)
        if entry is None or entry.platform != "bermuda" or str(getattr(entry, "unique_id", "") or "").endswith("_range_raw"):
            continue
        ids.append(eid)
    return ids


def _scanner_slugs_and_readings(hass):
    """Single pass over Bermuda distance sensors: every scanner slug Bermuda
    exposes, and the subset that currently has a live reading (for the heuristic
    tier)."""
    slugs = set()
    with_reading = set()
    allowed = set(_bermuda_distance_sensor_ids(hass))
    for st in hass.states.async_all("sensor"):
        eid = st.entity_id
        if eid not in allowed:
            continue
        slug = eid.split("_distance_to_", 1)[1]
        slugs.add(slug)
        if st.state not in (None, "", "unknown", "unavailable"):
            with_reading.add(slug)
    return slugs, with_reading


_SCANNER_TOKEN_RE = re.compile(r"^[0-9a-f]{5,12}$")


def _scanner_token(slug):
    """The trailing hardware id Bermuda embeds in a scanner slug — the hex group
    derived from the device's MAC, e.g. 'master_bedroom_esp32c5_f17464' ->
    'f17464'. This survives renames of the human-readable prefix, so it is a
    stable identity for a physical scanner. None when the slug has no hex tail.
    """
    if not slug:
        return None
    last = str(slug).rsplit("_", 1)[-1].lower()
    return last if _SCANNER_TOKEN_RE.match(last) else None


def _suggest_scanner(placement_slug, stored_uid, candidates):
    """Best live scanner slug to re-link a stale placement to. Prefer a shared
    hardware token (an explicitly-stored uid, else the placement's own trailing
    token — the same physical scanner after a rename); otherwise require manual selection. Never suggest by
    string similarity alone. Returns a candidate slug or None.
    """
    token = stored_uid or _scanner_token(placement_slug)
    if token:
        tok_matches = [c for c in candidates if _scanner_token(c) == token]
        if len(tok_matches) == 1:
            return tok_matches[0]
    return None


def _placed_receivers(coordinates_json):
    """Placed receivers as (floor_name, slug, stored scanner_uid, scanner_mac).

    Parsed defensively: a malformed or hand-edited bpsdata.txt yields [] rather
    than raising into a caller (diagnostics must never break read_text). Each
    entity_id must be a non-empty string — a non-string slug isn't a real
    scanner name and would be unhashable when callers build a set of slugs.
    """
    placed = []
    try:
        parsed = json.loads(coordinates_json)
        floors = parsed.get("floor") if isinstance(parsed, dict) else None
        for fl in floors if isinstance(floors, list) else []:
            if not isinstance(fl, dict):
                continue
            receivers_ = fl.get("receivers")
            for rec in receivers_ if isinstance(receivers_, list) else []:
                if isinstance(rec, dict) and isinstance(rec.get("entity_id"), str) and rec["entity_id"]:
                    placed.append((fl.get("name"), rec["entity_id"], rec.get("scanner_uid"), rec.get("scanner_mac")))
    except Exception:
        return []
    return placed


# Distance-sensor states that mean "no distance right now" (as opposed to a
# real numeric reading). Shared by the diagnostics and the linking debug view.
_NO_DISTANCE_STATES = (None, "", "unknown", "unavailable")


def _receiver_floors(coordinates_json):
    try:
        parsed = json.loads(coordinates_json)
        floors = parsed.get("floor", []) if isinstance(parsed, dict) else []
        return [{**floor, "receivers": [rec if isinstance(rec, dict) else {} for rec in floor.get("receivers", [])]
                 if isinstance(floor.get("receivers"), list) else []}
                for floor in floors if isinstance(floor, dict)] if isinstance(floors, list) else []
    except (ValueError, TypeError):
        return []


def _alias_diagnostics(hass, coordinates_json, receiver_sources=None):
    sources = receiver_sources if receiver_sources is not None else ReceiverSources(hass)
    aliases, issues = [], []
    for floor in _receiver_floors(coordinates_json):
        for index, (rec, binding) in enumerate(zip(floor.get("receivers", []), sources.floor_bindings(floor))):
            row = {"floor": floor.get("name"), "placement_index": index, "entity_id": rec.get("entity_id"),
                   "aliases": binding["aliases"], "status": binding["status"]}
            if sources.blocked(binding):
                row["action"] = "Match this placement to the correct Bermuda scanner. Keep one placement per physical scanner on this floor; reconcile differing positions and properties."
                issues.append(row)
            elif binding["identity"]:
                row.update(scanner_mac=binding["identity"][1], config_entry_id=binding["identity"][0])
                aliases.append(row)
    return aliases, issues


def _scanner_diagnostics(hass, coordinates_json):
    """Compare placed receivers against the scanner slugs Bermuda actually
    exposes, to flag naming mismatches (issue #64):
      - unmatched_receivers: a placed slug that has NO matching distance sensor
        (a genuinely wrong/stale name), each with a suggested live scanner.
      - unplaced_scanners: scanner slugs currently reporting a distance that
        aren't placed on any floor.
    """
    slugs, with_reading = _scanner_slugs_and_readings(hass)
    sources = ReceiverSources(hass)
    matching = sources.matching
    slugs.update(row["entity_id"] for row in matching["options"])
    aliases, issues = _alias_diagnostics(hass, coordinates_json, sources)
    slugs.update(row["entity_id"] for row in aliases)
    placed = _placed_receivers(coordinates_json)
    placed_slugs = {p[1] for p in placed}
    placed_by_floor = {}
    for fname, slug, _uid, _mac in placed:
        key = fname if isinstance(fname, str) else None
        placed_by_floor.setdefault(key, set()).add(slug)
    # A saved hardware binding also places its proven current/previous aliases.
    # Keep them out of unplaced reports and same-floor relink suggestions.
    # Proven duplicate/conflicting placements still occupy that scanner; their
    # separate issue rows explain why it cannot currently supply tracking data.
    for row in aliases + issues:
        placed_slugs.update(row["aliases"])
        key = row["floor"] if isinstance(row["floor"], str) else None
        placed_by_floor.setdefault(key, set()).update(row["aliases"])
    # Candidates for a re-link: live scanner slugs not already correctly placed.
    candidates = {row["entity_id"]: row for row in matching["options"] if row["selectable"]}
    free_by_floor, identities_by_token = {}, {}
    for row in matching["options"]:
        token = row["token"] or _scanner_token(row["entity_id"])
        identities_by_token.setdefault(token, set()).add((tuple(row["config_entry_ids"]), row["scanner_mac"] or row["entity_id"]))
    unmatched = []
    for fname, slug, uid, mac in placed:
        if slug in slugs:
            continue  # a real sensor exists (offline is a separate concern)
        key = fname if isinstance(fname, str) else None
        if key not in free_by_floor:
            free_by_floor[key] = [option for candidate, option in candidates.items()
                                 if candidate not in placed_by_floor[key]]
        free = free_by_floor[key]
        token = uid or _scanner_token(slug)
        proof = []
        identities = identities_by_token.get(token, set()) if isinstance(token, str) else set()
        for option in free:
            option_mac = option["scanner_mac"]
            if mac and option_mac:
                matched = str(mac).lower() == option_mac
            else:
                candidate_token = option["token"] or _scanner_token(option["entity_id"])
                matched = token and token == candidate_token and len(identities) == 1
            if matched:
                proof.append(option["entity_id"])
        proof.sort(key=lambda candidate: (not candidates[candidate]["distance_sensors"], candidate))
        proof_ids = {(tuple(candidates[candidate]["config_entry_ids"]), candidates[candidate]["scanner_mac"] or candidate) for candidate in proof}
        unmatched.append({
            "entity_id": slug,
            "floor": fname,
            "suggested": proof[0] if len(proof_ids) == 1 else None,
        })
    unplaced = sorted(with_reading - placed_slugs)
    return {"unmatched_receivers": unmatched, "unplaced_scanners": unplaced, "matching": matching, "alias_receivers": aliases, "placement_issues": issues}


def _scanner_linking(hass, coordinates_json):
    """Debug view for issue #64: for every placed receiver, the Bermuda distance
    sensors that feed it and each one's current state.

    This distinguishes the two failure modes David couldn't tell apart from the
    map alone: a receiver correctly linked but simply not reporting a distance
    right now ("silent" — usually just no recent BLE contact), versus one whose
    name matches no distance sensor at all ("unmatched" — a real naming
    mismatch). Reads live HA state directly, so it does not depend on a device
    being actively tracked. Returns:
      - placed:   one row per placed receiver with its status and per-device
                  readings (the exact `<device>_distance_to_<slug>` sensors
                  update_receiver_radii looks up).
      - unplaced: scanner slugs that have distance sensors but no placement,
                  for context (a superset of the diagnostics' "reporting" list).
    """
    by_slug = {}
    allowed = set(_bermuda_distance_sensor_ids(hass))
    for st in hass.states.async_all("sensor"):
        eid = st.entity_id
        if eid not in allowed:
            continue
        device_part, slug = eid.split("_distance_to_", 1)
        device = device_part[len("sensor."):] if device_part.startswith("sensor.") else device_part
        state = None if st.state is None else str(st.state)
        by_slug.setdefault(slug, []).append({"device": device, "entity_id": eid, "state": state})

    def _reporting(sensors):
        return [s for s in sensors if s["state"] not in _NO_DISTANCE_STATES]

    sources = ReceiverSources(hass)
    matching = sources.matching
    options = {row["entity_id"]: row for row in matching["options"]}
    owned_by_identity = {}
    if sources.registry is not None:
        # Group the snapshot once; scanning every tracker/scanner reading for
        # each placement would block the event loop with quadratic work.
        for (_tracker, identity), eids in sources.readings.items():
            owned_by_identity.setdefault(identity, set()).update(eids)
    rows, placed_slugs = [], set()
    for floor in _receiver_floors(coordinates_json):
        for index, (rec, binding) in enumerate(zip(floor.get("receivers", []), sources.floor_bindings(floor))):
            slug = rec.get("entity_id", "")
            placed_slugs.update(binding["aliases"])
            sensors = sorted((sensor for alias in binding["aliases"] for sensor in by_slug.get(alias, [])), key=lambda row: row["entity_id"])
            if sources.registry is not None:
                key = binding["identity"] or slug
                # Malformed legacy slugs may be lists/dicts. Copy the group so
                # exact legacy fallbacks remain local to this placement.
                owned = set(owned_by_identity.get(key, ())) if isinstance(key, (str, tuple)) else set()
                for tracker in {sensor["device"] for sensor in sensors}:
                    source, state = sources.source(tracker, rec, binding)
                    if state is not None:
                        owned.add(source)
                sensors = [sensor for sensor in sensors if sensor["entity_id"] in owned]
            reporting = _reporting(sensors)
            matched = next((option for alias in binding["aliases"]
                            if (option := sources.resolved_option(alias, binding)) is not None),
                           options.get(slug) if isinstance(slug, str) else None)
            if sources.blocked(binding):
                status = "conflict"
            elif slug in hass.data.get(DOMAIN, {}).get("rl_offline", []):
                status = "offline"
            elif reporting:
                status = "live"
            elif sensors:
                status = "silent"
            else:
                status = "unknown" if matched else "unmatched"
            selected = {}
            for tracker in {sensor["device"] for sensor in sensors}:
                source, state = sources.source(tracker, rec, binding)
                if state is not None:
                    selected[tracker] = source
            rows.append({"entity_id": slug, "floor": floor.get("name"), "placement_index": index,
                "scanner_uid": rec.get("scanner_uid"), "scanner_mac": rec.get("scanner_mac"),
                "token": (matched or {}).get("token") or _scanner_token(slug), "status": status,
                "binding_status": binding["status"], "aliases": binding["aliases"], "selected_sources": selected,
                "sensor_count": len(sensors), "reporting_count": len(reporting), "sensors": sensors,
                "matching": matched})
    unplaced = []
    for slug, sensors in by_slug.items():
        if slug in placed_slugs:
            continue
        unplaced.append({
            "entity_id": slug,
            "token": _scanner_token(slug),
            "sensor_count": len(sensors),
            "reporting_count": len(_reporting(sensors)),
        })
    unplaced.sort(key=lambda u: u["entity_id"])
    return {"placed": rows, "unplaced": unplaced, "matching": matching}


def _beacon_links(hass):
    """Debug view: for every tracked device (beacon), the receivers currently
    detecting it, sorted closest -> farthest. The inverse of _scanner_linking
    (grouped by the tracked device instead of by the scanner). Distances are
    normalized to metres only for sorting — Bermuda reports per-entity feet or
    metres — while the value is shown in its own unit. A beacon with no live
    reading still appears (empty list) so a device that's gone dark is visible.
    """
    beacons = {}  # device -> [{scanner, distance, unit}]
    sources, seen = ReceiverSources(hass), set()
    allowed = set(_bermuda_distance_sensor_ids(hass))
    candidates = [(st.entity_id, st) for st in hass.states.async_all("sensor") if st.entity_id in allowed]
    included = {eid for eid, _state in candidates}
    direct_trackers = sources.direct_trackers()
    for (device, owner), proofs in sources.direct_tracker_proofs.items():
        if device not in direct_trackers or owner not in sources.direct.snapshots:
            continue
        # A disabled registry anchor still proves the existing BPS tracker and
        # receiver keys. Never invent display slugs from API names, or expose
        # disabled legacy states as readings when the direct API is absent.
        beacons.setdefault(device, [])
        for eid, _uid, _identity in proofs:
            if eid not in included:
                candidates.append((eid, None))
                included.add(eid)
    for eid, st in candidates:
        device_part, slug = eid.split("_distance_to_", 1)
        device = device_part[len("sensor."):] if device_part.startswith("sensor.") else device_part
        beacons.setdefault(device, [])
        binding = sources.binding({"entity_id": slug})
        selected, chosen = sources.source(device, {"entity_id": slug}, binding)
        provenance = getattr(chosen, "attributes", {}).get("bps_source", {}) if chosen is not None else {}
        is_direct = provenance.get("path") == "direct"
        identity = (device, binding["identity"] or slug, provenance.get("device_id") if is_direct else sources.tracker_ids.get(eid))
        if sources.registry is not None and ((not is_direct and selected != eid) or chosen is None or identity in seen):
            continue
        if st is None and not is_direct:
            continue
        seen.add(identity)
        if is_direct:
            st = chosen
        if st.state in _NO_DISTANCE_STATES:
            continue
        try:
            val = float(st.state)
        except (ValueError, TypeError):
            continue
        unit = st.attributes.get("unit_of_measurement")
        meters = val * 0.3048 if unit == "ft" else val
        beacons[device].append({
            "scanner": slug,
            "distance": round(val, 2),
            "unit": unit if isinstance(unit, str) and unit else "m",
            "_m": meters,
        })
    out = []
    for device in sorted(beacons):
        recs = sorted(beacons[device], key=lambda r: r["_m"])
        for r in recs:
            r.pop("_m", None)  # internal sort key only
        out.append({"device": device, "receivers": recs})
    return out


def _refresh_dump_ages(hass, dom, devices):
    """Update the cached per-scanner Bermuda-liveness ages from a dump payload.

    last_seen is monotonic (seconds since HA boot), not epoch; the freshest
    stamp in the payload is "now" on that clock. Re-anchor only when the payload
    aged forward — if newest didn't advance (every scanner stopped hearing
    adverts, a real fleet-wide outage) keep the previous ages so they grow with
    wall time and cross the timeout; a large backward jump is a monotonic clock
    reset (HA reboot), so accept it.
    """
    newest = 0.0
    for dev in devices.values():
        ls = dev.get("last_seen") if isinstance(dev, dict) else None
        if isinstance(ls, (int, float)) and ls > newest:
            newest = ls
    prev_newest = dom.get("rl_newest")
    now = time.time()
    if not (prev_newest is not None and prev_newest - 60 < newest <= prev_newest):
        ages, mac_ages, repeated_names, seen_names = {}, {}, set(), set()
        for address, dev in devices.items():
            if not isinstance(dev, dict) or dev.get("_is_scanner") is not True:
                continue
            slug = slugify(str(dev.get("name") or ""))
            if not slug:
                continue
            if slug in seen_names:
                repeated_names.add(slug)
            seen_names.add(slug)
            ls = dev.get("last_seen")
            if isinstance(ls, (int, float)) and not isinstance(ls, bool) and math.isfinite(ls):
                age = max(0.0, newest - ls)
                ages[slug] = age
                # Bermuda retains the Wi-Fi MAC in range entity IDs while
                # dump observations can use a different Bluetooth address.
                mac = dev.get("address_wifi_mac") or dev.get("address", address)
                if isinstance(mac, str) and bermuda_diagnostics.MAC.fullmatch(mac):
                    mac_ages[mac.lower()] = age
        for slug in repeated_names:
            ages.pop(slug, None)
        dom["rl_newest"] = newest
        dom["rl_anchor_wall"] = now
        dom["rl_ages"] = ages
        dom["rl_mac_ages"] = mac_ages


def _iter_registry_devices(dev_reg):
    """Every device in the registry, on any Home Assistant version, warning-free.

    Up to 2026.8, ``dev_reg.devices`` is a mapping keyed by device id: iterating
    it yields ids, and ``.values()`` is how you reach the entries. From 2026.9
    it is a read-only collection of the entries themselves, and every mapping
    use - ``.values()``, ``[id]``, ``.get()``, even ``hasattr(devices,
    "values")`` - logs a deprecation and stops working in 2027.9. So the shape
    is told apart with isinstance, which never touches the attributes that
    report.
    """
    devices = dev_reg.devices
    return devices.values() if isinstance(devices, Mapping) else devices


def _build_device_availability(hass):
    """Maps for the device-availability tier: slug -> device, device -> entities.

    A device slug that isn't unique is dropped (ambiguous) so a stale/duplicate
    device can't decide a receiver's status. That includes the copies HA 2026.8
    made when it split every device shared by several integrations - same
    name, so same slug. Pooling those back into one was tried and rejected: a
    proxy's sibling copy is typically a router's device_tracker, whose state is
    never "unavailable", so a dead proxy read as alive - and the Lovelace card,
    which this tier mirrors, can't see the split at all.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    slug_to_device = {}
    ambiguous = set()
    for d in _iter_registry_devices(dev_reg):
        name = d.name_by_user or d.name
        if not name:
            continue
        slug = slugify(name)
        if not slug:
            continue
        if slug in slug_to_device and slug_to_device[slug] != d.id:
            ambiguous.add(slug)
        else:
            slug_to_device[slug] = d.id
    for slug in ambiguous:
        slug_to_device.pop(slug, None)
    device_entities = {}
    for ent in ent_reg.entities.values():
        if ent.device_id:
            device_entities.setdefault(ent.device_id, []).append(ent.entity_id)
    return slug_to_device, device_entities


async def update_receiver_liveness(hass):
    """Offline requires Bermuda scanner-age or device/connectivity evidence.

    Scanner identity survives a name change when its public MAC is proven by
    Bermuda-owned range entities. Distance readings prove a scanner is working;
    their absence leaves liveness unknown. Never infer offline from silence.
    """
    dom = hass.data.setdefault(DOMAIN, {})
    sources = ReceiverSources(hass)
    try:
        devices = None if not sources.direct.needs_legacy else await wait_for(
            hass.services.async_call(
                "bermuda", "dump_devices", {"configured_devices": True},
                blocking=True, return_response=True,
            ),
            timeout=DUMP_DEVICES_TIMEOUT_S,
        ) or {}
    except TimeoutError:
        _LOGGER.warning(
            "Receiver liveness: bermuda.dump_devices timed out after %ss; "
            "skipping this refresh", DUMP_DEVICES_TIMEOUT_S,
        )
        devices = None
    except Exception as e:
        _LOGGER.info(f"Receiver liveness: dump_devices unavailable: {e}")
        devices = None

    if isinstance(devices, dict):
        _refresh_dump_ages(hass, dom, devices)
    ages = dom.get("rl_ages", {})
    elapsed = max(0.0, time.time() - dom["rl_anchor_wall"]) if dom.get("rl_anchor_wall") else 0.0

    receivers, _ = _scanner_slugs_and_readings(hass)
    matching = {row["entity_id"]: row for row in sources.matching["options"]}
    receivers.update(matching)
    identity_conflicts = set()
    placed_identities = {}
    bindings = {slug: sources.binding({"entity_id": slug}) for slug in matching}
    layout = get_bps_data(hass) or {}
    for floor in layout.get("floor", []):
        if not isinstance(floor, dict):
            continue
        for rec, binding in zip(floor.get("receivers", []), sources.floor_bindings(floor)):
            if not isinstance(rec, dict) or not isinstance(rec.get("entity_id"), str) or not rec["entity_id"]:
                continue
            bindings[rec["entity_id"]] = binding
            placed_identities.setdefault(rec["entity_id"], set()).add(binding["identity"])
            if binding["status"] in ("scanner_identity_conflict", "scanner_identity_unavailable", "ambiguous_scanner_identity", "invalid_scanner_identity"):
                identity_conflicts.add(rec["entity_id"])
                receivers.add(rec["entity_id"])
            if not sources.blocked(binding) and binding["identity"]:
                alias = next((option for name in binding["aliases"]
                              if (option := sources.resolved_option(name, binding)) is not None), None)
                if alias:
                    matching[rec["entity_id"]] = alias
                    receivers.add(rec["entity_id"])
    # Legacy liveness APIs are keyed by slug, not floor/placement. Different
    # saved hardware under one slug has no single authoritative status; never
    # let the last floor mark another scanner online or offline.
    identity_conflicts.update(slug for slug, identities in placed_identities.items() if len(identities) > 1)
    slug_to_device, device_entities = _build_device_availability(hass)

    def device_online(slug):
        device_id = slug_to_device.get(slug)
        if not device_id:
            return None
        eids = device_entities.get(device_id)
        if not eids:
            return None
        # A connectivity entity reports link state directly and is authoritative:
        # ESPHome keeps its status sensor available with state "off" when the
        # proxy disconnects, so "any entity not unavailable" would miss it.
        for eid in eids:
            st = hass.states.get(eid)
            if st and st.attributes.get("device_class") == "connectivity":
                return None if st.state in (None, "", "unknown", "unavailable") else _state_looks_online(st.state)
        saw_state = False
        for eid in eids:
            st = hass.states.get(eid)
            if st is None or st.state is None:
                continue
            saw_state = True
            if st.state != "unavailable":
                return True
        return False if saw_state else None

    def evidence_slugs(slug):
        # Connectivity/device names carry no owner identity themselves. Expand
        # only a proven owner/MAC whose runtime name and MAC owner are unique.
        binding = bindings.get(slug, {})
        option = matching.get(slug, {})
        key = binding.get("identity")
        names = [] if sources.options.get(slug, {}).get("identity_ambiguous") else [slug]
        if key and option.get("liveness_name_unique", True) and len(sources.owners_by_mac.get(key[1], ())) == 1:
            names.extend(alias for alias in binding.get("aliases", [])
                         if not sources.options.get(alias, {}).get("identity_ambiguous"))
            if option.get("name"):
                names.append(slugify(option["name"]))
        return list(dict.fromkeys(names))

    def live_distance(slug):
        binding = bindings.get(slug, {})
        key = binding.get("identity")
        if key in sources.direct.scanners:
            clock, cutoff = time.time(), _reading_max_age(layout)
            for device in sources.direct.snapshots[key[0]]["devices"]:
                state = sources.direct.reading(key[0], device, key[1], "", now=clock)
                if isinstance(state.state, (int, float)) and (not cutoff or _reading_age_secs(state, now=clock) <= cutoff):
                    return True
            return False
        options = [option for name in binding.get("aliases", [])
                   if (option := sources.resolved_option(name, binding)) is not None] if key else [sources.options.get(slug, {})]
        candidates = {eid for option in options for eid in option.get("distance_sensors", [])
                      if eid not in option.get("disabled_sensors", [])}
        now, max_age = time.time(), _reading_max_age(layout)
        for eid in candidates:
            entry = sources.registry.entities.get(eid) if sources.registry is not None else None
            if entry is None or entry.platform != "bermuda" or getattr(entry, "disabled_by", None) is not None:
                continue
            if key and getattr(entry, "config_entry_id", None) != key[0]:
                continue
            state = hass.states.get(eid)
            if state is None:
                continue
            try:
                value = float(state.state)
                stamp = getattr(state, "last_updated", None) or getattr(state, "last_changed", None)
                observed = stamp.timestamp()
                if math.isfinite(value) and value >= 0 and math.isfinite(observed) and observed <= now \
                        and (max_age == 0 or now - observed <= max_age):
                    return True
            except (TypeError, ValueError, AttributeError, OSError, OverflowError):
                continue
        return False

    def is_online(slug):
        if slug in identity_conflicts or sources.blocked(bindings.get(slug, {"status": "unmatched"})):
            return None
        name = matching.get(slug, {}).get("name")
        mac = matching.get(slug, {}).get("scanner_mac")
        identity = bindings.get(slug, {}).get("identity")
        if identity in sources.direct.scanners:
            direct_age = sources.direct.scanner_age(identity)
            if direct_age is not None and direct_age >= 0:
                return direct_age <= RECEIVER_OFFLINE_SECS
        age = (dom.get("rl_mac_ages", {}).get(mac) if len(sources.owners_by_mac.get(mac, ())) <= 1
               and identity not in sources.direct.scanners else None)
        if age is None and not mac and matching.get(slug, {}).get("liveness_name_unique", True):
            # Name-only ages have no hardware proof. A proven MAC must use its
            # own age or current connectivity/readings, even after dump failure.
            age = ages.get(slug, ages.get(slugify(name)) if name else None)
        if age is not None:
            return (age + elapsed) <= RECEIVER_OFFLINE_SECS
        aliases = evidence_slugs(slug)
        for alias in aliases:
            st = hass.states.get(f"binary_sensor.{alias}_status")
            if st and st.attributes.get("device_class") == "connectivity" and st.state not in (None, "", "unknown", "unavailable"):
                return _state_looks_online(st.state)
        for alias in aliases:
            resolved = device_online(alias)
            if resolved is not None:
                return resolved
        # Fresh filtered distances prove online; absence of contact proves nothing.
        return True if live_distance(slug) else None

    resolved = {s: is_online(s) for s in receivers}
    dom["rl_offline"] = sorted(s for s, status in resolved.items() if status is False)
    dom["rl_unknown"] = sorted(s for s, status in resolved.items() if status is None)


async def update_receiver_radii(hass, eids, receiver_sources=None):
    """Update receiver 'r' values (pixels) and raw 'distance' (meters) for an entity"""
    tracker_h = _tracker_height(eids["data"], eids["entity"])
    tracker_factor = _tracker_distance_factor(eids["data"], eids["entity"])
    max_age = _reading_max_age(eids["data"])
    outdoor = outdoor_settings(eids["data"])["enabled"]
    recording = getattr(hass, "data", {}).get(DOMAIN, {}).get("_diagnostics")
    capture = bool(recording and recording.active and eids["entity"] in recording.sources)
    if outdoor:
        max_age = finite_number(max_age, READING_MAX_AGE_SECS, minimum=0.0, maximum=1e6)
    sources = receiver_sources if receiver_sources is not None else ReceiverSources(hass)
    for floor in (f for f in eids["data"]["floor"] if f["scale"] is not None):
        for receiver, binding in zip(floor["receivers"], sources.floor_bindings(floor)):
            receiver.pop("distance", None)
            receiver.get("cords", {}).pop("r", None)
            receiver.pop("_diagnostic_reading", None)
            if outdoor:
                # A saved or previous-cycle radius is never a current reading.
                receiver.pop("distance", None)
                receiver.pop("_outdoor_reading", None)
            entity_id, rec_value = sources.source(eids["entity"], receiver, binding)
            if rec_value is not None:
                # Drop a STUCK reading: when a scanner stops hearing the
                # tracker its distance sensor keeps the last value instead of
                # going unavailable, and that frozen radius would anchor the
                # fix to a receiver that can no longer see the device. Removing
                # "distance" takes this receiver out of the cycle's candidate
                # solve (see extract_candidate_floors).
                reading_time = time.time() if outdoor else None
                age = _reading_age_secs(rec_value, now=reading_time)
                if max_age and age is not None and (age >= max_age if outdoor else age > max_age):
                    receiver.pop("distance", None)
                    _LOGGER.debug(
                        "Ignoring stale distance for %s (%.0fs old, max %.0fs)",
                        entity_id, age, max_age,
                    )
                    continue
                try:
                    distance = float(rec_value.state)
                    # Bermuda's distance_to sensors can report in feet or
                    # meters, chosen per entity. The floor scale and the
                    # calibration corrections are both in meters, so normalize
                    # to meters first — otherwise a feet sensor reads ~3.28x too
                    # far (treated as metres), inflating its circle and pulling
                    # the trilateration toward it.
                    unit = rec_value.attributes.get("unit_of_measurement")
                    if unit in DistanceConverter.VALID_UNITS and unit != UnitOfLength.METERS:
                        distance = DistanceConverter.convert(distance, unit, UnitOfLength.METERS)
                    if not math.isfinite(distance) or distance < 0:
                        continue
                    if outdoor:
                        alias_without_clock = age is None and entity_id != f'sensor.{eids["entity"]}_distance_to_{receiver["entity_id"]}'
                        receiver["_outdoor_reading"] = {
                            "measured_distance_m": distance, "reading_age_s": age,
                            "observed": None if alias_without_clock else reading_time - (age if age is not None else 0.0),
                            "entity_id": entity_id,
                        }
                    # Per-receiver correction factor learned by the
                    # calibration (calibration.py); equivalent to a
                    # per-scanner RSSI offset in Bermuda's exponential model.
                    correction = receiver.get("correction")
                    if isinstance(correction, (int, float)) and correction > 0:
                        distance = distance * correction
                    # Per-TRACKER ref-power trim (issue #92): a tag whose
                    # transmit power differs from Bermuda's configured
                    # ref_power reads consistently long or short from EVERY
                    # receiver, which no per-receiver correction can fix.
                    # Applied before the slant leg so the height geometry sees
                    # the trimmed range, and included in the election distance
                    # below (a per-tracker constant, so cross-floor ordering
                    # for this tracker is unchanged).
                    distance = distance * tracker_factor
                    if not math.isfinite(distance) or distance < 0:
                        continue
                    # Known mount height: the estimate is a slant range, so
                    # remove the vertical leg (mount height vs the assumed
                    # tracker height) to get the horizontal distance the 2D
                    # solve actually needs. A slant shorter than the vertical
                    # leg means "practically underneath" — horizontal ~ 0; the
                    # solver's MIN_WEIGHT_RADIUS_M clamp keeps such a near-zero
                    # radius from monopolizing the fit. The range guard also
                    # rejects NaN/Infinity from a hand-edited data file (NaN
                    # fails both comparisons), which would otherwise poison
                    # every solve on the floor.
                    horizontal = distance
                    height = receiver.get("height")
                    if isinstance(height, (int, float)) and 0 <= height <= 10:
                        dz = float(height) - tracker_h
                        # Floored: sqrt(d^2 - dz^2) has a singularity at
                        # d -> dz where its sensitivity blows up, and any
                        # d <= dz collapsed to EXACTLY 0. Bermuda's filtered
                        # distances are sustainedly biased low, so a filtered
                        # slant could sit below dz for many cycles and the
                        # collapsed radius (clamped to min weight radius at
                        # ~100x the weight of a 5 m receiver) dragged the fix
                        # onto that receiver — the 1.7.0 accuracy regression.
                        # The floor never exceeds the raw slant itself, so a
                        # receiver at ~tracker height (dz ~ 0, no singularity)
                        # keeps honest sub-floor readings like the no-height
                        # path does.
                        floor_sq = min(distance * distance,
                                       MIN_WEIGHT_RADIUS_M * MIN_WEIGHT_RADIUS_M)
                        horizontal = math.sqrt(max(distance * distance - dz * dz, floor_sq))
                    radius = floor["scale"] * horizontal
                    if not math.isfinite(radius) or radius < 0:
                        continue
                    receiver["cords"]["r"] = radius
                    # Raw SLANT distance for the floor election: radii are in
                    # per-floor pixel scales and must not be compared across
                    # floors — and the dz correction must not leak in here
                    # either. sqrt(d^2 - dz^2) is only valid when the tracker
                    # is on the receiver's own floor, which is exactly what
                    # the election hasn't decided yet: electing on corrected
                    # values lets a high-mounted probe hearing the tracker
                    # through the slab shrink its through-floor slant and
                    # steal the election from the correct floor.
                    receiver["distance"] = distance
                    if capture:
                        ts = getattr(rec_value, "last_updated", None) or getattr(rec_value, "last_changed", None)
                        try:
                            observed = ts.timestamp()
                        except (AttributeError, ValueError, TypeError, OSError, OverflowError):
                            observed = None
                        receiver["_diagnostic_reading"] = {
                            "observed": observed, "entity_id": entity_id, "corrected_distance_m": distance,
                            "horizontal_distance_m": horizontal, "measurement_source": sources.source_info(binding, rec_value),
                        }
                except ValueError:
                    #_LOGGER.info(f"Invalid numerical value: {rec_value.state}")
                    pass
            else:
                #_LOGGER.info(f"Entity had no value: {receiver['entity_id']}")
                pass

async def update_trilateration_and_zone(hass, new_global_data, entity):
    """Trilateration with floor hypothesis competition, soft radius-jump
    weighting and Kalman position smoothing.

    The floor used to be elected by the single nearest receiver before any
    position existed — one noisy reading through a ceiling could steal the
    tracker for a cycle (issue #94). Now the top FLOOR_CANDIDATES floors (by
    nearest receiver) are each SOLVED, scored by how well the fix explains
    that floor's whole receiver ensemble, folded into smoothed per-floor
    probabilities, and elected with incumbent hysteresis. The winning floor's
    fix continues into the unchanged Kalman/zone/publish pipeline.
    """
    global apitricords

    context = _capture_tracking_context(hass)
    if not _tracking_context_current(context):
        return

    # Store last r-values per sensor and entity (for soft radius-jump weighting).
    if not hasattr(update_trilateration_and_zone, "last_r_values"):
        update_trilateration_and_zone.last_r_values = {}
    if not hasattr(update_trilateration_and_zone, "last_floor"):
        update_trilateration_and_zone.last_floor = {}

    candidates = extract_candidate_floors(new_global_data, entity)
    entity_layout = next((e["data"] for e in new_global_data if e["entity"] == entity), {})
    outdoor = outdoor_settings(entity_layout)["enabled"]

    if not candidates:
        # No receiver reports any distance for this device: it is out of
        # range. The zone/floor sensors keep their last value (historical
        # behavior), but nearest-zone explicitly reports unknown.
        update_bps_sensor_state(hass, f"sensor.{entity}_bps_nearest_zone", "unknown")
        return

    # Get previous r-values for this entity
    last_r = update_trilateration_and_zone.last_r_values.get(entity, {})

    # Remember radii for EVERY floor with data — not only the ones solved
    # below — so a floor stays jump-gated even while unelected or briefly
    # pushed out of the candidate cut (keys include the floor: radii are in
    # per-floor pixel scales and must not be compared across floors).
    new_last_r = {}
    for cand in candidates:
        new_last_r.update({(cand["name"], x, y): r for (x, y, r, _srad) in cand["cords"]})

    incumbent = update_trilateration_and_zone.last_floor.get(entity)

    # Only floors with enough receivers to trilaterate compete for the solve
    # slots: an unsolvable floor whose single through-slab receiver reads
    # short must not consume a slot and push the only solvable floor out.
    # The incumbent, when solvable, ALWAYS defends its title — even ranked
    # below the cut — so nearest-slant noise alone can never evict it.
    solvable = [c for c in candidates if len(c["cords"]) >= 3]
    to_solve = solvable[:FLOOR_CANDIDATES]
    if incumbent is not None and not any(c["name"] == incumbent for c in to_solve):
        inc_cand = next((c for c in solvable if c["name"] == incumbent), None)
        if inc_cand is not None:
            to_solve.append(inc_cand)

    solved = {}  # floor name -> everything the publish pipeline needs
    for cand in to_solve:
        floor_name, cords = cand["name"], cand["cords"]

        # Physical floor for the geometric weight and the jump comparison, in
        # this floor's pixels. Slant correction (known mount heights) makes
        # near-zero radii a normal reading, and 1/r^2 must not hand one such
        # receiver the whole fit.
        scale = _floor_scale(new_global_data, entity, floor_name)
        min_wr = MIN_WEIGHT_RADIUS_M * scale if scale else 1e-3

        # Soft radius-jump weighting (replaces the old hard 50% discard). A
        # receiver whose radius jumped versus its previous update is
        # DOWN-WEIGHTED rather than dropped, so the solver keeps enough points
        # to fix a position even while every distance is legitimately changing
        # during movement. The measured slant (px) rides along as each point's
        # weight radius, so the solver's 1/r^2 weight reflects what was
        # MEASURED — a projection collapsed to the minimum can't buy influence.
        weighted = []
        for (x, y, r, slant_px) in cords:
            w = _jump_weight(r, last_r.get((floor_name, x, y)), min_wr)
            weighted.append((x, y, r, w, slant_px))

        # Indoor fixes remain bounded by the floor. Outdoor fixes can be
        # beyond the map: extend the bounded search by the observed ranges.
        zone_polys = list(_floor_zone_polygons(new_global_data, entity, floor_name))
        xs = [p[0] for p in weighted]
        ys = [p[1] for p in weighted]
        for _zone_id, polygon, _buffer_size, _no_go in zone_polys:
            minx, miny, maxx, maxy = polygon.bounds
            xs.extend((minx, maxx))
            ys.extend((miny, maxy))
        margin = 0.1 * max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
        if outdoor and scale:
            margin = max(margin, min(max(pt[2] for pt in weighted), 1000 * scale))
        floor_bounds = (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)

        outdoor_result = None
        if outdoor:
            floor = next(f for f in entity_layout["floor"] if f["name"] == floor_name)
            prior = next((p.get("raw") for p in apitricords
                          if p["ent"] == entity and p["floor"] == floor_name), None)
            # Additional geometry and bounded re-solving belong off HA's loop.
            outdoor_result = await _tracking_executor_job(hass, partial(
                solve_outdoor, floor, weighted, floor_bounds, scale, min_wr,
                _reading_max_age(entity_layout), prior, trilaterate,
                position_timeout_s=entity_layout.get("position_timeout", STALE_POSITION_SECS),
                nearby_trust=True,
                obstruction_strength=outdoor_settings(entity_layout)["obstruction_strength"],
            ))
            # Map saves replace the cached layout. Do not feed an obsolete
            # snapshot into election, smoothing, history or sensor state.
            if not _tracking_context_current(context):
                return
            if outdoor_result is None:
                continue
            fix, weighted = outdoor_result["fix"], outdoor_result["weighted"]
        else:
            fix = trilaterate(weighted, bounds=floor_bounds, min_weight_radius=min_wr)
        if fix is None:
            continue  # this floor's readings don't converge; not a contender
        election_weighted = outdoor_result["floor_election_weighted"] if outdoor_result else weighted
        conf, rms_m, coverage = _score_floor_fit(fix, election_weighted, scale)
        # A fit landing in this floor's no-go zone is physically impossible
        # here (issue #60): down-weight it so the competition prefers the
        # floor where that spot is a real room. Down-weight, not eliminate —
        # a sole candidate still wins and is snapped out below.
        if _point_in_no_go(fix, zone_polys):
            conf *= NO_GO_CONF_PENALTY
        solved[floor_name] = {
            "fix": fix,
            "weighted": weighted,
            "bounds": floor_bounds,
            "zone_polys": zone_polys,
            "scale": scale,
            "conf": conf,
            "rms_m": rms_m,     # kept for the elected floor's telemetry payload
            "outdoor": outdoor_result["outdoor"] if outdoor_result else None,
        }

    # Store current r-values for next time
    update_trilateration_and_zone.last_r_values[entity] = new_last_r

    if outdoor and not solved:
        previous_payload = next((p for p in apitricords if p["ent"] == entity), None)
        for cand in candidates:
            floor_name = cand["name"]
            scale = _floor_scale(new_global_data, entity, floor_name)
            floor = next(f for f in entity_layout["floor"] if f["name"] == floor_name)
            min_wr = MIN_WEIGHT_RADIUS_M * scale if scale else 1e-3
            weighted = [(x, y, radius, _jump_weight(radius, last_r.get((floor_name, x, y)), min_wr), slant)
                        for x, y, radius, slant in cand["cords"]]
            detection = solve_sparse_detection(floor, weighted, scale, _reading_max_age(entity_layout),
                previous_payload, now=time.time(), position_timeout_s=entity_layout.get("position_timeout", STALE_POSITION_SECS),
                nearby_trust=True,
                obstruction_strength=outdoor_settings(entity_layout)["obstruction_strength"])
            if detection is not None:
                quality = detection["outdoor"]
                # Election compares reception floors only; this is not a
                # directional fix and never claims a room/zone membership.
                solved[floor_name] = {"fix": detection["fix"], "weighted": detection["weighted"],
                    "bounds": None, "zone_polys": [], "scale": scale,
                    "conf": .1 / (1 + quality["floor_election_range_m"]), "rms_m": None, "outdoor": quality}

    if not solved:
        # No candidate floor has three converging receivers (historical
        # behavior: sensors keep their last value until pruned).
        return

    valid_floors = {
        f["name"]
        for e in new_global_data if e["entity"] == entity
        for f in e["data"]["floor"]
    }

    # The elected floor was renamed or deleted in the data file: that is a
    # change of world, not a dark blip — routing it into the grace below
    # would freeze the ghost name for the grace period and then elect
    # whichever OTHER floor inherited the stale probability mass. Reset the
    # whole election state instead (exactly like a prune), so this cycle's
    # fresh scores elect the renamed floor immediately.
    if incumbent is not None and incumbent not in valid_floors:
        _floor_probability.pop(entity, None)
        _floor_challenge.pop(entity, None)
        _floor_dark_cycles.pop(entity, None)
        update_trilateration_and_zone.last_floor.pop(entity, None)
        incumbent = None

    # Dark-incumbent grace: the elected floor blipping below three receivers
    # (or its solve failing) for a few cycles is a sensor hiccup, not
    # evidence the tracker moved — hold everything frozen: last published
    # values stand, probabilities are NOT updated (a competitor must not
    # accumulate election lead from the incumbent's blind cycles), and no
    # other floor inherits incumbency by forfeit. Only a disappearance
    # longer than the grace lapses the incumbency, and then the best solved
    # floor is adopted on its merits.
    if incumbent is not None and incumbent not in solved \
            and incumbent in _floor_probability.get(entity, {}):
        dark = _floor_dark_cycles.get(entity, 0) + 1
        if dark <= FLOOR_DARK_GRACE_CYCLES:
            _floor_dark_cycles[entity] = dark
            return
        incumbent = None  # dark beyond grace: incumbency lapses
    _floor_dark_cycles.pop(entity, None)
    probs = _update_floor_probabilities(
        entity, {f: s["conf"] for f, s in solved.items()}, valid_floors
    )
    lowest_floor_name, challenge = _elect_floor(
        probs, incumbent, solved, _floor_challenge.get(entity)
    )
    if challenge is None:
        _floor_challenge.pop(entity, None)
    else:
        _floor_challenge[entity] = challenge
    if lowest_floor_name is None:
        return  # nothing electable this cycle: keep last values

    if update_trilateration_and_zone.last_floor.get(entity) != lowest_floor_name:
        # The Kalman state holds pixel coordinates in the previously elected
        # floor's map space; it must not carry over to the newly elected floor.
        _kf_position_state.pop(entity, None)
        update_trilateration_and_zone.last_floor[entity] = lowest_floor_name

    elected = solved[lowest_floor_name]
    weighted = elected["weighted"]
    zone_polys = elected["zone_polys"]
    floor_bounds = elected["bounds"]
    scale = elected["scale"]
    tricords = elected["fix"]

    if tricords is not None:
        # Constant-velocity Kalman smoothing of the published position. The RAW
        # trilaterated fix feeds the filter (so the estimate is never biased by
        # the zone snapping applied below); the filtered output is clipped to the
        # floor bounds inside the helper.
        detection_only = bool(elected.get("outdoor", {}).get("detection_only")) if elected.get("outdoor") else False
        if detection_only:
            _kf_position_state.pop(entity, None)
            avg_x, avg_y = tricords
        else:
            avg_x, avg_y = _kalman_position_update(
                entity, lowest_floor_name, tricords, scale, floor_bounds
            )

        # A fix outside every zone is physically implausible (BLE noise pushed
        # it into a wall or off the apartment): publish the nearest point on
        # the zone union instead. Snapping is applied to the filter OUTPUT only;
        # the filter state keeps the raw fix, so smoothing is not biased toward
        # the boundary.
        test_point = Point(float(avg_x), float(avg_y))
        snapped = snap_point_into_zones(zone_polys, test_point) if not outdoor else None
        if snapped is not None:
            test_point = snapped
            avg_x, avg_y = float(snapped.x), float(snapped.y)
        if detection_only:
            zone = nearest_zone = sub_zone = sub_parent = "unknown"
        else:
            zone = find_zone_for_point(new_global_data, entity, lowest_floor_name, test_point)
            nearest_zone = find_nearest_zone(new_global_data, entity, lowest_floor_name, test_point)
            sub_zone, sub_parent = find_sub_zone_for_point(new_global_data, entity, lowest_floor_name, test_point)
        # parent_zone always names the enclosing main zone: the sub-zone's
        # declared parent when inside one, otherwise the current main zone.
        parent_zone = sub_parent if sub_parent else zone
        extra = {}
        recording = hass.data.get(DOMAIN, {}).get("_diagnostics")
        if recording and recording.active and entity in recording.sources:
            selected_floor = next(f for f in entity_layout["floor"] if f["name"] == lowest_floor_name)
            selected_receivers = [r for r in selected_floor["receivers"]
                if r.get("distance") is not None and "r" in r.get("cords", {})
                and not (outdoor and r.get("outdoor_policy") == "ignore")]
            extra["diagnostic_inputs"] = [{"receiver": r.get("entity_id"),
                "observed": r.get("_diagnostic_reading", {}).get("observed"),
                "entity_id": r.get("_diagnostic_reading", {}).get("entity_id"),
                "corrected_distance_m": r.get("_diagnostic_reading", {}).get("corrected_distance_m"),
                "horizontal_distance_m": r.get("_diagnostic_reading", {}).get("horizontal_distance_m"),
                "measurement_source": r.get("_diagnostic_reading", {}).get("measurement_source"),
                "radius_px": pt[2], "jump_environment_weight": pt[3]}
                for r, pt in zip(selected_receivers, weighted)]
        if outdoor and elected["outdoor"] is not None:
            quality = elected["outdoor"]
            account_for_published_position(quality, tricords, (avg_x, avg_y), scale)
            source_floor = next(f for f in entity_layout["floor"] if f["name"] == lowest_floor_name)
            quality.update(reporting_radius_limit(source_floor, (avg_x, avg_y),
                quality.get("receiver_diagnostics"), time.time(), _reading_max_age(entity_layout)))
            # Captured with the measurements before executor queuing/solving.
            # Never reconstruct it using a later publication clock.
            if quality.get("observed") is None:
                quality["observed"] = time.time()
            extra["outdoor"] = quality
        apitricords = update_or_add_entry(
            apitricords,
            {
                "ent": entity,
                "cords": [avg_x, avg_y],
                "zone": zone,
                "floor": lowest_floor_name,
                # The exact solver input (post-correction, post-filter), for
                # the panel's trilateration circles.
                "radii": [[float(pt[0]), float(pt[1]), float(pt[2])] for pt in weighted],
                # Smoothed floor-election probabilities, for debugging "why
                # did it pick this floor" (issue #94).
                "floors": {f: round(p, 3) for f, p in probs.items()},
                # Positioning telemetry for the eval harness (tools/bps_eval.py)
                # and the debug tab: the pre-Kalman, pre-snap trilaterated fix
                # next to the published (filtered + snapped) `cords`, so solver
                # bias can be told apart from filter lag; plus this fix's
                # weighted RMS residual (m) and the elected floor's confidence.
                # Both frontends read named fields, so these keys are inert.
                "raw": [round(float(tricords[0]), 2), round(float(tricords[1]), 2)],
                "rms_m": round(elected["rms_m"], 3) if elected["rms_m"] is not None else None,
                "conf": 0 if detection_only else round(elected["conf"], 3),
                "updated": elected["outdoor"]["observed"] if detection_only else time.time(),
                **extra,
            },
        )
        await update_apitricords(hass, apitricords)
        # Feed the position history. Stored in METRES in this floor's frame, so
        # a later map re-export (which changes every pixel) cannot move the
        # past; the pixel projection happens at render time. A floor with no
        # scale has no metric frame, so its fixes are not recordable.
        if scale:
            try:
                if detection_only:
                    get_position_history(hass).mark_gap(entity)
                else:
                    get_position_history(hass).record(
                        entity, time.time(), avg_x / scale, avg_y / scale,
                        lowest_floor_name, scale, zone)
            except Exception as e:  # history must never break tracking
                _LOGGER.debug("Position history record failed for %s: %s", entity, e)
        update_bps_sensor_state(hass, f"sensor.{entity}_bps_zone", zone)
        update_bps_sensor_state(hass, f"sensor.{entity}_bps_nearest_zone", nearest_zone)
        update_bps_sensor_state(hass, f"sensor.{entity}_bps_floor", lowest_floor_name)
        update_bps_sensor_state(hass, f"sensor.{entity}_bps_sub_zone", sub_zone, {"parent_zone": parent_zone})

def update_or_add_entry(data, new_entry):
    for index, item in enumerate(data):
        if item["ent"] == new_entry["ent"]:  # Check if "ent" already exists
            if item.get("group") and not new_entry.get("group"):
                # A discovered real beacon takes ownership of this map key.
                # Replace all fused metadata so reconciliation keeps its fix.
                data[index] = new_entry
                return data
            item["cords"] = new_entry["cords"]  # Update "cords"
            item["zone"] = new_entry["zone"]  # Update "zone"
            item["floor"] = new_entry["floor"]  # Floor the fix belongs to
            item["radii"] = new_entry["radii"]  # Solver input, for circles
            item["floors"] = new_entry["floors"]  # Election probabilities
            item["raw"] = new_entry["raw"]  # Pre-Kalman fix (telemetry)
            item["rms_m"] = new_entry["rms_m"]  # Fit residual, m (telemetry)
            item["conf"] = new_entry["conf"]  # Elected floor confidence
            item["updated"] = new_entry["updated"]  # Freshness for pruning
            # Remove opt-in telemetry as soon as the feature is disabled.
            item.pop("outdoor", None)
            if "outdoor" in new_entry:
                item["outdoor"] = new_entry["outdoor"]
            item.pop("diagnostic_inputs", None)
            if "diagnostic_inputs" in new_entry:
                item["diagnostic_inputs"] = new_entry["diagnostic_inputs"]
            return data

    # If "ent" was not found, add as new post
    data.append(new_entry)
    return data


async def prune_stale_positions(hass):
    """Drop trackers not detected by any receiver for the timeout period.

    Without this, a person who left home stayed on the map at their last
    position forever, and the zone/floor sensors kept the stale values.
    """
    global apitricords
    timeout = STALE_POSITION_SECS
    layout = get_bps_data(hass)
    if isinstance(layout, dict):
        configured = layout.get("position_timeout")
        if isinstance(configured, (int, float)) and configured > 0:
            timeout = configured

    now = time.time()
    # Fused groups retain source observation time, which can be older than
    # this timeout while still valid under their configured freshness gate.
    # Group fusion owns their expiry and marks the history gap once.
    stale_ents = {e["ent"] for e in apitricords
                  if not e.get("group") and now - e.get("updated", now) > timeout}
    if not stale_ents:
        return
    apitricords = [e for e in apitricords if e["ent"] not in stale_ents]
    await update_apitricords(hass, apitricords)
    # History is deliberately NOT pruned with the live entry: the point of it is
    # to survive the absence. Just break the line so the scrubber does not draw
    # a straight segment across the gap.
    try:
        hist = get_position_history(hass)
        for ent in stale_ents:
            hist.mark_gap(ent)
    except Exception as e:
        _LOGGER.debug("Position history gap mark failed: %s", e)
    for ent in sorted(stale_ents):
        # Drop the Kalman state too: a returning tracker should re-seed fresh
        # rather than predict velocity across the whole absence. Same for the
        # whole election state — probabilities, pending challenge, and the
        # INCUMBENCY itself: it may well come back on another floor, and an
        # hours-stale incumbent must not enjoy hysteresis against it (with
        # freshly-reset probabilities the margin could pin the wrong floor
        # indefinitely). Jump-gate radii from before the absence are equally
        # meaningless.
        _kf_position_state.pop(ent, None)
        _floor_probability.pop(ent, None)
        _floor_challenge.pop(ent, None)
        _floor_dark_cycles.pop(ent, None)
        getattr(update_trilateration_and_zone, "last_floor", {}).pop(ent, None)
        getattr(update_trilateration_and_zone, "last_r_values", {}).pop(ent, None)
        _LOGGER.info("Tracker %s not seen for %ss; clearing its position", ent, timeout)
        update_bps_sensor_state(hass, f"sensor.{ent}_bps_zone", "unknown")
        update_bps_sensor_state(hass, f"sensor.{ent}_bps_floor", "unknown")
        update_bps_sensor_state(hass, f"sensor.{ent}_bps_nearest_zone", "unknown")
        update_bps_sensor_state(hass, f"sensor.{ent}_bps_sub_zone", "unknown", {"parent_zone": "unknown"})

async def update_apitricords(hass, new_data):
    """Update apitricords in hass.data"""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["apitricords"] = new_data


async def update_tracker_groups(hass):
    """Fuse complete per-beacon fixes after a cycle, preserving every original.

    Never use a fused group as a beacon or refresh a source's age. Group state
    disappears promptly on disable/delete or when all beacon fixes become stale.
    """
    global apitricords
    context = _capture_tracking_context(hass)
    if not _tracking_context_current(context):
        return
    layout = get_bps_data(hass)
    dom = hass.data.setdefault(DOMAIN, {})
    if not outdoor_settings(layout)["enabled"] and not dom.get("groups_active"):
        # The common legacy path does no geometry, fusion or registry scans.
        sync = dom.get("sync_group_sensors")
        if sync is not None:
            await sync([])
        return
    originals = [p for p in apitricords if not p.get("group")]
    known = {p["ent"] for p in originals}
    known.update(eid.removeprefix("sensor.").split("_distance_to_", 1)[0]
                 for eid in tracked_entities)
    known.update(_direct_tracker_keys(hass))
    groups = normalize_groups(layout, known_trackers=known)
    sync = hass.data.get(DOMAIN, {}).get("sync_group_sensors")
    if sync is not None:
        await sync(groups, known_trackers=known)
        if not _tracking_context_current(context):
            return
    dom["groups_active"] = bool(groups) or len(originals) != len(apitricords)
    if not groups and len(originals) == len(apitricords):
        dom.pop("group_stability", None)
        return
    scales = {f["name"]: f.get("scale") for f in layout.get("floor", [])} if isinstance(layout, dict) else {}
    safe_layout = {**layout, "tracker_groups": groups} if isinstance(layout, dict) else {}
    group_max_age = finite_number(_reading_max_age(layout), READING_MAX_AGE_SECS,
                                  minimum=0.0, maximum=1e6)
    use_observation_age = group_max_age > 0
    if not use_observation_age:
        # Disabling the measurement-age gate also applies to group sources.
        # Expire by the last source solve, using the normal position grace.
        group_max_age = finite_number(layout.get("position_timeout"), STALE_POSITION_SECS,
                                      minimum=0.0) or STALE_POSITION_SECS
    fusion_time = time.time()
    previous = {p["ent"]: p for p in apitricords if p.get("group")}
    fused = fuse_groups(safe_layout, originals, scales, fusion_time, max_age_s=group_max_age,
                        use_observation_age=use_observation_age, known_trackers=known,
                        previous=previous, stability=dom.setdefault("group_stability", {}))
    source_positions = {p["ent"]: p for p in originals}
    for position in fused:
        floor = next(f for f in layout["floor"] if f["name"] == position["floor"])
        diagnostics = [row for member in position["beacon_positions"] if member["used"]
                       for row in (source_positions[member["ent"]].get("outdoor") or {}).get("receiver_diagnostics", [])]
        position["outdoor"].update(reporting_radius_limit(floor, position["cords"], diagnostics,
                                                        fusion_time, group_max_age))
    # Geometry workers mutate only their unpublished fix and read detached
    # Python layout data, never the live position/cache objects.
    group_layout = copy.deepcopy(layout)
    lookup = [{"entity": p["ent"], "data": group_layout} for p in fused]
    for position in fused:
        await _tracking_executor_job(hass, _assign_group_zone, position, lookup, scales[position["floor"]])
        if not _tracking_context_current(context):
            return
        position["last_update_age_s"] = max(0.0, time.time() - position["updated"])
        source_max_age = group_max_age
        if position["outdoor"].get("detection_only"):
            source_max_age = min(source_max_age, *(finite_number(
                (source_positions[member["ent"]].get("outdoor") or {}).get("stale_after_s"),
                30.0, minimum=0.0) or 30.0
                for member in position["beacon_positions"] if member["used"]))
        position["outdoor"].update(stale_after_s=source_max_age,
                                   position_age_s=position["last_update_age_s"],
                                   observed=position["updated"], stale=False)
        # Entity attributes keep source timestamps rather than ticking ages.
        # Live ages remain in the API payload for the panel/card diagnostics.
        attrs = {k: v for k, v in position.items() if k not in {"ent", "zone", "last_update_age_s"}}
        attrs.update(tracker_key=position["ent"], zone=position["zone"])
        attrs["outdoor"] = {k: v for k, v in position["outdoor"].items() if k != "position_age_s"}
        attrs["beacon_positions"] = [{k: v for k, v in beacon.items()
                                     if k not in ("age_s", "weight", "normalized_weight")}
                                     for beacon in position["beacon_positions"]]
        update_bps_sensor_state(hass, f"sensor.{position['ent']}_bps_position", "tracking", attrs,
                                only_changed=True)
        update_bps_sensor_state(hass, f"sensor.{position['ent']}_bps_zone", position["zone"], attrs,
                                only_changed=True)
        update_bps_sensor_state(hass, f"sensor.{position['ent']}_bps_floor", position["floor"], attrs,
                                only_changed=True)
        # Freshness can change the fused point without another observation.
        # History dates the published result; API freshness keeps source time.
        # Its existing movement/interval/heartbeat gates bound repeated rows.
        try:
            if position["outdoor"].get("detection_only"):
                get_position_history(hass).mark_gap(position["ent"])
            else:
                get_position_history(hass).record(
                    position["ent"], fusion_time,
                    position["cords"][0] / scales[position["floor"]],
                    position["cords"][1] / scales[position["floor"]],
                    position["floor"], scales[position["floor"]], position["zone"],
                )
        except Exception as error:
            _LOGGER.debug("Group history record failed for %s: %s", position["ent"], error)
    live = {p["ent"] for p in fused}
    for group in groups:
        ent = "bps_group_" + group["id"]
        if ent not in live:
            update_bps_sensor_state(hass, f"sensor.{ent}_bps_position", "unknown",
                                    {"group": True, "tracker_key": ent, "name": group["name"],
                                     "beacons_reporting": 0, "total_beacons": len(group["beacons"])},
                                    only_changed=True)
            for kind in ("zone", "floor"):
                update_bps_sensor_state(hass, f"sensor.{ent}_bps_{kind}", "unknown", {}, only_changed=True)
    for ent in previous.keys() - live:
        get_position_history(hass).mark_gap(ent)
    apitricords = originals + fused
    dom["groups_active"] = bool(groups) or bool(fused)
    await update_apitricords(hass, apitricords)


def _assign_group_zone(position, lookup, scale):
    """Classify the group point without snapping outdoor estimates into zones."""
    if position.get("outdoor", {}).get("detection_only"):
        position.update(zone="unknown", nearest_zone="unknown", sub_zone="unknown", parent_zone="unknown")
        return
    point = Point(*position["cords"])
    position["zone"] = find_zone_for_point(lookup, position["ent"], position["floor"], point)
    position["nearest_zone"] = find_nearest_zone(lookup, position["ent"], position["floor"], point)


def update_bps_sensor_state(hass, entity_id, state, attributes=None, *, only_changed=False):
    """Update state (and optional extra attributes) on a registered BPS SensorEntity."""
    sensors_cache = hass.data.get("bps_sensors")
    if not sensors_cache:
        return
    sensor = sensors_cache.get(entity_id)
    if sensor is None:
        return
    if only_changed and sensor._state == state and (attributes is None or getattr(sensor, "_attrs", None) == attributes):
        return
    sensor._state = state
    if attributes is not None:
        sensor._attrs = attributes
    if getattr(sensor, "_bps_pending", False) or getattr(sensor, "_bps_group_pending", False):
        # AddEntitiesCallback schedules registration. Keep the latest state for
        # HA's initial write, without writing before the entity has a platform.
        return
    sensor.async_write_ha_state()

async def process_single_entity(hass, new_global_data, eids, receiver_sources=None):
    """Process a single entity: first receivers, then trilateration"""
    await update_receiver_radii(hass, eids, receiver_sources)  # Wait for the receivers to update
    await update_trilateration_and_zone(hass, new_global_data, eids["entity"])  # When it is complete → perform trilateration

async def process_entities(hass, new_global_data, receiver_sources=None):
    """Finish every tracker before another refresh or group publication starts."""
    bucket = hass.data.setdefault(DOMAIN, {})
    lock = bucket.setdefault("_tracking_batch_lock", asyncio.Lock())
    context = _capture_tracking_context(hass)
    async with lock:
        if not _tracking_context_current(context):
            return
        receiver_sources = receiver_sources if receiver_sources is not None else ReceiverSources(hass)
        token = _tracking_context.set(context)
        sources_token = _receiver_sources_context.set(receiver_sources)
        slots = asyncio.Semaphore(MAX_CONCURRENT_TRACKERS)

        async def process(eids):
            async with slots:
                if _tracking_context_current(context):
                    await process_single_entity(hass, new_global_data, eids, receiver_sources)

        try:
            # gather's default raises before surviving siblings finish, letting
            # the next cycle overlap their same-entity solves and publications.
            results = await asyncio.gather(*(process(eids) for eids in new_global_data),
                                           return_exceptions=True)
            failures = [(eids["entity"], result) for eids, result in zip(new_global_data, results)
                        if isinstance(result, Exception)]
            if failures:
                entity, error = failures[0]
                summary = (len(failures), str(entity)[:128], str(error)[:240])
                now = time.monotonic()
                previous = bucket.get("_tracking_last_failure")
                if previous is None or previous[1] != summary or now - previous[0] >= 60:
                    _LOGGER.warning("BPS tracking failed for %d tracker(s); first %s: %s", *summary)
                    bucket["_tracking_last_failure"] = (now, summary)
            else:
                bucket.pop("_tracking_last_failure", None)
            if _tracking_context_current(context):
                await update_tracker_groups(hass)
                await _capture_recent_tracking(hass)
        finally:
            _receiver_sources_context.reset(sources_token)
            _tracking_context.reset(token)

def extract_candidate_floors(new_global_data, tmpentity):
    """Every floor hearing the tracker, ranked by its nearest receiver.

    Returns a list of {"name", "cords": [(x, y, r, slant_px), ...],
    "nearest_m"} sorted by nearest_m — slant_px is the measured (corrected)
    slant distance in this floor's pixels, carried alongside the projected
    radius r so the solver can weight by what was MEASURED rather than by the
    projection (whose height-corrected value can legitimately collapse to the
    minimum). The ranking compares raw slant distances (meters),
    not radii: radii are scaled into each floor's own pixel space, so
    comparing them across floors would let the floor with the smallest scale
    win regardless of where the tracker actually is. Ties (the same receiver
    placed on several floors) keep the data file's floor order. Each floor's
    cords feed that floor's own candidate solve — cords from different floors
    live in different pixel coordinate systems and never mix.
    """
    candidates = []
    for entity in new_global_data:
        if entity["entity"] != tmpentity:
            continue
        outdoor = outdoor_settings(entity["data"])["enabled"]
        for floor in entity["data"]["floor"]:
            if outdoor and finite_number(floor.get("scale"), minimum=1e-6, maximum=1e6) is None:
                continue
            nearest, cords = float("inf"), []
            for receiver in floor["receivers"]:
                if outdoor and receiver.get("outdoor_policy") == "ignore":
                    continue
                distance = receiver.get("distance")
                if distance is None or "r" not in receiver.get("cords", {}):
                    continue
                nearest = min(nearest, distance)
                cords.append((
                    receiver["cords"]["x"], receiver["cords"]["y"],
                    receiver["cords"]["r"], distance * floor["scale"],
                ))
            if cords:
                candidates.append({
                    "name": floor["name"],
                    "cords": cords,
                    "nearest_m": nearest,
                })
    candidates.sort(key=lambda c: c["nearest_m"])  # stable: file order breaks ties
    return candidates


def _score_floor_fit(fix, weighted, scale):
    """Score one candidate floor's solve. Returns (confidence, rms_m, coverage).

    Confidence blends how well the fix explains ALL of the floor's reporting
    receivers (weighted RMS residual, converted to metres so floors with
    different pixel scales compare fairly) with how many receivers corroborate
    it. Modelled on ESPresense's scenario confidence (fit quality + node
    coverage): the tracker's true floor tends to explain its whole receiver
    ensemble, while a wrong floor fits one loud through-slab reading and
    contradicts the rest.
    """
    x, y = fix
    n = len(weighted)
    num = den = 0.0
    for pt in weighted:  # (x, y, r, w[, slant_px]) — indexed so both shapes work
        xi, yi, ri, wi = pt[0], pt[1], pt[2], pt[3]
        res = math.hypot(xi - x, yi - y) - ri
        num += wi * res * res
        den += wi
    rms_px = math.sqrt(num / den) if den > 0 else float("inf")
    # Reduced-chi-square-style correction: a 3-receiver floor fits the 2
    # position unknowns almost perfectly no matter what (a single residual
    # degree of freedom), which flatters exactly the floors with the least
    # evidence. Inflate by sqrt(n / (n - 2)) so fits compete on evidence.
    rms_px *= math.sqrt(n / max(n - 2.0, 1.0))
    rms_m = rms_px / scale if scale else rms_px
    quality = 1.0 / (1.0 + (rms_m / FLOOR_RESIDUAL_SCALE_M) ** 2)
    # Corroboration: how many receivers hear the tracker on this floor,
    # saturating at COVERAGE_TARGET_N. An absolute count, NOT a share of the
    # floor's placed receivers: a dead or unmatched placement must not
    # handicap its floor forever, and a tiny fully-reporting 3-receiver floor
    # must not out-cover a floor with 6 of 8 receivers reporting.
    coverage = min(1.0, n / COVERAGE_TARGET_N)
    return 0.5 * coverage + 0.5 * quality, rms_m, coverage


def _update_floor_probabilities(entity, scores, valid_floors=None):
    """Fold this cycle's per-floor confidences into smoothed probabilities.

    Each floor's probability moves 1-FLOOR_PROB_SMOOTHING of the way toward
    its share of this cycle's total confidence; floors with no data this
    cycle decay toward zero and are dropped once negligible, so the dict
    cannot grow unbounded. Floors no longer present in the data file
    (renamed/deleted) are dropped immediately — a ghost name must not hold
    probability mass nor appear in the published election. Returns a
    normalized copy.
    """
    probs = _floor_probability.setdefault(entity, {})
    if valid_floors is not None:
        for floor in [f for f in probs if f not in valid_floors]:
            del probs[floor]
    total = sum(scores.values())
    for floor in set(probs) | set(scores):
        target = (scores.get(floor, 0.0) / total) if total > 0 else 0.0
        probs[floor] = FLOOR_PROB_SMOOTHING * probs.get(floor, 0.0) \
            + (1.0 - FLOOR_PROB_SMOOTHING) * target
    for floor in [f for f, p in probs.items() if p < 0.01]:
        del probs[floor]
    norm = sum(probs.values())
    if norm > 0:
        for floor in probs:
            probs[floor] /= norm
    return dict(probs)


def _elect_floor(probs, incumbent, solved, challenge):
    """Pick the floor to publish. Returns (floor, challenge_state).

    The caller guarantees the incumbent is either solved this cycle or None
    (a dark incumbent is handled by the grace hold upstream, and one dark
    beyond the grace loses its standing entirely — incumbency is never
    transferred by forfeit, it lapses).

    A solved incumbent only loses to a challenger that leads its probability
    by FLOOR_SWITCH_MARGIN for FLOOR_SWITCH_CYCLES consecutive cycles
    (challenge_state carries the count between cycles). The small margin
    filters share noise, the dwell filters single-cycle geometry flukes, and
    together they cannot permanently dead-band a genuinely better floor the
    way a large margin alone would — floor flapping was the disease
    (issue #94), a stuck wrong floor must not be the cure.
    """
    contenders = {f: p for f, p in probs.items() if f in solved}
    if not contenders:
        return None, None
    best = max(contenders, key=contenders.get)
    if incumbent is None or incumbent not in contenders:
        return best, None  # no standing incumbent: adopt the best immediately
    if best == incumbent or contenders[best] - contenders[incumbent] < FLOOR_SWITCH_MARGIN:
        return incumbent, None
    count = challenge["count"] + 1 if challenge and challenge.get("floor") == best else 1
    if count >= FLOOR_SWITCH_CYCLES:
        return best, None
    return incumbent, {"floor": best, "count": count}

def _floor_zone_polygons(data, entity, floor_name):
    """Yield (zone entity_id, polygon, buffer_size, no_go) for the floor.

    no_go marks a zone a tracker can't be in (issue #60). Callers keep no-go
    zones for the solver bounds (they still bound the floor) but exclude them
    from zone assignment and snapping — a fix must never be reported as, or
    snapped into, dead space.
    """
    buffer_percent = 0.05  # set to 5%

    def order_zone_points(coords):
        """Order polygon points clockwise around centroid to avoid self-intersections.

        Only for legacy rectangle zones, whose four corners were stored in
        scan order. Polygon zones (poly: true) keep their drawn order — a
        centroid sort would corrupt concave shapes.
        """
        if len(coords) < 3:
            return coords
        center_x = sum(coord["x"] for coord in coords) / len(coords)
        center_y = sum(coord["y"] for coord in coords) / len(coords)
        return sorted(
            coords,
            key=lambda coord: np.arctan2(coord["y"] - center_y, coord["x"] - center_x)
        )

    for entity_data in data:
        if entity_data["entity"] == entity:
            for floor in entity_data["data"]["floor"]:
                if floor["name"] == floor_name:
                    for zone in floor["zones"]:
                        coords = zone.get("cords") or []
                        if len(coords) < 3:
                            continue
                        if not zone.get("poly") and len(coords) == 4:
                            coords = order_zone_points(coords)
                        polygon = Polygon([(coord["x"], coord["y"]) for coord in coords])
                        if not polygon.is_valid:
                            # Self-intersecting drawing; make_valid keeps every
                            # lobe (buffer(0) can drop one). Keep only the
                            # areal parts of the repair.
                            repaired = (
                                shapely_make_valid(polygon)
                                if shapely_make_valid
                                else polygon.buffer(0)
                            )
                            if repaired.geom_type == "GeometryCollection":
                                parts = [
                                    g for g in repaired.geoms
                                    if g.geom_type in ("Polygon", "MultiPolygon")
                                ]
                                repaired = unary_union(parts) if parts else None
                            if repaired is None or repaired.is_empty:
                                continue
                            polygon = repaired
                        xs = [coord["x"] for coord in coords]
                        ys = [coord["y"] for coord in coords]
                        width = max(xs) - min(xs)
                        height = max(ys) - min(ys)
                        buffer_size = ((width + height) / 2) * buffer_percent
                        yield zone["entity_id"], polygon, buffer_size, bool(zone.get("no_go"))


def _point_in_no_go(point, zone_polys):
    """True if the point falls strictly inside any no-go zone in zone_polys.

    zone_polys is the (zone_id, polygon, buffer_size, no_go) list from
    _floor_zone_polygons. Accepts a shapely Point or an (x, y) pair. Uses
    strict containment (not boundary-inclusive covers): a no-go zone's edge is
    typically a physical railing, and a tracker genuinely ON the walkway there
    sits on that boundary — it must not be penalised as if over the void.
    """
    if not isinstance(point, Point):
        point = Point(float(point[0]), float(point[1]))
    return any(no_go and polygon.contains(point)
               for _zone_id, polygon, _buffer_size, no_go in zone_polys)


def find_zone_for_point(data, entity, floor_name, point):
    """Find zone for point, prioritize correct polygon, select nearest buffer if no correct zone matches.

    No-go zones (issue #60) are skipped: a tracker can't be in one, so a fix
    there is reported as belonging to the nearest real zone (or "unknown").
    """
    buffer_candidates = []
    for zone_id, polygon, buffer_size, no_go in _floor_zone_polygons(data, entity, floor_name):
        if no_go:
            continue
        # covers() also matches points on the polygon boundary.
        if polygon.covers(point):
            return zone_id  # Prioritize correct polygon
        if polygon.buffer(buffer_size).contains(point):
            # Save candidate: (distance to edge, entity_id)
            # boundary works for Polygon and MultiPolygon alike.
            buffer_candidates.append((polygon.boundary.distance(point), zone_id))
    if buffer_candidates:
        # Select zone whose edge is closest to the point
        buffer_candidates.sort()
        return buffer_candidates[0][1]
    return "unknown"


def snap_point_into_zones(zone_polys, point):
    """Project a point onto valid (allowed, non-no-go) space.

    Returns the snapped Point, or None when the point is already in valid
    space (or there is nowhere valid to put it). No-go zones (issue #60) are
    subtracted from the allowed region — grown by NO_GO_SNAP_MARGIN_PX first —
    so a fix in dead space is pushed to the nearest genuinely-allowed point,
    clear of the boundary-inclusive no-go edge. This holds even when a no-go
    zone is drawn overlapping or nested inside an allowed room (subtraction
    carves the void out of the room), and when the floor's only zones are
    no-go (the point is at least pushed off the dead-space footprint).
    """
    allowed = [polygon for _zone_id, polygon, _buffer_size, no_go in zone_polys if not no_go]
    nogo = [polygon for _zone_id, polygon, _buffer_size, no_go in zone_polys if no_go]
    nogo_union = unary_union(nogo) if nogo else None
    nogo_blocks = nogo_union is not None and not nogo_union.is_empty

    valid = None
    if allowed:
        valid = unary_union(allowed)
        if nogo_blocks:
            # Grow-and-subtract: the snap target's boundary ends up
            # NO_GO_SNAP_MARGIN_PX outside the dead space, so the snapped point
            # is not still read as inside it by the boundary-inclusive tests.
            valid = valid.difference(nogo_union.buffer(NO_GO_SNAP_MARGIN_PX))

    if valid is not None and not valid.is_empty:
        if valid.covers(point):
            return None  # already in valid space
        snapped, _ = nearest_points(valid, point)
        return snapped

    # No allowed space to land in. If the point sits in declared dead space,
    # at least push it just off the no-go footprint; otherwise leave it as-is.
    if nogo_blocks and nogo_union.covers(point):
        snapped, _ = nearest_points(nogo_union.buffer(NO_GO_SNAP_MARGIN_PX).boundary, point)
        return snapped
    return None


def find_nearest_zone(data, entity, floor_name, point):
    """The zone closest to the point, no matter how far away.

    Trilateration jitter can land a fix between two zones or outside the map
    entirely; this always names the closest zone on the elected floor (a point
    inside a zone has distance 0, so it matches find_zone_for_point there).
    Returns "unknown" only when the floor has no usable zones.
    """
    nearest_id = "unknown"
    nearest_distance = None
    for zone_id, polygon, _buffer_size, no_go in _floor_zone_polygons(data, entity, floor_name):
        if no_go:
            continue  # dead space is never "the nearest zone" (issue #60)
        distance = polygon.distance(point)
        if nearest_distance is None or distance < nearest_distance:
            nearest_id, nearest_distance = zone_id, distance
    return nearest_id


def _floor_sub_zone_polygons(data, entity, floor_name):
    """Yield (sub-zone name, parent zone name, polygon) for the entity's floor.

    Sub-zones are small precise areas drawn inside a zone (a couch, a desk), so
    they are matched strictly (no soft buffer). They live in a separate
    "subzones" list, so the main-zone election/snap/nearest logic is untouched.
    """
    for entity_data in data:
        if entity_data["entity"] != entity:
            continue
        for floor in entity_data["data"]["floor"]:
            if floor["name"] != floor_name:
                continue
            # Sub-zones link to their parent by the zone's stable id; resolve it
            # to the zone's display name for the parent_zone attribute.
            zone_name_by_id = {}
            for z in floor.get("zones") or []:
                zone_name_by_id[z.get("zone_id") or z.get("entity_id")] = z.get("entity_id")
            for sub in floor.get("subzones") or []:
                coords = sub.get("cords") or []
                if len(coords) < 3:
                    continue
                polygon = Polygon([(c["x"], c["y"]) for c in coords])
                if not polygon.is_valid:
                    repaired = (
                        shapely_make_valid(polygon)
                        if shapely_make_valid
                        else polygon.buffer(0)
                    )
                    if repaired is None or repaired.is_empty:
                        continue
                    polygon = repaired
                parent_ref = sub.get("parent")
                yield sub.get("entity_id"), zone_name_by_id.get(parent_ref, parent_ref), polygon


def find_sub_zone_for_point(data, entity, floor_name, point):
    """The sub-zone containing the point and its parent zone name.

    Returns (sub_zone_name, parent_zone_name); ("unknown", None) when the point
    is in no sub-zone.
    """
    for sub_id, parent_id, polygon in _floor_sub_zone_polygons(data, entity, floor_name):
        if polygon.covers(point):
            return sub_id, parent_id
    return "unknown", None


async def async_setup(hass, config):
    """Set up the BPS integration."""
    _LOGGER.info("BPS integration initierad.")

    if hass.data.get("bps_initialized", False):
        _LOGGER.debug("BPS already initialized in current runtime; skipping duplicate init.")
        return True  # Abort if already running

    hass.data["bps_initialized"] = True  # Set flag
    await _stop_tracking(hass)
    setup_token = object()
    hass.data.setdefault(DOMAIN, {})["_tracking_setup"] = setup_token
    initialization_lock = asyncio.Lock()

    async def initialize_once():
        """Initialize the BPS component"""
        if (hass.data.get(DOMAIN, {}).get("_tracking_setup") is not setup_token
                or not hass.data.get("bps_initialized")):
            return
        _LOGGER.info("Initializing BPS...")

        if "bps_views_registered" not in hass.data:
            hass.http.register_view(BPSFrontendView())
            hass.http.register_view(BPSSaveAPIText())
            hass.http.register_view(BPSMapsListAPI())
            hass.http.register_view(BPSTrackerIconsListAPI())
            hass.http.register_view(BPSUploadTrackerIconAPI())
            hass.http.register_view(BPSReadAPIText())
            hass.http.register_view(BPSAdjustZonesAPI())
            hass.http.register_view(BPSReceiverStatusAPI())
            hass.http.register_view(BPSScannerLinkingAPI())
            hass.http.register_view(BPSGPSLocationsAPI())
            hass.http.register_view(BPSHeightSourcesAPI())
            hass.http.register_view(BPSCordsAPI(hass))
            hass.http.register_view(BPSCalibrationAPI(_calibration_tracking_check))
            hass.http.register_view(BPSSelfTestAPI(hass))
            hass.http.register_view(BPSTrackerTuneAPI())
            hass.http.register_view(BPSHistoryAPI(hass))
            hass.http.register_view(BPSDiagnosticsAPI(_diagnostic_snapshot, _diagnostic_inventory, _diagnostic_report_snapshot))
            hass.data["bps_views_registered"] = True

        config_path = hass.config.path()
        target_dir = os.path.join(config_path, "www", "bps_maps")
        tracker_icons_dir = os.path.join(config_path, "www", "bps_icons")

        try:
            await aiofiles.os.makedirs(target_dir, exist_ok=True)
            _LOGGER.info(f"Folder {target_dir} has been created or already existed")
        except Exception as e:
            _LOGGER.error(f"Could not create the folder {target_dir}: {e}")
            return

        try:
            await aiofiles.os.makedirs(tracker_icons_dir, exist_ok=True)
            _LOGGER.info(f"Folder {tracker_icons_dir} has been created or already existed")
        except Exception as e:
            _LOGGER.error(f"Could not create the folder {tracker_icons_dir}: {e}")
            return

        show_sidebar_panel = True
        if hasattr(config, "options"):
            show_sidebar_panel = config.options.get(OPTION_SHOW_SIDEBAR_PANEL, True)
        else:
            entries = hass.config_entries.async_entries(DOMAIN)
            if entries:
                show_sidebar_panel = entries[0].options.get(OPTION_SHOW_SIDEBAR_PANEL, True)
        panels = hass.data.get("frontend_panels", {})
        if "bps" in panels:
            async_remove_panel(hass, "bps")

        if show_sidebar_panel:
            try:
                _LOGGER.debug("Registering the custom panel for BPS...")
                # A custom panel (not a bare iframe) so the panel element
                # receives `hass` and can hand the app an HA access token to
                # authenticate its /api/bps/* calls (those views now require
                # auth). The element (bps-panel.js) still hosts the existing
                # app in an inner iframe and only couriers the token in.
                await panel_custom.async_register_panel(
                    hass,
                    frontend_url_path="bps",
                    webcomponent_name="bps-panel",
                    module_url="/bps/bps-panel.js",
                    sidebar_title="BPS",
                    sidebar_icon="mdi:map",
                    require_admin=False,
                    embed_iframe=False,
                )
                _LOGGER.info("Panel registered successfully.")
            except Exception as e:
                _LOGGER.error(f"Failed to register panel: {e}")
        else:
            _LOGGER.info("BPS sidebar panel is disabled by integration options.")

        # Move any legacy www/bps_maps flat files into the Store (once), then
        # load the layout into the in-memory cache. Store writes are atomic, so
        # a crash can no longer leave a 0-byte layout (issue #104).
        await migrate_legacy(hass)
        await load_bps_data(hass)
        # Layout is loaded, so the history settings are readable: bring the
        # retained window back before the tracking loop starts appending.
        await restore_position_history(hass)

        bucket = hass.data.setdefault(DOMAIN, {})
        if bucket.get("_tracking_setup") is not setup_token or not hass.data.get("bps_initialized"):
            return
        bucket["_tracking_lifecycle"] = object()
        bucket["_tracking_active"] = True
        resume_discovery = bucket.get("resume_sensor_discovery")
        if resume_discovery is not None:
            resume_discovery()
        activate_diagnostics(hass)
        hass.data["bps_update_task"] = hass.async_create_task(update_tracked_entities(hass))

        async def handle_homeassistant_stop(event):
            """Stop background work promptly so shutdown cannot drag or leave
            the unload half-done (which strands stale registry entries)."""
            await _stop_tracking(hass)
            await shutdown_diagnostics(hass)
            try:
                await flush_position_history(hass)
            except Exception as e:
                _LOGGER.debug("BPS position history final flush failed: %s", e)
            await async_shutdown_calibration(hass)

        hass.bus.async_listen_once("homeassistant_stop", handle_homeassistant_stop)

        await async_restore_calibration_state(hass)
        await async_start_auto_if_enabled(hass)

        _LOGGER.info("The BPS integration is fully initialized")

    async def initialize_bps():
        # A failed unload may retry an initialization whose started event
        # arrived during teardown. Serialize both attempts and never spawn a
        # second tracking loop if the first valid attempt already completed.
        async with initialization_lock:
            if hass.data.get("bps_update_task") is None:
                await initialize_once()

    hass.data.setdefault(DOMAIN, {})["_tracking_initialize"] = initialize_bps

    async def handle_homeassistant_started(event):
        """Handles the 'homeassistant_started' event"""
        await initialize_bps()

    if hass.is_running:
        await initialize_bps()
    else:
        hass.bus.async_listen_once("homeassistant_started", handle_homeassistant_started)

    return True

async def async_unload_entry(hass: HomeAssistant, entry):
    """Remove a configuration entry"""
    _LOGGER.info("Attempting to offload platforms for entry: %s", entry.entry_id)
    restart_on_failure = hass.data.get("bps_update_task") is not None
    bucket = hass.data.get(DOMAIN, {})
    pending_setup = bucket.get("_tracking_setup")
    pending_initialize = bucket.get("_tracking_initialize")
    await _stop_tracking(hass)

    async def unload_failed():
        # HA retains the entry after a failed unload. Keep its existing
        # tracking behavior, but restart with a fresh generation so cancelled
        # old jobs can never publish into the resumed installation.
        if restart_on_failure:
            bucket = hass.data.setdefault(DOMAIN, {})
            bucket["_tracking_lifecycle"] = object()
            bucket["_tracking_active"] = True
            resume_discovery = bucket.get("resume_sensor_discovery")
            if resume_discovery is not None:
                resume_discovery()
            activate_diagnostics(hass)
            hass.data["bps_update_task"] = hass.async_create_task(update_tracked_entities(hass))
        elif pending_setup is not None and pending_initialize is not None:
            # Before HA starts there is no tracking task to restart. Restore
            # the deferred callback's token. If its one-shot started event was
            # consumed during teardown, retry initialization now instead.
            bucket = hass.data.setdefault(DOMAIN, {})
            bucket["_tracking_setup"] = pending_setup
            bucket["_tracking_initialize"] = pending_initialize
            if getattr(hass, "is_running", False):
                await pending_initialize()
        return False

    await shutdown_diagnostics(hass)

    # Get whatever the tracking loop buffered since the last 60 s flush onto
    # disk before the task goes away. The in-memory ring survives an unload
    # (hass.data[DOMAIN] is not cleared), so this is belt-and-braces, but an
    # unload followed by a hard stop would otherwise lose that minute.
    try:
        await flush_position_history(hass)
    except Exception as e:
        _LOGGER.debug("BPS position history flush on unload failed: %s", e)

    try: # Attempt to unload platforms before removing their listener/registry.
        unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    except Exception as e:
        _LOGGER.error(f"Error during offloading of platforms for entry {entry.entry_id}: {e}")
        return await unload_failed()

    if not unload_ok:
        _LOGGER.error("Failed to offload platforms for entry: %s", entry.entry_id)
        return await unload_failed()

    # Once the sensor platform is gone, complete teardown. A frontend cleanup
    # error must not restart tracking against removed SensorEntity objects.
    state_listener_unsub = hass.data.pop("bps_state_listener_unsub", None)
    if state_listener_unsub:
        state_listener_unsub()

    cleanup_legacy_bps_registry_and_states(hass)

    entity_registry = er.async_get(hass)

    # Find and remove all entities that belong to "bps"
    entities_to_remove = [
        entity.entity_id for entity in entity_registry.entities.values()
        if entity.platform == "bps"
    ]

    for entity_id in entities_to_remove:
        _LOGGER.info(f"Removes sensor: {entity_id}")
        entity_registry.async_remove(entity_id)

    try: #Remove the frontend panel
        async_remove_panel(hass, frontend_url_path="bps")
        _LOGGER.info("Frontend-panel removed for entry: %s", entry.entry_id)
    except Exception as e:
        _LOGGER.error(f"Error when removing frontend-panel for entry {entry.entry_id}: {e}")

    await async_shutdown_calibration(hass)

    # Remove BPS states from the state machine when integration is unloaded.
    bps_state_ids = [
        state.entity_id
        for state in hass.states.async_all()
        if state.entity_id.startswith("sensor.")
        and state.entity_id.endswith(("_bps_zone", "_bps_floor", "_bps_nearest_zone", "_bps_sub_zone"))
    ]
    for entity_id in bps_state_ids:
        hass.states.async_remove(entity_id)

    # Allow clean setup after integration reload/removal.
    hass.data.pop("bps_initialized", None)
    hass.data.pop("bps_sensors", None)
    hass.data.get(DOMAIN, {}).pop("sync_group_sensors", None)
    hass.data.get(DOMAIN, {}).pop("resume_sensor_discovery", None)

    return True


async def async_setup_entry(hass, entry):
    """Set the integration from a configuration entry"""
    _LOGGER.info("async_setup_entry called")
    cleanup_legacy_bps_registry_and_states(hass)
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    """Set up BPS from a config entry."""
    return await async_setup(hass, entry)


async def async_update_options(hass, entry):
    """Reload integration when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)

class BPSFrontendView(HomeAssistantView):
    """Serve the frontend files."""

    url = "/bps/{file_name}"
    name = "bps:frontend"
    requires_auth = False

    async def get(self, request, file_name):
        """Serve static files from the frontend folder."""
        frontend_path = FRONTEND_PATH / file_name

        _LOGGER.info(f"Serving file: {frontend_path}")

        if not frontend_path.is_file():
            _LOGGER.error(f"Requested file not found: {frontend_path}")
            return web.Response(status=404, text="File not found")

        response = web.FileResponse(path=str(frontend_path))
        # The panel's script.js/CSS change on every integration update. Without
        # an explicit directive, browsers cache these heuristically and keep
        # serving the old panel after an update. "no-cache" keeps the cached
        # copy but forces revalidation (a cheap 304 when unchanged, the new file
        # when it changed), so updates show up on a normal reload.
        response.headers["Cache-Control"] = "no-cache"
        # bps-panel.js is loaded as an ES module (panel_custom), and browsers
        # refuse a module served with a non-JS MIME type. Force it rather than
        # trust the host's mimetypes registry (a Windows HA host can map .js to
        # text/plain, which would silently blank the panel).
        if file_name.endswith(".js"):
            response.headers["Content-Type"] = "text/javascript"
        return response

class BPSSaveAPIText(HomeAssistantView):
    """Handle saving of BPS coordinates to a text file."""

    url = "/api/bps/save_text"
    name = "api:bps:save_text"
    requires_auth = True

    async def post(self, request):
        """Handle saving coordinates to a text file."""
        hass = request.app["hass"]
        data = await request.post()

        coordinates = data.get("coordinates")

        if not coordinates:
            return web.Response(status=400, text="Missing coordinates")
        # Reject a non-JSON layout blob before it can reach the store.
        try:
            coords_obj = json.loads(coordinates)
        except (ValueError, TypeError):
            return web.Response(status=400, text="Coordinates must be valid JSON")

        maps_path = hass.config.path("www/bps_maps")

        try: # Persist the layout to the store + handle map upload/removal
            # Serialized with the calibration writers so read-modify-write
            # cycles on the layout cannot interleave.
            async with BPS_FILE_LOCK:
                error = await self._write_save(hass, maps_path, data, coords_obj)
            if error is not None:
                return error
            # A calibration window may be sampling right now; hand it the
            # edited placements so a re-linked/added receiver starts matching
            # on the next dump instead of after the next window/solve cycle.
            refresh_receivers_from_coords(hass, coordinates)
            _LOGGER.info("Saved BPS floor layout")
            return web.Response(status=200, text="Coordinates saved successfully")

        except Exception as e:
            _LOGGER.error(f"Failed to save coordinates: {e}")
            return web.Response(status=500, text="Failed to save coordinates")

    async def _write_save(self, hass, maps_path, data, coords_obj):
        """Validate all inputs, THEN persist; returns an error Response or None.

        Ordering matters: the new-floor map upload and the removal target are
        validated (and the upload read + size-checked) BEFORE the layout is
        saved, so a rejected upload can no longer strand the saved layout.
        Every filesystem target derived from client input is constrained to
        maps_path via _safe_maps_child (audit #2: path traversal / arbitrary
        write+delete). The layout goes to the Store (atomic); only the map
        images live under maps_path.
        """
        error = await hass.async_add_executor_job(validate_georeference, coords_obj)
        if error:
            return web.Response(status=400, text=error)
        error = await hass.async_add_executor_job(validate_height_surveys, coords_obj)
        if error:
            return web.Response(status=400, text=error)
        error = await hass.async_add_executor_job(validate_outdoor_layout, coords_obj)
        if error:
            return web.Response(status=400, text=error)
        # Shape validation also runs during startup, before Bermuda is ready.
        # At every group save, verify members and output IDs against real sensors.
        groups = coords_obj.get("tracker_groups", []) if isinstance(coords_obj, dict) else []
        if groups:
            known = {eid.removeprefix("sensor.").split("_distance_to_", 1)[0]
                     for eid in _bermuda_distance_sensor_ids(hass)} | _direct_tracker_keys(hass)
            error = await hass.async_add_executor_job(validate_outdoor_layout, coords_obj, known)
            if error:
                return web.Response(status=400, text=error)
        # --- Validate the optional new-floor map upload ---
        map_target = None
        map_bytes = None
        if data.get("new_floor") == "true":
            map_file = data.get("file")
            if not map_file:
                return web.Response(status=400, text="Missing file")
            map_target = _safe_maps_child(maps_path, map_file.filename, _ALLOWED_MAP_EXTS)
            if map_target is None:
                return web.Response(status=400, text="Invalid map filename")
            map_bytes = map_file.file.read()
            if len(map_bytes) > MAX_MAP_UPLOAD_BYTES:
                return web.Response(status=413, text="Map file too large")

        # --- Validate the optional removal target ---
        # No extension allowlist here: a map stored under any earlier-accepted
        # extension must stay deletable (the allowlist is an UPLOAD guard, not a
        # reason to strand an existing floor). Containment still applies, and
        # the layout/calibration files are explicitly protected.
        remove_target = None
        remove_file = data.get("remove")
        if remove_file:
            remove_target = _safe_maps_child(maps_path, remove_file, None)
            if remove_target is None or remove_target.name in _PROTECTED_MAPS_FILES:
                return web.Response(status=400, text="Invalid remove target")

        # --- Inputs valid: persist. Map first (so the saved layout never names
        # a map that failed to write), then the layout, then delete the old map. ---
        if map_target is not None:
            try:
                async with aiofiles.open(map_target, "wb") as f:
                    await f.write(map_bytes)
            except Exception as e:
                _LOGGER.error(f"Failed to save maps: {e}")
                return web.Response(status=500, text="Failed to save maps")

        await save_bps_data(hass, coords_obj)

        # Never delete the map we just wrote (a replace with the same filename).
        if remove_target is not None and remove_target != map_target and remove_target.exists():
            try:
                remove_target.unlink()
                _LOGGER.info(f"Removed file: {remove_target.name}")
            except Exception as e:
                _LOGGER.error(f"Failed to remove file {remove_target.name}: {e}")
                return web.Response(status=500, text="Failed to remove file")
        return None


class BPSAdjustZonesAPI(HomeAssistantView):
    """Propose a cleaned-up set of zones (square boxy rooms, snap shared
    boundaries incl. T-junctions, remove overlaps, re-clamp sub-zones).

    Pure-geometry PREVIEW: computes and returns a proposal plus a per-zone
    change report; it does NOT save. The panel renders the proposal, lets the
    user accept/reject, then persists via the existing save_text endpoint.
    """

    url = "/api/bps/adjust_zones"
    name = "api:bps:adjust_zones"
    requires_auth = True

    async def post(self, request):
        hass = request.app["hass"]
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON body")
        if not isinstance(body, dict):
            return web.Response(status=400, text="Body must be a JSON object")
        target = "subzones" if body.get("target") == "subzones" else "zones"
        zones = body.get("zones") or []
        subzones = body.get("subzones") or []
        options = body.get("options") or {}
        if not isinstance(zones, list):
            zones = []
        if not isinstance(subzones, list):
            subzones = []
        if not isinstance(options, dict):
            options = {}
        if target == "subzones":
            if not subzones:
                return web.Response(status=400, text="No sub-zones to adjust")
        elif not zones:
            return web.Response(status=400, text="No zones to adjust")
        func = adjust_subzones if target == "subzones" else adjust_zones
        try:
            # shapely work is synchronous; keep it off the event loop.
            result = await hass.async_add_executor_job(func, zones, subzones, options)
        except Exception as e:
            _LOGGER.error(f"adjust_zones ({target}) failed: {e}")
            return web.Response(status=500, text="Zone adjustment failed")
        return web.json_response(result)


class BPSReadAPIText(HomeAssistantView):
    """Return the stored BPS layout plus the tracked-device / receiver lists."""

    url = "/api/bps/read_text"
    name = "api:bps:read_text"
    requires_auth = True

    async def get(self, request):
        hass = request.app["hass"]
        # Both the tracked devices and their receivers come from the same
        # Bermuda "_distance_to_" sensors — the device is the part before
        # "_distance_to_", the receiver (scanner) the part after. Filtering to
        # Bermuda's own sensors keeps look-alike sensors from other integrations
        # out of both the tracked-device list and the receiver picker.
        entities = []
        receivers = []
        try:
            allowed = _bermuda_distance_sensor_ids(hass)
            entities = sorted({eid[len("sensor."):].split("_distance_to_")[0] for eid in allowed})
            entities = sorted(set(entities) | ReceiverSources(hass).direct_trackers())
            receivers = sorted({eid.split("_distance_to_", 1)[1] for eid in allowed})
        except Exception as e:
            _LOGGER.info(f"Error listing Bermuda distance sensors: {e}")

        # Offline scanners come from the Bermuda-liveness poller (proximity-
        # independent), refreshed on a slower cadence by the background loop.
        offline_receivers = list(hass.data.get(DOMAIN, {}).get("rl_offline", []))

        try:
            # The layout now lives in the Store; a fresh install has none, and
            # the frontend's `if (data.coordinates)` guard expects "" (not "[]")
            # in that case so it never JSON.parses an empty layout.
            layout = get_bps_data(hass)
            content = json.dumps(layout) if layout else ""
            diagnostics = _scanner_diagnostics(hass, content)
            matching = diagnostics["matching"]
            return web.json_response({
                "coordinates": content,
                "entities": entities,
                "receivers": [row["entity_id"] for row in matching["options"]],
                "receiver_matching": matching,
                "offline_receivers": offline_receivers,
                "unknown_receivers": list(hass.data.get(DOMAIN, {}).get("rl_unknown", [])),
                # Naming-mismatch diagnostics (issue #64): placed receivers whose
                # slug has no Bermuda distance sensor, and reporting scanners not
                # placed anywhere.
                "scanner_diagnostics": diagnostics,
            })

        except Exception as e:
            _LOGGER.error(f"Failed to read coordinates: {e}")
            return web.Response(status=500, text="Failed to read coordinates")

def _diagnostic_inventory(hass):
    """Only actual Bermuda beacons and enabled logical groups are selectable."""
    known = sorted({eid[len("sensor."):].split("_distance_to_", 1)[0]
                    for eid in _bermuda_distance_sensor_ids(hass)} | ReceiverSources(hass).direct_trackers())
    data = get_bps_data(hass)
    groups = normalize_groups(data, known)
    return {"trackers": [{"key": key, "name": key} for key in known] + [
        {"key": "bps_group_" + group["id"], "name": group["name"] + " (group)"}
        for group in groups], "floors": [f["name"] for f in data.get("floor", [])
        if isinstance(f.get("name"), str) and finite_number(f.get("scale"), minimum=1e-6) is not None]
        if isinstance(data, dict) else []}


async def _calibration_tracking_check(hass, floor_name):
    """Compare detached diagnostic samples without touching live calibration."""
    from .calibration_validation import compare_tracking_export
    from .calibration import _build_receiver_map, _equivalent_updates
    dom = hass.data.setdefault(DOMAIN, {})
    if dom.get("calibration_validation_busy"):
        raise ValueError("A tracking comparison is already running")
    recording = dom.get("_diagnostics")
    if recording is None or not recording.frames:
        raise ValueError("Record Diagnostics at two measured stationary locations first")
    if not isinstance(floor_name, str) or not floor_name.strip():
        raise ValueError("Select a floor first")
    result = next((v for k, v in dom.get("calibration", {}).get("results", {}).items()
                   if k.strip().lower() == floor_name.strip().lower()), None)
    layout = get_bps_data(hass)
    floor = next((f for f in layout.get("floor", []) if f.get("name") == result.get("floor")), None) if result else None
    if floor is None:
        raise ValueError("Solve calibration for the selected floor first")
    # Freeze candidate and geometry before awaiting export/executor work.
    floor = copy.deepcopy(floor)
    corrections = _equivalent_updates(hass, floor, copy.deepcopy(result["receivers"]),
                                     identities=result.get("receiver_identities"))
    current_layout = copy.deepcopy(layout)
    eligible_receivers = set(_build_receiver_map(current_layout, floor["name"], hass=hass))
    baseline_corrections = {receiver.get("entity_id"): receiver.get("correction", 1)
                            for receiver in floor.get("receivers", [])}
    solved_at = result.get("solved_at")
    dom["calibration_validation_busy"] = True
    try:
        payload = await recording.export()
        report = await hass.async_add_executor_job(partial(compare_tracking_export, payload,
            floor["name"], corrections, floor, trilaterate,
            baseline_corrections=baseline_corrections, current_layout=current_layout,
            eligible_receivers=eligible_receivers))
        report.update(candidate_solved_at=solved_at, candidate_corrections=corrections)
        return report
    finally:
        dom.pop("calibration_validation_busy", None)


def _diagnostic_snapshot(hass, targets, now):
    """Detached, read-only snapshot. Published telemetry retains its own clock.

    Current sensor readings are deliberately separate from the earlier solver
    inputs in positions: polling never pretends these were one atomic solve.
    """
    report = targets is None
    data = get_bps_data(hass)
    if not isinstance(data, dict):
        if not report:
            raise ValueError("Save a BPS layout before recording")
        data = {}
    sources = ReceiverSources(hass)
    known = {eid[len("sensor."):].split("_distance_to_", 1)[0]
             for eid in _bermuda_distance_sensor_ids(hass)} | sources.direct_trackers()
    groups = {"bps_group_" + g["id"]: g["beacons"] for g in normalize_groups(data, known)}
    recording = hass.data.get(DOMAIN, {}).get("_diagnostics")
    retained = recording.members if not report and recording and recording.active else {}
    if report:
        targets = sorted(known | set(groups) | {p["ent"] for p in
            hass.data.get(DOMAIN, {}).get("apitricords", []) if isinstance(p.get("ent"), str)})
    # Freeze subjects for this session, including when a group is edited or
    # disabled/deleted. A new recording resolves the current membership afresh.
    members = {target: list(retained[target]) if target in retained else
               [target] if target in known else list(groups.get(target, [target]))
               for target in targets}
    beacons = sorted({beacon for values in members.values() for beacon in values})
    if not report and len(beacons) > 16:
        raise ValueError("Record at most 16 constituent beacons at once")
    floors = data.get("floor", [])
    placements = sum(len(f.get("receivers", [])) for f in floors)
    if not report and placements > 128:
        raise ValueError("Diagnostic recording supports at most 128 receiver placements")
    if report and len(beacons) * placements > 32768:
        raise ValueError("Full diagnostic report supports at most 32768 beacon/receiver readings")
    context = {"layout": {key: copy.deepcopy(data[key]) for key in (
        "outdoor_tracking", "tracker_groups", "tracker_height", "tracker_heights",
        "tracker_hardware", "tracker_ref_offsets", "reading_max_age", "position_timeout") if key in data},
        "target_members": members, "defaults": {"tracker_height_m": TRACKER_HEIGHT_M,
        "reading_max_age_s": READING_MAX_AGE_SECS, "position_timeout_s": STALE_POSITION_SECS}}
    context["layout"]["floor"] = [{key: copy.deepcopy(floor[key]) for key in (
        "name", "scale", "receivers", "zones", "subzones", "environment", "georeference") if key in floor} for floor in floors]
    cal = hass.data.get(DOMAIN, {}).get("calibration", {})
    context["calibration"] = {key: copy.deepcopy(cal.get(key)) for key in (
        "mode", "floor", "results", "applied", "last_solved_at")}
    readings = []
    max_age = _reading_max_age(data)
    for beacon in beacons:
        factor, tracker_height = _tracker_distance_factor(data, beacon), _tracker_height(data, beacon)
        for floor in floors:
            for receiver, binding in zip(floor.get("receivers", []), sources.floor_bindings(floor)):
                slug = receiver.get("entity_id", "")
                source, state = sources.source(beacon, receiver, binding, now=now)
                row = {"tracker": beacon, "receiver": slug, "floor": floor.get("name"),
                       "entity_id": source, "status": binding["status"] if sources.blocked(binding) else "missing",
                       "scanner_aliases": binding["aliases"], "tracker_height_m": tracker_height,
                       "tracker_distance_factor": factor, "ignored": receiver.get("outdoor_policy") == "ignore",
                       "measurement_source": sources.source_info(binding, state)}
                if state is not None:
                    raw = state.state
                    unit = state.attributes.get("unit_of_measurement")
                    age = _reading_age_secs(state, now=now)
                    row.update({"raw_state": str(raw)[:256], "unit": str(unit)[:32] if unit is not None else None,
                                "age_s": age, "status": "invalid"})
                    provenance = state.attributes.get("bps_source")
                    if isinstance(provenance, dict):
                        row["measurement_source"] = copy.deepcopy(provenance)
                    ts = getattr(state, "last_updated", None) or getattr(state, "last_changed", None)
                    try:
                        row["observed"] = ts.timestamp()
                    except (AttributeError, ValueError, TypeError, OSError, OverflowError):
                        row["observed"] = None
                    try:
                        measured = float(raw)
                        if unit in DistanceConverter.VALID_UNITS and unit != UnitOfLength.METERS:
                            measured = DistanceConverter.convert(measured, unit, UnitOfLength.METERS)
                        if math.isfinite(measured) and measured >= 0:
                            correction = receiver.get("correction")
                            correction = float(correction) if isinstance(correction, (int, float)) and correction > 0 else 1.0
                            corrected = measured * correction * factor
                            horizontal = corrected
                            height = receiver.get("height")
                            if isinstance(height, (int, float)) and 0 <= height <= 10:
                                horizontal = math.sqrt(max(corrected ** 2 - (height - tracker_height) ** 2,
                                    min(corrected ** 2, MIN_WEIGHT_RADIUS_M ** 2)))
                            row.update({"measured_distance_m": measured, "corrected_distance_m": corrected,
                                        "horizontal_distance_m": horizontal, "receiver_correction": correction,
                                        "assumed_unit_m": unit not in DistanceConverter.VALID_UNITS,
                                        "status": "current"})
                            if max_age and age is not None and age >= max_age:
                                row["status"] = "stale"
                    except (ValueError, TypeError, OverflowError):
                        pass
                    if raw in ("unknown", "unavailable"):
                        row["status"] = str(raw)
                        if isinstance(provenance, dict):
                            row["status"] = provenance.get("status") or "unavailable"
                readings.append(row)
    wanted = set(targets) | set(beacons)
    positions = [copy.deepcopy(p) for p in hass.data.get(DOMAIN, {}).get("apitricords", [])
                 if p.get("ent") in wanted]
    return {"time": now, "context": context, "readings": readings, "positions": positions,
            "calibration_status": {"state": cal.get("state", "idle"), "error": cal.get("error"),
                "pair_counts": {str(k): len(v) for k, v in list(cal.get("samples", {}).items())[:256]}},
            "offline_receivers": list(hass.data.get(DOMAIN, {}).get("rl_offline", []))[:128]}


def _recent_tracking_snapshot(hass, now):
    """Detached completed-batch telemetry; never poll all HA sensor states.

    Original fixes remain visible after exclusion/expiry, with their original
    observation times. Absence from a group's valid members is not a new fix.
    """
    dom = hass.data.get(DOMAIN, {})
    layout = get_bps_data(hass) or {}
    context = {"layout": {key: copy.deepcopy(layout[key]) for key in (
        "outdoor_tracking", "tracker_groups", "tracker_height", "tracker_heights",
        "tracker_hardware", "tracker_ref_offsets", "reading_max_age", "position_timeout") if key in layout}}
    context["layout"]["floor"] = [{key: copy.deepcopy(floor[key]) for key in (
        "name", "scale", "receivers", "zones", "subzones", "environment") if key in floor}
        for floor in layout.get("floor", [])]
    context["bermuda_settings"] = bermuda_diagnostics.settings_snapshot(hass)
    cal = dom.get("calibration", {})
    context["calibration"] = {key: copy.deepcopy(cal.get(key)) for key in (
        "mode", "floor", "applied", "last_solved_at", "auto_status")}
    context["defaults"] = {"reading_max_age_s": READING_MAX_AGE_SECS,
        "position_timeout_s": STALE_POSITION_SECS, "tracker_height_m": TRACKER_HEIGHT_M,
        "tracking_update_interval_s": secToUpdate}
    positions = dom.get("apitricords", [])
    known = {p.get("ent") for p in positions if not p.get("group") and isinstance(p.get("ent"), str)}
    known.update(_direct_tracker_keys(hass))
    members = {beacon for group in normalize_groups(layout, known_trackers=known)
               for beacon in group.get("beacons", [])}
    # Groups are appended after originals at publication. Retain their
    # decisions and member solver inputs before unrelated beacon fixes.
    positions = ([p for p in positions if p.get("group")]
        + [p for p in positions if not p.get("group") and p.get("ent") in members]
        + [p for p in positions if not p.get("group") and p.get("ent") not in members])
    keys = ("ent", "group", "floor", "zone", "cords", "updated", "outdoor",
            "beacons_reporting", "total_beacons", "beacon_positions", "anchor_beacon",
            "anchor_switch_pending", "motion_gate", "fusion_confidence", "beacon_disagreement_m")
    copied = [{key: copy.deepcopy(p[key]) for key in keys if key in p} for p in positions[:128]]
    for position in copied:
        quality = position.get("outdoor")
        if isinstance(quality, dict):
            observed = (position.get("updated") if quality.get("use_observation_age") is False
                        else quality.get("observed", position.get("updated")))
            observed = finite_number(observed, minimum=0.0)
            if observed is not None:
                age = max(0.0, now - observed)
                cutoff = finite_number(quality.get("stale_after_s"), minimum=0.0) or 30.0
                quality.update(position_age_s=age, stale=age >= cutoff)
        if position.get("group"):
            valid = {p["ent"]: p for p in position.get("beacon_positions", [])}
            group = next((g for g in layout.get("tracker_groups", [])
                          if "bps_group_" + str(g.get("id")) == position.get("ent")), {})
            position["member_status"] = {member: (
                valid[member].get("selection_reason", "used" if valid[member].get("used") else "excluded")
                if member in valid else "no_eligible_fix") for member in group.get("beacons", [])}
    return {"time": now, "context": context, "positions": copied,
        "omitted_positions": max(0, len(positions) - 128),
        "offline_receivers": list(dom.get("rl_offline", []))[:128]}


async def _capture_recent_tracking(hass):
    """Use the existing batch task; failures never interrupt positioning."""
    if not hass.data.get("bps_initialized"):
        return
    dom = hass.data.setdefault(DOMAIN, {})
    history = dom.get("_recent_tracking")
    if history is None:
        history = dom["_recent_tracking"] = recent_tracking.RecentTrackingHistory()
    if not outdoor_settings(get_bps_data(hass) or {})["enabled"]:
        history.clear()
        return
    now = time.time()
    if not history.due(now):
        return
    last = dom.get("_recent_tracking_last_attempt")
    if last and last[0] is history and 0 <= now - last[1] < recent_tracking.SAMPLE_INTERVAL_SECONDS:
        return
    dom["_recent_tracking_last_attempt"] = (history, now)
    context = _capture_tracking_context(hass)
    try:
        detached = _recent_tracking_snapshot(hass, now)
        packed = await hass.async_add_executor_job(recent_tracking.pack, detached)
        # An unload/layout change during encoding invalidates this batch.
        if (_tracking_context_current(context) and hass.data.get("bps_initialized")
                and dom.get("_recent_tracking") is history):
            history.append(packed, time.time())
    except recent_tracking.PackError as err:
        history.record_failure(err.reason)
    except Exception:
        history.record_failure()


def _diagnostic_report_snapshot(hass, now):
    """All saved tracking configuration and current telemetry; never starts capture."""
    snapshot = _diagnostic_snapshot(hass, None, now)
    dom = hass.data.get(DOMAIN, {})
    recent = dom.get("_recent_tracking")
    snapshot["_recent_tracking_snapshot"] = (recent or recent_tracking.RecentTrackingHistory()).snapshot(now)
    layout = get_bps_data(hass)
    # Saved settings only; omit map/icon assets and unrelated entity state.
    if isinstance(layout, dict):
        snapshot["context"]["layout"].update({key: copy.deepcopy(layout[key]) for key in (
            "history_enabled", "history_max_age", "history_min_interval", "history_min_move_m",
            "history_max_points", "history_heartbeat") if key in layout})
    snapshot["context"]["defaults"].update(kalman_measurement_noise_m=KF_MEAS_NOISE_M,
        kalman_acceleration_noise_ms2=KF_ACCEL_NOISE_MS2, radius_jump_tolerance=RADIUS_JUMP_TOL,
        min_weight_radius_m=MIN_WEIGHT_RADIUS_M, solver_robust_f_scale=SOLVER_ROBUST_F_SCALE)
    cal = dom.get("calibration", {})
    snapshot["calibration_status"].update({key: copy.deepcopy(cal.get(key)) for key in (
        "mode", "floor", "auto_status", "last_solved_at", "ends_at", "duration")})
    snapshot["calibration_status"]["pair_counts"] = {str(k): len(v) for k, v in cal.get("samples", {}).items()}
    snapshot["offline_receivers"] = list(dom.get("rl_offline", []))
    snapshot["inventory"] = _diagnostic_inventory(hass)
    snapshot["scanner_diagnostics"] = _scanner_diagnostics(hass, json.dumps(snapshot["context"]["layout"]))
    snapshot["scanner_linking"] = _scanner_linking(hass, json.dumps(snapshot["context"]["layout"]))
    updater = hass.data.get("bps_update_task")
    recording = dom.get("_diagnostics")
    snapshot["runtime"] = {"initialized": bool(hass.data.get("bps_initialized")),
        "tracking_task_running": updater is not None and not updater.done(),
        "recording": copy.deepcopy(recording.status()) if recording else {"active": False, "available": False}}
    snapshot["runtime"]["effective_history_settings"] = history_mod.history_config(layout)
    task = cal.get("task")
    snapshot["runtime"].update(calibration_task_running=task is not None and not task.done(),
        tracking_update_interval_s=secToUpdate, max_concurrent_trackers=MAX_CONCURRENT_TRACKERS,
        receiver_offline_after_s=RECEIVER_OFFLINE_SECS, receiver_liveness_poll_interval_s=RECEIVER_DUMP_INTERVAL,
        queued_tracking_updates=update_queue.qsize())
    snapshot["bermuda"] = bermuda_diagnostics.snapshot(hass, now)
    try:
        snapshot["bps_entities"], _, snapshot["bps_entity_counts"] = bermuda_diagnostics.entity_snapshot(hass, DOMAIN, now)
    except (AttributeError, KeyError, TypeError):
        snapshot["bps_entities"] = []
        snapshot["runtime"]["bps_entities_unavailable"] = True
    # Snapshot fixes keep observation timestamps, but report age/staleness at
    # collection time rather than the frozen values from their original solve.
    for position in snapshot["positions"]:
        quality = position.get("outdoor")
        if not isinstance(quality, dict):
            continue
        observed = position.get("updated") if quality.get("use_observation_age") is False else quality.get("observed", position.get("updated"))
        observed = finite_number(observed, minimum=0.0)
        if observed is not None:
            age = max(0.0, now - observed)
            quality.update(position_age_s=age, stale=age >= quality.get("stale_after_s", 30.0))
    return snapshot


class BPSReceiverStatusAPI(HomeAssistantView):
    """Current offline receivers (Bermuda liveness), polled live by the panel."""
    url = "/api/bps/receiver_status"
    name = "api:bps:receiver_status"
    requires_auth = True

    async def get(self, request):
        hass = request.app["hass"]
        offline = hass.data.get(DOMAIN, {}).get("rl_offline", [])
        return web.json_response({"offline": list(offline), "unknown": list(hass.data.get(DOMAIN, {}).get("rl_unknown", []))})


class BPSScannerLinkingAPI(HomeAssistantView):
    """On-demand debug view (issue #64): how each placed receiver links to its
    Bermuda distance sensors and whether each is reporting right now. Fetched
    live only when the panel's 'Scanner linking' section is expanded, so it
    never adds cost to a normal panel load or the tracking loop."""
    url = "/api/bps/scanner_linking"
    name = "api:bps:scanner_linking"
    requires_auth = True

    async def get(self, request):
        hass = request.app["hass"]
        # No placements to compare against is fine; still report the sensors.
        layout = get_bps_data(hass)
        content = json.dumps(layout) if layout else ""
        try:
            data = _scanner_linking(hass, content)
        except Exception as e:
            _LOGGER.error(f"scanner_linking failed: {e}")
            data = {"placed": [], "unplaced": []}
        try:
            data["beacons"] = _beacon_links(hass)
        except Exception as e:
            _LOGGER.error(f"beacon_links failed: {e}")
            data["beacons"] = []
        return web.json_response(data)


class BPSMapsListAPI(HomeAssistantView):
    """API to list map files in /www/bps_maps."""
    url = "/api/bps/maps"
    name = "api:bps:maps"
    requires_auth = True

    @staticmethod
    def _list_map_files(maps_path):
        """List map file names from disk (runs in executor)."""
        with os.scandir(maps_path) as entries:
            return [
                entry.name
                for entry in entries
                if entry.is_file() and entry.name.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
            ]

    async def get(self, request):
        """Return a list of map files as JSON."""
        hass = request.app["hass"]
        maps_path = hass.config.path("www/bps_maps")

        try:
            file_names = await hass.async_add_executor_job(self._list_map_files, maps_path)
            return web.json_response(file_names)
        except Exception as e:
            _LOGGER.error(f"Error listing map files: {e}")
            return web.Response(status=500, text="Error listing map files")


class BPSTrackerIconsListAPI(HomeAssistantView):
    """API to list tracker icon files."""

    url = "/api/bps/tracker_icons"
    name = "api:bps:tracker_icons"
    requires_auth = True

    @staticmethod
    def _list_tracker_icons(icons_path):
        if not os.path.isdir(icons_path):
            return []
        with os.scandir(icons_path) as entries:
            return [
                {"value": f"/local/bps_icons/{entry.name}", "label": entry.name}
                for entry in entries
                if entry.is_file() and entry.name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".svg"))
            ]

    async def get(self, request):
        hass = request.app["hass"]
        icons_path = hass.config.path("www/bps_icons")
        try:
            custom_icons = await hass.async_add_executor_job(self._list_tracker_icons, icons_path)
            defaults = [
                {"value": "/bps/person.svg", "label": "Person (default)"},
                {"value": "/bps/beacon.svg", "label": "Beacon"},
            ]
            return web.json_response(defaults + custom_icons)
        except Exception as e:
            _LOGGER.error(f"Error listing tracker icons: {e}")
            return web.Response(status=500, text="Error listing tracker icons")


class BPSUploadTrackerIconAPI(HomeAssistantView):
    """API to upload custom tracker icons."""

    url = "/api/bps/upload_tracker_icon"
    name = "api:bps:upload_tracker_icon"
    requires_auth = True

    async def post(self, request):
        hass = request.app["hass"]
        data = await request.post()
        icon_file = data.get("icon")
        if not icon_file:
            return web.Response(status=400, text="Missing icon")

        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(icon_file.filename).name)
        if not safe_name:
            return web.Response(status=400, text="Invalid filename")

        icons_path = hass.config.path("www/bps_icons")
        try:
            await aiofiles.os.makedirs(icons_path, exist_ok=True)
            target_path = Path(icons_path) / safe_name
            async with aiofiles.open(target_path, "wb") as f:
                await f.write(icon_file.file.read())
        except Exception as e:
            _LOGGER.error(f"Failed to upload tracker icon: {e}")
            return web.Response(status=500, text="Failed to upload icon")

        return web.json_response({
            "icon_url": f"/local/bps_icons/{safe_name}",
            "icon_name": safe_name,
        })


class BPSCordsAPI(HomeAssistantView):
    """API endpoint that returns apitricords."""

    url = "/api/bps/cords"
    name = "api:bps:cords"
    requires_auth = True

    def __init__(self, hass):
        """Spara referens till hass"""
        self.hass = hass

    async def get(self, request):
        """Return apitricords from hass.data."""
        apitricords = self.hass.data.get(DOMAIN, {}).get("apitricords", {})

        if not apitricords:
            return web.json_response({"error": "No data available"}, status=404)

        # A frozen last-known fix remains visible during the existing grace
        # period, but must not look newly measured because HTTP polling works.
        now = time.time()
        payload = []
        for position in apitricords:
            if "outdoor" not in position:
                payload.append(position)
                continue
            quality = dict(position["outdoor"])
            # Disabling measurement-age gating retains the normal last-solve
            # grace. Keep the real measurement observation in the payload.
            observed = position.get("updated", now) if quality.get("use_observation_age") is False \
                else quality.get("observed", position.get("updated", now))
            age = max(0.0, now - observed)
            quality.update(position_age_s=age, stale=age >= quality.get("stale_after_s", 30.0))
            payload.append({**position, "outdoor": quality})
        return web.json_response(payload)

# Trilateration function
# Floor on the distance used in the Jacobian's direction vector. Only guards
# the 0/0 at a fit sitting exactly on a receiver; far below any real geometry.
_JAC_MIN_DIST = 1e-9


def trilaterate(known_points, bounds=None, min_weight_radius=1e-3):
    """Weighted least-squares position fit.

    known_points are (x, y, r[, w[, wr]]) tuples. The optional w is a per-point
    reliability in [0, 1] (1 = fully trusted); it multiplies the geometric
    1/r^2 weight, so a spiky reading pulls the fit less without being dropped.
    Missing w defaults to 1. The optional wr overrides r as the RADIUS USED IN
    THE GEOMETRIC WEIGHT (the residual always uses r): live callers pass the
    measured slant (px) here, so a height-corrected projection that collapsed
    to the floor cannot buy itself dominant 1/r^2 influence — following the
    4-tuple form with collapsed projected radii reintroduces exactly the 1.7.0
    hijack. min_weight_radius clamps whichever weight radius is in use.

    bounds, when given as (minx, miny, maxx, maxy), constrains the solution to
    the floor's extent: the fit then finds the best position WITHIN the map,
    which lands on the boundary when an unconstrained fit would escape it.

    min_weight_radius clamps the radius used in the 1/r^2 WEIGHT (never the
    residual). Callers pass a physical floor in pixels (MIN_WEIGHT_RADIUS_M x
    floor scale): a near-zero radius — device at the receiver, or a slant range
    fully consumed by a known mount height — is an honest reading, but its
    weight must stay finite or that single receiver decides the whole fit. The
    default keeps the bare no-scale fallback safe against division by zero.
    """
    num_points = len(known_points)

    if num_points < 3: # Make sure there are enough points (min 3) to do a trilataration
        _LOGGER.error("At least three known points are required for trilateration.")
        return None

    # The per-point arrays are built ONCE here, not rebuilt inside the
    # objective: least_squares calls the objective (and the Jacobian) many
    # times per solve, and at this problem size the Python loop that used to
    # live in there cost far more than the arithmetic it performed.
    px = np.fromiter((p[0] for p in known_points), dtype=float, count=num_points)
    py = np.fromiter((p[1] for p in known_points), dtype=float, count=num_points)
    pr = np.fromiter((p[2] for p in known_points), dtype=float, count=num_points)
    rel = np.fromiter((p[3] if len(p) > 3 else 1.0 for p in known_points),
                      dtype=float, count=num_points)
    # Weight radius: the MEASURED slant when supplied (5th element), else the
    # fit radius. Height correction can legitimately collapse the projected
    # radius to the minimum while the measured slant is ~dz (metres) —
    # weighting by the projection handed such a receiver ~100x influence and
    # the fit snapped onto it (1.7.0 regression).
    wrad = np.fromiter((p[4] if len(p) > 4 else p[2] for p in known_points),
                       dtype=float, count=num_points)
    # reliability x geometric (1/r^2) weight, pre-rooted: the residual is
    # sqrt(w) * (distance - r), so sqrt() belongs here rather than per call.
    sqrt_w = np.sqrt(rel / np.maximum(wrad, min_weight_radius) ** 2)

    def objective_function(X):
        return sqrt_w * (np.hypot(px - X[0], py - X[1]) - pr)

    def jacobian(X):
        """Exact derivative of the residual: d/dX sqrt(w)(|X - p| - r).

        Supplying it saves least_squares the finite-difference pass, which
        costs one extra objective evaluation per unknown per iteration.
        The distance is floored before dividing: a fit sitting exactly on a
        receiver has an undefined direction, and a NaN there would poison the
        whole step rather than just that row.
        """
        dx = X[0] - px
        dy = X[1] - py
        d = np.maximum(np.hypot(dx, dy), _JAC_MIN_DIST)
        return np.column_stack((sqrt_w * dx / d, sqrt_w * dy / d))

    # Start from the receiver centroid: it is always a plausible position,
    # unlike the map corner, and it must lie inside any given bounds.
    x0 = np.array([
        float(np.mean([p[0] for p in known_points])),
        float(np.mean([p[1] for p in known_points])),
    ])
    # Robust least squares: soft_l1 down-weights a single spatial outlier so it
    # can't drag the fit. least_squares already defaults to TRF (which supports
    # both bounds and a robust loss), so this only adds the loss and makes the
    # method + bounds explicit; without bounds the search is the whole plane,
    # seeded at the plausible centroid.
    if bounds is not None:
        minx, miny, maxx, maxy = bounds
        x0[0] = np.clip(x0[0], minx, maxx)
        x0[1] = np.clip(x0[1], miny, maxy)
        lo, hi = [minx, miny], [maxx, maxy]
    else:
        lo, hi = [-np.inf, -np.inf], [np.inf, np.inf]
    result = least_squares(
        objective_function, x0, jac=jacobian,
        bounds=(lo, hi), method="trf",
        loss="soft_l1", f_scale=SOLVER_ROBUST_F_SCALE,
    )

    if not result.success:
        # Non-convergence is an expected, handled outcome on ill-conditioned
        # input — a floor whose readings don't agree, or the self-test's
        # leave-one-out on a corner/degenerate receiver. Every caller treats
        # None as "not a contender" / "unsolved", so this is DEBUG, not ERROR
        # (it was spamming the log once the periodic self-test began running).
        _LOGGER.debug("Trilateration did not converge for %d points; skipping.", len(known_points))
        return None
    x, y = result.x # Extract the calculated coordinates
    return x, y # return the result


# --- Receiver self-localization: a zero-setup, ground-truthed accuracy bench --
# Every receiver's true position is known (it is placed on the map) and Bermuda
# measures the distance between scanners, so each receiver's position can be
# solved from the OTHERS' measured distances -- through the SAME trilaterate() +
# per-receiver correction + slant the live tracker uses -- and compared to where
# it actually is (leave-one-out). This measures the SOLVER, per-receiver
# calibration, and geometry with no parked beacon and no hand-measured truth.
# It does NOT exercise the temporal filtering (a static, always-fresh anchor has
# no motion to smooth or stale readings to reject), and it is an OPTIMISTIC
# bound on real tracker accuracy: calibration is fit on these very inter-receiver
# links, and receiver-to-receiver paths (ceiling height, clean line of sight)
# are easier than a body-worn beacon's. Consumed by tools/bps_eval.py `selftest`.
SELFTEST_MIN_SAMPLES = 3  # need a median over at least this many raw distances
SELFTEST_SENSOR_INTERVAL = 1800  # refresh the accuracy sensor every 30 min


def _selftest_receivers(coords):
    """(floor, slug) -> geometry for each usable floor-local placement."""
    out = {}
    if not isinstance(coords, dict):
        return out
    for floor in coords.get("floor", []):
        scale = floor.get("scale")
        if not scale:
            continue
        for rec in floor.get("receivers", []):
            cords = rec.get("cords") or {}
            eid = rec.get("entity_id")
            if not eid or cords.get("x") is None or cords.get("y") is None:
                continue
            height = rec.get("height")
            corr = rec.get("correction")
            out[(floor.get("name"), str(eid))] = {
                "entity": str(eid),
                "x": float(cords["x"]),
                "y": float(cords["y"]),
                "scale": float(scale),
                "floor": floor.get("name"),
                "height": float(height) if isinstance(height, (int, float)) and 0 <= height <= 10 else None,
                "correction": float(corr) if isinstance(corr, (int, float)) and corr > 0 else 1.0,
                "identity": (rec.get("scanner_config_entry_id"), rec["scanner_mac"])
                if rec.get("scanner_mac") else None,
            }
    return out


def _selftest_snapshot(hass, samples=None):
    """Detach registry bindings, current geometry and raw distances on the loop.

    Calibration stores each physical scanner under one canonical sample slug;
    placements on other floors retain their own geometry and displayed slug.
    Captured hardware proof keeps an old window from following a replacement
    scanner merely because it inherited the same label.
    """
    coords = get_bps_data(hass)
    coords = ReceiverSources(hass).calibration_layout(coords if isinstance(coords, dict) else {})
    receivers = _selftest_receivers(coords)
    cal = get_calibration_state(hass)
    captured = cal.get("receivers") or {}
    routes = cal.get("_auto_routes") or {}
    # Restore deliberately leaves live receiver maps empty until calibration
    # starts. The persisted observation baseline still owns these raw samples.
    # Its canonical routes and hardware proof also apply while auto is off.
    baseline = cal.get("_auto_geometry")
    require_proof = baseline is not None
    if isinstance(baseline, dict):
        if isinstance(baseline.get("floors"), dict) and isinstance(baseline.get("routes"), dict):
            routes = baseline["routes"]
            captured = {}
            for floor, placements in baseline["floors"].items():
                for slug, row in placements.items():
                    canonical = routes.get(floor, {}).get(slug)
                    if canonical is not None:
                        captured.setdefault(canonical, row)
        else:
            captured, routes = baseline, {}
    by_identity, claims = {}, {}
    for slug, row in captured.items():
        identity = (row.get("scanner_config_entry_id"), row["scanner_mac"]) if row.get("scanner_mac") else None
        if identity:
            by_identity.setdefault(identity, set()).add(slug)
    for (_, slug), row in receivers.items():
        claims.setdefault(slug, set()).add(row["identity"] or ("exact", slug))
    ambiguous = {slug for slug, identities in claims.items() if len(identities) != 1}
    ambiguous.update(cal.get("_auto_ambiguous_slugs") or ())
    receivers = {key: row for key, row in receivers.items() if key[1] not in ambiguous}
    for (floor, slug), row in receivers.items():
        sample_slug = routes.get(floor, {}).get(slug, slug)
        proven = by_identity.get(row["identity"], ())
        if len(proven) == 1:
            sample_slug = next(iter(proven))
        previous = captured.get(sample_slug)
        if previous is None and require_proof:
            sample_slug = None
        elif previous is not None:
            identity = (previous.get("scanner_config_entry_id"), previous["scanner_mac"]) if previous.get("scanner_mac") else None
            if identity != row["identity"]:
                sample_slug = None
        row["sample_slug"] = sample_slug
    return {
        "receivers": receivers,
        "bounds": _selftest_floor_bounds(coords, receivers),
        "samples": {key: list(values) for key, values in
                    (samples if samples is not None else cal.get("samples", {})).items()},
    }


def _selftest_floor_bounds(coords, receivers):
    """Per-floor solver bounds = extent of ALL placed receivers + zone polygons
    (+10% margin), mirroring the live solve (update_trilateration_and_zone).

    Keyed on the floor so a left-out perimeter receiver's own known position
    still lies inside the feasible box — bounding only to the OTHER receivers
    would clamp it and inflate its error.
    """
    xs_by, ys_by = {}, {}
    for r in receivers.values():
        xs_by.setdefault(r["floor"], []).append(r["x"])
        ys_by.setdefault(r["floor"], []).append(r["y"])
    if isinstance(coords, dict):
        for floor in coords.get("floor", []):
            name = floor.get("name")
            for zone in floor.get("zones", []) or []:  # no-go zones still bound the floor
                for c in (zone.get("cords") or []):
                    if isinstance(c, dict) and c.get("x") is not None and c.get("y") is not None:
                        xs_by.setdefault(name, []).append(float(c["x"]))
                        ys_by.setdefault(name, []).append(float(c["y"]))
    out = {}
    for name, xs in xs_by.items():
        ys = ys_by[name]
        margin = 0.1 * max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
        out[name] = (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)
    return out


def run_selftest(hass, samples=None, snapshot=None):
    """Leave-one-out receiver self-localization accuracy against known positions.

    Solves on the RAW inter-receiver distances BPS collects for calibration
    (median per link), not the Bermuda-filtered distance sensor the live tracker
    reads — so the numbers also exclude Bermuda's own smoothing.

    Direct/synchronous callers may pass ``samples={"tx|rx": [floats]}`` or use
    the live window. Executor callers MUST pass ``_selftest_snapshot(hass)``
    taken on the event loop as ``snapshot``: registry/model access and copying
    the sample deques must finish before calibration or the layout can change.
    """
    snapshot = snapshot if snapshot is not None else _selftest_snapshot(hass, samples)
    receivers = snapshot["receivers"]
    floor_bounds = snapshot["bounds"]
    samples = snapshot["samples"]

    def _measured_m(target, rx):
        # The live tracker sees the beacon (target) transmit and a receiver hear
        # it, so use exactly that direction ("target|rx") and the receiver's gain.
        vals = [float(v) for v in samples.get(f"{target}|{rx}", [])
                if isinstance(v, (int, float)) and v > 0]
        if len(vals) < SELFTEST_MIN_SAMPLES:
            return None
        return float(np.median(vals))

    solved, unsolved = [], []
    for key, tgt in receivers.items():
        slug = tgt["entity"]
        pts = []
        for other_key, o in receivers.items():
            if other_key == key or o["floor"] != tgt["floor"]:
                continue
            if tgt["sample_slug"] is None or o["sample_slug"] is None:
                continue
            raw = _measured_m(tgt["sample_slug"], o["sample_slug"])
            if raw is None:
                continue
            d = raw * o["correction"]  # receiving scanner's correction (live path)
            horizontal = d
            if tgt["height"] is not None and o["height"] is not None:
                dz = o["height"] - tgt["height"]
                # Same floored projection as the live path (singularity guard;
                # the floor never exceeds the raw slant).
                floor_sq = min(d * d, MIN_WEIGHT_RADIUS_M * MIN_WEIGHT_RADIUS_M)
                horizontal = math.sqrt(max(d * d - dz * dz, floor_sq))
            # (x, y, projected radius, measured slant) in pixels — the slant is
            # the weight radius, mirroring the live solve.
            pts.append((o["x"], o["y"], horizontal * tgt["scale"], d * tgt["scale"]))
        if len(pts) < 3:
            unsolved.append({"entity": slug, "floor": tgt["floor"], "heard_by": len(pts)})
            continue
        fix = trilaterate(
            [(px, py, pr, 1.0, psl) for (px, py, pr, psl) in pts],
            bounds=floor_bounds.get(tgt["floor"]),
            min_weight_radius=MIN_WEIGHT_RADIUS_M * tgt["scale"],
        )
        if fix is None:
            unsolved.append({"entity": slug, "floor": tgt["floor"], "heard_by": len(pts), "reason": "no convergence"})
            continue
        err_px = math.hypot(fix[0] - tgt["x"], fix[1] - tgt["y"])
        solved.append({
            "entity": slug, "floor": tgt["floor"],
            "known": [round(tgt["x"], 1), round(tgt["y"], 1)],
            "est": [round(float(fix[0]), 1), round(float(fix[1]), 1)],
            "error_m": round(err_px / tgt["scale"], 3),
            "heard_by": len(pts),
        })
    return {
        "receivers": solved,
        "unsolved": unsolved,
        "counts": {"placed": len(receivers), "solved": len(solved), "unsolved": len(unsolved)},
    }


def _selftest_summary(result):
    """(state, attributes) for the accuracy sensor from a run_selftest result.

    State is CEP95 in metres (lower is better), or None (-> "unknown") when no
    receiver was solvable. Attributes stay compact (no per-receiver list) so
    they don't bloat recorder history; the full detail is on /api/bps/selftest.
    """
    recv = [r for r in result.get("receivers", []) if isinstance(r.get("error_m"), (int, float))]
    counts = result.get("counts", {})
    attrs = {
        "solved": counts.get("solved"),
        "placed": counts.get("placed"),
        "unsolved": counts.get("unsolved"),
    }
    if not recv:
        return None, attrs
    errs = np.array([r["error_m"] for r in recv], dtype=float)
    attrs["cep50_m"] = round(float(np.percentile(errs, 50)), 3)
    attrs["cep95_m"] = round(float(np.percentile(errs, 95)), 3)
    attrs["mean_m"] = round(float(errs.mean()), 3)
    attrs["max_m"] = round(float(errs.max()), 3)
    worst = max(recv, key=lambda r: r["error_m"])
    attrs["worst"] = f"{worst.get('entity')} ({worst.get('error_m')} m)"
    by_floor = {}
    for r in recv:
        by_floor.setdefault(r.get("floor"), []).append(r["error_m"])
    attrs["per_floor_cep95_m"] = {
        f: round(float(np.percentile(v, 95)), 3) for f, v in by_floor.items()
    }
    return attrs["cep95_m"], attrs


class BPSTrackerTuneAPI(HomeAssistantView):
    """Set one tracker's ref-power trim and apply it immediately (issue #92).

    The panel's "Save Floor Plan" writes the WHOLE layout, so it can't be used
    for live tuning: it would also commit whatever zone/receiver edits happen to
    be staged. This writes just the one field under the layout lock, so the
    tracking loop picks the new value up on its next tick (~1 s) and the map
    updates on the panel's next poll — the real-time feedback the request asked
    for — while the panel keeps its own copy in sync so a later full save agrees.
    """

    url = "/api/bps/tracker_tune"
    name = "api:bps:tracker_tune"
    requires_auth = True

    async def post(self, request):
        hass = request.app["hass"]
        try:
            body = await request.json()
        except Exception:
            # Not just JSONDecodeError: a bad charset raises UnicodeDecodeError,
            # which is a sibling ValueError and would otherwise escape as a 500.
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            # A valid non-object body ([], null, 5) parses fine but has no .get.
            return web.json_response({"error": "Body must be a JSON object"}, status=400)

        entity = body.get("entity")
        if not isinstance(entity, str) or not entity:
            return web.json_response({"error": "entity is required"}, status=400)

        raw = body.get("ref_offset_db")
        offset = None  # None = clear the trim for this tracker
        if raw is not None:
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return web.json_response({"error": "ref_offset_db must be a number"}, status=400)
            try:
                # Coerce FIRST: a JSON integer literal with hundreds of digits
                # parses to a Python int that math.isfinite() can't convert,
                # raising OverflowError (a 500) for what is just out of range.
                value = float(raw)
            except (OverflowError, ValueError):
                return web.json_response(
                    {"error": f"ref_offset_db must be within +/-{TRACKER_REF_OFFSET_MAX_DB} dB"},
                    status=400,
                )
            if not math.isfinite(value) or abs(value) > TRACKER_REF_OFFSET_MAX_DB:
                return web.json_response(
                    {"error": f"ref_offset_db must be within +/-{TRACKER_REF_OFFSET_MAX_DB} dB"},
                    status=400,
                )
            offset = value

        async with BPS_FILE_LOCK:
            coords = get_bps_data_for_edit(hass)
            if not isinstance(coords, dict):
                return web.json_response({"error": "No layout saved yet"}, status=400)
            offsets = coords.get("tracker_ref_offsets")
            if not isinstance(offsets, dict):
                offsets = {}
            if offset is None or offset == 0.0:
                offsets.pop(entity, None)  # unset -> back to no trim
            else:
                offsets[entity] = offset
            coords["tracker_ref_offsets"] = offsets
            await save_bps_data(hass, coords)

        applied = 0.0 if offset is None else offset
        return web.json_response({
            "entity": entity,
            "ref_offset_db": applied,
            "distance_factor": round(10.0 ** (applied / (10.0 * PATH_LOSS_EXPONENT)), 4),
        })


# Points returned per history query when the caller does not ask. Enough to
# draw a smooth trail on a floor plan; the decimation still spans the window.
HISTORY_DEFAULT_POINTS = 3000
HISTORY_MAX_QUERY_POINTS = 20000


class BPSHistoryAPI(HomeAssistantView):
    """Past positions of a tracked device, for the map's time scrubber.

    GET with no ``entity`` returns the index (what is retained, for whom, plus
    the effective settings) so the panel can populate its picker and size the
    slider. GET with an ``entity`` returns that tracker's trail over
    [from, to], decimated to ``max_points``.

    Coordinates come back in METRES in the named floor's frame together with
    that floor's scale, so the panel projects them with the CURRENT map scale
    and a re-exported floor plan does not misplace the past.
    """

    url = "/api/bps/history"
    name = "api:bps:history"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass

    @staticmethod
    def _num(raw, default):
        """A query param as a float, falling back rather than 400-ing on junk."""
        if raw is None:
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            return default
        return value if math.isfinite(value) else default

    async def get(self, request):
        hass = self.hass
        hist = get_position_history(hass)
        hist.configure(history_mod.history_config(get_bps_data(hass)))
        # Drop anything now outside the window before answering: record() only
        # ages the tracker it touched, so a device that stopped reporting would
        # otherwise still be served long past the configured retention.
        hist.evict_all()
        cfg = dict(hist.cfg)
        now = time.time()

        entity = request.query.get("entity")
        if not entity:
            files, size = await hass.async_add_executor_job(
                history_mod.disk_usage, history_dir(hass))
            return web.json_response({
                "now": now,
                "config": cfg,
                "trackers": [dict(hist.retained(e) or {}, ent=e) for e in hist.entities()],
                "disk": {"files": files, "bytes": size},
                "pending": hist.pending_count(),
                "dropped": hist.dropped_pending,
            })

        to = self._num(request.query.get("to"), now)
        # Default window = the whole retained span, so the slider opens showing
        # everything there is rather than an arbitrary slice.
        frm = self._num(request.query.get("from"), to - cfg["max_age"])
        if frm > to:
            frm, to = to, frm
        max_points = int(min(max(2.0, self._num(request.query.get("max_points"),
                                                HISTORY_DEFAULT_POINTS)),
                             HISTORY_MAX_QUERY_POINTS))

        data = hist.query(entity, frm, to, max_points)
        data["now"] = now
        data["from"] = frm
        data["to"] = to
        data["retained"] = hist.retained(entity)
        data["config"] = cfg
        return web.json_response(data)

    async def post(self, request):
        """Clear the record — for one tracker, or all of it.

        Settings live in the layout (``history_*``) and are written by the
        normal floor-plan save; the only stateful action worth its own endpoint
        is forgetting, which nothing else can do.
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "Body must be a JSON object"}, status=400)
        if body.get("action") != "clear":
            return web.json_response({"error": "Unsupported action"}, status=400)

        hass = self.hass
        hist = get_position_history(hass)
        entity = body.get("entity")
        if entity is not None and not isinstance(entity, str):
            return web.json_response({"error": "entity must be a string"}, status=400)

        dirpath = history_dir(hass)
        # Under the flush lock from end to end: otherwise a flush that has
        # already drained its rows lands after the delete and puts the
        # forgotten positions straight back on disk.
        async with _history_lock(hass):
            # Forget in memory FIRST - this also drops that tracker's queued
            # rows, so the next flush cannot write back what we just erased.
            hist.forget(entity or None)
            if entity:
                # Segments are shared by every tracker, so a per-tracker clear
                # cannot just delete files: rewrite them without its rows.
                removed = await hass.async_add_executor_job(
                    history_mod.drop_entity, dirpath, entity)
            else:
                removed = await hass.async_add_executor_job(
                    history_mod.clear_segments, dirpath)
        return web.json_response({"cleared": entity or "*", "removed": removed})


class BPSSelfTestAPI(HomeAssistantView):
    """Read-only receiver leave-one-out self-localization accuracy (see run_selftest)."""

    url = "/api/bps/selftest"
    name = "api:bps:selftest"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass

    async def get(self, request):
        # Bind aliases and detach geometry/samples before leaving the loop.
        snapshot = _selftest_snapshot(self.hass)
        data = await self.hass.async_add_executor_job(run_selftest, None, None, snapshot)
        return web.json_response(data)
