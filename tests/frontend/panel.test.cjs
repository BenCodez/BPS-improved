const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const directory = join(__dirname, '../../custom_components/bps/frontend');
const plain = x => JSON.parse(JSON.stringify(x));

async function panel(layout, entities = ['beacon_a', 'beacon_b']) {
    const ids = new Map(), listeners = new Map(), requests = [], intervals = [], canvasCalls = [];
    const network = {cords: [], cordsStatus: 200, failCords: false};
    let clock = Date.now();
    class ClockDate extends Date {static now() {return clock;}}
    const context = new Proxy({}, {get: (obj, key) => key === 'setLineDash' ? dash => canvasCalls.push(['dash', ...dash])
        : key === 'arc' ? (...args) => canvasCalls.push(['arc', ...args])
        : key === 'measureText' ? () => ({width: 30}) : key === 'createLinearGradient'
        ? () => ({addColorStop() {}}) : obj[key] || (() => {}), set: (obj, key, value) => {obj[key] = value; return true;}});
    function element(tag = 'div') {
        const events = new Map();
        const el = {tagName: tag.toUpperCase(), value: '', checked: false, style: {}, dataset: {}, children: [], width: 2000, height: 1000,
            offsetWidth: 100, offsetHeight: 30, classList: {add() {}, remove() {}, toggle() {}, contains: () => false},
            get options() {return this.children;}, get selectedOptions() {return this.children.filter(e => e.selected);},
            appendChild(child) {child.parentElement = this; this.children.push(child); if (child.id) ids.set(child.id, child); return child;},
            append(...children) {children.forEach(c => this.appendChild(c));},
            replaceChildren(...children) {this.children = []; this.append(...children);},
            remove() {if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(c => c !== this);},
            setAttribute(key, value) {if (key.startsWith('data-')) this.dataset[key.slice(5)] = value; else this[key] = value;},
            getAttribute(key) {return key.startsWith('data-') ? this.dataset[key.slice(5)] : this[key];},
            addEventListener(event, cb) {events.set(event, [...events.get(event) || [], cb]);},
            removeEventListener(event, cb) {events.set(event, (events.get(event) || []).filter(fn => fn !== cb));},
            async fire(event, extra = {}) {for (const cb of events.get(event) || []) await cb({target: this, preventDefault() {}, ...extra});},
            getContext: () => context, querySelector: () => null, querySelectorAll: () => [], closest: () => null,
            getBoundingClientRect: () => ({left: 0, top: 0, right: 2000, bottom: 1000, width: 2000, height: 1000}), focus() {}};
        Object.defineProperty(el, 'innerHTML', {get: () => '', set: () => {el.children = [];}});
        return el;
    }
    const document = {getElementById(id) {if (!ids.has(id)) ids.set(id, element(id === 'canvas' ? 'canvas' : 'div')); return ids.get(id);},
        createElement: element, querySelector: () => null, querySelectorAll: () => [],
        addEventListener: (name, cb) => listeners.set(name, cb), body: element(), documentElement: element()};
    const window = {location: {origin: 'https://home.test'}, scrollX: 0, scrollY: 0, innerHeight: 1000,
        addEventListener: (name, cb) => listeners.set(`window:${name}`, cb)}; window.parent = window;
    const sandbox = vm.createContext({document, window, console: {log() {}, warn() {}, error() {}}, Date: ClockDate, URL, FormData, Response,
        Image: class {constructor() {this.naturalWidth = 0;}}, setTimeout: () => 1, clearTimeout() {}, setInterval: (cb, ms) => {intervals.push({cb, ms}); return intervals.length;}, clearInterval() {},
        requestAnimationFrame: () => 1, localStorage: {getItem: () => null, setItem() {}},
        fetch: async (url, options) => {
            requests.push({url, options});
            if (url === '/api/bps/cords' && network.failCords) throw new Error('connection lost');
            const body = url === '/api/bps/read_text' ? {coordinates: JSON.stringify(layout), entities, receivers: []}
                : url === '/api/bps/cords' ? network.cords
                : url === '/api/bps/scanner_linking' ? {placed: [], unplaced: [], beacons: []} : url === '/api/bps/calibration' ? {} : [];
            const status = url === '/api/bps/cords' ? network.cordsStatus : 200;
            return {ok: status === 200, status, json: async () => body};
        }});
    vm.runInContext(readFileSync(join(directory, 'outdoor.js'), 'utf8'), sandbox);
    vm.runInContext(readFileSync(join(directory, 'diagnostics.js'), 'utf8'), sandbox);
    // Expose closures only in the VM so production keeps its private state.
    const code = readFileSync(join(directory, 'script.js'), 'utf8').replace('    // With a single configured floor', `
        globalThis.hooks = {layout: () => finalcords, beginEnvironmentEdit, finalizeShape, cancelShapeEdit, savedata,
            select: name => {SelMapName = name; mapname.value = name; img.naturalWidth = 2000; new_floor = false;},
            setPoints: points => {zonePoints = points;}, editing: () => editTarget,
            tracks: () => lastTracks, tracked: () => trackedDevices};
    // With a single configured floor`);
    vm.runInContext(code, sandbox);
    const ready = listeners.get('DOMContentLoaded')();
    listeners.get('window:message')({origin: window.location.origin, data: {type: 'bps-auth', token: 'test-token'}});
    await ready;
    return {hooks: sandbox.hooks, el: id => document.getElementById(id), requests, intervals, canvasCalls, network,
        setClock: now => {clock = now;}};
}
const layout = () => ({floor: [{name: 'Property', scale: 20, zones: [], receivers: []}]});
const points = [{x: 100, y: 100}, {x: 200, y: 100}, {x: 200, y: 200}, {x: 100, y: 200}];

