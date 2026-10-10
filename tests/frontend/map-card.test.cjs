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
    assert.ok(cv.calls.some(v => Array.isArray(v) && v[2] === 60)); // beacon radius
    assert.ok(cv.calls.includes('Rover'));
    row.beacon_positions.push({ent: 'beacon_b', floor: 'Barn', cords: [400, 300], estimated_uncertainty_m: 9});
    cv.calls.length = 0; c._drawMarkers();
    assert.ok(!cv.calls.some(v => Array.isArray(v) && v[0] === 400 && v[1] === 300), 'foreign floor pixels are never rendered here');
    row.updated = 1; row.outdoor.observed = .5;
    await c._pollOnce();
    assert.equal(c._positions.get('bps_group_rover').receivedAt, 500, 'repeated poll preserves source observation time');
});

test('card constituent circles inherit group freshness limits and age cached source times', () => {
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
    assert.ok(cv.calls.some(v => Array.isArray(v) && v[2] === 60));
    assert.ok(!cv.dashes.some(dash => dash[0] === 3 && dash[1] === 6), '45-second beacon remains fresh with a 90-second cutoff');
    // An old cached diagnostic age cannot keep a source circle fresh forever.
    beacon.updated = now / 1000 - 91;
    cv.dashes.length = 0; c._drawMarkers();
    assert.ok(cv.dashes.some(dash => dash[0] === 3 && dash[1] === 6));
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
