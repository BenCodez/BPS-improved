const {test} = require('node:test');
const assert = require('node:assert/strict');
const gps = require('../../custom_components/bps/frontend/gps.js');
const flush = () => new Promise(r => setImmediate(r));
const NOW = 1800000000000;

function ui() {
    const ids = new Map(), timers = new Map(), watchers = new Map(), windowEvents = new Map(), documentEvents = new Map();
    let clock = NOW, counter = 0, context = 'synthetic-context', pendingPoll = null, pendingStart = null;
    let candidates = [], notify = [], saveCalls = [], requests = [], pollCount = 0, pollActive = 0, maxPollActive = 0;
    function element() {
        const events = new Map();
        return {value: '', checked: false, disabled: false, textContent: '', children: [],
            replaceChildren(...rows) {this.children = rows;}, appendChild(row) {this.children.push(row);},
            addEventListener(name, fn) {events.set(name, fn);}, fire(name, event) {return events.get(name)?.(event);}};
    }
    const document = {hidden: false, getElementById(id) {if (!ids.has(id)) ids.set(id, element()); return ids.get(id);},
        createElement: element, addEventListener(name, fn) {documentEvents.set(name, fn);}};
    const floor = {name: 'Synthetic floor'};
    let pollData = {capturing: true, receiver: 'synthetic_receiver', elapsed_s: 2, sample_count: 1, status: 'Stand still'};
    const hooks = {getContext: () => context, getFloor: () => floor, getBounds: () => ({width: 2000, height: 1000}),
        surveyCanStart: () => true, redraw() {}, surveySaved: async (candidate, response, capturedUIContext) => {saveCalls.push([candidate, response, capturedUIContext]);},
        fetch: async (url, options) => {
            assert.equal(url, '/api/bps/phone_survey');
            const body = options?.body ? JSON.parse(options.body) : null; requests.push(body);
            if (!body) return {ok: true, json: async () => ({trackers: [{key: 'synthetic_phone', name: 'Synthetic phone'}], candidates, notify_services: notify})};
            if (body.action === 'start') return pendingStart ? await pendingStart : {ok: true, json: async () => ({session: 'synthetic-session'})};
            if (body.action === 'poll') {
                pollCount++; pollActive++; maxPollActive = Math.max(maxPollActive, pollActive);
                const response = pendingPoll ? await pendingPoll : {ok: true, json: async () => pollData}; pollActive--; return response;
            }
            if (body.action === 'verify') return {ok: true, json: async () => ({saved: true, candidate: {...candidates[0], status: 'verified'}, position: candidates[0].position})};
            return {ok: true, json: async () => ({stopped: true, removed: true})};
        }};
    const api = gps.surveyController(hooks, {document, window: {addEventListener(name, fn) {windowEvents.set(name, fn);}}, now: () => clock,
        setTimeout(fn) {timers.set(++counter, fn); return counter;}, clearTimeout(id) {timers.delete(id);},
        geolocation: {watchPosition(done, fail, options) {assert.equal(options.maximumAge, 0); watchers.set(++counter, done); return counter;}, clearWatch(id) {watchers.delete(id);}}});
    const el = id => document.getElementById(id);
    return {api, hooks, el, requests, timers, watchers, saveCalls, click: id => el(id).fire('click'),
        select: () => {el('gpsSurveyPhone').value = 'synthetic_phone';},
        emit: (timestamp = clock) => [...watchers.values()].forEach(done => done({coords: {latitude: 10, longitude: 20, accuracy: 2}, timestamp})),
        tick: async () => {clock += 2000; const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(fn => fn()); await flush();},
        hide: () => {document.hidden = true; documentEvents.get('visibilitychange')();},
        unload: () => windowEvents.get('pagehide')(), changeFloor: () => {context = 'replacement-context';},
        pollData: data => {pollData = data;}, candidates: rows => {candidates = rows;}, services: rows => {notify = rows;},
        deferPoll: promise => {pendingPoll = promise;}, deferStart: promise => {pendingStart = promise;},
        pollCount: () => pollCount, maxPollActive: () => maxPollActive};
}

const candidate = () => ({id: 'synthetic-candidate', receiver: 'synthetic_receiver', floor: 'Synthetic floor', phone: 'synthetic_phone', status: 'pending',
    position: {x: 300, y: 300}, map_accuracy_m: 4, gps: {sample_count: 16, reported_accuracy_m: 2, spread_m: 1}});

test('auto survey is opt-in and loads BLE phones and services without guessing selections', async () => {
    const p = ui(); assert.equal(p.requests.length, 0); assert.equal(p.watchers.size, 0);
    await p.click('gpsSurveyStart'); assert.equal(p.requests.length, 0); assert.match(p.el('gpsSurveyStatus').textContent, /Select your own/);
    p.services([{key: 'mobile_app_synthetic_phone', name: 'notify.mobile_app_synthetic_phone'}]);
    await p.click('gpsSurveyRefresh'); assert.equal(p.el('gpsSurveyPhone').value, ''); assert.equal(p.el('gpsSurveyNotify').value, '');
    p.select(); await p.click('gpsSurveyStart'); assert.equal(p.watchers.size, 1); assert.equal(p.api.snapshot().active, true);
    p.emit(); await p.tick(); const upload = p.requests.filter(r => r?.action === 'poll').at(-1);
    assert.equal(upload.browser_samples[0].timestamp, NOW); assert.equal(upload.browser_samples[0].accuracy_m, 2);
    assert.equal(p.saveCalls.length, 0);
});

