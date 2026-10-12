"""Synthetic replay evidence: pooling cannot prove tracker/location accuracy."""
import copy

import pytest

from bps import calibration_validation as validation


def compare_errors(monkeypatch, evidence, *, invalid_locations=None, cleared_locations=(),
                   group_members=None):
    """Replay explicit error pairs while exercising real filtering and sampling."""
    floor = {'name': 'Synthetic', 'scale': 1., 'zones': [], 'receivers': [
        {'entity_id': f'r{i}', 'cords': {'x': i, 'y': i % 2}}
        for i in range(3)]}
    data = {'format': 'bps-diagnostics-v1', 'started': 0.,
            'targets': list(evidence), 'contexts': {'c': {'layout': {'floor': [floor]}}},
            'annotations': [], 'frames': [{'time': 0., 'context_id': 'c', 'readings': []}]}
    if group_members is not None:
        data['targets'] = ['bps_group_synthetic']
        data['contexts']['c']['target_members'] = {'bps_group_synthetic': group_members}
    now = 0
    for tracker, locations in evidence.items():
        for location, errors in locations.items():
            mark = {'time': float(now + 1), 'kind': 'known_position',
                    'target': tracker, 'floor': 'Synthetic', 'x_m': float(location), 'y_m': 0.}
            if (tracker, location) in cleared_locations:
                mark = {'time': float(now + 1), 'kind': 'clear_position', 'target': tracker}
            if group_members is not None:
                mark['target'] = 'bps_group_synthetic'
            data['annotations'].append(mark)
            for baseline, candidate in errors:
                now += 1
                rows = [{'tracker': tracker, 'receiver': r['entity_id'], 'floor': 'Synthetic',
                         'status': 'current', 'measured_distance_m': 1., 'observed': float(now),
                         'test_location': location, 'test_errors': (baseline, candidate)}
                        for r in floor['receivers']]
                invalid = (invalid_locations or {}).get((tracker, location))
                if invalid == 'stale':
                    for row in rows:
                        row['status'] = 'stale'
                elif invalid == 'missing':
                    rows = []
                elif invalid == 'insufficient':
                    rows = rows[:2]
                data['frames'].append({'time': float(now), 'context_id': 'c', 'readings': rows})
    calls = []

    def replay(floor, rows, corrections, *args, **kwargs):
        calls.append(bool(corrections))
        error = rows[0]['test_errors'][1 if corrections else 0]
        return None if error is None else {'cords': [rows[0]['test_location'] + error, 0.]}

    monkeypatch.setattr(validation, 'replay', replay)
    original = copy.deepcopy(data)
    report = validation.compare_tracking(data, 'Synthetic', {'r0': 2.}, floor, None)
    assert data == original
    assert len(calls) == 2 * report['replayed_samples']
    return report


def test_each_tracker_and_measured_location_earns_positive_evidence(monkeypatch):
    result = compare_errors(monkeypatch, {
        'tag_a': {0: [(3., 1.)] * 5, 1: [(2., .5)] * 5},
        'tag_b': {0: [(4., 2.)] * 5, 2: [(3., 1.)] * 5}})
    assert result['verdict'] == 'improves'
    assert result['verification'] == {
        'sufficient': True, 'reasons': [],
        'requirements': {'samples_per_location': 5, 'locations_per_tracker': 2},
        'observed_locations': 3, 'trackers': 2, 'sufficient_trackers': 2}
    for stats in result['trackers'].values():
        assert stats['sufficient'] and stats['improves']
        assert all(s['baseline']['samples'] == 5 and s['sufficient'] for s in stats['locations'])
        assert all('x_m' not in s and 'y_m' not in s for s in stats['locations'])


def test_different_beacons_cannot_pool_their_single_locations(monkeypatch):
    result = compare_errors(monkeypatch, {
        'tag_a': {0: [(3., 1.)] * 10}, 'tag_b': {1: [(3., 1.)] * 10}})
    assert result['baseline']['samples'] == 20
    assert result['locations'] == 2
    assert result['verdict'] == 'inconclusive'
    assert result['verification']['reasons'] == ['insufficient_tracker_locations']


def test_one_extra_observation_does_not_prove_a_second_location(monkeypatch):
    result = compare_errors(monkeypatch, {'tag': {0: [(3., 1.)] * 10, 1: [(3., 1.)]}})
    assert result['verdict'] == 'inconclusive'
    assert result['verification']['reasons'] == ['insufficient_location_samples']
    assert [s['sufficient'] for s in result['trackers']['tag']['locations']] == [True, False]


