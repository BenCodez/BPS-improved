const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');

function panel() {
    const elements = new Map(), requests = [], timers = new Map(), downloads = [];
    let nextTimer = 1;
    function element(tag = 'div') {
        let value = '';
        return {children: [], disabled: false, textContent: '', events: {},
            get value() {return value || (tag === 'select' && this.children.length ? this.children[0].value : '');},
            set value(v) {value = v;}, appendChild(e) {this.children.push(e);},
            replaceChildren() {this.children = []; value = '';}, remove() {},
            addEventListener(type, cb) {this.events[type] = cb;},
            fire(type = 'click') {return this.events[type]();}, click() {downloads.push({href: this.href, name: this.download});}};
    }
    for (const name of ['Start', 'Stop', 'Download', 'Clear', 'Mark', 'Release', 'Note', 'Refresh', 'Target', 'Duration', 'Floor', 'Status', 'X', 'Y', 'Label']) {
        elements.set('diagnostic' + name, element(['Target', 'Duration', 'Floor'].includes(name) ? 'select' : 'input'));
    }
    elements.get('diagnosticDuration').value = '600';
    const network = {error: null, pending: null,
        status: {active: false, available: false, targets: [], reason: 'Not started', frames: 0, annotations: 0, bytes: 0,
                 trackers: [{key: 'bps_group_dog', name: 'Dog (group)'}], floors: ['Yard']}};
    const el = name => elements.get('diagnostic' + name);
    const sandbox = vm.createContext({document: {getElementById: key => elements.get(key),
        createElement: element, body: element()},
        URL: {createObjectURL: blob => {assert.equal(blob, 'diagnostic-blob'); return 'blob:local';}, revokeObjectURL: url => downloads.push({revoked: url})},
        setInterval: (cb, ms) => {const id = nextTimer++; timers.set(id, {cb, ms}); return id;}, clearInterval: id => timers.delete(id),
        setTimeout: cb => {cb();}});
    vm.runInContext(readFileSync(join(__dirname, '../../custom_components/bps/frontend/diagnostics.js'), 'utf8'), sandbox);
    sandbox.BPSDiagnostics.init(async (url, options) => {
        requests.push({url, options});
        if (network.pending) await network.pending;
        if (network.error) return {ok: false, status: 400, json: async () => ({error: network.error})};
        if (options) {
            const body = JSON.parse(options.body);
            if (body.action === 'start') Object.assign(network.status, {active: true, available: true, targets: body.targets, frames: 1, reason: 'Recording', seconds_left: 600});
            if (body.action === 'stop') Object.assign(network.status, {active: false, reason: 'Stopped'});
            if (body.action === 'clear') Object.assign(network.status, {active: false, available: false, reason: 'Cleared'});
            if (body.action === 'annotate') network.status.annotations++;
        }
        return {ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(network.status)), blob: async () => 'diagnostic-blob'};
    });
    return {el, network, timers, requests, downloads};
}

test('capture is off by default, controls load selectable trackers and poll only while recording', async () => {
    const p = panel();
    assert.equal(p.requests.length, 0); assert.equal(p.timers.size, 0);
    assert.equal(p.el('Start').disabled, true); assert.equal(p.el('Download').disabled, true);
    await p.el('Refresh').fire();
    assert.equal(p.el('Target').value, 'bps_group_dog'); assert.equal(p.el('Floor').value, 'Yard');
    assert.equal(p.el('Start').disabled, false);
    await p.el('Start').fire();
    const body = JSON.parse(p.requests[1].options.body);
    assert.deepEqual(body, {action: 'start', targets: ['bps_group_dog'], duration_s: 600});
    assert.equal(p.el('Target').disabled, true); assert.equal(p.el('Stop').disabled, false);
    assert.equal(p.timers.size, 1); assert.equal([...p.timers.values()][0].ms, 5000);
    await p.el('Stop').fire();
    assert.equal(p.timers.size, 0); assert.equal(p.el('Download').disabled, false);
});

test('known markers require coordinates and clear/note actions preserve their semantics', async () => {
    const p = panel(); await p.el('Refresh').fire(); await p.el('Start').fire();
    await p.el('Mark').fire();
    assert.equal(p.requests.length, 2); assert.match(p.el('Status').textContent, /coordinates/);
    p.el('X').value = '0'; p.el('Y').value = '4.2'; p.el('Label').value = '<shop>';
    await p.el('Mark').fire();
    assert.deepEqual(JSON.parse(p.requests.at(-1).options.body), {action: 'annotate', kind: 'known_position',
        target: 'bps_group_dog', floor: 'Yard', x_m: 0, y_m: 4.2, label: '<shop>'});
    await p.el('Release').fire();
    assert.equal(JSON.parse(p.requests.at(-1).options.body).kind, 'clear_position');
    await p.el('Note').fire();
    assert.equal(JSON.parse(p.requests.at(-1).options.body).kind, 'note');
});

test('download uses the supplied authenticated fetch and releases the temporary local URL', async () => {
    const p = panel(); await p.el('Refresh').fire(); await p.el('Start').fire(); await p.el('Stop').fire();
    await p.el('Download').fire();
    assert.equal(p.requests.at(-1).url, '/api/bps/diagnostics?download=1');
    assert.deepEqual(p.downloads, [{href: 'blob:local', name: 'bps-diagnostics.json'}, {revoked: 'blob:local'}]);
    await p.el('Clear').fire();
    assert.equal(p.el('Download').disabled, true);
});

test('failed requests preserve capture controls and concurrent clicks do not send duplicate starts', async () => {
    const p = panel(); await p.el('Refresh').fire();
    let release; p.network.pending = new Promise(resolve => {release = resolve;});
    const first = p.el('Start').fire();
    await p.el('Start').fire();
    assert.equal(p.requests.length, 2); assert.equal(p.el('Refresh').disabled, true);
    release(); p.network.pending = null; await first;
    p.network.error = 'Marker limit reached';
    await p.el('Note').fire();
    assert.equal(p.el('Status').textContent, 'Marker limit reached');
    assert.equal(p.el('Stop').disabled, false); assert.equal(p.timers.size, 1);
});
