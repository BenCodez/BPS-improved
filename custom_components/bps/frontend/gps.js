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
    function parseCoordinates(text) {
        if (typeof text !== 'string' || text.length > 100) return null;
        const match = text.trim().match(/^([+-]?\d+(?:\.\d+)?)\s*[,;\s]\s*([+-]?\d+(?:\.\d+)?)$/);
        if (!match) return null;
        const p = {latitude: Number(match[1]), longitude: Number(match[2])};
        return validGPS(p) ? p : null;
    }
    function mergeSurveyPosition(layout, candidate, position, beforeContext, afterContext) {
        let before, after;
        try {before = JSON.parse(beforeContext); after = JSON.parse(afterContext);} catch (_) {return false;}
        if (!Array.isArray(before) || !Array.isArray(after) || before.length !== 6 || after.length !== 6) return false;
        const a = before[5];
        const floor = (layout.floor || []).filter(f => f.name === candidate.floor);
        const b = floor.length === 1 ? floor[0] : null;
        if (!a || !b || a.name !== candidate.floor || a.scale !== b.scale
            || JSON.stringify(a.georeference) !== JSON.stringify(b.georeference)) return false;
        // Navigating to another floor cannot leave the old floor's committed
        // move out of the next full save. Pixel-frame edits on this floor can.
        if (after[5]?.name === candidate.floor && before.slice(2, 5).some((value, i) => value !== after[i + 2])) return false;
        const prior = (a.receivers || []).filter(r => r.entity_id === candidate.receiver);
        const current = (b.receivers || []).filter(r => r.entity_id === candidate.receiver);
        const targets = floor.length === 1 ? (floor[0].receivers || []).filter(r => r.entity_id === candidate.receiver) : [];
        if (prior.length !== 1 || current.length !== 1 || targets.length !== 1
            || !number(position?.x, 0, 1e6) || !number(position?.y, 0, 1e6)) return false;
        if (['scanner_mac', 'scanner_config_entry_id'].some(k => prior[0][k] !== current[0][k])
            || ['x', 'y'].some(k => prior[0].cords?.[k] !== current[0].cords?.[k])) return false;
        targets[0].cords = {...targets[0].cords, x: position.x, y: position.y};
        return true;
    }
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
        const origin = recent[0], points = recent.map(p => {
            // Every observation already has valid GPS. Exceeding the local
            // property limit here is a jump, rather than an unavailable source.
            try {return local(p, origin);}
            catch {throw new Error('GPS moved or jumped during capture. Stand still and capture again.');}
        });
        const center = {x: median(points.map(p => p.x)), y: median(points.map(p => p.y))};
        const spread_m = Math.max(...points.map(p => Math.hypot(p.x-center.x, p.y-center.y)));
        const reported_accuracy_m = Math.max(...recent.map(p => p.accuracy_m));
        // Repeated correlated GPS fixes do not buy sqrt(N) precision. Movement
        // outside the reported noise envelope makes this stationary capture unusable.
        if (spread_m > Math.max(5, reported_accuracy_m * 2)) throw new Error('GPS moved or jumped during capture. Stand still and capture again.');
        if (recent.length < 2 || Math.max(...recent.map(p => p.timestamp))-Math.min(...recent.map(p => p.timestamp)) < 20000)
            throw new Error('Need distinct GPS fixes spanning at least 20 seconds. Capture again or use a more frequently updated source.');
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

    function surveyController(hooks, environment = {}) {
        const doc = environment.document || root.document, win = environment.window || root.window;
        const geo = environment.geolocation || root.navigator?.geolocation;
        const now = environment.now || (() => Date.now());
        const later = environment.setTimeout || root.setTimeout, clear = environment.clearTimeout || root.clearTimeout;
        const el = id => doc.getElementById(id);
        if (!el('gpsSurveyStart')) return null;
        let active = null, generation = 0, watch = null, timer = null, loading = null;
        let candidates = [], samples = [], verifying = false;
        const status = text => {el('gpsSurveyStatus').textContent = text;};
        const signature = () => hooks.getContext();
        const valid = g => g === generation && active && active.context === signature() && !doc.hidden;
        const json = async (body, keepalive = false) => {
            const response = await hooks.fetch('/api/bps/phone_survey', body ? {method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), keepalive} : undefined);
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Phone survey is unavailable.');
            return data;
        };
        function render() {
            el('gpsSurveyStart').disabled = !!active; el('gpsSurveyStop').disabled = !active;
            el('gpsSurveyCandidates').replaceChildren();
            candidates.filter(c => c.floor === hooks.getFloor()?.name).forEach(c => {
                const row = doc.createElement('div'), details = doc.createElement('p');
                const gps = c.gps || {}, position = c.position || {};
                details.textContent = `${c.receiver}: averaged ${gps.sample_count || 0} GPS fixes · reported accuracy ${Number(gps.reported_accuracy_m).toFixed(1)} m · spread ${Number(gps.spread_m).toFixed(1)} m · map uncertainty ${Number(c.map_accuracy_m).toFixed(1)} m · ${c.status === 'verified' ? 'verified and saved' : 'awaiting verification'}.`;
                row.appendChild(details);
                if (c.status !== 'verified') {
                    const verify = doc.createElement('button'); verify.type = 'button';
                    verify.className = 'bps-btn bps-btn-primary'; verify.textContent = `Verify and save ${c.receiver}`;
                    verify.disabled = verifying;
                    verify.addEventListener('click', async () => {
                        if (verifying) return;
                        const bounds = hooks.getBounds();
                        if (!number(position.x, 0, bounds.width) || !number(position.y, 0, bounds.height)) {
                            status('GPS candidate is outside the current map. Review saved anchors before verifying.'); return;
                        }
                        try {if (hooks.surveyCanStart?.() === false) {status('Save or discard floor edits before verifying a candidate.'); return;}}
                        catch (error) {status(error.message); return;}
                        verifying = true; render();
                        const capturedUIContext = hooks.getContext();
                        // Stop collection before committing a saved map change.
                        stop('Verifying receiver position.');
                        try {
                            const result = await json({action: 'verify', candidate_id: c.id});
                            candidates = candidates.map(row => row.id === c.id ? result.candidate : row);
                            await hooks.surveySaved?.(result.candidate, result, capturedUIContext);
                            status(result.warning || `${c.receiver} position verified and saved.`);
                        } catch (error) {status(error.message);}
                        finally {verifying = false; render(); hooks.redraw();}
                    }); row.appendChild(verify);
                    const remove = doc.createElement('button'); remove.type = 'button'; remove.textContent = 'Discard candidate';
                    remove.className = 'bps-btn bps-btn-outline';
                    remove.addEventListener('click', async () => {
                        try {await json({action: 'remove', candidate_id: c.id}); candidates = candidates.filter(row => row.id !== c.id); render();}
                        catch (error) {status(error.message);}
                    }); row.appendChild(remove);
                    details.textContent += ` Proposed map position ${Number(position.x).toFixed(1)}, ${Number(position.y).toFixed(1)} px. Check it against the real receiver before verifying.`;
                }
                el('gpsSurveyCandidates').appendChild(row);
            });
        }
        function stop(reason = 'Auto survey stopped. Saved candidates remain available for verification.') {
            const token = active?.session;
            generation++; active = null; samples = [];
            if (watch !== null) geo?.clearWatch(watch);
            if (timer !== null) clear(timer);
            watch = timer = null;
            if (token) json({action: 'stop', session: token}, true).catch(() => {});
            status(reason); render();
        }
        function selectRows(id, rows, blankText) {
            const select = el(id), selected = select.value; select.replaceChildren();
            const blank = doc.createElement('option'); blank.value = ''; blank.textContent = blankText; select.appendChild(blank);
            rows.forEach(r => {if (typeof r.key !== 'string') return; const option = doc.createElement('option');
                option.value = r.key; option.textContent = r.name || r.key; select.appendChild(option);});
            if (rows.some(r => r.key === selected)) select.value = selected;
            else select.value = '';
        }
        async function load() {
            if (loading) return loading;
            loading = (async () => {
                try {const data = await json(); selectRows('gpsSurveyPhone', data.trackers || [], 'Select your BLE phone tracker');
                    selectRows('gpsSurveyNotify', data.notify_services || [], 'Select your phone Mobile App service');
                    candidates = Array.isArray(data.candidates) ? data.candidates.slice(0, 32) : []; render(); hooks.redraw();}
                catch (error) {status(error.message);}
                finally {loading = null;}
            })();
            return loading;
        }
        async function poll(g) {
            if (!valid(g)) {if (active) stop('Floor changed or page hidden. Auto survey stopped.'); return;}
            try {
                const batch = samples.filter(p => observation(p, 'browser', now()));
                const data = await json({action: 'poll', session: active.session, browser_samples: batch});
                if (!valid(g)) return;
                if (data.candidate) {candidates = candidates.filter(c => !(c.floor === data.candidate.floor && c.receiver === data.candidate.receiver && c.phone === data.candidate.phone)); candidates.push(data.candidate); render(); hooks.redraw();}
                let text = data.status || 'Auto survey active.';
                if (data.capturing) text += ` ${Math.floor(data.elapsed_s)} s · ${data.sample_count} distinct fixes.`;
                if (data.gps_request?.status === 'requested') text += ' Phone GPS update requested; response is not yet known.';
                if (data.gps_request?.status === 'request_failed') text += ' Phone GPS request failed; capture can use browser GPS.';
                status(text);
            } catch (error) {if (valid(g)) {stop(error.message); return;}}
            if (valid(g)) timer = later(() => {timer = null; poll(g);}, 2000);
        }
        async function start() {
            if (active || doc.hidden) return;
            try {if (!hooks.getFloor() || hooks.surveyCanStart?.() === false) {status('Save or discard floor edits, including anchors, before starting auto survey.'); return;}}
            catch (error) {status(error.message); return;}
            const phone = el('gpsSurveyPhone').value;
            if (!phone) {status('Select your own Bermuda BLE phone tracker first.'); return;}
            hooks.surveyStarting?.();
            const g = ++generation, ctx = signature(), name = hooks.getFloor().name;
            // Reserve the local operation before awaiting start to prevent
            // double-clicks or an old start response reviving a stopped survey.
            active = {session: null, context: ctx}; samples = []; render();
            try {
                const result = await json({action: 'start', floor: name, phone,
                    gps_entity: el('gpsPhone').value || '', browser_time_ms: now(),
                    receiver: el('gpsReceiver').value !== '' && el('gpsReceiver').value !== 'draft'
                        ? hooks.getFloor().receivers?.[Number(el('gpsReceiver').value)]?.entity_id || '' : '',
                    request_gps: !!el('gpsSurveyRequestGPS').checked, notify_service: el('gpsSurveyNotify').value || ''});
                if (!valid(g)) {json({action: 'stop', session: result.session}, true).catch(() => {}); return;}
                active.session = result.session;
                if (geo) {
                    try {watch = geo.watchPosition(position => {
                        if (!valid(g)) return;
                        const p = observation({latitude: position.coords.latitude, longitude: position.coords.longitude,
                            accuracy_m: position.coords.accuracy, timestamp: position.timestamp}, 'browser', now());
                        if (p && !samples.some(s => s.timestamp === p.timestamp)) {
                            samples = samples.filter(s => now()-s.timestamp <= MAX_AGE_MS); samples.push(p);
                            samples = samples.slice(-MAX_SAMPLES);
                        }
                    }, () => {if (valid(g)) status('Browser GPS unavailable. Selected HA phone location may still supply fixes.');},
                    {enableHighAccuracy: true, maximumAge: 0, timeout: 15000});}
                    catch {status('Browser GPS unavailable. Selected HA location may still supply fixes.');}
                }
                await poll(g);
            } catch (error) {if (valid(g)) stop(error.message);}
        }
        el('gpsSurveyRefresh').addEventListener('click', load);
        el('gpsSurveyStart').addEventListener('click', start);
        el('gpsSurveyStop').addEventListener('click', () => stop());
        ['gpsSurveyPhone', 'gpsPhone', 'gpsReceiver', 'gpsSurveyNotify', 'gpsSurveyRequestGPS'].forEach(id =>
            el(id).addEventListener('change', () => {if (id === 'gpsPhone') hooks.surveySourceChanged?.();
                if (active) stop('Phone source, selected receiver or GPS request option changed. Start auto survey again.');}));
        doc.addEventListener?.('visibilitychange', () => {if (doc.hidden && active) stop('Page hidden. Auto survey stopped.');});
        win.addEventListener('pagehide', () => stop()); win.addEventListener('beforeunload', () => stop());
        status('Auto survey is off. Averaged GPS candidates require verification before a receiver moves.'); render();
        return {load, stop, isVerifying: () => verifying,
            refresh() {if (active && active.context !== signature()) stop('Floor or layout changed. Auto survey stopped.'); render();},
            draw(ctx, zoom = 1) {for (const c of candidates) {
                if (c.floor !== hooks.getFloor()?.name || c.status === 'verified') continue;
                const p = c.position; if (!p || !number(p.x, 0, 1e6) || !number(p.y, 0, 1e6)) continue;
                ctx.save(); ctx.strokeStyle = '#f59e0b'; ctx.lineWidth = 2/zoom;
                let radius = null;
                try {radius = c.map_accuracy_m * fit(c.context?.floor?.georeference?.anchors).scale;} catch (_) {}
                if (number(radius, 0, 1e9)) {ctx.beginPath(); ctx.arc(p.x, p.y, radius, 0, Math.PI*2); ctx.stroke();}
                ctx.beginPath(); ctx.arc(p.x, p.y, 8/zoom, 0, Math.PI*2); ctx.stroke(); ctx.restore();
            }}, snapshot: () => ({active: !!active, candidate_count: candidates.length, status: el('gpsSurveyStatus').textContent})};
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
        let locationsFlight = null;
        let survey = null;
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
            survey?.stop();
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
            fitStatus(); updateButtons(); survey?.refresh();
        }
        function loadLocations(g = null) {
            if (locationsFlight) return locationsFlight;
            locationsFlight = readLocations(g).finally(() => {locationsFlight = null;});
            return locationsFlight;
        }
        async function readLocations(g = null) {
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
                picked = hooks.pickMapPoint(point => {picked = null;
                    if (current(g)) {setAnchor(point); status('Map point selected. Enter its latitude, longitude and accuracy, then add the anchor.');}
                    else status('Floor or layout changed while selecting the point. Select its map point again.');});
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
                const input = (id, label, min, max) => {const e = el(id);
                    if (e.value.trim() === '' || e.validity?.badInput) throw new Error(`Enter GPS ${label}.`);
                    const value = Number(e.value);
                    if (!number(value, min, max)) throw new Error(`Enter valid GPS ${label} between ${min} and ${max}.`);
                    return value;};
                const anchor = {...anchorPoint, latitude: input('gpsLatitude', 'latitude', -85, 85), longitude: input('gpsLongitude', 'longitude', -180, 180),
                    accuracy_m: input('gpsAccuracy', 'accuracy in metres', 0, 1000), label: el('gpsAnchorLabel').value.trim().slice(0, 80)};
                const floor = hooks.getFloor(), anchors = [...(floor.georeference?.anchors || []), anchor];
                if (anchors.length > MAX_ANCHORS) throw new Error('Use at most 12 anchors.');
                if (anchors.length >= 2) fit(anchors);
                floor.georeference = {version: 1, anchors}; lastContext = context(); hooks.stageChange();
                cancel('GPS anchor staged. Save Floor Plan to persist.'); refresh();
            } catch (error) {status(error.message);}
        });
        el('gpsLatitude').addEventListener('paste', event => {
            const point = parseCoordinates(event.clipboardData?.getData('text'));
            if (!point) return;
            event.preventDefault(); el('gpsLatitude').value = String(point.latitude); el('gpsLongitude').value = String(point.longitude);
            status('Latitude and longitude filled. Enter anchor accuracy, then add the anchor.');
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
        survey = surveyController({...hooks, surveyStarting: () => cancel('Starting auto survey.'),
            surveySourceChanged: () => cancel('Phone source changed. Capture again.')}, environment);
        win.addEventListener('pagehide', () => cancel()); win.addEventListener('beforeunload', () => {release(); survey?.stop();});
        refresh();
        return {refresh, cancel, isVerifying: () => !!survey?.isVerifying(), draw(ctx, zoom = 1) {
            survey?.draw(ctx, zoom);
            if (!preview || !current(preview.guard)) return;
            ctx.save(); ctx.strokeStyle = '#06b6d4'; ctx.fillStyle = 'rgba(6,182,212,0.12)'; ctx.lineWidth = 2/zoom;
            ctx.beginPath(); ctx.arc(preview.x, preview.y, preview.radius_px, 0, Math.PI*2); ctx.fill(); ctx.stroke();
            ctx.fillStyle = '#06b6d4'; ctx.beginPath(); ctx.arc(preview.x, preview.y, 5/zoom, 0, Math.PI*2); ctx.fill(); ctx.restore();
        }, snapshot() {
            if (!lastChosen) return {active: !!capture, status: message, auto_survey: survey?.snapshot()};
            return {active: !!capture, status: message, usable_for_placement: !!fix && current(fix.guard) && now()-fix.timestamp <= MAX_AGE_MS,
                chosen_gps: {...lastChosen, age_s: (now()-lastChosen.timestamp)/1000,
                stale: now()-lastChosen.timestamp > MAX_AGE_MS},
                preview: preview ? {x: preview.x, y: preview.y, accuracy_m: preview.accuracy_m, receiver: preview.target.label} : null,
                auto_survey: survey?.snapshot()};
        }};
    }
    const api = {fit, local, fromLocal, project, observation, summarize, choose, collector, parseCoordinates, mergeSurveyPosition, surveyController, init};
    root.BPSGPS = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
