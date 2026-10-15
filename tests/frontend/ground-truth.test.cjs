const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

function setup(initialError = null, withGPS = false) {
    const nodes = new Map(), requests = [], copied = [], timers = new Map(), events = new Map();
    let timerId = 0;
    function node() {return {value: '', hidden: false, disabled: false, textContent: '', children: [], events: {},
        addEventListener(type, fn) {this.events[type] = fn;}, fire(type = 'click') {return this.events[type]();},
        replaceChildren() {this.children = [];}, appendChild(item) {this.children.push(item);},
        focus() {this.focused = true;}, select() {this.selected = true;}};}
    for (const name of ['Panel', 'Mark', 'Copy', 'Clear', 'History', 'Status', 'Accuracy', 'Label', 'Report',
        'Target', 'Focus', 'Shortcut', 'SurveyPick', 'SurveyStart', 'SurveyStop', 'SurveyPanel', 'Progress', 'SurveyStatus', 'Download', 'Retention']) nodes.set('liveTruth' + name, node());
    if (withGPS) for (const name of ['Start', 'Stop', 'Mode', 'Distance', 'Status']) nodes.set('liveGPS' + name, node());
    if (withGPS) {nodes.get('liveGPSMode').value = 'walk'; nodes.get('liveGPSDistance').value = '5';}
    const el = name => nodes.get('liveTruth' + name);
    const options = {open: false}; el('Report').closest = selector => selector === 'details' ? options : null;
    el('Accuracy').value = '1';
    const state = {target: 'bps_group_dog', pick: null, cancelled: null, clears: 0, marks: [], pending: null, error: initialError,
        valid: true, clipboard: true, now: 100000, context: 'saved-map-a', focused: false, downloadError: null, downloads: 0,
        targets: [{key: 'bps_group_dog', name: 'Dog'}, {key: 'bps_group_other', name: 'Other dog'}],
        wakeRequests: 0, wakeReleases: 0, wakePending: null};
    const document = {hidden: false, getElementById: name => nodes.get(name), createElement: node,
        addEventListener: (type, fn) => events.set(type, fn)};
    const sandbox = vm.createContext({document,
        setTimeout: (fn, ms) => {timers.set(++timerId, {fn, at: state.now + ms}); return timerId;},
        clearTimeout: id => timers.delete(id),
        Date, Math, navigator: {clipboard: {writeText: async text => {if (!state.clipboard) throw new Error('denied'); copied.push(text);}},
            wakeLock: {request: async () => {state.wakeRequests++; if (state.wakePending) await state.wakePending;
                return {release: async () => {state.wakeReleases++;}};}}}});
    if (withGPS) for (const file of ['gps.js', 'gps-walk.js']) vm.runInContext(readFileSync('custom_components/bps/frontend/' + file, 'utf8'), sandbox);
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
                    ...(body.action === 'gps_mark' ? {accuracy_m: 14} : {}),
                    position_errors: [{target: body.marker.target, scored: true, error_m: 2}]};
                state.marks.push(saved);
            }
        }
        return {ok: true, json: async () => ({marks: [...state.marks], saved, evicted: state.evicted || 0,
            limits: {max_marks: 500, max_bytes: 128 * 1048576, max_age_s: 30 * 86400},
            ...(url.includes('download') ? {records: [{report: {readings: ['original BLE data']}}]} : {})})};
    }, {getTarget: () => state.target, getTargets: () => state.targets, selectTarget: key => {state.target = key;},
        getGPSMap: () => ({name: 'Yard', scale: 10, layout_revision: 'a'.repeat(64),
            georeference: {version: 1, anchors: [[0,0],[30,0],[0,30]].map(([x,y]) => ({
                ...sandbox.BPSGPS.fromLocal({x,y}, {latitude:10,longitude:20}),
                x:x*10, y:y*10, accuracy_m:2, label:'Synthetic'}))}}),
        getContext: () => state.context, focusView: value => {state.focused = value;},
        download: async () => {state.downloads++; return state.downloadError ? {ok: false, error: state.downloadError} : {ok: true};},
        validate() {if (!state.valid) throw new Error('Return to Live first.');},
        pick(done, cancelled) {state.pick = done; state.cancelled = cancelled; return () => cancelled();},
        clearPoint() {state.clears++;}, confirmClear: async () => true}, {now: () => state.now,
        monotonic: () => state.now, geolocation: {watchPosition(ok) {state.gps = ok; return 1;}, clearWatch() {state.gps = null;}}});
    return {el, gpsEl: name => nodes.get('liveGPS' + name), state, requests, copied, controller, options, timers, document,
        async hide() {document.hidden = true; events.get('visibilitychange')(); await settle();},
        async advance(ms) {
            state.now += ms;
            for (const [id, timer] of [...timers]) if (timer.at <= state.now) {timers.delete(id); await timer.fn();}
            await settle();
        }};
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

