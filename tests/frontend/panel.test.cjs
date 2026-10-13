const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const directory = join(__dirname, '../../custom_components/bps/frontend');
const plain = x => JSON.parse(JSON.stringify(x));

async function panel(layout, entities = ['beacon_a', 'beacon_b'], iconsLoaded = false, matching = null) {
    const ids = new Map(), listeners = new Map(), requests = [], intervals = [], canvasCalls = [];
    const network = {cords: [], cordsStatus: 200, failCords: false, calibration: {}, matching, matchingRequest: null};
    let clock = Date.now();
    class ClockDate extends Date {static now() {return clock;}}
    const context = new Proxy({}, {get: (obj, key) => key === 'setLineDash' ? dash => canvasCalls.push(['dash', ...dash])
        : ['beginPath', 'moveTo', 'lineTo', 'closePath', 'fill', 'stroke'].includes(key) ? (...args) => canvasCalls.push([key, ...args, ...(key === 'stroke' ? [obj.strokeStyle, obj.lineWidth] : [])])
        : key === 'arc' ? (...args) => canvasCalls.push(['arc', ...args])
        : key === 'fillText' ? (...args) => canvasCalls.push(['text', ...args])
        : key === 'drawImage' ? (img, ...args) => canvasCalls.push(['image', img.src, ...args])
        : key === 'measureText' ? () => ({width: 30}) : key === 'createLinearGradient'
        ? () => ({addColorStop() {}}) : obj[key] || (() => {}), set: (obj, key, value) => {obj[key] = value; return true;}});
    function element(tag = 'div') {
        const events = new Map();
        const el = {tagName: tag.toUpperCase(), value: '', checked: false, style: {}, dataset: {}, children: [], width: 2000, height: 1000,
            focus() {}, select() {this.selected = true;},
            offsetWidth: 100, offsetHeight: 30, validity: {badInput: false}, classList: {add() {}, remove() {}, toggle() {}, contains: () => false},
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
        addEventListener: (name, cb) => listeners.set(name, cb), removeEventListener: name => listeners.delete(name), body: element(), documentElement: element()};
    const window = {location: {origin: 'https://home.test'}, scrollX: 0, scrollY: 0, innerHeight: 1000,
        addEventListener: (name, cb) => listeners.set(`window:${name}`, cb)}; window.parent = window;
    const sandbox = vm.createContext({document, window, console: {log() {}, warn() {}, error() {}}, Date: ClockDate, URL, FormData, Response, TextEncoder,
        Image: class {constructor() {this.naturalWidth = iconsLoaded ? 24 : 0; this.complete = iconsLoaded;}}, setTimeout: () => 1, clearTimeout() {}, setInterval: (cb, ms) => {intervals.push({cb, ms}); return intervals.length;}, clearInterval() {},
        requestAnimationFrame: () => 1, localStorage: {getItem: () => null, setItem() {}},
        fetch: async (url, options) => {
            requests.push({url, options});
            if (url === '/api/bps/cords' && network.failCords) throw new Error('connection lost');
            if (url === '/api/bps/scanner_linking' && network.matchingRequest) return await network.matchingRequest();
            if (url === '/api/bps/scanner_linking' && network.matching) return {ok: true, status: 200,
                json: async () => ({placed: [], unplaced: [], beacons: [], matching: network.matching})};
            const body = url === '/api/bps/read_text' ? {coordinates: JSON.stringify(layout), entities, receivers: (matching?.options || []).map(o => o.entity_id), receiver_matching: matching, scanner_diagnostics: matching?.scanner_diagnostics}
                : url === '/api/bps/cords' ? network.cords
                : url === '/api/bps/scanner_linking' ? {placed: [], unplaced: [], beacons: []} : url === '/api/bps/calibration' ? network.calibration
                : url.startsWith('/api/bps/diagnostics?report=1') ? {text: 'BPS full diagnostics\n{"context":{"layout":{"floor":[]}}}'} : [];
            const status = url === '/api/bps/cords' ? network.cordsStatus : 200;
            return {ok: status === 200, status, json: async () => body};
        }});
    vm.runInContext(readFileSync(join(directory, 'outdoor.js'), 'utf8'), sandbox);
    vm.runInContext(readFileSync(join(directory, 'diagnostics.js'), 'utf8'), sandbox);
    // Expose closures only in the VM so production keeps its private state.
    const code = readFileSync(join(directory, 'script.js'), 'utf8').replace('    // With a single configured floor', `
        globalThis.hooks = {layout: () => finalcords, beginEnvironmentEdit, finalizeShape, cancelShapeEdit, savedata, drawZonePreview, drawEnvironmentPolygons, redrawAll,
            select: name => {SelMapName = name; mapname.value = name; img.naturalWidth = 2000; new_floor = false;},
            setPoints: points => {zonePoints = points;}, editing: () => editTarget,
            tracks: () => lastTracks, tracked: () => trackedDevices, hitDeviceAt, addDeviceButton, saveReceiver,
            setCalibration: results => {calibLastResults=results;}};
    // With a single configured floor`);
    vm.runInContext(code, sandbox);
    const ready = listeners.get('DOMContentLoaded')();
    listeners.get('window:message')({origin: window.location.origin, data: {type: 'bps-auth', token: 'test-token'}});
    await ready;
    await new Promise(resolve => setImmediate(resolve));
    return {hooks: sandbox.hooks, el: id => document.getElementById(id), requests, intervals, canvasCalls, network, document, listeners,
        setClock: now => {clock = now;}};
}
const layout = () => ({floor: [{name: 'Property', scale: 20, zones: [], receivers: []}]});
const points = [{x: 100, y: 100}, {x: 200, y: 100}, {x: 200, y: 200}, {x: 100, y: 200}];

function receiverProperties(p, id = 'proxy') {
    const button = {getAttribute: () => id};
    p.listeners.get('click')({target: {closest: selector => selector === '[data-type="recheight"]' ? button : null}});
    const dialog = p.document.body.children.findLast(e => e.className === 'bps-modal-overlay').children[0];
    return {height: dialog.children[1], unit: dialog.children[2], model: dialog.children[3].children[0], antenna: dialog.children[4].children[0],
        accuracy: dialog.children[5].children[0], hardware: dialog.children[6], external: dialog.children[7].children[0],
        save: dialog.children.at(-1).children[1], cancel: dialog.children.at(-1).children[0]};
}

test('receiver expected distance error converts selected feet and metres, persists canonically and validates atomically', async () => {
    const source = layout(); source.floor[0].receivers = [{entity_id: 'proxy', cords: {x: 100, y: 200}, height: 2}];
    const p = await panel(source); p.hooks.select('Property'); const receiver = p.hooks.layout().floor[0].receivers[0];
    let edit = receiverProperties(p); edit.accuracy.value = '2'; edit.unit.value = 'ft'; await edit.unit.fire('change');
    assert.ok(Math.abs(Number(edit.accuracy.value) - 2 / .3048) < 1e-8);
    edit.accuracy.value = '5'; await edit.save.fire('click'); await Promise.resolve();
    assert.equal(receiver.distance_accuracy_m, 1.524); assert.equal(receiver.height, 2); assert.equal(receiver.height_unit, 'ft');
    edit = receiverProperties(p); assert.equal(edit.accuracy.value, '5'); edit.unit.value = 'm'; await edit.unit.fire('change');
    assert.equal(Number(edit.accuracy.value), 1.524); await edit.cancel.fire('click'); await Promise.resolve();
    for (const raw of ['-1', '1001', 'Infinity', 'bad']) {
        edit = receiverProperties(p); edit.unit.value = 'm'; await edit.unit.fire('change'); edit.accuracy.value = raw; edit.model.value = 'changed';
        await edit.save.fire('click'); assert.equal(receiver.distance_accuracy_m, 1.524); assert.equal(receiver.hardware_model, undefined);
        await edit.cancel.fire('click'); await Promise.resolve();
    }
    edit = receiverProperties(p); edit.accuracy.value = ''; edit.accuracy.validity.badInput = true;
    await edit.save.fire('click'); assert.equal(receiver.distance_accuracy_m, 1.524);
    await edit.cancel.fire('click'); await Promise.resolve();
    await p.hooks.savedata(true);
    const saved = JSON.parse(p.requests.find(r => r.url === '/api/bps/save_text').options.body.get('coordinates'));
    assert.equal(saved.floor[0].receivers[0].distance_accuracy_m, 1.524);
    edit = receiverProperties(p); edit.height.value = ''; await edit.save.fire('click'); await Promise.resolve();
    assert.equal(receiver.height, undefined); assert.equal(receiver.height_unit, 'ft', 'accuracy alone retains unit preference');
    edit = receiverProperties(p); edit.accuracy.value = '0'; await edit.save.fire('click'); await Promise.resolve(); assert.equal(receiver.distance_accuracy_m, 0);
    edit = receiverProperties(p); edit.accuracy.value = ''; await edit.save.fire('click'); await Promise.resolve();
    assert.equal(receiver.distance_accuracy_m, undefined); assert.equal(receiver.height_unit, undefined);
});

test('beacon hardware profiles persist per beacon, remain independent of tuning and reject invalid or cancelled edits', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    source.tracker_groups = [{id: 'rover', name: 'Rover', beacons: ['beacon_a', 'beacon_b']}];
    const p = await panel(source); p.hooks.select('Property');
    await p.el('trackerHardwareProfile').fire('click'); assert.equal(p.hooks.layout().tracker_hardware, undefined);
    p.el('entSelector').value = 'bps_group_rover'; await p.el('entSelector').fire('change');
    await p.el('trackerHardwareProfile').fire('click'); assert.equal(p.hooks.layout().tracker_hardware, undefined);
    assert.ok(p.el('bpsToastHost').children.some(item => /Select an individual beacon/.test(item.textContent || '')));
    p.el('entSelector').value = 'beacon_a'; await p.el('entSelector').fire('change');
    const open = async () => {
        const pending = p.el('trackerHardwareProfile').fire('click');
        const dialog = p.document.body.children.findLast(e => e.className === 'bps-modal-overlay').children[0];
        return {pending, model: dialog.children[1].children[0], notes: dialog.children[2].children[0], save: dialog.children[3].children[1], cancel: dialog.children[3].children[0]};
    };
    let edit = await open(); edit.model.value = ' BLE tag '; edit.notes.value = ' Synthetic test hardware ';
    await edit.save.fire('click'); await edit.pending;
    assert.deepEqual(plain(p.hooks.layout().tracker_hardware), {beacon_a: {hardware_model: 'BLE tag', notes: 'Synthetic test hardware'}});
    assert.equal(p.hooks.layout().tracker_heights, undefined); assert.equal(p.hooks.layout().tracker_ref_power_trims, undefined);
    edit = await open(); assert.equal(edit.model.value, 'BLE tag'); edit.model.value = 'x'.repeat(129); edit.notes.value = 'changed';
    await edit.save.fire('click'); assert.equal(p.hooks.layout().tracker_hardware.beacon_a.notes, 'Synthetic test hardware');
    await edit.cancel.fire('click'); await edit.pending;
    edit = await open(); edit.notes.value = 'x'.repeat(257); await edit.save.fire('click');
    assert.equal(p.hooks.layout().tracker_hardware.beacon_a.notes, 'Synthetic test hardware'); await edit.cancel.fire('click'); await edit.pending;
    await p.hooks.savedata(true);
    const saved = JSON.parse(p.requests.find(r => r.url === '/api/bps/save_text').options.body.get('coordinates'));
    assert.equal(saved.tracker_hardware.beacon_a.hardware_model, 'BLE tag');
    edit = await open(); edit.model.value = ''; edit.notes.value = ''; await edit.save.fire('click'); await edit.pending;
    assert.equal(p.hooks.layout().tracker_hardware, undefined);
});

