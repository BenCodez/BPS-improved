"""Synthetic phone sensor data only; never use private phone measurements."""
import asyncio
import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import bps
from bps import height_sources as heights
from conftest import make_hass

NOW = 1800000000


def phone(eid="sensor.synthetic_pressure", value="1000", unit="hPa", age=0, **attrs):
    attributes = {"device_class": "pressure", "unit_of_measurement": unit,
                  "friendly_name": "Synthetic phone", "latitude": 10, "secret": "private"}
    attributes.update(attrs)
    return SimpleNamespace(entity_id=eid, state=value, attributes=attributes,
                           last_updated=datetime.fromtimestamp(NOW-age, timezone.utc))


def inventory(monkeypatch, states, platforms=None):
    hass = make_hass()
    hass.states = SimpleNamespace(get=lambda eid: next((s for s in states if s.entity_id == eid), None))
    entries = {s.entity_id: SimpleNamespace(entity_id=s.entity_id, platform=(platforms or {}).get(s.entity_id, "mobile_app")) for s in states}
    monkeypatch.setattr(heights.er, "async_get", lambda _: SimpleNamespace(entities=entries), raising=False)
    return hass


@pytest.mark.parametrize("value,unit,expected", [("1000", "hPa", 1000), ("1000", "mbar", 1000),
    ("100000", "Pa", 1000), ("100", "kPa", 1000), ("nan", "hPa", None), ("inf", "hPa", None),
    ("1000", "psi", None), ("1000", [], None), (True, "hPa", None), ("299", "hPa", None), ("1101", "hPa", None)])
def test_unit_conversion_rejects_missing_unsupported_and_nonfinite_pressure(value, unit, expected):
    assert heights.pressure_hpa(value, unit) == expected


def test_authenticated_mobile_app_proof_and_allowlisted_fields(monkeypatch):
    states = [phone(), phone("sensor.synthetic_weather"), phone("sensor.synthetic_not_pressure", device_class="temperature"),
              phone("sensor.synthetic_unknown_unit", unit="psi"), phone("device_tracker.synthetic_phone")]
    hass = inventory(monkeypatch, states, {"sensor.synthetic_weather": "weather"})
    rows = heights.height_sources(hass, NOW)
    assert heights.BPSHeightSourcesAPI.requires_auth is True
    assert len(rows) == 1
    assert set(rows[0]) == {"entity_id", "name", "kind", "available", "pressure_hpa", "age_s", "timestamp_kind"}
    assert rows[0]["pressure_hpa"] == 1000 and rows[0]["age_s"] == 0
    assert rows[0]["timestamp_kind"] == "ha_state_update"
    assert len(heights.height_sources(inventory(monkeypatch, [phone(device_class="atmospheric_pressure")]), NOW)) == 1


def test_disabled_registry_sensor_without_state_is_discoverable(monkeypatch):
    hass = make_hass(); hass.states = SimpleNamespace(get=lambda eid: None)
    entry = SimpleNamespace(entity_id="sensor.synthetic_disabled", platform="mobile_app", disabled_by="user",
                            original_device_class="atmospheric_pressure", unit_of_measurement="hPa", original_name="Synthetic disabled phone")
    monkeypatch.setattr(heights.er, "async_get", lambda _: SimpleNamespace(entities={entry.entity_id: entry}), raising=False)
    row = heights.height_sources(hass, NOW)[0]
    assert row["name"] == "Synthetic disabled phone" and row["available"] is False
    assert row["pressure_hpa"] is None and row["age_s"] is None


def test_stale_future_unavailable_and_missing_time_remain_discoverable_without_values(monkeypatch):
    no_time = phone("sensor.synthetic_no_time"); no_time.last_updated = None
    rows = heights.height_sources(inventory(monkeypatch, [phone(age=61), phone("sensor.synthetic_future", age=-1),
        phone("sensor.synthetic_disabled", value="unavailable"), no_time]), NOW)
    assert len(rows) == 4
    assert all(r["available"] is False and r["pressure_hpa"] is None and r["age_s"] is None for r in rows)
    assert heights.height_sources(inventory(monkeypatch, [phone(age=60)]), NOW)[0]["available"] is True


def test_endpoint_bounded_inventory_and_names(monkeypatch):
    hass = inventory(monkeypatch, [phone(f"sensor.synthetic_{i}", friendly_name="x"*200) for i in range(150)])
    monkeypatch.setattr(heights.time, "time", lambda: NOW)
    response = asyncio.run(heights.BPSHeightSourcesAPI().get(SimpleNamespace(app={"hass": hass})))
    assert len(response.json_body["sources"]) == 128
    assert len(response.json_body["sources"][0]["name"]) == 128


