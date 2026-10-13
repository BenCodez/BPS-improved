const {test} = require('node:test');
const assert = require('node:assert/strict');
const gps = require('../../custom_components/bps/frontend/gps.js');
// All locations are synthetic; this origin has no relationship to a user.
const origin = {latitude: 10, longitude: 20};
const now = 1800000000000;
const at = (x, y, accuracy_m = 1, timestamp = now, source = 'browser') => ({...gps.fromLocal({x, y}, origin), accuracy_m, timestamp, source});
const near = (actual, expected, tolerance = 1e-6) => assert.ok(Math.abs(actual-expected) <= tolerance, `${actual} != ${expected}`);
const samples = (x, y, accuracy = 1, source = 'browser') => [at(x, y, accuracy, now-30000, source), at(x, y, accuracy, now, source)];
const anchors = () => [[0, 0], [30, 0], [0, 30]].map(([x, y]) => ({...at(x, y, 1), x: 200+x*20, y: 150+y*20, label: 'Synthetic anchor'}));

test('GPS similarity fit handles north-down pixels, rotation, translation and independent residual', () => {
    const a = anchors();
    const theta = .7, c = Math.cos(theta), s = Math.sin(theta);
    for (const p of a) {const local = gps.local(p, origin); p.x = 900+12*(c*local.x-s*local.y); p.y = 100+12*(s*local.x+c*local.y);}
    const fit = gps.fit(a), projected = gps.project(fit, at(8, 9));
    near(fit.scale, 12, 1e-6); near(fit.residual_m, 0, 1e-6); assert.equal(fit.independently_checked, true);
    near(projected.x, 900+12*(c*8-s*9), 1e-6); near(projected.y, 100+12*(s*8+c*9), 1e-6);
    assert.ok(projected.accuracy_m > 2, 'anchor errors remain in preview uncertainty');
});

test('two anchors fit exactly but cannot independently validate alignment', () => {
    const fit = gps.fit(anchors().slice(0, 2));
    near(fit.scale, 20); near(fit.residual_m, 0); assert.equal(fit.independently_checked, false);
});

test('third anchor exposes disagreement and blocks inconsistent GPS placement', () => {
    const a = anchors(); a[2].x += 700;
    const fit = gps.fit(a); assert.ok(fit.residual_m > 5); assert.equal(fit.consistent, false);
    assert.throws(() => gps.project(fit, at(5, 5)), /disagree/);
});

test('dateline-aware local geometry and property-size/polar/finite guards', () => {
    const ref = {latitude: 10, longitude: 179.999};
    const farSide = {latitude: 10, longitude: -179.999};
    assert.ok(gps.local(farSide, ref).x > 200 && gps.local(farSide, ref).x < 230);
    const f = gps.fit([{...ref, x: 100, y: 100, accuracy_m: 1}, {...farSide, x: 500, y: 100, accuracy_m: 1}]);
    assert.ok(f.scale > 1);
    for (const p of [{latitude: 90, longitude: 20}, {latitude: NaN, longitude: 20}, {latitude: 10, longitude: Infinity}])
        assert.throws(() => gps.local(p, origin), /finite latitude/);
    assert.throws(() => gps.local({latitude: 11, longitude: 20}, origin), /5 km/);
});

test('coincident, duplicate, invalid and collapsed map fits cannot place receivers', () => {
    const a = anchors();
    assert.throws(() => gps.fit([]), /2–12/);
    assert.throws(() => gps.fit([...a, {...a[0]}]), /distinct/);
    assert.throws(() => gps.fit(a.map(p => ({...p, x: 100, y: 100}))), /distinct/);
    assert.throws(() => gps.fit([{...a[0], latitude: '10'}, a[1]]), /invalid/);
    assert.throws(() => gps.fit(a.map(p => ({...p, accuracy_m: Infinity}))), /invalid/);
    assert.throws(() => gps.fit([...a, ...a, ...a, ...a, a[0]]), /2–12/);
});

test('robust stationary median retains spread and reported accuracy without sqrt(N)', () => {
    const rows = samples(0, 0, 1);
    for (let i = 0; i < 60; i++) rows.push(at(i%2 ? 2 : -2, 0, 1, now-i*500));
    const result = gps.summarize(rows, now);
    near(gps.local(result, origin).x, 0, 1e-6);
    near(result.spread_m, 2, 1e-6); near(result.accuracy_m, 2, 1e-6);
    const tight = gps.summarize(samples(0, 0, 4), now); assert.equal(tight.accuracy_m, 4);
    assert.equal(result.sample_count, 62); assert.equal(result.source, 'browser');
});

