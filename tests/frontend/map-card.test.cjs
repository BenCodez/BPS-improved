const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const directory = join(__dirname, '../../custom_components/bps/frontend');
const classes = new Map();
const sandbox = vm.createContext({Date, console: {warn() {}}, HTMLElement: class {}, window: {},
    customElements: {define: (name, type) => classes.set(name, type)}});
vm.runInContext(readFileSync(join(directory, 'outdoor.js'), 'utf8'), sandbox);
// Node's VM has no browser URL loader; the same helper is loaded above.
vm.runInContext(readFileSync(join(directory, 'bps-map-card.js'), 'utf8')
    .replace("const bpsOutdoorReady = import('./outdoor.js');", 'const bpsOutdoorReady = Promise.resolve();'), sandbox);
const Card = classes.get('bps-map-card');
const plain = x => JSON.parse(JSON.stringify(x));
function card() {
    const item = Object.create(Card.prototype);
    Object.assign(item, {_config: {floor: 'Property', entities: ['sensor.beacon_a'], show_uncertainty: true, scale_icon: 100,
        scale_labels: 100, show_receivers: false, show_labels: true}, _positions: new Map(), _entityByTrackerKey: new Map(),
        _hass: {states: {'sensor.beacon_a_bps_floor': {state: 'Property'}}}, _imgNaturalW: 2000,
        _outdoorSettings: {enabled: false, show_uncertainty: true}, _floorScale: 20, _runGeneration: 1});
    return item;
}
function canvas() {
    const calls = [], dashes = [];
    const ctx = {save() {}, restore() {}, beginPath() {}, fill() {}, stroke() {}, setLineDash(dash) {dashes.push([...dash]);}, moveTo() {}, lineTo() {},
        arc(...args) {calls.push(args);}, fillText(text) {calls.push(text);}, measureText() {return {width: 50};}};
    return {calls, dashes, value: {width: 2000, height: 1000, getContext: () => ctx}};
}

test('card polling holds short radius spikes while retaining raw diagnostics and one circle', async () => {
    const c = card(), cv = canvas();
    c._canvas = cv.value; c._outdoorSettings = {enabled: true, show_uncertainty: true};
    c._getIconImage = () => null; c._trackerIconUrl = () => 'beacon.svg';
    c._redraw = () => {cv.calls.length = 0; c._drawMarkers();};
    let now = 100000;
    class ClockDate extends Date {static now() {return now;}}
    sandbox.Date = ClockDate;
    try {
        for (const [seconds, radius] of [[0, 3], [1, 120], [2, 120], [3, 120], [4, 3]]) {
            now = 100000 + seconds * 1000;
            const row = {ent: 'beacon_a', floor: 'Property', cords: [200, 100], updated: now / 1000,
                outdoor: {estimated_uncertainty_m: radius, observed: now / 1000}};
            c._apiFetch = async () => ({ok: true, json: async () => [row]});
            await c._pollOnce();
            assert.equal(c._positions.get('beacon_a').payload.outdoor.estimated_uncertainty_m, radius);
            assert.deepEqual(cv.calls.filter(a => Array.isArray(a) && a[2] > 24).map(a => a[2]), [60]);
        }
    } finally {
        sandbox.Date = Date;
    }
});

test('card polling caps displayed uncertainty and exposes cap diagnostics without changing raw payload', async () => {
    const c = card(), cv = canvas(); c._canvas = cv.value;
    c._outdoorSettings = {enabled: true, show_uncertainty: true}; c._config.show_outdoor_diagnostics = true;
    c._getIconImage = () => null; c._trackerIconUrl = () => 'beacon.svg';
    c._baseImage = {}; c._updateFloorStatus = () => {};
    let status; c._setStatus = text => {status = text;};
    c._redraw = () => {cv.calls.length = 0; c._drawMarkers();};
    const row = {ent: 'beacon_a', floor: 'Property', cords: [200, 100], updated: Date.now() / 1000,
        outdoor: {estimated_uncertainty_m: 120, display_radius_limit_m: 8}};
    c._apiFetch = async () => ({ok: true, json: async () => [row]});
    await c._pollOnce();
    assert.deepEqual(cv.calls.filter(a => Array.isArray(a) && a[2] > 24).map(a => a[2]), [160]);
    assert.equal(c._positions.get('beacon_a').payload.outdoor.estimated_uncertainty_m, 120);
    assert.match(status, /capped for display.*displayed 8.0 m/);
});

test('card keeps outside-map icons visible without moving uncertainty and labels detection-only markers', () => {
    const c = card(), cv = canvas(); c._canvas = cv.value; c._outdoorSettings.enabled = true;
    c._getIconImage = () => null; c._trackerIconUrl = () => 'beacon.svg';
    const raw = {x: 2200, y: 500, label: 'Beacon A', outdoor: {estimated_uncertainty_m: 8}};
    c._positions.set('beacon_a', raw); c._drawMarkers();
    const arcs = cv.calls.filter(Array.isArray);
    assert.deepEqual(arcs.map(a => a.slice(0, 3)), [[2200, 500, 160], [1974, 500, 24], [1974, 500, 20]]);
    assert.ok(cv.calls.some(c => typeof c === 'string' && /Off map.*inferred/.test(c)));
    assert.equal(raw.x, 2200);
    raw.x = 300; raw.outdoor.detection_only = true; raw.outdoor.direction_known = false;
    c._config.show_labels = false; cv.calls.length = 0; c._drawMarkers();
    assert.ok(cv.calls.some(c => typeof c === 'string' && /Detected only.*direction unknown/.test(c)));
});

test('card loads floor scale/settings from authenticated existing layout endpoint', async () => {
    const c = card();
    let url;
    c._apiFetch = async path => {url = path; return {ok: true, json: async () => ({coordinates: JSON.stringify({
        outdoor_tracking: {enabled: true, show_uncertainty: true, hide_uncertainty_below_m: 2},
        floor: [{name: 'Property', scale: 20, receivers: [], zones: []}]})})};};
    c._refreshBermudaScanners = async () => {};
    c._computeReceiverStatuses = () => new Map();
    c._resolveMapUrl = async () => 'property.png';
    c._loadFloorImage = async () => {c._baseImage = {};};
    await c._loadFloorResources(1);
    assert.equal(url, '/api/bps/read_text');
    assert.equal(c._floorScale, 20);
    assert.equal(c._outdoorSettings.enabled, true);
    assert.equal(c._outdoorSettings.hide_uncertainty_below_m, 2);
});

test('card rendering preserves old markers and adds correctly scaled optional uncertainty', () => {
    const c = card(); const cv = canvas(); c._canvas = cv.value;
    c._getIconImage = () => null; c._trackerIconUrl = () => 'beacon.svg';
    c._positions.set('beacon_a', {x: 200, y: 100, label: 'Beacon A', outdoor: {estimated_uncertainty_m: 7.4}});
    c._drawMarkers();
    assert.equal(cv.calls.filter(Array.isArray).length, 1);
    assert.equal(cv.calls.find(Array.isArray)[2], 20); // original fallback icon radius
    c._outdoorSettings.enabled = true; cv.calls.length = 0; c._drawMarkers();
    assert.equal(cv.calls.filter(Array.isArray).length, 2);
    assert.equal(cv.calls[0][2], 148);
    c._config.show_uncertainty = false; cv.calls.length = 0; c._drawMarkers();
    assert.equal(cv.calls.filter(Array.isArray).length, 1);
    c._config.show_uncertainty = true; c._outdoorSettings.hide_uncertainty_below_m = 8;
    cv.calls.length = 0; c._drawMarkers();
    assert.equal(cv.calls.filter(Array.isArray).length, 1);
});

