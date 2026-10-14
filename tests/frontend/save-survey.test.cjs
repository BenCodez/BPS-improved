const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

function productionSave({warning = null} = {}) {
    const source = readFileSync('custom_components/bps/frontend/script.js', 'utf8');
    const start = source.indexOf('    async function savedata(');
    const end = source.indexOf('    // =================================================================', start);
    const method = source.match(/surveyCanStart\(\) \{([\s\S]*?)\n        \},/)[1];
    let release, fail = false;
    const pending = new Promise(resolve => {release = resolve;});
    const requests = [], messages = [];
    const state = vm.createContext({layoutSavePending: false, layoutRevision: 'a'.repeat(64), gpsUI: {isVerifying: () => false},
        myScaleVal: 20, removefile: false, new_floor: false, finalcords: {floor: []},
        removeListeners() {}, FormData: class {constructor() {this.values = new Map();} append(k, v) {this.values.set(k, v);}}, saveButton: {remove() {}},
        bpsToast: value => messages.push(value), console: {error() {}},
        bpsFetch: async (url, options) => {requests.push(options.body); await pending; if (fail) throw new Error('synthetic failure'); return {ok: true, headers: {get: name => name === 'X-BPS-Layout-Revision' ? 'b'.repeat(64) : warning}};},
        savebuttondiv: {contains: () => false}, mapReady: () => true, anyLayoutToolActive: () => false});
    vm.runInContext(source.slice(start, end) + '\nfunction canSurvey() {' + method + '\n}', state);
    return {state, requests, messages, release, fail() {fail = true;}};
}

test('central floor-save path blocks an in-flight receiver verification before posting stale coordinates', async () => {
    const p = productionSave(); p.state.gpsUI.isVerifying = () => true;
    assert.equal(await p.state.savedata(), false);
    assert.equal(p.requests.length, 0);
    assert.match(p.messages[0], /verification/);
});

test('an in-flight floor save blocks verification and duplicate full saves until completion', async () => {
    const p = productionSave();
    const save = p.state.savedata();
    assert.equal(p.state.layoutSavePending, true);
    assert.throws(() => p.state.canSurvey(), /floor-plan save/);
    assert.equal(await p.state.savedata(), false);
    assert.equal(p.requests.length, 1);
    p.release(); assert.equal(await save, true);
    assert.equal(p.requests[0].values.get('layout_revision'), 'a'.repeat(64));
    assert.equal(p.state.layoutRevision, 'b'.repeat(64));
    assert.equal(p.state.layoutSavePending, false); assert.equal(p.state.canSurvey(), true);
});

test('failed saves and early validation failures release the shared save guard', async () => {
    const p = productionSave(); p.fail();
    const save = p.state.savedata(); p.release(); await save;
    assert.equal(p.state.layoutSavePending, false);
    p.state.myScaleVal = null; await p.state.savedata();
    assert.equal(p.state.layoutSavePending, false);
});

test('committed layout cleanup warnings are visible and retain the revision for the next save', async () => {
    const warning = 'Layout saved, but the old map file could not be removed.';
    const p = productionSave({warning});
    const save = p.state.savedata(); p.release();
    assert.equal(await save, true);
    assert.deepEqual(p.messages, [warning]);
    assert.equal(p.state.layoutSavePending, false);
    assert.equal(p.state.layoutRevision, 'b'.repeat(64));
    assert.equal(await p.state.savedata(), true);
    assert.equal(p.requests[1].values.get('layout_revision'), 'b'.repeat(64));
});