test('panel old layout loads without outdoor fields and opt-in saves via authenticated layout endpoint', async () => {
    const p = await panel(layout());
    assert.ok(p.hooks, 'panel initialization completed');
    assert.equal(p.hooks.layout().outdoor_tracking, undefined);
    assert.equal(p.el('outdoorEnabled').checked, false);
    assert.equal(p.el('outdoorControls').hidden, true);
    p.el('outdoorEnabled').checked = true;
    await p.el('outdoorEnabled').fire('change');
    assert.equal(p.hooks.layout().outdoor_tracking.enabled, true);
    assert.equal(p.el('outdoorControls').hidden, false);
    p.el('outdoorUncertainty').checked = false; await p.el('outdoorUncertainty').fire('change');
    assert.equal(p.hooks.layout().outdoor_tracking.show_uncertainty, false);
    p.el('outdoorThreshold').value = '7.4'; await p.el('outdoorThreshold').fire('change');
    assert.equal(p.hooks.layout().outdoor_tracking.hide_uncertainty_below_m, 7.4);
    p.hooks.select('Property'); await p.hooks.savedata(true);
    const request = p.requests.find(r => r.url === '/api/bps/save_text');
    assert.equal(request.options.headers.Authorization, 'Bearer test-token');
    assert.equal(JSON.parse(request.options.body.get('coordinates')).outdoor_tracking.enabled, true);
});

test('panel enforces uncertainty threshold bounds before changing or saving the layout', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    const p = await panel(source);
    for (const value of ['0', '10000']) {
        p.el('outdoorThreshold').value = value; await p.el('outdoorThreshold').fire('change');
        assert.equal(p.hooks.layout().outdoor_tracking.hide_uncertainty_below_m, Number(value));
    }
    const before = JSON.stringify(plain(p.hooks.layout()));
    for (const value of ['10000.1', '1e309', '-0.1', 'invalid']) {
        p.el('outdoorThreshold').value = value; await p.el('outdoorThreshold').fire('change');
        assert.equal(JSON.stringify(plain(p.hooks.layout())), before);
        assert.equal(Number(p.el('outdoorThreshold').value), 10000, 'restore the valid saved value');
    }
    p.hooks.select('Property'); await p.hooks.savedata(true);
    const saved = JSON.parse(p.requests.find(r => r.url === '/api/bps/save_text').options.body.get('coordinates'));
    assert.equal(saved.outdoor_tracking.hide_uncertainty_below_m, 10000);
});

