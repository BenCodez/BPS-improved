"""A real manual solve remains exportable after a proven receiver relink."""
import asyncio
import copy
import json
from types import SimpleNamespace as NS

import pytest

import bps
from bps import calibration as C
from bps import bermuda_calibration as B
from bps.storage import get_bps_data_for_edit, save_bps_data
from test_bermuda_calibration import fixture
from test_calibration_validation import FACTORS, recording
from test_receiver_matching import configure


async def prepared(hass, monkeypatch):
    entries, states, live, add = configure(hass, monkeypatch)
    data, _ = recording()
    original = copy.deepcopy(data['contexts']['c']['layout'])
    _, devices, _, _, _ = fixture(current=1.)
    for index, slug in enumerate(FACTORS):
        mac = f'aa:bb:cc:dd:ee:{index:02x}'
        add(f'sensor.tag_distance_to_{slug}', '4', uid='synthetic-tag_' + mac + '_range')
        live[mac] = NS(**{**devices[mac], 'adverts': {k: NS(**v) for k, v in devices[mac]['adverts'].items()}})
    entry = hass.config_entries.async_entries('bermuda')[0]
    coordinator = entry.runtime_data.coordinator
    coordinator.options = {'attenuation': 4., 'ref_power': -45., 'rssi_offsets': {mac: -9. for mac in live}}
    await save_bps_data(hass, original)
    cal = C.get_calibration_state(hass)
    cal['mode'] = 'manual'
    cal['receivers'] = C._build_receiver_map(original, 'Yard', hass=hass)
    scanner_map = C._match_scanners(cal, devices)
    for index in range(C.MIN_SAMPLES_PER_PAIR):
        for tx_mac, dev in devices.items():
            tx = scanner_map[tx_mac]
            for rx_mac, advert in dev['adverts'].items():
                rx = scanner_map[rx_mac]
                advert.update(stamp=100. + index,
                    rssi_distance_raw=C._true_distance_m(cal, tx, rx) / FACTORS[rx])
        C._ingest_dump(cal, devices)
    api = C.BPSCalibrationAPI(bps._calibration_tracking_check)
    async def post(action):
        async def body():
            return {'action': action, 'floor': 'Yard'}
        return await api.post(NS(app={'hass': hass}, json=body))
    response = await post('solve')
    assert api.requires_auth and response.status == 200
    solved = cal['results']['Yard']
    assert solved['receivers'] == pytest.approx(FACTORS, abs=.0002)
    assert solved['receiver_identities']['r0']['scanner_config_entry_id'] == 'entry'
    current = copy.deepcopy(original)
    current['floor'][0]['receivers'][0].update(entity_id='r0_current', scanner_uid='ee00',
        scanner_mac='aa:bb:cc:dd:ee:00', scanner_config_entry_id='entry')
    add('sensor.tag_distance_to_r0_current', '4', uid='synthetic-tag_aa:bb:cc:dd:ee:00_range')
    live['aa:bb:cc:dd:ee:00'].name = 'r0_current'
    # Measurements were recorded after the relink and at the same geometry.
    data['contexts']['c']['layout'] = copy.deepcopy(current)
    for frame in data['frames']:
        for row in frame['readings']:
            if row['receiver'] == 'r0':
                row['receiver'] = 'r0_current'
    async def export():
        return json.dumps(data).encode()
    hass.data['bps']['_diagnostics'] = NS(frames=[1], export=export)
    await save_bps_data(hass, current)
    return post, current, solved, coordinator, entries


