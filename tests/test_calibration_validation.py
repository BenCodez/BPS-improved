"""Use the real solver: paired tracking accuracy, clock guards and read-only API."""
import asyncio
import copy
import json
import math
import types

import pytest
import bps
from bps.calibration_validation import compare_tracking

FACTORS = {'r0': .5, 'r1': 2., 'r2': 1., 'r3': 1.}


def recording():
    floor = {'name': 'Yard', 'scale': 10., 'zones': [], 'receivers': [
        {'entity_id': f'r{i}', 'cords': {'x': x * 10, 'y': y * 10}, 'height': 0}
        for i, (x, y) in enumerate(((0, 0), (20, 0), (0, 20), (20, 20)))]}
    context = {'layout': {'floor': [floor], 'outdoor_tracking': {'enabled': True}},
               'target_members': {'bps_group_dog': ['tag']}}
    bundle = {'format': 'bps-diagnostics-v1', 'started': 0., 'targets': ['bps_group_dog'],
              'contexts': {'c': context}, 'annotations': [], 'frames': [
                  {'time': 0., 'context_id': 'c', 'readings': []}]}
    for i in range(1, 21):
        point = (6., 7.) if i <= 10 else (14., 12.)
        if i in (1, 11):
            bundle['annotations'].append({'time': float(i), 'kind': 'known_position',
                'target': 'bps_group_dog', 'floor': 'Yard', 'x_m': point[0], 'y_m': point[1]})
        rows = [{'tracker': 'tag', 'receiver': r['entity_id'], 'floor': 'Yard', 'status': 'current',
                 'measured_distance_m': math.dist(point, [v / 10 for v in r['cords'].values()]) / FACTORS[r['entity_id']],
                 'observed': float(i), 'receiver_correction': 1., 'tracker_height_m': 0.,
                 'tracker_distance_factor': 1., 'assumed_unit_m': False} for r in floor['receivers']]
        bundle['frames'].append({'time': float(i), 'context_id': 'c', 'readings': rows})
    return bundle, floor


def compare(bundle, floor, factors=FACTORS):
    return compare_tracking(bundle, 'Yard', factors, floor, bps.trilaterate)


def test_actual_paired_tracking_improves_without_mutating_inputs():
    data, floor = recording()
    before = copy.deepcopy((data, floor, FACTORS))
    result = compare(data, floor)
    assert result['verdict'] == 'improves'
    assert result['baseline']['samples'] == 20
    assert result['locations'] == 2
    assert result['candidate']['p95_m'] < .01
    assert result['baseline']['median_m'] > 1
    assert (data, floor, FACTORS) == before


def test_replay_preserves_opt_in_interval_mode_and_reprojects_candidate_corrections(monkeypatch):
    from bps import calibration_validation as validation
    data, floor = recording()
    layout = data['contexts']['c']['layout']
    layout['outdoor_tracking']['long_range_intervals'] = True
    readings = data['frames'][1]['readings']
    readings[0]['measurement_source'] = {'range_interval': {'status': 'available', 'solver_eligible': True,
        'lower_m': 5, 'center_m': 10, 'upper_m': 20}}
    captured = {}
    def solver(candidate, weighted, *args, **kwargs):
        captured.update(floor=candidate, kwargs=kwargs)
        return {'fix': (0, 0), 'outdoor': {}}
    monkeypatch.setattr(validation, 'solve_outdoor', solver)
    validation.replay(floor, readings, {'r0': 2}, 10, layout, 1, bps.trilaterate)
    assert captured['kwargs']['long_range_intervals'] is True
    band = captured['floor']['receivers'][0]['_outdoor_reading']['horizontal_interval']
    assert (band['lower_px'], band['center_px'], band['upper_px']) == (100, 200, 400)


def test_bad_corrections_make_tracking_worse_and_one_location_is_inconclusive():
    data, floor = recording()
    for frame in data['frames']:
        for row in frame['readings']:
            row['measured_distance_m'] *= FACTORS[row['receiver']]
    assert compare(data, floor)['verdict'] == 'worse'
    data, floor = recording()
    data['frames'] = data['frames'][:11]
    assert compare(data, floor)['verdict'] == 'inconclusive'