test('automatic preference considers only provided selected sources, accuracy and freshness', () => {
    const browser = samples(0, 0, 4), phone = samples(0.5, 0, 1, 'device_tracker.synthetic_phone');
    const chosen = gps.choose([browser, phone], now);
    assert.equal(chosen.source, 'device_tracker.synthetic_phone'); assert.equal(chosen.accuracy_m, 1);
    assert.equal(chosen.compared_sources.length, 2);
    assert.equal(gps.choose([browser, phone.map(p => ({...p, timestamp: now-70000}))], now).source, 'browser');
    const noisyPhone = [...phone, at(3, 0, 1, now-15000, 'device_tracker.synthetic_phone')];
    assert.ok(gps.choose([browser, noisyPhone], now).accuracy_m >= 2.49);
});

test('movement, mutually inconsistent phones and old/single cached fixes are rejected', () => {
    assert.throws(() => gps.choose([samples(0, 0, 1), [at(0, 0, 1, now-30000), at(20, 0, 1)]], now), /moved or jumped/);
    assert.throws(() => gps.choose([samples(0, 0, 1), samples(30, 0, 1, 'device_tracker.synthetic_phone')], now), /disagree/);
    assert.throws(() => gps.summarize([at(0, 0)], now), /distinct GPS fixes/);
    assert.throws(() => gps.summarize(samples(0, 0).map(p => ({...p, timestamp: now-70000})), now), /stale/);
    assert.equal(gps.observation(at(0, 0, 1, now+1), 'browser', now), null);
    assert.equal(gps.observation(at(0, 0, 0), 'browser', now), null);
    assert.equal(gps.observation(at(0, 0, NaN), 'browser', now), null);
});

test('movement from a late phone source blocks placement even before its observation span is eligible', () => {
    const phone = 'device_tracker.synthetic_phone';
    for (const distance of [200, 10000]) {
        const late = [at(0, 0, 1, now-10000, phone), at(distance, 0, 1, now, phone)];
        assert.throws(() => gps.choose([samples(0, 0, 1), late], now), /moved or jumped/);
    }
    const lateStationary = [at(0, 0, 1, now-10000, phone), at(.5, 0, 1, now, phone)];
    assert.equal(gps.choose([samples(0, 0, 1), lateStationary], now).source, 'browser');
});

test('collector deduplicates source/timestamp and bounds observations', () => {
    const c = gps.collector();
    assert.equal(c.add(at(0, 0, 1, now-30000), 'browser', now), true);
    assert.equal(c.add(at(0, 0, 1, now-30000), 'browser', now), false);
    assert.equal(c.add(at(0, 0, 1, now-30000), 'device_tracker.synthetic_phone', now), true);
    for (let i = 0; i < 500; i++) c.add(at(0, 0, 1, now-i), 'browser', now);
    assert.equal(c.count(), 181);
    assert.ok(c.result(now).accuracy_m >= 1);
});

test('unchanged HA coordinates never count as new GPS fixes when unrelated attributes update', () => {
    const c = gps.collector(), phone = 'device_tracker.synthetic_phone';
    c.add(at(0, 0, 1, now-30000), phone, now);
    assert.equal(c.add(at(0, 0, 1, now), phone, now), false);
    assert.throws(() => c.result(now), /distinct GPS fixes/);
    assert.equal(c.add(at(.5, 0, 1, now), phone, now), true);
    const result = c.result(now); assert.equal(result.timestamp_kind, 'ha_state_update'); assert.equal(result.accuracy_m, 1);
});

