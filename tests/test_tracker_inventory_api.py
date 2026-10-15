"""Device/area discovery uses synthetic, owned identities, never map edits."""

import asyncio
import copy
from types import SimpleNamespace as NS

import bps
from bps.receiver_matching import inventory
from bps.receiver_sources import ReceiverSources
from test_bermuda_direct import prepared, snapshot
from test_receiver_matching import MAC


def test_api_only_phone_is_selectable_and_refresh_does_not_modify_layout(hass, monkeypatch):
    data, entries, states, _add, _old, _new, _payloads, _calls = prepared(hass, monkeypatch)
    entries.clear()
    states.clear()
    hass.data["bps"]["layout"] = data
    before = copy.deepcopy(data)
    sources = ReceiverSources(hass)
    key = next(iter(sources.fallback_trackers))
    response = asyncio.run(bps.BPSTrackerInventoryAPI().get(NS(app={"hass": hass})))
    assert bps.BPSTrackerInventoryAPI.requires_auth
    assert response.status == 200 and response.json_body["ready"]
    assert response.json_body["trackers"] == [{"key": key, "name": "Synthetic beacon"}]
    assert data == before and not hass._store_saves


def test_saved_group_does_not_make_temporarily_unavailable_phone_inventory_ready(hass, monkeypatch):
    data, entries, states, _add, _old, _new, payloads, _calls = prepared(hass, monkeypatch)
    entries.clear()
    states.clear()
    key = next(iter(ReceiverSources(hass).fallback_trackers))
    data.update(outdoor_tracking={"enabled": True}, tracker_groups=[{
        "id": "dog", "name": "Synthetic group", "enabled": True, "beacons": [key]}])
    hass.data["bps"]["layout"] = data
    api = bps.BPSTrackerInventoryAPI()
    request = NS(app={"hass": hass})
    assert asyncio.run(api.get(request)).json_body["ready"]
    payloads["entry"] = None
    waiting = asyncio.run(api.get(request)).json_body
    assert waiting["trackers"] and all(row.get("group") for row in waiting["trackers"])
    assert waiting["ready"] is False
    payloads["entry"] = snapshot()
    assert asyncio.run(api.get(request)).json_body["ready"]


def test_partial_api_outage_retains_choices_even_when_legacy_trackers_remain(hass, monkeypatch):
    data, _entries, _states, _add, _old, _new, payloads, _calls = prepared(hass, monkeypatch)
    hass.data["bps"]["layout"] = data
    configs = hass.config_entries.async_entries("bermuda")
    hass.config_entries.async_entries = lambda _domain: [*configs, NS(entry_id="other", state="loaded")]
    payloads["other"] = None
    result = asyncio.run(bps.BPSTrackerInventoryAPI().get(NS(app={"hass": hass}))).json_body
    assert result["trackers"] and result["ready"] is False


def test_stock_bermuda_legacy_inventory_remains_refreshable(hass, monkeypatch):
    import sys
    data, _entries, _states, _add, _old, _new, _payloads, _calls = prepared(hass, monkeypatch)
    hass.data["bps"]["layout"] = data
    monkeypatch.setitem(sys.modules, "custom_components.bermuda.api", NS())
    result = asyncio.run(bps.BPSTrackerInventoryAPI().get(NS(app={"hass": hass}))).json_body
    assert result["trackers"] and result["ready"] is True


def test_receiver_area_metadata_is_scoped_to_owner_and_clears_when_absent(hass, monkeypatch):
    _data, entries, _states, add, _old, _new, payloads, _calls = prepared(hass, monkeypatch)
    first = inventory(hass)
    option = next(row for row in first["options"] if row["entity_id"] == "current_station")
    assert option["area_name"] == "Synthetic room"
    assert option["scanner_identities"][0]["area_name"] == "Synthetic room"
    configs = hass.config_entries.async_entries("bermuda")
    second = NS(entry_id="other_entry", state="loaded")
    hass.config_entries.async_entries = lambda _domain: [*configs, second]
    payloads["other_entry"] = snapshot(entry="other_entry")
    payloads["other_entry"]["scanners"][MAC].update(area_id="other_area", area_name="Other synthetic area")
    add("sensor.other_tag_distance_to_current_station", None)
    entries["sensor.other_tag_distance_to_current_station"].config_entry_id = "other_entry"
    result = inventory(hass)
    option = next(row for row in result["options"] if row["entity_id"] == "current_station")
    assert option["identity_ambiguous"] and "area_name" not in option
    by_owner = {proof["config_entry_id"]: proof["area_name"] for proof in option["scanner_identities"]}
    assert by_owner == {"entry": "Synthetic room", "other_entry": "Other synthetic area"}
    for payload in payloads.values():
        payload["scanners"][MAC].pop("area_name", None)
    cleared = next(row for row in inventory(hass)["options"] if row["entity_id"] == "current_station")
    assert cleared["area_names"] == [] and "area_name" not in cleared
    assert all("area_name" not in proof for proof in cleared["scanner_identities"])