test('receiver feet heights save canonical metres and reopen in preferred units without migrating legacy values', async () => {
    const source = layout(); source.floor[0].receivers = [{entity_id: 'proxy', cords: {x: 100, y: 200}, height: 5}];
    const p = await panel(source); p.hooks.select('Property');
    const receiver = p.hooks.layout().floor[0].receivers[0];
    let edit = receiverProperties(p); assert.equal(edit.unit.value, 'm'); assert.equal(edit.height.value, '5');
    edit.unit.value = 'ft'; await edit.unit.fire('change');
    assert.ok(Math.abs(Number(edit.height.value) - 5 / .3048) < 1e-8, 'unit change preserves physical height');
    assert.equal(receiver.height, 5, 'untagged legacy height is not migrated');
    await edit.cancel.fire('click'); await Promise.resolve(); assert.equal(receiver.height_unit, undefined);
    for (const [feet, metres] of [[5, 1.524], [4, 1.2192]]) {
        edit = receiverProperties(p); edit.unit.value = 'ft'; await edit.unit.fire('change'); edit.height.value = String(feet);
        await edit.save.fire('click'); await Promise.resolve();
        assert.equal(receiver.height, metres); assert.equal(receiver.height_unit, 'ft');
        edit = receiverProperties(p); assert.equal(edit.height.value, String(feet));
        edit.unit.value = 'm'; await edit.unit.fire('change'); assert.equal(Number(edit.height.value), metres);
        await edit.cancel.fire('click'); await Promise.resolve();
    }
    await p.hooks.savedata(true);
    const saved = JSON.parse(p.requests.find(r => r.url === '/api/bps/save_text').options.body.get('coordinates'));
    assert.equal(saved.floor[0].receivers[0].height, 1.2192); assert.equal(saved.floor[0].receivers[0].height_unit, 'ft');
});

test('receiver height unit edits preserve atomic cancellation, reject malformed ranges and clear empty height preferences', async () => {
    const source = layout(); source.floor[0].receivers = [{entity_id: 'proxy', cords: {x: 100, y: 200}, hardware_model: 'M5Stack'}];
    const p = await panel(source); p.hooks.select('Property'); const receiver = p.hooks.layout().floor[0].receivers[0];
    let edit = receiverProperties(p); edit.unit.value = 'ft'; await edit.unit.fire('change'); assert.equal(edit.height.value, '');
    await edit.save.fire('click'); await Promise.resolve(); assert.equal(receiver.height, undefined); assert.equal(receiver.height_unit, undefined);
    edit = receiverProperties(p); edit.unit.value = 'ft'; await edit.unit.fire('change'); edit.model.value = 'changed';
    for (const value of ['bad', '-1', '40', 'Infinity']) {
        edit.height.value = value; await edit.save.fire('click');
        assert.equal(receiver.height, undefined); assert.equal(receiver.hardware_model, 'M5Stack');
    }
    edit.height.value = ''; edit.height.validity.badInput = true; await edit.save.fire('click'); assert.equal(receiver.height, undefined);
    edit.unit.value = 'm'; await edit.unit.fire('change'); assert.equal(edit.unit.value, 'ft', 'malformed input prevents unit change');
    await edit.cancel.fire('click'); await Promise.resolve(); assert.equal(receiver.hardware_model, 'M5Stack');
    receiver.height = 1.524; receiver.height_unit = 'ft'; edit = receiverProperties(p); edit.height.value = '';
    await edit.save.fire('click'); await Promise.resolve(); assert.equal(receiver.height, undefined); assert.equal(receiver.height_unit, undefined);
});

test('receiver property editor persists hardware and antenna notes without altering measurement calibration', async () => {
    const source = layout(); source.floor[0].receivers = [{entity_id: 'proxy', cords: {x: 100, y: 200}, height: 2}];
    const p = await panel(source); p.hooks.select('Property');
    const open = () => receiverProperties(p);
    let edit = open(); edit.model.value = ' ESP32 '; edit.antenna.value = ' External omni antenna ';
    await edit.save.fire('click'); await Promise.resolve();
    const receiver = p.hooks.layout().floor[0].receivers[0];
    assert.equal(receiver.hardware_model, 'ESP32'); assert.equal(receiver.antenna_notes, 'External omni antenna');
    assert.equal(receiver.height, 2); assert.equal(receiver.calibration, undefined);
    edit = open(); assert.equal(edit.model.value, 'ESP32'); edit.model.value = 'x'.repeat(129);
    await edit.save.fire('click'); assert.equal(receiver.hardware_model, 'ESP32');
    await edit.cancel.fire('click'); await Promise.resolve();
    await p.hooks.savedata(true);
    const saved = JSON.parse(p.requests.find(r => r.url === '/api/bps/save_text').options.body.get('coordinates'));
    assert.equal(saved.floor[0].receivers[0].antenna_notes, 'External omni antenna');
    edit = open(); edit.model.value = ''; edit.antenna.value = '';
    await edit.save.fire('click'); await Promise.resolve();
    assert.equal(receiver.hardware_model, undefined); assert.equal(receiver.antenna_notes, undefined);
});

test('receiver properties follow detected hardware and persist only explicit model and antenna overrides', async () => {
    const source = layout(); source.floor[0].receivers = [{entity_id: 'old_station', scanner_mac: 'aa:bb:cc:11:22:33',
        scanner_config_entry_id: 'entry', cords: {x: 100, y: 200}, correction: 1.3}];
    const hardware = {detected: {manufacturer: 'Synthetic', model: 'ESP32 board'},
        effective: {manufacturer: 'Synthetic', model: 'ESP32 board', external_antenna: true}, provenance: 'esphome'};
    const matching = {revision: 'hardware-1', options: [{entity_id: 'current_station', config_entry_ids: ['entry'],
        scanner_mac: 'aa:bb:cc:11:22:33', selectable: true, hardware,
        scanner_identities: [{config_entry_id: 'entry', scanner_mac: 'aa:bb:cc:11:22:33', hardware}]}]};
    const p = await panel(source, undefined, false, matching); p.hooks.select('Property');
    const receiver = p.hooks.layout().floor[0].receivers[0];
    let edit = receiverProperties(p, 'old_station');
    assert.match(edit.hardware.textContent, /Synthetic · ESP32 board/);
    assert.equal(edit.model.value, ''); assert.equal(edit.model.placeholder, 'Automatic: ESP32 board');
    assert.equal(edit.external.value, ''); assert.match(edit.external.options[0].textContent, /external/);
    edit.external.value = 'false'; await edit.save.fire('click'); await Promise.resolve();
    assert.equal(receiver.external_antenna, false); assert.equal(receiver.hardware_model, undefined);
    assert.equal(receiver.correction, 1.3); assert.equal(receiver.hardware, undefined);
    edit = receiverProperties(p, 'old_station'); assert.equal(edit.external.value, 'false');
    edit.external.value = 'true'; edit.model.value = 'Manual board'; await edit.cancel.fire('click'); await Promise.resolve();
    assert.equal(receiver.external_antenna, false); assert.equal(receiver.hardware_model, undefined);
    await p.hooks.savedata(true);
    const saved = JSON.parse(p.requests.find(r => r.url === '/api/bps/save_text').options.body.get('coordinates'));
    assert.equal(saved.floor[0].receivers[0].external_antenna, false);
    edit = receiverProperties(p, 'old_station'); edit.external.value = ''; await edit.save.fire('click'); await Promise.resolve();
    assert.equal(receiver.external_antenna, undefined);
});