function ui() {
    const ids = new Map(), windowEvents = new Map(), timers = new Map(), intervals = new Map();
    const watchers = new Map(), cleared = []; let clock = now, token = 0, requests = [], staged = [];
    function element() {
        const events = new Map();
        return {value: '', children: [], disabled: false, validity: {}, textContent: '',
            get options() {return this.children;}, appendChild(e) {this.children.push(e);}, replaceChildren(...rows) {this.children = rows;},
            addEventListener(name, f) {events.set(name, f);}, fire(name) {return events.get(name)?.();}};
    }
    const document = {getElementById(id) {if (!ids.has(id)) ids.set(id, element()); return ids.get(id);}, createElement: element};
    document.getElementById('gpsPhone').value = '';
    let floor = {name: 'Synthetic property', scale: 20, receivers: [{entity_id: 'synthetic_station', cords: {x: 100, y: 100}}],
        georeference: {version: 1, anchors: anchors()}};
    let targetRevision = 0, pointCallback = null;
    let response = {locations: []}, deferred = null;
    const hooks = {getFloor: () => floor, getContext: () => JSON.stringify(floor), getBounds: () => ({width: 2000, height: 1000}),
        redraw() {}, stageChange() {}, getTarget: choice => choice === '' ? null : ({label: 'Synthetic station', revision: targetRevision}),
        stageReceiver(target, point) {if (target.revision !== targetRevision) return false; staged.push(point); floor.receivers[0].cords = point; return true;},
        pickMapPoint(callback) {pointCallback = callback; return () => {pointCallback = null;};},
        fetch: async url => {requests.push(url); return deferred ? await deferred : {ok: true, json: async () => response};}};
    const api = gps.init(hooks, {document, window: {addEventListener(name, cb) {windowEvents.set(name, cb);}}, now: () => clock,
        geolocation: {watchPosition(done, failed, options) {assert.equal(options.enableHighAccuracy, true); watchers.set(++token, {done, failed}); return token;},
            clearWatch(id) {cleared.push(id); watchers.delete(id);}},
        setTimeout(cb) {timers.set(++token, cb); return token;}, clearTimeout(id) {timers.delete(id);},
        setInterval(cb) {intervals.set(++token, cb); return token;}, clearInterval(id) {intervals.delete(id);}});
    const emit = (x = 5, y = 5, accuracy = 1, timestamp = clock) => [...watchers.values()].forEach(w => w.done({coords: {...at(x, y), accuracy}, timestamp}));
    const click = id => document.getElementById(id).fire('click');
    const capture = () => {click('gpsCapture'); emit(); clock += 30000; emit(); click('gpsStop');};
    return {api, click, el: id => document.getElementById(id), emit, capture, watchers, cleared, timers, intervals, staged, requests,
        clock: ms => {clock += ms;}, currentTime: () => clock, tick: async () => {for (const cb of [...intervals.values()]) await cb();},
        replaceFloor: () => {floor = {...floor, name: 'Other synthetic floor'};}, editFloor: () => {floor.receivers[0].cords.x += 1;},
        changeTarget: () => {targetRevision++;}, point: p => pointCallback?.(p), unload: () => windowEvents.get('pagehide')(),
        response: rows => {response = {locations: rows};}, defer: promise => {deferred = promise;}};
}

test('browser permissions start only on explicit capture and stop clears watches/timers', () => {
    const p = ui(); assert.equal(p.watchers.size, 0); assert.equal(p.requests.length, 0);
    p.capture(); assert.equal(p.watchers.size, 0); assert.equal(p.timers.size, 0); assert.equal(p.intervals.size, 0);
    assert.equal(p.cleared.length, 1); assert.match(p.el('gpsStatus').textContent, /reported accuracy 1.0 m/);
    assert.equal(p.api.snapshot().chosen_gps.sample_count, 2);
});

test('GPS preview displays conservative radius and stages only after confirmation', () => {
    const p = ui(); p.capture(); p.el('gpsReceiver').value = '0'; p.click('gpsPreview');
    assert.equal(p.staged.length, 0); assert.equal(p.el('gpsConfirm').disabled, false);
    const circles = []; p.api.draw(new Proxy({}, {get: (o, k) => k === 'arc' ? (...args) => circles.push(args) : (() => {}), set: () => true}), 2);
    assert.equal(circles.length, 2); assert.ok(circles[0][2] > 40);
    p.click('gpsConfirm'); assert.equal(p.staged.length, 1); near(p.staged[0].x, 300); near(p.staged[0].y, 250);
    assert.equal(p.api.snapshot().chosen_gps.source, 'browser');
});

test('capture refuses early stop, stale fix and missing target', () => {
    const p = ui(); p.click('gpsCapture'); p.emit(); p.click('gpsStop'); assert.match(p.el('gpsStatus').textContent, /at least 30 seconds/);
    p.capture(); p.click('gpsPreview'); assert.match(p.el('gpsStatus').textContent, /Select a receiver/);
    p.el('gpsReceiver').value = '0'; p.clock(60001); p.click('gpsPreview'); assert.match(p.el('gpsStatus').textContent, /stale/);
});

test('floor edits and identity changes invalidate asynchronous placement instead of moving wrong station', async () => {
    const p = ui(); p.click('gpsCapture'); p.emit(); p.editFloor(); await p.tick();
    assert.equal(p.watchers.size, 0); assert.equal(p.api.snapshot().chosen_gps, undefined);
    p.capture(); p.el('gpsReceiver').value = '0'; p.click('gpsPreview'); p.changeTarget(); p.click('gpsConfirm');
    assert.equal(p.staged.length, 0); assert.match(p.el('gpsStatus').textContent, /changed/);
    p.capture(); p.click('gpsPreview'); p.replaceFloor(); p.click('gpsConfirm'); assert.equal(p.staged.length, 0);
});

