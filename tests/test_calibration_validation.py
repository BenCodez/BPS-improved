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


def test_bad_corrections_make_tracking_worse_and_one_location_is_inconclusive():
    data, floor = recording()
    for frame in data['frames']:
        for row in frame['readings']:
            row['measured_distance_m'] *= FACTORS[row['receiver']]
    assert compare(data, floor)['verdict'] == 'worse'
    data, floor = recording()
    data['frames'] = data['frames'][:11]
    assert compare(data, floor)['verdict'] == 'inconclusive'


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
            if bad == 'old': row['observed'] = 0.
            if bad == 'future': row['observed'] = frame['time'] + 1
            if bad == 'assumed': row['assumed_unit_m'] = True
            if bad == 'stale': row['status'] = 'stale'
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
    hass.data['bps'] = {'layout': {'floor': [floor]}, '_diagnostics': types.SimpleNamespace(frames=[1], export=export),
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