test('card keeps exactly one uncertainty circle per selected tracker including a group', () => {
    const c = card(), cv = canvas(); c._canvas = cv.value;
    c._outdoorSettings.enabled = true; c._config.show_outdoor_diagnostics = true;
    c._config.entities.push('sensor.bps_group_rover_bps_position');
    c._entityByTrackerKey.set('bps_group_rover', 'sensor.bps_group_rover_bps_position');
    c._hass.states['sensor.bps_group_rover_bps_position'] = {state: 'tracking', attributes: {group: true, floor: 'Property'}};
    c._getIconImage = () => null; c._trackerIconUrl = () => 'beacon.svg';
    c._positions.set('beacon_a', {x: 100, y: 100, outdoor: {estimated_uncertainty_m: 3}});
    c._positions.set('bps_group_rover', {x: 200, y: 100, outdoor: {estimated_uncertainty_m: 4},
        payload: {group: true, beacon_positions: [
            {ent: 'beacon_a', cords: [100, 100], estimated_uncertainty_m: 3},
            {ent: 'beacon_b', cords: [220, 100], estimated_uncertainty_m: 3}]}});
    c._drawMarkers();
    assert.deepEqual(cv.calls.filter(v => Array.isArray(v) && v[2] >= 60).map(v => v.slice(0, 3)),
        [[100, 100, 60], [200, 100, 80]], 'only the individual tracker and the main group have uncertainty circles');
});

test('card shows quiet member icons without diagnostics and retains one main group icon', () => {
    const c = card(), cv = canvas(); c._canvas = cv.value;
    c._config.entities = ['sensor.bps_group_rover_bps_zone'];
    c._entityByTrackerKey.set('bps_group_rover', c._config.entities[0]);
    c._hass.states['sensor.bps_group_rover_bps_floor'] = {state: 'Property'};
    c._outdoorSettings = {enabled: true, show_uncertainty: false};
    c._trackerGroups = [{id: 'rover', beacons: ['beacon_a', 'beacon_b']}];
    c._getIconImage = () => null; c._trackerIconUrl = () => 'beacon.svg';
    const beaconA = {ent: 'beacon_a', floor: 'Property', cords: [100, 100]};
    const beaconB = {ent: 'beacon_b', floor: 'Property', cords: [300, 100]};
    const row = {group: true, beacon_positions: [beaconA, beaconB,
        {ent: 'other_floor', floor: 'Barn', cords: [400, 300]}, {ent: 'invalid', cords: [null, 100]}, null]};
    c._positions.set('bps_group_rover', {x: 200, y: 100, label: 'Rover', payload: row});
    c._drawMarkers();
    assert.deepEqual(cv.calls.filter(Array.isArray).map(v => v.slice(0, 3)), [[100, 100, 6], [300, 100, 6], [200, 100, 20]],
        'two tiny markers drawn underneath one full-size marker; no diagnostic circles');
    assert.deepEqual(cv.calls.filter(v => typeof v === 'string'), ['1', '2', 'Rover'], 'no member name labels');
    row.beacon_positions = [beaconB]; cv.calls.length = 0; c._drawMarkers();
    assert.deepEqual(cv.calls.filter(v => typeof v === 'string'), ['2', 'Rover']);
    c._outdoorSettings.enabled = false; cv.calls.length = 0; c._drawMarkers();
    assert.equal(cv.calls.filter(Array.isArray).length, 1, 'disabled outdoor mode retains only the old main marker');
});

test('group zone sensor resolves fused map key/name and shows constituent beacon diagnostics', async () => {
    const c = card(); const cv = canvas(); c._canvas = cv.value;
    c._config.entities = ['sensor.bps_group_rover_bps_zone']; c._config.show_outdoor_diagnostics = true;
    c._entityByTrackerKey.set('bps_group_rover', c._config.entities[0]);
    c._hass.states['sensor.bps_group_rover_bps_floor'] = {state: 'Property'};
    c._outdoorSettings.enabled = true;
    const row = {ent: 'bps_group_rover', group: true, name: 'Rover', floor: 'Property', cords: [200, 100], zone: 'Yard',
        beacons_reporting: 2, total_beacons: 2, beacon_disagreement_m: 2,
        outdoor: {estimated_uncertainty_m: 4, confidence: 'good'}, beacon_positions: [{ent: 'beacon_a', cords: [205, 100], estimated_uncertainty_m: 3}]};
    c._apiFetch = async () => ({ok: true, json: async () => [row]});
    c._baseImage = {}; c._redraw = () => {}; c._setStatus = text => {c.status = text;};
    await c._pollOnce();
    assert.equal(c._trackerKeyFromEntity('sensor.bps_group_rover_bps_zone'), 'bps_group_rover');
    assert.equal(c._trackerKeyFromEntity('sensor.phone_bps_zone'), 'phone_bps_zone'); // old key handling preserved
    assert.equal(c._positions.get('bps_group_rover').label, 'Rover');
    assert.deepEqual(plain(c._positions.get('bps_group_rover').payload), row);
    assert.match(c.status, /2\/2 beacons/);
    c._getIconImage = () => null; c._trackerIconUrl = () => 'beacon.svg'; c._drawMarkers();
    assert.ok(cv.calls.some(v => Array.isArray(v) && v[2] === 80)); // fused radius
    assert.ok(!cv.calls.some(v => Array.isArray(v) && v[2] === 60)); // member has no second uncertainty circle
    assert.ok(cv.calls.includes('Rover'));
    row.beacon_positions.push({ent: 'beacon_b', floor: 'Barn', cords: [400, 300], estimated_uncertainty_m: 9});
    cv.calls.length = 0; c._drawMarkers();
    assert.ok(!cv.calls.some(v => Array.isArray(v) && v[0] === 400 && v[1] === 300), 'foreign floor pixels are never rendered here');
    row.updated = 1; row.outdoor.observed = .5;
    await c._pollOnce();
    assert.equal(c._positions.get('bps_group_rover').receivedAt, 500, 'repeated poll preserves source observation time');
});

test('card draws one group uncertainty circle even with member diagnostics enabled', () => {
    const c = card(), cv = canvas(), now = Date.now(); c._canvas = cv.value;
    c._config.entities = ['sensor.bps_group_rover_bps_zone']; c._config.show_outdoor_diagnostics = true;
    c._entityByTrackerKey.set('bps_group_rover', c._config.entities[0]);
    c._hass.states['sensor.bps_group_rover_bps_floor'] = {state: 'Property'};
    c._outdoorSettings.enabled = true;
    c._getIconImage = () => null; c._trackerIconUrl = () => 'beacon.svg';
    const beacon = {ent: 'beacon_a', floor: 'Property', cords: [205, 100], age_s: 45,
        updated: now / 1000 - 45, estimated_uncertainty_m: 3};
    const pos = {x: 200, y: 100, receivedAt: now, outdoor: {estimated_uncertainty_m: 4, stale_after_s: 90},
        payload: {group: true, beacon_positions: [beacon]}};
    c._positions.set('bps_group_rover', pos); c._drawMarkers();
    assert.equal(cv.calls.filter(v => Array.isArray(v) && v[2] >= 60).length, 1);
    assert.ok(cv.calls.some(v => Array.isArray(v) && v[2] === 80));
    assert.ok(!cv.dashes.some(dash => dash[0] === 3 && dash[1] === 6), '45-second beacon remains fresh with a 90-second cutoff');
    // Member age affects the mini icon, not another uncertainty circle.
    beacon.updated = now / 1000 - 91;
    cv.dashes.length = 0; c._drawMarkers();
    assert.ok(!cv.dashes.some(dash => dash[0] === 3 && dash[1] === 6));
    assert.ok(cv.dashes.some(dash => dash[0] === 2 && dash[1] === 2));
    pos.receivedAt = now - 91 * 1000;
    cv.dashes.length = 0; c._drawMarkers();
    assert.ok(cv.dashes.some(dash => dash[0] === 3 && dash[1] === 6), 'only the main group circle ages with its own fix');
    pos.outdoor.stale_after_s = 300; beacon.age_s = 200; beacon.updated = now / 1000 - 200;
    cv.dashes.length = 0; c._drawMarkers();
    assert.ok(!cv.dashes.some(dash => dash[0] === 3 && dash[1] === 6), 'disabled reading gate uses the group position timeout');
});