def test_aggregate_gain_does_not_prove_an_unchanged_tracker_improved(monkeypatch):
    result = compare_errors(monkeypatch, {
        'tag_a': {0: [(10., 1.)] * 20, 1: [(10., 1.)] * 20},
        'tag_b': {0: [(1., 1.)] * 5, 1: [(1., 1.)] * 5}})
    assert result['candidate']['median_m'] < result['baseline']['median_m']
    assert result['candidate']['p95_m'] < result['baseline']['p95_m']
    assert result['verification']['sufficient']
    assert result['verdict'] == 'inconclusive'
    assert result['verification']['reasons'] == ['tracker_improvement_not_demonstrated']


@pytest.mark.parametrize('bad_samples', [1, 5])
def test_pooled_gain_cannot_hide_a_regressing_measured_location(monkeypatch, bad_samples):
    result = compare_errors(monkeypatch, {'tag': {
        0: [(10., 1.)] * 100, 1: [(1., 2.)] * bad_samples}})
    assert result['candidate']['median_m'] < result['baseline']['median_m']
    assert result['candidate']['p95_m'] < result['baseline']['p95_m']
    assert result['trackers']['tag']['improves']
    assert result['verdict'] == 'worse'
    assert 'location_regression' in result['verification']['reasons']
    assert result['trackers']['tag']['locations'][1]['regression']


def test_p95_regression_alone_vetoes_a_location(monkeypatch):
    result = compare_errors(monkeypatch, {'tag': {
        0: [(10., 1.)] * 100, 1: [(2., 1.)] * 4 + [(2., 4.)]}})
    local = result['trackers']['tag']['locations'][1]
    assert local['candidate']['median_m'] < local['baseline']['median_m']
    assert local['candidate']['p95_m'] > local['baseline']['p95_m']
    assert result['verdict'] == 'worse'
    assert 'location_regression' in result['verification']['reasons']


def test_candidate_failure_retains_its_location_and_negative_verdict(monkeypatch):
    result = compare_errors(monkeypatch, {'tag': {
        0: [(3., 1.)] * 5, 1: [(3., 1.)] * 5, 2: [(3., None)]}})
    local = result['trackers']['tag']['locations'][2]
    assert result['verdict'] == 'worse'
    assert result['candidate_failures'] == local['candidate_failures'] == 1
    assert local['baseline']['samples'] == local['candidate']['samples'] == 0
    assert 'candidate_replay_failure' in result['verification']['reasons']
    assert 'insufficient_location_samples' in result['verification']['reasons']


def test_baseline_failure_cannot_supply_coverage_at_a_measured_location(monkeypatch):
    result = compare_errors(monkeypatch, {'tag': {
        0: [(3., 1.)] * 5, 1: [(None, 1.)] * 5}})
    local = result['trackers']['tag']['locations'][1]
    assert result['verdict'] == 'inconclusive'
    assert local['baseline_failures'] == 5
    assert local['replayed_samples'] == 5
    assert not local['sufficient']
    assert 'insufficient_location_samples' in result['verification']['reasons']


@pytest.mark.parametrize('invalid', ['stale', 'missing', 'insufficient'])
def test_marked_stop_without_usable_readings_cannot_disappear_from_coverage(monkeypatch, invalid):
    result = compare_errors(monkeypatch, {'tag': {
        0: [(3., 1.)] * 5, 1: [(3., 1.)] * 5, 2: [(3., 1.)] * 5}},
        invalid_locations={('tag', 2): invalid})
    assert result['eligible_samples'] == result['replayed_samples'] == 10
    assert result['locations'] == 2
    assert result['verification']['observed_locations'] == 3
    local = result['trackers']['tag']['locations'][2]
    assert local['eligible_samples'] == local['replayed_samples'] == 0
    assert not local['sufficient']
    assert result['verdict'] == 'inconclusive'
    assert result['verification']['reasons'] == ['insufficient_location_samples']


def test_cleared_truth_does_not_create_a_missing_measured_stop(monkeypatch):
    result = compare_errors(monkeypatch, {'tag': {
        0: [(3., 1.)] * 5, 1: [(3., 1.)] * 5, 2: [(3., 1.)] * 5}},
        invalid_locations={('tag', 2): 'missing'}, cleared_locations={('tag', 2)})
    assert result['verdict'] == 'improves'
    assert result['verification']['observed_locations'] == 2
    assert len(result['trackers']['tag']['locations']) == 2