test('panel shared polygon editor creates/edits/cancels/deletes environments and reloads saved points', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    const p = await panel(source); p.hooks.select('Property');
    p.el('environmentType').value = 'building'; p.el('environmentMaterial').value = 'metal';
    p.hooks.beginEnvironmentEdit(); p.hooks.setPoints(points); p.el('zoneName').value = 'Metal shop';
    assert.equal(p.hooks.finalizeShape(), true);
    let polygon = p.hooks.layout().floor[0].environment[0];
    assert.equal(polygon.material, 'metal'); assert.deepEqual(plain(polygon.points), points);
    p.hooks.beginEnvironmentEdit(polygon); p.hooks.setPoints(points.map(p => ({x: p.x + 20, y: p.y}))); p.hooks.cancelShapeEdit();
    assert.deepEqual(plain(polygon.points), points);
    p.hooks.beginEnvironmentEdit(polygon); p.hooks.setPoints(points.map(p => ({x: p.x + 10, y: p.y}))); p.el('zoneName').value = 'Shop renamed';
    assert.equal(p.hooks.finalizeShape(), true); p.hooks.cancelShapeEdit();
    polygon = p.hooks.layout().floor[0].environment[0]; assert.equal(polygon.name, 'Shop renamed');
    await p.hooks.savedata(true);
    const saved = JSON.parse(p.requests.find(r => r.url === '/api/bps/save_text').options.body.get('coordinates'));
    const reloaded = await panel(saved); reloaded.hooks.select('Property');
    assert.deepEqual(plain(reloaded.hooks.layout().floor[0].environment), saved.floor[0].environment);
    // Redraw by cancelling the editor builds the saved-environment sidebar row.
    reloaded.hooks.beginEnvironmentEdit(saved.floor[0].environment[0]); reloaded.hooks.cancelShapeEdit();
    await reloaded.el('environmentList').children[0].children[1].fire('click');
    assert.equal(reloaded.hooks.layout().floor[0].environment.length, 0);
});

test('panel rejects a 129th environment polygon but edits and saves at capacity', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    source.floor[0].environment = Array.from({length: 128}, (_, i) => ({id: `area_${i}`,
        name: `Area ${i}`, type: 'building', material: 'metal', points}));
    const p = await panel(source); p.hooks.select('Property');
    await p.el('addTrees').fire('click');
    p.hooks.setPoints(points); p.el('zoneName').value = 'Extra trees';
    const before = JSON.stringify(plain(p.hooks.layout()));
    assert.equal(p.hooks.finalizeShape(), false);
    assert.equal(JSON.stringify(plain(p.hooks.layout())), before);
    assert.equal(p.hooks.editing().kind, 'environment', 'failed insertion leaves the editor open');
    p.hooks.cancelShapeEdit();
    p.hooks.beginEnvironmentEdit(p.hooks.layout().floor[0].environment[0]);
    p.hooks.setPoints(points.map(p => ({x: p.x + 10, y: p.y}))); p.el('zoneName').value = 'Shop renamed';
    assert.equal(p.hooks.finalizeShape(), true);
    p.hooks.cancelShapeEdit(); await p.hooks.savedata(true);
    const saved = JSON.parse(p.requests.find(r => r.url === '/api/bps/save_text').options.body.get('coordinates'));
    assert.equal(saved.floor[0].environment.length, 128);
    assert.equal(saved.floor[0].environment[0].id, 'area_0');
    assert.equal(saved.floor[0].environment[0].name, 'Shop renamed');
    assert.equal(saved.floor[0].environment[0].points[0].x, 110);
});

test('Add building and Add trees save separate editable map areas', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    const p = await panel(source); p.hooks.select('Property');
    p.el('environmentMaterial').value = 'metal';
    await p.el('addBuilding').fire('click');
    assert.equal(p.hooks.editing().kind, 'environment');
    assert.equal(p.el('environmentType').value, 'building');
    p.hooks.setPoints(points); p.el('zoneName').value = 'Metal shop';
    assert.equal(p.hooks.finalizeShape(), true); p.hooks.cancelShapeEdit();
    await p.el('addTrees').fire('click');
    assert.equal(p.hooks.editing().kind, 'environment');
    assert.equal(p.el('environmentType').value, 'dense_trees');
    assert.equal(p.el('environmentMaterial').value, 'unknown');
    p.hooks.setPoints(points.map(p => ({x: p.x + 300, y: p.y}))); p.el('zoneName').value = 'Tree line';
    assert.equal(p.hooks.finalizeShape(), true); p.hooks.cancelShapeEdit();
    await p.hooks.savedata(true);
    const saved = JSON.parse(p.requests.find(r => r.url === '/api/bps/save_text').options.body.get('coordinates'));
    assert.deepEqual(saved.floor[0].environment.map(p => [p.name, p.type, p.material]),
        [['Metal shop', 'building', 'metal'], ['Tree line', 'dense_trees', 'unknown']]);
    const reloaded = await panel(saved);
    assert.deepEqual(plain(reloaded.hooks.layout().floor[0].environment), saved.floor[0].environment);
});