for (const enabled of [false, true]) {
    for (const suffix of ['zone', 'floor']) {
        test(`card preserves genuine group-prefixed beacon ending in ${suffix} with outdoor ${enabled}`, async () => {
            const c = card(), cv = canvas(); c._canvas = cv.value;
            const key = `bps_group_rover_bps_${suffix}`, entity = `sensor.${key}`;
            c._config.entities = [entity];
            c._outdoorSettings.enabled = enabled;
            c._hass.states = {[entity]: {state: 'home', attributes: {}},
                [`sensor.${key}_bps_floor`]: {state: 'Property'}};
            c._apiFetch = async () => ({ok: true, json: async () => [{ent: key, floor: 'Property', cords: [200, 100]}]});
            c._redraw = () => {}; c._setStatus = () => {};
            c._getIconImage = () => null; c._trackerIconUrl = () => 'beacon.svg';
            // Even a previously identified group cannot override an actual raw beacon key.
            c._groupTrackerKeys = new Set(['bps_group_rover']);
            await c._pollOnce();
            assert.equal(c._trackerKeyFromEntity(entity), key);
            assert.ok(c._positions.has(key));
            assert.equal(c._entityByTrackerKey.get(key), entity);
            c._drawMarkers();
            assert.equal(cv.calls.filter(Array.isArray).length, 1);
            assert.equal(cv.calls.find(Array.isArray)[0], 200);
        });
    }
}

for (const kind of ['zone', 'floor']) {
    test(`card resolves renamed group ${kind} sensors by stable metadata and clears removed markers`, async () => {
        const c = card(), cv = canvas(); c._canvas = cv.value;
        const entity = `sensor.rover_${kind}`, key = 'bps_group_rover';
        c._config.entities = [entity];
        c._hass.states = {[entity]: {state: kind === 'zone' ? 'Yard' : 'Property', attributes: {
            group: true, tracker_key: key, floor: 'Property', zone: 'Yard', friendly_name: 'Rover renamed'}}};
        const row = {ent: key, group: true, name: 'Rover', floor: 'Property', zone: 'Yard', cords: [200, 100]};
        c._apiFetch = async () => ({ok: true, json: async () => [row]});
        c._redraw = () => {}; c._setStatus = () => {};
        c._getIconImage = () => null; c._trackerIconUrl = () => 'beacon.svg';
        await c._pollOnce();
        assert.equal(c._trackerKeyFromEntity(entity), key);
        assert.ok(c._positions.has(key));
        assert.equal(c._floorPresenceSignature(), 'Property');
        assert.equal(c._zoneLabelSignature(), 'Yard');
        c._drawMarkers();
        assert.equal(cv.calls.filter(Array.isArray).length, 1);
        // Floor metadata changes remain authoritative with both sensors renamed.
        c._hass.states[entity].attributes.floor = 'Barn';
        await c._pollOnce();
        assert.ok(!c._positions.has(key));
        c._hass.states[entity].attributes.floor = 'Property';
        await c._pollOnce();
        assert.ok(c._positions.has(key));
        delete c._hass.states[entity];
        c._apiFetch = async () => ({ok: true, json: async () => []});
        await c._pollOnce();
        assert.equal(c._trackerKeyFromEntity(entity), key, 'cached identity permits cleanup after deletion');
        assert.ok(!c._positions.has(key));
    });
}

test('failed card poll repaints cached fixes so freshness can expire', async () => {
    const c = card();
    c._outdoorSettings.enabled = true;
    const position = {x: 200, y: 100, receivedAt: 1, outdoor: {estimated_uncertainty_m: 4, stale_after_s: 30}};
    c._positions.set('beacon_a', position);
    c._apiFetch = async () => {throw new Error('connection lost');};
    let repaints = 0;
    c._redraw = () => {repaints++; assert.equal(sandbox.BPSOutdoor.isStale(position.outdoor, position.receivedAt), true);};
    await c._pollOnce();
    assert.equal(repaints, 1);
    assert.equal(c._positions.get('beacon_a'), position);
    c._outdoorSettings.enabled = false;
    await c._pollOnce();
    assert.equal(repaints, 1, 'legacy failed-poll behavior is unchanged');
});

for (const kind of ['partial', 'empty', 'no-data']) {
    test(`card clears vanished group marker and diagnostics on authoritative ${kind} response`, async () => {
        const c = card(); const cv = canvas(); c._canvas = cv.value;
        c._config.entities.push('sensor.bps_group_rover_bps_zone');
        c._config.show_outdoor_diagnostics = true; c._outdoorSettings.enabled = true;
        // Keep HA floor state unchanged to exercise the API cache cleanup.
        c._hass.states['sensor.bps_group_rover_bps_floor'] = {state: 'Property'};
        c._baseImage = {}; c._setStatus = text => {c.status = text;};
        c._getIconImage = () => null; c._trackerIconUrl = () => 'beacon.svg';
        c._redraw = () => {cv.calls.length = 0; c._drawMarkers();};
        const beacon = {ent: 'beacon_a', floor: 'Property', cords: [100, 100],
            outdoor: {estimated_uncertainty_m: 3}};
        const group = {...beacon, ent: 'bps_group_rover', group: true, name: 'Rover',
            beacons_reporting: 1, total_beacons: 1, outdoor: {estimated_uncertainty_m: 7.4}};
        c._apiFetch = async () => ({ok: true, json: async () => [beacon, group]});
        await c._pollOnce();
        assert.equal(c._positions.has('bps_group_rover'), true);
        assert.match(c.status, /Rover: 1\/1/);
        const body = kind === 'partial' ? [beacon] : kind === 'empty' ? [] : {error: 'No data available'};
        c._apiFetch = async () => ({ok: kind !== 'no-data', status: kind === 'no-data' ? 404 : 200, json: async () => body});
        await c._pollOnce();
        assert.equal(c._positions.has('bps_group_rover'), false);
        assert.equal(c._positions.has('beacon_a'), true, 'legacy beacon caching remains unchanged');
        assert.ok(!cv.calls.some(v => Array.isArray(v) && v[2] === 148), 'removed group uncertainty is not drawn');
        assert.ok(!cv.calls.includes('Rover'), 'removed group marker is not drawn');
        assert.doesNotMatch(c.status, /Rover: 1\/1/);
    });
}

test('card retains group fixes through HTTP failures and malformed responses', async () => {
    const c = card(); c._outdoorSettings.enabled = true;
    c._config.entities = ['sensor.bps_group_rover_bps_zone'];
    c._hass.states['sensor.bps_group_rover_bps_floor'] = {state: 'Property'};
    c._redraw = () => {};
    const row = {ent: 'bps_group_rover', group: true, name: 'Rover', floor: 'Property', cords: [100, 100]};
    c._apiFetch = async () => ({ok: true, json: async () => [row]});
    await c._pollOnce();
    const cached = c._positions.get('bps_group_rover');
    for (const [status, body] of [[500, []], [401, []], [404, {error: 'Not found'}], [200, {}]]) {
        c._apiFetch = async () => ({ok: status === 200, status, json: async () => body});
        await c._pollOnce();
        assert.equal(c._positions.get('bps_group_rover'), cached);
    }
    c._apiFetch = async () => {throw new Error('connection lost');};
    await c._pollOnce();
    assert.equal(c._positions.get('bps_group_rover'), cached);
});