def test_replay_bound_cannot_erase_a_short_measured_stop(monkeypatch):
    result = compare_errors(monkeypatch, {'tag': {
        0: [(3., 1.)] * 500, 1: [(3., 1.)], 2: [(3., 1.)] * 499}})
    assert result['eligible_samples'] == 1000
    assert result['replayed_samples'] == validation.MAX_REPLAYS == 128
    assert result['truncated']
    assert result['baseline']['samples'] == 128
    assert result['locations'] == 2
    assert result['verification']['observed_locations'] == 3
    local = result['trackers']['tag']['locations'][1]
    assert local['eligible_samples'] == 1
    assert local['replayed_samples'] == local['baseline']['samples'] == 0
    assert result['verdict'] == 'inconclusive'
    assert result['verification']['reasons'] == ['insufficient_location_samples']


def test_empty_evidence_explains_why_it_is_inconclusive(monkeypatch):
    result = compare_errors(monkeypatch, {})
    assert result['verdict'] == 'inconclusive'
    assert result['verification']['reasons'] == ['no_paired_samples']
    assert not result['verification']['sufficient']


@pytest.mark.parametrize('invalid', ['missing', 'stale', 'insufficient'])
def test_wholly_unobserved_tracker_cannot_be_omitted_from_verification(monkeypatch, invalid):
    result = compare_errors(monkeypatch, {
        'tag_a': {0: [(3., 1.)] * 5, 1: [(3., 1.)] * 5},
        'tag_b': {0: [(3., 1.)] * 5, 1: [(3., 1.)] * 5}},
        invalid_locations={('tag_b', 0): invalid, ('tag_b', 1): invalid})
    assert result['trackers']['tag_a']['sufficient']
    assert result['trackers']['tag_a']['improves']
    assert result['verification']['trackers'] == 2
    assert result['verification']['sufficient_trackers'] == 1
    missing = result['trackers']['tag_b']
    assert missing['eligible_samples'] == missing['replayed_samples'] == 0
    assert missing['baseline']['samples'] == missing['candidate']['samples'] == 0
    assert len(missing['locations']) == 2
    assert not missing['sufficient']
    assert result['verdict'] == 'inconclusive'
    assert not result['verification']['sufficient']
    assert 'insufficient_location_samples' in result['verification']['reasons']


def test_group_truth_and_context_members_require_wholly_missing_beacon(monkeypatch):
    result = compare_errors(monkeypatch, {'tag_a': {
        0: [(3., 1.)] * 5, 1: [(3., 1.)] * 5}}, group_members=['tag_a', 'tag_b'])
    assert result['baseline']['samples'] == result['replayed_samples'] == 10
    assert result['candidate']['median_m'] < result['baseline']['median_m']
    assert result['candidate']['p95_m'] < result['baseline']['p95_m']
    assert set(result['trackers']) == {'tag_a', 'tag_b'}
    assert result['verification']['trackers'] == 2
    assert result['verification']['sufficient_trackers'] == 1
    missing = result['trackers']['tag_b']
    assert missing['eligible_samples'] == missing['replayed_samples'] == 0
    assert [s['location'] for s in missing['locations']] == [1, 2]
    assert all(s['baseline']['samples'] == 0 and not s['sufficient'] for s in missing['locations'])
    assert result['verdict'] == 'inconclusive'
    assert not result['verification']['sufficient']
    assert result['verification']['reasons'] == ['insufficient_location_samples']


def test_group_with_complete_member_evidence_can_still_improve(monkeypatch):
    result = compare_errors(monkeypatch, {
        'tag_a': {0: [(3., 1.)] * 5, 1: [(3., 1.)] * 5},
        'tag_b': {0: [(3., 1.)] * 5, 1: [(3., 1.)] * 5}},
        group_members=['tag_a', 'tag_b'])
    assert result['verdict'] == 'improves'
    assert result['verification']['sufficient']
    assert result['verification']['trackers'] == result['verification']['sufficient_trackers'] == 2
    assert result['verification']['reasons'] == []


def test_all_group_members_missing_still_reports_required_beacon_count(monkeypatch):
    result = compare_errors(monkeypatch, {'tag_a': {
        0: [(3., 1.)] * 5, 1: [(3., 1.)] * 5}}, group_members=['tag_a', 'tag_b'],
        invalid_locations={('tag_a', 0): 'missing', ('tag_a', 1): 'missing'})
    assert result['eligible_samples'] == result['replayed_samples'] == 0
    assert result['verification']['observed_locations'] == 2
    assert result['verification']['trackers'] == 2
    assert result['verification']['sufficient_trackers'] == 0
    assert set(result['trackers']) == {'tag_a', 'tag_b'}
    assert all(not s['sufficient'] and s['baseline']['samples'] == 0 for s in result['trackers'].values())
    assert result['verdict'] == 'inconclusive'
    assert not result['verification']['sufficient']
    assert result['verification']['reasons'] == ['no_paired_samples', 'insufficient_location_samples']
