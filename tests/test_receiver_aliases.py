"""Synthetic owned filtered receiver aliases through real tracking/calibration paths."""
import asyncio
import copy
import datetime
import json
import math
from types import SimpleNamespace as NS

import pytest

import bps
from bps import calibration as C
from bps.receiver_sources import ReceiverSources
from test_receiver_matching import configure, MAC


def timestamp(value):
    return datetime.datetime.fromtimestamp(value, datetime.timezone.utc)


def aliases(hass, monkeypatch, *, outdoor=False):
    entries, states, devices, add = configure(hass, monkeypatch)
    monkeypatch.setattr(bps.time, "time", lambda: 1000.)
    for name in ("apitricords", "tracked_entities"):
        monkeypatch.setattr(bps, name, [])
    for name in ("_kf_position_state", "_floor_probability", "_floor_challenge", "_floor_dark_cycles"):
        monkeypatch.setattr(bps, name, {})
    for name in ("last_floor", "last_r_values"):
        monkeypatch.setattr(bps.update_trilateration_and_zone, name, {}, raising=False)
    devices["station"] = NS(_is_scanner=True, address=MAC, name="Current station", adverts={})
    data = {"floor": [{"name": "Ground", "scale": 10, "zones": [], "receivers": [{
        "entity_id": "old_station", "cords": {"x": 10, "y": 20}, "height": 2,
        "correction": 2, "hardware_model": "Synthetic hardware", "custom": {"keep": True}}]}]}
    if outdoor:
        data["outdoor_tracking"] = {"enabled": True}
    hass.data["bps"]["layout"] = copy.deepcopy(data)
    old, new = "sensor.tag_distance_to_old_station", "sensor.tag_distance_to_current_station"
    add(old, "unavailable"); add(new, "4")
    states[old].last_updated, states[new].last_updated = timestamp(990), timestamp(998)
    return data, entries, states, devices, add, old, new


@pytest.mark.parametrize("outdoor", [False, True])
@pytest.mark.parametrize("saved_name", ["old_station", "current_station"])
def test_old_and_current_scanner_names_use_fresh_filtered_source_with_original_clock_and_metadata(hass, monkeypatch, outdoor, saved_name):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch, outdoor=outdoor)
    rec = data["floor"][0]["receivers"][0]; rec["entity_id"] = saved_name
    before = copy.deepcopy(rec)
    states[old].state = "20"; states[old].last_updated = timestamp(900)
    hass.data["bps"]["_diagnostics"] = NS(active=True, sources={"tag"}, members={})
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    assert rec["distance"] == 8 and rec["cords"]["r"] == pytest.approx(10 * math.sqrt(63))
    assert rec["_diagnostic_reading"]["observed"] == 998
    assert rec["_diagnostic_reading"]["entity_id"] == new
    assert rec["entity_id"] == saved_name and rec["custom"] == before["custom"] and rec["height"] == before["height"]
    assert rec["hardware_model"] == before["hardware_model"] and rec["correction"] == 2
    if outdoor:
        assert rec["_outdoor_reading"]["observed"] == 998 and rec["_outdoor_reading"]["reading_age_s"] == 2
    hass.data["bps"]["layout"] = data
    row = bps._diagnostic_snapshot(hass, ["tag"], 1000)["readings"][0]
    assert row["entity_id"] == new and row["observed"] == 998 and row["age_s"] == 2
    assert row["corrected_distance_m"] == 8 and row["receiver"] == saved_name
    assert set(row["scanner_aliases"]) == {"old_station", "current_station"}
    assert not hass._store_saves


def test_current_name_can_use_old_filtered_entity_when_current_entity_is_absent(hass, monkeypatch):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch)
    del entries[new]; del states[new]
    states[old].state = "3"; states[old].last_updated = timestamp(999)
    rec = data["floor"][0]["receivers"][0]; rec["entity_id"] = "current_station"
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    assert rec["distance"] == 6
    assert bps._scanner_linking(hass, json.dumps(data))["placed"][0]["selected_sources"] == {"tag": old}


def test_saved_mac_and_owner_survive_removed_old_entities_without_rewriting_name(hass, monkeypatch):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch)
    del entries[old]; del states[old]
    rec = data["floor"][0]["receivers"][0]; rec.update(scanner_mac=MAC, scanner_config_entry_id="entry")
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    assert rec["entity_id"] == "old_station" and rec["distance"] == 8
    diagnostic = bps._scanner_diagnostics(hass, json.dumps(data))
    assert diagnostic["unmatched_receivers"] == [] and diagnostic["alias_receivers"][0]["scanner_mac"] == MAC
    rec["scanner_config_entry_id"] = "wrong-owner"
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    assert "distance" not in rec
    assert bps._scanner_diagnostics(hass, json.dumps(data))["placement_issues"][0]["status"] == "scanner_identity_unavailable"


