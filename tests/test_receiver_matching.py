"""Synthetic receiver identity, status proof, ownership and read-only API checks."""
import asyncio
import copy
import json
import pytest
from types import SimpleNamespace as NS

import bps
from bps.receiver_matching import inventory


MAC = "aa:bb:cc:11:22:33"


def configure(hass, monkeypatch):
    entries, states, devices = {}, {}, {}
    registry = NS(entities=entries, async_get=entries.get)
    monkeypatch.setattr(bps.er, "async_get", lambda _h: registry, raising=False)
    monkeypatch.setattr(bps.dr, "async_get", lambda _h: NS(devices={}), raising=False)
    monkeypatch.setattr(bps, "slugify", lambda value: str(value).lower().replace(" ", "_"))
    import bps.receiver_matching as matching
    monkeypatch.setattr(matching, "slugify", bps.slugify)
    coordinator = NS(devices=devices, options={"arbitrary_secret": "synthetic-secret"})
    config = NS(entry_id="entry", runtime_data=NS(coordinator=coordinator))
    hass.config_entries = NS(async_entries=lambda _domain: [config])
    hass.states = NS(get=states.get, async_all=lambda _domain: [value for key, value in states.items() if key.startswith("sensor.")])
    hass.data["bps"] = {"layout": {"floor": [{"name": "Ground", "scale": 10, "receivers": [
        {"entity_id": "old_112233", "cords": {"x": 2, "y": 3}, "correction": 1.4}]}]}}
    async def call(*args, **kwargs):
        return {}
    hass.services = NS(async_call=call)

    def add(eid, value="unknown", platform="bermuda", disabled=None, uid=None):
        entries[eid] = NS(entity_id=eid, unique_id=uid or "synthetic-tracker_" + MAC + "_range",
                          platform=platform, config_entry_id="entry", device_id=None, disabled_by=disabled)
        if value is not None:
            states[eid] = NS(entity_id=eid, state=value, attributes={})
    return entries, states, devices, add


def test_inventory_names_mac_disabled_renamed_scanner_only_and_ownership(hass, monkeypatch):
    entries, states, devices, add = configure(hass, monkeypatch)
    devices["scanner"] = NS(_is_scanner=True, address=MAC, name="Workshop 112233", irk="synthetic-secret")
    add("sensor.tag_distance_to_workshop_112233", None, disabled="user")
    add("sensor.manually_renamed_range", "2")
    add("sensor.fake_distance_to_unrelated", platform="esphome")
    add("sensor.raw_distance_to_noise", uid="synthetic-tracker_" + MAC + "_range_raw")
    devices["scanner_only"] = NS(_is_scanner=True, address="aa:bb:cc:44:55:66", name="Atrium 445566")
    result = inventory(hass)
    by_slug = {r["entity_id"]: r for r in result["options"]}
    assert set(by_slug) == {"workshop_112233", "atrium_445566"}
    row = by_slug["workshop_112233"]
    assert row["name"] == "Workshop 112233" and row["scanner_mac"] == MAC
    assert row["token"] == "112233" and MAC in row["label"]
    assert row["disabled_sensors"] == ["sensor.tag_distance_to_workshop_112233"]
    assert row["renamed_sensors"] == ["sensor.manually_renamed_range"]
    assert "synthetic-secret" not in json.dumps(result) and "synthetic-tracker" not in json.dumps(result)
    states["sensor.manually_renamed_range"].state = "6"
    assert inventory(hass)["revision"] == result["revision"]
    entries["sensor.manually_renamed_range"].disabled_by = "user"
    assert inventory(hass)["revision"] != result["revision"]


def test_conflicting_bermuda_owners_disable_identity_choice(hass, monkeypatch):
    entries, states, devices, add = configure(hass, monkeypatch)
    add("sensor.a_distance_to_workshop")
    add("sensor.b_distance_to_workshop")
    entries["sensor.b_distance_to_workshop"].config_entry_id = "second_entry"
    row = inventory(hass)["options"][0]
    assert row["identity_ambiguous"] and not row["selectable"] and row["scanner_mac"] is None