test('dedicated group position entity resolves floor and both small member markers', async () => {
    const c = card(), cv = canvas(); c._canvas = cv.value;
    const eid = 'sensor.bps_group_rover_bps_position';
    c._config.entities = [eid]; c._outdoorSettings.enabled = true;
    c._hass.states[eid] = {state: 'tracking', attributes: {group: true, tracker_key: 'bps_group_rover', floor: 'Property'}};
    const row = {ent: 'bps_group_rover', group: true, name: 'Rover', floor: 'Property', cords: [200, 100],
        zone: 'unknown', updated: Date.now() / 1000, beacons_reporting: 2, total_beacons: 2,
        outdoor: {estimated_uncertainty_m: 4, confidence: 'good'}, beacon_positions: [
            {ent: 'beacon_a', floor: 'Property', cords: [100, 100], estimated_uncertainty_m: 3},
            {ent: 'beacon_b', floor: 'Property', cords: [300, 100], estimated_uncertainty_m: 3}]};
    c._apiFetch = async () => ({ok: true, json: async () => [row]});
    c._baseImage = {}; c._redraw = () => {}; c._setStatus = () => {};
    await c._pollOnce();
    assert.equal(c._trackerKeyFromEntity(eid), 'bps_group_rover');
    assert.equal(c._floorStateForTracker('bps_group_rover'), 'Property');
    c._getIconImage = () => null; c._trackerIconUrl = () => 'beacon.svg';
    c._drawMarkers();
    assert.ok(cv.calls.some(v => Array.isArray(v) && v[0] === 100 && v[1] === 100));
    assert.ok(cv.calls.some(v => Array.isArray(v) && v[0] === 300 && v[1] === 100));
    // User renames survive through stable attributes.
    c._hass.states['sensor.my_dog'] = c._hass.states[eid];
    assert.equal(c._trackerKeyFromEntity('sensor.my_dog'), 'bps_group_rover');
});

test('receiver status remains unknown without distance contact, respects confirmed offline and resolves Bermuda friendly names after rename', () => {
    const c = card(); c._config.show_receivers = true; c._config.receiver_status = {}; c._config.receiver_timeout = 30;
    c._receivers = [{entity_id: 'old_slug', cords: {x: 10, y: 10}}];
    assert.equal(c._computeReceiverStatuses().get('old_slug'), null);
    c._config.receiver_status.old_slug = 'heuristic';
    assert.equal(c._computeReceiverStatuses().get('old_slug'), null, 'explicit distance heuristic cannot infer offline from absence');
    delete c._config.receiver_status.old_slug;
    c._hass.states['binary_sensor.old_slug_status'] = {state: 'off', attributes: {device_class: 'connectivity'}};
    assert.equal(c._computeReceiverStatuses().get('old_slug'), false);
    c._hass.states['binary_sensor.old_slug_status'].state = 'unknown';
    assert.equal(c._computeReceiverStatuses().get('old_slug'), null);
    c._receiverMatching = new Map([['old_slug', {name: 'Workshop', identity_ambiguous: false}]]);
    c._bermudaScanners = new Map([['workshop', 0]]); c._bermudaDumpAt = Date.now();
    assert.equal(c._computeReceiverStatuses().get('old_slug'), true);
    c._bermudaScanners.clear(); delete c._hass.states['binary_sensor.old_slug_status'];
    c._hass.states['sensor.other_distance_to_old_slug'] = {state: '4'};
    c._hass.entities = {'sensor.other_distance_to_old_slug': {platform: 'esphome'}};
    assert.equal(c._computeReceiverStatuses().get('old_slug'), null);
});

test('duplicate friendly names resolve liveness by MAC or remain unknown regardless of dump order', async () => {
    for (const hasAddress of [true, false]) for (const reverse of [true, false]) {
        const c = card(); c._config.show_receivers = true; c._config.receiver_status = {}; c._config.receiver_timeout = 30;
        c._receivers = [{entity_id: 'old_a'}, {entity_id: 'old_b'}];
        c._receiverMatching = new Map([['old_a', {name: 'Workshop', scanner_mac: 'aa:bb:cc:11:22:33'}],
            ['old_b', {name: 'Workshop', scanner_mac: 'aa:bb:cc:44:55:66'}]]);
        const rows = [['a', {_is_scanner: true, name: 'Workshop', last_seen: 100}],
            ['b', {_is_scanner: true, name: 'Workshop', last_seen: 1}]];
        if (hasAddress) {rows[0][1].address = 'aa:bb:cc:11:22:33'; rows[1][1].address = 'aa:bb:cc:44:55:66';}
        c._hass.callWS = async () => ({response: Object.fromEntries(reverse ? rows.reverse() : rows)});
        c._apiFetch = async () => ({ok: true, json: async () => ({matching: {
            options: [...c._receiverMatching].map(([entity_id, option]) => ({entity_id, ...option})), mac_owners: {}}})});
        await c._refreshBermudaScanners();
        assert.equal(c._bermudaScanners.has('workshop'), false);
        const statuses = c._computeReceiverStatuses();
        assert.equal(statuses.get('old_a'), hasAddress ? true : null);
        assert.equal(statuses.get('old_b'), hasAddress ? false : null);
    }
    const c = card(); c._config.show_receivers = true;
    c._hass.callWS = async () => ({response: {a: {_is_scanner: true, name: 'Workshop', last_seen: 100},
        b: {_is_scanner: true, name: 'Workshop', last_seen: null}}});
    await c._refreshBermudaScanners();
    assert.equal(c._bermudaScanners.has('workshop'), false, 'missing clock must not borrow a same-name scanner age');
});

test('a scanner absent from the dump cannot inherit a shared friendly name age', async () => {
    const c = card(); c._config.show_receivers = true; c._config.receiver_status = {}; c._config.receiver_timeout = 30;
    c._receivers = [{entity_id: 'old_a'}, {entity_id: 'old_b'}];
    c._receiverMatching = new Map([['old_a', {name: 'Workshop', scanner_mac: 'aa:bb:cc:11:22:33', liveness_name_unique: false}],
        ['old_b', {name: 'Workshop', scanner_mac: 'aa:bb:cc:44:55:66', liveness_name_unique: false}]]);
    c._hass.callWS = async () => ({response: {'aa:bb:cc:44:55:66': {_is_scanner: true, name: 'Workshop', last_seen: 1},
        tag: {last_seen: 100}}});
    c._apiFetch = async () => ({ok: true, json: async () => ({matching: {
        options: [...c._receiverMatching].map(([entity_id, option]) => ({entity_id, ...option})), mac_owners: {}}})});
    await c._refreshBermudaScanners();
    assert.equal(c._computeReceiverStatuses().get('old_a'), null);
    assert.equal(c._computeReceiverStatuses().get('old_b'), false);
    c._hass.states['binary_sensor.old_a_status'] = {state: 'on', attributes: {device_class: 'connectivity'}};
    assert.equal(c._computeReceiverStatuses().get('old_a'), true);
});

test('ownerless Bermuda ages cannot mark separate owners of the same MAC online or offline', () => {
    for (const age of [0, 90]) {
        const c = card(); c._config.show_receivers = true; c._config.receiver_status = {}; c._config.receiver_timeout = 30;
        c._receivers = [{entity_id: 'station_a'}, {entity_id: 'station_b'}];
        const mac = 'aa:bb:cc:11:22:33';
        c._receiverMatching = new Map([
            ['station_a', {name: 'Station A', scanner_mac: mac, config_entry_ids: ['owner-a']}],
            ['station_b', {name: 'Station B', scanner_mac: mac, config_entry_ids: ['owner-b']}],
        ]);
        c._bermudaScanners = new Map([['station_a', age], ['station_b', age]]);
        c._bermudaScannerMacAges = new Map([[mac, age]]); c._bermudaDumpAt = Date.now();
        assert.equal(c._computeReceiverStatuses().get('station_a'), null);
        assert.equal(c._computeReceiverStatuses().get('station_b'), null);
        c._hass.states['binary_sensor.station_a_status'] = {state: 'off', attributes: {device_class: 'connectivity'}};
        assert.equal(c._computeReceiverStatuses().get('station_a'), false, 'independent connectivity remains authoritative');
    }
});

