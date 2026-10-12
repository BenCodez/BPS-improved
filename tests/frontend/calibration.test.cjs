const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');

function panel() {
    const elements = new Map(), listeners = new Map(), requests = [], copied = [];
    function make(tag = 'div') {
        let id = '';
        const e = {tag, children: [], handlers: {}, textContent: '', value: '', disabled: false,
            set id(v) {id = v; elements.set(v, e);}, get id() {return id;},
            setAttribute(k, v) {this[k] = v;}, appendChild(c) {this.children.push(c);},
            replaceChildren() {this.children = [];}, contains(c) {return this === c || this.children.some(e => e.contains(c));},
            addEventListener(type, cb) {this.handlers[type] = cb;}, fire(type = 'click') {return this.handlers[type]?.();},
            focus() {this.focused = true;}, select() {this.selected = true;}};
        return e;
    }
    make().id = 'calibrationdiv'; make('textarea').id = 'mapname'; elements.get('mapname').value = 'Yard';
    const row = {receiver: '<receiver>', scanner_name: 'Probe', scanner_mac: 'aa:bb:cc:dd:ee:00', bermuda_entry_id: 'synthetic-entry',
        attenuation: 4, current_offset_db: -9, current_bps_factor: .5, transfer_current_offset_db: 3.0412,
        proposed_bps_factor: 2, proposed_offset_db: -21.0412, bps_factor_after_manual_transfer: 1,
        candidate_eligible: false, transfer_current_eligible: true, reasons: ['solve_worsens_reference_error']};
    const network = {pending: null, error: null, clipboardError: false,
        data: {format: 'bps-bermuda-correction-preview-v1', read_only: true, floor: 'Yard', units: {offset: 'dB'},
            rows: [row], instructions: 'Read-only. Apply manually in Bermuda and reset affected BPS factors to 1.'}};
    const document = {getElementById: id => elements.get(id), createElement: make,
        addEventListener(type, cb) {listeners.set(type, cb);}};
    const sandbox = vm.createContext({window: {}, document,
        navigator: {clipboard: {writeText: async text => {if (network.clipboardError) throw new Error('denied'); copied.push(text);}}}});
    vm.runInContext(readFileSync(join(__dirname, '../../custom_components/bps/frontend/calibration.js'), 'utf8'), sandbox);
    const fetch = async (url, options) => {
        requests.push({url, options});
        if (network.pending) await network.pending;
        return {ok: !network.error, status: network.error ? 400 : 200,
            json: async () => network.error ? {error: network.error} : JSON.parse(JSON.stringify(network.data))};
    };
    sandbox.window.BPSBermudaCalibration.init(fetch);
    function buttons() {
        const all = [];
        function visit(e) {if (e.tag === 'button') all.push(e); for (const c of e.children) visit(c);}
        visit(elements.get('bermudaPreviewRows')); return all;
    }
    return {el: id => elements.get('bermuda' + id), floor: elements.get('mapname'), document,
        network, requests, copied, buttons, listeners, init: () => sandbox.window.BPSBermudaCalibration.init(fetch)};
}

test('preview installs once without fetching and uses only authenticated read-only action', async () => {
    const p = panel(); p.init();
    assert.equal(p.requests.length, 0); assert.equal(p.el('PreviewCopy').disabled, true);
    await p.el('Preview').fire();
    assert.equal(p.requests.length, 1);
    assert.equal(p.requests[0].url, '/api/bps/calibration');
    assert.deepEqual(JSON.parse(p.requests[0].options.body), {action: 'preview_bermuda', floor: 'Yard'});
    assert.match(p.el('PreviewStatus').textContent, /Read-only.*No settings saved/);
    assert.equal(p.el('PreviewPanel').children.filter(c => c.id === 'bermudaPreview').length, 1);
});

test('blocked candidate copy stays disabled while range-preserving transfer copies reset instruction', async () => {
    const p = panel(); await p.el('Preview').fire();
    const [transfer, proposed] = p.buttons();
    assert.equal(transfer.disabled, false); assert.equal(proposed.disabled, true);
    await proposed.fire(); assert.equal(p.copied.length, 0);
    await transfer.fire();
    const row = JSON.parse(p.copied[0]);
    assert.equal(row.mode, 'transfer_current'); assert.equal(row.read_only, true);
    assert.equal(row.proposed_offset_db, 3.0412); assert.equal(row.proposed_bps_factor, .5);
    assert.equal(row.bps_factor_after_manual_transfer, 1);
    assert.match(row.instructions, /reset affected BPS factors to 1/);
    assert.equal(p.requests.length, 1);
});

