/* Shared optional map/editor helpers. Distances are metres; map points are floor pixels. */
(function (root) {
    'use strict';
    const TYPES = ['building', 'dense_trees', 'light_vegetation', 'custom', 'fence'];
    const MATERIALS = ['unknown', 'light', 'wood', 'heavy', 'metal'];
    const POLICIES = ['auto', 'normal', 'prefer', 'deprioritize', 'ignore'];
    const MAX_UNCERTAINTY_THRESHOLD_M = 10000;
    const MAX_ENVIRONMENT_POLYGONS = 128;
    const GROUP_LIMITS = Object.freeze({groups: 32, beacons: 16, id_length: 64});
    const slug = value => typeof value === 'string' && value.length > 0
        && value.length <= GROUP_LIMITS.id_length && !/[^a-z0-9_]/.test(value);
    const number = v => typeof v === 'number' && Number.isFinite(v);
    const groups = layout => Array.isArray(layout && layout.tracker_groups)
        ? layout.tracker_groups.filter(g => g && typeof g.id === 'string'
            && (g.name === undefined || typeof g.name === 'string') && Array.isArray(g.beacons))
            .map(g => ({...g, name: (typeof g.name === 'string' && g.name.trim() || g.id).slice(0, 128),
                enabled: g.enabled === undefined || g.enabled === true})) : [];
    const environment = floor => Array.isArray(floor && floor.environment)
        ? floor.environment.filter(p => p && typeof p.id === 'string' && typeof p.name === 'string') : [];
    function settings(layout) {
        const s = layout && layout.outdoor_tracking || {};
        return {enabled: s.enabled === true, long_range_intervals: s.long_range_intervals === true, show_uncertainty: s.show_uncertainty !== false,
            obstruction_strength: number(s.obstruction_strength) && s.obstruction_strength >= 1
                && s.obstruction_strength <= 4 ? s.obstruction_strength : 1,
            hide_uncertainty_below_m: number(s.hide_uncertainty_below_m) && s.hide_uncertainty_below_m >= 0
                && s.hide_uncertainty_below_m <= MAX_UNCERTAINTY_THRESHOLD_M
                ? s.hide_uncertainty_below_m : 0};
    }
    async function positionSnapshot(response) {
        if (!response || !response.ok && response.status !== 404) return null;
        const data = await response.json();
        if (response.ok) return Array.isArray(data) ? data : null;
        // The existing API reports its authoritative empty set with this 404.
        // Other HTTP failures and malformed responses cannot revoke a fix.
        return data && data.error === 'No data available' ? [] : null;
    }
    // Group fixes have no solver circles of their own. Preserve every member's
    // measurement on this floor; equal receiver locations with different ranges
    // still carry distinct information, while identical circles draw only once.
    function distanceRadii(row, snapshot, sameFloor = (a, b) => a === b) {
        if (!row?.group) return row?.radii;
        const circles = [], seen = new Set();
        for (const member of row.beacon_positions || []) {
            if (!member || member.floor && row.floor && !sameFloor(member.floor, row.floor)) continue;
            const source = Array.isArray(member.radii) ? member
                : (snapshot || []).find(p => p?.ent === member.ent && !p.group);
            if (!source || source.floor && row.floor && !sameFloor(source.floor, row.floor)) continue;
            for (const circle of Array.isArray(source.radii) ? source.radii : []) {
                if (!Array.isArray(circle) || !circle.slice(0, 3).every(number)
                    || circle.length < 3 || circle[2] <= 0) continue;
                const key = JSON.stringify(circle.slice(0, 3));
                if (!seen.has(key)) {seen.add(key); circles.push(circle);}
            }
        }
        return circles;
    }
    const elapsedNow = () => typeof performance !== 'undefined' ? performance.now() : Date.now();
    function freshDistanceIntervals(intervals, now = elapsedNow()) {
        return (intervals || []).filter(c => {
            const age = c[4];
            return number(age?.server_age_s) && number(age?.received_elapsed_ms) && now >= age.received_elapsed_ms
                && age.server_age_s >= 0 && age.server_age_s + (now - age.received_elapsed_ms) / 1000 <= 3;
        });
    }
    function distanceIntervals(row, snapshot, sameFloor = (a, b) => a === b, received = elapsedNow()) {
        const sources = row?.group ? (row.beacon_positions || []).map(member =>
            (snapshot || []).find(p => p?.ent === member?.ent && !p.group)) : [row];
        const intervals = [], seen = new Set();
        for (const source of sources) {
            if (!source || source.floor && row.floor && !sameFloor(source.floor, row.floor)) continue;
            const serverTime = source.outdoor?.server_time;
            const transportAge = number(source._bps_transport_age_s) && source._bps_transport_age_s >= 0 ? source._bps_transport_age_s : 0;
            for (const reading of source.outdoor?.receiver_diagnostics || []) {
                const band = reading.horizontal_interval, raw = reading.range_interval, xy = reading.receiver_xy_px;
                if (reading.excluded || reading.used === false || raw?.status !== 'available' || raw.center_m < 10
                    || !number(serverTime) || !number(raw.source_observed) || serverTime < raw.source_observed
                    || serverTime - raw.source_observed + transportAge > 3
                    || band?.status !== 'available' || !Array.isArray(xy) || !xy.slice(0, 2).every(number) || xy.length < 2
                    || !number(band.lower_px) || !number(band.upper_px) || band.lower_px < 0 || band.upper_px <= band.lower_px) continue;
                const item = [xy[0], xy[1], band.lower_px, band.upper_px, {source_observed: raw.source_observed,
                    server_age_s: serverTime - raw.source_observed + transportAge, received_elapsed_ms: received}];
                const key = JSON.stringify(item.slice(0, 4));
                if (!seen.has(key)) {seen.add(key); intervals.push(item);}
            }
        }
        return intervals;
    }
    function displayLimit(outdoor) {
        return number(outdoor?.display_radius_limit_m) && outdoor.display_radius_limit_m >= 0
            ? outdoor.display_radius_limit_m : Infinity;
    }
    function markerPlacement(pos, width, height, margin = 24) {
        const valid = number(pos?.x) && number(pos?.y) && number(width) && width > 0 && number(height) && height > 0;
        const offMap = valid && (pos.x < 0 || pos.x > width || pos.y < 0 || pos.y > height);
        const clamp = (value, size) => Math.max(Math.min(margin, size / 2), Math.min(size - Math.min(margin, size / 2), value));
        return {x: offMap ? clamp(pos.x, width) : pos?.x, y: offMap ? clamp(pos.y, height) : pos?.y, offMap};
    }
    function markerStatus(outdoor, offMap = false) {
        const labels = [];
        if (outdoor?.detection_only) labels.push('Detected only · direction unknown');
        if (offMap) labels.push('Off map · inferred');
        return labels.join(' · ');
    }
    function uncertaintyRadius(outdoor, scale, config) {
        if (!config || config.enabled !== true || config.show_uncertainty === false) return null;
        const estimate = outdoor && number(outdoor.display_uncertainty_m)
            ? outdoor.display_uncertainty_m : outdoor && outdoor.estimated_uncertainty_m;
        const metres = number(estimate) ? Math.min(estimate, displayLimit(outdoor)) : estimate;
        if (!number(metres) || metres < 0 || !number(scale) || scale <= 0) return null;
        if (metres < (config.hide_uncertainty_below_m || 0)) return null;
        const radius = metres * scale;
        return number(radius) ? radius : null;
    }
    function isStale(outdoor, receivedAt, now = Date.now()) {
        const limit = outdoor && number(outdoor.stale_after_s) && outdoor.stale_after_s > 0 ? outdoor.stale_after_s : 30;
        return !!(outdoor && (outdoor.stale === true || outdoor.confidence === 'stale'
            || number(outdoor.position_age_s) && outdoor.position_age_s >= limit))
            || number(receivedAt) && now - receivedAt >= limit * 1000;
    }
    function fixTime(row, now = Date.now()) {
        const observed = row && row.outdoor && row.outdoor.use_observation_age === false
            ? row.updated : row && row.outdoor && row.outdoor.observed;
        const timestamp = number(observed) && observed >= 0 ? observed : row && row.updated;
        if (number(timestamp) && timestamp >= 0 && number(timestamp * 1000)) return Math.min(now, timestamp * 1000);
        const age = row && row.outdoor && row.outdoor.position_age_s;
        return number(age) && age >= 0 ? now - age * 1000 : now;
    }
    function stabilizeUncertainty(row, previous, now = Date.now()) {
        const raw = row?.outdoor?.estimated_uncertainty_m;
        // Re-solving cached BLE readings advances publication time, not the
        // observation clock. Require distinct measurements to confirm growth.
        const source = number(row?.outdoor?.observed) ? row.outdoor.observed : row?.updated;
        const stamp = source * 1000;
        if (!number(raw) || raw < 0 || !number(stamp) || stamp < 0) {
            return {outdoor: row?.outdoor, uncertaintyState: null};
        }
        const prior = previous?.uncertaintyState;
        const stale = isStale(row.outdoor, fixTime(row, now), now);
        const limit = number(row.outdoor.stale_after_s) && row.outdoor.stale_after_s > 0
            ? row.outdoor.stale_after_s * 1000 : 30000;
        const reset = !prior || prior.floor !== row.floor || !stale && (
            stamp - prior.stamp >= limit || isStale(previous?.outdoor, previous?.receivedAt, now));
        let state;
        if (reset) {
            state = {floor: row.floor, stamp, radius: raw, elevatedSince: null, elevatedCount: 0};
        } else if (stale || stamp <= prior.stamp) {
            // A redraw/cached poll cannot confirm a spike or make old data fresh.
            state = prior;
        } else {
            state = {...prior, stamp};
            const dt = Math.min(2, (stamp - prior.stamp) / 1000);
            if (raw > prior.radius + Math.max(1, prior.radius * .25)) {
                state.elevatedSince ??= stamp;
                state.elevatedCount += 1;
                // Require three distinct elevated fixes spanning five seconds.
                if (state.elevatedCount >= 3 && stamp - state.elevatedSince >= 5000) {
                    state.radius = Math.min(raw, Math.max(prior.radius + .5 * dt,
                        prior.radius * Math.pow(1.25, dt)));
                }
            } else {
                state.elevatedSince = null;
                state.elevatedCount = 0;
                const tau = raw < prior.radius ? 1.5 : 3;
                state.radius = prior.radius + (raw - prior.radius) * (1 - Math.exp(-dt / tau));
            }
        }
        // Display-only: retain the instantaneous estimate, confidence and raw
        // payload for diagnostics/calibration. Marker coordinates never change.
        const cap = displayLimit(row.outdoor);
        // Apply the spatial bound immediately, even on cached/stale polls. Keep
        // the bounded value as smoothing state so a changing receiver footprint
        // cannot reveal the previous oversized circle on the next observation.
        state = {...state, radius: Math.min(state.radius, cap)};
        return {outdoor: {...row.outdoor, display_uncertainty_m: state.radius,
            display_uncertainty_capped: raw > cap}, uncertaintyState: state};
    }
    function drawUncertainty(ctx, pos, scale, config, color, zoom = 1, now = Date.now()) {
        const radius = uncertaintyRadius(pos && pos.outdoor, scale, config);
        if (radius === null || !number(pos.x) || !number(pos.y)) return false;
        const stale = isStale(pos.outdoor, pos.receivedAt, now);
        ctx.save();
        ctx.beginPath();
        ctx.arc(pos.x, pos.y, radius, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.globalAlpha = stale ? 0.04 : 0.10;
        ctx.fill();
        ctx.globalAlpha = stale ? 0.45 : 0.7;
        ctx.strokeStyle = stale ? '#888' : color;
        ctx.lineWidth = 2 / zoom;
        ctx.setLineDash(stale ? [3 / zoom, 6 / zoom]
            : pos.outdoor.confidence === 'poor' ? [8 / zoom, 5 / zoom] : []);
        ctx.stroke();
        ctx.restore();
        return true;
    }
    function beaconNumber(beacon, group) {
        const index = group?.beacons?.indexOf(beacon.ent) ?? -1;
        return index >= 0 ? String(index + 1) : '•';
    }
    // Small, quiet member icons, drawn before the full-size fused marker.
    // Keep numbering tied to membership, even when a beacon has no fix.
    function drawMiniBeacon(ctx, beacon, mainSize, color, label, staleAfter, zoom = 1, now = Date.now()) {
        if (!Array.isArray(beacon?.cords) || beacon.cords.length < 2
            || !beacon.cords.every(number)) return false;
        const size = Math.min(mainSize * 0.3, 24 / zoom);
        if (!number(size) || size <= 0) return false;
        const [x, y] = beacon.cords;
        const muted = beacon.used === false || isStale({position_age_s: beacon.age_s,
            stale_after_s: staleAfter}, fixTime(beacon, now), now);
        ctx.save();
        ctx.globalAlpha = muted ? 0.45 : 0.75;
        ctx.fillStyle = muted ? '#888' : color;
        ctx.strokeStyle = muted ? '#666' : color;
        ctx.lineWidth = 1 / zoom;
        ctx.setLineDash(muted ? [2 / zoom, 2 / zoom] : []);
        ctx.beginPath(); ctx.arc(x, y, size / 2, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
        ctx.fillStyle = '#fff';
        ctx.font = `600 ${size * 0.6}px system-ui, sans-serif`;
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(label, x, y);
        ctx.restore();
        return true;
    }
    function validPolygon(points) {
        if (!Array.isArray(points) || points.length < 3 || points.length > 256
            || points.some(p => !p || !number(p.x) || !number(p.y))) return false;
        const cross = (a, b, c) => (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
        const on = (a, b, c) => Math.abs(cross(a, b, c)) < 1e-9
            && c.x >= Math.min(a.x, b.x) && c.x <= Math.max(a.x, b.x)
            && c.y >= Math.min(a.y, b.y) && c.y <= Math.max(a.y, b.y);
        const intersects = (a, b, c, d) => cross(a, b, c) * cross(a, b, d) < 0
            && cross(c, d, a) * cross(c, d, b) < 0
            || on(a, b, c) || on(a, b, d) || on(c, d, a) || on(c, d, b);
        let area = 0;
        for (let i = 0; i < points.length; i++) {
            const a = points[i], b = points[(i + 1) % points.length];
            if (a.x === b.x && a.y === b.y) return false;
            area += a.x * b.y - b.x * a.y;
            for (let j = i + 2; j < points.length; j++) {
                if (i === 0 && j === points.length - 1) continue;
                if (intersects(a, b, points[j], points[(j + 1) % points.length])) return false;
            }
        }
        return Math.abs(area) > 1e-6;
    }
    function upsertEnvironment(floor, item) {
        if (!floor || !item || typeof item.id !== 'string' || !item.id
            || typeof item.name !== 'string' || !item.name.trim() || item.name.length > 256
            || !TYPES.includes(item.type) || !MATERIALS.includes(item.material)
            || !validEnvironmentPoints(item)) throw new Error(item?.type === 'fence'
                ? 'Use a named open fence with at least two distinct points and no crossing or retraced segments.'
                : 'Use a named polygon with at least three distinct corners and no crossing edges.');
        if (item.thickness !== undefined && (item.type !== 'fence' || !['thin', 'solid'].includes(item.thickness)))
            throw new Error('Choose thin or solid fence thickness.');
        if (item.wall_materials !== undefined && (item.type !== 'building'
            || !Array.isArray(item.wall_materials) || item.wall_materials.length !== item.points.length
            || item.wall_materials.some(m => m !== null && !MATERIALS.includes(m))))
            throw new Error('Choose one material or Building default for each wall.');
        const entries = Array.isArray(floor.environment) ? floor.environment : [];
        const index = entries.findIndex(p => p.id === item.id);
        if (entries.length > MAX_ENVIRONMENT_POLYGONS || index < 0 && entries.length >= MAX_ENVIRONMENT_POLYGONS)
            throw new Error('A floor supports at most 128 environment polygons. Delete a polygon before adding another.');
        const copy = {...item, name: item.name.trim(), points: item.points.map(p => ({x: p.x, y: p.y})),
            ...(item.type === 'fence' ? {thickness: item.thickness || 'solid'} : {}),
            ...(item.wall_materials ? {wall_materials: [...item.wall_materials]} : {})};
        if (!Array.isArray(floor.environment)) floor.environment = [];
        if (index < 0) floor.environment.push(copy); else floor.environment[index] = copy;
        return copy;
    }
    function validPolyline(points) {
        if (!Array.isArray(points) || points.length < 2 || points.length > 256
            || points.some(p => !p || !number(p.x) || !number(p.y))) return false;
        const seen = new Set();
        const cross = (a, b, c) => (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
        const on = (a, b, c) => Math.abs(cross(a, b, c)) < 1e-9
            && c.x >= Math.min(a.x, b.x) && c.x <= Math.max(a.x, b.x)
            && c.y >= Math.min(a.y, b.y) && c.y <= Math.max(a.y, b.y);
        const intersects = (a, b, c, d) => cross(a, b, c) * cross(a, b, d) < 0
            && cross(c, d, a) * cross(c, d, b) < 0
            || on(a, b, c) || on(a, b, d) || on(c, d, a) || on(c, d, b);
        for (let i = 0; i < points.length; i++) {
            const a = points[i], key = `${a.x},${a.y}`;
            if (seen.has(key)) return false;
            seen.add(key);
            if (i + 2 < points.length && (on(a, points[i + 1], points[i + 2])
                || on(points[i + 1], points[i + 2], a))) return false;
            for (let j = i + 2; i + 1 < points.length && j + 1 < points.length; j++) {
                if (intersects(a, points[i + 1], points[j], points[j + 1])) return false;
            }
        }
        return true;
    }
    const validEnvironmentPoints = item => item?.type === 'fence' ? validPolyline(item.points) : validPolygon(item?.points);
    function fenceStyle(item) {
        return {color: {wood: '#8d6e63', metal: '#607d8b', light: '#78909c', heavy: '#455a64', unknown: '#757575'}[item.material] || '#757575',
            width: item.thickness === 'thin' ? 2 : 5, dash: item.thickness === 'thin' ? [5, 3] : []};
    }
    function drawFence(ctx, item, zoom = 1) {
        if (!validPolyline(item?.points)) return false;
        const style = fenceStyle(item);
        ctx.save(); ctx.beginPath(); ctx.moveTo(item.points[0].x, item.points[0].y);
        item.points.slice(1).forEach(point => ctx.lineTo(point.x, point.y));
        ctx.strokeStyle = style.color; ctx.lineWidth = style.width / zoom;
        ctx.setLineDash(style.dash.map(length => length / zoom)); ctx.stroke(); ctx.restore();
        return true;
    }
    function upsertGroup(layout, group, knownTrackers = []) {
        const entries = Array.isArray(layout.tracker_groups) ? layout.tracker_groups : [];
        if (!group || !slug(group.id)
            || typeof group.name !== 'string' || !group.name.trim()
            || !Array.isArray(group.beacons) || !group.beacons.length
            || group.beacons.some(b => !slug(b) || !knownTrackers.includes(b)
                && (b === `bps_group_${group.id}` || entries.some(g => g && b === `bps_group_${g.id}`))))
            throw new Error('Choose a name, a stable lowercase ID of 1–64 letters, digits or underscores, and valid individual beacon slugs.');
        if (group.beacons.length > GROUP_LIMITS.beacons)
            throw new Error('A group supports at most 16 beacons.');
        const index = entries.findIndex(g => g.id === group.id);
        const enabled = group.enabled === undefined || group.enabled === true;
        const key = `bps_group_${group.id}`;
        if (knownTrackers.includes(key) && (index < 0 || enabled))
            throw new Error(`Group ID '${group.id}' conflicts with real tracker '${key}'. Choose another ID, or disable/delete the existing group.`);
        if (entries.length > GROUP_LIMITS.groups || index < 0 && entries.length >= GROUP_LIMITS.groups)
            throw new Error('A layout supports at most 32 groups. Delete a group before adding another.');
        const copy = {...group, name: group.name.trim(), enabled,
            beacons: [...new Set(group.beacons)]};
        if (!Array.isArray(layout.tracker_groups)) layout.tracker_groups = [];
        if (index < 0) layout.tracker_groups.push(copy); else layout.tracker_groups[index] = copy;
        return copy;
    }
    const display = (n, unit = '') => number(n) ? `${n.toFixed(1)}${unit}` : '—';
    function diagnosticsText(row) {
        const out = row && row.outdoor;
        if (!out) return '';
        const lines = [`Estimated uncertainty: ${display(out.estimated_uncertainty_m, ' m')} · ${out.confidence || 'unknown'} · ${out.receivers_used || 0} receivers`];
        if (out.detection_only) lines.push(`Detected only · direction unknown${typeof out.receiver === 'string' ? ' · reporting receiver ' + out.receiver : ''}`);
        if (Number.isFinite(displayLimit(out))) lines.push(`Display radius limit: ${display(out.display_radius_limit_m, ' m')} · nearest reporting receivers${out.estimated_uncertainty_m > displayLimit(out) ? ' · capped for display' : ''}${number(out.display_uncertainty_m) ? ' · displayed ' + display(out.display_uncertainty_m, ' m') : ''}`);
        if (out.environment_fallback) lines.push('Environmental geometry unavailable: neutral obstruction weights used; confidence reduced.');
        if (out.range_trust_enabled) lines.push('Nearby receiver priority: fresh reliable ranges within 5 m have strongest trust; trust declines beyond 10 m. Distant receivers still contribute direction.');
        if (number(out.obstruction_strength)) lines.push(`Mapped obstruction influence: ×${display(out.obstruction_strength)} (relative trust; distances unchanged).`);
        const radiusInputs = (details, prefix = '') => {
            if (!details || typeof details !== 'object') return;
            if (number(details.noise_floor_m)) lines.push(`${prefix}Radius inputs: weighted residual ${display(details.residual_m, ' m')} · noise floor ${display(details.noise_floor_m, ' m')} · geometry ×${display(details.geometry_factor)} · effective receivers ${display(details.effective_receivers)}`);
            if (number(details.unadjusted_residual_m)) lines.push(`${prefix}Robust weighting: residual before ${display(details.unadjusted_residual_m, ' m')} → after ${display(details.residual_m, ' m')} · ${details.robust_downweighted_receivers || 0} readings downweighted`);
            if (number(details.pre_publication_uncertainty_m) && number(details.publication_displacement_m)) lines.push(`${prefix}Publication adjustment: radius before ${display(details.pre_publication_uncertainty_m, ' m')} · raw-to-published shift ${display(details.publication_displacement_m, ' m')}`);
        };
        radiusInputs(out);
        if (row.group && number(out.combined_beacon_uncertainty_m)) lines.push(`Group radius inputs: ${out.beacons_used || 0} beacons combined · beacon uncertainty ${display(out.combined_beacon_uncertainty_m, ' m')} · position spread ${display(out.beacon_scatter_m, ' m')}`);
        if (row.motion_gate) lines.push(`Group motion: ${row.motion_gate.state || 'filtered'} · candidate shift ${display(row.motion_gate.displacement_m, ' m')} · allowed step ${display(row.motion_gate.allowed_step_m, ' m')}`);
        if (out.receiver_guard) lines.push(`Group receiver check: ${out.receiver_guard.state} · no fresh member range at ${(out.receiver_guard.receivers || []).map(r => r.receiver).join(', ')} · near-station limit ${display(out.receiver_guard.radius_m, ' m')}`);
        if (number(out.position_age_s)) lines.push(`Position age: ${display(out.position_age_s, ' s')}${isStale(out) ? ' · stale / last known' : ''}`);
        if (out.position_solver_mode) lines.push(`Position calculation: ${out.position_solver_mode === 'long_range_intervals' ? 'experimental distance ranges' : 'point ranges'} · ${out.range_interval_receivers || 0} range inputs`);
        if (row.group) lines.push(`${row.name || row.ent}: ${row.beacons_reporting || 0}/${row.total_beacons || 0} beacons · disagreement ${display(row.beacon_disagreement_m, ' m')} · ${row.fusion_confidence || 'unknown'}`);
        (row.beacon_positions || []).forEach(p => {
            if (!p) return;
            lines.push(`${p.ent}: estimated uncertainty ${display(p.estimated_uncertainty_m, ' m')}${p.floor ? ', ' + p.floor : ''}${number(p.age_s) ? ', age ' + display(p.age_s, ' s') : ''}${p.used === false ? ', excluded from fusion' : ''}`);
            radiusInputs(p.uncertainty_details, `${p.ent}: `);
            if (p.position_solver_mode) lines.push(`${p.ent}: position calculation ${p.position_solver_mode}`);
        });
        (out.receiver_diagnostics || []).forEach(r => {
            const band = r.range_interval;
            if (band?.status === 'available') lines.push(`${r.receiver}: raw signal-variation range ${display(band.lower_m, ' m')}–${display(band.upper_m, ' m')}${r.range_interval_used ? ' · used in position calculation' : ''} · excludes systematic bias`);
            if (number(r.range_trust_weight)) lines.push(`${r.receiver}: distance trust ×${display(r.range_trust_weight)} · corrected slant ${display(r.range_trust_distance_m, ' m')}${number(r.nearby_priority_reference_m) ? ' · reference receiver range ' + display(r.nearby_priority_reference_m, ' m') : ''}`);
            lines.push(`${r.receiver}: measured ${display(r.measured_distance_m, ' m')}, corrected ${display(r.corrected_distance_m, ' m')}, age ${display(r.reading_age_s, ' s')}, ${r.classification || 'unknown'}, building crossings ${r.building_crossings ?? '—'}${r.fence_crossings !== undefined ? ', fence crossings ' + r.fence_crossings : ''}${r.reflection_risk ? ', reflection / multipath risk' : ''}, environment weight ${display(r.environmental_weight)}, reliability ${display(r.reliability_weight)}, ${r.status || 'unknown'}`);
            if (number(r.mapped_environmental_weight)) lines.push(`${r.receiver}: mapped trust ${display(r.mapped_environmental_weight)} → effective ${display(r.environmental_weight)} · tree depth ${display(r.vegetation_depth_m, ' m')}`);
            (r.intersections || []).filter(item => item.type === 'fence').forEach(item => lines.push(
                `${r.receiver}: ${item.name || item.id || 'Fence'} · ${item.material || 'unknown'} · ${item.thickness || 'solid'} · crossings ${item.fence_crossings ?? '—'} · trust weight ${display(item.weight)}`));
        });
        return lines.join('\n');
    }
    root.BPSOutdoor = Object.freeze({TYPES, MATERIALS, POLICIES, MAX_UNCERTAINTY_THRESHOLD_M, MAX_ENVIRONMENT_POLYGONS,
        groups, environment, settings, positionSnapshot, distanceRadii, distanceIntervals, freshDistanceIntervals, elapsedNow, markerPlacement, markerStatus, uncertaintyRadius,
        drawUncertainty, stabilizeUncertainty, beaconNumber, drawMiniBeacon, isStale, fixTime, validPolygon, validPolyline,
        validEnvironmentPoints, fenceStyle, drawFence, upsertEnvironment, upsertGroup, diagnosticsText});
})(globalThis);
