const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');

function panel(panelSnapshot, locationTools = {}) {
    const elements = new Map(), requests = [], timers = new Map(), downloads = [];
    let nextTimer = 1;
    function element(tag = 'div') {
        let value = '';
        return {children: [], disabled: false, textContent: '', events: {}, hidden: true,
            focus() {this.focused = true;}, select() {this.selected = true;},
            get value() {return value || (tag === 'select' && this.children.length ? this.children[0].value : '');},
            set value(v) {value = v;}, appendChild(e) {this.children.push(e);},
            replaceChildren() {this.children = []; value = '';}, remove() {},
            addEventListener(type, cb) {this.events[type] = cb;},
            fire(type = 'click') {return this.events[type]();}, click() {downloads.push({href: this.href, name: this.download});}};
    }
    for (const name of ['CopyAll', 'ReportText', 'ReportStatus', 'ReportPanel', 'SelectReport', 'Start', 'Stop', 'Download', 'Clear', 'Mark', 'Release', 'Note', 'Refresh', 'Target', 'Duration', 'Floor', 'Status', 'X', 'Y', 'Label',
        'SnapshotLoad', 'SnapshotPick', 'SnapshotTarget', 'SnapshotFloor', 'SnapshotX', 'SnapshotY', 'SnapshotAccuracy', 'SnapshotLabel', 'SnapshotCapture', 'SnapshotComparison']) {
        elements.set('diagnostic' + name, element(['Target', 'Duration', 'Floor', 'SnapshotTarget', 'SnapshotFloor'].includes(name) ? 'select' : 'input'));
    }
    elements.get('diagnosticDuration').value = '600';
    elements.get('diagnosticSnapshotAccuracy').value = '1';
    const copied = [];
    const network = {error: null, pending: null, clipboardError: false, report: 'BPS full diagnostics\n{"trackers": ["tag", "tag2"]}',
        status: {active: false, available: false, targets: [], reason: 'Not started', frames: 0, annotations: 0, bytes: 0,
                 trackers: [{key: 'bps_group_dog', name: 'Dog (group)'}], floors: ['Yard']}};
    const el = name => elements.get('diagnostic' + name);
    const sandbox = vm.createContext({document: {getElementById: key => elements.get(key),
        createElement: element, body: element()},
        TextEncoder, URL: {createObjectURL: blob => {assert.equal(blob, 'diagnostic-blob'); return 'blob:local';}, revokeObjectURL: url => downloads.push({revoked: url})},
        setInterval: (cb, ms) => {const id = nextTimer++; timers.set(id, {cb, ms}); return id;}, clearInterval: id => timers.delete(id),
        navigator: {clipboard: {writeText: async text => {if (network.clipboardError) throw new Error('denied'); copied.push(text);}}},
        setTimeout: cb => {cb();}});
    vm.runInContext(readFileSync(join(__dirname, '../../custom_components/bps/frontend/diagnostics.js'), 'utf8'), sandbox);
    sandbox.BPSDiagnostics.init(async (url, options) => {
        requests.push({url, options});
        if (network.pending) await network.pending;
        if (network.error) return {ok: false, status: 400, json: async () => ({error: network.error})};
        if (url.includes('?report=1')) return {ok: true, status: 200, json: async () => ({text: network.report})};
        if (options) {
            const body = JSON.parse(options.body);
            if (body.action === 'snapshot') return {ok: true, status: 200,
                json: async () => ({text: network.report, read_only: true, time: 100,
                    comparison: {time: 100, markers: body.known_positions.map(marker => ({...marker,
                        summary: {disagreeing_readings: 1, unscored_readings: 0}, position_errors: [],
                        ranked_readings: [{tracker: '<tag>', receiver: '<receiver>', status: 'current', expected_distance_m: 2,
                            corrected_distance_m: 5, signed_error_m: 3, age_s: 1, environment: network.snapshotEnvironment === undefined
                                ? {intersections: [{type: 'building', material: 'metal'}]} : network.snapshotEnvironment}],
                        unscored_readings: []}))}})};
            if (body.action === 'start') Object.assign(network.status, {active: true, available: true, targets: body.targets, frames: 1, reason: 'Recording', seconds_left: 600});
            if (body.action === 'stop') Object.assign(network.status, {active: false, reason: 'Stopped'});
            if (body.action === 'clear') Object.assign(network.status, {active: false, available: false, reason: 'Cleared'});
            if (body.action === 'annotate') network.status.annotations++;
        }
        return {ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(network.status)), blob: async () => 'diagnostic-blob'};
    }, panelSnapshot, locationTools);
    return {el, network, timers, requests, downloads, copied};
}

