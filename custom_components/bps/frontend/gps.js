/* Optional private GPS map setup. Geometry and capture are also usable in Node. */
(function (root) {
    'use strict';
    const EARTH_M = 6371008.8, MAX_PROPERTY_M = 5000, MAX_ANCHORS = 12;
    const MAX_AGE_MS = 60000, MAX_SAMPLES = 180;
    const number = (v, min, max) => typeof v === 'number' && Number.isFinite(v) && v >= min && v <= max;
    const validGPS = p => p && number(p.latitude, -85, 85) && number(p.longitude, -180, 180);
    const median = xs => {const a = [...xs].sort((x, y) => x - y), m = Math.floor(a.length / 2);
        return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;};
    const radians = degrees => degrees * Math.PI / 180;
    function local(p, origin) {
        if (!validGPS(p) || !validGPS(origin)) throw new Error('Use finite latitude (−85 to 85) and longitude (−180 to 180).');
        const east = radians(((p.longitude - origin.longitude + 540) % 360) - 180) * EARTH_M * Math.cos(radians(origin.latitude));
        const north = radians(p.latitude - origin.latitude) * EARTH_M;
        if (Math.hypot(east, north) > MAX_PROPERTY_M) throw new Error('GPS points must be within a 5 km property.');
        return {x: east, y: -north}; // map Y points down; GPS north points up
    }
    function fromLocal(p, origin) {
        return {latitude: origin.latitude - p.y / EARTH_M * 180 / Math.PI,
            longitude: ((origin.longitude + p.x / (EARTH_M * Math.cos(radians(origin.latitude))) * 180 / Math.PI + 540) % 360) - 180};
    }
    function fit(anchors) {
        if (!Array.isArray(anchors) || anchors.length < 2 || anchors.length > MAX_ANCHORS) throw new Error('Add 2–12 separated GPS/map anchors.');
        if (anchors.some(a => !validGPS(a) || !number(a.x, 0, 1e6) || !number(a.y, 0, 1e6)
            || !number(a.accuracy_m, 0, 1000))) throw new Error('An anchor has invalid coordinates or accuracy.');
        const origin = {latitude: anchors[0].latitude, longitude: anchors[0].longitude};
        const points = anchors.map(a => local(a, origin));
        points.forEach((p, i) => {
            for (let j = 0; j < i; j++) if (Math.hypot(p.x-points[j].x, p.y-points[j].y) < .1
                || Math.hypot(anchors[i].x-anchors[j].x, anchors[i].y-anchors[j].y) < 1)
                throw new Error('Anchors must identify distinct GPS and map points.');
        });
        const mean = field => points.reduce((sum, p) => sum + p[field], 0) / points.length;
        const cx = mean('x'), cy = mean('y');
        const mx = anchors.reduce((s, a) => s + a.x, 0) / anchors.length;
        const my = anchors.reduce((s, a) => s + a.y, 0) / anchors.length;
        let denom = 0, dot = 0, cross = 0, mapVariance = 0;
        points.forEach((p, i) => {
            const x = p.x - cx, y = p.y - cy, u = anchors[i].x - mx, v = anchors[i].y - my;
            denom += x*x + y*y; mapVariance += u*u + v*v;
            dot += x*u + y*v; cross += x*v - y*u;
        });
        if (denom / points.length < 1 || mapVariance / points.length < 1) throw new Error('Anchors are coincident or too close. Separate them by several metres.');
        const a = dot / denom, b = cross / denom, scale = Math.hypot(a, b);
        if (!number(scale, 0.01, 100000)) throw new Error('GPS/map fit is ill-conditioned. Review the anchors.');
        const tx = mx - a*cx + b*cy, ty = my - b*cx - a*cy;
        const residuals = points.map((p, i) => Math.hypot(a*p.x-b*p.y+tx-anchors[i].x, b*p.x+a*p.y+ty-anchors[i].y) / scale);
        const residual_m = Math.sqrt(residuals.reduce((s, r) => s + r*r, 0) / points.length);
        const anchor_accuracy_m = Math.max(...anchors.map(p => p.accuracy_m));
        return {origin, a, b, tx, ty, scale, residual_m, residuals_m: residuals,
            anchor_accuracy_m, independently_checked: anchors.length >= 3,
            consistent: residual_m <= Math.max(5, anchor_accuracy_m * 2),
            center: {x: cx, y: cy}, baseline_m: Math.sqrt(denom / points.length)};
    }
    function project(transform, fix) {
        if (!transform.consistent) throw new Error('GPS/map anchors disagree. Review them before placing receivers.');
        if (!number(fix?.accuracy_m, .1, 1000)) throw new Error('GPS accuracy must be finite and between 0.1 and 1000 metres.');
        const p = local(fix, transform.origin);
        const anchorError = transform.anchor_accuracy_m * (1 + Math.hypot(p.x-transform.center.x, p.y-transform.center.y) / transform.baseline_m);
        const accuracy_m = fix.accuracy_m + anchorError + transform.residual_m;
        return {x: transform.a*p.x-transform.b*p.y+transform.tx, y: transform.b*p.x+transform.a*p.y+transform.ty,
            accuracy_m, radius_px: accuracy_m * transform.scale};
    }
    function observation(raw, source, now = Date.now()) {
        if (!validGPS(raw) || !number(raw.accuracy_m, 0.1, 1000) || !number(raw.timestamp, 1, now)
            || now - raw.timestamp > MAX_AGE_MS || typeof source !== 'string' || !source) return null;
        return {latitude: raw.latitude, longitude: raw.longitude, accuracy_m: raw.accuracy_m, timestamp: raw.timestamp, source,
            timestamp_kind: source === 'browser' ? 'browser_position' : 'ha_state_update'};
    }
    function summarize(samples, now = Date.now()) {
        if (!samples.length) throw new Error('No fresh GPS observations. Check permissions or your selected phone entity.');
        const recent = samples.filter(s => observation(s, s.source, now));
        if (!recent.length) throw new Error('GPS observations are stale. Capture again.');
        if (recent.length < 2 || Math.max(...recent.map(p => p.timestamp))-Math.min(...recent.map(p => p.timestamp)) < 20000)
            throw new Error('Need distinct GPS fixes spanning at least 20 seconds. Capture again or use a more frequently updated source.');
        const origin = recent[0], points = recent.map(p => local(p, origin));
        const center = {x: median(points.map(p => p.x)), y: median(points.map(p => p.y))};
        const spread_m = Math.max(...points.map(p => Math.hypot(p.x-center.x, p.y-center.y)));
        const reported_accuracy_m = Math.max(...recent.map(p => p.accuracy_m));
        // Repeated correlated GPS fixes do not buy sqrt(N) precision. Movement
        // outside the reported noise envelope makes this stationary capture unusable.
        if (spread_m > Math.max(5, reported_accuracy_m * 2)) throw new Error('GPS moved or jumped during capture. Stand still and capture again.');
        const timestamp = Math.max(...recent.map(p => p.timestamp));
        return {...fromLocal(center, origin), timestamp, source: origin.source,
            sample_count: recent.length, spread_m, reported_accuracy_m,
            timestamp_kind: origin.timestamp_kind || (origin.source === 'browser' ? 'browser_position' : 'ha_state_update'),
            accuracy_m: Math.max(reported_accuracy_m, spread_m), age_s: (now-timestamp)/1000};
    }
    function choose(sources, now = Date.now()) {
        const results = [], errors = [];
        for (const samples of sources) {
            if (!samples.length) continue;
            try {results.push(summarize(samples, now));} catch (error) {errors.push(error.message);}
        }
        // A source with movement is evidence against a stationary observation,
        // even when another source has a smaller reported accuracy.
        if (errors.some(e => /moved or jumped/.test(e))) throw new Error(errors.find(e => /moved or jumped/.test(e)));
        if (!results.length) throw new Error(errors[0] || 'No fresh GPS observations.');
        if (results.length > 1) {
            const separation = local(results[0], results[1]);
            if (Math.hypot(separation.x, separation.y) > Math.max(5, results[0].accuracy_m + results[1].accuracy_m))
                throw new Error('Browser and selected phone GPS disagree. Review the source and capture again.');
        }
        results.sort((a, b) => a.accuracy_m - b.accuracy_m || b.timestamp - a.timestamp);
        return {...results[0], compared_sources: results.map(p => ({source: p.source, accuracy_m: p.accuracy_m,
            spread_m: p.spread_m, sample_count: p.sample_count, age_s: p.age_s}))};
    }
    function collector() {
        const sources = new Map(), seen = new Set();
        return {add(raw, source, now = Date.now()) {
            const p = observation(raw, source, now);
            if (!p) return false;
            // HA last_updated can change for unrelated attributes. An unchanged
            // coordinate/accuracy tuple is not another GPS observation.
            const key = source + ':' + (source === 'browser' ? p.timestamp : [p.latitude, p.longitude, p.accuracy_m].join(':'));
            if (seen.has(key) || seen.size >= MAX_SAMPLES * 2) return false;
            seen.add(key);
            const rows = sources.get(source) || [];
            if (rows.length >= MAX_SAMPLES) return false;
            rows.push(p); sources.set(source, rows); return true;
        }, result: now => choose([...sources.values()], now), count: () => [...sources.values()].reduce((s, p) => s+p.length, 0)};
    }

    function init(hooks, environment = {}) {
        const doc = environment.document || root.document, win = environment.window || root.window;
        const geo = environment.geolocation || root.navigator?.geolocation;
        const now = environment.now || (() => Date.now());
        const later = environment.setTimeout || root.setTimeout, clearLater = environment.clearTimeout || root.clearTimeout;
        const every = environment.setInterval || root.setInterval, clearEvery = environment.clearInterval || root.clearInterval;
        const el = id => doc.getElementById(id);
        if (!el('gpsSetup')) return null;
        let generation = 0, capture = null, fix = null, preview = null, anchorPoint = null, anchorGuard = null;
        let watch = null, timer = null, poll = null, picked = null, lastContext = null, lastChosen = null;
        let message = 'GPS is optional. Align the map with at least two known points.';
        let locationsRequest = 0;
        const status = text => {message = text; el('gpsStatus').textContent = text;};
        const context = () => hooks.getContext();
        const guard = () => ({floor: hooks.getFloor(), context: context(), generation});
        const current = g => !!g && g.floor === hooks.getFloor() && g.context === context() && g.generation === generation;
        function release() {
            if (watch !== null) geo?.clearWatch(watch);
            if (timer !== null) clearLater(timer);
            if (poll !== null) clearEvery(poll);
            watch = timer = poll = null;
        }
        function cancel(reason = 'GPS capture cancelled.') {
            generation++; release(); capture = null; fix = preview = anchorPoint = anchorGuard = null;
            picked?.(); picked = null;
            el('gpsCapture').disabled = false; el('gpsStop').disabled = true;
            el('gpsUseFix').disabled = el('gpsPreview').disabled = el('gpsConfirm').disabled = true;
            status(reason); hooks.redraw();
        }
        function updateButtons() {
            const usable = fix && now()-fix.timestamp <= MAX_AGE_MS;
            el('gpsUseFix').disabled = !usable; el('gpsPreview').disabled = !usable;
            el('gpsConfirm').disabled = !preview || !current(preview.guard);
        }
        function fitStatus() {
            const rawAnchors = hooks.getFloor()?.georeference?.anchors;
            const anchors = Array.isArray(rawAnchors) ? rawAnchors.slice(0, MAX_ANCHORS) : [];
            el('gpsAnchors').replaceChildren();
            anchors.forEach((anchor, index) => {
                const row = doc.createElement('div'), remove = doc.createElement('button');
                row.textContent = `${index+1}. ${anchor?.label || 'Map point'} — accuracy ${number(anchor?.accuracy_m, 0, 1000) ? anchor.accuracy_m.toFixed(1) : '?'} m `;
                remove.type = 'button'; remove.className = 'bps-btn bps-btn-outline'; remove.textContent = 'Remove';
                remove.addEventListener('click', () => {
                    const floor = hooks.getFloor(); if (!floor) return;
                    floor.georeference.anchors.splice(index, 1); lastContext = context();
                    cancel('Anchor removed. Save Floor Plan to keep changes.');
                    hooks.stageChange(); refresh();
                }); row.appendChild(remove); el('gpsAnchors').appendChild(row);
            });
            try {
                const transform = fit(anchors), scale = hooks.getFloor().scale;
                let text = `${anchors.length} anchors · GPS scale ${transform.scale.toFixed(2)} px/m · residual ${transform.residual_m.toFixed(1)} m · anchor accuracy ≤ ${transform.anchor_accuracy_m.toFixed(1)} m.`;
                if (number(scale, .001, 100000)) text += ` Difference from map scale: ${((transform.scale/scale-1)*100).toFixed(1)}%.`;
                text += transform.independently_checked ? ' Residual checks anchor agreement, not absolute GPS accuracy.' : ' Two anchors fit exactly; add a third to check agreement.';
                if (!transform.consistent) text += ' Anchors disagree; placement is blocked.';
                el('gpsFit').textContent = text;
            } catch (error) {el('gpsFit').textContent = error.message;}
        }
        function refresh() {
            const signature = context(), changed = lastContext !== null && signature !== lastContext;
            lastContext = signature;
            if (changed) cancel('Floor or layout changed. Capture GPS again.');
            const previous = el('gpsReceiver').value;
            el('gpsReceiver').replaceChildren();
            const blank = doc.createElement('option'); blank.value = ''; blank.textContent = 'Select placed receiver'; el('gpsReceiver').appendChild(blank);
            (hooks.getFloor()?.receivers || []).forEach((rec, i) => {
                const option = doc.createElement('option'); option.value = String(i); option.textContent = rec.entity_id; el('gpsReceiver').appendChild(option);
            });
            const draft = doc.createElement('option'); draft.value = 'draft'; draft.textContent = 'Current Place Receiver picker'; el('gpsReceiver').appendChild(draft);
            if ([...el('gpsReceiver').options].some(o => o.value === previous)) el('gpsReceiver').value = previous;
            fitStatus(); updateButtons();
        }
        async function loadLocations(g = null) {
            const requestId = ++locationsRequest;
            const requestedAt = now();
            try {
                const response = await hooks.fetch('/api/bps/gps_locations');
                if (!response.ok) throw new Error('Phone locations unavailable. Check Home Assistant access.');
                const data = await response.json();
                if (requestId !== locationsRequest || g && !current(g)) return;
                const selected = el('gpsPhone').value;
                const rows = Array.isArray(data.locations) ? data.locations.slice(0, 128) : [];
                el('gpsPhone').replaceChildren();
                const blank = doc.createElement('option'); blank.value = ''; blank.textContent = 'Browser only'; el('gpsPhone').appendChild(blank);
                rows.forEach(row => {
                    if (!/^(device_tracker|person)\.[a-z0-9_]{1,128}$/.test(row.entity_id)) return;
                    const option = doc.createElement('option'); option.value = row.entity_id;
                    option.textContent = `${row.name || row.entity_id} (${row.entity_id})`; el('gpsPhone').appendChild(option);
                });
                if (selected && !rows.some(row => row.entity_id === selected)) {
                    const missing = doc.createElement('option'); missing.value = selected; missing.textContent = selected + ' (stale or unavailable)'; el('gpsPhone').appendChild(missing);
                }
                el('gpsPhone').value = selected;
                if (g && capture && current(g) && selected) {
                    const row = rows.find(p => p.entity_id === selected);
                    // HA computes state age on its own clock. Translate it to
                    // this browser's clock and conservatively include the full
                    // request duration instead of comparing two wall clocks.
                    if (row && number(row.age_s, 0, MAX_AGE_MS / 1000)) capture.samples.add({latitude: row.latitude, longitude: row.longitude,
                        accuracy_m: row.gps_accuracy, timestamp: requestedAt-row.age_s*1000}, selected, now());
                    showCapture();
                }
            } catch (error) {if (!g || current(g)) status(error.message);}
        }
        function showCapture() {
            if (!capture) return;
            status(`Stand still: ${Math.floor((now()-capture.started)/1000)} s · ${capture.samples.count()} distinct observations. Stop after 30–60 s.`);
        }
        function stop() {
            const active = capture;
            if (!active) return;
            release(); capture = null; el('gpsCapture').disabled = false; el('gpsStop').disabled = true;
            if (!current(active.guard)) {cancel('Floor or layout changed. Capture GPS again.'); return;}
            try {
                if (now()-active.started < 30000) throw new Error('Capture at least 30 seconds while stationary.');
                fix = active.samples.result(now()); fix.captured_at = now(); fix.guard = active.guard;
                const {guard: privateGuard, ...fields} = fix; lastChosen = {...fields, floor: hooks.getFloor().name};
                status(`${fix.source}: ${fix.sample_count} observations · reported accuracy ${fix.reported_accuracy_m.toFixed(1)} m · spread ${fix.spread_m.toFixed(1)} m · conservative accuracy ${fix.accuracy_m.toFixed(1)} m.${fix.timestamp_kind === 'ha_state_update' ? ' HA state update age is used; raw GPS measurement age is unknown.' : ''} GPS cannot guarantee 1 m; correct the map manually if needed.`);
            } catch (error) {fix = null; status(error.message);}
            updateButtons();
        }
        function start() {
            if (!hooks.getFloor()) {status('Select a saved floor first.'); return;}
            cancel('Starting stationary capture.'); lastChosen = null; lastContext = context();
            const g = guard(); capture = {guard: g, started: now(), samples: collector()};
            el('gpsCapture').disabled = true; el('gpsStop').disabled = false;
            // Permission is requested only in this explicit click handler.
            if (geo) {
                try {watch = geo.watchPosition(position => {
                    if (!capture || !current(g)) return;
                    capture.samples.add({latitude: position.coords.latitude, longitude: position.coords.longitude,
                        accuracy_m: position.coords.accuracy, timestamp: position.timestamp}, 'browser', now()); showCapture();
                }, error => {if (capture && current(g)) status(`${error.code === 1 ? 'Browser location denied' : 'Browser location unavailable'}. Selected phone entity may still supply GPS.`);},
                {enableHighAccuracy: true, maximumAge: 0, timeout: 15000});}
                catch (error) {status('Browser location unavailable. Select your Companion App phone entity.');}
            } else status('Browser location unavailable. Select your Companion App phone entity.');
            if (el('gpsPhone').value) loadLocations(g);
            poll = every(() => {
                if (!current(g)) {cancel('Floor or layout changed. Capture GPS again.'); return;}
                if (el('gpsPhone').value) loadLocations(g); else showCapture();
            }, 5000);
            timer = later(stop, 45000);
        }
        function requireFix() {
            if (!fix || !current(fix.guard) || !observation(fix, fix.source, now())) throw new Error('GPS capture is stale or the layout changed. Capture again.');
            return fix;
        }
        function setAnchor(point) {
            anchorPoint = point; anchorGuard = guard();
            el('gpsMapPoint').textContent = `Selected map point: ${point.x.toFixed(1)}, ${point.y.toFixed(1)} px.`;
        }
        el('gpsPickPoint').addEventListener('click', () => {
            if (!hooks.getFloor()) {status('Select a saved floor first.'); return;}
            try {
                picked?.(); let g;
                picked = hooks.pickMapPoint(point => {picked = null; if (current(g)) setAnchor(point);});
                g = guard();
                status('Click a known point on the map.');
            } catch (error) {status(error.message);}
        });
        el('gpsUseReceiver').addEventListener('click', () => {
            const value = el('gpsReceiver').value, receiver = value !== '' && value !== 'draft' ? hooks.getFloor()?.receivers?.[Number(value)] : null;
            if (receiver?.cords) setAnchor({...receiver.cords}); else status('Select a placed receiver to use its map point.');
        });
        el('gpsUseFix').addEventListener('click', () => {
            try {const p = requireFix(); el('gpsLatitude').value = String(p.latitude); el('gpsLongitude').value = String(p.longitude);
                el('gpsAccuracy').value = String(p.accuracy_m); status('Captured GPS copied. Select its known map point, then add the anchor.');}
            catch (error) {status(error.message);}
        });
        el('gpsAddAnchor').addEventListener('click', () => {
            try {
                if (!anchorPoint || !current(anchorGuard)) throw new Error('Select a known map point first.');
                const input = id => {const e = el(id); if (e.value.trim() === '' || e.validity?.badInput) throw new Error('Enter GPS latitude, longitude and accuracy.'); return Number(e.value);};
                const anchor = {...anchorPoint, latitude: input('gpsLatitude'), longitude: input('gpsLongitude'),
                    accuracy_m: input('gpsAccuracy'), label: el('gpsAnchorLabel').value.trim().slice(0, 80)};
                if (!validGPS(anchor) || !number(anchor.accuracy_m, 0, 1000)) throw new Error('Enter valid GPS and accuracy between 0 and 1000 metres.');
                const floor = hooks.getFloor(), anchors = [...(floor.georeference?.anchors || []), anchor];
                if (anchors.length > MAX_ANCHORS) throw new Error('Use at most 12 anchors.');
                if (anchors.length >= 2) fit(anchors);
                floor.georeference = {version: 1, anchors}; lastContext = context(); hooks.stageChange();
                cancel('GPS anchor staged. Save Floor Plan to persist.'); refresh();
            } catch (error) {status(error.message);}
        });
        el('gpsPreview').addEventListener('click', () => {
            try {
                const p = requireFix(), floor = hooks.getFloor(), target = hooks.getTarget(el('gpsReceiver').value);
                if (!target) throw new Error('Select a receiver, or select one in the active Place Receiver picker first.');
                const transform = fit(floor.georeference?.anchors), projected = project(transform, p);
                const bounds = hooks.getBounds();
                if (!number(projected.x, 0, bounds.width) || !number(projected.y, 0, bounds.height)) throw new Error('GPS position is outside the map. Review the anchors or correct it manually.');
                preview = {...projected, target, guard: guard(), source: p.source};
                status(`Preview for ${target.label}: accuracy radius ${projected.accuracy_m.toFixed(1)} m including map anchors. Confirm to stage, then Save Floor Plan. Manual correction remains available.`);
                updateButtons(); hooks.redraw();
            } catch (error) {preview = null; status(error.message); updateButtons(); hooks.redraw();}
        });
        el('gpsConfirm').addEventListener('click', () => {
            try {
                requireFix();
                if (!preview || !current(preview.guard) || !hooks.stageReceiver(preview.target, {x: preview.x, y: preview.y})) throw new Error('Receiver or layout changed. Preview again.');
                preview = null; fix = null; status('GPS receiver position staged. Save the receiver picker if adding a new receiver, then Save Floor Plan.');
                hooks.stageChange(); lastContext = context(); updateButtons(); hooks.redraw();
            } catch (error) {preview = null; status(error.message); updateButtons(); hooks.redraw();}
        });
        el('gpsReceiver').addEventListener('change', () => {preview = null; updateButtons(); hooks.redraw();});
        el('gpsPhone').addEventListener('change', () => cancel('Phone source changed. Capture again.'));
        el('gpsRefreshPhones').addEventListener('click', () => loadLocations());
        el('gpsCapture').addEventListener('click', start); el('gpsStop').addEventListener('click', stop);
        el('gpsCancel').addEventListener('click', () => cancel());
        win.addEventListener('pagehide', () => cancel()); win.addEventListener('beforeunload', release);
        refresh();
        return {refresh, cancel, draw(ctx, zoom = 1) {
            if (!preview || !current(preview.guard)) return;
            ctx.save(); ctx.strokeStyle = '#06b6d4'; ctx.fillStyle = 'rgba(6,182,212,0.12)'; ctx.lineWidth = 2/zoom;
            ctx.beginPath(); ctx.arc(preview.x, preview.y, preview.radius_px, 0, Math.PI*2); ctx.fill(); ctx.stroke();
            ctx.fillStyle = '#06b6d4'; ctx.beginPath(); ctx.arc(preview.x, preview.y, 5/zoom, 0, Math.PI*2); ctx.fill(); ctx.restore();
        }, snapshot() {
            if (!lastChosen) return {active: !!capture, status: message};
            return {active: !!capture, status: message, usable_for_placement: !!fix && current(fix.guard) && now()-fix.timestamp <= MAX_AGE_MS,
                chosen_gps: {...lastChosen, age_s: (now()-lastChosen.timestamp)/1000,
                stale: now()-lastChosen.timestamp > MAX_AGE_MS},
                preview: preview ? {x: preview.x, y: preview.y, accuracy_m: preview.accuracy_m, receiver: preview.target.label} : null};
        }};
    }
    const api = {fit, local, fromLocal, project, observation, summarize, choose, collector, init};
    root.BPSGPS = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