test('multiple aliases belonging to one Bermuda owner still resolve liveness', () => {
    const c = card(); c._config.show_receivers = true; c._config.receiver_status = {}; c._config.receiver_timeout = 30;
    c._receivers = [{entity_id: 'old_station'}, {entity_id: 'current_station'}];
    const mac = 'aa:bb:cc:11:22:33';
    c._receiverMatching = new Map(['old_station', 'current_station'].map(id => [id,
        {name: 'Current Station', scanner_mac: mac, config_entry_ids: ['owner-a']} ]));
    c._bermudaScanners = new Map(); c._bermudaScannerMacAges = new Map([[mac, 0]]); c._bermudaDumpAt = Date.now();
    assert.equal(c._computeReceiverStatuses().get('old_station'), true);
    assert.equal(c._computeReceiverStatuses().get('current_station'), true);
});

test('saved identity conflicts cannot borrow automatic liveness from a reused scanner name', () => {
    for (const conflict of ['mac', 'owner']) for (const age of [0, 90]) for (const override of [undefined, false, 'heuristic']) {
        const c = card(); c._config.show_receivers = true; c._config.receiver_status = {}; c._config.receiver_timeout = 30;
        if (override !== undefined) c._config.receiver_status.station = override;
        const mac = 'aa:bb:cc:11:22:33';
        const rec = {entity_id: 'station', scanner_mac: mac, scanner_config_entry_id: 'owner-a'};
        if (conflict === 'mac') rec.scanner_mac = 'dd:ee:ff:11:22:33';
        else rec.scanner_config_entry_id = 'owner-b';
        c._receivers = [rec];
        c._receiverMatching = new Map([['station', {name: 'Station', scanner_mac: mac, config_entry_ids: ['owner-a']}]]);
        c._bermudaScanners = new Map([['station', age]]); c._bermudaScannerMacAges = new Map([[mac, age]]); c._bermudaDumpAt = Date.now();
        c._hass.states['binary_sensor.station_status'] = {state: 'on', attributes: {device_class: 'connectivity'}};
        c._hass.states['sensor.tag_distance_to_station'] = {state: '3'};
        assert.equal(c._computeReceiverStatuses().get('station'), null);
        c._hass.states['binary_sensor.explicit_status'] = {state: 'on'};
        c._config.receiver_status.station = 'binary_sensor.explicit_status';
        assert.equal(c._computeReceiverStatuses().get('station'), true, 'explicit user-selected status remains authoritative');
        delete c._config.receiver_status.station;
        rec.scanner_mac = mac; rec.scanner_config_entry_id = 'owner-a';
        assert.equal(c._computeReceiverStatuses().get('station'), age === 0);
    }
});

test('card loads all runtime owner evidence and excludes omitted scanners from name liveness', async () => {
    for (const sharedMac of [false, true]) for (const age of [0, 90]) {
        const c = card(); c._config.show_receivers = true; c._config.receiver_status = {}; c._config.receiver_timeout = 30;
        const mac = 'aa:bb:cc:11:22:33';
        const otherMac = sharedMac ? mac : 'dd:ee:ff:44:55:66';
        const matching = {options: [{entity_id: 'old_station', name: 'Workshop', scanner_mac: mac,
            config_entry_ids: ['owner-a'], liveness_name_unique: false}],
            mac_owners: {[mac]: sharedMac ? ['owner-a', 'owner-b'] : ['owner-a']}};
        c._apiFetch = async () => ({ok: true, json: async () => ({receiver_matching: matching, coordinates: JSON.stringify({
            floor: [{name: 'Property', scale: 20, receivers: [{entity_id: 'old_station', cords: {x: 1, y: 2}}], zones: []}]})})});
        c._refreshBermudaScanners = async () => {
            c._bermudaScanners = new Map([['workshop', age]]);
            c._bermudaScannerMacAges = new Map([[otherMac, age]]); c._bermudaDumpAt = Date.now();
        };
        c._resolveMapUrl = async () => 'synthetic.png'; c._loadFloorImage = async () => {c._baseImage = {};};
        await c._loadFloorResources(1);
        assert.equal(c._receiverStatuses.get('old_station'), null);
    }
});

function fallbackCard(saved = 'old_station') {
    const c = card(); c._config.show_receivers = true; c._config.receiver_status = {}; c._config.receiver_timeout = 30;
    const mac = 'aa:bb:cc:11:22:33';
    c._receivers = [{entity_id: saved, scanner_mac: mac, scanner_config_entry_id: 'owner-a'}];
    c._receiverMatching = new Map(['old_station', 'current_station'].map(id => [id,
        {entity_id: id, name: 'Current station', scanner_mac: mac, config_entry_ids: ['owner-a'],
            liveness_name_unique: true, distance_sensors: [`sensor.tag_distance_to_${id}`], disabled_sensors: []}]));
    c._hass.entities = {};
    for (const id of ['old_station', 'current_station']) {
        const eid = `sensor.tag_distance_to_${id}`;
        c._hass.entities[eid] = {platform: 'bermuda', config_entry_id: 'owner-a'};
        c._hass.states[eid] = {state: 'unavailable', last_updated: new Date(Date.now() - 1000).toISOString()};
    }
    return c;
}

test('automatic unknown connectivity falls through while explicit status remains authoritative', () => {
    for (const unknown of ['unknown', 'unavailable']) for (const evidence of ['distance', 'device']) {
        const c = fallbackCard();
        c._hass.states['binary_sensor.old_station_status'] = {state: unknown, attributes: {device_class: 'connectivity'}};
        if (evidence === 'distance') c._hass.states['sensor.tag_distance_to_old_station'].state = '4';
        else {
            c._hass.devices = {station: {id: 'station', name: 'Old station'}};
            c._hass.entities['sensor.device_temperature'] = {device_id: 'station'};
            c._hass.states['sensor.device_temperature'] = {state: '20'};
        }
        assert.equal(c._computeReceiverStatuses().get('old_station'), true, `${unknown} uses ${evidence}`);
        c._hass.states['binary_sensor.old_station_status'].state = 'off';
        assert.equal(c._computeReceiverStatuses().get('old_station'), false, 'automatic off wins over distance');
        c._hass.states['binary_sensor.old_station_status'].state = unknown;
        c._config.receiver_status.old_station = 'binary_sensor.old_station_status';
        assert.equal(c._computeReceiverStatuses().get('old_station'), null, 'explicit unknown is authoritative');
    }
});

test('both proven receiver aliases use connectivity, device and filtered distance fallback without dump ages', () => {
    for (const saved of ['old_station', 'current_station']) for (const evidence of ['connectivity_on', 'connectivity_off', 'device', 'distance']) {
        const c = fallbackCard(saved), other = saved === 'old_station' ? 'current_station' : 'old_station';
        if (evidence.startsWith('connectivity')) {
            c._hass.states[`binary_sensor.${other}_status`] = {state: evidence.endsWith('off') ? 'off' : 'on', attributes: {device_class: 'connectivity'}};
            c._hass.states[`sensor.tag_distance_to_${other}`].state = '4';
        } else if (evidence === 'device') {
            c._hass.devices = {station: {id: 'station', name: other.replace('_', ' ')}};
            c._hass.entities['sensor.device_temperature'] = {device_id: 'station'};
            c._hass.states['sensor.device_temperature'] = {state: '20'};
        } else c._hass.states[`sensor.tag_distance_to_${other}`].state = '4';
        assert.equal(c._computeReceiverStatuses().get(saved), evidence !== 'connectivity_off', `${saved} uses ${evidence}`);
    }
});

test('alias distance proof rejects raw disabled foreign wrong-owner and invalid or stale clocks', () => {
    for (const invalid of ['raw', 'disabled', 'foreign', 'owner', 'stale', 'future', 'missing_clock', 'nan', 'negative']) {
        const c = fallbackCard(), eid = 'sensor.tag_distance_to_current_station';
        const source = c._hass.states[eid]; source.state = '4';
        if (invalid === 'raw') {c._receiverMatching.get('current_station').distance_sensors = []; c._hass.entities[eid].unique_id = 'synthetic_range_raw';}
        else if (invalid === 'disabled') c._receiverMatching.get('current_station').disabled_sensors = [eid];
        else if (invalid === 'foreign') c._hass.entities[eid].platform = 'esphome';
        else if (invalid === 'owner') c._hass.entities[eid].config_entry_id = 'owner-b';
        else if (invalid === 'stale') source.last_updated = new Date(Date.now() - 90000).toISOString();
        else if (invalid === 'future') source.last_updated = new Date(Date.now() + 90000).toISOString();
        else if (invalid === 'missing_clock') delete source.last_updated;
        else if (invalid === 'nan') source.state = 'NaN';
        else if (invalid === 'negative') source.state = '-1';
        assert.equal(c._computeReceiverStatuses().get('old_station'), null, invalid);
    }
});