@pytest.mark.parametrize("invalid", ["raw", "disabled", "other_integration", "future", "different_tracker"])
def test_invalid_alias_source_cannot_displace_trusted_exact_source(hass, monkeypatch, invalid):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch)
    states[old].state = "3"; states[old].last_updated = timestamp(995)
    if invalid == "raw": entries[new].unique_id += "_raw"
    elif invalid == "disabled": entries[new].disabled_by = "user"
    elif invalid == "other_integration": entries[new].platform = "esphome"
    elif invalid == "future": states[new].last_updated = timestamp(1005)
    elif invalid == "different_tracker": entries[new].unique_id = "another-synthetic-tag_" + MAC + "_range"
    resolver = ReceiverSources(hass)
    assert resolver.source("tag", data["floor"][0]["receivers"][0])[0] == old
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    assert data["floor"][0]["receivers"][0]["distance"] == 6


def test_future_or_stale_only_source_cannot_keep_previous_distance(hass, monkeypatch):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch)
    rec = data["floor"][0]["receivers"][0]
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data})); assert rec["distance"] == 8
    states[new].last_updated = timestamp(1005)
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data})); assert "distance" not in rec
    states[new].last_updated = timestamp(900)
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data})); assert "distance" not in rec


@pytest.mark.parametrize("outdoor", [False, True])
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1"])
def test_invalid_numeric_aliases_cannot_retain_a_previous_radius(hass, monkeypatch, outdoor, value):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch, outdoor=outdoor)
    rec = data["floor"][0]["receivers"][0]
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data})); assert rec["distance"] == 8
    states[old].state = states[new].state = value
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    assert "distance" not in rec and "r" not in rec["cords"]


def test_unknown_clock_alias_does_not_invent_sparse_freshness(hass, monkeypatch):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch, outdoor=True)
    del states[new].last_updated
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    rec = data["floor"][0]["receivers"][0]
    assert rec["_outdoor_reading"]["observed"] is None
    from bps.sparse_tracking import solve_sparse_detection
    assert solve_sparse_detection(data["floor"][0], [(10, 20, rec["cords"]["r"], 1, 80)], 10, 30, now=1000) is None


@pytest.mark.parametrize("conflict", [False, True])
def test_duplicate_aliases_count_once_or_exclude_conflicting_physical_placements(hass, monkeypatch, conflict):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch)
    floor = data["floor"][0]
    duplicate = copy.deepcopy(floor["receivers"][0]); duplicate["entity_id"] = "current_station"
    if conflict: duplicate["cords"]["x"] = 200
    floor["receivers"].append(duplicate)
    before = copy.deepcopy(floor)
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    candidates = bps.extract_candidate_floors([{"entity": "tag", "data": data}], "tag")
    assert (len(candidates[0]["cords"]) if candidates else 0) == (0 if conflict else 1)
    issues = bps._scanner_diagnostics(hass, json.dumps(data))["placement_issues"]
    assert issues and "Keep one placement" in issues[0]["action"]
    assert issues[0]["status"] == ("conflicting_scanner_placements" if conflict else "duplicate_scanner_alias")
    assert [rec["cords"]["x"] for rec in floor["receivers"]] == [rec["cords"]["x"] for rec in before["receivers"]]
    assert len(C._build_receiver_map(data, hass=hass)) == (0 if conflict else 1)


@pytest.mark.parametrize("collision", ["different_mac", "different_owner", "token_only"])
def test_scanner_collision_never_creates_equivalent_identity(hass, monkeypatch, collision):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch)
    states[old].state = "3"
    if collision == "different_mac":
        entries[new].unique_id = "synthetic-tracker_dd:ee:ff:11:22:33_range"
        devices["station"].address = "dd:ee:ff:11:22:33"
    elif collision == "different_owner": entries[new].config_entry_id = "second-owner"
    else:
        entries[old].unique_id = "legacy-no-proof"
        data["floor"][0]["receivers"][0]["scanner_uid"] = "112233"
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    assert data["floor"][0]["receivers"][0]["distance"] == 6


def test_sparse_tracking_group_and_diagnostics_keep_alias_source_clock(hass, monkeypatch):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch, outdoor=True)
    data["tracker_groups"] = [{"id": "rover", "beacons": ["tag"]}]
    hass.data["bps"]["layout"] = data
    item = {"entity": "tag", "data": copy.deepcopy(data)}
    asyncio.run(bps.process_single_entity(hass, [item], item))
    assert bps.apitricords[0]["outdoor"]["observed"] == 998
    assert bps.apitricords[0]["outdoor"]["receivers_used"] == 1
    asyncio.run(bps.update_tracker_groups(hass))
    group = bps.apitricords[-1]
    assert group["group"] and group["outdoor"]["observed"] == 998
    assert group["outdoor"]["detection_receiver"] == "old_station"
    assert group["cords"] == [10, 20]


