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
    const calls = [];
    const ctx = {save() {}, restore() {}, beginPath() {}, fill() {}, stroke() {}, setLineDash() {}, moveTo() {}, lineTo() {},
        arc(...args) {calls.push(args);}, fillText(text) {calls.push(text);}, measureText() {return {width: 50};}};
    return {calls, value: {width: 2000, height: 1000, getContext: () => ctx}};
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