test('panel groups retain stable IDs and original beacon picker entries', async () => {
    const p = await panel(layout());
    p.el('outdoorEnabled').checked = true; await p.el('outdoorEnabled').fire('change');
    p.el('groupId').value = 'rover'; p.el('groupName').value = 'Rover'; p.el('groupEnabled').checked = true;
    p.el('groupBeacons').options.forEach(o => {o.selected = true;}); await p.el('saveGroup').fire('click');
    assert.deepEqual(plain(p.hooks.layout().tracker_groups[0].beacons), ['beacon_a', 'beacon_b']);
    assert.equal(p.el('groupId').disabled, true);
    assert.deepEqual(p.el('entSelector').options.map(o => o.value).filter(Boolean), ['beacon_a', 'beacon_b', 'bps_group_rover']);
    p.el('groupName').value = 'Rover renamed'; await p.el('saveGroup').fire('click');
    assert.equal(p.hooks.layout().tracker_groups.length, 1); assert.equal(p.hooks.layout().tracker_groups[0].id, 'rover');
    await p.el('deleteGroup').fire('click'); assert.equal(p.hooks.layout().tracker_groups.length, 0);
    assert.deepEqual(p.el('entSelector').options.map(o => o.value).filter(Boolean), ['beacon_a', 'beacon_b']);
});

test('panel rejects group creation colliding with a known beacon and accepts a different ID', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    const p = await panel(source, ['beacon_a', 'bps_group_rover']);
    const before = JSON.stringify(plain(p.hooks.layout()));
    p.el('groupId').value = 'rover'; p.el('groupName').value = 'Rover';
    p.el('groupBeacons').options.forEach(o => {o.selected = o.value === 'beacon_a';});
    for (const enabled of [true, false]) {
        p.el('groupEnabled').checked = enabled;
        await p.el('saveGroup').fire('click');
        assert.equal(JSON.stringify(plain(p.hooks.layout())), before);
        assert.equal(p.el('entSelector').options.filter(o => o.value === 'bps_group_rover').length, 1);
    }
    p.el('groupId').value = 'rover_dog'; p.el('groupEnabled').checked = true;
    await p.el('saveGroup').fire('click');
    assert.equal(p.hooks.layout().tracker_groups[0].id, 'rover_dog');
    assert.ok(p.el('entSelector').options.some(o => o.value === 'bps_group_rover_dog'));
    p.hooks.select('Property'); await p.hooks.savedata(true);
    const saved = JSON.parse(p.requests.find(r => r.url === '/api/bps/save_text').options.body.get('coordinates'));
    assert.equal(saved.tracker_groups[0].id, 'rover_dog');
});

test('panel rejects re-enabling a saved group that collides with a known beacon', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    source.tracker_groups = [{id: 'rover', name: 'Rover', enabled: false, beacons: ['beacon_a']}];
    const p = await panel(source, ['beacon_a', 'bps_group_rover']);
    p.el('entSelector').value = 'bps_group_rover'; await p.el('entSelector').fire('change');
    p.el('groupSelector').value = 'rover'; await p.el('groupSelector').fire('change');
    const before = JSON.stringify(plain(p.hooks.layout()));
    p.el('groupEnabled').checked = true; await p.el('saveGroup').fire('click');
    assert.equal(JSON.stringify(plain(p.hooks.layout())), before);
    assert.deepEqual(plain(p.hooks.tracked()), ['bps_group_rover']);
    p.el('groupEnabled').checked = false; p.el('groupName').value = 'Retired group';
    await p.el('saveGroup').fire('click');
    assert.equal(p.hooks.layout().tracker_groups[0].name, 'Retired group');
    assert.deepEqual(plain(p.hooks.tracked()), ['bps_group_rover']);
});