def test_calibration_alias_geometry_maps_one_physical_scanner_and_ingests_raw_advert_once(hass, monkeypatch):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch)
    floor = data["floor"][0]; duplicate = copy.deepcopy(floor["receivers"][0]); duplicate["entity_id"] = "current_station"; floor["receivers"].append(duplicate)
    other_mac = "aa:bb:cc:44:55:66"
    add("sensor.tag_distance_to_other_station", "5", uid="synthetic-tracker_" + other_mac + "_range")
    devices["other"] = NS(_is_scanner=True, address=other_mac, name="Other station", adverts={})
    floor["receivers"].append({"entity_id": "other_station", "cords": {"x": 100, "y": 20}})
    cal = {"receivers": C._build_receiver_map(data, hass=hass), "samples": {}}
    assert set(cal["receivers"]) == {"old_station", "other_station"}
    payload = {MAC: {"_is_scanner": True, "address": MAC, "name": "Current station", "adverts": {
        "first": {"scanner_address": other_mac, "stamp": 100, "rssi_distance_raw": 7},
        "alias_copy": {"scanner_address": other_mac, "stamp": 100, "rssi_distance_raw": 7},
        "stale": {"scanner_address": other_mac, "stamp": 90, "rssi_distance_raw": 20}}},
        other_mac: {"_is_scanner": True, "address": other_mac, "name": "Other station", "adverts": {}}}
    C._ingest_dump(cal, payload); C._ingest_dump(cal, payload)
    assert list(cal["samples"]["old_station|other_station"]) == [7]
    assert "current_station" not in cal["matched_placed"]


def test_normal_solve_and_group_distances_contain_one_input_per_physical_scanner(hass, monkeypatch):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch)
    floor = data["floor"][0]
    first = floor["receivers"][0]; first.update(height=1, correction=1); first["cords"] = {"x": 0, "y": 0}
    clone = copy.deepcopy(first); clone["entity_id"] = "current_station"; clone["cords"]["r"] = 9999; clone["distance"] = 999
    floor["receivers"].append(clone)
    states[new].state = str(math.sqrt(50))
    for slug, mac, point in [("east", "aa:bb:cc:44:55:66", (100, 0)), ("north", "aa:bb:cc:77:88:99", (0, 100))]:
        eid = f"sensor.tag_distance_to_{slug}"; add(eid, str(math.sqrt(50)), uid="synthetic-tracker_" + mac + "_range")
        states[eid].last_updated = timestamp(998)
        devices[slug] = NS(_is_scanner=True, address=mac, name=slug, adverts={})
        floor["receivers"].append({"entity_id": slug, "cords": {"x": point[0], "y": point[1]}, "height": 1})
    data["tracker_groups"] = [{"id": "rover", "beacons": ["tag"]}]
    hass.data["bps"]["layout"] = data
    hass.data["bps"]["_diagnostics"] = NS(active=True, sources={"tag"}, members={})
    item = {"entity": "tag", "data": copy.deepcopy(data)}
    asyncio.run(bps.process_single_entity(hass, [item], item))
    position = bps.apitricords[0]
    assert position["raw"] == pytest.approx([50, 50], abs=.02)
    assert len(position["radii"]) == 3 and len(position["diagnostic_inputs"]) == 3
    assert {row["receiver"] for row in position["diagnostic_inputs"]} == {"old_station", "east", "north"}
    assert position["diagnostic_inputs"][0]["entity_id"] == new
    assert "r" not in item["data"]["floor"][0]["receivers"][1]["cords"]
    data["outdoor_tracking"] = {"enabled": True}
    asyncio.run(bps.update_tracker_groups(hass))
    group = bps.apitricords[-1]
    assert group["group"] and group["beacon_positions"][0]["ent"] == "tag"
    assert group["beacon_positions"][0]["used"]
    assert len(bps.apitricords[0]["radii"]) == 3
    report = bps._diagnostic_snapshot(hass, ["bps_group_rover"], 1000)
    assert report["readings"][1]["status"] == "duplicate_scanner_alias"
    assert "corrected_distance_m" not in report["readings"][1]


def test_unfiltered_alias_entity_and_secret_tracker_identity_never_leak_into_linking(hass, monkeypatch):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch)
    raw = "sensor.tag_distance_to_raw_station"
    add(raw, "123", uid="synthetic-secret_" + MAC + "_range_raw")
    for eid in (old, new): entries[eid].unique_id = "synthetic-secret_" + MAC + "_range"
    payload = bps._scanner_linking(hass, json.dumps(data))
    assert raw not in json.dumps(payload) and "synthetic-secret" not in json.dumps(payload)
    assert payload["placed"][0]["selected_sources"] == {"tag": new}


