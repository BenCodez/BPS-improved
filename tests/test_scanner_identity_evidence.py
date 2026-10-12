"""Expose why a scanner mapping is unavailable without arbitrary identifiers."""
import json
from types import SimpleNamespace as NS

from bps import bermuda_diagnostics as D
from test_bermuda_diagnostics import fixture


def test_scanner_address_field_validation_is_bounded_and_does_not_expose_invalid_ids(hass, monkeypatch):
    _entry, coordinator, *_ = fixture(hass, monkeypatch)
    coordinator.devices['scanner'] = NS(_is_scanner=True, name='Synthetic scanner',
        address='source-private-non-MAC-identifier', address_ble_mac='AA:BB:CC:DD:EE:FF',
        address_wifi_mac='aa-bb-cc-dd-ee-00')
    row = D.snapshot(hass, 110)['entries'][0]['scanner_identity_validation'][0]
    assert row['source_address']['value'] is None
    assert row['source_address']['valid'] is False
    assert row['ble_address']['value'] == 'aa:bb:cc:dd:ee:ff'
    assert row['wifi_address']['hyphen_count'] == 5
    assert row['wifi_address']['value'] is None
    assert 'source-private-non-MAC-identifier' not in json.dumps(row)
    for i in range(130):
        coordinator.devices[str(i)] = coordinator.devices['scanner']
    report = D.snapshot(hass, 110)
    assert len(report['entries'][0]['scanner_identity_validation']) == 128
    assert report['entries'][0]['scanner_identity_validation_truncated']