test('accepted optional group defaults remain visible, enabled and preserved across edits', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    source.tracker_groups = [
        {id: 'rover', beacons: ['beacon_a']},
        {id: 'spot', name: 'Spot', beacons: ['beacon_b']},
        {id: 'paused', name: 'Paused', enabled: false, beacons: ['beacon_a']},
    ];
    const p = await panel(source);
    const picker = () => p.el('entSelector').options.map(o => o.value).filter(Boolean);
    assert.deepEqual(picker(), ['beacon_a', 'beacon_b', 'bps_group_rover', 'bps_group_spot']);
    // Loading defaults must not rewrite the stored layout.
    assert.deepEqual(plain(p.hooks.layout().tracker_groups), source.tracker_groups);
    p.el('groupSelector').value = 'rover'; await p.el('groupSelector').fire('change');
    assert.equal(p.el('groupName').value, 'rover');
    assert.equal(p.el('groupEnabled').checked, true);
    p.el('groupSelector').value = 'spot'; await p.el('groupSelector').fire('change');
    assert.equal(p.el('groupEnabled').checked, true);
    p.el('groupName').value = 'Spot renamed'; await p.el('saveGroup').fire('click');
    assert.equal(p.hooks.layout().tracker_groups.find(g => g.id === 'spot').enabled, true);
    p.el('groupSelector').value = 'paused'; await p.el('groupSelector').fire('change');
    assert.equal(p.el('groupEnabled').checked, false);
    await p.el('deleteGroup').fire('click');
    assert.deepEqual(plain(p.hooks.layout().tracker_groups.map(g => g.id)), ['rover', 'spot']);
    p.hooks.select('Property');
    await p.hooks.savedata(true);
    const saved = JSON.parse(p.requests.find(r => r.url === '/api/bps/save_text').options.body.get('coordinates'));
    const reloaded = await panel(saved);
    assert.deepEqual(reloaded.el('entSelector').options.map(o => o.value).filter(Boolean),
        ['beacon_a', 'beacon_b', 'bps_group_rover', 'bps_group_spot']);
});

test('Apply group rejects backend limit overflows and allows editing at capacity', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    const entities = Array.from({length: 17}, (_, i) => `beacon_${i}`);
    source.tracker_groups = Array.from({length: 32}, (_, i) =>
        ({id: `dog_${i}`, name: `Dog ${i}`, enabled: true, beacons: ['beacon_0']}));
    const p = await panel(source, entities);
    const before = JSON.stringify(plain(p.hooks.layout()));
    p.el('groupName').value = 'New dog'; p.el('groupId').value = 'r'.repeat(65);
    p.el('groupBeacons').options.forEach(o => {o.selected = o.value === 'beacon_0';});
    await p.el('saveGroup').fire('click');
    assert.equal(JSON.stringify(plain(p.hooks.layout())), before);
    p.el('groupId').value = 'rover';
    p.el('groupBeacons').options.forEach(o => {o.selected = true;});
    await p.el('saveGroup').fire('click');
    assert.equal(JSON.stringify(plain(p.hooks.layout())), before);
    p.el('groupBeacons').options.forEach(o => {o.selected = o.value === 'beacon_0';});
    await p.el('saveGroup').fire('click');
    assert.equal(JSON.stringify(plain(p.hooks.layout())), before);
    p.el('groupSelector').value = 'dog_0'; await p.el('groupSelector').fire('change');
    p.el('groupName').value = 'Dog renamed'; await p.el('saveGroup').fire('click');
    assert.equal(p.hooks.layout().tracker_groups.length, 32);
    assert.equal(p.hooks.layout().tracker_groups[0].name, 'Dog renamed');
});