def test_bermuda_calibration_model_uses_proven_saved_alias_mac_and_owner(hass, monkeypatch):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch)
    from bps.bermuda_calibration import runtime_context
    devices["station"].adverts = {"ad": NS(scanner_address=MAC, conf_attenuation=2,
        conf_rssi_offset=-3, conf_ref_power=-60, ref_power=-60)}
    row = runtime_context(hass, data, "Ground")["receivers"]["old_station"]
    assert row["scanner_mac"] == MAC and row["entry_id"] == "entry"
    assert row["scanner_name"] == "Current station" and row["attenuation"] == 2
    assert row["geometry"]["scanner_config_entry_id"] == "entry"


def test_saved_alias_liveness_and_readonly_api_use_proven_current_scanner(hass, monkeypatch):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch)
    del entries[old]; del states[old]
    rec = data["floor"][0]["receivers"][0]; rec.update(scanner_mac=MAC, scanner_config_entry_id="entry")
    hass.data["bps"]["layout"] = copy.deepcopy(data)
    async def call(*args, **kwargs):
        return {MAC: {"_is_scanner": True, "address": MAC, "name": "Current station", "last_seen": 500}}
    hass.services.async_call = call
    asyncio.run(bps.update_receiver_liveness(hass))
    assert "old_station" not in hass.data["bps"]["rl_offline"] and "old_station" not in hass.data["bps"]["rl_unknown"]
    before = copy.deepcopy(hass.data["bps"]["layout"])
    response = asyncio.run(bps.BPSReadAPIText().get(NS(app={"hass": hass})))
    aliases_ = response.json_body["scanner_diagnostics"]["alias_receivers"]
    assert aliases_[0]["entity_id"] == "old_station" and aliases_[0]["aliases"] == ["current_station"]
    assert hass.data["bps"]["layout"] == before and not hass._store_saves


def test_legacy_exact_calibration_name_stays_usable_without_alias_mac_proof(hass, monkeypatch):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch)
    entries[old].unique_id = "legacy-format"
    devices.clear()
    receivers = C._build_receiver_map(data, hass=hass)
    payload = {MAC: {"_is_scanner": True, "address": MAC, "name": "old_station", "adverts": {}}}
    assert C._match_scanners({"receivers": receivers}, payload) == {MAC: "old_station"}
    payload["second"] = {"_is_scanner": True, "address": "dd:ee:ff:11:22:33", "name": "old_station", "adverts": {}}
    assert C._match_scanners({"receivers": receivers}, payload) == {}
    payload = {MAC: {"_is_scanner": True, "address": MAC, "name": "new_name_112233", "adverts": {}}}
    receivers["old_station"]["uid"] = "112233"
    assert C._match_scanners({"receivers": receivers}, payload) == {}, 'token alone cannot expand calibration aliases'


@pytest.mark.parametrize("outdoor", [False, True])
@pytest.mark.parametrize("runtime_identity", [False, True])
def test_exact_owned_legacy_reading_survives_runtime_identity_without_unproven_alias_expansion(hass, monkeypatch, outdoor, runtime_identity):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch, outdoor=outdoor)
    entries[old].unique_id = "legacy-format"
    states[old].state = "3"; states[old].last_updated = timestamp(995)
    devices["station"].name = "old_station"
    if not runtime_identity:
        devices.clear()
    resolver = ReceiverSources(hass)
    rec = data["floor"][0]["receivers"][0]
    assert resolver.source("tag", rec)[0] == old, 'fresh alias cannot substitute an unproven tracker identity'
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    assert rec["distance"] == 6
    hass.data["bps"]["layout"] = data
    row = bps._diagnostic_snapshot(hass, ["tag"], 1000)["readings"][0]
    assert row["entity_id"] == old and row["observed"] == 995 and row["age_s"] == 5
    states[old].last_updated = timestamp(900)
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    assert "distance" not in rec, 'legacy source does not rejuvenate its original clock'


@pytest.mark.parametrize("invalid", ["different_owner", "raw", "disabled", "foreign"])
def test_runtime_identity_does_not_admit_unowned_or_disabled_exact_legacy_sources(hass, monkeypatch, invalid):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch)
    del entries[new]; del states[new]
    devices["station"].name = "old_station"
    entries[old].unique_id = "legacy-format"
    states[old].state = "3"
    if invalid == "different_owner": entries[old].config_entry_id = "other-owner"
    elif invalid == "raw": entries[old].unique_id = "legacy_range_raw"
    elif invalid == "disabled": entries[old].disabled_by = "user"
    elif invalid == "foreign": entries[old].platform = "esphome"
    resolver = ReceiverSources(hass)
    assert resolver.source("tag", data["floor"][0]["receivers"][0])[1] is None
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    assert "distance" not in data["floor"][0]["receivers"][0]