test('panel context is frozen separately before the server fetch and is included in copied text', async () => {
    const editor = {selected_floor: 'Barn', editor_layout: {floor: [{name: 'Barn', receivers: [{cords: {x: 2, y: 3}}]}]}};
    const p = panel(() => editor);
    let release; p.network.pending = new Promise(resolve => {release = resolve;});
    const action = p.el('CopyAll').fire();
    editor.editor_layout.floor[0].receivers[0].cords.x = 20;
    release(); p.network.pending = null; await action;
    const text = p.copied[0];
    const report = JSON.parse(text.slice(text.indexOf('{')));
    assert.deepEqual(report.trackers, ['tag', 'tag2']);
    const reservation = Number(new URL(p.requests[0].url, 'https://synthetic.invalid').searchParams.get('panel_bytes'));
    assert.equal(reservation, new TextEncoder().encode(JSON.stringify({panel_context: report.panel_context}, null, 2)).length + 256);
    const copiedPanel = report.panel_context;
    assert.equal(copiedPanel.editor_layout.floor[0].receivers[0].cords.x, 2);
    assert.equal(p.el('ReportText').value, text);
});

test('oversized combined reports preserve the previous report and do not copy incomplete output', async () => {
    let extra = '';
    const p = panel(() => ({extra}));
    await p.el('CopyAll').fire();
    const previous = p.el('ReportText').value;
    extra = 'x'.repeat(4 * 1024 * 1024);
    await p.el('CopyAll').fire();
    assert.match(p.el('ReportStatus').textContent, /exceeds 4 MiB.*previous report/);
    assert.equal(p.el('ReportText').value, previous);
    assert.equal(p.copied.length, 1);
});

test('full report is copied on demand without loading trackers or starting capture', async () => {
    const p = panel();
    await p.el('CopyAll').fire();
    assert.deepEqual(p.requests.map(r => r.url), ['/api/bps/diagnostics?report=1']);
    assert.deepEqual(p.copied, [p.network.report]);
    assert.equal(p.el('ReportText').value, p.network.report);
    assert.equal(p.el('ReportPanel').hidden, false);
    assert.match(p.el('ReportStatus').textContent, /copied/);
    assert.equal(p.timers.size, 0);
});

test('clipboard rejection leaves a selected report and failed refresh preserves the previous text', async () => {
    const p = panel(); p.network.clipboardError = true;
    await p.el('CopyAll').fire();
    assert.equal(p.el('ReportText').selected, true);
    assert.match(p.el('ReportStatus').textContent, /Ctrl\+C/);
    p.el('ReportText').selected = false;
    p.el('SelectReport').fire();
    assert.equal(p.el('ReportText').selected, true);
    p.network.error = 'Full diagnostic report exceeds 4 MiB';
    await p.el('CopyAll').fire();
    assert.equal(p.el('ReportText').value, p.network.report);
    assert.match(p.el('ReportStatus').textContent, /exceeds 4 MiB.*previous report/);
    assert.equal(p.el('CopyAll').disabled, false);
});

test('concurrent full report clicks are coalesced and empty reports are rejected', async () => {
    const p = panel();
    let release; p.network.pending = new Promise(resolve => {release = resolve;});
    const first = p.el('CopyAll').fire();
    await p.el('CopyAll').fire();
    assert.equal(p.requests.length, 1);
    assert.equal(p.el('CopyAll').disabled, true);
    release(); p.network.pending = null; await first;
    p.network.report = '';
    await p.el('CopyAll').fire();
    assert.match(p.el('ReportStatus').textContent, /empty/);
    assert.equal(p.copied.length, 1);
});

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

