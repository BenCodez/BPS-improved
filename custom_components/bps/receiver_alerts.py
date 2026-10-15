"""Three-hour silence notices for placed, identity-proven BLE receivers."""
import asyncio
import copy
import logging
import math
import time

from homeassistant.helpers.storage import Store

from .storage import finish_persistence, save_verified_store

THRESHOLD_S = 3 * 60 * 60
FLUSH_INTERVAL_S = 5 * 60
MAX_RECEIVERS = 4096
STORAGE_KEY = "bps_receiver_activity"
NOTIFICATION_ID = "bps_receiver_silence"
_LOGGER = logging.getLogger(__name__)


def _number(value):
    try:
        return (float(value) if not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value) and value >= 0 else None)
    except OverflowError:
        return None


def scanner_activity(hass, sources, identity, dump):
    """Return (reception age, scanner known) independently of map validity.

    Prefer HA's scanner clock: Bermuda's device last_seen can also advance
    when another scanner hears the receiver's own beacon transmission.
    """
    addresses = {address for address, keys in sources.by_ble_address.items() if identity in keys}
    rows = sources.direct.scanners.get(identity, [])
    if len(rows) == 1:
        addresses.add(rows[0][0])
    if not addresses:
        addresses.add(identity[1])
    try:
        from homeassistant.components.bluetooth import async_scanner_by_source
    except ImportError:
        pass
    else:
        ages, known = [], False
        try:
            for address in addresses:
                scanner = async_scanner_by_source(hass, address.upper())
                if scanner is not None and scanner.source.lower() == address.lower():
                    known = True
                    if (age := _number(scanner.time_since_last_detection())) is not None:
                        ages.append(age)
        except Exception:
            # A missing manager or indeterminate source cannot prove activity.
            return None, False
        return (min(ages) if ages else None), known

    age = _number(sources.direct.scanner_age(identity))
    if age is not None:
        return age + max(0.0, time.time() - sources.direct.now), True
    if len(rows) == 1:
        return None, True
    if len(sources.owners_by_mac.get(identity[1], ())) != 1 or not isinstance(dump, dict):
        return None, False
    # Legacy last_seen uses HA's monotonic clock. Relative ages anchored to the
    # newest device in a dump can falsely make an entirely stale fleet fresh.
    stamps, known = [], False
    clock = time.monotonic()
    for address, device in dump.items():
        if not isinstance(device, dict) or device.get("_is_scanner") is not True:
            continue
        mac = device.get("address_wifi_mac") or device.get("address", address)
        stamp = _number(device.get("last_seen"))
        if isinstance(mac, str) and mac.lower() == identity[1] and stamp is not None and stamp <= clock + 2:
            known = True
            if stamp > 0:
                stamps.append(stamp)
    return (max(0.0, clock - max(stamps)) if stamps else None), known


class ReceiverAlerts:
    """One serialized monitor per HA instance; notifications have a stable ID."""

    def __init__(self, hass):
        self.hass = hass
        self.store = Store(hass, 1, STORAGE_KEY)
        self.lock = asyncio.Lock()
        self.records = None
        self.last_saved = None
        self.notified = None
        self.error = None
        self.status = {"threshold_s": THRESHOLD_S, "active": [], "monitored": 0}

    async def async_update(self, observations, now, current=lambda: True):
        async with self.lock:
            if self.records is None:
                try:
                    saved = await self.store.async_load()
                    if saved is not None and (not isinstance(saved, dict)
                            or not isinstance(saved.get("receivers"), dict)
                            or len(saved["receivers"]) > MAX_RECEIVERS):
                        raise ValueError("Invalid receiver activity store")
                    records = (saved or {}).get("receivers", {})
                    if any(not isinstance(key, str) or len(key) != 64 or not isinstance(row, dict)
                           or _number(row.get("last_seen")) is None for key, row in records.items()):
                        raise ValueError("Invalid receiver activity record")
                    self.records = records
                except Exception:
                    error = "Receiver activity history could not be loaded; retrying."
                    if self.status.get("error") != error:
                        _LOGGER.warning(error)
                    self.status["error"] = error
                    return
            if not current():
                return
            records, active = {}, []
            for key, observation in list(observations.items())[:MAX_RECEIVERS]:
                age = _number(observation.get("age_s"))
                previous = self.records.get(key, {})
                # Missing distances alone do not prove a scanner is silent:
                # start monitoring only when the scanner itself is known.
                if not previous and not observation.get("scanner_activity_known"):
                    continue
                last_seen = min(previous.get("last_seen", now), now)
                # Scanner ages retain the actual source clock. A first report
                # can already prove a three-hour gap; no extra grace is needed.
                if age is not None:
                    last_seen = max(0.0, now - age) if not previous else max(last_seen, now - age)
                records[key] = {"last_seen": last_seen}
                silent = max(0.0, now - last_seen)
                if silent >= THRESHOLD_S:
                    active.append({"key": key, "name": observation["name"],
                        "receivers": observation["receivers"], "floors": observation["floors"],
                        "silent_s": silent, "last_seen": last_seen})
            signature = tuple(sorted((row["key"], row["name"], tuple(row["floors"])) for row in active))
            changed = set(records) != set(self.records)
            self.records = records
            # Save promptly at alert/recovery/configuration transitions, and at
            # most once every five minutes while receivers keep reporting.
            if changed or signature != self.notified or self.error or self.last_saved is None or now - self.last_saved >= FLUSH_INTERVAL_S:
                await self._save(now)
            if not current():
                return
            self.status = {"threshold_s": THRESHOLD_S, "active": active, "monitored": len(records),
                           "activity_unverified": len(observations) - len(records)}
            if self.error:
                self.status["error"] = self.error
            if signature != self.notified:
                from homeassistant.components import persistent_notification
                if active:
                    lines = ["BPS has received no BLE data from these placed receivers for at least 3 hours:"]
                    lines.extend(f"- {row['name']} ({', '.join(row['floors'])})" for row in active)
                    lines.append("Check power, network connectivity, BLE scanning and Bermuda. This is scanner activity, not reception of a particular tracker. The alert clears when data resumes.")
                    persistent_notification.async_create(self.hass, "\n\n".join(lines),
                        title="BPS: receivers silent for 3 hours", notification_id=NOTIFICATION_ID)
                else:
                    persistent_notification.async_dismiss(self.hass, NOTIFICATION_ID)
                self.notified = signature

    async def _save(self, now):
        payload = {"receivers": copy.deepcopy(self.records)}
        try:
            await finish_persistence(save_verified_store(self.hass, self.store, payload, key=STORAGE_KEY))
            self.last_saved = now
            self.error = None
        except Exception:
            error = "Receiver activity history could not be saved; timing may be lost on restart."
            if self.error != error:
                _LOGGER.warning(error)
            self.error = error

    async def async_flush(self):
        """Keep the last few minutes of activity through a graceful stop."""
        async with self.lock:
            if self.records is not None:
                await self._save(self.last_saved)

    def clear_notification(self):
        from homeassistant.components import persistent_notification
        persistent_notification.async_dismiss(self.hass, NOTIFICATION_ID)
        self.notified = None


def get_monitor(hass):
    domain = hass.data.setdefault("bps", {})
    if "_receiver_alerts" not in domain:
        domain["_receiver_alerts"] = ReceiverAlerts(hass)
    return domain["_receiver_alerts"]


def snapshot(hass):
    monitor = hass.data.get("bps", {}).get("_receiver_alerts")
    return copy.deepcopy(monitor.status if monitor else {"threshold_s": THRESHOLD_S, "active": [], "monitored": 0})
