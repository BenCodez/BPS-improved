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