test('shared runtime names and all-owner evidence block alias connectivity and device borrowing', () => {
    for (const shared of ['name', 'mac_owner']) {
        const c = fallbackCard();
        if (shared === 'name') for (const option of c._receiverMatching.values()) option.liveness_name_unique = false;
        else c._receiverMacOwners = new Map([['aa:bb:cc:11:22:33', ['owner-a', 'owner-b']]]);
        c._hass.states['binary_sensor.current_station_status'] = {state: 'on', attributes: {device_class: 'connectivity'}};
        c._hass.devices = {station: {id: 'station', name: 'Current station'}};
        c._hass.entities['sensor.device_temperature'] = {device_id: 'station'};
        c._hass.states['sensor.device_temperature'] = {state: '20'};
        assert.equal(c._computeReceiverStatuses().get('old_station'), null, shared);
    }
});

test('saved alias omitted from registry resolves current sources after read_text inventory load', async () => {
    const c = fallbackCard(), current = c._receiverMatching.get('current_station');
    const matching = {options: [current], mac_owners: {'aa:bb:cc:11:22:33': ['owner-a']}};
    c._apiFetch = async () => ({ok: true, json: async () => ({receiver_matching: matching,
        scanner_diagnostics: {alias_receivers: [{entity_id: 'old_station', scanner_mac: current.scanner_mac, config_entry_id: 'owner-a'}]},
        coordinates: JSON.stringify({floor: [{name: 'Property', scale: 20, receivers: [{...c._receivers[0], cords: {x: 1, y: 2}}], zones: []}]})})});
    c._hass.states['sensor.tag_distance_to_current_station'].state = '4';
    delete c._hass.states['sensor.tag_distance_to_old_station']; delete c._hass.entities['sensor.tag_distance_to_old_station'];
    c._refreshBermudaScanners = async () => {};
    c._resolveMapUrl = async () => 'synthetic.png'; c._loadFloorImage = async () => {c._baseImage = {};};
    await c._loadFloorResources(1);
    assert.equal(c._receiverStatuses.get('old_station'), true);
    c._receivers[0].scanner_config_entry_id = 'missing-owner';
    assert.equal(c._computeReceiverStatuses().get('old_station'), null, 'unavailable saved identity cannot borrow fallback');
});


test('inventory-proven filtered distance survives partial or absent frontend registry metadata', () => {
    for (const metadata of ['partial', 'absent']) {
        const c = fallbackCard(), eid = 'sensor.tag_distance_to_current_station';
        c._hass.states[eid].state = '4';
        if (metadata === 'partial') c._hass.entities[eid] = {device_id: 'synthetic-station'};
        else delete c._hass.entities[eid];
        assert.equal(c._computeReceiverStatuses().get('old_station'), true, metadata);
    }
});

async function loadedIdentityCard({legacy = false} = {}) {
    const c = fallbackCard('old_station');
    const rec = legacy ? {entity_id: 'old_station'} : {...c._receivers[0]};
    const matching = {options: [...c._receiverMatching.values()], mac_owners: {'aa:bb:cc:11:22:33': ['owner-a']}};
    c._apiFetch = async path => {
        if (path === '/api/bps/read_text') return {ok: true, json: async () => ({receiver_matching: matching,
            coordinates: JSON.stringify({floor: [{name: 'Property', scale: 20, receivers: [{...rec, cords: {x: 1, y: 2}}], zones: []}]})})};
        assert.equal(path, '/api/bps/scanner_linking');
        return {ok: true, json: async () => ({matching})};
    };
    c._hass.callWS = async () => ({response: {'aa:bb:cc:11:22:33': {
        _is_scanner: true, address: 'aa:bb:cc:11:22:33', name: 'Current station', last_seen: 100}}});
    c._resolveMapUrl = async () => 'synthetic.png'; c._loadFloorImage = async () => {c._baseImage = {};};
    await c._loadFloorResources(1);
    assert.equal(c._receiverStatuses.get('old_station'), true);
    return c;
}

function nextInventory(c, {mac = 'aa:bb:cc:11:22:33', owner = 'owner-a', slug = 'old_station', name = 'Current station'} = {}) {
    const source = `sensor.tag_distance_to_${slug}`;
    return {options: [{entity_id: slug, name, scanner_mac: mac, config_entry_ids: [owner],
        liveness_name_unique: true, distance_sensors: [source], disabled_sensors: []}], mac_owners: {[mac]: [owner]}};
}

test('real floor loading keeps malformed saved identity fields unknown instead of inferring current hardware', async () => {
    const cases = [['entity_id', ['old_station']], ['entity_id', {bad: true}],
        ['scanner_mac', ['aa:bb:cc:11:22:33']], ['scanner_mac', {bad: true}],
        ['scanner_config_entry_id', ['owner-a']], ['scanner_config_entry_id', {bad: true}],
        ['scanner_config_entry_id', 1]];
    for (const [field, value] of cases) {
        const c = fallbackCard('old_station'), rec = {...c._receivers[0], [field]: value, cords: {x: 1, y: 2}};
        const matching = {options: [...c._receiverMatching.values()], mac_owners: {'aa:bb:cc:11:22:33': ['owner-a']}};
        c._apiFetch = async path => ({ok: true, json: async () => path === '/api/bps/read_text'
            ? {receiver_matching: matching, coordinates: JSON.stringify({floor: [{name: 'Property', scale: 20, receivers: [rec], zones: []}]})}
            : {matching}});
        c._hass.callWS = async () => ({response: {'aa:bb:cc:11:22:33': {
            _is_scanner: true, address: 'aa:bb:cc:11:22:33', name: 'Current station', last_seen: 100}}});
        c._resolveMapUrl = async () => 'synthetic.png'; c._loadFloorImage = async () => {c._baseImage = {};};
        await c._loadFloorResources(1);
        const id = c._receivers[0].entity_id;
        assert.equal(c._currentReceiverOption(id), null, field);
        assert.equal(c._receiverStatuses.get(id), null, `${field}: fresh dump`);
        for (const age of [0, 100]) for (const override of [undefined, false, 'heuristic']) {
            c._bermudaScannerMacAges.set('aa:bb:cc:11:22:33', age);
            c._config.receiver_status[id] = override;
            assert.equal(c._computeReceiverStatuses().get(id), null, `${field}/${age}/${override}`);
        }
        if (typeof id === 'string') {
            c._config.receiver_status[id] = 'binary_sensor.explicit_status';
            c._hass.states['binary_sensor.explicit_status'] = {state: 'on'};
            assert.equal(c._computeReceiverStatuses().get(id), true, 'explicit user status stays authoritative');
        }
    }
});

test('malformed field edits cannot use a previously captured valid automatic binding', async () => {
    for (const field of ['scanner_mac', 'scanner_config_entry_id']) {
        const c = await loadedIdentityCard();
        c._receivers[0][field] = {bad: true};
        assert.equal(c._computeReceiverStatuses().get('old_station'), null, field);
    }
});

