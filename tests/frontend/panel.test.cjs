const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const directory = join(__dirname, '../../custom_components/bps/frontend');
const plain = x => JSON.parse(JSON.stringify(x));

async function panel(layout, entities = ['beacon_a', 'beacon_b'], iconsLoaded = false) {
    const ids = new Map(), listeners = new Map(), requests = [], intervals = [], canvasCalls = [];
    const network = {cords: [], cordsStatus: 200, failCords: false, calibration: {}};
    let clock = Date.now();
    class ClockDate extends Date {static now() {return clock;}}
    const context = new Proxy({}, {get: (obj, key) => key === 'setLineDash' ? dash => canvasCalls.push(['dash', ...dash])
        : key === 'arc' ? (...args) => canvasCalls.push(['arc', ...args])
        : key === 'fillText' ? (...args) => canvasCalls.push(['text', ...args])
        : key === 'drawImage' ? (img, ...args) => canvasCalls.push(['image', img.src, ...args])
        : key === 'measureText' ? () => ({width: 30}) : key === 'createLinearGradient'
        ? () => ({addColorStop() {}}) : obj[key] || (() => {}), set: (obj, key, value) => {obj[key] = value; return true;}});
    function element(tag = 'div') {
        const events = new Map();
        const el = {tagName: tag.toUpperCase(), value: '', checked: false, style: {}, dataset: {}, children: [], width: 2000, height: 1000,
            offsetWidth: 100, offsetHeight: 30, classList: {add() {}, remove() {}, toggle() {}, contains: () => false},
            get options() {return this.children;}, get selectedOptions() {return this.children.filter(e => e.selected);},
            appendChild(child) {child.parentElement = this; this.children.push(child); if (child.id) ids.set(child.id, child); return child;},
            contains(child) {return this.children.includes(child) || this.children.some(c => c.contains?.(child));},
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
        let html = '';
        Object.defineProperty(el, 'innerHTML', {get: () => html, set: value => {html = value; el.children = [];}});
        return el;
    }
    const document = {getElementById(id) {if (!ids.has(id)) ids.set(id, element(id === 'canvas' ? 'canvas' : 'div')); return ids.get(id);},
        createElement: element, querySelector: () => null, querySelectorAll: () => [],
        addEventListener: (name, cb) => listeners.set(name, cb), body: element(), documentElement: element()};
    const window = {location: {origin: 'https://home.test'}, scrollX: 0, scrollY: 0, innerHeight: 1000,
        addEventListener: (name, cb) => listeners.set(`window:${name}`, cb)}; window.parent = window;
    const sandbox = vm.createContext({document, window, console: {log() {}, warn() {}, error() {}}, Date: ClockDate, URL, FormData, Response,
        Image: class {constructor() {this.naturalWidth = iconsLoaded ? 24 : 0; this.complete = iconsLoaded;}}, setTimeout: () => 1, clearTimeout() {}, setInterval: (cb, ms) => {intervals.push({cb, ms}); return intervals.length;}, clearInterval() {},
        requestAnimationFrame: () => 1, localStorage: {getItem: () => null, setItem() {}},
        fetch: async (url, options) => {
            requests.push({url, options});
            if (url === '/api/bps/cords' && network.failCords) throw new Error('connection lost');
            const body = url === '/api/bps/read_text' ? {coordinates: JSON.stringify(layout), entities, receivers: []}
                : url === '/api/bps/cords' ? network.cords
                : url === '/api/bps/scanner_linking' ? {placed: [], unplaced: [], beacons: []} : url === '/api/bps/calibration' ? network.calibration : [];
            const status = url === '/api/bps/cords' ? network.cordsStatus : 200;
            return {ok: status === 200, status, json: async () => body};
        }});
    vm.runInContext(readFileSync(join(directory, 'outdoor.js'), 'utf8'), sandbox);
    vm.runInContext(readFileSync(join(directory, 'diagnostics.js'), 'utf8'), sandbox);
    // Expose closures only in the VM so production keeps its private state.
    const code = readFileSync(join(directory, 'script.js'), 'utf8').replace('    // With a single configured floor', `
        globalThis.hooks = {layout: () => finalcords, beginEnvironmentEdit, finalizeShape, cancelShapeEdit, savedata, drawZonePreview,
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

test('panel smooths the group circle without adding member circles or altering raw diagnostics', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    source.tracker_groups = [{id: 'rover', name: 'Rover', beacons: ['beacon_a', 'beacon_b']}];
    const p = await panel(source); p.hooks.select('Property');
    p.el('entSelector').value = 'bps_group_rover'; await p.el('entSelector').fire('change');
    await p.el('starttrack').fire('click');
    const poll = p.intervals.findLast(i => i.ms === 500);
    for (const [seconds, radius] of [[0, 3], [1, 120], [2, 120], [3, 120], [4, 3]]) {
        const now = 100000 + seconds * 1000;
        p.setClock(now); p.canvasCalls.length = 0;
        p.network.cords = [{ent: 'bps_group_rover', group: true, name: 'Rover', floor: 'Property',
            cords: [200, 100], updated: now / 1000, outdoor: {estimated_uncertainty_m: radius, observed: now / 1000},
            beacon_positions: [{ent: 'beacon_a', cords: [195, 100]}, {ent: 'beacon_b', cords: [205, 100]}]}];
        await poll.cb();
        const fix = p.hooks.tracks().get('bps_group_rover');
        assert.equal(fix.payload.outdoor.estimated_uncertainty_m, radius);
        const circles = p.canvasCalls.filter(c => c[0] === 'arc' && c[3] > 24);
        assert.equal(circles.length, 1);
        assert.equal(circles[0][3], 60);
    }
});

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

test('panel displays tiny numbered beacons under the main icon with diagnostics off', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true, show_uncertainty: false};
    source.tracker_groups = [{id: 'rover', name: 'Rover', beacons: ['beacon_a', 'beacon_b']}];
    const p = await panel(source, ['beacon_a', 'beacon_b'], true); p.hooks.select('Property'); p.setClock(1000000);
    const beaconA = {ent: 'beacon_a', cords: [60, 100], floor: 'Property', updated: 1000};
    const beaconB = {ent: 'beacon_b', cords: [140, 100], floor: 'Property', updated: 1000};
    p.network.cords = [{ent: 'bps_group_rover', group: true, name: 'Rover', cords: [100, 100], floor: 'Property',
        updated: 1000, outdoor: {observed: 1000}, beacon_positions: [beaconA, beaconB,
            {ent: 'other', cords: [500, 500], floor: 'Barn'}, {ent: 'invalid', cords: [NaN, 100]}, null]}];
    p.el('entSelector').value = 'bps_group_rover'; await p.el('entSelector').fire('change');
    await p.el('starttrack').fire('click');
    const poll = p.intervals.findLast(i => i.ms === 500);
    p.canvasCalls.length = 0; await poll.cb();
    assert.equal(p.el('outdoorDiagnostics').checked, false);
    assert.deepEqual(p.canvasCalls.filter(c => c[0] === 'arc').map(c => c.slice(1, 4)), [[60, 100, 12], [140, 100, 12]],
        'only tiny member circles, no extra diagnostic overlays');
    const main = p.canvasCalls.findIndex(c => c[0] === 'image' && c[1] === '/bps/person.svg');
    assert.ok(main > p.canvasCalls.findLastIndex(c => c[0] === 'arc'), 'main icon is painted on top');
    assert.deepEqual(p.canvasCalls[main].slice(-2), [80, 80], 'main icon stays full size');
    assert.ok(p.canvasCalls.some(c => c[0] === 'text' && c[1] === 'Rover'));
    assert.deepEqual(p.canvasCalls.filter(c => c[0] === 'text' && ['1', '2'].includes(c[1])).map(c => c[1]), ['1', '2']);
    p.network.cords[0].beacon_positions = [beaconB];
    p.canvasCalls.length = 0; await poll.cb();
    assert.deepEqual(p.canvasCalls.filter(c => c[0] === 'text' && ['1', '2'].includes(c[1])).map(c => c[1]), ['2']);
});

test('panel draws only the main group circle with diagnostics and ages it on failed polling', async () => {
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
    assert.ok(!p.canvasCalls.some(call => call[0] === 'arc' && call[3] === 60));
    assert.equal(p.canvasCalls.filter(call => call[0] === 'arc' && call[3] >= 60).length, 1);
    assert.ok(p.canvasCalls.some(call => call[0] === 'arc' && call[3] === 148));
    const staleDash = call => call[0] === 'dash' && call[1] === 3 && call[2] === 6;
    assert.ok(!p.canvasCalls.some(staleDash), '45-second beacon is fresh under the 90-second cutoff');
    p.setClock(1091000); p.network.failCords = true;
    p.canvasCalls.length = 0; await poll.cb();
    assert.ok(p.canvasCalls.some(staleDash), 'cached beacon expires at its inherited cutoff');
    const cached = p.hooks.tracks().get('bps_group_rover');
    cached.outdoor.stale_after_s = 300; p.setClock(1155000);
    p.canvasCalls.length = 0; await poll.cb();
    assert.ok(!p.canvasCalls.some(staleDash), 'group position timeout applies when the reading gate is disabled');
});

test('panel keeps one uncertainty circle per tracker when a group and an individual are selected', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    source.tracker_groups = [{id: 'rover', name: 'Rover', beacons: ['beacon_a', 'beacon_b']}];
    const p = await panel(source, undefined, true); p.hooks.select('Property'); p.setClock(1000000);
    p.el('outdoorDiagnostics').checked = true;
    const outdoor = {observed: 1000, estimated_uncertainty_m: 3};
    p.network.cords = [{ent: 'beacon_a', cords: [60, 100], floor: 'Property', updated: 1000, outdoor},
        {ent: 'bps_group_rover', group: true, name: 'Rover', cords: [100, 100], floor: 'Property', updated: 1000,
            outdoor: {...outdoor, estimated_uncertainty_m: 4}, beacon_positions: [
                {ent: 'beacon_a', cords: [60, 100], floor: 'Property', estimated_uncertainty_m: 3},
                {ent: 'beacon_b', cords: [140, 100], floor: 'Property', estimated_uncertainty_m: 3}]}];
    p.el('entSelector').value = 'beacon_a'; await p.el('entSelector').fire('change');
    await p.el('starttrack').fire('click');
    p.el('entSelector').value = 'bps_group_rover'; await p.el('entSelector').fire('change');
    p.canvasCalls.length = 0; await p.intervals.findLast(i => i.ms === 500).cb();
    assert.deepEqual(p.canvasCalls.filter(c => c[0] === 'arc' && c[3] >= 60).map(c => c.slice(1, 4)),
        [[60, 100, 60], [100, 100, 80]], 'one circle each; diagnostic members do not add bubbles');
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

test('panel applies and saves groups containing genuine group-prefixed beacons', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    const p = await panel(source, ['bps_group_beacon', 'beacon_b']);
    p.el('groupId').value = 'rover'; p.el('groupName').value = 'Rover'; p.el('groupEnabled').checked = true;
    p.el('groupBeacons').options.forEach(o => {o.selected = true;});
    await p.el('saveGroup').fire('click');
    assert.deepEqual(plain(p.hooks.layout().tracker_groups[0].beacons), ['bps_group_beacon', 'beacon_b']);
    assert.deepEqual(p.el('entSelector').options.map(o => o.value).filter(Boolean),
        ['bps_group_beacon', 'beacon_b', 'bps_group_rover']);
    p.hooks.select('Property'); await p.hooks.savedata(true);
    const saved = JSON.parse(p.requests.find(r => r.url === '/api/bps/save_text').options.body.get('coordinates'));
    assert.deepEqual(saved.tracker_groups[0].beacons, ['bps_group_beacon', 'beacon_b']);
});

test('visible building tool enables outdoor tracking and saves mixed wall materials', async () => {
    const p = await panel(layout()); p.hooks.select('Property');
    await p.el('addBuilding').fire('click');
    assert.equal(p.hooks.layout().outdoor_tracking.enabled, true);
    assert.equal(p.el('outdoorEditor').open, true);
    p.hooks.setPoints(points); p.hooks.drawZonePreview();
    assert.equal(p.el('environmentWalls').hidden, false);
    p.el('environmentMaterial').value = 'metal';
    p.el('environmentWall1').value = 'wood'; await p.el('environmentWall1').fire('change');
    p.el('environmentWall3').value = 'metal'; await p.el('environmentWall3').fire('change');
    p.el('zoneName').value = 'Mixed shop';
    assert.equal(p.hooks.finalizeShape(), true);
    const shop = p.hooks.layout().floor[0].environment[0];
    assert.deepEqual(plain(shop.wall_materials), ['wood', null, 'metal', null]);
    p.hooks.cancelShapeEdit();
    assert.equal(p.el('environmentWalls').hidden, true);
    p.hooks.beginEnvironmentEdit(shop);
    assert.equal(p.el('environmentWall1').value, 'wood');
    assert.equal(p.el('environmentWall2').value, '');
    await p.el('addTrees').fire('click');
    assert.equal(p.el('environmentWalls').hidden, true);
    p.hooks.cancelShapeEdit(); await p.hooks.savedata(true);
    const saved = JSON.parse(p.requests.find(r => r.url === '/api/bps/save_text').options.body.get('coordinates'));
    assert.deepEqual(saved.floor[0].environment[0].wall_materials, ['wood', null, 'metal', null]);
});

test('map tools are outside the opt-in controls in the actual document', () => {
    const html = readFileSync(join(directory, 'index.html'), 'utf8');
    const controls = html.indexOf('id="outdoorControls"');
    for (const id of ['addBuilding', 'addTrees', 'openGroupEditor']) {
        assert.ok(html.indexOf(`id="${id}"`) < controls);
        assert.equal(html.split(`id="${id}"`).length, 2);
    }
});

test('calibration tracking test uses the authenticated read-only action', async () => {
    const p = await panel(layout()); p.hooks.select('Property');
    p.network.calibration = {floor: 'Property', verdict: 'improves', baseline: {median_m: 3, p95_m: 5, samples: 20},
        candidate: {median_m: 1, p95_m: 2}, locations: 2, candidate_failures: 0, candidate_solved_at: 'test-date', scope: 'No calibration changed.'};
    await p.el('calibTestTracking').fire('click');
    const request = p.requests.find(r => r.url === '/api/bps/calibration' && r.options?.method === 'POST');
    assert.deepEqual(JSON.parse(request.options.body), {action: 'validate_tracking', floor: 'Property'});
    assert.equal(request.options.headers.Authorization, 'Bearer test-token');
    assert.equal(p.el('calibTestTracking').disabled, false);
    assert.match(p.el('calibTrackingTest').textContent, /Property: improves/);
    assert.match(p.el('calibTrackingTest').textContent, /3.00 m → 1.00 m/);
    assert.ok(!p.requests.some(r => r.url === '/api/bps/save_text'));
});

test('auto calibration displays apply and skip reasons even before a first result', async () => {
    const p = await panel(layout());
    p.hooks.select('Property');
    p.network.calibration = {mode: 'auto', auto_status: {Property: {
        state: 'skipped', reason: 'Collecting fresh, independent receiver observations', updated_receivers: 0}}};
    p.el('calibAuto').checked = true;
    await p.el('calibAuto').fire('change');
    assert.match(p.el('calibStatus').textContent, /skipped: Collecting fresh/);
    p.network.calibration.auto_status.Property = {state: 'applied', reason: 'Stable estimate',
        updated_receivers: 4, max_change_pct: 10, unchanged_receivers: ['weak_receiver'],
        tracking_validation: {verdict: 'inconclusive'}};
    await p.el('calibAuto').fire('change');
    assert.match(p.el('calibStatus').textContent, /applied: Stable estimate/);
    assert.match(p.el('calibStatus').textContent, /4 receivers, maximum change 10%/);
    assert.match(p.el('calibStatus').textContent, /1 unsupported receivers unchanged/);
    assert.match(p.el('calibStatus').textContent, /measured tracking check: inconclusive/);
});

test('auto preview distinguishes existing weak receivers from newly placed receivers', async () => {
    const source = layout();
    source.floor[0].receivers = ['r0', 'r1', 'r2', 'r3', 'extra'].map(entity_id => ({entity_id, cords: {x: 0, y: 0}}));
    const p = await panel(source);
    p.hooks.select('Property');
    p.network.calibration = {mode: 'auto', results: {Property: {floor: 'Property', receivers: {r0: 1, r1: 1, r2: 1, r3: 1},
        matrix: [], auto_excluded_receivers: ['extra'], missing_unmatched: [], missing_no_data: []}}};
    p.el('calibAuto').checked = true;
    await p.el('calibAuto').fire('change');
    assert.match(p.el('calibResults').innerHTML, /Auto updates excluded: extra/);
    assert.doesNotMatch(p.el('calibResults').innerHTML, /Placed since this report/);
    p.network.calibration.results.Property.auto_excluded_receivers = [];
    p.network.calibration.results.Property.missing_unmatched = ['extra'];
    await p.el('calibAuto').fire('change');
    assert.match(p.el('calibResults').innerHTML, /No matching Bermuda scanner:/);
    assert.doesNotMatch(p.el('calibResults').innerHTML, /Placed since this report/);
});
