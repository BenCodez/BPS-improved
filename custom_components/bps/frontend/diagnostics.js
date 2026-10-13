/* Authenticated, opt-in diagnostic capture controls. No polling until requested. */
(function (root) {
    'use strict';
    function init(apiFetch, panelSnapshot, locationTools = {}) {
        const el = name => document.getElementById('diagnostic' + name);
        if (!el('Start')) return;
        let reportBusy = false;
        function selectReport() {
            el('ReportText').focus();
            el('ReportText').select();
        }
        el('SelectReport').addEventListener('click', selectReport);
        async function captureReport(knownPositions = null) {
            if (reportBusy) return;
            reportBusy = true; el('CopyAll').disabled = true;
            if (el('SnapshotCapture')) el('SnapshotCapture').disabled = true;
            el('ReportStatus').textContent = 'Collecting full diagnostics…';
            try {
                const panel = panelSnapshot ? JSON.stringify(panelSnapshot(), null, 2) : null;
                const panelContext = panel ? {guide: 'Editor state may contain unsaved changes; the backend uses context.layout.', ...JSON.parse(panel)} : null;
                const panelBytes = panelContext ? new TextEncoder().encode(JSON.stringify({panel_context: panelContext}, null, 2)).length + 256 : 0;
                if (panelBytes > 4 * 1024 * 1024) throw new Error('Combined diagnostic report exceeds 4 MiB.');
                const response = knownPositions
                    ? await apiFetch('/api/bps/diagnostics', {method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({action: 'snapshot', known_positions: knownPositions, panel_bytes: panelBytes})})
                    : await apiFetch('/api/bps/diagnostics?report=1' + (panelContext ? '&panel_bytes=' + panelBytes : ''));
                const body = await response.json();
                if (!response.ok) throw new Error(body.error || `Diagnostic request failed (${response.status})`);
                if (typeof body.text !== 'string' || !body.text.trim()) throw new Error('The diagnostic report was empty. Try again.');
                if (knownPositions && (body.read_only !== true || !Array.isArray(body.comparison?.markers))) {
                    throw new Error('Invalid known-position snapshot response.');
                }
                let text = body.text;
                if (panel) {
                    const start = text.indexOf('{');
                    const report = JSON.parse(text.slice(start));
                    report.panel_context = panelContext;
                    text = text.slice(0, start) + JSON.stringify(report, null, 2);
                }
                if (new TextEncoder().encode(text).length > 4 * 1024 * 1024) throw new Error('Combined diagnostic report exceeds 4 MiB.');
                el('ReportText').value = text;
                el('ReportPanel').hidden = false;
                if (el('SnapshotComparison')) renderComparison(body.comparison);
                try {
                    await root.navigator.clipboard.writeText(text);
                    el('ReportStatus').textContent = 'Full diagnostics copied. Paste them into your support message.';
                } catch (_) {
                    selectReport();
                    el('ReportStatus').textContent = 'Report ready. Press Ctrl+C / Cmd+C, or touch and hold the selected text to copy.';
                }
            } catch (error) {
                el('ReportStatus').textContent = `Could not refresh diagnostics: ${error.message}${el('ReportText').value ? ' The previous report below is unchanged.' : ''}`;
            } finally {
                reportBusy = false; el('CopyAll').disabled = false;
                if (el('SnapshotCapture')) el('SnapshotCapture').disabled = false;
            }
        }
        el('CopyAll').addEventListener('click', () => captureReport());
        const make = (tag, text = '') => {const node = document.createElement(tag); node.textContent = text; return node;};
        const metric = value => Number.isFinite(value) ? value.toFixed(2) : '—';
        function renderComparison(comparison) {
            const host = el('SnapshotComparison'); host.replaceChildren();
            if (!comparison) return;
            host.appendChild(make('p', 'Frozen snapshot: ' + new Date(comparison.time * 1000).toLocaleString()
                + '. Range disagreement does not by itself prove a bad receiver. Readings and position fixes have separate timestamps.'));
            for (const marker of comparison.markers) {
                host.appendChild(make('h4', `${marker.target}: actual (${metric(marker.x_m)}, ${metric(marker.y_m)}) m on ${marker.floor}`));
                host.appendChild(make('p', `${marker.summary.disagreeing_readings} current ranges differ by more than your ${metric(marker.accuracy_m)} m tolerance. `
                    + `${marker.summary.unscored_readings} readings are unscored. All BLE data remains in the copied report.`));
                for (const position of marker.position_errors) {
                    host.appendChild(make('p', `${position.target}: ${position.status}; position error ${metric(position.error_m)} m; age ${metric(position.age_s)} s.`));
                }
                const tableFor = rows => {
                    const table = make('table'); table.className = 'bps-calib-table';
                    const headings = make('tr');
                    for (const title of ['Beacon', 'Receiver', 'Expected m', 'Reported m', 'Error m', 'Age s', 'Mapped path / status']) headings.appendChild(make('th', title));
                    table.appendChild(headings);
                    for (const row of rows) {
                        const tr = make('tr');
                        const paths = (row.environment?.intersections || []).map(p => `${p.type || p.kind || ''} ${p.material || ''}`).join(', ');
                        const environment = row.environment;
                        const pathStatus = !environment || environment.classification === 'unknown'
                            || environment.environment_error || environment.invalid_path ? 'Path unavailable'
                            : environment.classification === 'not_evaluated' ? 'Path not assessed'
                            : paths || 'No mapped crossing';
                        const prior = row.solver_evidence?.reading;
                        const note = (row.reasons?.join(', ').replace(/_/g, ' ') || row.status)
                            + (prior ? `; prior solve ${prior.used === false ? 'excluded' : prior.used === true ? 'used' : prior.status || 'unknown'}` : '');
                        for (const value of [row.tracker, row.receiver, metric(row.expected_distance_m), metric(row.corrected_distance_m),
                            metric(row.signed_error_m), metric(row.age_s), `${pathStatus}; trust ${metric(environment?.environmental_weight)}; ${note}`]) tr.appendChild(make('td', value));
                        table.appendChild(tr);
                    }
                    return table;
                };
                host.appendChild(tableFor(marker.ranked_readings.slice(0, 12)));
                const remaining = [...marker.ranked_readings.slice(12), ...marker.unscored_readings];
                if (remaining.length) {
                    const details = make('details'); details.appendChild(make('summary', `Show ${remaining.length} other readings`));
                    details.appendChild(tableFor(remaining)); host.appendChild(details);
                }
            }
        }
        let status = {active: false, available: false}, timer = null, busy = false, cancelPick = null, cancelRecordingPick = null;
        const idleWaiters = [];
        function invalidateSnapshotLocation(clearCoordinates) {
            if (cancelPick) {cancelPick(); cancelPick = null;}
            if (clearCoordinates) {
                el('SnapshotX').value = ''; el('SnapshotY').value = '';
            }
            locationTools.clearPoint?.();
            el('ReportStatus').textContent = 'Location changed. Pick or enter the location again before capturing current BLE data.';
        }
        function controls() {
            ['Start', 'Stop', 'Download', 'Clear', 'Mark', 'Release', 'Note', 'Refresh'].forEach(name => {
                const enabled = name === 'Start' ? !status.active && !!el('Target').value
                    : ['Stop', 'Mark', 'Release', 'Note'].includes(name) ? status.active
                    : ['Download', 'Clear'].includes(name) ? status.available : true;
                el(name).disabled = busy || !enabled;
            });
            el('Target').disabled = busy || status.active;
            el('Duration').disabled = busy || status.active;
            if (el('Pick')) el('Pick').disabled = busy || !status.active;
        }
        function options(node, items) {
            const selected = node.value;
            node.replaceChildren();
            items.forEach(item => {
                const option = document.createElement('option');
                option.value = item.key; option.textContent = item.name;
                node.appendChild(option);
            });
            if (items.some(item => item.key === selected)) node.value = selected;
        }
        function render(next) {
            const previousSnapshotTarget = el('SnapshotTarget')?.value;
            const previousSnapshotFloor = el('SnapshotFloor')?.value;
            status = next;
            if (!next.active && cancelRecordingPick) {cancelRecordingPick(); cancelRecordingPick = null;}
            if (Array.isArray(next.trackers)) {
                const items = next.trackers.slice();
                (next.targets || []).forEach(key => {if (!items.some(item => item.key === key)) items.push({key, name: key});});
                options(el('Target'), items);
                if (next.active && next.targets.length) el('Target').value = next.targets[0];
            }
            if (Array.isArray(next.floors)) options(el('Floor'), next.floors.map(key => ({key, name: key})));
            if (el('SnapshotTarget') && Array.isArray(next.trackers)) options(el('SnapshotTarget'), next.trackers);
            if (el('SnapshotFloor') && Array.isArray(next.floors)) options(el('SnapshotFloor'), next.floors.map(key => ({key, name: key})));
            if (el('SnapshotCapture') && (previousSnapshotTarget !== el('SnapshotTarget').value
                    || previousSnapshotFloor !== el('SnapshotFloor').value)) invalidateSnapshotLocation(true);
            el('Status').textContent = `${next.reason}. ${next.frames || 0} frames, ${((next.bytes || 0) / 1048576).toFixed(1)} MiB, ${next.annotations || 0} markers${next.active ? `, ${next.seconds_left}s remaining` : ''}.`;
            if (next.active && timer === null) timer = setInterval(() => {if (!busy) run(refresh);}, 5000);
            if (!next.active && timer !== null) {clearInterval(timer); timer = null;}
            controls();
        }
        async function json(options) {
            const response = await apiFetch('/api/bps/diagnostics', options);
            const body = await response.json();
            if (!response.ok) throw new Error(body.error || `Diagnostic request failed (${response.status})`);
            return body;
        }
        async function refresh() {render(await json());}
        async function post(body) {
            render(await json({method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}));
        }
        async function run(action) {
            if (busy) return;
            busy = true; controls();
            try {await action();} catch (error) {el('Status').textContent = error.message;}
            finally {busy = false; controls(); idleWaiters.splice(0).forEach(resolve => resolve());}
        }
        el('Refresh').addEventListener('click', () => run(refresh));
        if (el('SnapshotCapture')) {
            for (const name of ['SnapshotTarget', 'SnapshotFloor', 'SnapshotX', 'SnapshotY']) {
                el(name).addEventListener('change', () => {
                    invalidateSnapshotLocation(name === 'SnapshotTarget' || name === 'SnapshotFloor');
                });
            }
            const marker = () => {
                if (!el('SnapshotTarget').value || !el('SnapshotFloor').value) throw new Error('Load dogs / floors and choose a dog first.');
                if (!el('SnapshotX').value.trim() || !el('SnapshotY').value.trim()) throw new Error('Pick the actual location or enter X and Y first.');
                const x = Number(el('SnapshotX').value), y = Number(el('SnapshotY').value), accuracy = Number(el('SnapshotAccuracy').value);
                if (!Number.isFinite(x) || !Number.isFinite(y) || Math.abs(x) > 100000 || Math.abs(y) > 100000
                    || !el('SnapshotAccuracy').value.trim() || !Number.isFinite(accuracy) || accuracy < 0 || accuracy > 100) {
                    throw new Error('Enter finite coordinates and a location tolerance from 0 to 100 metres.');
                }
                return {target: el('SnapshotTarget').value, floor: el('SnapshotFloor').value,
                    x_m: x, y_m: y, accuracy_m: accuracy, label: el('SnapshotLabel').value};
            };
            el('SnapshotLoad').addEventListener('click', () => run(refresh));
            el('SnapshotCapture').addEventListener('click', async () => {
                if (reportBusy) return;
                try {locationTools.validate?.(); await captureReport([marker()]);}
                catch (error) {el('ReportStatus').textContent = error.message;}
            });
            el('SnapshotPick').addEventListener('click', () => {
                if (reportBusy) return;
                if (cancelPick) {cancelPick(); cancelPick = null; return;}
                try {
                    if (!locationTools.pick) throw new Error('Map picking is unavailable; enter the measured coordinates.');
                    cancelPick = locationTools.pick(point => {
                        cancelPick = null;
                        el('SnapshotFloor').value = point.floor;
                        el('SnapshotX').value = point.x_m.toFixed(3); el('SnapshotY').value = point.y_m.toFixed(3);
                        el('ReportStatus').textContent = 'Actual location selected. Keep the dog there and capture the snapshot.';
                    }, () => {cancelPick = null; el('ReportStatus').textContent = 'Map location selection cancelled. Pick again or enter coordinates.';});
                    el('ReportStatus').textContent = 'Click the dog’s actual location on the map. Press Esc to cancel.';
                } catch (error) {el('ReportStatus').textContent = error.message;}
            });
        }
        el('Start').addEventListener('click', () => run(() => post({action: 'start', targets: [el('Target').value], duration_s: Number(el('Duration').value)})));
        el('Stop').addEventListener('click', () => run(() => post({action: 'stop'})));
        el('Clear').addEventListener('click', () => run(() => post({action: 'clear'})));
        el('Mark').addEventListener('click', () => run(() => {
            if (!el('X').value.trim() || !el('Y').value.trim()) throw new Error('Enter the measured X and Y coordinates first.');
            return post({action: 'annotate', kind: 'known_position', target: el('Target').value,
                floor: el('Floor').value, x_m: Number(el('X').value), y_m: Number(el('Y').value), label: el('Label').value});
        }));
        if (el('Pick')) el('Pick').addEventListener('click', () => {
            if (busy || !status.active) return;
            if (cancelRecordingPick) {cancelRecordingPick(); cancelRecordingPick = null; return;}
            const target = el('Target').value;
            try {
                if (!locationTools.pick) throw new Error('Map picking is unavailable. Open a saved floor map first.');
                cancelRecordingPick = locationTools.pick(async point => {
                    cancelRecordingPick = null;
                    // A map click remains available during the periodic status
                    // refresh. Preserve that annotation until the request ends.
                    if (busy) await new Promise(resolve => idleWaiters.push(resolve));
                    if (!status.active || el('Target').value !== target) {locationTools.clearPoint?.(); return;}
                    el('Floor').value = point.floor;
                    el('X').value = point.x_m.toFixed(3); el('Y').value = point.y_m.toFixed(3);
                    return run(() => post({action: 'annotate', kind: 'known_position', target,
                        floor: point.floor, x_m: point.x_m, y_m: point.y_m, label: el('Label').value}));
                }, () => {cancelRecordingPick = null; el('Status').textContent = 'Map location selection cancelled.';});
                el('Status').textContent = 'Click or tap the dog’s actual location to record it. Press Esc to cancel.';
            } catch (error) {el('Status').textContent = error.message;}
        });
        el('Release').addEventListener('click', () => run(async () => {
            if (cancelRecordingPick) {cancelRecordingPick(); cancelRecordingPick = null;}
            await post({action: 'annotate', kind: 'clear_position', target: el('Target').value});
            locationTools.clearPoint?.(); el('X').value = ''; el('Y').value = '';
        }));
        el('Note').addEventListener('click', () => run(() => post({action: 'annotate', kind: 'note', label: el('Label').value})));
        el('Download').addEventListener('click', () => run(async () => {
            const response = await apiFetch('/api/bps/diagnostics?download=1');
            if (!response.ok) throw new Error(`Download failed (${response.status})`);
            const url = URL.createObjectURL(await response.blob());
            const link = document.createElement('a');
            link.href = url; link.download = 'bps-diagnostics.json';
            document.body.appendChild(link); link.click(); link.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        }));
        controls();
    }
    root.BPSDiagnostics = {init};
})(globalThis);
