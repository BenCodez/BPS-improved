"""The toolbar writes only its mode, with revision and durability safeguards."""
import asyncio
import copy

import pytest

from bps import storage
from bps.tracking_settings import BPSTrackingSettingsAPI


class Request:
    def __init__(self, hass, body):
        self.app = {"hass": hass}
        self.body = body

    async def json(self):
        return self.body


def layout(hass):
    value = {"floor": [{"name": "Synthetic", "receivers": [{"entity_id": "station", "correction": 1.4}]}],
             "outdoor_tracking": {"show_uncertainty": False, "custom": "preserve"}, "custom": {"keep": True}}
    hass.data.setdefault("bps", {})["layout"] = value
    return value


def test_toolbar_mode_persists_only_requested_settings_and_returns_current_revision(hass):
    async def scenario():
        old = layout(hass)
        before = copy.deepcopy(old)
        api = BPSTrackingSettingsAPI()
        for mode in (True, False):
            response = await api.post(Request(hass, {"long_range_intervals": mode,
                "layout_revision": storage.layout_revision(storage.get_bps_data(hass))}))
            assert response.status == 200
            saved = storage.get_bps_data(hass)
            assert saved["floor"] == before["floor"] and saved["custom"] == before["custom"]
            assert saved["outdoor_tracking"] == {**before["outdoor_tracking"], "enabled": True, "long_range_intervals": mode}
            assert response.json_body["layout_revision"] == storage.layout_revision(saved)
            assert hass._store_backing[storage.STORAGE_KEY_LAYOUT] == saved
        assert old == before
    asyncio.run(scenario())


@pytest.mark.parametrize("body", [None, [], {"long_range_intervals": 1, "layout_revision": "x"},
    {"long_range_intervals": True}, {"long_range_intervals": True, "layout_revision": "x", "coordinates": {}}])
def test_bad_or_broad_requests_never_write(hass, body):
    layout(hass)
    assert asyncio.run(BPSTrackingSettingsAPI().post(Request(hass, body))).status == 400
    assert not hass._store_saves


def test_stale_revision_and_unverified_storage_do_not_overwrite_current_settings(hass):
    value = layout(hass)
    body = {"long_range_intervals": True, "layout_revision": "stale"}
    assert asyncio.run(BPSTrackingSettingsAPI().post(Request(hass, body))).status == 409
    body["layout_revision"] = storage.layout_revision(value)
    hass.data["bps"]["layout_needs_reload"] = True
    assert asyncio.run(BPSTrackingSettingsAPI().post(Request(hass, body))).status == 503
    assert not hass._store_saves


def test_unacknowledged_write_reports_failure_and_preserves_live_cache(hass):
    async def scenario():
        old = layout(hass)
        async def drop(_data):
            pass
        storage._layout_store(hass).async_save = drop
        response = await BPSTrackingSettingsAPI().post(Request(hass, {"long_range_intervals": True,
            "layout_revision": storage.layout_revision(old)}))
        assert response.status == 503
        assert storage.get_bps_data(hass) is old
        assert storage.layout_needs_reload(hass)
    asyncio.run(scenario())


def test_cancelled_caller_keeps_writer_lock_until_disk_and_cache_agree(hass):
    async def scenario():
        old = layout(hass)
        entered, release = asyncio.Event(), asyncio.Event()
        store = storage._layout_store(hass)
        original = store.async_save
        async def delayed(data):
            entered.set()
            await release.wait()
            await original(data)
        store.async_save = delayed
        task = asyncio.create_task(BPSTrackingSettingsAPI().post(Request(hass, {"long_range_intervals": True,
            "layout_revision": storage.layout_revision(old)})))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert storage.BPS_FILE_LOCK.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert storage.get_bps_data(hass)["outdoor_tracking"]["long_range_intervals"] is True
        assert not storage.BPS_FILE_LOCK.locked()
    asyncio.run(scenario())


def test_endpoint_is_authenticated():
    assert BPSTrackingSettingsAPI.requires_auth is True
