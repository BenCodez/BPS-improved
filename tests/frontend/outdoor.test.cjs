const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const {join} = require('node:path');
const frontend = join(__dirname, '../../custom_components/bps/frontend');
const context = vm.createContext({Date});
vm.runInContext(readFileSync(join(frontend, 'outdoor.js'), 'utf8'), context);
const out = context.BPSOutdoor;
const plain = value => JSON.parse(JSON.stringify(value));
const polygon = [{x: 0, y: 0}, {x: 100, y: 0}, {x: 100, y: 100}, {x: 0, y: 100}];

test('old layouts do not enable or mutate optional tracking', () => {
    const layout = {floor: [{name: 'Property', scale: 20, zones: [], receivers: []}]};
    const before = JSON.stringify(layout);
    assert.equal(out.settings(layout).enabled, false);
    assert.equal(out.uncertaintyRadius({estimated_uncertainty_m: 7.4}, 20, out.settings(layout)), null);
    assert.equal(JSON.stringify(layout), before);
    assert.equal(out.settings({outdoor_tracking: {enabled: 'true'}}).enabled, false);
    assert.deepEqual(plain(out.groups({tracker_groups: {}})), []);
    assert.deepEqual(plain(out.environment({environment: [null, 3]})), []);
});

test('uncertainty settings honor backend threshold bounds without rewriting stored values', () => {
    for (const value of [0, 10000]) {
        assert.equal(out.settings({outdoor_tracking: {hide_uncertainty_below_m: value}}).hide_uncertainty_below_m, value);
    }
    const layout = {outdoor_tracking: {enabled: true, hide_uncertainty_below_m: 10000.1}};
    const before = JSON.stringify(layout);
    assert.equal(out.settings(layout).hide_uncertainty_below_m, 0);
    assert.equal(JSON.stringify(layout), before);
});

test('uncertainty is radius in floor pixels, respects toggle/threshold, and rejects invalid numbers', () => {
    const config = out.settings({outdoor_tracking: {enabled: true, hide_uncertainty_below_m: 5}});
    assert.equal(out.uncertaintyRadius({estimated_uncertainty_m: 7.4}, 20, config), 148);
    assert.equal(out.uncertaintyRadius({estimated_uncertainty_m: 4}, 20, config), null);
    assert.equal(out.uncertaintyRadius({estimated_uncertainty_m: 5}, 20, config), 100);
    assert.equal(out.uncertaintyRadius({estimated_uncertainty_m: 7}, 20, {...config, show_uncertainty: false}), null);
    for (const bad of [NaN, Infinity, -1, '7']) assert.equal(out.uncertaintyRadius({estimated_uncertainty_m: bad}, 20, config), null);
    for (const bad of [NaN, Infinity, 0, -20, '20']) assert.equal(out.uncertaintyRadius({estimated_uncertainty_m: 7}, bad, config), null);
});

test('canvas circle keeps world radius under zoom and distinguishes stale/poor fixes', () => {
    const calls = [];
    const ctx = {save() {}, restore() {}, beginPath() {}, fill() {}, stroke() {},
        arc(...args) {calls.push(['arc', ...args]);}, setLineDash(values) {calls.push(['dash', ...values]);}};
    const config = {enabled: true, show_uncertainty: true};
    const pos = {x: 200, y: 100, outdoor: {estimated_uncertainty_m: 7.4, confidence: 'good'}, receivedAt: 1000};
    assert.equal(out.drawUncertainty(ctx, pos, 20, config, 'blue', 3, 2000), true);
    assert.deepEqual(calls[0].slice(0, 4), ['arc', 200, 100, 148]);
    assert.equal(ctx.lineWidth, 2 / 3);
    assert.deepEqual(calls[1], ['dash']);
    calls.length = 0;
    out.drawUncertainty(ctx, pos, 20, config, 'blue', 3, 40000);
    assert.deepEqual(calls[1], ['dash', 1, 2]);
    assert.equal(ctx.strokeStyle, '#888');
    pos.outdoor.confidence = 'poor'; calls.length = 0;
    out.drawUncertainty(ctx, pos, 20, config, 'blue', 1, 2000);
    assert.deepEqual(calls[1], ['dash', 8, 5]);
});