def survey(method="gps"):
    base = {"value": 100, "sample_count": 2, "span_s": 30, "age_s": 0,
            "spread_m": .1, "reported_accuracy_m": .5, "timestamp_kind": "browser_position"}
    station = {**base, "value": 101}
    if method == "pressure":
        base.update(value=1000, reported_accuracy_m=None, timestamp_kind="ha_state_update")
        station = {**base, "value": 999.99}
    delta = heights_delta(base["value"], station["value"]) if method == "pressure" else 1
    return {"version": 1, "reference": "common_floor", "method": method,
            "source": "browser" if method == "gps" else "sensor.synthetic_pressure",
            "baseline_phone_height_m": 1.5, "station_offset_m": .5, "delta_m": delta,
            "estimate_m": 2+delta, "uncertainty_m": 1, "baseline_age_s": 60,
            "confirmed_at": NOW, "baseline": base, "station": station}


def heights_delta(base, station):
    return 44330 * (1-(station/base)**.1903)


def layout(provenance=None):
    provenance = survey() if provenance is None else provenance
    return {"floor": [{"name": "Synthetic floor", "scale": 20, "receivers": [
        {"entity_id": "synthetic_receiver", "height": provenance["estimate_m"], "height_survey": provenance}]}]}


def test_optional_legacy_and_valid_pressure_gps_provenance():
    assert heights.validate_height_surveys({"floor": [{"receivers": [{"height": 1.5}]}]}) is None
    for method in ("pressure", "gps"):
        assert heights.validate_height_surveys(layout(survey(method))) is None


@pytest.mark.parametrize("mutate", [lambda s: s.update(version=True), lambda s: s.update(reference="sea_level"),
    lambda s: s.update(source="person.someone_else"), lambda s: s.update(extra="private"),
    lambda s: s.update(baseline_age_s=901), lambda s: s.update(station_offset_m=float("nan")),
    lambda s: s.update(estimate_m=100), lambda s: s.update(uncertainty_m=.1),
    lambda s: s["baseline"].update(reported_accuracy_m=None), lambda s: s["station"].update(reported_accuracy_m=True),
    lambda s: s["station"].update(span_s=19), lambda s: s["station"].update(sample_count=181),
    lambda s: s["station"].update(sample_count=True), lambda s: s["station"].update(age_s=61),
    lambda s: s["station"].update(latitude=10), lambda s: s["station"].update(value=1e100)])
def test_malformed_or_unbounded_survey_rejected(mutate):
    data = layout(); mutate(data["floor"][0]["receivers"][0]["height_survey"])
    assert heights.validate_height_surveys(data)


def test_wrong_common_floor_conversion_pressure_metadata_and_missing_vertical_accuracy_rejected():
    data = layout(); data["floor"][0]["receivers"][0]["height"] = 101
    assert heights.validate_height_surveys(data)
    for key, value in [("spread_m", .61), ("reported_accuracy_m", 1), ("timestamp_kind", "browser_position")]:
        data = layout(survey("pressure")); data["floor"][0]["receivers"][0]["height_survey"]["station"][key] = value
        assert heights.validate_height_surveys(data)
    data = layout(); s = data["floor"][0]["receivers"][0]["height_survey"]
    s["baseline"]["reported_accuracy_m"] = 1.1; s["station"]["reported_accuracy_m"] = 1.1; s["uncertainty_m"] = 2.2
    assert heights.validate_height_surveys(data)


def test_save_rejects_before_persistence_and_preserves_private_confirmed_provenance():
    hass = make_hass(); data = layout(); data["floor"][0]["receivers"][0]["height_survey"]["baseline_age_s"] = 901
    response = asyncio.run(bps.BPSSaveAPIText()._write_save(hass, "/unused", {}, data))
    assert response.status == 400 and not hass._store_saves
    data = layout(survey("pressure"))
    assert asyncio.run(bps.BPSSaveAPIText()._write_save(hass, "/unused", {}, data)) is None
    assert hass._store_backing["bps"] == data
    retained = copy.deepcopy(hass._store_backing["bps"])
    retained["floor"][0]["receivers"][0]["height_survey"]["station"]["value"] = 900
    assert hass._store_backing["bps"] == data


def test_full_diagnostics_include_detached_confirmed_height_provenance(monkeypatch):
    hass = make_hass(); data = layout(survey("pressure")); hass.data["bps"] = {"layout": data}
    hass.states = SimpleNamespace(async_all=lambda domain: [], get=lambda eid: None)
    monkeypatch.setattr(bps, "_bermuda_distance_sensor_ids", lambda _: [])
    result = bps._diagnostic_snapshot(hass, None, NOW)
    saved = result["context"]["layout"]["floor"][0]["receivers"][0]["height_survey"]
    assert saved == data["floor"][0]["receivers"][0]["height_survey"]
    saved["station"]["value"] = 900
    assert data["floor"][0]["receivers"][0]["height_survey"]["station"]["value"] == 999.99
