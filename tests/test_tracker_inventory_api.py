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