test('real card refresh keeps saved and inferred binding identity through same-name MAC or owner replacement', async () => {
    for (const legacy of [false, true]) for (const replacement of ['mac', 'owner']) {
        const c = await loadedIdentityCard({legacy});
        const before = JSON.stringify(c._receivers);
        const mac = replacement === 'mac' ? 'dd:ee:ff:44:55:66' : 'aa:bb:cc:11:22:33';
        const owner = replacement === 'owner' ? 'owner-b' : 'owner-a';
        const matching = nextInventory(c, {mac, owner});
        c._apiFetch = async path => {assert.equal(path, '/api/bps/scanner_linking'); return {ok: true, json: async () => ({matching})};};
        c._hass.callWS = async () => ({response: {[mac]: {_is_scanner: true, address: mac, name: 'Current station', last_seen: 101}}});
        c._hass.states['binary_sensor.old_station_status'] = {state: 'on', attributes: {device_class: 'connectivity'}};
        c._hass.states['sensor.tag_distance_to_old_station'].state = '4';
        c._hass.entities['sensor.tag_distance_to_old_station'].config_entry_id = owner;
        c._nextBermudaDumpAt = 0; c._nextReceiverInventoryAt = 0;
        await c._refreshBermudaScanners();
        for (const automatic of [undefined, false, 'heuristic']) {
            c._config.receiver_status.old_station = automatic;
            assert.equal(c._computeReceiverStatuses().get('old_station'), null, `${legacy}/${replacement}/${automatic}`);
        }
        c._config.receiver_status.old_station = 'binary_sensor.old_station_status';
        assert.equal(c._computeReceiverStatuses().get('old_station'), true, 'manual selection remains authoritative');
        c._hass.states['binary_sensor.old_station_status'].state = 'unavailable';
        assert.equal(c._computeReceiverStatuses().get('old_station'), null);
        assert.equal(JSON.stringify(c._receivers), before, 'refresh cannot rewrite placement');
    }
});

test('real card refresh resolves removed old slug through unchanged proven MAC and owner', async () => {
    for (const legacy of [false, true]) for (const proof of ['dump', 'connectivity', 'distance']) {
        const c = await loadedIdentityCard({legacy}), matching = nextInventory(c, {slug: 'renamed_station', name: 'Renamed station'});
        const before = JSON.stringify(c._receivers);
        c._apiFetch = async () => ({ok: true, json: async () => ({matching})});
        if (proof === 'dump') c._hass.callWS = async () => ({response: {'aa:bb:cc:11:22:33': {
            _is_scanner: true, address: 'aa:bb:cc:11:22:33', name: 'Renamed station', last_seen: 101}}});
        else {
            c._bermudaScanners = null; c._bermudaScannerMacAges = null;
            c._hass.callWS = async () => {throw new Error('dump unavailable');};
            if (proof === 'connectivity') c._hass.states['binary_sensor.renamed_station_status'] = {state: 'on', attributes: {device_class: 'connectivity'}};
            else {
                c._hass.states['sensor.tag_distance_to_renamed_station'] = {state: '4', last_updated: new Date(Date.now() - 1000).toISOString()};
                c._hass.entities['sensor.tag_distance_to_renamed_station'] = {platform: 'bermuda', config_entry_id: 'owner-a'};
            }
        }
        c._nextBermudaDumpAt = 0; c._nextReceiverInventoryAt = 0; await c._refreshBermudaScanners();
        assert.equal(c._computeReceiverStatuses().get('old_station'), true, `${legacy}/${proof}`);
        const cv = canvas(); c._canvas = cv.value;
        c._config.show_receiver_labels = true; c._config.scale_receiver_labels = 100;
        c._config.scale_receiver_icon = 100; c._getIconImage = () => null;
        c._receiverStatuses = c._computeReceiverStatuses(); c._drawReceivers();
        assert.ok(cv.calls.includes('old_station'), 'proven alias renders the normal receiver label');
        assert.ok(!cv.calls.includes('(Unmatched) old_station'));
        assert.equal(JSON.stringify(c._receivers), before);
    }
});

test('inventory failure suspends automatic evidence and preserves last valid all-owner metadata', async () => {
    for (const failure of ['null', 'http', 'throw', 'schema']) {
        const c = await loadedIdentityCard({legacy: true});
        const originalMatching = c._receiverMatching, originalOwners = c._receiverMacOwners;
        c._apiFetch = async () => {
            if (failure === 'throw') throw new Error('network unavailable');
            if (failure === 'null') return null;
            if (failure === 'http') return {ok: false, status: 503};
            return {ok: true, json: async () => ({matching: {options: []}})};
        };
        c._hass.callWS = async () => ({response: {'dd:ee:ff:44:55:66': {
            _is_scanner: true, address: 'dd:ee:ff:44:55:66', name: 'Current station', last_seen: 101}}});
        c._hass.states['binary_sensor.old_station_status'] = {state: 'on', attributes: {device_class: 'connectivity'}};
        c._hass.states['sensor.tag_distance_to_old_station'].state = '4';
        c._nextBermudaDumpAt = 0; c._nextReceiverInventoryAt = 0; await c._refreshBermudaScanners();
        assert.equal(c._computeReceiverStatuses().get('old_station'), null, failure);
        assert.equal(c._receiverMatching, originalMatching); assert.equal(c._receiverMacOwners, originalOwners);
        c._config.receiver_status.old_station = 'binary_sensor.old_station_status';
        assert.equal(c._computeReceiverStatuses().get('old_station'), true);
        delete c._config.receiver_status.old_station;
        c._apiFetch = async () => ({ok: true, json: async () => ({matching: nextInventory(c, {slug: 'current_station'})})});
        c._nextBermudaDumpAt = 0; c._nextReceiverInventoryAt = 0; await c._refreshBermudaScanners();
        assert.equal(c._computeReceiverStatuses().get('old_station'), true, 'successful current proof restores fallback');
    }
});

test('inventory refresh still detects replacement when dump stamps do not advance or dump fails', async () => {
    for (const dump of ['unchanged', 'failed']) {
        const c = await loadedIdentityCard({legacy: true});
        const matching = nextInventory(c, {owner: 'owner-b'});
        c._apiFetch = async () => ({ok: true, json: async () => ({matching})});
        if (dump === 'failed') c._hass.callWS = async () => {throw new Error('dump unavailable');};
        c._nextBermudaDumpAt = 0; c._nextReceiverInventoryAt = 0; await c._refreshBermudaScanners();
        assert.equal(c._computeReceiverStatuses().get('old_station'), null, dump);
    }
});

test('canceled scanner inventory and dump requests cannot overwrite the next card generation', async () => {
    for (const stage of ['inventory', 'json', 'dump']) {
        const c = await loadedIdentityCard({legacy: true});
        let resolve;
        if (stage === 'inventory') c._apiFetch = async () => new Promise(done => {resolve = done;});
        else if (stage === 'json') c._apiFetch = async () => ({ok: true, json: async () => new Promise(done => {resolve = done;})});
        else {
            c._apiFetch = async () => ({ok: true, json: async () => ({matching: nextInventory(c)})});
            c._hass.callWS = async () => new Promise(done => {resolve = done;});
        }
        c._nextBermudaDumpAt = 0; c._nextReceiverInventoryAt = 0;
        const pending = c._refreshBermudaScanners();
        while (!resolve) await Promise.resolve();
        c._runGeneration += 1;
        const sentinelMatching = new Map([['next_floor', {entity_id: 'next_floor'}]]), sentinelScanners = new Map([['next_floor', 7]]);
        c._receiverMatching = sentinelMatching; c._bermudaScanners = sentinelScanners; c._receiverInventoryCurrent = true;
        c._nextBermudaDumpAt = 123;
        resolve(stage === 'inventory' ? {ok: true, json: async () => ({matching: nextInventory(c, {owner: 'owner-b'})})}
            : stage === 'json' ? {matching: nextInventory(c, {owner: 'owner-b'})} : {response: {'dd:ee:ff:44:55:66': {_is_scanner: true, name: 'Replacement', last_seen: 200}}});
        await pending;
        assert.equal(c._receiverMatching, sentinelMatching); assert.equal(c._bermudaScanners, sentinelScanners);
        assert.equal(c._receiverInventoryCurrent, true); assert.equal(c._nextBermudaDumpAt, 123);
    }
});