test('polling an old observation does not refresh freshness and backend stale threshold is respected', () => {
    const now = 100000;
    assert.equal(out.fixTime({updated: 90, outdoor: {observed: 50}}, now), 50000);
    assert.equal(out.fixTime({updated: 80}, now), 80000);
    assert.equal(out.fixTime({outdoor: {position_age_s: 45}}, now), 55000);
    assert.equal(out.fixTime({updated: NaN}, now), now);
    assert.equal(out.isStale({stale_after_s: 60}, 50000, now), false);
    assert.equal(out.isStale({stale_after_s: 60}, 40000, now), true);
    assert.equal(out.isStale({position_age_s: 45, stale_after_s: 60}), false);
    assert.equal(out.isStale({position_age_s: 60, stale_after_s: 60}), true);
});

test('environment edits clone points, retain IDs, survive JSON storage, and reject invalid geometry atomically', () => {
    const floor = {zones: [{entity_id: 'Yard'}], receivers: [{entity_id: 'shop'}]};
    out.upsertEnvironment(floor, {id: 'shop', name: ' Shop ', type: 'building', material: 'metal', points: polygon});
    assert.equal(floor.environment[0].name, 'Shop');
    assert.notEqual(floor.environment[0].points, polygon);
    const loaded = JSON.parse(JSON.stringify(floor));
    out.upsertEnvironment(loaded, {...loaded.environment[0], name: 'Metal shop', points: polygon.map(p => ({x: p.x + 10, y: p.y}))});
    assert.equal(loaded.environment.length, 1);
    assert.equal(loaded.environment[0].id, 'shop');
    assert.equal(loaded.environment[0].points[0].x, 10);
    assert.deepEqual(loaded.zones, floor.zones);
    const before = JSON.stringify(loaded);
    for (const bad of [[], [{x: 0, y: 0}, {x: 1, y: 1}, {x: 2, y: 2}],
        [{x: 0, y: 0}, {x: 1, y: 1}, {x: 0, y: 1}, {x: 1, y: 0}],
        [{x: Infinity, y: 0}, ...polygon]]) {
        assert.throws(() => out.upsertEnvironment(loaded, {...loaded.environment[0], points: bad}));
        assert.equal(JSON.stringify(loaded), before);
    }
});

test('group editing preserves original beacons, updates in place, and rejects entity IDs or nested groups', () => {
    const layout = {tracker_icons: {beacon_a: 'person.svg'}};
    out.upsertGroup(layout, {id: 'rover', name: 'Rover', enabled: true, beacons: ['beacon_a', 'beacon_b', 'beacon_a']});
    assert.deepEqual(plain(layout.tracker_groups[0].beacons), ['beacon_a', 'beacon_b']);
    out.upsertGroup(layout, {id: 'rover', name: 'Rover renamed', enabled: false, beacons: ['beacon_a']});
    assert.equal(layout.tracker_groups.length, 1);
    assert.equal(layout.tracker_groups[0].enabled, false);
    assert.equal(layout.tracker_icons.beacon_a, 'person.svg');
    for (const beacon of ['sensor.beacon_a', 'bps_group_other']) assert.throws(() => out.upsertGroup(layout, {id: 'rover', name: 'Dog', enabled: true, beacons: [beacon]}));
});

test('group readers and writers use backend optional defaults without mutating input', () => {
    const layout = {tracker_groups: [{id: 'rover', beacons: ['beacon_a']},
        {id: 'paused', name: '  ', enabled: false, beacons: ['beacon_b']}]};
    const original = plain(layout);
    assert.deepEqual(plain(out.groups(layout)), [{id: 'rover', name: 'rover', enabled: true, beacons: ['beacon_a']},
        {id: 'paused', name: 'paused', enabled: false, beacons: ['beacon_b']}]);
    assert.deepEqual(plain(layout), original);
    out.upsertGroup(layout, {id: 'spot', name: 'Spot', beacons: ['beacon_a']});
    assert.equal(layout.tracker_groups.find(g => g.id === 'spot').enabled, true);
});