test('receiver properties never borrow automatic hardware from a reused slug or another owner', async () => {
    for (const placement of [
        {scanner_mac: 'dd:ee:ff:11:22:33', scanner_config_entry_id: 'entry'},
        {scanner_mac: 'aa:bb:cc:11:22:33', scanner_config_entry_id: 'other'},
        {scanner_mac: 'invalid'},
    ]) {
        const source = layout(); source.floor[0].receivers = [{entity_id: 'proxy', cords: {x: 100, y: 200}, ...placement}];
        const hardware = {effective: {model: 'Wrong board'}};
        const matching = {revision: 'hardware-1', options: [{entity_id: 'proxy', scanner_mac: 'aa:bb:cc:11:22:33',
            config_entry_ids: ['entry'], hardware, scanner_identities: [{config_entry_id: 'entry', scanner_mac: 'aa:bb:cc:11:22:33', hardware}]}]};
        const p = await panel(source, undefined, false, matching); p.hooks.select('Property');
        const edit = receiverProperties(p);
        assert.match(edit.hardware.textContent, /unknown/); assert.doesNotMatch(edit.hardware.textContent, /Wrong board/);
        assert.match(edit.external.options[0].textContent, /unknown/); await edit.cancel.fire('click');
    }
});

test('Debugging refresh updates detected model and inherited antenna without rewriting placement metadata', async () => {
    const source=layout(); source.floor[0].receivers=[{entity_id:'proxy',cords:{x:100,y:100},correction:1.2}];
    const matching={revision:'old',options:[{entity_id:'proxy',hardware:{effective:{model:'Old board',external_antenna:true}}}]};
    const p=await panel(source,undefined,false,matching); p.hooks.select('Property');
    let edit=receiverProperties(p); assert.match(edit.hardware.textContent,/Old board/); await edit.cancel.fire('click');
    p.network.matching={revision:'new',options:[{entity_id:'proxy',hardware:{effective:{model:'New board',external_antenna:false}}}]};
    p.listeners.get('click')({target:{closest:selector=>selector==='[data-type="refreshlinking"]'?{}:null}});
    await new Promise(resolve=>setImmediate(resolve));
    edit=receiverProperties(p); assert.match(edit.hardware.textContent,/New board/); assert.match(edit.external.options[0].textContent,/built-in/);
    await edit.cancel.fire('click');
    p.network.matching={revision:'removed',options:[]};
    p.listeners.get('click')({target:{closest:selector=>selector==='[data-type="refreshlinking"]'?{}:null}});
    await new Promise(resolve=>setImmediate(resolve));
    edit=receiverProperties(p); assert.match(edit.hardware.textContent,/unknown/); await edit.cancel.fire('click');
    assert.equal(p.hooks.layout().floor[0].receivers[0].correction,1.2);
});

test('receiver-to-receiver labels show measured and map distances in both feet and metres', async () => {
    const source=layout(); source.floor[0].receivers=[{entity_id:'a',cords:{x:0,y:0}},
        {entity_id:'b',cords:{x:60,y:80}},{entity_id:'c',cords:{x:200,y:200}}];
    const p=await panel(source); p.hooks.select('Property');
    p.hooks.setCalibration({Property:{floor:'Property',matrix:[{tx:'a',rx:'b',corrected_m:3,measured_m:4,true_m:5}]}});
    p.el('recDistToggle').checked=true; p.el('recDistColor').value='all'; p.el('recDistMode').value='calibrated';
    p.listeners.get('click')({target:{closest:selector=>selector==='[data-type="focusrec"]'?{getAttribute:()=> 'a'}:null}});
    assert.ok(p.canvasCalls.some(call=>call[0]==='text'&&call[1]==='BLE 9.8 ft / 3.0 m · Map 16.4 ft / 5.0 m'));
});

test('panel Show distances draws same-floor group member ranges once while retaining different ranges', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    source.tracker_groups = [{id: 'rover', name: 'Rover', beacons: ['beacon_a', 'beacon_b', 'beacon_c']}];
    const p = await panel(source); p.hooks.select('Property');
    p.el('entSelector').value = 'bps_group_rover'; await p.el('entSelector').fire('change');
    await p.el('starttrack').fire('click'); p.el('circleToggle').checked = true;
    p.setClock(100000); p.canvasCalls.length = 0;
    p.network.cords = [{ent: 'beacon_a', floor: 'Property', cords: [195, 100], radii: [[0, 0, 90], [30, 40, 100]]},
        {ent: 'beacon_b', floor: 'Property', cords: [205, 100], radii: [[0, 0, 90], [30, 40, 110]]},
        {ent: 'beacon_c', floor: 'Barn', cords: [205, 100], radii: [[0, 0, 500]]},
        {ent: 'bps_group_rover', group: true, floor: 'Property', cords: [200, 100], updated: 100,
            outdoor: {estimated_uncertainty_m: 120, display_radius_limit_m: 8, observed: 100}, beacon_positions: [
                {ent: 'beacon_a', floor: 'Property'}, {ent: 'beacon_b', floor: 'Property', used: false}, {ent: 'beacon_c', floor: 'Barn'}]}];
    p.el('trackingDistanceMode').value = 'measured';
    await p.intervals.findLast(i => i.ms === 500).cb();
    assert.deepEqual(plain(p.hooks.tracks().get('bps_group_rover').circles), [[0, 0, 90], [30, 40, 100], [30, 40, 110]]);
    const radii = p.canvasCalls.filter(c => c[0] === 'arc' && c[3] > 24).map(c => c[3]);
    assert.deepEqual(radii.sort((a, b) => a - b), [90, 100, 110, 160]);
    p.el('outdoorDiagnostics').checked = true; await p.el('outdoorDiagnostics').fire('change');
    assert.match(p.el('outdoorDiagnosticText').textContent, /capped for display.*displayed 8.0 m/);
});

test('live distance defaults to dashed measured and solid estimated group circles, using consistent units', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    source.tracker_groups = [{id: 'dog', name: 'Dog', beacons: ['beacon_a', 'beacon_b']}];
    const p = await panel(source); p.hooks.select('Property');
    p.el('entSelector').value = 'bps_group_dog'; await p.el('entSelector').fire('change');
    await p.el('starttrack').fire('click'); p.el('circleToggle').checked = true;
    assert.equal(p.el('trackingDistanceMode').value, 'both'); p.canvasCalls.length = 0;
    p.network.cords = [{ent:'beacon_a',floor:'Property',cords:[45,60],radii:[[0,0,120]]},
        {ent:'beacon_b',floor:'Property',cords:[35,60],radii:[[0,0,140]]},
        {ent:'bps_group_dog',group:true,floor:'Property',cords:[40,60],beacon_positions:[
            {ent:'beacon_a',floor:'Property'},{ent:'beacon_b',floor:'Property'}]}];
    await p.intervals.findLast(i => i.ms === 500).cb();
    const atReceiver = () => p.canvasCalls.filter(call => call[0] === 'arc' && call[1] === 0 && call[2] === 0).map(call=>call[3]);
    assert.deepEqual(atReceiver(),[120,140,Math.hypot(40,60)]);
    assert.ok(p.canvasCalls.some(call=>JSON.stringify(call) === '["dash",10,7]'));
    assert.ok(p.canvasCalls.some(call=>JSON.stringify(call) === '["dash"]'));
    assert.ok(p.canvasCalls.some(call=>call[0] === 'text' && call[1] === 'BLE 6.0 m'));
    assert.ok(p.canvasCalls.some(call=>call[0] === 'text' && call[1] === 'Est 3.6 m'));
    await p.el('gridToggle').fire('click'); p.canvasCalls.length=0; await p.el('gridToggle').fire('click');
    assert.ok(p.canvasCalls.some(call=>call[0] === 'text' && call[1] === 'BLE 19.7 ft'));
    assert.ok(p.canvasCalls.some(call=>call[0] === 'text' && call[1] === 'Est 11.8 ft'));
    p.el('trackingDistanceMode').value='estimated'; p.canvasCalls.length=0; await p.el('trackingDistanceMode').fire('change');
    assert.deepEqual(atReceiver(),[Math.hypot(40,60)]);
    await p.el('diagnosticCopyAll').fire('click');
    const report = JSON.parse(p.el('diagnosticReportText').value.split('\n').slice(1).join('\n'));
    assert.deepEqual(plain(report.panel_context.distance_display),{enabled:true,mode:'estimated',unit:'ft'});
    assert.equal(report.panel_context.displayed_positions[0].displayed_receiver_radii.length,1);
    p.network.cords[2].outdoor={detection_only:true,direction_known:false}; p.canvasCalls.length=0;
    await p.intervals.findLast(i => i.ms === 500).cb();
    assert.deepEqual(atReceiver(),[], 'a one-receiver detection cannot claim an estimated direction');
});

