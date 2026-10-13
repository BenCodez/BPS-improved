const {test} = require('node:test');
const assert = require('node:assert/strict');
const height = require('../../custom_components/bps/frontend/height.js');
const NOW = 1800000000000;
const sensor = 'sensor.synthetic_pressure';
const near = (actual, expected, tolerance = 1e-6) => assert.ok(Math.abs(actual-expected) <= tolerance, `${actual} != ${expected}`);
const samples = (value, method = 'gps', accuracy = .5) => [NOW-30000, NOW].map(timestamp => height.observation(
    {value, accuracy_m: accuracy, timestamp}, method === 'gps' ? 'browser' : sensor, method, NOW));
const summary = (value, method = 'gps', accuracy = .5) => height.summarize(samples(value, method, accuracy), NOW);

test('common-floor calculation cancels absolute GPS datum and adds known phone height plus signed station offset', () => {
    const base = {...summary(100), started: NOW-60000}, station = summary(101);
    const result = height.estimate(base, station, 1.5, .5, NOW);
    near(result.estimate_m, 3); near(result.delta_m, 1); near(result.uncertainty_m, 1);
    near(height.estimate({...base, value: 10000}, {...station, value: 10001}, 1.5, -.5, NOW).estimate_m, 2);
    assert.equal(result.reference, 'common_floor'); assert.equal(result.baseline_age_s, 60);
    assert.equal(result.baseline.sample_count, 2); assert.equal(result.station.reported_accuracy_m, .5);
});

test('pressure ratio gives positive height for lower pressure and retains a precision floor', () => {
    near(height.pressureDelta(1000, 1000), 0);
    const delta = height.pressureDelta(1000, 999.9); assert.ok(delta > .8 && delta < .9);
    const base = {...summary(1000, 'pressure'), started: NOW-60000}, station = summary(999.9, 'pressure');
    const result = height.estimate(base, station, 1.5, .5, NOW);
    near(result.estimate_m, 2+delta, .0005); assert.equal(result.uncertainty_m, 1);
    assert.equal(result.baseline.reported_accuracy_m, null); assert.equal(result.source, sensor);
});

test('height units require known height rather than assuming zero and preserve signed offsets', () => {
    near(height.metres('5', 'ft'), 1.524); near(height.metres('-2', 'ft', true), -.6096);
    near(height.metres('0', 'm'), 0);
    for (const [value, unit, signed] of [['', 'm'], ['  ', 'm'], ['NaN', 'm'], ['11', 'm'], ['-1', 'm'], ['1', 'yards'], ['-11', 'm', true]])
        assert.throws(() => height.metres(value, unit, signed));
});

test('vertical quality is required and horizontal GPS accuracy is never substituted', () => {
    for (const accuracy_m of [null, undefined, NaN, Infinity, 0, -1, '1'])
        assert.equal(height.observation({value: 100, accuracy_m, horizontal_accuracy: .01, timestamp: NOW}, 'browser', 'gps', NOW), null);
    assert.equal(height.observation({value: null, accuracy_m: .5, timestamp: NOW}, 'browser', 'gps', NOW), null);
    const base = {...summary(100, 'gps', 1.1), started: NOW-60000};
    assert.throws(() => height.estimate(base, summary(101, 'gps', 1.1), 1.5, 0, NOW), /exceeds 2 m/);
    const repeated = height.summarize(Array.from({length: 30}, (_, i) => height.observation({value: 100,
        accuracy_m: .8, timestamp: NOW-i*1000}, 'browser', 'gps', NOW)), NOW);
    assert.equal(repeated.reported_accuracy_m, .8, 'correlated fixes do not reduce vertical accuracy');
});