test('group validation accepts exact backend limits and rejects overflow before mutation', () => {
    const beacons = Array.from({length: 16}, (_, i) => `beacon_${i}`);
    const group = {id: 'r'.repeat(64), name: 'Rover', enabled: true, beacons};
    const layout = {tracker_groups: Array.from({length: 31}, (_, i) => ({...group, id: `dog_${i}`}))};
    out.upsertGroup(layout, group);
    assert.equal(layout.tracker_groups.length, 32);
    assert.equal(layout.tracker_groups[31].id.length, 64);
    assert.equal(layout.tracker_groups[31].beacons.length, 16);
    out.upsertGroup(layout, {...group, name: 'Renamed'});
    assert.equal(layout.tracker_groups[31].name, 'Renamed');
    const before = JSON.stringify(layout);
    for (const invalid of [{...group, id: 'r'.repeat(65)},
        {...group, beacons: [...beacons, 'beacon_16']}, {...group, id: 'dog_32'},
        ...['A', 'bad slug', 'beacon\n', 'b'.repeat(65), 'bps_group_other'].map(b => ({...group, beacons: [b]}))]) {
        assert.throws(() => out.upsertGroup(layout, invalid));
        assert.equal(JSON.stringify(layout), before);
    }
    const accepted = {};
    out.upsertGroup(accepted, {...group, id: '_rover__', beacons: ['_beacon__', 'b'.repeat(64)]});
    assert.equal(accepted.tracker_groups[0].id, '_rover__');
});

test('known tracker collisions reject group creation and activation while permitting retirement', () => {
    const known = ['beacon_a', 'bps_group_rover'];
    const group = {id: 'rover', name: 'Rover', beacons: ['beacon_a']};
    const layout = {tracker_icons: {bps_group_rover: 'beacon.svg'}};
    const before = JSON.stringify(layout);
    for (const enabled of [true, false]) {
        assert.throws(() => out.upsertGroup(layout, {...group, enabled}, known), /conflicts with real tracker 'bps_group_rover'/);
        assert.equal(JSON.stringify(layout), before);
    }
    out.upsertGroup(layout, {...group, id: 'rover_dog'}, known);
    assert.equal(layout.tracker_groups[0].id, 'rover_dog');
    const saved = {tracker_groups: [{...group, enabled: false}]};
    const savedBefore = JSON.stringify(saved);
    for (const enabled of [true, undefined]) {
        assert.throws(() => out.upsertGroup(saved, {...group, enabled}, known), /conflicts with real tracker/);
        assert.equal(JSON.stringify(saved), savedBefore);
    }
    out.upsertGroup(saved, {...group, name: 'Retired group', enabled: false}, known);
    assert.equal(saved.tracker_groups[0].enabled, false);
});

test('diagnostics explain measurement trust and fused beacon disagreement', () => {
    const text = out.diagnosticsText({ent: 'bps_group_rover', group: true, name: 'Rover', beacons_reporting: 2, total_beacons: 2,
        beacon_disagreement_m: 4.5, fusion_confidence: 'moderate', beacon_positions: [{ent: 'beacon_a', estimated_uncertainty_m: 3}],
        outdoor: {estimated_uncertainty_m: 7.4, confidence: 'moderate', receivers_used: 4, receiver_diagnostics: [{receiver: 'shop', measured_distance_m: 15,
            corrected_distance_m: 13, reading_age_s: 2, classification: 'building_exit', building_crossings: 1, reflection_risk: true, environmental_weight: .4, reliability_weight: .3, status: 'down_weighted'}]}});
    assert.match(text, /2\/2 beacons/);
    assert.match(text, /disagreement 4.5 m/);
    assert.match(text, /measured 15.0 m, corrected 13.0 m/);
    assert.match(text, /reflection \/ multipath risk/);
});
