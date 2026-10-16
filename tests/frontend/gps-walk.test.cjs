const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {readFileSync} = require('node:fs');

function setup() {
    const nodes = new Map(), listeners = new Map(), timers = new Map(), saves = [], watches = new Map(), cleared = [];
    let serial = 0;
    for (const key of ['Start', 'Stop', 'Mode', 'Distance', 'Status']) nodes.set('liveGPS' + key, {
        disabled: false, value: '', textContent: '', handlers: {},
        addEventListener(type, fn) {this.handlers[type] = fn;}, fire() {return this.handlers.click();},
    });
    const el = key => nodes.get('liveGPS' + key);
    el('Mode').value = 'walk'; el('Distance').value = '5';
    const state = {now: 100000, monotonic: 50000, target: 'dog', context: 'saved-a', busy: false,
        valid: true, pending: null, fail: false, wakeReleases: 0};
    const doc = {hidden: false, getElementById: id => nodes.get(id), addEventListener: (event, fn) => listeners.set(event, fn)};
    const geo = {watchPosition(ok, error, options) {watches.set(++serial, {ok, error, options}); return serial;},
        clearWatch(id) {cleared.push(id); watches.delete(id);}};
    const sandbox = vm.createContext({Date, Math, console,
        navigator: {wakeLock: {request: async () => ({release: async () => {state.wakeReleases++;}})}}});
    for (const file of ['gps.js', 'gps-walk.js']) vm.runInContext(readFileSync('custom_components/bps/frontend/' + file, 'utf8'), sandbox);
    const origin = {latitude: 10, longitude: 20};
    const fix = (x=0, y=0, extra={}) => ({...sandbox.BPSGPS.fromLocal({x, y}, origin), accuracy_m: 2, ...extra});
    const map = {name: 'Garden', scale: 10, layout_revision: 'c'.repeat(64), georeference: {version: 1, anchors: [[0,0],[30,0],[0,30]].map(([x,y]) => ({...fix(x,y), x:x*10, y:y*10, label:'Synthetic'}))}};
    const ctl = sandbox.BPSGPSWalk.init({target: () => state.target, context: () => state.context, map: () => map,
        label: () => 'Walk', busy: () => state.busy, changed() {}, beforeStart() {},
        validate() {if (!state.valid) throw new Error('Return to live tracking');},
        async save(...args) {saves.push(args); if (state.pending) await state.pending;
            if (state.fail) throw new Error('disk failure'); return {accuracy_m: 14};},
    }, {document: doc, geolocation: geo, now: () => state.now, monotonic: () => state.monotonic,
        setTimeout: fn => {timers.set(++serial, fn); return serial;}, clearTimeout: id => timers.delete(id)});
    return {el, state, ctl, saves, watches, cleared, doc, map,
        async emit(x=0, y=0, changes={}) {
            const f = fix(x,y), position = {timestamp: state.now, coords: {latitude: f.latitude, longitude:f.longitude, accuracy:2, speed:1, heading:80}};
            Object.assign(position, changes); const handler = [...watches.values()][0];
            handler?.ok(position); await settle();
        }, advance(ms) {state.now += ms; state.monotonic += ms;},
        heartbeat() {for (const [id, fn] of [...timers]) {timers.delete(id); fn();}},
        hide() {doc.hidden = true; listeners.get('visibilitychange')();},
    };
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test('moving GPS snapshots retain distinct coordinates and timestamps, at most every 15 seconds', async () => {
    const p = setup(); p.el('Start').fire(); await p.emit(3,4);
    assert.equal(p.saves.length, 1);
    p.advance(1000); await p.emit(4,4); assert.equal(p.saves.length, 1);
    p.advance(14000); await p.emit(10,4); assert.equal(p.saves.length, 2);
    assert.notEqual(p.saves[0][4].longitude, p.saves[1][4].longitude);
    assert.equal(p.saves[0][4].timestamp_ms, 100000);
    assert.equal(p.saves[1][4].timestamp_ms, 115000);
    assert.equal(p.saves[0][4].session_id, p.saves[1][4].session_id);
    assert.equal(p.saves[0][4].dog_distance_m, 5);
    assert.match(p.el('Status').textContent, /2 GPS\/BLE snapshots saved/);
    assert.equal([...p.watches.values()][0].options.maximumAge, 0);
    p.el('Stop').fire(); await settle();
    assert.equal(p.state.wakeReleases, 1);
    assert.equal(p.cleared.length, 1);
});

test('stale fixes, duplicate timestamps, poor accuracy and excessive speed are not reused', async () => {
    const p = setup(); p.el('Start').fire();
    await p.emit(0,0,{timestamp:95000}); assert.equal(p.saves.length,0);
    await p.emit(0,0,{coords:{accuracy:50}}); assert.equal(p.saves.length,0);
    await p.emit(0,0); assert.equal(p.saves.length,1);
    p.advance(15000); await p.emit(0,0,{timestamp:100000}); assert.equal(p.saves.length,1);
    await p.emit(0,0,{coords:{accuracy:2, speed:4}}); assert.equal(p.saves.length,1);
    p.heartbeat(); assert.match(p.el('Status').textContent, /Waiting for fresh/);
});

test('a GPS jump pauses capture, while slow ride accepts movement under its own speed bound', async () => {
    const p = setup(); p.el('Mode').value = 'ride'; p.el('Start').fire();
    await p.emit(); p.advance(15000);
    await p.emit(500,0); assert.equal(p.saves.length,1);
    assert.match(p.el('Status').textContent,/jumped/);
    await p.emit(100,0); assert.equal(p.saves.length,2);
    assert.equal(p.saves[1][4].mode,'ride');
});

for (const kind of ['target','context','live','hidden']) test(`changing ${kind} stops collection and late callbacks`, async () => {
    const p = setup(); p.el('Start').fire(); const late = [...p.watches.values()][0].ok;
    if (kind==='target') p.state.target='other';
    if (kind==='context') p.state.context='changed';
    if (kind==='live') p.state.valid=false;
    if (kind==='hidden') p.hide(); else p.ctl.sync();
    await late({}); assert.equal(p.saves.length,0); assert.equal(p.ctl.isActive(),false);
    assert.equal(p.watches.size,0);
});

test('stop during a save does not schedule another point and denied permission stops the watch', async () => {
    const p = setup(); let finish; p.state.pending=new Promise(r=>finish=r);
    p.el('Start').fire(); await p.emit(); p.el('Stop').fire(); finish(); await settle();
    assert.equal(p.saves.length,1); assert.equal(p.ctl.isActive(),false);
    p.state.pending=null; p.el('Start').fire(); [...p.watches.values()][0].error({code:1});
    assert.equal(p.watches.size,0); assert.match(p.el('Status').textContent,/permission denied/);
});

test('save failure and 30-minute limit stop collection, without inventing missed points', async () => {
    const p=setup(); p.el('Start').fire(); p.state.fail=true; await p.emit();
    assert.equal(p.ctl.isActive(),false); assert.match(p.el('Status').textContent,/not confirmed.*disk failure/);
    p.state.fail=false; p.el('Start').fire(); p.advance(30*60000); p.heartbeat();
    assert.equal(p.ctl.isActive(),false); assert.equal(p.saves.length,1);
});