test('a slow poll never overlaps or creates queued recurring polls', async () => {
    const p = ui(); p.select(); let resolve;
    p.deferPoll(new Promise(r => {resolve = r;})); const started = p.click('gpsSurveyStart'); await flush();
    assert.equal(p.pollCount(), 1); assert.equal(p.timers.size, 0);
    await p.tick(); await p.tick(); assert.equal(p.pollCount(), 1);
    resolve({ok: true, json: async () => ({capturing: true, elapsed_s: 2, sample_count: 1})}); await started;
    assert.equal(p.timers.size, 1); assert.equal(p.maxPollActive(), 1);
    p.click('gpsSurveyStop'); assert.equal(p.timers.size, 0);
});

for (const cancel of ['hide', 'unload']) test(`${cancel} stops watches/polling and late poll cannot retain a candidate`, async () => {
    const p = ui(); p.select(); let resolve;
    p.deferPoll(new Promise(r => {resolve = r;})); const start = p.click('gpsSurveyStart'); await flush();
    p[cancel](); assert.equal(p.watchers.size, 0); assert.equal(p.timers.size, 0);
    resolve({ok: true, json: async () => ({candidate: candidate()})}); await start;
    assert.equal(p.api.snapshot().active, false); assert.equal(p.api.snapshot().candidate_count, 0);
    assert.ok(p.requests.some(r => r?.action === 'stop'));
});

test('stop during start awaits cleanup without letting the late start create a GPS watch', async () => {
    const p = ui(); p.select(); let resolve;
    p.deferStart(new Promise(r => {resolve = r;})); const start = p.click('gpsSurveyStart'); await flush(); p.click('gpsSurveyStop');
    resolve({ok: true, json: async () => ({session: 'synthetic-late-session'})}); await start;
    assert.equal(p.watchers.size, 0); assert.equal(p.pollCount(), 0); assert.equal(p.api.snapshot().active, false);
    assert.ok(p.requests.some(r => r?.action === 'stop' && r.session === 'synthetic-late-session'));
});

test('changed floor stops before polling and pending candidates require explicit verification', async () => {
    const p = ui(); p.select(); await p.click('gpsSurveyStart'); p.changeFloor(); await p.tick();
    assert.equal(p.api.snapshot().active, false); assert.equal(p.pollCount(), 1); assert.equal(p.watchers.size, 0);
    p.candidates([candidate()]); await p.click('gpsSurveyRefresh');
    assert.equal(p.saveCalls.length, 0); const row = p.el('gpsSurveyCandidates').children[0];
    assert.match(row.children[0].textContent, /reported accuracy 2.0 m.*spread 1.0 m/);
    await row.children[1].fire('click'); assert.equal(p.saveCalls.length, 1);
    assert.equal(p.saveCalls[0][2], 'replacement-context');
    assert.equal(p.requests.filter(r => r?.action === 'verify').length, 1);
    assert.match(p.el('gpsSurveyStatus').textContent, /verified and saved/);
});

test('GPS request option is explicit and changing it stops future requests', async () => {
    const p = ui(); p.select(); p.el('gpsSurveyRequestGPS').checked = true; p.el('gpsSurveyNotify').value = 'mobile_app_synthetic_phone';
    await p.click('gpsSurveyStart'); const start = p.requests.find(r => r?.action === 'start');
    assert.equal(start.request_gps, true); assert.equal(start.notify_service, 'mobile_app_synthetic_phone');
    p.el('gpsSurveyRequestGPS').checked = false; p.el('gpsSurveyRequestGPS').fire('change');
    assert.equal(p.api.snapshot().active, false); await p.tick(); assert.equal(p.pollCount(), 1);
});

test('unsaved editor guards are reported and candidate verification cannot overwrite a newer draft', async () => {
    const p = ui(); p.select(); p.hooks.surveyCanStart = () => {throw new Error('Save floor edits first.');};
    await p.click('gpsSurveyStart'); assert.equal(p.requests.length, 0); assert.match(p.el('gpsSurveyStatus').textContent, /Save floor edits first/);
    p.candidates([candidate()]); await p.click('gpsSurveyRefresh');
    await p.el('gpsSurveyCandidates').children[0].children[1].fire('click');
    assert.equal(p.saveCalls.length, 0); assert.equal(p.requests.filter(r => r?.action === 'verify').length, 0);
});

test('verification passes the pre-request editor context and retains the saved result if newer edits block a merge', async () => {
    const p = ui(); p.candidates([candidate()]); await p.click('gpsSurveyRefresh');
    p.hooks.surveySaved = async (c, response, capturedUIContext) => {
        assert.equal(capturedUIContext, 'synthetic-context'); assert.equal(response.saved, true);
        p.changeFloor(); assert.notEqual(capturedUIContext, p.hooks.getContext());
        throw new Error('Position saved; your newer map edits were retained. Reload after saving or discarding them.');
    };
    await p.el('gpsSurveyCandidates').children[0].children[1].fire('click');
    assert.match(p.el('gpsSurveyStatus').textContent, /Position saved; your newer map edits were retained/);
    assert.match(p.el('gpsSurveyCandidates').children[0].children[0].textContent, /verified and saved/);
});

test('an explicitly selected placed receiver is sent for colocated stations and changing it stops capture', async () => {
    const p = ui(); p.select(); p.hooks.getFloor().receivers = [{entity_id: 'synthetic_colocated_receiver'}];
    p.el('gpsReceiver').value = '0'; await p.click('gpsSurveyStart');
    assert.equal(p.requests.find(r => r?.action === 'start').receiver, 'synthetic_colocated_receiver');
    p.el('gpsReceiver').value = ''; p.el('gpsReceiver').fire('change');
    assert.equal(p.api.snapshot().active, false); assert.equal(p.watchers.size, 0);
});