test('stale baselines, stale stations and mixed phone sources cannot produce a height', () => {
    const base = {...summary(1000, 'pressure'), started: NOW-60000}, station = summary(999.99, 'pressure');
    assert.throws(() => height.estimate({...base, started: NOW-900001}, station, 1.5, 0, NOW), /Reference expired/);
    assert.throws(() => height.estimate(base, {...station, timestamp: NOW-60001}, 1.5, 0, NOW), /Station measurement expired/);
    assert.throws(() => height.estimate(base, {...station, source: 'sensor.other_synthetic_phone'}, 1.5, 0, NOW), /exact same phone/);
    assert.throws(() => height.estimate(base, summary(100), 1.5, 0, NOW), /exact same phone/);
    assert.throws(() => height.estimate(base, station, 10, 1, NOW), /outside 0–10/);
    assert.equal(height.observation({value: 1000, timestamp: NOW+1}, sensor, 'pressure', NOW), null);
});

test('fresh distinct measurements must span 20 seconds; unchanged HA tuples never manufacture observations', () => {
    const c = height.collector(sensor, 'pressure', NOW-30000);
    assert.equal(c.add({value: 1000, timestamp: NOW-30000}, NOW), true);
    assert.equal(c.add({value: 1000, timestamp: NOW}, NOW), false);
    assert.throws(() => c.result(NOW), /distinct fresh measurements/);
    assert.equal(c.add({value: 1000.001, timestamp: NOW}, NOW), true);
    assert.equal(c.result(NOW).sample_count, 2);
    assert.equal(c.add({value: 1000.002, timestamp: NOW-30001}, NOW), false, 'pre-capture publication is not a stationary sample');
    const bounded = height.collector('browser', 'gps', NOW-30000);
    for (let i = 0; i < 500; i++) bounded.add({value: 100, accuracy_m: .5, timestamp: NOW-30000+i*50}, NOW);
    assert.equal(bounded.count(), 180);
    assert.equal(bounded.add({value: 100, accuracy_m: .5, timestamp: NOW+20000}, NOW+20000), false, 'collection ends at 45 seconds');
});

test('vertical drift, noisy pressure and known motion reject stationary capture', () => {
    const pressureRows = samples(1000, 'pressure'); pressureRows[1].value = 999.9;
    assert.throws(() => height.summarize(pressureRows, NOW), /changed or was noisy/);
    const gpsRows = samples(100); gpsRows[1].value = 102;
    assert.throws(() => height.summarize(gpsRows, NOW), /changed or was noisy/);
    const moving = samples(100); moving[0].moving = true;
    assert.throws(() => height.summarize(moving, NOW), /changed or was noisy/);
    assert.throws(() => height.summarize(samples(100).map(p => ({...p, timestamp: NOW-60001})), NOW), /stale/);
});

function ui() {
    const ids = new Map(), timers = new Map(), intervals = new Map(), watchers = new Map(), events = new Map();
    let clock = NOW, token = 0, floor = {name: 'Synthetic floor', receivers: [{entity_id: 'synthetic_receiver', height: 1}]};
    let revision = 0, rows = [], pending = null, staged = [], requests = [], dirty = 0;
    function element() {
        const handlers = new Map();
        return {value: '', children: [], disabled: false, textContent: '',
            get options() {return this.children;}, appendChild(child) {this.children.push(child);}, replaceChildren() {this.children = [];},
            addEventListener(event, callback) {handlers.set(event, callback);}, fire(event) {return handlers.get(event)?.();}};
    }
    const document = {getElementById(id) {if (!ids.has(id)) ids.set(id, element()); return ids.get(id);}, createElement: element};
    document.getElementById('heightBase').value = '1.5'; document.getElementById('heightOffset').value = '.5';
    document.getElementById('heightUnit').value = 'm'; document.getElementById('heightSource').value = 'browser';
    const hooks = {getFloor: () => floor, getContext: () => floor.name,
        getTarget: selected => selected === '' ? null : {label: 'Synthetic receiver', revision},
        stageHeight(target, value, survey) {if (target.revision !== revision) return false; staged.push({value, survey}); floor.receivers[0].height = value; return true;},
        redraw() {}, stageChange() {dirty++;}, fetch: async url => {requests.push(url); return pending ? await pending : {ok: true, json: async () => ({sources: rows})};}};
    const api = height.init(hooks, {document, window: {addEventListener: (event, callback) => events.set(event, callback)}, now: () => clock,
        geolocation: {watchPosition(done, failed, options) {assert.deepEqual(options, {enableHighAccuracy: true, maximumAge: 0, timeout: 10000}); watchers.set(++token, {done, failed}); return token;}, clearWatch: id => watchers.delete(id)},
        setTimeout(callback, delay) {timers.set(++token, {callback, delay}); return token;}, clearTimeout: id => timers.delete(id),
        setInterval(callback) {intervals.set(++token, callback); return token;}, clearInterval: id => intervals.delete(id)});
    const el = id => document.getElementById(id), click = id => el(id).fire('click');
    const emit = (value = 100, accuracy = .5, extra = {}) => [...watchers.values()].forEach(w => w.done({coords: {altitude: value, altitudeAccuracy: accuracy, accuracy: .01, ...extra}, timestamp: clock}));
    const browserCapture = (button, value) => {click(button); emit(value); clock += 30000; emit(value); click('heightStop');};
    return {api, el, click, emit, watchers, timers, intervals, staged, requests, floor: () => floor, dirty: () => dirty,
        advance: ms => {clock += ms;}, time: () => clock, changeTarget: () => {revision++;},
        replaceFloor: () => {floor = {...floor, name: 'Other synthetic floor'};}, unload: () => events.get('pagehide')(),
        rows: next => {rows = next;}, defer: promise => {pending = promise;}, tick: async () => {for (const cb of [...intervals.values()]) await cb();},
        base: (value = 100) => browserCapture('heightBaseCapture', value),
        station: (value = 101) => {el('heightReceiver').value = '0'; browserCapture('heightStationCapture', value);}};
}
const row = (pressure = 1000, age_s = 0) => ({entity_id: sensor, name: 'Synthetic phone', kind: 'pressure',
    available: true, pressure_hpa: pressure, age_s, last_updated: 1});
