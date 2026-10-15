"""Synthetic phone GPS data only; never copy production locations here."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import bps
from bps.gps_locations import BPSGPSLocationsAPI, gps_locations, gps_sources, validate_georeference
from conftest import make_hass

NOW = 1800000000


def state(entity_id="device_tracker.synthetic_phone", age=0, **attributes):
    attrs = {"latitude": 10.0, "longitude": 20.0, "gps_accuracy": 1.0,
             "source_type": "gps", "friendly_name": "Synthetic phone", "secret": "must not leak"}
    attrs.update(attributes)
    return SimpleNamespace(entity_id=entity_id, attributes=attrs, state="home",
                           last_updated=datetime.fromtimestamp(NOW - age, timezone.utc))


def hass_with(*states):
    hass = make_hass()
    hass.states = SimpleNamespace(async_all=lambda domain: [s for s in states if s.entity_id.startswith(domain + ".")])
    return hass


def test_auth_allowlist_and_location_fields_only():
    assert BPSGPSLocationsAPI.requires_auth is True
    hass = hass_with(state(), state("person.synthetic_person"), state("sensor.synthetic_location"),
                     state("device_tracker.synthetic_router", source_type="router"))
    rows = gps_locations(hass, NOW)
    assert len(rows) == 2
    assert set(rows[0]) == {"entity_id", "name", "latitude", "longitude", "gps_accuracy", "last_updated", "age_s"}
    assert rows[0]["gps_accuracy"] == 1
    assert rows[0]["last_updated"] == NOW
    assert rows[0]["age_s"] == 0
    assert gps_locations(hass_with(state(age=12.5)), NOW)[0]["age_s"] == 12.5


@pytest.mark.parametrize("key,value", [("latitude", float("nan")), ("longitude", float("inf")),
                                      ("gps_accuracy", 0), ("gps_accuracy", -1), ("gps_accuracy", 1001),
                                      ("gps_accuracy", True), ("latitude", "10"), ("latitude", 86),
                                      ("longitude", 181), ("latitude", 10**1000)])
def test_invalid_location_values_are_omitted(key, value):
    assert gps_locations(hass_with(state(**{key: value})), NOW) == []


def test_stale_future_unknown_and_missing_timestamp_are_omitted():
    unknown = state(); unknown.state = "unknown"
    unavailable = state(); unavailable.state = "unavailable"
    missing = state(); missing.last_updated = None
    assert gps_locations(hass_with(state(age=61), state(age=-1), unknown, unavailable, missing), NOW) == []
    assert len(gps_locations(hass_with(state(age=60)), NOW)) == 1


def test_output_is_bounded_and_endpoint_uses_same_filter(monkeypatch):
    hass = hass_with(*(state(f"device_tracker.synthetic_{i}", friendly_name="x" * 200) for i in range(150)))
    monkeypatch.setattr("bps.gps_locations.time.time", lambda: NOW)
    result = asyncio.run(BPSGPSLocationsAPI().get(SimpleNamespace(app={"hass": hass})))
    assert len(result.json_body["locations"]) == 128
    assert len(result.json_body["locations"][0]["name"]) == 128
    assert len(result.json_body["sources"]) == 128


def test_source_inventory_remains_selectable_across_fresh_stale_unavailable_recovery():
    phone = state()
    for age, availability, expected in ((0, "home", "fresh"), (61, "home", "stale"),
                                        (61, "unavailable", "unavailable"), (0, "home", "fresh")):
        phone.last_updated = datetime.fromtimestamp(NOW - age, timezone.utc)
        phone.state = availability
        hass = hass_with(phone)
        source = gps_sources(hass, NOW)[0]
        assert source["entity_id"] == phone.entity_id
        assert source["status"] == expected
        assert source["age_s"] == age
        assert "secret" not in source and set(source) == {"entity_id", "name", "status", "reason", "age_s"}
        assert len(gps_locations(hass, NOW)) == (1 if expected == "fresh" else 0)


def test_mobile_app_registry_and_person_references_survive_missing_fix(monkeypatch):
    import bps.gps_locations as module
    mobile = state("device_tracker.synthetic_mobile", source_type="gps")
    mobile.state = "unavailable"
    mobile.attributes = {"friendly_name": "Synthetic mobile"}
    person = state("person.synthetic_owner")
    person.state = "unavailable"
    person.attributes = {"device_trackers": [mobile.entity_id], "friendly_name": "Synthetic owner"}
    router = state("device_tracker.synthetic_router", source_type="router")
    router.state = "unavailable"
    registry = SimpleNamespace(entities={mobile.entity_id: SimpleNamespace(entity_id=mobile.entity_id, platform="mobile_app"),
                                         router.entity_id: SimpleNamespace(entity_id=router.entity_id, platform="mobile_app")})
    monkeypatch.setattr(module.er, "async_get", lambda _: registry, raising=False)
    rows = gps_sources(hass_with(mobile, person, router), NOW)
    assert {row["entity_id"] for row in rows} == {mobile.entity_id, person.entity_id}
    assert all(row["status"] == "unavailable" for row in rows)


def test_source_inventory_is_deterministically_bounded_and_excludes_non_gps():
    states = [state(f"device_tracker.synthetic_{i:03d}", age=61) for i in reversed(range(140))]
    states += [state("device_tracker.synthetic_ble", source_type="bluetooth"),
               state("device_tracker.synthetic_router", source_type="router"),
               state("person.synthetic_router_owner", source="device_tracker.synthetic_router")]
    rows = gps_sources(hass_with(*states), NOW)
    assert len(rows) == 128
    assert rows[0]["entity_id"] == "device_tracker.synthetic_000"
    assert rows[-1]["entity_id"] == "device_tracker.synthetic_127"
    assert all(row["status"] == "stale" for row in rows)


def anchors():
    return {"version": 1, "anchors": [{"latitude": 10., "longitude": 20., "x": 50., "y": 100.,
                                       "accuracy_m": 1., "label": "Synthetic corner"}]}


def test_optional_georeference_limits_and_dateline():
    assert validate_georeference({"floor": [{"name": "Old floor"}]}) is None
    geo = anchors()
    assert validate_georeference({"floor": [{"georeference": geo}]}) is None
    geo["anchors"] = [{**geo["anchors"][0], "longitude": lon} for lon in (179.999, -179.999)]
    assert validate_georeference({"floor": [{"georeference": geo}]}) is None
    geo["anchors"][1]["longitude"] = 100
    assert "5 km" in validate_georeference({"floor": [{"georeference": geo}]})


@pytest.mark.parametrize("mutate", [lambda g: g.update(version=True), lambda g: g.update(extra="secret"),
    lambda g: g.update(anchors=g["anchors"] * 13), lambda g: g["anchors"][0].update(latitude=90),
    lambda g: g["anchors"][0].update(x=-1), lambda g: g["anchors"][0].update(accuracy_m=float("nan")),
    lambda g: g["anchors"][0].update(label="x"*81), lambda g: g["anchors"][0].update(arbitrary="secret")])
def test_malformed_georeference_is_rejected(mutate):
    geo = anchors(); mutate(geo)
    assert validate_georeference({"floor": [{"georeference": geo}]})


def test_save_rejects_bad_anchors_before_persisting_and_keeps_valid_anchors():
    hass = make_hass(); geo = anchors(); geo["anchors"][0]["longitude"] = 181
    error = asyncio.run(bps.BPSSaveAPIText()._write_save(hass, "/unused", {}, {"floor": [{"georeference": geo}]}))
    assert error.status == 400 and not hass._store_saves
    geo = anchors()
    layout = {"floor": [{"name": "Synthetic property", "scale": 20, "receivers": [], "georeference": geo}]}
    assert asyncio.run(bps.BPSSaveAPIText()._write_save(hass, "/unused", {}, layout)) is None
    assert hass._store_backing["bps"]["floor"][0]["georeference"] == geo
    assert hass._store_backing["bps"]["floor"][0]["scale"] == 20


def test_saved_anchors_are_included_in_detached_local_diagnostics(monkeypatch):
    hass = hass_with()
    geo = anchors()
    hass.data["bps"] = {"layout": {"floor": [{"name": "Synthetic property", "scale": 20,
                                              "receivers": [], "zones": [], "georeference": geo}]}}
    monkeypatch.setattr(bps, "_bermuda_distance_sensor_ids", lambda _hass: [])
    snapshot = bps._diagnostic_snapshot(hass, None, NOW)
    assert snapshot["context"]["layout"]["floor"][0]["georeference"] == geo
    snapshot["context"]["layout"]["floor"][0]["georeference"]["anchors"][0]["latitude"] = 12
    assert geo["anchors"][0]["latitude"] == 10, "diagnostic downloads cannot mutate saved anchors"