test('Clear stays available when expiry failure prevents stored marks from loading', async () => {
    const p = setup('Actual-location history expiry write failed'); await settle();
    assert.match(p.el('Status').textContent, /expiry write failed/);
    assert.equal(p.el('History').children.length, 0);
    assert.equal(p.el('Clear').disabled, false);
    p.state.error = null;
    await p.el('Clear').fire();
    assert.deepEqual(JSON.parse(p.requests.at(-1).options.body), {action: 'clear'});
    assert.match(p.el('Status').textContent, /history cleared/);
});

async function chooseSurvey(p) {
    p.el('SurveyPick').fire();
    await p.state.pick({floor: 'Yard', x_m: 12, y_m: 8});
}
const posts = p => p.requests.filter(r => r.options).map(r => JSON.parse(r.options.body));

test('GPS survey uses the actual-location API, blocks manual marks and restores controls after stop', async () => {
    const p = setup(null, true); await settle();
    p.gpsEl('Start').fire();
    p.state.gps({timestamp:p.state.now,coords:{latitude:10,longitude:20,accuracy:2,speed:1}});
    await settle();
    assert.equal(posts(p).length,1);
    assert.equal(posts(p)[0].action,'gps_mark');
    assert.deepEqual(posts(p)[0].marker,{target:'bps_group_dog',floor:'Yard',label:''});
    assert.equal(posts(p)[0].gps.layout_revision,'a'.repeat(64));
    assert.equal(p.el('Mark').disabled,true);
    p.el('Mark').fire(); await p.el('Copy').fire(); await p.el('Download').fire();
    assert.equal(posts(p).length,1);
    assert.equal(p.state.downloads,0);
    assert.match(p.gpsEl('Status').textContent,/1 GPS\/BLE/);
    p.gpsEl('Stop').fire();
    assert.equal(p.el('Mark').disabled,false); assert.equal(p.el('Download').disabled,false);
    assert.equal(p.state.gps,null);
});

test('stopping GPS during a pending save restores GPS start and inputs when the save finishes', async () => {
    const p = setup(null, true); await settle();
    let release; p.state.pending = new Promise(resolve => {release = resolve;});
    p.gpsEl('Start').fire();
    p.state.gps({timestamp:p.state.now,coords:{latitude:10,longitude:20,accuracy:2,speed:1}});
    await settle(); p.gpsEl('Stop').fire();
    assert.equal(p.gpsEl('Start').disabled,true);
    release(); await settle();
    for (const key of ['Start','Mode','Distance']) assert.equal(p.gpsEl(key).disabled,false);
    p.state.pending=null; p.gpsEl('Start').fire();
    assert.equal(p.gpsEl('Stop').disabled,false);
});

test('stationary survey waits, saves five spaced instantaneous labels and does not apply settings', async () => {
    const p = setup(); await settle(); await chooseSurvey(p);
    assert.equal(posts(p).length, 0, 'choosing a spot does not capture before settling');
    p.el('SurveyStart').fire();
    assert.equal(p.el('Download').disabled, true);
    await p.advance(29000); assert.equal(posts(p).length, 0);
    await p.advance(1000); assert.equal(posts(p).length, 1);
    for (let i = 0; i < 4; i++) {
        await p.advance(9000); assert.equal(posts(p).length, i + 1);
        await p.advance(1000); assert.equal(posts(p).length, i + 2);
    }
    assert.deepEqual(posts(p).map(r => r.marker), Array.from({length: 5}, () => ({target: 'bps_group_dog', floor: 'Yard', x_m: 12, y_m: 8, accuracy_m: 1, label: ''})));
    assert.equal(new Set(posts(p).map(r => r.id)).size, 5);
    assert.equal(p.timers.size, 0);
    assert.equal(p.state.wakeRequests, 1);
    assert.equal(p.state.wakeReleases, 1);
    assert.equal(p.el('Download').disabled, false);
    assert.match(p.el('Status').textContent, /5 snapshots saved.*desktop later/);
    assert.ok(p.requests.every(r => r.url.startsWith('/api/bps/ground_truth')));
});

test('delayed timers never backfill multiple labels for time spent away', async () => {
    const p = setup(); await settle(); await chooseSurvey(p); p.el('SurveyStart').fire();
    await p.advance(180000);
    assert.equal(posts(p).length, 1);
    await p.advance(1000); assert.equal(posts(p).length, 1);
    p.el('SurveyStop').fire(); await p.advance(100000);
    assert.equal(posts(p).length, 1);
});

