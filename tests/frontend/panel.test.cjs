const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const directory = join(__dirname, '../../custom_components/bps/frontend');
const plain = x => JSON.parse(JSON.stringify(x));

async function panel(layout) {
    const ids = new Map(), listeners = new Map(), requests = [];
    const context = new Proxy({}, {get: (obj, key) => key === 'measureText' ? () => ({width: 30}) : key === 'createLinearGradient'
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
    const sandbox = vm.createContext({document, window, console: {log() {}, warn() {}, error() {}}, Date, URL, FormData, Response,
        Image: class {constructor() {this.naturalWidth = 0;}}, setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {},
        requestAnimationFrame: () => 1, localStorage: {getItem: () => null, setItem() {}},
        fetch: async (url, options) => {
            requests.push({url, options});
            const body = url === '/api/bps/read_text' ? {coordinates: JSON.stringify(layout), entities: ['beacon_a', 'beacon_b'], receivers: []}
                : url === '/api/bps/scanner_linking' ? {placed: [], unplaced: [], beacons: []} : url === '/api/bps/calibration' ? {} : [];
            return {ok: true, status: 200, json: async () => body};
        }});
    vm.runInContext(readFileSync(join(directory, 'outdoor.js'), 'utf8'), sandbox);
    // Expose closures only in the VM so production keeps its private state.
    const code = readFileSync(join(directory, 'script.js'), 'utf8').replace('    // With a single configured floor', `
        globalThis.hooks = {layout: () => finalcords, beginEnvironmentEdit, finalizeShape, cancelShapeEdit, savedata,
            select: name => {SelMapName = name; mapname.value = name; img.naturalWidth = 2000; new_floor = false;},
            setPoints: points => {zonePoints = points;}, editing: () => editTarget};
    // With a single configured floor`);
    vm.runInContext(code, sandbox);
    const ready = listeners.get('DOMContentLoaded')();
    listeners.get('window:message')({origin: window.location.origin, data: {type: 'bps-auth', token: 'test-token'}});
    await ready;
    return {hooks: sandbox.hooks, el: id => document.getElementById(id), requests};
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

test('panel groups retain stable IDs and original beacon picker entries', async () => {
    const p = await panel(layout());
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