test('changing source/unload cancels capture and late callbacks cannot repopulate fixes', () => {
    const p = ui(); p.click('gpsCapture'); const callback = [...p.watchers.values()][0].done;
    p.el('gpsPhone').value = 'device_tracker.synthetic_phone'; p.el('gpsPhone').fire('change');
    callback({coords: {...at(0, 0), accuracy: 1}, timestamp: p.currentTime()});
    assert.equal(p.watchers.size, 0); assert.equal(p.api.snapshot().chosen_gps, undefined);
    p.click('gpsCapture'); p.unload(); assert.equal(p.timers.size, 0); assert.equal(p.intervals.size, 0);
});

test('phone selection stays explicit and late HA poll after cancellation is discarded', async () => {
    const p = ui(); let resolve;
    p.response([{entity_id: 'device_tracker.synthetic_other_person', name: 'Other synthetic phone'}]);
    await p.click('gpsRefreshPhones'); assert.equal(p.el('gpsPhone').value, '');
    p.el('gpsPhone').value = 'device_tracker.synthetic_phone';
    p.defer(new Promise(r => {resolve = r;})); p.click('gpsCapture'); p.click('gpsCancel');
    resolve({ok: true, json: async () => ({locations: [{entity_id: 'device_tracker.synthetic_phone', ...at(0, 0), gps_accuracy: 1, last_updated: now/1000, age_s: 0}]})});
    await new Promise(r => setImmediate(r)); assert.equal(p.api.snapshot().chosen_gps, undefined); assert.equal(p.watchers.size, 0);
});

for (const skew of [-120000, 120000]) test(`HA capture uses server age when browser clock differs by ${skew/1000} seconds`, async () => {
    const p = ui(), phone = 'device_tracker.synthetic_phone';
    p.clock(skew); p.el('gpsPhone').value = phone;
    p.response([{entity_id: phone, ...at(5, 5), gps_accuracy: 1, last_updated: now/1000-3, age_s: 3}]);
    p.click('gpsCapture'); p.emit(5, 5, 4); await new Promise(r => setImmediate(r));
    p.clock(30000);
    p.response([{entity_id: phone, ...at(5.5, 5), gps_accuracy: 1, last_updated: now/1000+27, age_s: 3}]);
    await p.tick(); await new Promise(r => setImmediate(r)); p.emit(5, 5, 4); p.click('gpsStop');
    const chosen = p.api.snapshot().chosen_gps;
    assert.equal(chosen.source, phone); assert.equal(chosen.sample_count, 2);
    near(chosen.age_s, 3); assert.equal(chosen.timestamp_kind, 'ha_state_update');
});

test('HA response latency counts toward freshness instead of renewing an old sample', async () => {
    const p = ui(); let resolve; p.el('gpsPhone').value = 'device_tracker.synthetic_phone';
    p.defer(new Promise(r => {resolve = r;})); p.click('gpsCapture'); p.clock(61000);
    resolve({ok: true, json: async () => ({locations: [{entity_id: 'device_tracker.synthetic_phone', ...at(5, 5),
        gps_accuracy: 1, last_updated: p.currentTime()/1000, age_s: 0}]})});
    await new Promise(r => setImmediate(r)); p.click('gpsStop');
    assert.equal(p.api.snapshot().chosen_gps, undefined);
    assert.match(p.el('gpsStatus').textContent, /No fresh GPS observations/);
});

test('manual anchor staging is bounded and independent of floor scale/receiver positions', () => {
    const p = ui(); p.el('gpsReceiver').value = '0'; p.click('gpsUseReceiver');
    p.el('gpsLatitude').value = String(at(10, 10).latitude); p.el('gpsLongitude').value = String(at(10, 10).longitude);
    p.el('gpsAccuracy').value = '0'; p.el('gpsAnchorLabel').value = 'Synthetic measured corner';
    p.click('gpsAddAnchor'); assert.match(p.el('gpsStatus').textContent, /staged/); assert.equal(p.staged.length, 0);
    p.click('gpsPickPoint'); p.point({x: 20, y: 20}); p.el('gpsLatitude').value = ''; p.click('gpsAddAnchor');
    assert.match(p.el('gpsStatus').textContent, /Enter GPS/);
});