@pytest.mark.parametrize('setting,value', [
    ('tracker_ref_offsets', {'tag': 9.0309}), ('tracker_height', 2.),
    ('tracker_heights', {'tag': 2.}), ('reading_max_age', 10.),
    ('position_timeout', 10.), ('tracker_groups', [{'id': 'new', 'beacons': ['tag']}]),
    ('outdoor_tracking', {'enabled': False}),
])
def test_auto_replay_drops_recordings_with_changed_tracking_settings(setting, value):
    data, floor = recording()
    current = copy.deepcopy(data['contexts']['c']['layout'])
    current[setting] = value
    result = compare_tracking(data, 'Yard', FACTORS, floor, bps.trilaterate,
                              {s: 1. for s in FACTORS}, current)
    assert result['verdict'] == 'inconclusive'
    assert result['eligible_samples'] == 0
    assert result['baseline']['samples'] == 0


def test_auto_replay_baseline_uses_live_corrections_and_scanner_relink_invalidates_recording():
    data, floor = recording()
    result = compare_tracking(data, 'Yard', FACTORS, floor, bps.trilaterate,
                              FACTORS, data['contexts']['c']['layout'])
    assert result['verdict'] == 'inconclusive'
    assert result['baseline']['median_m'] < .01
    assert result['candidate']['median_m'] == result['baseline']['median_m']
    current = copy.deepcopy(floor)
    current['receivers'][0]['scanner_uid'] = 'replacement-hardware'
    result = compare_tracking(data, 'Yard', FACTORS, current, bps.trilaterate,
                              FACTORS, data['contexts']['c']['layout'])
    assert result['eligible_samples'] == 0


@pytest.mark.parametrize('replacement', [
    {'scanner_mac': 'dd:ee:ff:11:22:33'},
    {'scanner_config_entry_id': 'replacement-owner'},
])
def test_full_scanner_binding_change_invalidates_recording_and_tracking_check(hass, monkeypatch, replacement):
    from test_receiver_matching import configure
    entries, states, devices, add = configure(hass, monkeypatch)
    data, floor = recording()
    floor['receivers'][0].update(scanner_uid='112233', scanner_mac='aa:bb:cc:11:22:33',
                                 scanner_config_entry_id='entry')
    current = copy.deepcopy(data['contexts']['c']['layout'])
    current['floor'][0]['receivers'][0].update(replacement)
    for index, receiver in enumerate(current['floor'][0]['receivers']):
        mac = receiver.get('scanner_mac', f'aa:bb:cc:44:55:{index:02x}')
        eid = f"sensor.tag_distance_to_{receiver['entity_id']}"
        add(eid, '4', uid='synthetic-tag_' + mac + '_range')
        entries[eid].config_entry_id = receiver.get('scanner_config_entry_id', 'entry')
    # Both full MACs have the same hardware suffix and the receiver name remains
    # unchanged. The current scanner is valid, but old measurements are not its data.
    async def export():
        return json.dumps(data).encode()
    hass.data['bps'] = {'layout': current,
        '_diagnostics': types.SimpleNamespace(frames=[1], export=export),
        'calibration': {'results': {'Yard': {'floor': 'Yard', 'receivers': FACTORS}}}}
    result = asyncio.run(bps._calibration_tracking_check(hass, 'Yard'))
    assert result['verdict'] == 'inconclusive'
    assert result['eligible_samples'] == result['baseline']['samples'] == 0
    # Automatic calibration uses the same replay entry point and geometry guard.
    result = compare_tracking(data, 'Yard', FACTORS, current['floor'][0], bps.trilaterate,
                              {s: 1. for s in FACTORS}, current, set(FACTORS))
    assert result['eligible_samples'] == result['baseline']['samples'] == 0


def test_recording_with_unchanged_full_scanner_binding_remains_eligible():
    data, floor = recording()
    floor['receivers'][0].update(scanner_uid='112233', scanner_mac='aa:bb:cc:11:22:33',
                                 scanner_config_entry_id='entry')
    result = compare(data, copy.deepcopy(floor))
    assert result['verdict'] == 'improves'
    assert result['baseline']['samples'] == 20