test('eligible proposal uses absolute candidate offset and full report JSON preserves evidence', async () => {
    const p = panel(); p.network.data.rows[0].candidate_eligible = true; p.network.data.rows[0].reasons = [];
    await p.el('Preview').fire(); await p.buttons()[1].fire();
    const row = JSON.parse(p.copied[0]);
    assert.equal(row.mode, 'candidate'); assert.equal(row.proposed_offset_db, -21.0412);
    assert.equal(row.proposed_bps_factor, 2); assert.equal(row.current_bps_factor, .5);
    await p.el('PreviewCopy').fire();
    assert.deepEqual(JSON.parse(p.copied[1]), p.network.data);
    assert.equal(p.el('PreviewText').readOnly, true);
    // Receiver labels are inserted as text rather than interpreted markup.
    assert.equal(p.el('PreviewRows').children[0].children[1].children[0].textContent,
        '<receiver>\nProbe\naa:bb:cc:dd:ee:00');
});

test('unknown transfer is disabled and clipboard denial exposes selected plain JSON', async () => {
    const p = panel(); p.network.data.rows[0].transfer_current_eligible = false;
    await p.el('Preview').fire(); assert.equal(p.buttons().length, 0);
    await p.el('PreviewAll').fire(); assert.equal(p.buttons()[0].disabled, true);
    await p.buttons()[0].fire(); assert.equal(p.copied.length, 0);
    p.network.clipboardError = true; await p.el('PreviewCopy').fire();
    assert.equal(p.el('PreviewText').selected, true);
    assert.match(p.el('PreviewStatus').textContent, /Ctrl\+C/);
});

test('floor/editor changes invalidate evidence and late responses cannot populate another floor', async () => {
    const p = panel(); let release;
    p.network.pending = new Promise(resolve => {release = resolve;});
    const pending = p.el('Preview').fire();
    await p.el('Preview').fire(); assert.equal(p.requests.length, 1);
    p.floor.value = 'Barn'; p.listeners.get('input')({target: p.floor});
    release(); await pending;
    assert.equal(p.el('PreviewCopy').disabled, true); assert.equal(p.el('PreviewRows').children.length, 0);
    assert.equal(p.el('Preview').disabled, false);
    p.network.pending = null; await p.el('Preview').fire();
    assert.equal(JSON.parse(p.requests.at(-1).options.body).floor, 'Barn');
    p.listeners.get('change')({target: {}});
    assert.equal(p.el('PreviewCopy').disabled, true);
    assert.equal(p.el('PreviewRows').children.length, 0);
});

test('failed refresh clears candidate actions and permits retry', async () => {
    const p = panel(); await p.el('Preview').fire();
    p.network.error = 'Bermuda runtime unavailable'; await p.el('Preview').fire();
    assert.equal(p.el('PreviewCopy').disabled, true); assert.equal(p.el('PreviewRows').children.length, 0);
    assert.equal(p.el('PreviewStatus').textContent, 'Bermuda runtime unavailable');
    assert.equal(p.el('Preview').disabled, false);
    p.network.error = null; await p.el('Preview').fire(); assert.equal(p.el('PreviewCopy').disabled, false);
});

test('preview defaults to useful eligible changes and expands blocked/no-op details without fetching', async () => {
    const p = panel();
    const row = p.network.data.rows[0];
    p.network.data.rows.push({...row, receiver: 'blocked', transfer_current_eligible: false});
    p.network.data.rows.push({...row, receiver: 'unchanged', current_bps_factor: 1,
        transfer_current_offset_db: row.current_offset_db, candidate_eligible: true,
        proposed_offset_db: row.current_offset_db + .1, reasons: []});
    await p.el('Preview').fire();
    assert.equal(p.buttons().length, 2);
    assert.match(p.el('PreviewSummary').textContent, /1 useful receiver change.*2 blocked or unchanged/);
    assert.equal(p.el('PreviewAll').hidden, false);
    await p.el('PreviewAll').fire();
    assert.equal(p.buttons().length, 6);
    assert.equal(p.buttons()[4].disabled, true); assert.equal(p.buttons()[5].disabled, true);
    await p.el('PreviewAll').fire(); assert.equal(p.buttons().length, 2);
    assert.equal(p.requests.length, 1);
    await p.el('PreviewCopy').fire();
    assert.equal(JSON.parse(p.copied[0]).rows.length, 3);
});

test('verification displays measured evidence and detached actions cannot copy an obsolete preview', async () => {
    const p = panel();
    p.network.data.verification = {source_matches: true, candidate_matches_test: true, tracking_verdict: 'inconclusive',
        tracking: {replayed_samples: 20, locations: 2, baseline: {median_m: 4, p95_m: 8},
            candidate: {median_m: 3, p95_m: 9}, verification: {reasons: ['location_regression']}}};
    await p.el('Preview').fire();
    assert.match(p.el('PreviewVerification').textContent, /inconclusive.*20 observations.*2 measured locations.*median 4 → 3.*p95 8 → 9.*location regression/);
    const obsolete = p.buttons()[0];
    await p.el('Preview').fire(); await obsolete.fire();
    assert.equal(p.copied.length, 0);
    await p.buttons()[0].fire(); assert.equal(p.copied.length, 1);
});