def test_unknown_is_not_offline_and_confirmed_connectivity_is_offline(hass, monkeypatch):
    entries, states, devices, add = configure(hass, monkeypatch)
    add("sensor.tag_distance_to_workshop_112233")
    devices["scanner"] = NS(_is_scanner=True, address=MAC, name="Workshop 112233")
    asyncio.run(bps.update_receiver_liveness(hass))
    assert hass.data["bps"]["rl_offline"] == []
    assert hass.data["bps"]["rl_unknown"] == ["workshop_112233"]
    states["binary_sensor.workshop_112233_status"] = NS(state="off", attributes={"device_class": "connectivity"})
    asyncio.run(bps.update_receiver_liveness(hass))
    assert hass.data["bps"]["rl_offline"] == ["workshop_112233"]
    states["binary_sensor.workshop_112233_status"].state = "unknown"
    asyncio.run(bps.update_receiver_liveness(hass))
    assert hass.data["bps"]["rl_offline"] == []


def test_online_renamed_scanner_uses_proven_name_without_rewriting_layout(hass, monkeypatch):
    entries, states, devices, add = configure(hass, monkeypatch)
    add("sensor.tag_distance_to_former_112233", None, disabled="user")
    devices["scanner"] = NS(_is_scanner=True, address=MAC, name="Workshop 112233")
    hass.data["bps"].update(rl_ages={"workshop_112233": 0}, rl_anchor_wall=bps.time.time(), rl_newest=1)
    asyncio.run(bps.update_receiver_liveness(hass))
    assert hass.data["bps"]["rl_offline"] == [] and hass.data["bps"]["rl_unknown"] == []
    content = json.dumps(hass.data["bps"]["layout"])
    data = bps._scanner_linking(hass, content)
    assert data["placed"][0]["status"] == "unmatched"
    diag = bps._scanner_diagnostics(hass, content)
    assert diag["unmatched_receivers"][0]["suggested"] == "former_112233"
    assert bps._suggest_scanner("similar_station", None, ["similar_stations"]) is None


def test_authenticated_linking_and_read_text_include_matching_read_only(hass, monkeypatch):
    entries, states, devices, add = configure(hass, monkeypatch)
    add("sensor.tag_distance_to_workshop_112233", "3")
    request = NS(app={"hass": hass})
    before = copy.deepcopy(hass.data["bps"]["layout"])
    for api in (bps.BPSScannerLinkingAPI(), bps.BPSReadAPIText()):
        assert api.requires_auth
        response = asyncio.run(api.get(request))
        data = response.json_body
        matching = data.get("matching", data.get("receiver_matching"))
        assert matching["read_only"] and matching["options"][0]["entity_id"] == "workshop_112233"
    assert hass.data["bps"]["layout"] == before and not hass._store_saves


def test_renamed_only_entities_are_reported_but_cannot_create_nonfunctional_match(hass, monkeypatch):
    entries, states, devices, add = configure(hass, monkeypatch)
    devices["scanner"] = NS(_is_scanner=True, address=MAC, name="Workshop 112233")
    add("sensor.custom_distance_name", "3")
    option = inventory(hass)["options"][0]
    assert option["entity_id"] == "workshop_112233" and not option["selectable"]
    assert "Restore Bermuda distance entity IDs" in option["warning"]
    content = json.dumps({"floor": [{"name": "Ground", "receivers": [{"entity_id": "workshop_112233"}]}]})
    row = bps._scanner_linking(hass, content)["placed"][0]
    assert row["status"] == "unknown" and row["matching"]["renamed_sensors"] == ["sensor.custom_distance_name"]


def test_disabled_and_scanner_only_receivers_are_known_and_not_unmatched(hass, monkeypatch):
    entries, states, devices, add = configure(hass, monkeypatch)
    devices["scanner"] = NS(_is_scanner=True, address=MAC, name="Workshop 112233")
    add("sensor.tag_distance_to_workshop_112233", None, disabled="user")
    content = json.dumps({"floor": [{"name": "Ground", "receivers": [{"entity_id": "workshop_112233"}]}]})
    assert bps._scanner_diagnostics(hass, content)["unmatched_receivers"] == []
    assert bps._scanner_linking(hass, content)["placed"][0]["status"] == "unknown"
    entries.clear()
    assert inventory(hass)["options"][0]["selectable"]
    assert bps._scanner_diagnostics(hass, content)["unmatched_receivers"] == []
    asyncio.run(bps.update_receiver_liveness(hass))
    assert hass.data["bps"]["rl_offline"] == []