for (const change of ['target', 'map', 'replay', 'hidden']) {
    test(`survey stops before another label when ${change} changes`, async () => {
        const p = setup(); await settle(); await chooseSurvey(p); p.el('SurveyStart').fire();
        if (change === 'target') {p.state.target = 'bps_group_other'; p.controller.sync();}
        if (change === 'map') p.state.context = 'saved-map-b';
        if (change === 'replay') p.state.valid = false;
        if (change === 'hidden') await p.hide();
        await p.advance(30000);
        assert.equal(posts(p).length, 0);
        assert.equal(p.timers.size, 0);
        assert.equal(p.el('SurveyStart').disabled, true);
        assert.match(p.el('Status').textContent, /stopped/i);
    });
}

test('stop during an in-flight save preserves that mark without scheduling the next one', async () => {
    const p = setup(); await settle(); await chooseSurvey(p); p.el('SurveyStart').fire();
    let release; p.state.pending = new Promise(resolve => {release = resolve;});
    await p.advance(30000);
    assert.equal(posts(p).length, 1);
    assert.equal(p.el('SurveyStop').disabled, false);
    p.el('SurveyStop').fire(); release(); await settle();
    await p.advance(100000);
    assert.equal(posts(p).length, 1);
    assert.equal(p.timers.size, 0);
    assert.equal(p.state.marks.length, 1);
});

test('capture errors stop a sequence and do not count a failed snapshot', async () => {
    const p = setup(); await settle(); await chooseSurvey(p); p.el('SurveyStart').fire();
    await p.advance(30000);
    p.state.error = 'disk unavailable'; await p.advance(10000);
    assert.equal(p.timers.size, 0);
    assert.match(p.el('Status').textContent, /stopped after 1 confirmed.*disk unavailable/);
    assert.equal(p.el('Download').disabled, false);
});

test('phone view and target chooser retain the same tracked selection and cancel old picks', async () => {
    const p = setup(); await settle();
    p.el('Shortcut').fire(); assert.equal(p.state.focused, true);
    p.el('SurveyPick').fire(); const previous = p.state.pick;
    p.el('Target').value = 'bps_group_other'; p.el('Target').fire('change');
    await previous({floor: 'Yard', x_m: 1, y_m: 2});
    assert.equal(posts(p).length, 0);
    assert.equal(p.el('Target').value, 'bps_group_other');
    p.state.target = null; p.controller.sync();
    assert.equal(p.state.focused, false);
});

test('map download reports server failure beside the map and restores its button', async () => {
    const p = setup(); await settle();
    await p.el('Download').fire();
    assert.equal(p.state.downloads, 1);
    assert.match(p.el('Status').textContent, /download started/);
    p.state.downloadError = 'bundle too large';
    await p.el('Download').fire();
    assert.match(p.el('Status').textContent, /Could not download.*bundle too large/);
    assert.equal(p.el('Download').disabled, false);
});

test('a wake lock granted after cancellation is released instead of keeping the screen awake', async () => {
    const p = setup(); await settle(); await chooseSurvey(p);
    let grant; p.state.wakePending = new Promise(resolve => {grant = resolve;});
    p.el('SurveyStart').fire(); p.el('SurveyStop').fire();
    grant(); await settle();
    assert.equal(p.state.wakeRequests, 1);
    assert.equal(p.state.wakeReleases, 1);
    assert.equal(p.timers.size, 0);
});

test('an armed map pick has a visible touch cancel and a cancelled point cannot start a survey', async () => {
    const p = setup(); await settle(); p.el('SurveyPick').fire();
    const oldPick = p.state.pick;
    assert.equal(p.el('SurveyPanel').hidden, false);
    assert.equal(p.el('SurveyStop').disabled, false);
    assert.equal(p.el('SurveyPick').textContent, 'Cancel map pick');
    p.el('SurveyStop').fire();
    await oldPick({floor: 'Yard', x_m: 8, y_m: 9});
    assert.equal(p.el('SurveyStart').disabled, true);
    assert.equal(posts(p).length, 0);
    assert.equal(p.el('SurveyPick').textContent, 'Choose survey spot');
});

test('history shows the server retention policy and supports a later desktop download', async () => {
    const p = setup(); await settle();
    assert.match(p.el('Retention').textContent, /0 snapshots.*30 days, 500 snapshots or 128 MiB.*desktop later/);
    p.state.evicted = 13;
    p.el('Mark').fire(); await p.state.pick({floor: 'Yard', x_m: 3, y_m: 4});
    assert.match(p.el('Retention').textContent, /1 snapshots.*13 older snapshots/);
    assert.equal(p.el('Download').disabled, false);
});
