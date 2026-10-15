/* Instantaneous actual-location marks, independent of stationary calibration labels. */
(function (root) {
    'use strict';
    function init(fetch, hooks, environment = {}) {
        const doc = environment.document || root.document;
        const el = name => doc.getElementById('liveTruth' + name);
        if (!el('Mark')) return null;
        let busy = false, picking = null, loaded = false, overview = {marks: []}, selection = null;
        let spot = null, run = null, timer = null, wakeLock = null, pickVersion = 0, focused = false, targetOptions = '';
        const now = environment.now || (() => Date.now());
        const clock = environment.monotonic || (() => root.performance?.now?.() ?? now());
        const schedule = environment.setTimeout || root.setTimeout.bind(root);
        const unschedule = environment.clearTimeout || root.clearTimeout.bind(root);
        const message = text => {el('Status').textContent = text;};
        const context = () => hooks.getContext?.() ?? '';
        function focusView(value) {
            focused = value;
            hooks.focusView?.(value);
            el('Focus').textContent = value ? 'Exit survey view' : 'Phone survey view';
            el('Focus').setAttribute?.('aria-pressed', String(value));
        }
        function stopSurvey(text) {
            if (timer !== null) unschedule(timer);
            timer = null; run = null; spot = null;
            if (wakeLock) {void wakeLock.release().catch(() => {}); wakeLock = null;}
            hooks.clearPoint();
            if (text) message(text);
        }
        function cancelPick() {
            pickVersion++;
            const cancel = picking; picking = null;
            cancel?.();
        }
        function validate(target, expectedContext) {
            hooks.validate();
            if (doc.hidden) throw new Error('Keep this page open while collecting. Choose the spot again after returning.');
            if (!target || hooks.getTarget() !== target) throw new Error('The selected tracker changed. Choose the survey spot again.');
            if (expectedContext !== context()) throw new Error('The saved map or tracking settings changed. Choose the spot again.');
        }
        function markOptions() {
            const accuracy = Number(el('Accuracy').value);
            if (!el('Accuracy').value.trim() || !Number.isFinite(accuracy) || accuracy < 0 || accuracy > 100)
                throw new Error('Location tolerance must be between 0 and 100 metres.');
            return {accuracy, label: el('Label').value};
        }
        function render() {
            const target = hooks.getTarget();
            el('Panel').hidden = !target;
            el('Mark').disabled = busy || !!run || !target;
            el('SurveyPick').disabled = busy || !!run || !target;
            el('SurveyStart').disabled = busy || !!run || !spot;
            el('SurveyStop').disabled = !spot && !run && !picking;
            el('SurveyPanel').hidden = !spot && !run && !picking;
            el('SurveyPick').textContent = picking ? 'Cancel map pick' : 'Choose survey spot';
            el('Copy').disabled = el('Clear').disabled = el('Download').disabled = busy || !!run;
            el('Accuracy').disabled = el('Label').disabled = busy || !!run;
            el('Target').disabled = busy;
            if (el('Shortcut')) el('Shortcut').disabled = busy || !target;
            const targets = hooks.getTargets?.() || (target ? [{key: target, name: target}] : []);
            const signature = JSON.stringify(targets);
            if (signature !== targetOptions) {
                el('Target').replaceChildren();
                for (const item of targets) {
                    const option = doc.createElement('option'); option.value = item.key; option.textContent = item.name;
                    el('Target').appendChild(option);
                }
                targetOptions = signature;
            }
            el('Target').value = target || '';
            el('Progress').value = run?.count || 0;
            el('SurveyStatus').textContent = run
                ? `${run.count}/5 snapshots saved · ${run.count ? 'Next snapshot' : 'Settling'}: ${Math.max(0, Math.ceil((run.nextAt - clock()) / 1000))} s`
                : picking ? 'Tap the actual location on the map, or cancel below.'
                : 'Spot selected. Keep the beacons still, then start capture.';
            el('History').replaceChildren();
            const marks = overview.marks.filter(m => m.target === target);
            for (const mark of marks.slice(-5).reverse()) {
                const row = doc.createElement('div');
                const error = mark.position_errors?.find(p => p.target === target);
                row.textContent = new Date(mark.time * 1000).toLocaleString() + ' · '
                    + (Number.isFinite(error?.error_m) && error.scored ? error.error_m.toFixed(1) + ' m error' : error?.status || 'Position unavailable')
                    + (mark.label ? ' · ' + mark.label : '');
                row.title = `${mark.capture?.captured_at_utc || new Date(mark.time * 1000).toISOString()} · ${mark.id}`;
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
            if (target !== selection || (spot && spot.context !== context())) {
                cancelPick();
                if (spot || run) stopSurvey('Survey stopped because the tracker or saved map changed. Choose the new spot.');
            }
            if (!target && focused) focusView(false);
            selection = target;
            render();
            if (target && !loaded && !busy) void load();
        }
        async function savePoint(point, target, options, expectedContext) {
            validate(target, expectedContext);
            busy = true; render(); message('Saving actual location and BLE snapshot…');
            const id = root.crypto?.randomUUID?.().replaceAll('-', '')
                || 'mark_' + now().toString(36) + '_' + Math.random().toString(36).slice(2);
            try {
                const result = await request({method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({action: 'mark', id, marker: {target, floor: point.floor,
                        x_m: point.x_m, y_m: point.y_m, accuracy_m: options.accuracy, label: options.label}})});
                if (result.saved?.id !== id || result.saved?.target !== target) throw new Error('The server did not confirm this snapshot.');
                overview = result;
                const error = overview.saved.position_errors?.find(p => p.target === target);
                message(`Saved ${overview.marks.filter(m => m.target === target).length} marks for this tracker.`
                    + (Number.isFinite(error?.error_m) && error.scored ? ` Current position error: ${error.error_m.toFixed(1)} m.` : '')
                    + (overview.evicted ? ` ${overview.evicted} older marks expired or were evicted. Download the ZIP after each spot.` : '')
                    + ' You can mark another location at any time.');
            } finally {busy = false; render();}
        }
        function choosePoint(survey) {
            if (busy || run) return;
            if (picking) {cancelPick(); return;}
            const target = hooks.getTarget();
            try {
                const expectedContext = context();
                validate(target, expectedContext);
                if (!target) throw new Error('Select a group or tracker in live tracking first.');
                const options = markOptions(), version = ++pickVersion;
                stopSurvey();
                picking = hooks.pick(async point => {
                    if (version !== pickVersion) return;
                    picking = null;
                    try {
                        validate(target, expectedContext);
                        if (survey) {
                            spot = {point: {...point}, target, context: expectedContext};
                            message('Survey spot selected. Keep the beacons still, then press Collect 5 snapshots.');
                            render();
                        } else await savePoint(point, target, options, expectedContext);
                    } catch (error) {hooks.clearPoint(); message('Location was not confirmed saved: ' + error.message);}
                }, () => {picking = null; message('Location marking cancelled.'); render();});
                message('Tap where the selected dog actually is. Stop / cancel spot or Esc cancels.');
                hooks.showMap?.(); render();
            } catch (error) {message(error.message);}
        }
        el('Mark').addEventListener('click', () => choosePoint(false));
        el('SurveyPick').addEventListener('click', () => choosePoint(true));
        async function tick(active) {
            timer = null;
            if (!active || run !== active) return;
            try {
                validate(active.target, active.context);
                if (clock() >= active.nextAt) {
                    await savePoint(active.point, active.target, active.options, active.context);
                    if (run !== active) return;
                    active.count++;
                    if (active.count === 5) {
                        stopSurvey(); render();
                        message('5 snapshots saved. Download the diagnostic ZIP before moving to the next spot.');
                        return;
                    }
                    // Space requests from completion, never catch up missed timers.
                    active.nextAt = clock() + 10000;
                }
                render(); timer = schedule(() => void tick(active), 1000);
            } catch (error) {
                if (run !== active) return;
                const count = active.count;
                stopSurvey(); render(); message(`Survey stopped after ${count} confirmed snapshots: ${error.message}`);
            }
        }
        el('SurveyStart').addEventListener('click', () => {
            if (busy || run || !spot) return;
            try {
                validate(spot.target, spot.context);
                run = {...spot, options: markOptions(), count: 0, nextAt: clock() + 30000};
                message('Survey started. Keep the dogs still and this page open; stop before moving.');
                const active = run;
                root.navigator?.wakeLock?.request('screen').then(lock => {
                    if (run === active) wakeLock = lock;
                    else void lock.release().catch(() => {});
                }).catch(() => {});
                render(); timer = schedule(() => void tick(active), 1000);
            } catch (error) {stopSurvey(); render(); message(error.message);}
        });
        el('SurveyStop').addEventListener('click', () => {
            cancelPick(); stopSurvey('Survey stopped. Any snapshot already being saved may finish. Download before moving.'); render();
        });
        function toggleFocus() {
            if (!focused) {
                try {validate(hooks.getTarget(), context());} catch (error) {message(error.message); return;}
            }
            if (focused) {cancelPick(); stopSurvey('Survey stopped. Your saved marks are available to download.');}
            focusView(!focused); render();
        }
        el('Focus').addEventListener('click', toggleFocus);
        el('Shortcut')?.addEventListener('click', toggleFocus);
        el('Target').addEventListener('change', () => {
            if (busy) return;
            hooks.selectTarget?.(el('Target').value); sync();
        });
        doc.addEventListener?.('visibilitychange', () => {
            if (doc.hidden) {
                cancelPick();
                if (run || spot) {stopSurvey('Survey stopped because the page was hidden. Choose the spot again when ready.'); render();}
            }
        });
        el('Download').addEventListener('click', async () => {
            if (busy || run) return;
            cancelPick(); busy = true; render(); message('Preparing full diagnostic ZIP…');
            try {
                const result = await hooks.download();
                if (!result?.ok) throw new Error(result?.error || 'Diagnostic download is unavailable.');
                message('Diagnostic ZIP download started. Keep this file for analysis before choosing the next spot.');
            } catch (error) {message('Could not download diagnostics: ' + error.message);}
            finally {busy = false; render();}
        });
        el('Copy').addEventListener('click', async () => {
            if (busy || run) return;
            cancelPick();
            busy = true; render(); message('Preparing marked tracking history…');
            try {
                const data = await request(undefined, '?download=1');
                const text = 'BPS actual-location history\n' + JSON.stringify(data, null, 2);
                el('Report').value = text;
                try {await root.navigator.clipboard.writeText(text); message('Marked history copied.');}
                catch (_) {const options = el('Report').closest('details'); if (options) options.open = true;
                    el('Report').hidden = false; el('Report').focus(); el('Report').select();
                    message('History ready. Copy the selected text.');}
            } catch (error) {message(error.message);}
            finally {busy = false; render();}
        });
        el('Clear').addEventListener('click', async () => {
            if (busy || run || !await hooks.confirmClear()) return;
            if (busy || run) return;
            cancelPick(); stopSurvey();
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