test('known-position capture freezes truth, includes all BLE data and creates no recording or timers', async () => {
    const p = panel(); await p.el('SnapshotLoad').fire();
    p.el('SnapshotX').value = '0'; p.el('SnapshotY').value = '4.2'; p.el('SnapshotLabel').value = '<stationary>';
    let release; p.network.pending = new Promise(resolve => {release = resolve;});
    const pending = p.el('SnapshotCapture').fire();
    await p.el('SnapshotCapture').fire();
    p.el('SnapshotX').value = '9'; release(); p.network.pending = null; await pending;
    const sent = p.requests.filter(r => r.options?.method === 'POST');
    assert.equal(sent.length, 1);
    assert.deepEqual(JSON.parse(sent[0].options.body), {action: 'snapshot', known_positions: [{target: 'bps_group_dog', floor: 'Yard',
        x_m: 0, y_m: 4.2, accuracy_m: 1, label: '<stationary>'}], panel_bytes: 0});
    assert.deepEqual(JSON.parse(p.copied[0].slice(p.copied[0].indexOf('{'))).trackers, ['tag', 'tag2']);
    assert.equal(p.timers.size, 0); assert.equal(p.network.status.active, false);
    const text = node => [node.textContent, ...node.children.map(text)].join(' ');
    assert.match(text(p.el('SnapshotComparison')), /actual \(0.00, 4.20\).*<receiver>.*metal/);
    assert.equal(p.el('SnapshotCapture').disabled, false);
});

test('snapshot paths distinguish clear geometry from failed or omitted assessments', async () => {
    const p = panel(); await p.el('SnapshotLoad').fire();
    p.el('SnapshotX').value = '1'; p.el('SnapshotY').value = '2';
    const text = node => [node.textContent, ...node.children.map(text)].join(' ');
    for (const environment of [null, {classification: 'unknown', intersections: []},
        {classification: 'clear', environment_error: 'ValueError', intersections: []},
        {classification: 'clear', invalid_path: true, intersections: []}]) {
        p.network.snapshotEnvironment = environment; await p.el('SnapshotCapture').fire();
        assert.match(text(p.el('SnapshotComparison')), /Path unavailable/);
        assert.doesNotMatch(text(p.el('SnapshotComparison')), /No mapped crossing/);
    }
    p.network.snapshotEnvironment = {classification: 'not_evaluated', intersections: []};
    await p.el('SnapshotCapture').fire(); assert.match(text(p.el('SnapshotComparison')), /Path not assessed/);
    p.network.snapshotEnvironment = {classification: 'clear', intersections: [], environmental_weight: 1};
    await p.el('SnapshotCapture').fire(); assert.match(text(p.el('SnapshotComparison')), /No mapped crossing; trust 1.00/);
});

test('snapshot location validation and unsaved-map guard preserve previous reports', async () => {
    let unsaved = false;
    const p = panel(null, {validate() {if (unsaved) throw new Error('Save Floor Plan first');}});
    await p.el('CopyAll').fire(); const previous = p.el('ReportText').value;
    await p.el('SnapshotCapture').fire(); assert.match(p.el('ReportStatus').textContent, /Load dogs/);
    await p.el('SnapshotLoad').fire();
    p.el('SnapshotX').value = ''; p.el('SnapshotY').value = '2';
    await p.el('SnapshotCapture').fire(); assert.match(p.el('ReportStatus').textContent, /actual location/);
    p.el('SnapshotX').value = 'Infinity'; await p.el('SnapshotCapture').fire(); assert.match(p.el('ReportStatus').textContent, /finite/);
    p.el('SnapshotX').value = '1'; unsaved = true; await p.el('SnapshotCapture').fire();
    assert.match(p.el('ReportStatus').textContent, /Save Floor Plan/); assert.equal(p.el('ReportText').value, previous);
    assert.equal(p.requests.filter(r => r.options?.method === 'POST').length, 0);
});

test('map picking populates the reference, cancels cleanly and selection changes require a new position', async () => {
    let callback, cancels = 0, clears = 0;
    const p = panel(null, {pick(done, cancel) {callback = done; return () => {cancels++; cancel();};}, clearPoint() {clears++;}});
    await p.el('SnapshotLoad').fire(); await p.el('SnapshotPick').fire(); callback({floor: 'Yard', x_m: 2.125, y_m: 4.25});
    assert.equal(p.el('SnapshotX').value, '2.125'); assert.equal(p.el('SnapshotY').value, '4.250');
    await p.el('SnapshotPick').fire(); await p.el('SnapshotPick').fire(); assert.equal(cancels, 1);
    await p.el('SnapshotTarget').fire('change'); assert.equal(p.el('SnapshotX').value, ''); assert.equal(clears, 1);
});