test('panel marks outside-map positions at the edge without status captions or moving raw ranges', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    const p = await panel(source); p.hooks.select('Property');
    p.el('entSelector').value = 'beacon_a'; await p.el('entSelector').fire('change');
    await p.el('starttrack').fire('click'); p.setClock(100000); p.canvasCalls.length = 0;
    const row = {ent: 'beacon_a', floor: 'Property', cords: [2200, 500], updated: 100,
        radii: [[100, 100, 90]], outdoor: {estimated_uncertainty_m: 8, observed: 100}};
    p.network.cords = [row]; await p.intervals.findLast(i => i.ms === 500).cb();
    assert.equal(p.hooks.tracks().get('beacon_a').x, 2200);
    assert.ok(p.canvasCalls.some(c => c[0] === 'arc' && c[1] === 2200 && c[3] === 160), 'uncertainty stays at raw position');
    assert.ok(p.canvasCalls.some(c => c[0] === 'arc' && c[1] < 2000 && c[1] > 1900), 'edge marker remains inside map');
    assert.deepEqual(p.canvasCalls.filter(c => c[0] === 'text').map(c => c[1]), ['beacon_a']);
    assert.equal(p.hooks.hitDeviceAt({x: 1954, y: 500}), 'beacon_a', 'visible edge marker is selectable');
    assert.equal(p.hooks.hitDeviceAt({x: 2200, y: 500}), null, 'raw coordinates are not an invisible hit target');
    row.cords = [100, 100]; row.outdoor.detection_only = true; row.outdoor.direction_known = false;
    p.canvasCalls.length = 0; await p.intervals.findLast(i => i.ms === 500).cb();
    assert.deepEqual(p.canvasCalls.filter(c => c[0] === 'text').map(c => c[1]), ['beacon_a']);
});

test('full report includes selected map and unsaved placements separately without images or auth tokens', async () => {
    const source = layout();
    source.floor[0].receivers = [{entity_id: 'proxy', cords: {x: 100, y: 200}, height: 2}];
    source.tracker_icons = {beacon: 'data:image/private'};
    const p = await panel(source); p.hooks.select('Property');
    p.hooks.layout().floor[0].receivers[0].cords.x = 150;
    await p.el('diagnosticCopyAll').fire('click');
    const text = p.el('diagnosticReportText').value;
    const editor = JSON.parse(text.slice(text.indexOf('{'))).panel_context;
    assert.equal(editor.selected_floor, 'Property');
    assert.equal(editor.image_size_px.width, 2000);
    assert.equal(editor.editor_layout.floor[0].receivers[0].cords.x, 150);
    assert.ok(!text.includes('data:image/private') && !text.includes('test-token'));
    assert.ok(!p.requests.some(r => r.url === '/api/bps/save_text'));
    assert.equal(p.el('diagnosticReportText').selected, true);
});

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
        updated: 1000, outdoor: {observed: 1000, detection_only: true, direction_known: false}, beacon_positions: [beaconA, beaconB,
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
    assert.deepEqual(p.canvasCalls.filter(c => c[0] === 'text').map(c => c[1]), ['1', '2', 'Rover']);
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

test('visible fence tool saves open paths, edits material/thickness, cancels, reloads and deletes', async () => {
    const p = await panel(layout()); p.hooks.select('Property');
    await p.el('addFence').fire('click');
    assert.equal(p.hooks.layout().outdoor_tracking.enabled, true);
    assert.equal(p.el('environmentType').value, 'fence');
    assert.equal(p.el('environmentFenceControls').hidden, false);
    assert.equal(p.el('environmentWalls').hidden, true);
    assert.equal(p.el('environmentThickness').value, 'solid');
    p.hooks.setPoints(points.slice(0, 1)); p.el('zoneName').value = 'Fence';
    assert.equal(p.hooks.finalizeShape(), false);
    assert.equal(p.hooks.layout().floor[0].environment, undefined);
    const path = points.slice(0, 3);
    p.hooks.setPoints(path); p.el('environmentMaterial').value = 'metal'; p.el('environmentThickness').value = 'thin';
    p.canvasCalls.length = 0; p.hooks.drawZonePreview();
    const start = p.canvasCalls.findLastIndex(c => c[0] === 'moveTo' && c[1] === 100 && c[2] === 100);
    const handles = p.canvasCalls.findIndex((c, i) => i > start && c[0] === 'arc');
    assert.equal(p.canvasCalls.slice(start, handles).some(c => c[0] === 'fill' || c[0] === 'closePath'), false);
    assert.equal(p.hooks.finalizeShape(), true);
    const fence = p.hooks.layout().floor[0].environment[0];
    assert.deepEqual(plain(fence.points), path);
    assert.equal(fence.material, 'metal'); assert.equal(fence.thickness, 'thin');
    p.hooks.cancelShapeEdit();
    p.canvasCalls.length = 0; p.hooks.drawEnvironmentPolygons(p.hooks.layout().floor[0]);
    assert.equal(p.canvasCalls.some(c => c[0] === 'fill' || c[0] === 'closePath'), false);
    assert.equal(p.el('environmentList').children[0].children[0].disabled, false);
    await p.el('environmentList').children[0].children[0].fire('click');
    assert.equal(p.el('environmentMaterial').value, 'metal'); assert.equal(p.el('environmentThickness').value, 'thin');
    p.hooks.setPoints(path.map(point => ({x: point.x + 30, y: point.y}))); p.el('environmentThickness').value = 'solid';
    p.hooks.cancelShapeEdit(); assert.deepEqual(plain(fence.points), path); assert.equal(fence.thickness, 'thin');
    p.hooks.beginEnvironmentEdit(fence); p.el('environmentMaterial').value = 'wood'; p.el('environmentThickness').value = 'solid';
    p.hooks.setPoints(path.slice(0, 2)); assert.equal(p.hooks.finalizeShape(), true);
    p.hooks.cancelShapeEdit(); await p.hooks.savedata(true);
    const saved = JSON.parse(p.requests.find(r => r.url === '/api/bps/save_text').options.body.get('coordinates'));
    assert.equal(saved.floor[0].environment[0].material, 'wood'); assert.equal(saved.floor[0].environment[0].thickness, 'solid');
    assert.deepEqual(saved.floor[0].environment[0].points, path.slice(0, 2));
    const loaded = await panel(saved); loaded.hooks.select('Property');
    loaded.hooks.beginEnvironmentEdit(saved.floor[0].environment[0]); loaded.hooks.cancelShapeEdit();
    assert.deepEqual(plain(loaded.hooks.layout().floor[0].environment), saved.floor[0].environment);
    await loaded.el('environmentList').children[0].children[1].fire('click');
    assert.deepEqual(plain(loaded.hooks.layout().floor[0].environment), []);
});

test('fence editing drags segments, adds outside points and rejects malformed edits without losing saved data', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    const fence = {id: 'fence', name: 'Fence', type: 'fence', material: 'metal', thickness: 'thin',
        points: [{x: 100, y: 100}, {x: 300, y: 100}]};
    source.floor[0].environment = [fence];
    const p = await panel(source); p.hooks.select('Property'); p.hooks.beginEnvironmentEdit(fence);
    await p.el('canvas').fire('mousedown', {button: 0, clientX: 200, clientY: 100});
    await p.el('canvas').fire('mousemove', {clientX: 220, clientY: 120});
    await p.el('canvas').fire('mouseup'); assert.equal(p.hooks.finalizeShape(), true);
    assert.deepEqual(plain(p.hooks.layout().floor[0].environment[0].points), [{x: 120, y: 120}, {x: 320, y: 120}]);
    const before = JSON.stringify(p.hooks.layout().floor[0].environment);
    p.hooks.setPoints([{x: 0, y: 0}, {x: 100, y: 100}, {x: 0, y: 100}, {x: 100, y: 0}]);
    assert.equal(p.hooks.finalizeShape(), false); assert.equal(JSON.stringify(p.hooks.layout().floor[0].environment), before);
    p.hooks.cancelShapeEdit();
    p.hooks.beginEnvironmentEdit({...fence, points: null}); assert.equal(p.hooks.editing(), null);
    p.hooks.beginEnvironmentEdit(p.hooks.layout().floor[0].environment[0]);
    await p.el('canvas').fire('mousedown', {button: 0, clientX: 500, clientY: 200});
    await p.el('canvas').fire('mouseup'); assert.equal(p.hooks.finalizeShape(), true);
    assert.deepEqual(plain(p.hooks.layout().floor[0].environment[0].points.at(-1)), {x: 500, y: 200});
});

