/* Optional common-floor height survey. No GPS/map anchors or radio changes. */
(function (root) {
    'use strict';
    const MAX_AGE_MS = 60000, SESSION_MS = 900000, MAX_SAMPLES = 180, CAPTURE_MS = 45000;
    const number = (v, lo, hi) => typeof v === 'number' && Number.isFinite(v) && v >= lo && v <= hi;
    const median = values => {const a = [...values].sort((x, y) => x-y), m = Math.floor(a.length/2);
        return a.length % 2 ? a[m] : (a[m-1]+a[m])/2;};
    const pressureDelta = (base, station) => 44330 * (1 - (station/base) ** .1903);
    function metres(value, unit, signed = false) {
        if (typeof value !== 'string' || value.trim() === '' || !['m', 'ft'].includes(unit)) throw new Error('Enter a known phone height and receiver offset in metres or feet.');
        const m = Number(value) * (unit === 'ft' ? .3048 : 1);
        if (!number(m, signed ? -10 : 0, 10)) throw new Error(signed ? 'Receiver offset must be −10 to 10 m.' : 'Known phone height must be 0 to 10 m above the common floor.');
        return m;
    }
    function observation(raw, source, method, now = Date.now()) {
        if (!raw || !number(raw.timestamp, 1, now) || now-raw.timestamp > MAX_AGE_MS
            || typeof source !== 'string' || (method !== 'pressure' && method !== 'gps')
            || (method === 'pressure' ? !/^sensor\.[a-z0-9_]{1,128}$/.test(source) : source !== 'browser')
            || !number(raw.value, method === 'pressure' ? 300 : -1000, method === 'pressure' ? 1100 : 20000)
            || (method === 'gps' && !number(raw.accuracy_m, .01, 1000))) return null;
        return {value: raw.value, timestamp: raw.timestamp, accuracy_m: method === 'gps' ? raw.accuracy_m : null,
            moving: method === 'gps' && number(raw.speed, 0, 1000) && raw.speed > .3,
            source, method, timestamp_kind: method === 'pressure' ? 'ha_state_update' : 'browser_position'};
    }
    function summarize(samples, now = Date.now()) {
        if (!samples.length) throw new Error('No fresh height measurements. Enable your phone barometer or enter the receiver height manually.');
        const first = samples[0];
        if (samples.some(p => p.source !== first.source || p.method !== first.method)) throw new Error('Reference and station must use the exact same phone source.');
        if (samples.some(p => !observation(p, p.source, p.method, now))) throw new Error('Height measurements are stale or lack vertical accuracy. Capture again.');
        const times = samples.map(p => p.timestamp), span_s = (Math.max(...times)-Math.min(...times))/1000;
        if (samples.length < 2 || samples.length > MAX_SAMPLES || !number(span_s, 20, CAPTURE_MS/1000))
            throw new Error('Need at least two distinct fresh measurements spanning 20 seconds. Use manual height if the phone updates too slowly.');
        const value = median(samples.map(p => p.value));
        const spread_m = Math.max(...samples.map(p => Math.abs(first.method === 'pressure' ? pressureDelta(value, p.value) : p.value-value)));
        const reported_accuracy_m = first.method === 'gps' ? Math.max(...samples.map(p => p.accuracy_m)) : null;
        const ordered = [...samples].sort((a, b) => a.timestamp-b.timestamp), edge = Math.max(1, Math.floor(ordered.length/3));
        const early = median(ordered.slice(0, edge).map(p => p.value)), late = median(ordered.slice(-edge).map(p => p.value));
        const drift = Math.abs(first.method === 'pressure' ? pressureDelta(early, late) : late-early);
        if (samples.some(p => p.moving) || spread_m > (first.method === 'pressure' ? .6 : 2)
            || drift > (first.method === 'pressure' ? .6 : Math.max(.5, reported_accuracy_m)))
            throw new Error('Height changed or was noisy during capture. Keep the phone still, avoid wind/air vents and capture again.');
        return {source: first.source, method: first.method, timestamp: Math.max(...times), value,
            sample_count: samples.length, span_s, spread_m, reported_accuracy_m,
            timestamp_kind: first.timestamp_kind, age_s: (now-Math.max(...times))/1000};
    }
    function collector(source, method, started) {
        const rows = [], seen = new Set();
        return {add(raw, now = Date.now()) {
            const p = observation(raw, source, method, now);
            if (!p || p.timestamp < started || now-started > CAPTURE_MS || rows.length >= MAX_SAMPLES) return false;
            // State publication for unrelated attributes cannot create new
            // barometer evidence. Browser timestamps identify browser fixes.
            const key = method === 'pressure' ? String(p.value) : String(p.timestamp);
            if (seen.has(key)) return false;
            seen.add(key); rows.push(p); return true;
        }, count: () => rows.length, result: now => summarize(rows, now)};
    }
    function estimate(base, station, phoneHeight, offset, now = Date.now()) {
        if (!base || !station || base.source !== station.source || base.method !== station.method)
            throw new Error('Reference and station must use the exact same phone source. Recapture the reference.');
        if (!number(now-base.started, 0, SESSION_MS)) throw new Error('Reference expired after 15 minutes. Return to the reference point and recapture.');
        if (!number(now-station.timestamp, 0, MAX_AGE_MS)) throw new Error('Station measurement expired. Capture again.');
        if (!number(phoneHeight, 0, 10) || !number(offset, -10, 10)) throw new Error('Enter valid common-floor phone height and signed receiver offset.');
        const delta_m = base.method === 'pressure' ? pressureDelta(base.value, station.value) : station.value-base.value;
        const uncertainty_m = [base, station].reduce((sum, p) => sum+Math.max(base.method === 'pressure' ? .5 : p.reported_accuracy_m, p.spread_m), 0);
        if (!number(uncertainty_m, 0, 2)) throw new Error('Combined vertical uncertainty exceeds 2 m. Use a phone barometer or enter receiver height manually.');
        const height = phoneHeight+delta_m+offset;
        if (!number(height, 0, 10)) throw new Error('Estimated receiver height is outside 0–10 m above the common floor. Check the reference and signed offset.');
        const summary = p => Object.fromEntries(['value', 'sample_count', 'span_s', 'age_s', 'spread_m', 'reported_accuracy_m', 'timestamp_kind'].map(k => [k, p[k]]));
        return {version: 1, reference: 'common_floor', method: base.method, source: base.source,
            baseline_phone_height_m: phoneHeight, station_offset_m: offset, delta_m,
            estimate_m: Math.round(height*1000)/1000, uncertainty_m, baseline_age_s: (now-base.started)/1000,
            confirmed_at: now/1000, baseline: summary(base), station: summary(station)};
    }

    function init(hooks, environment = {}) {
        const doc = environment.document || root.document, win = environment.window || root.window;
        const geo = environment.geolocation || root.navigator?.geolocation;
        const now = environment.now || (() => Date.now());
        const later = environment.setTimeout || root.setTimeout, clearLater = environment.clearTimeout || root.clearTimeout;
        const every = environment.setInterval || root.setInterval, clearEvery = environment.clearInterval || root.clearInterval;
        const el = id => doc.getElementById(id);
        if (!el('heightSurvey')) return null;
        let generation = 0, base = null, station = null, preview = null, capture = null;
        let watch = null, timer = null, poll = null, expiry = null, lastContext = null, lastConfirmed = null, request = 0, inFlight = false;
        let sourceStop = null;
        let message = 'Height survey is optional. Load phone barometers and select your own sensor, or explicitly choose browser altitude.';
        const status = text => {message = text; el('heightStatus').textContent = text;};
        const source = () => el('heightSource').value || 'browser';
        const method = () => source() === 'browser' ? 'gps' : 'pressure';
        const guard = () => ({floor: hooks.getFloor(), context: hooks.getContext(), generation});
        const current = g => g && g.floor === hooks.getFloor() && g.context === hooks.getContext() && g.generation === generation;
        function release() {
            sourceStop?.();
            if (watch !== null) geo?.clearWatch(watch);
            if (timer !== null) clearLater(timer);
            if (poll !== null) clearEvery(poll);
            watch = timer = poll = null;
        }
        function buttons() {
            el('heightBaseCapture').disabled = !!capture;
            el('heightStationCapture').disabled = !!capture || !base || !current(base.guard) || now()-base.started > SESSION_MS;
            el('heightStop').disabled = !capture;
            el('heightConfirm').disabled = !preview || !current(preview.guard) || now()-station.timestamp > MAX_AGE_MS;
        }
        function cancel(reason = 'Height survey cancelled. Recapture the reference before another visit.', keepBase = false) {
            generation++; request++; release(); capture = station = preview = null;
            if (!keepBase) {
                base = null; if (expiry !== null) clearLater(expiry); expiry = null;
            } else if (base) base.guard = guard();
            status(reason); buttons();
        }
        function refresh() {
            const context = hooks.getContext();
            if (lastContext !== null && context !== lastContext) cancel('Floor or map changed. Recapture the common-floor reference.');
            lastContext = context;
            if (base && (!current(base.guard) || now()-base.started > SESSION_MS)) cancel('Reference expired or floor changed. Return to the reference and recapture.');
            const selected = el('heightReceiver').value;
            el('heightReceiver').replaceChildren();
            const blank = doc.createElement('option'); blank.value = ''; blank.textContent = 'Select placed receiver'; el('heightReceiver').appendChild(blank);
            (hooks.getFloor()?.receivers || []).forEach((rec, i) => {
                const option = doc.createElement('option'); option.value = String(i); option.textContent = rec.entity_id; el('heightReceiver').appendChild(option);
            });
            if ([...el('heightReceiver').options].some(o => o.value === selected)) el('heightReceiver').value = selected;
            buttons();
        }
        async function loadSources(g = null) {
            if (inFlight) return;
            inFlight = true; const token = ++request, requestedAt = now();
            const controller = typeof root.AbortController === 'function' ? new root.AbortController() : null;
            let deadline, stopRequest;
            try {
                const bounded = new Promise((resolve, reject) => {
                    stopRequest = () => {controller?.abort(); clearLater(deadline); reject(new Error('Height source request cancelled.'));};
                    sourceStop = stopRequest;
                    deadline = later(() => {controller?.abort(); reject(new Error('Phone barometer request timed out. Check the connection and retry, or enter height manually.'));}, 10000);
                });
                const data = await Promise.race([bounded, (async () => {
                    const response = await hooks.fetch('/api/bps/height_sources', controller ? {signal: controller.signal} : {});
                    if (!response.ok) throw new Error('Phone barometers unavailable. Check Home Assistant access or enter height manually.');
                    return await response.json();
                })()]);
                if (token !== request || g && !current(g)) return;
                const selected = source(), rows = Array.isArray(data.sources) ? data.sources.slice(0, 128) : [];
                el('heightSource').replaceChildren();
                const browser = doc.createElement('option'); browser.value = 'browser'; browser.textContent = 'Browser altitude (requires vertical accuracy)'; el('heightSource').appendChild(browser);
                for (const row of rows) {
                    if (row.kind !== 'pressure' || !/^sensor\.[a-z0-9_]{1,128}$/.test(row.entity_id)) continue;
                    const option = doc.createElement('option'); option.value = row.entity_id;
                    option.textContent = `${row.name || row.entity_id} (${row.entity_id})${row.available ? '' : ' — stale/unavailable'}`;
                    el('heightSource').appendChild(option);
                }
                if (selected !== 'browser' && !rows.some(r => r.entity_id === selected)) {
                    const missing = doc.createElement('option'); missing.value = selected; missing.textContent = selected+' — unavailable'; el('heightSource').appendChild(missing);
                }
                el('heightSource').value = selected;
                if (g && capture && current(g)) {
                    const row = rows.find(p => p.entity_id === capture.source);
                    // Server age translated at request start includes the full
                    // request delay and avoids comparing HA/browser wall clocks.
                    if (row?.available === true && number(row.age_s, 0, 60)) capture.samples.add({value: row.pressure_hpa, timestamp: requestedAt-row.age_s*1000}, now());
                    showCapture();
                } else status(rows.length ? 'Select your own phone barometer explicitly, then capture the reference. Pressure spread measures precision, not proven absolute accuracy.'
                    : 'No mobile_app pressure sensors found. Enable the Companion App barometer if your phone has one, or use browser altitude/manual height.');
            } catch (error) {if (token === request && (!g || current(g))) status(error.message);}
            finally {clearLater(deadline); if (sourceStop === stopRequest) sourceStop = null; inFlight = false;}
        }
        function showCapture() {
            if (capture) status(`Keep the phone stationary at the ${capture.kind === 'base' ? 'reference' : 'receiver'}: ${Math.floor((now()-capture.started)/1000)} s · ${capture.samples.count()} distinct measurements. Capture 30–45 s.`);
        }
        function stop() {
            const active = capture; if (!active) return;
            request++;
            release(); capture = null;
            try {
                if (!current(active.guard)) throw new Error('Floor/source changed. Recapture the reference.');
                if (!number(now()-active.started, 30000, CAPTURE_MS)) throw new Error('Capture for 30–45 seconds while stationary.');
                const result = active.samples.result(now());
                if (active.kind === 'base') {
                    if (result.method === 'gps' && Math.max(result.reported_accuracy_m, result.spread_m) > 1)
                        throw new Error('Reference vertical uncertainty exceeds 1 m; two captures would exceed 2 m. Use a phone barometer or manual height.');
                    base = {...result, started: active.started, phoneHeight: active.phoneHeight, guard: active.guard};
                    expiry = later(() => cancel('Reference expired after 15 minutes. Return to the reference point and recapture.'), SESSION_MS-(now()-base.started));
                    status(`Reference captured with ${result.source}: ${result.sample_count} measurements · spread ${result.spread_m.toFixed(2)} m.${result.method === 'pressure' ? ' HA publication age is used; raw measurement age is unknown. Pressure precision is not proven absolute accuracy.' : ` Reported vertical accuracy ${result.reported_accuracy_m.toFixed(2)} m.`} Visit receivers within this 15-minute session; recheck the reference if weather, temperature or ventilation changes.`);
                } else {
                    station = result;
                    const survey = estimate(base, station, base.phoneHeight, active.offset, now());
                    preview = {survey, target: active.target, guard: active.guard};
                    const display = el('heightUnit').value === 'ft' ? `${(survey.estimate_m/.3048).toFixed(3)} ft (saved as ${survey.estimate_m.toFixed(3)} m)` : `${survey.estimate_m.toFixed(3)} m`;
                    status(`Preview ${active.target.label}: ${display} above the common floor · ${survey.method === 'pressure' ? 'pressure precision allowance' : 'conservative vertical uncertainty'} ${survey.uncertainty_m.toFixed(2)} m · delta ${survey.delta_m.toFixed(2)} m · receiver above phone ${survey.station_offset_m.toFixed(2)} m · reference age ${Math.floor(survey.baseline_age_s)} s. Confirm to overwrite its height.${survey.method === 'pressure' ? ' Pressure precision is not proven absolute accuracy; weather, temperature or ventilation can shift the reference. Recheck the reference if conditions change.' : ''}`);
                }
            } catch (error) {station = preview = null; status(error.message);}
            buttons();
        }
        function start(kind) {
            try {
                refresh();
                if (!hooks.getFloor()) throw new Error('Open a saved floor first.');
                if (capture) return;
                const phoneHeight = metres(el('heightBase').value, el('heightUnit').value);
                let target = null, offset = null;
                if (kind === 'station') {
                    if (!base || !current(base.guard) || now()-base.started > SESSION_MS) throw new Error('Capture a fresh reference with this source first.');
                    target = hooks.getTarget(el('heightReceiver').value);
                    if (!target) throw new Error('Select a placed receiver before capturing its height.');
                    offset = metres(el('heightOffset').value, el('heightUnit').value, true);
                }
                cancel('Starting height capture.', kind === 'station');
                const g = guard(), started = now(), selected = source(), chosenMethod = method();
                capture = {kind, target, offset, phoneHeight, guard: g, started, source: selected, samples: collector(selected, chosenMethod, started)};
                buttons(); timer = later(stop, CAPTURE_MS);
                if (chosenMethod === 'pressure') {loadSources(g); poll = every(() => loadSources(g), 5000);}
                else {
                    if (!geo) throw new Error('Browser altitude unavailable. Select a phone barometer or enter height manually.');
                    watch = geo.watchPosition(position => {
                        if (!capture || !current(g)) return;
                        const coords = position.coords || {};
                        const accepted = capture.samples.add({value: coords.altitude, accuracy_m: coords.altitudeAccuracy, speed: coords.speed, timestamp: position.timestamp}, now());
                        if (accepted) showCapture();
                        else status('Waiting for fresh altitude with finite vertical accuracy. Horizontal GPS accuracy cannot qualify a height measurement.');
                    }, error => {if (current(g)) {cancel(`Browser altitude failed: ${error.message || 'permission denied'}. Select a barometer or enter height manually.`, kind === 'station');}},
                    {enableHighAccuracy: true, maximumAge: 0, timeout: 10000});
                    poll = every(showCapture, 1000);
                }
                showCapture();
            } catch (error) {release(); capture = null; status(error.message); buttons();}
        }
        el('heightLoad').addEventListener('click', () => loadSources());
        el('heightBaseCapture').addEventListener('click', () => start('base'));
        el('heightStationCapture').addEventListener('click', () => start('station'));
        el('heightStop').addEventListener('click', stop);
        el('heightCancel').addEventListener('click', () => cancel());
        for (const id of ['heightSource', 'heightUnit', 'heightBase']) el(id).addEventListener('change', () => cancel('Source or common-floor reference changed. Recapture the reference.'));
        for (const id of ['heightReceiver', 'heightOffset']) el(id).addEventListener('change', () => cancel('Receiver or offset changed. Capture the station again.', true));
        for (const id of ['heightBase', 'heightOffset']) el(id).addEventListener('input', () => cancel('Height input changed. Capture again.', id === 'heightOffset'));
        el('heightConfirm').addEventListener('click', () => {
            if (!preview || !current(preview.guard)) return;
            try {
                const survey = estimate(base, station, base.phoneHeight, preview.survey.station_offset_m, now());
                if (!hooks.stageHeight(preview.target, survey.estimate_m, survey)) throw new Error('Receiver changed or was removed. Select it and capture again.');
                lastConfirmed = survey; preview = station = null;
                hooks.stageChange(); buttons(); hooks.redraw();
                status('Receiver height staged. Save Floor Plan to keep it. Recalibrate after changing mount height.');
            } catch (error) {preview = null; status(error.message); buttons();}
        });
        win?.addEventListener('pagehide', () => cancel('Page left. Recapture the reference for a later visit.'));
        refresh(); status(message);
        const publicSummary = p => p ? {...Object.fromEntries(['source', 'method', 'value', 'sample_count', 'span_s', 'spread_m', 'reported_accuracy_m', 'timestamp_kind', 'age_s'].map(k => [k, p[k]])),
            age_s: (now()-p.timestamp)/1000} : null;
        return {refresh, cancel, snapshot: () => ({status: message, source: source(), capturing: !!capture,
            baseline: publicSummary(base), baseline_age_s: base ? (now()-base.started)/1000 : null,
            baseline_phone_height_m: base?.phoneHeight ?? null, station: publicSummary(station),
            preview: preview?.survey ?? null, confirmed: lastConfirmed})};
    }
    const api = {metres, pressureDelta, observation, summarize, collector, estimate, init};
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    root.BPSHeight = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
