/* Opt-in moving phone references. Never averages across a walk or changes calibration. */
(function (root) {
    'use strict';
    const LIMITS = {walk: 3, ride: 10};
    function init(hooks, env = {}) {
        const doc = env.document || root.document, geo = env.geolocation || root.navigator?.geolocation;
        const el = name => doc.getElementById('liveGPS' + name);
        if (!el('Start')) return null;
        const now = env.now || (() => Date.now());
        const clock = env.monotonic || (() => root.performance.now());
        const later = env.setTimeout || root.setTimeout.bind(root), clear = env.clearTimeout || root.clearTimeout.bind(root);
        let active = null, watch = null, timer = null, wake = null;
        let accuracy = null;
        const status = text => {el('Status').textContent = text;};
        const id = () => root.crypto?.randomUUID?.().replaceAll('-', '')
            || 'gps_' + now().toString(36) + '_' + Math.random().toString(36).slice(2);
        function render() {
            for (const key of ['Start', 'Mode', 'Distance']) el(key).disabled = !!active || hooks.busy() || !!accuracy?.busy();
            el('Stop').disabled = !active;
            accuracy?.sync(!!active);
            hooks.changed();
        }
        function stop(text) {
            active = null;
            accuracy?.stop();
            if (watch !== null) geo.clearWatch(watch);
            watch = null;
            if (timer !== null) clear(timer);
            timer = null;
            if (wake) {void wake.release().catch(() => {}); wake = null;}
            if (text) status(text);
            render();
        }
        function valid(run) {
            if (active !== run) return false;
            hooks.validate();
            if (doc.hidden || run.target !== hooks.target() || run.context !== hooks.context())
                throw new Error('Page, tracker or saved map changed. Start a new GPS survey.');
            if (clock() - run.started >= 30 * 60000) throw new Error('30-minute session complete. Start another if needed.');
            return true;
        }
        async function receive(run, position) {
            if (active !== run || run.saving) return;
            try {
                if (!valid(run)) return;
                const c = position.coords, stamp = position.timestamp;
                if (!c || !Number.isFinite(stamp) || stamp > now() + 1000 || now() - stamp > 3000)
                    throw new Error('Waiting for a fresh GPS fix (less than 3 seconds old).');
                if (!Number.isFinite(c.accuracy) || c.accuracy < .1 || c.accuracy > 20)
                    throw new Error('Waiting for GPS accuracy of 20 m or better.');
                if (stamp <= run.lastStamp) return;
                const fix = {latitude: c.latitude, longitude: c.longitude, accuracy_m: c.accuracy};
                const projected = root.BPSGPS.project(run.transform, fix);
                if (c.speed != null && (!Number.isFinite(c.speed) || c.speed < 0 || c.speed > LIMITS[run.mode]))
                    throw new Error('Slow down to collect: walking ≤3 m/s; slow ride ≤10 m/s.');
                if (run.previous) {
                    const delta = root.BPSGPS.local(fix, run.previous);
                    const elapsed = (stamp - run.previous.timestamp) / 1000;
                    if (Math.hypot(delta.x, delta.y) > LIMITS[run.mode] * elapsed + c.accuracy + run.previous.accuracy_m)
                        throw new Error('GPS jumped faster than this survey mode allows. Waiting for a consistent fix.');
                }
                run.lastStamp = stamp; run.lastFix = clock(); run.previous = {...fix, timestamp: stamp};
                if (clock() < run.nextAt || hooks.busy()) return;
                run.saving = true;
                const gps = {...fix, timestamp_ms: stamp, mode: run.mode, dog_distance_m: run.distance,
                    session_id: run.id, alignment: run.alignment, layout_revision: run.revision};
                if (c.speed != null) gps.speed_m_s = c.speed;
                if (Number.isFinite(c.heading)) gps.heading_deg = c.heading;
                const result = await hooks.save({floor: run.floor, x_m: projected.x, y_m: projected.y},
                    run.target, {label: run.label}, run.context, gps);
                if (active !== run) return;
                run.count++;
                run.nextAt = clock() + 15000;
                status(`${run.count} GPS/BLE snapshots saved · GPS ±${c.accuracy.toFixed(1)} m · total location allowance ${result.accuracy_m.toFixed(1)} m. Keep the dog within ${run.distance} m of this phone.`);
                if (run.count >= 120) stop('120 snapshots saved. Session complete; download later from your desktop.');
            } catch (error) {
                if (active !== run) return;
                if (run.saving) stop('GPS survey stopped; the last save was not confirmed: ' + error.message);
                else {
                    try {valid(run); status(error.message);} catch (changed) {stop(changed.message);}
                }
            } finally {run.saving = false;}
        }
        function heartbeat(run) {
            if (active !== run) return;
            try {
                valid(run);
                accuracy?.heartbeat();
                if (!run.saving && (run.lastFix === null || clock() - run.lastFix > 5000))
                    status(`${run.count} snapshots saved. Waiting for fresh, accurate GPS; no points are being reused.`);
                timer = later(() => heartbeat(run), 1000);
            } catch (error) {stop(error.message);}
        }
        el('Start').addEventListener('click', () => {
            if (active || hooks.busy()) return;
            try {
                hooks.validate();
                accuracy?.validate();
                if (!geo) throw new Error('Phone browser GPS is unavailable. Open BPS over HTTPS and allow location access.');
                const map = hooks.map(), mode = el('Mode').value, distance = Number(el('Distance').value);
                if (!map || !LIMITS[mode] || !el('Distance').value.trim() || !Number.isFinite(distance) || distance < 0 || distance > 30)
                    throw new Error('Set a dog-to-phone distance allowance between 0 and 30 m.');
                const alignment = JSON.parse(JSON.stringify({scale: map.scale, georeference: map.georeference}));
                const transform = root.BPSGPS.fit(alignment.georeference?.anchors);
                if (!/^[a-f0-9]{64}$/.test(map.layout_revision || ''))
                    throw new Error('Reload the saved map before starting a GPS survey.');
                const target = hooks.target();
                if (!target) throw new Error('Select the dog or group first.');
                hooks.beforeStart();
                const run = active = {target, context: hooks.context(), floor: map.name, transform, alignment,
                    mode, distance, revision: map.layout_revision, id: id(), label: hooks.label(), started: clock(), nextAt: 0,
                    count: 0, lastStamp: 0, lastFix: null, previous: null, saving: false};
                status('Waiting for phone GPS permission and a fresh fix. Keep the dog nearby and this screen open.');
                render();
                void accuracy?.start();
                watch = geo.watchPosition(p => void receive(run, p), error => {
                    if (active !== run) return;
                    if (error.code === 1) stop('GPS permission denied. Allow precise location for this phone/browser, then start again.');
                    else status('Waiting for phone GPS: ' + (error.message || 'location unavailable'));
                }, {enableHighAccuracy: true, maximumAge: 0, timeout: 15000});
                root.navigator?.wakeLock?.request('screen').then(lock => {
                    if (active === run) wake = lock; else void lock.release().catch(() => {});
                }).catch(() => {});
                heartbeat(run);
            } catch (error) {stop(error.message);}
        });
        el('Stop').addEventListener('click', () => stop('GPS survey stopped. A snapshot already saving may finish; retained points stay available for a desktop download.'));
        doc.addEventListener('visibilitychange', () => {if (doc.hidden && active) stop('GPS survey stopped because the page was hidden. Start again when ready.');});
        (env.window || root.window)?.addEventListener('pagehide', () => stop());
        if (root.BPSPhoneAccuracy) accuracy = root.BPSPhoneAccuracy.init({fetch: hooks.fetch,
            active: () => !!active, changed: render, stopSurvey: () => stop('GPS survey stopped. Saved points remain in Home Assistant.')}, env);
        render();
        return {isActive: () => !!active, stop, sync() {
            if (active) {try {valid(active);} catch (error) {stop(error.message);}}
            render();
        }};
    }
    root.BPSGPSWalk = {init};
})(globalThis);