test('map tools are outside the opt-in controls in the actual document', () => {
    const html = readFileSync(join(directory, 'index.html'), 'utf8');
    const controls = html.indexOf('id="outdoorControls"');
    for (const id of ['addBuilding', 'addTrees', 'addFence', 'openGroupEditor']) {
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

test('calibration summarizes material factor changes and collapses the complete matrix', async () => {
    const source = layout(); source.floor[0].receivers = [
        {entity_id: 'unchanged', correction: 2}, {entity_id: 'minor', correction: 1},
        {entity_id: '<changed>', correction: .5}];
    const p = await panel(source); p.hooks.select('Property');
    p.network.calibration = {mode: 'manual', results: {Property: {floor: 'Property',
        receivers: {unchanged: 2, minor: 1.01, '<changed>': 1}, low_confidence: ['<changed>'], matrix: []}}};
    p.el('calibAuto').checked = false; await p.el('calibAuto').fire('change');
    const html = p.el('calibResults').innerHTML;
    assert.match(html, /1 proposed receiver changes of at least 5%/);
    assert.match(html, /×0.5 → ×1/); assert.match(html, /\+100.0%/);
    assert.match(html, /Apply corrections applies the full solution/);
    assert.match(html, /<details><summary>Show full probe matrix and all factors/);
    assert.doesNotMatch(html, /<details open/);
    assert.doesNotMatch(html, /<changed>/); assert.match(html, /&lt;changed&gt;/);
});

test('obstruction strength is an explicit saved setting and rejects invalid values atomically', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true};
    const p = await panel(source); p.hooks.select('Property');
    assert.equal(Number(p.el('outdoorObstructionStrength').value), 1);
    p.el('outdoorObstructionStrength').value = '2'; await p.el('outdoorObstructionStrength').fire('change');
    assert.equal(p.hooks.layout().outdoor_tracking.obstruction_strength, 2);
    p.el('outdoorObstructionStrength').value = '5'; await p.el('outdoorObstructionStrength').fire('change');
    assert.equal(p.hooks.layout().outdoor_tracking.obstruction_strength, 2);
    assert.equal(Number(p.el('outdoorObstructionStrength').value), 2);
    await p.hooks.savedata(true);
    const sent = p.requests.find(r => r.url === '/api/bps/save_text');
    assert.equal(JSON.parse(sent.options.body.get('coordinates')).outdoor_tracking.obstruction_strength, 2);
});

test('fractional obstruction strength stays selectable through unrelated edits and saves', async () => {
    const source = layout(); source.outdoor_tracking = {enabled: true, obstruction_strength: 1.5};
    const p = await panel(source); p.hooks.select('Property');
    const strength = p.el('outdoorObstructionStrength');
    assert.equal(strength.value, '1.5');
    assert.equal(strength.options.find(o => o.value === strength.value)?.textContent, 'Custom (1.5)');
    p.el('outdoorEnabled').checked = false; await p.el('outdoorEnabled').fire('change');
    assert.equal(p.hooks.layout().outdoor_tracking.enabled, false);
    p.el('outdoorUncertainty').checked = false; await p.el('outdoorUncertainty').fire('change');
    assert.equal(p.hooks.layout().outdoor_tracking.obstruction_strength, 1.5);
    assert.equal(strength.options.filter(o => o.dataset.bpsCustomStrength === 'true').length, 1);
    strength.value = '0'; await strength.fire('change');
    assert.equal(strength.value, '1.5');
    await p.hooks.savedata(true);
    const sent = p.requests.find(r => r.url === '/api/bps/save_text');
    assert.equal(JSON.parse(sent.options.body.get('coordinates')).outdoor_tracking.obstruction_strength, 1.5);
    strength.value = '2'; await strength.fire('change');
    assert.equal(strength.options.filter(o => o.dataset.bpsCustomStrength === 'true').length, 0);
});

test('known-position map picking converts world pixels without moving receivers or dirtying the layout', async () => {
    const source = layout(); source.floor[0].receivers = [{entity_id: 'proxy', cords: {x: 100, y: 100}}];
    const p = await panel(source); p.hooks.select('Property'); const before = plain(p.hooks.layout());
    p.el('diagnosticSnapshotFloor').value = 'Property';
    await p.el('diagnosticSnapshotPick').fire('click');
    await p.el('canvas').fire('mousedown', {clientX: 200, clientY: 400, button: 0});
    await p.el('canvas').fire('click', {clientX: 200, clientY: 400, button: 0});
    assert.equal(p.el('diagnosticSnapshotX').value, '10.000'); assert.equal(p.el('diagnosticSnapshotY').value, '20.000');
    assert.deepEqual(plain(p.hooks.layout()), before);
    assert.equal(p.requests.some(r => r.url === '/api/bps/save_text'), false);
    await p.el('diagnosticSnapshotPick').fire('click'); p.listeners.get('keydown')({key: 'Escape'});
    await p.el('canvas').fire('click', {clientX: 400, clientY: 200, button: 0});
    assert.equal(p.el('diagnosticSnapshotX').value, '10.000');
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

const matchingFixture = () => ({read_only: true, revision: 'inventory-1', options: [
    {entity_id: 'workshop_112233', name: 'Workshop', scanner_mac: 'aa:bb:cc:11:22:33', token: '112233', label: 'Workshop · workshop_112233 · aa:bb:cc:11:22:33', selectable: true},
    {entity_id: 'atrium_445566', name: 'Atrium', scanner_mac: 'aa:bb:cc:44:55:66', token: '445566', label: 'Atrium · atrium_445566 · aa:bb:cc:44:55:66', selectable: true},
    {entity_id: 'ambiguous', name: 'Ambiguous', selectable: false, label: 'Ambiguous'},
]});
const flushMatching = () => new Promise(resolve => setImmediate(resolve));
async function receiverMatch(p, index = 0) {
    const rec = p.hooks.layout().floor.find(f => f.name === 'Property').receivers[index];
    const button = {getAttribute: key => key === 'data-index' ? String(index) : rec.entity_id};
    p.listeners.get('click')({target: {closest: selector => selector === '[data-type="matchrec"]' ? button : null}});
    await flushMatching();
    const found = p.document.body.children.findLast(e => e.className === 'bps-modal-overlay');
    assert.ok(found, JSON.stringify({requests: p.requests.map(r => r.url), children: p.document.body.children.map(e => ({className: e.className, children: e.children.map(c => c.textContent)}))}));
    const dialog = found.children[0];
    return {select: dialog.children[2], hint: dialog.children[3], error: dialog.children[4], cancel: dialog.children[5].children[0], apply: dialog.children[5].children[1]};
}

test('receiver match chooser uses Bermuda labels and authenticated reads; cancel is atomic and Apply preserves placement metadata and other floors', async () => {
    const source = layout();
    source.floor[0].receivers = [{entity_id: 'old_112233', cords: {x: 10, y: 20}, height: 2.5, height_unit: 'ft', correction: 1.3,
        hardware_model: 'Synthetic station', antenna_notes: 'Synthetic antenna', distance_accuracy_m: 2, outdoor_policy: 'auto', custom_metadata: {keep: true}}];
    source.floor.push({...source.floor[0], name: 'Upstairs', receivers: JSON.parse(JSON.stringify(source.floor[0].receivers))});
    const p = await panel(source, [], false, matchingFixture()); p.hooks.select('Property');
    const before = plain(p.hooks.layout());
    let dialog = await receiverMatch(p);
    assert.equal(dialog.select.value, '', 'hardware suggestion never auto-selects');
    assert.match(dialog.select.children[1].textContent, /Workshop.*112233.*aa:bb/);
    assert.match(dialog.hint.textContent, /Hardware match/);
    dialog.select.value = 'workshop_112233'; await dialog.cancel.fire('click');
    assert.deepEqual(plain(p.hooks.layout()), before);
    dialog = await receiverMatch(p); dialog.select.value = 'workshop_112233'; await dialog.apply.fire('click');
    const after = plain(p.hooks.layout());
    assert.equal(after.floor[0].receivers[0].entity_id, 'workshop_112233');
    assert.equal(after.floor[0].receivers[0].scanner_uid, '112233');
    assert.equal(after.floor[0].receivers[0].scanner_mac, 'aa:bb:cc:11:22:33');
    const metadata = {...after.floor[0].receivers[0]}; delete metadata.scanner_uid; delete metadata.scanner_mac; metadata.entity_id = 'old_112233';
    assert.deepEqual(metadata, before.floor[0].receivers[0]);
    assert.deepEqual(after.floor[1], before.floor[1]);
    assert.equal(p.requests.filter(r => r.url === '/api/bps/save_text').length, 0);
    for (const request of p.requests.filter(r => r.url === '/api/bps/scanner_linking')) {
        assert.equal(request.options.headers.Authorization, 'Bearer test-token');
    }
    await p.hooks.savedata(true);
    const saved = JSON.parse(p.requests.find(r => r.url === '/api/bps/save_text').options.body.get('coordinates'));
    assert.deepEqual(saved.floor[0].receivers[0], after.floor[0].receivers[0]);
});

test('receiver matcher rejects forged, ambiguous, duplicate and stale choices, and permits distinct co-located scanners and same scanner on other floors', async () => {
    const source = layout();
    source.floor[0].receivers = [{entity_id: 'old_112233', cords: {x: 10, y: 20}}, {entity_id: 'workshop_112233', cords: {x: 10, y: 20}}];
    source.floor.push({name: 'Upstairs', scale: 10, zones: [], receivers: [{entity_id: 'atrium_445566', cords: {x: 2, y: 3}}]});
    const p = await panel(source, [], false, matchingFixture()); p.hooks.select('Property');
    const before = plain(p.hooks.layout());
    let dialog = await receiverMatch(p);
    for (const invalid of ['forged', 'ambiguous']) {
        dialog.select.value = invalid; await dialog.apply.fire('click'); assert.match(dialog.error.textContent, /known Bermuda/);
        assert.deepEqual(plain(p.hooks.layout()), before);
    }
    dialog.select.value = 'workshop_112233'; await dialog.apply.fire('click');
    assert.match(dialog.error.textContent, /already bound/); assert.deepEqual(plain(p.hooks.layout()), before);
    p.network.matching = {...p.network.matching, revision: 'inventory-2'};
    dialog.select.value = 'atrium_445566'; await dialog.apply.fire('click');
    assert.match(dialog.error.textContent, /inventory changed/); assert.deepEqual(plain(p.hooks.layout()), before);
    await dialog.cancel.fire('click');
    dialog = await receiverMatch(p); dialog.select.value = 'atrium_445566'; await dialog.apply.fire('click');
    assert.equal(p.hooks.layout().floor[0].receivers[0].entity_id, 'atrium_445566');
    assert.equal(p.hooks.layout().floor[0].receivers.length, 2, 'co-located distinct scanners retained');
    assert.deepEqual(plain(p.hooks.layout().floor[1]), before.floor[1]);
});

test('receiver match rejects changed floors/placements and cancellation while freshness request is pending cannot mutate', async () => {
    const source = layout(); source.floor[0].receivers = [{entity_id: 'old_112233', cords: {x: 1, y: 2}, correction: 1.4}];
    const p = await panel(source, [], false, matchingFixture()); p.hooks.select('Property');
    let dialog = await receiverMatch(p); dialog.select.value = 'workshop_112233';
    p.hooks.layout().floor[0].receivers[0].height = 3;
    await dialog.apply.fire('click'); assert.match(dialog.error.textContent, /placement changed/);
    await dialog.cancel.fire('click');
    dialog = await receiverMatch(p); dialog.select.value = 'workshop_112233';
    p.hooks.select('Upstairs'); await dialog.apply.fire('click'); assert.match(dialog.error.textContent, /floor or placement changed/);
    await dialog.cancel.fire('click'); p.hooks.select('Property');
    dialog = await receiverMatch(p); dialog.select.value = 'workshop_112233';
    let resolveRequest;
    p.network.matchingRequest = () => new Promise(resolve => {resolveRequest = resolve;});
    const before = plain(p.hooks.layout()); const pending = dialog.apply.fire('click'); await flushMatching();
    await dialog.cancel.fire('click'); resolveRequest({ok: true, status: 200, json: async () => ({matching: p.network.matching})});
    await pending; assert.deepEqual(plain(p.hooks.layout()), before);
});

test('distinct full scanner MACs sharing a token can be co-located and contradictory MACs never produce a hardware hint', async () => {
    const matching = matchingFixture();
    matching.options.push({entity_id: 'courtyard_112233', name: 'Courtyard', scanner_mac: 'dd:ee:ff:11:22:33', token: '112233', selectable: true, label: 'Courtyard · courtyard_112233 · dd:ee:ff:11:22:33'});
    const source = layout(); source.floor[0].receivers = [
        {entity_id: 'old_112233', scanner_mac: '00:00:00:11:22:33', scanner_uid: '112233', cords: {x: 10, y: 20}},
        {entity_id: 'workshop_112233', scanner_mac: 'aa:bb:cc:11:22:33', scanner_uid: '112233', cords: {x: 10, y: 20}},
    ];
    const p = await panel(source, [], false, matching); p.hooks.select('Property');
    const dialog = await receiverMatch(p); assert.match(dialog.hint.textContent, /No unique hardware match/);
    dialog.select.value = 'courtyard_112233'; await dialog.apply.fire('click');
    assert.equal(p.hooks.layout().floor[0].receivers[0].entity_id, 'courtyard_112233');
    assert.equal(p.hooks.layout().floor[0].receivers[0].scanner_mac, 'dd:ee:ff:11:22:33');
    assert.equal(p.hooks.layout().floor[0].receivers.length, 2);
});

test('same-floor scanner aliases with proven identical MAC cannot be bound twice', async () => {
    const matching = matchingFixture(); matching.options.push({...matching.options[0], entity_id: 'renamed_workshop', label: 'Workshop renamed'});
    const source = layout(); source.floor[0].receivers = [
        {entity_id: 'old', cords: {x: 10, y: 20}}, {entity_id: 'workshop_112233', cords: {x: 20, y: 30}},
    ];
    const p = await panel(source, [], false, matching); p.hooks.select('Property');
    const before = plain(p.hooks.layout()); const dialog = await receiverMatch(p);
    dialog.select.value = 'renamed_workshop'; await dialog.apply.fire('click');
    assert.match(dialog.error.textContent, /already bound/); assert.deepEqual(plain(p.hooks.layout()), before);
    await dialog.cancel.fire('click');
});

test('applying an existing scanner slug repairs stored hardware identity without changing placement properties', async () => {
    const source = layout(); source.floor[0].receivers = [{entity_id: 'workshop_112233', scanner_uid: 'obsolete',
        scanner_mac: 'dd:ee:ff:11:22:33', cords: {x: 10, y: 20}, height: 2, correction: 1.2, hardware_model: 'Synthetic'}];
    const p = await panel(source, [], false, matchingFixture()); p.hooks.select('Property');
    const before = plain(p.hooks.layout().floor[0].receivers[0]);
    const dialog = await receiverMatch(p); dialog.select.value = 'workshop_112233'; await dialog.apply.fire('click');
    const after = plain(p.hooks.layout().floor[0].receivers[0]);
    assert.deepEqual(after, {...before, scanner_uid: '112233', scanner_mac: 'aa:bb:cc:11:22:33'});
    await p.hooks.savedata(true);
    const saved = JSON.parse(p.requests.find(r => r.url === '/api/bps/save_text').options.body.get('coordinates'));
    assert.deepEqual(saved.floor[0].receivers[0], after);
});

test('real receiver chooser persists full MAC and config owner only on explicit Save', async () => {
    const matching = matchingFixture(); matching.options[0].config_entry_ids = ['synthetic-owner'];
    const source = layout(); source.floor[0].receivers = [{entity_id: 'old_station', cords: {x: 10, y: 20},
        height: 2, correction: 1.2, hardware_model: 'Synthetic', custom_metadata: {keep: true}}];
    const before = plain(source.floor[0].receivers[0]);
    const p = await panel(source, [], false, matching); p.hooks.select('Property');
    const dialog = await receiverMatch(p); dialog.select.value = 'workshop_112233'; await dialog.apply.fire('click');
    const matched = plain(p.hooks.layout().floor[0].receivers[0]);
    assert.deepEqual(matched, {...before, entity_id: 'workshop_112233', scanner_uid: '112233',
        scanner_mac: 'aa:bb:cc:11:22:33', scanner_config_entry_id: 'synthetic-owner'});
    assert.equal(p.requests.filter(r => r.url === '/api/bps/save_text').length, 0);
    await p.hooks.savedata(true);
    const save = p.requests.find(r => r.url === '/api/bps/save_text');
    assert.equal(save.options.headers.Authorization, 'Bearer test-token');
    const saved = JSON.parse(save.options.body.get('coordinates'));
    assert.deepEqual(saved.floor[0].receivers[0], matched);
    const reopened = await panel(saved, [], false, matching);
    assert.equal(reopened.hooks.layout().floor[0].receivers[0].scanner_config_entry_id, 'synthetic-owner');
    assert.equal(reopened.hooks.layout().floor[0].receivers[0].scanner_mac, 'aa:bb:cc:11:22:33');
});

test('proven saved aliases render as known and conflicting placements show actionable diagnostics', async () => {
    const matching = matchingFixture();
    matching.scanner_diagnostics = {alias_receivers: [{entity_id: 'old_station', scanner_mac: 'aa:bb:cc:11:22:33',
        config_entry_ids: ['synthetic-owner'], aliases: ['workshop_112233']}], placement_issues: [{floor: 'Property',
        placement_index: 1, entity_id: 'workshop_112233', status: 'conflicting_scanner_placements',
        action: 'Keep one placement for this physical scanner on this floor, or match the other placement to a different scanner.'}]};
    const source = layout(); source.floor[0].receivers = [
        {entity_id: 'old_station', scanner_mac: 'aa:bb:cc:11:22:33', scanner_config_entry_id: 'synthetic-owner', cords: {x: 10, y: 20}},
        {entity_id: 'workshop_112233', cords: {x: 30, y: 40}},
    ];
    const before = plain(source); const p = await panel(source, [], false, matching);
    p.hooks.select('Property'); p.hooks.redrawAll();
    assert.match(p.el('scannerissues').innerHTML, /Receiver matching issues/);
    assert.match(p.el('scannerissues').innerHTML, /Keep one placement/);
    assert.doesNotMatch(p.el('scannerissues').innerHTML, /No known Bermuda identity/);
    assert.deepEqual(plain(p.hooks.layout().floor), before.floor);
    assert.equal(p.requests.filter(r => r.url === '/api/bps/save_text').length, 0);
});

test('Place Receiver hides physical aliases on this floor and Save rejects a custom alias duplicate', async () => {
    for (const storedIdentity of [false, true]) {
        const matching = matchingFixture(); matching.options[0].config_entry_ids = ['owner-a'];
        matching.options.push({...matching.options[0], entity_id: 'old_station'});
        const source = layout(); source.floor[0].receivers = [{entity_id: 'old_station', cords: {x: 100, y: 100}}];
        if (storedIdentity) {
            source.floor[0].receivers[0].scanner_mac = matching.options[0].scanner_mac;
            source.floor[0].receivers[0].scanner_config_entry_id = 'owner-a';
            matching.options.pop(); // old registry alias was removed
        }
        const p = await panel(source, [], false, matching); p.hooks.select('Property');
        await p.hooks.addDeviceButton.fire('click'); await p.el('canvas').fire('click', {clientX: 400, clientY: 400});
        assert.ok(!p.el('receiverName').options.some(o => o.value === 'workshop_112233'));
        assert.ok(p.el('receiverName').options.some(o => o.value === 'atrium_445566'), 'distinct scanner remains available');
        p.el('receiverName').value = '__custom__'; p.el('receiverCustomName').value = 'workshop_112233';
        const before = plain(p.hooks.layout()); await p.hooks.saveReceiver();
        assert.deepEqual(plain(p.hooks.layout()), before, 'custom entry cannot bypass physical duplicate guard');
        assert.equal(p.requests.filter(r => r.url === '/api/bps/save_text').length, 0);
    }
});

test('Place Receiver allows another floor and preserves proven identity for a new placement', async () => {
    const matching = matchingFixture(); matching.options[0].config_entry_ids = ['owner-a'];
    const source = layout(); source.floor[0].receivers = [{entity_id: 'workshop_112233', cords: {x: 100, y: 100}}];
    source.floor.push({name: 'Upstairs', scale: 20, zones: [], receivers: []});
    const p = await panel(source, [], false, matching); p.hooks.select('Upstairs');
    await p.hooks.addDeviceButton.fire('click'); await p.el('canvas').fire('click', {clientX: 400, clientY: 400});
    assert.ok(p.el('receiverName').options.some(o => o.value === 'workshop_112233'));
    p.el('receiverName').value = 'workshop_112233'; await p.hooks.saveReceiver();
    const rec = p.hooks.layout().floor[1].receivers[0];
    assert.equal(rec.scanner_mac, matching.options[0].scanner_mac);
    assert.equal(rec.scanner_config_entry_id, 'owner-a');
    await p.hooks.savedata(true);
    const saved = JSON.parse(p.requests.find(r => r.url === '/api/bps/save_text').options.body.get('coordinates'));
    assert.equal(saved.floor[1].receivers[0].scanner_config_entry_id, 'owner-a');
});

test('Place Receiver normalizes a proven saved BLE binding when hiding and rejecting physical duplicates', async () => {
    for (const owned of [false, true]) {
        const matching = matchingFixture(); matching.options[0].config_entry_ids = ['owner-a'];
        const ble = 'dd:ee:ff:90:80:00';
        matching.runtime_address_map = [{config_entry_id: 'owner-a', scanner_mac: matching.options[0].scanner_mac, scanner_ble_mac: ble}];
        const source = layout(); source.floor[0].receivers = [{entity_id: 'removed_old_station', scanner_mac: ble,
            cords: {x: 100, y: 100}, height: 2, correction: 1.3}];
        if (owned) source.floor[0].receivers[0].scanner_config_entry_id = 'owner-a';
        const p = await panel(source, [], false, matching); p.hooks.select('Property');
        await p.hooks.addDeviceButton.fire('click'); await p.el('canvas').fire('click', {clientX: 400, clientY: 400});
        assert.ok(!p.el('receiverName').options.some(o => o.value === 'workshop_112233'));
        p.el('receiverName').value = '__custom__'; p.el('receiverCustomName').value = 'workshop_112233';
        const before = plain(p.hooks.layout()); await p.hooks.saveReceiver();
        assert.deepEqual(plain(p.hooks.layout()), before);
        assert.equal(p.requests.filter(r => r.url === '/api/bps/save_text').length, 0);
    }
});

test('receiver matching blocks a canonical alias already placed using its proven Bluetooth binding', async () => {
    const matching = matchingFixture(); matching.options[0].config_entry_ids = ['owner-a'];
    const ble = 'dd:ee:ff:90:80:00';
    matching.runtime_address_map = [{config_entry_id: 'owner-a', scanner_mac: matching.options[0].scanner_mac, scanner_ble_mac: ble}];
    const source = layout(); source.floor[0].receivers = [
        {entity_id: 'unmatched', cords: {x: 10, y: 20}},
        {entity_id: 'removed_old_station', scanner_mac: ble, scanner_config_entry_id: 'owner-a', cords: {x: 100, y: 100}},
    ];
    const p = await panel(source, [], false, matching); p.hooks.select('Property');
    const before = plain(p.hooks.layout()), dialog = await receiverMatch(p);
    dialog.select.value = 'workshop_112233'; await dialog.apply.fire('click');
    assert.match(dialog.error.textContent, /already bound/);
    assert.deepEqual(plain(p.hooks.layout()), before);
    await dialog.cancel.fire('click');
});

function sharedSavedMatching() {
    const matching = matchingFixture(); matching.options[0].config_entry_ids = ['owner-a'];
    const mac = matching.options[0].scanner_mac;
    matching.options.push({entity_id: 'shared', name: 'Shared', identity_ambiguous: true, selectable: false,
        scanner_mac: null, config_entry_ids: ['owner-a', 'owner-b'], scanner_identities: [
            {config_entry_id: 'owner-a', scanner_mac: mac},
            {config_entry_id: 'owner-b', scanner_mac: 'dd:ee:ff:44:55:66'}]});
    return matching;
}

test('Place Receiver hides and rejects a unique alias of saved hardware under an ambiguous slug', async () => {
    for (const bluetooth of [false, true]) {
        const matching = sharedSavedMatching(), mac = matching.options[0].scanner_mac, ble = 'dd:ee:ff:90:80:00';
        if (bluetooth) matching.runtime_address_map = [{config_entry_id: 'owner-a', scanner_mac: mac, scanner_ble_mac: ble}];
        const source = layout(); source.floor[0].receivers = [{entity_id: 'shared', scanner_mac: bluetooth ? ble : mac,
            scanner_config_entry_id: 'owner-a', cords: {x: 100, y: 100}}];
        const p = await panel(source, [], false, matching); p.hooks.select('Property');
        await p.hooks.addDeviceButton.fire('click'); await p.el('canvas').fire('click', {clientX: 400, clientY: 400});
        assert.ok(!p.el('receiverName').options.some(o => o.value === 'workshop_112233'));
        p.el('receiverName').value = '__custom__'; p.el('receiverCustomName').value = 'workshop_112233';
        const before = plain(p.hooks.layout()); await p.hooks.saveReceiver();
        assert.deepEqual(plain(p.hooks.layout()), before);
        assert.match(placementToasts(p), /already placed/);
    }
});

test('Match rejects a unique alias already bound by saved hardware under an ambiguous slug', async () => {
    const matching = sharedSavedMatching(), mac = matching.options[0].scanner_mac;
    const source = layout(); source.floor[0].receivers = [
        {entity_id: 'unmatched', cords: {x: 10, y: 20}},
        {entity_id: 'shared', scanner_mac: mac, scanner_config_entry_id: 'owner-a', cords: {x: 100, y: 100}}];
    const p = await panel(source, [], false, matching); p.hooks.select('Property');
    const before = plain(p.hooks.layout()), dialog = await receiverMatch(p);
    dialog.select.value = 'workshop_112233'; await dialog.apply.fire('click');
    assert.match(dialog.error.textContent, /already bound/);
    assert.deepEqual(plain(p.hooks.layout()), before);
    await dialog.cancel.fire('click');
});

test('ambiguous saved slug does not block the same MAC proven to a distinct Bermuda owner', async () => {
    const matching = sharedSavedMatching(), mac = matching.options[0].scanner_mac;
    matching.options.find(o => o.entity_id === 'shared').scanner_identities[1].scanner_mac = mac;
    matching.options.push({...matching.options[0], entity_id: 'owner_b_alias', config_entry_ids: ['owner-b']});
    const source = layout(); source.floor[0].receivers = [{entity_id: 'shared', scanner_mac: mac,
        scanner_config_entry_id: 'owner-a', cords: {x: 100, y: 100}}];
    const p = await panel(source, [], false, matching); p.hooks.select('Property');
    await p.hooks.addDeviceButton.fire('click'); await p.el('canvas').fire('click', {clientX: 400, clientY: 400});
    assert.ok(p.el('receiverName').options.some(o => o.value === 'owner_b_alias'));
    p.el('receiverName').value = 'owner_b_alias'; await p.hooks.saveReceiver();
    assert.equal(p.hooks.layout().floor[0].receivers[1].scanner_config_entry_id, 'owner-b');
});

test('proven distinct Bermuda owners with the same MAC can be placed and matched independently', async () => {
    const matching = matchingFixture(); matching.options[0].config_entry_ids = ['owner-a'];
    matching.options.push({...matching.options[0], entity_id: 'second_owner_station', config_entry_ids: ['owner-b']});
    const source = layout(); source.floor[0].receivers = [{entity_id: 'workshop_112233', cords: {x: 100, y: 100}}];
    const p = await panel(source, [], false, matching); p.hooks.select('Property');
    await p.hooks.addDeviceButton.fire('click'); await p.el('canvas').fire('click', {clientX: 400, clientY: 400});
    assert.ok(p.el('receiverName').options.some(o => o.value === 'second_owner_station'));
    p.el('receiverName').value = 'second_owner_station'; await p.hooks.saveReceiver();
    assert.equal(p.hooks.layout().floor[0].receivers.length, 2);
    const dialog = await receiverMatch(p); dialog.select.value = 'workshop_112233'; await dialog.apply.fire('click');
    assert.equal(dialog.error.textContent || '', '');
    assert.equal(p.hooks.layout().floor[0].receivers.length, 2);
});

async function beginPlacement(p, choice = 'workshop_112233') {
    p.hooks.select('Property');
    await p.hooks.addDeviceButton.fire('click');
    await p.el('canvas').fire('click', {clientX: 400, clientY: 400});
    p.el('receiverName').value = choice;
}
const placementToasts = p => p.el('bpsToastHost').children.map(item => item.textContent || '').join('\n');

test('Place Receiver rejects replaced same-slug inventory and explicit retry saves the fresh MAC and owner', async () => {
    const matching = matchingFixture(); matching.options[0].config_entry_ids = ['owner-before'];
    const source = layout(); source.floor[0].receivers = [{entity_id: 'legacy_station', cords: {x: 1, y: 2},
        height: 2, correction: 1.2, custom_metadata: {keep: true}}];
    const p = await panel(source, [], false, matching);
    await beginPlacement(p);
    p.el('receiverSearch').value = 'workshop';
    p.el('receiverCustomName').value = 'draft custom name';
    p.el('receiverHeight').value = '2.5';
    const before = plain(p.hooks.layout());
    const fresh = plain(matching); fresh.revision = 'inventory-2';
    Object.assign(fresh.options[0], {scanner_mac: 'dd:ee:ff:11:22:33', config_entry_ids: ['owner-after'],
        label: 'Workshop replacement · workshop_112233 · dd:ee:ff:11:22:33'});
    p.network.matching = fresh;
    await p.el('saveToolBtn').fire('click');
    assert.deepEqual(plain(p.hooks.layout()), before);
    assert.match(placementToasts(p), /inventory changed/);
    assert.equal(p.hooks.addDeviceButton.dataset.active, 'true');
    assert.equal(p.el('receiverName').value, 'workshop_112233');
    assert.equal(p.el('receiverSearch').value, 'workshop');
    assert.equal(p.el('receiverCustomName').value, 'draft custom name');
    assert.equal(p.el('receiverHeight').value, '2.5');
    assert.match(p.el('receiverName').options.find(o => o.value === 'workshop_112233').textContent, /replacement.*dd:ee/);
    await p.el('saveToolBtn').fire('click');
    const rec = plain(p.hooks.layout().floor[0].receivers[1]);
    assert.deepEqual(rec, {entity_id: 'workshop_112233', cords: {x: 400, y: 400}, height: 2.5,
        scanner_mac: 'dd:ee:ff:11:22:33', scanner_config_entry_id: 'owner-after'});
    assert.deepEqual(plain(p.hooks.layout().floor[0].receivers[0]), before.floor[0].receivers[0]);
    assert.equal(p.requests.filter(r => r.url === '/api/bps/save_text').length, 0);
    for (const request of p.requests.filter(r => r.url === '/api/bps/scanner_linking')) {
        assert.equal(request.options.headers.Authorization, 'Bearer test-token');
    }
    await p.hooks.savedata(true);
    const saved = JSON.parse(p.requests.find(r => r.url === '/api/bps/save_text').options.body.get('coordinates'));
    assert.deepEqual(saved.floor[0].receivers[1], rec);
});

test('Place Receiver uses updated physical alias evidence to block a duplicate after inventory refresh', async () => {
    const matching = matchingFixture(); matching.options[0].config_entry_ids = ['owner-a'];
    const source = layout(); source.floor[0].receivers = [{entity_id: 'legacy_station', cords: {x: 1, y: 2}, correction: 1.4}];
    const p = await panel(source, [], false, matching); await beginPlacement(p);
    const before = plain(p.hooks.layout());
    p.network.matching = {...plain(matching), revision: 'inventory-2',
        options: [...plain(matching.options), {...plain(matching.options[0]), entity_id: 'legacy_station'}]};
    await p.el('saveToolBtn').fire('click');
    assert.deepEqual(plain(p.hooks.layout()), before);
    assert.equal(p.el('receiverName').value, 'workshop_112233');
    assert.equal(p.el('receiverName').options.find(o => o.value === 'workshop_112233').disabled, true);
    await p.el('saveToolBtn').fire('click');
    assert.deepEqual(plain(p.hooks.layout()), before);
    assert.match(placementToasts(p), /already placed/);
    p.el('receiverName').value = '__custom__'; p.el('receiverCustomName').value = 'workshop_112233';
    await p.el('saveToolBtn').fire('click');
    assert.deepEqual(plain(p.hooks.layout()), before, 'custom aliases also use the fresh physical identity');
});

test('Place Receiver never treats a formerly-known unavailable scanner as an unknown custom name', async () => {
    for (const change of ['ambiguous', 'disabled', 'removed']) {
        const matching = matchingFixture();
        const p = await panel(layout(), [], false, matching); await beginPlacement(p, '__custom__');
        p.el('receiverCustomName').value = 'workshop_112233';
        p.network.matching = plain(matching); p.network.matching.revision = 'inventory-2';
        if (change === 'removed') p.network.matching.options.shift();
        else if (change === 'disabled') p.network.matching.options[0].selectable = false;
        else p.network.matching.options[0].identity_ambiguous = true;
        await p.el('saveToolBtn').fire('click');
        assert.equal(p.el('receiverCustomName').value, 'workshop_112233');
        await p.el('saveToolBtn').fire('click');
        assert.deepEqual(plain(p.hooks.layout().floor[0].receivers), [], change);
        assert.match(placementToasts(p), /available Bermuda receiver/);
    }
});

test('Place Receiver preserves unknown custom exact names and mount-height validation', async () => {
    const source = layout(); source.floor[0].receivers = [{entity_id: 'LegacyStation', cords: {x: 1, y: 2}}];
    const p = await panel(source, [], false, matchingFixture()); await beginPlacement(p, '__custom__');
    p.el('receiverCustomName').value = 'legacystation'; p.el('receiverHeight').value = '12';
    await p.el('saveToolBtn').fire('click');
    assert.equal(p.hooks.layout().floor[0].receivers.length, 1);
    p.el('receiverHeight').value = ''; p.el('receiverHeight').validity.badInput = true;
    await p.el('saveToolBtn').fire('click');
    assert.equal(p.hooks.layout().floor[0].receivers.length, 1);
    p.el('receiverHeight').validity.badInput = false;
    await p.el('saveToolBtn').fire('click');
    assert.deepEqual(plain(p.hooks.layout().floor[0].receivers[1]), {entity_id: 'legacystation', cords: {x: 400, y: 400}});
    await beginPlacement(p, '__custom__'); p.el('receiverCustomName').value = 'legacystation';
    await p.el('saveToolBtn').fire('click');
    assert.equal(p.hooks.layout().floor[0].receivers.length, 2, 'exact custom duplicate is blocked');
});

test('Place Receiver cancellation, replacement session and pending input changes cannot commit an obsolete draft', async () => {
    for (const change of ['cancel', 'restart', 'floor', 'marker', 'name', 'height', 'floor-object']) {
        const p = await panel(layout(), [], false, matchingFixture()); await beginPlacement(p);
        let resolveRequest;
        p.network.matchingRequest = () => new Promise(resolve => {resolveRequest = resolve;});
        const before = plain(p.hooks.layout());
        const pending = p.el('saveToolBtn').fire('click'); await flushMatching();
        const requestCount = p.requests.filter(r => r.url === '/api/bps/scanner_linking').length;
        await p.el('saveToolBtn').fire('click');
        assert.equal(p.requests.filter(r => r.url === '/api/bps/scanner_linking').length, requestCount, 'double Save is single-flight');
        if (change === 'cancel' || change === 'restart') await p.el('cancelToolBtn').fire('click');
        if (change === 'restart') await beginPlacement(p);
        if (change === 'floor') p.hooks.select('Upstairs');
        if (change === 'marker') await p.el('canvas').fire('click', {clientX: 500, clientY: 500});
        if (change === 'name') p.el('receiverName').value = 'atrium_445566';
        if (change === 'height') p.el('receiverHeight').value = '3';
        if (change === 'floor-object') p.hooks.layout().floor[0] = plain(p.hooks.layout().floor[0]);
        resolveRequest({ok: true, status: 200, json: async () => ({matching: p.network.matching})});
        await pending;
        assert.deepEqual(plain(p.hooks.layout()), before, change);
        assert.equal(p.requests.filter(r => r.url === '/api/bps/save_text').length, 0);
        if (change === 'name') assert.equal(p.el('receiverName').value, 'atrium_445566');
        if (change === 'height') assert.equal(p.el('receiverHeight').value, '3');
    }
});

test('Place Receiver inventory failures preserve the draft and a fresh retry can commit', async () => {
    for (const response of [{ok: false, status: 503}, {ok: true, json: async () => ({matching: {options: []}})}]) {
        const p = await panel(layout(), [], false, matchingFixture()); await beginPlacement(p);
        p.el('receiverHeight').value = '2';
        p.network.matchingRequest = async () => response;
        await p.el('saveToolBtn').fire('click');
        assert.deepEqual(plain(p.hooks.layout().floor[0].receivers), []);
        assert.equal(p.hooks.addDeviceButton.dataset.active, 'true');
        assert.equal(p.el('receiverName').value, 'workshop_112233');
        assert.equal(p.el('receiverHeight').value, '2');
        p.network.matchingRequest = null;
        await p.el('saveToolBtn').fire('click');
        assert.equal(p.hooks.layout().floor[0].receivers[0].height, 2);
    }
});