test('disabled outdoor groups do not alter picker and genuine group-prefixed beacons remain', async () => {
    const source = layout();
    source.tracker_groups = [{id: 'rover', name: 'Rover', enabled: true, beacons: ['beacon_a']}];
    const p = await panel(source, ['beacon_a', 'bps_group_beacon']);
    const keys = () => p.el('entSelector').options.map(o => o.value).filter(Boolean);
    assert.deepEqual(keys(), ['beacon_a', 'bps_group_beacon']);
    p.el('outdoorEnabled').checked = true; await p.el('outdoorEnabled').fire('change');
    assert.deepEqual(keys(), ['beacon_a', 'bps_group_beacon', 'bps_group_rover']);
    p.el('outdoorEnabled').checked = false; await p.el('outdoorEnabled').fire('change');
    assert.deepEqual(keys(), ['beacon_a', 'bps_group_beacon']);
});

for (const action of ['disable', 'delete']) {
    test(`${action} of a colliding group preserves tracking of the genuine beacon`, async () => {
        const source = layout(); source.outdoor_tracking = {enabled: true};
        source.tracker_groups = [{id: 'rover', name: 'Rover', enabled: true, beacons: ['beacon_a']}];
        const p = await panel(source, ['beacon_a', 'bps_group_rover']); p.hooks.select('Property');
        p.network.cords = [{ent: 'bps_group_rover', floor: 'Property', cords: [100, 100]}];
        p.el('entSelector').value = 'bps_group_rover'; await p.el('entSelector').fire('change');
        await p.el('starttrack').fire('click');
        await p.intervals.findLast(i => i.ms === 500).cb();
        const cached = p.hooks.tracks().get('bps_group_rover');
        p.el('groupSelector').value = 'rover'; await p.el('groupSelector').fire('change');
        if (action === 'disable') {
            p.el('groupEnabled').checked = false; await p.el('saveGroup').fire('click');
            assert.equal(p.hooks.layout().tracker_groups[0].enabled, false);
        } else {
            await p.el('deleteGroup').fire('click');
            assert.equal(p.hooks.layout().tracker_groups.length, 0);
        }
        assert.deepEqual(plain(p.hooks.tracked()), ['bps_group_rover']);
        assert.equal(p.hooks.tracks().get('bps_group_rover'), cached);
        assert.ok(p.el('entSelector').options.some(o => o.value === 'bps_group_rover'));
    });
}

test('failed position poll repaints last-known panel uncertainty as stale', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    const p = await panel(source); p.hooks.select('Property'); p.setClock(1000000);
    p.network.cords = [{ent: 'beacon_a', cords: [100, 100], floor: 'Property', updated: 1000,
        outdoor: {observed: 1000, estimated_uncertainty_m: 7.4, stale_after_s: 30}}];
    p.el('entSelector').value = 'beacon_a'; await p.el('entSelector').fire('change');
    await p.el('starttrack').fire('click');
    const poll = p.intervals.findLast(i => i.ms === 500);
    assert.ok(poll, 'tracking timer started');
    await poll.cb();
    assert.ok(p.canvasCalls.some(c => c[0] === 'arc' && c[3] === 148), 'fresh circle was painted');
    p.canvasCalls.length = 0; p.setClock(1031000); p.network.failCords = true;
    await poll.cb();
    assert.ok(p.canvasCalls.some(c => c[0] === 'arc' && c[3] === 148), 'cached circle repainted on failure');
    assert.ok(p.canvasCalls.some(c => c[0] === 'dash' && c[1] === 3 && c[2] === 6), 'expired observation uses stale styling');
});