const flush = () => new Promise(resolve => setImmediate(resolve));

test('optional UI requests no permission until capture, requires known phone height and cleans capture resources', () => {
    const p = ui(); assert.equal(p.watchers.size, 0); assert.equal(p.requests.length, 0);
    p.el('heightBase').value = ''; p.click('heightBaseCapture'); assert.equal(p.watchers.size, 0);
    assert.match(p.el('heightStatus').textContent, /known phone height/);
    p.el('heightBase').value = '1.5'; p.base();
    assert.equal(p.watchers.size, 0); assert.equal(p.intervals.size, 0); assert.equal(p.timers.size, 1, 'only session expiry remains');
    assert.equal(p.api.snapshot().baseline.sample_count, 2);
    p.click('heightCancel'); assert.equal(p.timers.size, 0); assert.equal(p.api.snapshot().baseline, null);
});

test('preview preserves prior height; confirmation stages common-floor metres and retains baseline for next receiver', () => {
    const p = ui(); p.base(); p.station();
    assert.equal(p.floor().receivers[0].height, 1); assert.equal(p.staged.length, 0);
    assert.match(p.el('heightStatus').textContent, /3.000 m above the common floor/);
    assert.equal(p.el('heightConfirm').disabled, false); p.click('heightConfirm');
    assert.equal(p.staged.length, 1); assert.equal(p.staged[0].value, 3); assert.equal(p.dirty(), 1);
    assert.ok(p.api.snapshot().baseline); assert.equal(p.el('heightStationCapture').disabled, false);
    p.station(100); p.click('heightConfirm'); assert.equal(p.staged.length, 2); assert.equal(p.staged[1].value, 2);
    assert.equal(p.api.snapshot().confirmed.station_offset_m, .5);
});

test('late automatic stop accepts fresh bounded reference and station samples but rejects stale captures', () => {
    for (const delay of [1, 250, 12000]) {
        const p = ui();
        const automaticCapture = (button, value) => {
            p.click(button); p.emit(value); p.advance(30000); p.emit(value);
            const timer = [...p.timers.values()].find(t => t.delay === 45000);
            assert.ok(timer); p.advance(15000+delay); timer.callback();
        };
        automaticCapture('heightBaseCapture', 100);
        assert.equal(p.api.snapshot().baseline.sample_count, 2);
        p.el('heightReceiver').value = '0'; automaticCapture('heightStationCapture', 101);
        assert.equal(p.el('heightConfirm').disabled, false); p.click('heightConfirm');
        assert.equal(p.staged[0].value, 3);
    }
    const p = ui(); p.click('heightBaseCapture'); p.emit(100); p.advance(30000); p.emit(100);
    const timer = [...p.timers.values()].find(t => t.delay === 45000);
    p.advance(35001); timer.callback();
    assert.equal(p.api.snapshot().baseline, null); assert.match(p.el('heightStatus').textContent, /stale/);
});

