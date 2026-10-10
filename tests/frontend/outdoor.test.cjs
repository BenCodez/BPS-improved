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

test('mini beacon numbering remains stable when earlier members lose their fixes', () => {
    const group = {beacons: ['beacon_a', 'beacon_b']};
    assert.equal(out.beaconNumber({ent: 'beacon_b'}, group), '2');
    assert.equal(out.beaconNumber({ent: 'beacon_a'}, group), '1');
    assert.equal(out.beaconNumber({ent: 'unlisted'}, group), '•');
});

test('mini beacon icons stay small under zoom and mute excluded or aging cached fixes', () => {
    const calls = [];
    const ctx = {save() {}, restore() {}, beginPath() {}, fill() {}, stroke() {},
        arc(...args) {calls.push(args);}, setLineDash() {}, fillText(text) {calls.push(text);}};
    const beacon = {cords: [200, 100], updated: 50, age_s: 0, used: true};
    out.drawMiniBeacon(ctx, beacon, 80, 'blue', '2', 90, 1, 60000);
    assert.equal(calls[0][2], 12, 'diameter capped at 24');
    assert.equal(calls[1], '2');
    assert.equal(ctx.globalAlpha, 0.75);
    assert.equal(ctx.fillStyle, '#fff');
    calls.length = 0;
    out.drawMiniBeacon(ctx, beacon, 80, 'blue', '2', 90, 4, 60000);
    assert.equal(calls[0][2] * 4, 12, 'screen diameter stays capped under zoom');
    out.drawMiniBeacon(ctx, beacon, 40, 'blue', '2', 90, 1, 141000);
    assert.equal(ctx.globalAlpha, 0.45, 'cached source timestamp expires despite its cached age');
    assert.equal(ctx.strokeStyle, '#666');
    beacon.used = false;
    out.drawMiniBeacon(ctx, beacon, 40, 'blue', '2', 90, 1, 60000);
    assert.equal(ctx.globalAlpha, 0.45, 'excluded but fresh fixes are subdued');
    for (const cords of [null, [200], [NaN, 100], ['200', 100]]) {
        const before = calls.length;
        assert.equal(out.drawMiniBeacon(ctx, {...beacon, cords}, 40, 'blue', '2', 90), false);
        assert.equal(calls.length, before);
    }
});

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

