/* Shared optional map/editor helpers. Distances are metres; map points are floor pixels. */
(function (root) {
    'use strict';
    const TYPES = ['building', 'dense_trees', 'light_vegetation', 'custom'];
    const MATERIALS = ['unknown', 'light', 'heavy', 'metal'];
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
        return {enabled: s.enabled === true, show_uncertainty: s.show_uncertainty !== false,
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
    function uncertaintyRadius(outdoor, scale, config) {
        if (!config || config.enabled !== true || config.show_uncertainty === false) return null;
        const metres = outdoor && outdoor.estimated_uncertainty_m;
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
        const observed = row && row.outdoor && row.outdoor.observed;
        const timestamp = number(observed) && observed >= 0 ? observed : row && row.updated;
        if (number(timestamp) && timestamp >= 0 && number(timestamp * 1000)) return Math.min(now, timestamp * 1000);
        const age = row && row.outdoor && row.outdoor.position_age_s;
        return number(age) && age >= 0 ? now - age * 1000 : now;
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
            || typeof item.name !== 'string' || !item.name.trim()
            || !TYPES.includes(item.type) || !MATERIALS.includes(item.material)
            || !validPolygon(item.points)) throw new Error('Use a named polygon with at least three distinct corners and no crossing edges.');
        const entries = Array.isArray(floor.environment) ? floor.environment : [];
        const index = entries.findIndex(p => p.id === item.id);
        if (entries.length > MAX_ENVIRONMENT_POLYGONS || index < 0 && entries.length >= MAX_ENVIRONMENT_POLYGONS)
            throw new Error('A floor supports at most 128 environment polygons. Delete a polygon before adding another.');
        const copy = {...item, name: item.name.trim(), points: item.points.map(p => ({x: p.x, y: p.y}))};
        if (!Array.isArray(floor.environment)) floor.environment = [];
        if (index < 0) floor.environment.push(copy); else floor.environment[index] = copy;
        return copy;
    }
    function upsertGroup(layout, group, knownTrackers = []) {
        if (!group || !slug(group.id)
            || typeof group.name !== 'string' || !group.name.trim()
            || !Array.isArray(group.beacons) || !group.beacons.length
            || group.beacons.some(b => !slug(b) || b.startsWith('bps_group_')))
            throw new Error('Choose a name, a stable lowercase ID of 1–64 letters, digits or underscores, and valid individual beacon slugs.');
        if (group.beacons.length > GROUP_LIMITS.beacons)
            throw new Error('A group supports at most 16 beacons.');
        const entries = Array.isArray(layout.tracker_groups) ? layout.tracker_groups : [];
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
        if (number(out.position_age_s)) lines.push(`Position age: ${display(out.position_age_s, ' s')}${isStale(out) ? ' · stale / last known' : ''}`);
        if (row.group) lines.push(`${row.name || row.ent}: ${row.beacons_reporting || 0}/${row.total_beacons || 0} beacons · disagreement ${display(row.beacon_disagreement_m, ' m')} · ${row.fusion_confidence || 'unknown'}`);
        (row.beacon_positions || []).forEach(p => lines.push(`${p.ent}: estimated uncertainty ${display(p.estimated_uncertainty_m, ' m')}${p.floor ? ', ' + p.floor : ''}${number(p.age_s) ? ', age ' + display(p.age_s, ' s') : ''}${p.used === false ? ', excluded from fusion' : ''}`));
        (out.receiver_diagnostics || []).forEach(r => lines.push(`${r.receiver}: measured ${display(r.measured_distance_m, ' m')}, corrected ${display(r.corrected_distance_m, ' m')}, age ${display(r.reading_age_s, ' s')}, ${r.classification || 'unknown'}, building crossings ${r.building_crossings ?? '—'}${r.reflection_risk ? ', reflection / multipath risk' : ''}, environment weight ${display(r.environmental_weight)}, reliability ${display(r.reliability_weight)}, ${r.status || 'unknown'}`));
        return lines.join('\n');
    }
    root.BPSOutdoor = Object.freeze({TYPES, MATERIALS, POLICIES, MAX_UNCERTAINTY_THRESHOLD_M, MAX_ENVIRONMENT_POLYGONS,
        groups, environment, settings, positionSnapshot, uncertaintyRadius,
        drawUncertainty, beaconNumber, drawMiniBeacon, isStale, fixTime, validPolygon, upsertEnvironment, upsertGroup, diagnosticsText});
})(globalThis);