test('selected feet display converts to metres at confirmation', () => {
    const p = ui(); p.el('heightUnit').value = 'ft'; p.el('heightBase').value = '5'; p.el('heightOffset').value = '2';
    p.base(); p.station(100); assert.match(p.el('heightStatus').textContent, /7.001 ft \(saved as 2.134 m\)/);
    p.click('heightConfirm'); near(p.staged[0].value, 2.134);
});

test('changed receiver cannot accept preview, stale station cannot overwrite, floor/source changes and exit clear baseline', () => {
    const p = ui(); p.base(); p.station(); p.changeTarget(); p.click('heightConfirm');
    assert.equal(p.staged.length, 0); assert.match(p.el('heightStatus').textContent, /Receiver changed/);
    p.station(); p.advance(60001); p.click('heightConfirm'); assert.equal(p.staged.length, 0);
    assert.match(p.el('heightStatus').textContent, /expired/);
    p.replaceFloor(); p.api.refresh(); assert.equal(p.api.snapshot().baseline, null);
    p.base(); p.el('heightSource').value = sensor; p.el('heightSource').fire('change'); assert.equal(p.api.snapshot().baseline, null);
    p.el('heightSource').value = 'browser'; p.base(); p.unload();
    assert.equal(p.timers.size, 0); assert.equal(p.intervals.size, 0); assert.equal(p.watchers.size, 0); assert.equal(p.api.snapshot().baseline, null);
});

test('bounded reference timer expires and invalidates confirmation', () => {
    const p = ui(); p.base(); p.station();
    const expiry = [...p.timers.values()][0]; assert.equal(expiry.delay, 870000);
    p.advance(900000); expiry.callback(); p.click('heightConfirm');
    assert.equal(p.staged.length, 0); assert.equal(p.api.snapshot().baseline, null);
    assert.match(p.el('heightStatus').textContent, /15 minutes/);
});

test('browser missing vertical accuracy, weak accuracy, permission denial and early stop offer manual fallback', () => {
    const p = ui(); p.click('heightBaseCapture'); p.emit(100, null); p.advance(30000); p.emit(100, null); p.click('heightStop');
    assert.equal(p.api.snapshot().baseline, null); assert.match(p.el('heightStatus').textContent, /manually/);
    p.click('heightBaseCapture'); p.emit(100, 10); p.advance(30000); p.emit(100, 10); p.click('heightStop');
    assert.equal(p.api.snapshot().baseline, null); assert.match(p.el('heightStatus').textContent, /uncertainty exceeds 1 m/);
    p.click('heightBaseCapture'); [...p.watchers.values()][0].failed({message: 'denied'});
    assert.equal(p.watchers.size, 0); assert.equal(p.timers.size, 0); assert.match(p.el('heightStatus').textContent, /denied/);
    p.click('heightBaseCapture'); p.emit(); p.click('heightStop'); assert.equal(p.api.snapshot().baseline, null);
    assert.match(p.el('heightStatus').textContent, /at least 30 seconds/);
});