def test_cached_readings_do_not_inflate_samples_and_geometry_changes_excluded():
    data, floor = recording()
    data['frames'].extend(copy.deepcopy(data['frames'][-1:]) * 20)
    assert compare(data, floor)['baseline']['samples'] == 20
    changed = copy.deepcopy(floor)
    changed['receivers'][0]['height'] = 2
    assert compare(data, changed)['verdict'] == 'inconclusive'
    assert compare(data, changed)['baseline']['samples'] == 0


@pytest.mark.parametrize('bad', ['old', 'future', 'assumed', 'stale'])
def test_invalid_observations_cannot_count_as_tracking_improvement(bad):
    data, floor = recording()
    for frame in data['frames']:
        for row in frame['readings']:
            if bad == 'old':
                row['observed'] = 0.
            if bad == 'future':
                row['observed'] = frame['time'] + 1
            if bad == 'assumed':
                row['assumed_unit_m'] = True
            if bad == 'stale':
                row['status'] = 'stale'
    assert compare(data, floor)['baseline']['samples'] == 0


def test_candidate_failure_cannot_claim_improvement():
    data, floor = recording()
    def solver(points, **kw):
        if points[0][4] < 120:
            return None
        return bps.trilaterate(points, **kw)
    result = compare_tracking(data, 'Yard', FACTORS, floor, solver)
    assert result['candidate_failures'] > 0
    assert result['verdict'] == 'worse'


def test_authenticated_calibration_test_uses_detached_recording(hass, monkeypatch):
    data, floor = recording()
    async def export(): return json.dumps(data).encode()
    hass.data['bps'] = {'layout': copy.deepcopy(data['contexts']['c']['layout']), '_diagnostics': types.SimpleNamespace(frames=[1], export=export),
        'calibration': {'results': {'Yard': {'floor': 'Yard', 'receivers': FACTORS, 'solved_at': 'test-date'}}}}
    before = copy.deepcopy(hass.data['bps']['layout'])
    result = asyncio.run(bps._calibration_tracking_check(hass, 'Yard'))
    assert result['verdict'] == 'improves'
    assert result['candidate_solved_at'] == 'test-date'
    assert hass.data['bps']['layout'] == before
    assert 'calibration_validation_busy' not in hass.data['bps']
    hass.data['bps']['calibration_validation_busy'] = True
    with pytest.raises(ValueError, match='already running'):
        asyncio.run(bps._calibration_tracking_check(hass, 'Yard'))


def test_tracking_comparison_uses_corrections_apply_saves_after_proven_rematch(hass, monkeypatch):
    from test_receiver_matching import configure
    from bps import calibration as calibration
    entries, states, devices, add = configure(hass, monkeypatch)
    data, floor = recording()
    floor['receivers'][0]['entity_id'] = 'r0_current'
    for frame in data['frames']:
        for row in frame['readings']:
            if row['receiver'] == 'r0':
                row['receiver'] = 'r0_current'
    for index, slug in enumerate(['r0', 'r0_current', 'r1', 'r2', 'r3']):
        mac = 'aa:bb:cc:11:22:33' if index < 2 else f'aa:bb:cc:44:55:{index:02x}'
        add(f'sensor.tag_distance_to_{slug}', '4', uid='synthetic-tag_' + mac + '_range')
    current = copy.deepcopy(data['contexts']['c']['layout'])
    async def export():
        return json.dumps(data).encode()
    hass.data['bps'] = {'layout': current,
        '_diagnostics': types.SimpleNamespace(frames=[1], export=export),
        'calibration': {'results': {'Yard': {'floor': 'Yard', 'receivers': FACTORS, 'solved_at': 'solve-date',
            'receiver_identities': {'r0': {'scanner_mac': 'aa:bb:cc:11:22:33', 'scanner_config_entry_id': 'entry'}}}}}}
    result = asyncio.run(bps._calibration_tracking_check(hass, 'Yard'))
    proof = hass.data['bps']['calibration']['results']['Yard']['receiver_identities']
    applied = calibration._equivalent_updates(hass, current['floor'][0], FACTORS, identities=proof)
    assert result['candidate_corrections'] == applied == {'r0_current': .5, 'r1': 2., 'r2': 1., 'r3': 1.}
    assert result['candidate_solved_at'] == 'solve-date'
    assert result['verdict'] == 'improves'
    assert result['candidate']['p95_m'] < .01
    assert result['baseline']['samples'] == 20


