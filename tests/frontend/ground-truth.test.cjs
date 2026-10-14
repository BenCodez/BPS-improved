const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

function setup() {
    const nodes = new Map(), requests = [], copied = [];
    function node() {return {value: '', hidden: false, disabled: false, textContent: '', children: [], events: {},
        addEventListener(type, fn) {this.events[type] = fn;}, fire() {return this.events.click();},
        replaceChildren() {this.children = [];}, appendChild(item) {this.children.push(item);},
        focus() {this.focused = true;}, select() {this.selected = true;}};}
    for (const name of ['Panel', 'Mark', 'Copy', 'Clear', 'History', 'Status', 'Accuracy', 'Label', 'Report']) nodes.set('liveTruth' + name, node());
    const el = name => nodes.get('liveTruth' + name);
    const options = {open: false}; el('Report').closest = selector => selector === 'details' ? options : null;
    el('Accuracy').value = '1';
    const state = {target: 'bps_group_dog', pick: null, cancelled: null, clears: 0, marks: [], pending: null, error: null,
        valid: true, clipboard: true, now: 100000};
    const sandbox = vm.createContext({document: {getElementById: name => nodes.get(name), createElement: node},
        Date, Math, navigator: {clipboard: {writeText: async text => {if (!state.clipboard) throw new Error('denied'); copied.push(text);}}}});
    vm.runInContext(readFileSync('custom_components/bps/frontend/ground-truth.js', 'utf8'), sandbox);
    const controller = sandbox.BPSGroundTruth.init(async (url, options) => {
        requests.push({url, options});
        if (state.pending) await state.pending;
        if (state.error) return {ok: false, status: 400, json: async () => ({error: state.error})};
        let saved;
        if (options) {
            const body = JSON.parse(options.body);
            if (body.action === 'clear') state.marks = [];
            else {
                saved = {id: body.id, time: state.now / 1000, ...body.marker,
                    position_errors: [{target: body.marker.target, scored: true, error_m: 2}]};
                state.marks.push(saved);
            }
        }
        return {ok: true, json: async () => ({marks: [...state.marks], saved, evicted: 0,
            ...(url.includes('download') ? {records: [{report: {readings: ['original BLE data']}}]} : {})})};
    }, {getTarget: () => state.target, validate() {if (!state.valid) throw new Error('Return to Live first.');},
        pick(done, cancelled) {state.pick = done; state.cancelled = cancelled; return () => cancelled();},
        clearPoint() {state.clears++;}, confirmClear: async () => true}, {now: () => state.now});
    return {el, state, requests, copied, controller, options};
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test('repeated map marks capture the selected group and independent instantaneous points', async () => {
    const p = setup(); await settle();
    assert.equal(p.requests.length, 1);
    p.el('Mark').fire();
    assert.equal(p.requests.length, 1, 'arming the picker does not collect prematurely');
    await p.state.pick({floor: 'Yard', x_m: 3, y_m: 4});
    p.state.now += 1000;
    p.el('Mark').fire();
    await p.state.pick({floor: 'Yard', x_m: 8, y_m: 4});
    const marks = p.requests.filter(r => r.options).map(r => JSON.parse(r.options.body));
    assert.equal(marks.length, 2);
    assert.notEqual(marks[0].id, marks[1].id);
    assert.deepEqual(marks.map(m => m.marker), [{target: 'bps_group_dog', floor: 'Yard', x_m: 3, y_m: 4, accuracy_m: 1, label: ''},
        {target: 'bps_group_dog', floor: 'Yard', x_m: 8, y_m: 4, accuracy_m: 1, label: ''}]);
    assert.match(p.el('Status').textContent, /Saved 2 marks.*2.0 m/);
    assert.equal(p.el('History').children.length, 2);
});

test('changing the selected tracker cancels an armed point and rejects a late callback', async () => {
    const p = setup(); await settle(); p.el('Mark').fire();
    const click = p.state.pick;
    p.state.target = 'bps_group_other'; p.controller.sync();
    assert.match(p.el('Status').textContent, /cancelled/);
    await click({floor: 'Yard', x_m: 3, y_m: 4});
    assert.equal(p.requests.length, 1); assert.equal(p.state.clears, 1);
});

test('replay, invalid tolerance and a lost live session cannot save marks', async () => {
    const p = setup(); await settle();
    p.state.valid = false; p.el('Mark').fire();
    assert.match(p.el('Status').textContent, /Return to Live/);
    p.state.valid = true; p.el('Accuracy').value = '-1'; p.el('Mark').fire();
    assert.match(p.el('Status').textContent, /tolerance/);
    p.el('Accuracy').value = '1'; p.el('Mark').fire();
    p.state.valid = false; await p.state.pick({floor: 'Yard', x_m: 3, y_m: 4});
    assert.equal(p.requests.length, 1);
    p.state.target = null; p.controller.sync(); assert.equal(p.el('Panel').hidden, true);
});

test('a pending save blocks another mark and errors are not reported as saved', async () => {
    const p = setup(); await settle(); p.el('Mark').fire();
    let release; p.state.pending = new Promise(resolve => {release = resolve;});
    const save = p.state.pick({floor: 'Yard', x_m: 3, y_m: 4});
    p.el('Mark').fire(); assert.equal(p.requests.length, 2);
    assert.equal(p.el('Mark').disabled, true);
    p.state.error = 'synthetic disk failure'; release(); await save;
    assert.match(p.el('Status').textContent, /not confirmed saved.*disk failure/);
    assert.equal(p.el('Mark').disabled, false);
});

test('history export includes raw snapshots and clipboard fallback preserves selectable text', async () => {
    const p = setup(); await settle(); p.state.clipboard = false;
    await p.el('Copy').fire();
    assert.equal(p.requests.at(-1).url, '/api/bps/ground_truth?download=1');
    assert.match(p.el('Report').value, /original BLE data/);
    assert.equal(p.el('Report').hidden, false); assert.equal(p.el('Report').selected, true);
    assert.equal(p.options.open, true);
});

test('clearing labelled history uses a separate endpoint and leaves tracking alone', async () => {
    const p = setup(); await settle(); p.el('Mark').fire();
    await p.state.pick({floor: 'Yard', x_m: 3, y_m: 4});
    await p.el('Clear').fire();
    assert.deepEqual(JSON.parse(p.requests.at(-1).options.body), {action: 'clear'});
    assert.equal(p.state.target, 'bps_group_dog');
    assert.equal(p.el('History').children.length, 0);
});