test('barometer discovery never selects a family phone; exact pressure source is used without browser permission', async () => {
    const p = ui(); p.rows([row(), {...row(), entity_id: 'sensor.other_synthetic_phone'}]); await p.click('heightLoad');
    assert.equal(p.el('heightSource').value, 'browser'); assert.equal(p.watchers.size, 0);
    p.el('heightSource').value = sensor; p.el('heightSource').fire('change');
    p.click('heightBaseCapture'); await flush(); p.advance(30000); p.rows([row(1000.002)]); await p.tick(); p.click('heightStop');
    assert.equal(p.api.snapshot().baseline.source, sensor); assert.equal(p.watchers.size, 0);
    assert.match(p.el('heightStatus').textContent, /raw measurement age is unknown/);
    p.advance(1000); p.el('heightReceiver').value = '0'; p.rows([row(999.99)]); p.click('heightStationCapture'); await flush();
    p.advance(30000); p.rows([row(999.992)]); await p.tick(); p.click('heightStop');
    assert.match(p.el('heightStatus').textContent, /precision is not proven absolute accuracy/);
    assert.match(p.el('heightStatus').textContent, /reference age 61 s/); assert.match(p.el('heightStatus').textContent, /Recheck the reference/);
    p.click('heightConfirm'); assert.equal(p.staged.length, 1); assert.equal(p.staged[0].survey.source, sensor);
    assert.equal(p.staged[0].survey.method, 'pressure'); assert.equal(p.staged[0].survey.baseline.reported_accuracy_m, null);
});

test('unchanged pressure, stale publication and clock skew do not count as new measurements', async () => {
    const p = ui(); p.el('heightSource').value = sensor; p.rows([row()]); p.click('heightBaseCapture'); await flush();
    p.advance(30000); await p.tick(); p.click('heightStop'); assert.equal(p.api.snapshot().baseline, null);
    assert.match(p.el('heightStatus').textContent, /distinct fresh measurements/);
    p.rows([row(1000, 61)]); p.click('heightBaseCapture'); await flush(); p.advance(30000); p.rows([row(1000.001, 61)]); await p.tick(); p.click('heightStop');
    assert.equal(p.api.snapshot().baseline, null);
    p.rows([{...row(), last_updated: NOW/1000+999999}]); p.click('heightBaseCapture'); await flush();
    p.advance(30000); p.rows([{...row(1000.001), last_updated: 1}]); await p.tick(); p.click('heightStop');
    assert.equal(p.api.snapshot().baseline.sample_count, 2, 'HA server age is normalized at request start, not the HA wall clock');
});

test('request duration is included in freshness and delayed responses cannot overwrite completed status', async () => {
    const p = ui(); p.el('heightSource').value = sensor; p.rows([row()]); p.click('heightBaseCapture'); await flush();
    p.advance(30000); p.rows([row(1000.001)]); await p.tick();
    let resolve; p.defer(new Promise(done => {resolve = done;})); const pending = p.tick();
    p.click('heightStop'); const status = p.el('heightStatus').textContent;
    resolve({ok: true, json: async () => ({sources: [row(1000.002)]})}); await pending;
    assert.equal(p.el('heightStatus').textContent, status, 'Stop invalidates in-flight source responses');
    const q = ui(); q.el('heightSource').value = sensor;
    let finish; q.defer(new Promise(done => {finish = done;})); q.click('heightBaseCapture'); q.advance(61000);
    finish({ok: true, json: async () => ({sources: [row(1000)]})}); await flush(); q.click('heightStop');
    assert.equal(q.api.snapshot().baseline, null, 'slow request cannot make an old measurement fresh');
});

test('a stalled source request times out and capture exit releases request timers without waiting for the network', async () => {
    const p = ui(); p.defer(new Promise(() => {}));
    const load = p.click('heightLoad'); const deadline = [...p.timers.values()].find(t => t.delay === 10000);
    assert.ok(deadline); deadline.callback(); await load;
    assert.match(p.el('heightStatus').textContent, /timed out/); assert.equal(p.timers.size, 0);
    p.defer(null); p.rows([row()]); await p.click('heightLoad'); assert.equal(p.el('heightSource').options.length, 2, 'retry recovers discovery');
    p.el('heightSource').value = sensor; p.defer(new Promise(() => {})); p.click('heightBaseCapture');
    assert.equal(p.timers.size, 2, 'capture and request have separate bounded deadlines');
    p.unload(); assert.equal(p.timers.size, 0); assert.equal(p.intervals.size, 0); await flush();
    assert.equal(p.api.snapshot().baseline, null); assert.match(p.el('heightStatus').textContent, /Page left/);
});
