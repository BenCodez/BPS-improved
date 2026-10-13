/* Instantaneous actual-location marks, independent of stationary calibration labels. */
(function (root) {
    'use strict';
    function init(fetch, hooks, environment = {}) {
        const doc = environment.document || root.document;
        const el = name => doc.getElementById('liveTruth' + name);
        if (!el('Mark')) return null;
        let busy = false, picking = null, loaded = false, overview = {marks: []}, selection = null;
        const now = environment.now || (() => Date.now());
        const message = text => {el('Status').textContent = text;};
        function render() {
            const target = hooks.getTarget();
            el('Panel').hidden = !target;
            el('Mark').disabled = busy || !target;
            el('Copy').disabled = busy;
            el('Clear').disabled = busy || !overview.marks.length;
            el('History').replaceChildren();
            const marks = overview.marks.filter(m => m.target === target);
            for (const mark of marks.slice(-5).reverse()) {
                const row = doc.createElement('div');
                const error = mark.position_errors?.find(p => p.target === target);
                row.textContent = new Date(mark.time * 1000).toLocaleTimeString() + ' · '
                    + (Number.isFinite(error?.error_m) && error.scored ? error.error_m.toFixed(1) + ' m error' : error?.status || 'Position unavailable')
                    + (mark.label ? ' · ' + mark.label : '');
                el('History').appendChild(row);
            }
        }
        async function request(options, suffix = '') {
            const response = await fetch('/api/bps/ground_truth' + suffix, options);
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || `Location history request failed (${response.status})`);
            if (!Array.isArray(data.marks)) throw new Error('Invalid actual-location history response.');
            return data;
        }
        async function load() {
            if (busy) return;
            busy = true; render();
            try {overview = await request(); loaded = true;}
            catch (error) {message(error.message);}
            finally {busy = false; render();}
        }
        function sync() {
            const target = hooks.getTarget();
            if (target !== selection && picking) {picking(); picking = null;}
            selection = target;
            render();
            if (target && !loaded && !busy) void load();
        }
        el('Mark').addEventListener('click', () => {
            if (busy) return;
            if (picking) {picking(); picking = null; return;}
            const target = hooks.getTarget();
            try {
                hooks.validate();
                if (!target) throw new Error('Select a group or tracker in live tracking first.');
                const accuracy = Number(el('Accuracy').value);
                if (!el('Accuracy').value.trim() || !Number.isFinite(accuracy) || accuracy < 0 || accuracy > 100)
                    throw new Error('Location tolerance must be between 0 and 100 metres.');
                const label = el('Label').value;
                picking = hooks.pick(async point => {
                    picking = null;
                    if (hooks.getTarget() !== target) {hooks.clearPoint(); return;}
                    try {hooks.validate();} catch (error) {hooks.clearPoint(); message(error.message); return;}
                    busy = true; render(); message('Saving actual location and BLE snapshot…');
                    const id = root.crypto?.randomUUID?.().replaceAll('-', '')
                        || 'mark_' + now().toString(36) + '_' + Math.random().toString(36).slice(2);
                    try {
                        overview = await request({method: 'POST', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({action: 'mark', id, marker: {target, floor: point.floor,
                                x_m: point.x_m, y_m: point.y_m, accuracy_m: accuracy, label}})});
                        const error = overview.saved?.position_errors?.find(p => p.target === target);
                        message(`Saved ${overview.marks.filter(m => m.target === target).length} marks for this tracker.`
                            + (Number.isFinite(error?.error_m) && error.scored ? ` Current position error: ${error.error_m.toFixed(1)} m.` : '')
                            + (overview.evicted ? ` ${overview.evicted} older marks expired or were evicted.` : '')
                            + ' You can mark another location at any time.');
                    } catch (error) {hooks.clearPoint(); message('Location was not confirmed saved: ' + error.message);}
                    finally {busy = false; render();}
                }, () => {picking = null; message('Location marking cancelled.');});
                message('Click or tap where the selected dog actually is. Esc cancels.');
            } catch (error) {message(error.message);}
        });
        el('Copy').addEventListener('click', async () => {
            if (busy) return;
            busy = true; render(); message('Preparing marked tracking history…');
            try {
                const data = await request(undefined, '?download=1');
                const text = 'BPS actual-location history\n' + JSON.stringify(data, null, 2);
                el('Report').value = text;
                try {await root.navigator.clipboard.writeText(text); message('Marked history copied.');}
                catch (_) {el('Report').hidden = false; el('Report').focus(); el('Report').select();
                    message('History ready. Copy the selected text.');}
            } catch (error) {message(error.message);}
            finally {busy = false; render();}
        });
        el('Clear').addEventListener('click', async () => {
            if (busy || !await hooks.confirmClear()) return;
            busy = true; render();
            try {overview = await request({method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action: 'clear'})});
                hooks.clearPoint(); message('Actual-location history cleared.');}
            catch (error) {message(error.message);}
            finally {busy = false; render();}
        });
        sync();
        return {sync};
    }
    root.BPSGroundTruth = {init};
})(globalThis);