@pytest.mark.parametrize('old_name', ['present', 'removed', 'reused'])
def test_authenticated_preview_after_real_solve_and_rematch_matches_apply(hass, monkeypatch, old_name):
    async def scenario():
        post, current, solved, coordinator, entries = await prepared(hass, monkeypatch)
        if old_name == 'removed':
            del entries['sensor.tag_distance_to_r0']
        elif old_name == 'reused':
            entries['sensor.tag_distance_to_r0'].unique_id = 'synthetic-tag_aa:bb:cc:88:99:00_range'
        before = copy.deepcopy((current, solved, coordinator.options, hass._store_saves))
        response = await post('preview_bermuda')
        assert response.status == 200
        preview = response.json_body
        assert preview['warnings'] == []
        assert all(row['candidate_eligible'] for row in preview['rows'])
        row = preview['rows'][0]
        assert row['receiver'] == 'r0_current'
        assert row['proposed_bps_factor'] == solved['receivers']['r0']
        assert row['scanner_mac'] == 'aa:bb:cc:dd:ee:00'
        assert row['proposed_offset_db'] == pytest.approx(-9. + B.delta_db(row['proposed_bps_factor'], 4.))
        assert (current, solved, coordinator.options, hass._store_saves) == before
        assert get_bps_data_for_edit(hass) == current
        applied = await post('apply')
        assert applied.status == 200 and applied.json_body['applied'] == 4
        saved = get_bps_data_for_edit(hass)
        assert {rec['entity_id']: rec['correction'] for rec in saved['floor'][0]['receivers']} == {
            row['receiver']: row['proposed_bps_factor'] for row in preview['rows']}
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['replacement', 'wrong_owner', 'unproven', 'conflict',
    'geometry', 'height', 'scale', 'offset', 'attenuation', 'reference', 'transmitter_reference',
    'source_owner', 'source_mac', 'weak',
    'missing_validation', 'stale_validation', 'untested_factor'])
def test_rematched_preview_preserves_source_and_validation_guards(hass, monkeypatch, change):
    async def scenario():
        post, current, solved, coordinator, entries = await prepared(hass, monkeypatch)
        tracking = await bps._calibration_tracking_check(hass, 'Yard')
        assert tracking['verdict'] == 'improves'
        receiver = current['floor'][0]['receivers'][0]
        if change == 'replacement':
            receiver['scanner_mac'] = 'aa:bb:cc:88:99:00'
            entries['sensor.tag_distance_to_r0_current'].unique_id = 'synthetic-tag_aa:bb:cc:88:99:00_range'
        elif change == 'wrong_owner':
            receiver['scanner_config_entry_id'] = 'other-entry'
            entries['sensor.tag_distance_to_r0_current'].config_entry_id = 'other-entry'
        elif change == 'unproven':
            solved.pop('receiver_identities')
        elif change == 'conflict':
            alias = copy.deepcopy(receiver)
            alias['entity_id'] = 'r0'
            alias['cords']['x'] += 50.
            current['floor'][0]['receivers'].append(alias)
        elif change == 'geometry':
            receiver['cords']['x'] += 1.
        elif change == 'height':
            receiver['height'] += 1.
        elif change == 'scale':
            current['floor'][0]['scale'] += 1.
        elif change == 'offset':
            coordinator.options['rssi_offsets']['aa:bb:cc:dd:ee:00'] = -6.
        elif change == 'attenuation':
            coordinator.options['attenuation'] = 3.
        elif change == 'reference':
            coordinator.options['ref_power'] = -50.
        elif change == 'transmitter_reference':
            for advert in coordinator.devices['aa:bb:cc:dd:ee:00'].adverts.values():
                advert.ref_power = -50.
        elif change == 'source_owner':
            solved['bermuda_source']['receivers']['r0']['geometry']['scanner_config_entry_id'] = 'other-entry'
        elif change == 'source_mac':
            solved['bermuda_source']['receivers']['r0']['geometry']['scanner_mac'] = 'aa:bb:cc:88:99:00'
        elif change == 'weak':
            solved['low_confidence'] = ['r0']
        elif change == 'missing_validation':
            tracking = None
        elif change == 'stale_validation':
            tracking['candidate_solved_at'] = 'other-solve'
        elif change == 'untested_factor':
            tracking['candidate_corrections']['r0_current'] = 1.
        source = B.runtime_context(hass, current, 'Yard')
        preview = B.build_preview(current, 'Yard', solved, source, tracking, hass=hass)
        row = next(r for r in preview['rows'] if r['receiver'] == 'r0_current')
        assert not row['candidate_eligible']
        if change in ('geometry', 'height', 'scale', 'offset', 'attenuation', 'reference',
                      'transmitter_reference', 'source_owner', 'source_mac'):
            assert 'sample_source_changed' in row['reasons']
        elif change == 'weak':
            assert 'weak_receiver_confidence' in row['reasons']
        elif change.endswith('validation') or change == 'untested_factor':
            assert 'measured_tracking_revalidation_required' in row['reasons']
    asyncio.run(scenario())