def test_tracking_test_compares_candidate_to_current_installed_corrections(hass):
    data, floor = recording()
    current = copy.deepcopy(data['contexts']['c']['layout'])
    for receiver in current['floor'][0]['receivers']:
        receiver['correction'] = FACTORS[receiver['entity_id']]
    async def export():
        return json.dumps(data).encode()
    hass.data['bps'] = {'layout': current,
        '_diagnostics': types.SimpleNamespace(frames=[1], export=export),
        'calibration': {'results': {'Yard': {'floor': 'Yard', 'receivers': FACTORS}}}}
    result = asyncio.run(bps._calibration_tracking_check(hass, 'Yard'))
    assert result['verdict'] != 'improves'
    assert result['baseline'] == result['candidate']


@pytest.mark.parametrize('enabled,expected', [(False, 20), (True, 0)])
def test_saved_ignore_policy_matches_live_outdoor_enablement(enabled, expected):
    data, floor = recording()
    floor['receivers'] = floor['receivers'][:3]
    floor['receivers'][0]['outdoor_policy'] = 'ignore'
    data['contexts']['c']['layout']['outdoor_tracking']['enabled'] = enabled
    for frame in data['frames']:
        frame['readings'] = [r for r in frame['readings'] if r['receiver'] != 'r3']
        for row in frame['readings']:
            row['ignored'] = row['receiver'] == 'r0'
    assert compare(data, floor)['baseline']['samples'] == expected


def test_export_decoding_and_replay_both_run_off_the_home_assistant_loop(hass, monkeypatch):
    import threading
    import bps.calibration_validation as validation
    data, floor = recording()
    payload = json.dumps(data).encode()
    loop_thread = threading.get_ident()
    original_loads = json.loads
    def decode(value, **kwargs):
        assert threading.get_ident() != loop_thread
        return original_loads(value, **kwargs)
    monkeypatch.setattr(validation.json, 'loads', decode)
    async def executor(fn, *args):
        return await asyncio.to_thread(fn, *args)
    async def export():
        assert threading.get_ident() == loop_thread
        return payload
    hass.async_add_executor_job = executor
    hass.data['bps'] = {'layout': copy.deepcopy(data['contexts']['c']['layout']), '_diagnostics': types.SimpleNamespace(frames=[1], export=export),
        'calibration': {'results': {'Yard': {'floor': 'Yard', 'receivers': FACTORS}}}}
    result = asyncio.run(bps._calibration_tracking_check(hass, 'Yard'))
    assert result['verdict'] == 'improves'


@pytest.mark.parametrize('value', ['30', -5, True, None, float('nan'), float('inf'), 1e20])
def test_invalid_reading_age_uses_live_fallback_in_filter_and_solver(value):
    data, floor = recording()
    data['contexts']['c']['layout']['reading_max_age'] = value
    assert bps._reading_max_age(data['contexts']['c']['layout']) == 30
    result = compare(data, floor)
    assert result['baseline']['samples'] == 20
    assert result['verdict'] == 'improves'


@pytest.mark.parametrize('value,expected', [(30., 0), (0, 20)])
def test_replay_age_gate_matches_filtering_and_preserves_zero_opt_out(value, expected):
    data, floor = recording()
    data['contexts']['c']['layout']['reading_max_age'] = value
    # Readings are genuinely after truth/context but old when polled. Status
    # deliberately remains current to verify the comparison's own clock guard.
    data['annotations'] = data['annotations'][:1]
    for frame in data['frames'][1:]:
        frame['time'] += 40
    assert bps._reading_max_age(data['contexts']['c']['layout']) == value
    assert compare(data, floor)['baseline']['samples'] == expected