test('disabled reading gate uses the solve clock without refreshing it on polls', () => {
    const row = {updated: 200, outdoor: {observed: 5, use_observation_age: false,
        stale_after_s: 120, position_age_s: 0}};
    assert.equal(out.fixTime(row, 250000), 200000);
    assert.equal(out.fixTime(row, 310000), 200000);
    assert.equal(out.isStale(row.outdoor, out.fixTime(row, 310000), 310000), false);
    assert.equal(out.isStale(row.outdoor, out.fixTime(row, 320000), 320000), true);
    row.outdoor.use_observation_age = true;
    assert.equal(out.fixTime(row, 250000), 5000, 'enabled gate retains measurement freshness');
    assert.equal(out.isStale(row.outdoor, out.fixTime(row, 250000), 250000), true);
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

test('environment polygon capacity rejects insertion atomically and permits editing at capacity', () => {
    const item = {id: 'shop', name: 'Shop', type: 'building', material: 'metal', points: polygon};
    const floor = {environment: Array.from({length: 127}, (_, i) => ({...item, id: `area_${i}`}))};
    out.upsertEnvironment(floor, item);
    assert.equal(floor.environment.length, 128);
    const before = JSON.stringify(floor);
    assert.throws(() => out.upsertEnvironment(floor, {...item, id: 'extra'}), /at most 128 environment polygons/);
    assert.equal(JSON.stringify(floor), before);
    out.upsertEnvironment(floor, {...item, name: 'Renamed shop', points: polygon.map(p => ({x: p.x + 10, y: p.y}))});
    assert.equal(floor.environment.length, 128);
    assert.equal(floor.environment[127].name, 'Renamed shop');
    assert.equal(floor.environment[127].points[0].x, 10);
});

test('group editing preserves original beacons, updates in place, and rejects entity IDs or nested groups', () => {
    const layout = {tracker_icons: {beacon_a: 'person.svg'}};
    out.upsertGroup(layout, {id: 'rover', name: 'Rover', enabled: true, beacons: ['beacon_a', 'beacon_b', 'beacon_a']});
    assert.deepEqual(plain(layout.tracker_groups[0].beacons), ['beacon_a', 'beacon_b']);
    out.upsertGroup(layout, {id: 'rover', name: 'Rover renamed', enabled: false, beacons: ['beacon_a']});
    assert.equal(layout.tracker_groups.length, 1);
    assert.equal(layout.tracker_groups[0].enabled, false);
    assert.equal(layout.tracker_icons.beacon_a, 'person.svg');
    for (const beacon of ['sensor.beacon_a', 'bps_group_rover']) assert.throws(() => out.upsertGroup(layout, {id: 'rover', name: 'Dog', enabled: true, beacons: [beacon]}));
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
        ...['A', 'bad slug', 'beacon\n', 'b'.repeat(65), 'bps_group_dog_0'].map(b => ({...group, beacons: [b]}))]) {
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
        outdoor: {estimated_uncertainty_m: 7.4, confidence: 'moderate', receivers_used: 4,
            residual_m: 2, noise_floor_m: 1.5, geometry_factor: 1.2, effective_receivers: 3.5,
            beacons_used: 2, combined_beacon_uncertainty_m: 2.5, beacon_scatter_m: 1,
            receiver_diagnostics: [{receiver: 'shop', measured_distance_m: 15,
            corrected_distance_m: 13, reading_age_s: 2, classification: 'building_exit', building_crossings: 1, reflection_risk: true, environmental_weight: .4, reliability_weight: .3, status: 'down_weighted'}]}});
    assert.match(text, /2\/2 beacons/);
    assert.match(text, /disagreement 4.5 m/);
    assert.match(text, /measured 15.0 m, corrected 13.0 m/);
    assert.match(text, /reflection \/ multipath risk/);
    assert.match(text, /weighted residual 2.0 m · noise floor 1.5 m · geometry ×1.2 · effective receivers 3.5/);
    assert.match(text, /2 beacons combined · beacon uncertainty 2.5 m · position spread 1.0 m/);
});

test('tracking diagnostics disclose neutral obstruction fallback', () => {
    const text = out.diagnosticsText({outdoor: {estimated_uncertainty_m: 2,
        confidence: 'poor', environment_fallback: true}});
    assert.match(text, /neutral obstruction weights used; confidence reduced/);
    assert.doesNotMatch(out.diagnosticsText({outdoor: {estimated_uncertainty_m: 2}}), /unavailable/);
});

test('unknown prefixed beacon members survive missing inventory but configured groups cannot nest', () => {
    const layout = {tracker_groups: [{id: 'other', name: 'Other', enabled: false, beacons: ['beacon_a']}]};
    const group = {id: 'rover', name: 'Rover', beacons: ['bps_group_beacon']};
    out.upsertGroup(layout, group, ['bps_group_beacon']);
    out.upsertGroup(layout, {...group, name: 'Rover renamed'}, []);
    assert.deepEqual(plain(layout.tracker_groups[1].beacons), ['bps_group_beacon']);
    const before = JSON.stringify(layout);
    assert.throws(() => out.upsertGroup(layout, {...group, beacons: ['bps_group_other']}));
    assert.equal(JSON.stringify(layout), before);
    out.upsertGroup(layout, {...group, beacons: ['bps_group_other']}, ['bps_group_other']);
    assert.deepEqual(plain(layout.tracker_groups[1].beacons), ['bps_group_other']);
});

test('polygon names require nonempty bounded text before layout mutation', () => {
    const floor = {};
    const item = {id: 'shop', name: 'Metal shop', type: 'building', material: 'metal', points: polygon};
    for (const name of [undefined, null, false, 3, {}, [], '', ' \t', 'x'.repeat(257)]) {
        assert.throws(() => out.upsertEnvironment(floor, {...item, name}));
        assert.deepEqual(plain(floor), {});
    }
    out.upsertEnvironment(floor, {...item, name: 'x'.repeat(256)});
    assert.equal(floor.environment[0].name.length, 256);
});