for (const kind of ['partial', 'empty', 'no-data']) {
    test(`panel clears vanished groups from authoritative ${kind} response`, async () => {
        const source = layout(); source.outdoor_tracking = {enabled: true};
        source.tracker_groups = [{id: 'rover', name: 'Rover', beacons: ['beacon_a']}];
        const p = await panel(source); p.hooks.select('Property'); p.setClock(1000000);
        const beacon = {ent: 'beacon_a', cords: [100, 100], floor: 'Property', updated: 1000,
            outdoor: {observed: 1000, estimated_uncertainty_m: 3}};
        const group = {...beacon, ent: 'bps_group_rover', group: true, name: 'Rover',
            beacons_reporting: 1, total_beacons: 1, outdoor: {...beacon.outdoor, estimated_uncertainty_m: 7.4}};
        p.network.cords = [beacon, group];
        for (const key of ['beacon_a', 'bps_group_rover']) {
            p.el('entSelector').value = key; await p.el('entSelector').fire('change');
        }
        p.el('outdoorDiagnostics').checked = true;
        await p.el('starttrack').fire('click');
        const poll = p.intervals.findLast(i => i.ms === 500);
        await poll.cb();
        assert.equal(p.hooks.tracks().has('bps_group_rover'), true);
        assert.match(p.el('outdoorDiagnosticText').textContent, /Rover: 1\/1/);
        p.canvasCalls.length = 0;
        p.network.cords = kind === 'partial' ? [beacon] : kind === 'empty' ? [] : {error: 'No data available'};
        p.network.cordsStatus = kind === 'no-data' ? 404 : 200;
        await poll.cb();
        assert.equal(p.hooks.tracks().has('bps_group_rover'), false);
        assert.equal(p.hooks.tracks().has('beacon_a'), true, 'legacy beacon caching remains unchanged');
        assert.ok(!p.canvasCalls.some(c => c[0] === 'arc' && c[3] === 148), 'removed group uncertainty is not drawn');
        assert.doesNotMatch(p.el('outdoorDiagnosticText').textContent, /Rover: 1\/1/);
        assert.equal(p.el('zonediv').style.display, kind === 'partial' ? '' : 'none');
    });
}

test('panel constituent circles inherit group freshness and expire during failed polling', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    source.tracker_groups = [{id: 'rover', name: 'Rover', beacons: ['beacon_a', 'beacon_b']}];
    const p = await panel(source); p.hooks.select('Property'); p.setClock(1000000);
    p.el('outdoorDiagnostics').checked = true;
    p.network.cords = [{ent: 'bps_group_rover', group: true, name: 'Rover', cords: [100, 100],
        floor: 'Property', updated: 1000, outdoor: {observed: 1000, estimated_uncertainty_m: 7.4, stale_after_s: 90},
        beacon_positions: [{ent: 'beacon_a', cords: [120, 100], floor: 'Property',
            updated: 955, age_s: 45, estimated_uncertainty_m: 3}]}];
    p.el('entSelector').value = 'bps_group_rover'; await p.el('entSelector').fire('change');
    await p.el('starttrack').fire('click');
    const poll = p.intervals.findLast(i => i.ms === 500);
    p.canvasCalls.length = 0; await poll.cb();
    assert.ok(p.canvasCalls.some(call => call[0] === 'arc' && call[3] === 60));
    const staleDash = call => call[0] === 'dash' && call[1] === 3 && call[2] === 6;
    assert.ok(!p.canvasCalls.some(staleDash), '45-second beacon is fresh under the 90-second cutoff');
    p.setClock(1046000); p.network.failCords = true;
    p.canvasCalls.length = 0; await poll.cb();
    assert.ok(p.canvasCalls.some(staleDash), 'cached beacon expires at its inherited cutoff');
    const cached = p.hooks.tracks().get('bps_group_rover');
    cached.outdoor.stale_after_s = 300; p.setClock(1155000);
    p.canvasCalls.length = 0; await poll.cb();
    assert.ok(!p.canvasCalls.some(staleDash), 'group position timeout applies when the reading gate is disabled');
});

test('panel retains group fixes through HTTP failures and malformed responses', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    source.tracker_groups = [{id: 'rover', name: 'Rover', beacons: ['beacon_a']}];
    const p = await panel(source); p.hooks.select('Property'); p.setClock(1000000);
    p.network.cords = [{ent: 'bps_group_rover', group: true, name: 'Rover', cords: [100, 100],
        floor: 'Property', updated: 1000, outdoor: {observed: 1000, estimated_uncertainty_m: 7.4}}];
    p.el('entSelector').value = 'bps_group_rover'; await p.el('entSelector').fire('change');
    await p.el('starttrack').fire('click');
    const poll = p.intervals.findLast(i => i.ms === 500);
    await poll.cb();
    const cached = p.hooks.tracks().get('bps_group_rover');
    for (const [status, body] of [[500, []], [401, []], [404, {error: 'Not found'}], [200, {}]]) {
        p.network.cordsStatus = status; p.network.cords = body;
        await poll.cb();
        assert.equal(p.hooks.tracks().get('bps_group_rover'), cached);
    }
    p.network.failCords = true;
    await poll.cb();
    assert.equal(p.hooks.tracks().get('bps_group_rover'), cached);
});