def test_matching_suggestion_uses_full_mac_and_rejects_contradictory_token(hass, monkeypatch):
    entries, states, devices, add = configure(hass, monkeypatch)
    add("sensor.tag_distance_to_workshop")
    content = json.dumps({"floor": [{"name": "Ground", "receivers": [
        {"entity_id": "old_station", "scanner_mac": MAC, "scanner_uid": "112233"}]}]})
    assert bps._scanner_diagnostics(hass, content)["unmatched_receivers"][0]["suggested"] == "workshop"
    content = content.replace(MAC, "dd:ee:ff:11:22:33")
    assert bps._scanner_diagnostics(hass, content)["unmatched_receivers"][0]["suggested"] is None


@pytest.mark.parametrize("has_address", [True, False])
@pytest.mark.parametrize("reverse", [True, False])
def test_shared_friendly_names_use_mac_ages_or_remain_unknown(hass, monkeypatch, has_address, reverse):
    entries, states, devices, add = configure(hass, monkeypatch)
    second_mac = "aa:bb:cc:44:55:66"
    for slug, mac in (("old_a_112233", MAC), ("old_b_445566", second_mac)):
        add(f"sensor.tag_distance_to_{slug}", uid="synthetic-tracker_" + mac + "_range")
        devices[slug] = NS(_is_scanner=True, address=mac, name="Workshop")
    dump = [("a", {"_is_scanner": True, "name": "Workshop", "last_seen": 100}),
            ("b", {"_is_scanner": True, "name": "Workshop", "last_seen": 1})]
    if has_address:
        dump[0][1]["address"] = MAC
        dump[1][1]["address"] = second_mac
    async def call(*args, **kwargs):
        return dict(reversed(dump) if reverse else dump)
    hass.services.async_call = call
    asyncio.run(bps.update_receiver_liveness(hass))
    assert "workshop" not in hass.data["bps"]["rl_ages"]
    assert hass.data["bps"]["rl_offline"] == (["old_b_445566"] if has_address else [])
    assert hass.data["bps"]["rl_unknown"] == ([] if has_address else ["old_a_112233", "old_b_445566"])


def test_shared_name_with_missing_clock_does_not_borrow_other_scanner_age(hass, monkeypatch):
    configure(hass, monkeypatch)
    dom = hass.data["bps"]
    bps._refresh_dump_ages(hass, dom, {"a": {"_is_scanner": True, "name": "Workshop", "last_seen": 100},
                                    "b": {"_is_scanner": True, "name": "Workshop", "last_seen": None}})
    assert dom["rl_ages"] == {} and dom["rl_mac_ages"] == {}


def test_scanner_absent_from_dump_cannot_borrow_shared_name_age(hass, monkeypatch):
    entries, states, devices, add = configure(hass, monkeypatch)
    second_mac = "aa:bb:cc:44:55:66"
    for slug, mac in (("old_a_112233", MAC), ("old_b_445566", second_mac)):
        add(f"sensor.tag_distance_to_{slug}", uid="synthetic-tracker_" + mac + "_range")
        devices[slug] = NS(_is_scanner=True, address=mac, name="Workshop")
    assert all(not row["liveness_name_unique"] for row in inventory(hass)["options"])
    async def call(*args, **kwargs):
        return {second_mac: {"_is_scanner": True, "name": "Workshop", "last_seen": 1},
                "tag": {"last_seen": 100}}
    hass.services.async_call = call
    asyncio.run(bps.update_receiver_liveness(hass))
    assert hass.data["bps"]["rl_offline"] == ["old_b_445566"]
    assert hass.data["bps"]["rl_unknown"] == ["old_a_112233"]
    states["binary_sensor.old_a_112233_status"] = NS(state="on", attributes={"device_class": "connectivity"})
    asyncio.run(bps.update_receiver_liveness(hass))
    assert hass.data["bps"]["rl_offline"] == ["old_b_445566"] and hass.data["bps"]["rl_unknown"] == []