test('canceled floor load cannot commit statuses or request its old map after scanner refresh', async () => {
    const c = await loadedIdentityCard();
    const fetchInitial = c._apiFetch;
    let resolve;
    c._apiFetch = async path => path === '/api/bps/scanner_linking'
        ? new Promise(done => {resolve = done;}) : fetchInitial(path);
    c._nextBermudaDumpAt = 0; c._nextReceiverInventoryAt = 0;
    let maps = 0; c._resolveMapUrl = async () => {maps += 1; return 'old-floor.png';};
    const pending = c._loadFloorResources(1);
    while (!resolve) await Promise.resolve();
    c._runGeneration += 1;
    const receivers = [{entity_id: 'next_floor'}], statuses = new Map([['next_floor', false]]);
    c._receivers = receivers; c._receiverStatuses = statuses;
    resolve({ok: true, json: async () => ({matching: nextInventory(c, {owner: 'owner-b'})})});
    await pending;
    assert.equal(c._receivers, receivers); assert.equal(c._receiverStatuses, statuses);
    assert.equal(maps, 0);
});

test('real legacy binding capture retains fresh unproven exact-name connectivity and distance fallback', async () => {
    const c = fallbackCard();
    c._receivers = [{entity_id: 'old_station'}];
    const option = {entity_id: 'old_station', name: 'Old station', scanner_mac: null, config_entry_ids: [],
        identity_ambiguous: false, distance_sensors: ['sensor.tag_distance_to_old_station'], disabled_sensors: []};
    const matching = {options: [option, {...option, entity_id: 'current_station', name: 'Current station',
        distance_sensors: ['sensor.tag_distance_to_current_station']}], mac_owners: {}};
    c._apiFetch = async path => ({ok: true, json: async () => path === '/api/bps/read_text'
        ? {receiver_matching: matching, coordinates: JSON.stringify({floor: [{name: 'Property', scale: 20,
            receivers: [{entity_id: 'old_station', cords: {x: 1, y: 2}}], zones: []}]})} : {matching}});
    c._hass.callWS = async () => ({response: {}});
    c._resolveMapUrl = async () => 'synthetic.png'; c._loadFloorImage = async () => {c._baseImage = {};};
    c._hass.states['binary_sensor.old_station_status'] = {state: 'on', attributes: {device_class: 'connectivity'}};
    await c._loadFloorResources(1);
    assert.equal(c._receiverBindings.get('old_station'), null, 'legacy entity has no inferred MAC/owner proof');
    for (const status of ['on', 'off', 'unknown', 'unavailable']) {
        c._hass.states['binary_sensor.old_station_status'].state = status;
        c._nextBermudaDumpAt = 0; c._nextReceiverInventoryAt = 0; await c._refreshBermudaScanners();
        assert.equal(c._computeReceiverStatuses().get('old_station'), status === 'on' ? true : status === 'off' ? false : null, status);
        if (status === 'unknown' || status === 'unavailable') {
            c._hass.states['sensor.tag_distance_to_current_station'].state = '4';
            assert.equal(c._computeReceiverStatuses().get('old_station'), null, 'unproven alias cannot lend a reading');
            c._hass.states['sensor.tag_distance_to_old_station'].state = '4';
            assert.equal(c._computeReceiverStatuses().get('old_station'), true, 'fresh exact reading still proves online');
            c._hass.states['sensor.tag_distance_to_old_station'].state = 'unavailable';
        }
    }
});

test('older same-generation scanner refresh cannot overwrite a newer inventory or dump', async () => {
    for (const stage of ['inventory', 'dump']) {
        const c = await loadedIdentityCard({legacy: true});
        let resolve;
        const oldMatching = nextInventory(c);
        if (stage === 'inventory') c._apiFetch = async () => new Promise(done => {resolve = done;});
        else {
            c._apiFetch = async () => ({ok: true, json: async () => ({matching: oldMatching})});
            c._hass.callWS = async () => new Promise(done => {resolve = done;});
        }
        c._nextBermudaDumpAt = 0; c._nextReceiverInventoryAt = 0;
        const oldPending = c._refreshBermudaScanners();
        while (!resolve) await Promise.resolve();
        const replacement = nextInventory(c, {mac: 'dd:ee:ff:44:55:66'});
        c._apiFetch = async () => ({ok: true, json: async () => ({matching: replacement})});
        c._hass.callWS = async () => ({response: {'dd:ee:ff:44:55:66': {_is_scanner: true,
            address: 'dd:ee:ff:44:55:66', name: 'Current station', last_seen: 200}}});
        c._nextBermudaDumpAt = 0; c._nextReceiverInventoryAt = 0;
        await c._refreshBermudaScanners();
        const currentMatching = c._receiverMatching, currentScanners = c._bermudaScannerMacAges, nextAt = c._nextBermudaDumpAt;
        resolve(stage === 'inventory' ? {ok: true, json: async () => ({matching: oldMatching})}
            : {response: {'aa:bb:cc:11:22:33': {_is_scanner: true, address: 'aa:bb:cc:11:22:33', name: 'Current station', last_seen: 201}}});
        await oldPending;
        assert.equal(c._receiverMatching, currentMatching); assert.equal(c._bermudaScannerMacAges, currentScanners);
        assert.equal(c._nextBermudaDumpAt, nextAt); assert.equal(c._receiverInventoryCurrent, true);
        assert.equal(c._computeReceiverStatuses().get('old_station'), null);
    }
});


test('unmatched legacy placement stays unknown after binding capture and authenticated refresh', async () => {
    const c = fallbackCard();
    const matching = {options: [], mac_owners: {}};
    c._apiFetch = async path => ({ok: true, json: async () => path === '/api/bps/read_text'
        ? {receiver_matching: matching, coordinates: JSON.stringify({floor: [{name: 'Property', scale: 20,
            receivers: [{entity_id: 'missing_station', cords: {x: 1, y: 2}}], zones: []}]})} : {matching}});
    c._hass.callWS = async () => ({response: {'dd:ee:ff:44:55:66': {_is_scanner: true, name: 'Missing station', last_seen: 100}}});
    c._resolveMapUrl = async () => 'synthetic.png'; c._loadFloorImage = async () => {c._baseImage = {};};
    await c._loadFloorResources(1);
    assert.equal(c._receiverBindings.get('missing_station'), null);
    assert.equal(c._receiverStatuses.get('missing_station'), null, 'unproven name age cannot lend liveness');
});


test('authenticated inventory refresh stays current during Bermuda WS backoff and is independently throttled', async () => {
    const c = await loadedIdentityCard({legacy: true});
    let wsCalls = 0, inventoryCalls = 0;
    c._hass.callWS = async () => {wsCalls += 1; throw new Error('dump unavailable');};
    let matching = nextInventory(c);
    c._apiFetch = async path => {inventoryCalls += 1; assert.equal(path, '/api/bps/scanner_linking');
        return {ok: true, json: async () => ({matching})};};
    c._nextBermudaDumpAt = 0; c._nextReceiverInventoryAt = 0;
    await c._refreshBermudaScanners();
    const dumpNextAt = c._nextBermudaDumpAt;
    assert.equal(wsCalls, 1); assert.equal(inventoryCalls, 1);
    assert.ok(dumpNextAt > c._nextReceiverInventoryAt, 'WS backoff does not extend identity refresh cadence');
    await c._refreshBermudaScanners();
    assert.equal(wsCalls, 1); assert.equal(inventoryCalls, 1, 'neither timer due creates no request');
    matching = nextInventory(c, {mac: 'dd:ee:ff:44:55:66'});
    c._nextReceiverInventoryAt = 0;
    await c._refreshBermudaScanners();
    assert.equal(inventoryCalls, 2); assert.equal(wsCalls, 1, 'only inventory refresh runs during WS backoff');
    assert.equal(c._nextBermudaDumpAt, dumpNextAt);
    assert.equal(c._computeReceiverStatuses().get('old_station'), null, 'replacement proof supersedes cached dump and automatic fallback');
});
