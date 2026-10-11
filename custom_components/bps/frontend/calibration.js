/* Read-only Bermuda scanner correction preview. All application remains manual. */
(() => {
    function init(apiFetch) {
        const host = document.getElementById('calibrationdiv');
        const floor = document.getElementById('mapname');
        if (!host || !floor || document.getElementById('bermudaPreview')) return;
        const make = (tag, text, id) => {
            const element = document.createElement(tag);
            if (text) element.textContent = text;
            if (id) element.id = id;
            return element;
        };
        const panel = make('div', '', 'bermudaPreviewPanel');
        const button = make('button', 'Preview Bermuda RSSI corrections', 'bermudaPreview');
        button.type = 'button'; button.className = 'bps-btn bps-btn-outline';
        const status = make('p', '', 'bermudaPreviewStatus'); status.setAttribute('role', 'status');
        const results = make('div', '', 'bermudaPreviewRows');
        results.className = 'bps-calib-matrix';
        const instructions = make('p');
        const text = make('textarea', '', 'bermudaPreviewText'); text.readOnly = true; text.hidden = true;
        text.className = 'bps-input'; text.rows = 12;
        const copy = make('button', 'Copy read-only preview JSON', 'bermudaPreviewCopy');
        copy.type = 'button'; copy.className = 'bps-btn bps-btn-outline'; copy.disabled = true;
        for (const e of [button, status, instructions, results, copy, text]) panel.appendChild(e);
        host.appendChild(panel);
        let busy = false, preview = null, generation = 0;
        const invalidate = () => {
            generation++; preview = null; copy.disabled = true; results.replaceChildren();
            text.hidden = true; instructions.textContent = '';
            status.textContent = 'Settings or floor changed. Refresh the read-only preview before using it.';
        };
        // Parent editor may change floors via selector, inputs, or map buttons.
        for (const event of ['input', 'change', 'click']) {
            document.addEventListener(event, e => {
                if (!panel.contains(e.target)) invalidate();
            });
        }
        async function copyText(value) {
            const token = generation;
            text.value = value; text.hidden = false;
            try {
                await navigator.clipboard.writeText(value);
                if (token === generation) status.textContent = 'Read-only JSON copied. No configuration changed.';
            } catch (_) {
                if (token !== generation) return;
                text.focus(); text.select();
                status.textContent = 'Select the JSON and press Ctrl+C. No configuration changed.';
            }
        }
        copy.addEventListener('click', async () => {
            if (preview) await copyText(JSON.stringify(preview, null, 2));
        });
        const show = value => typeof value === 'number' ? value.toFixed(4).replace(/\.?0+$/, '') : 'unknown';
        function render(data) {
            results.replaceChildren();
            instructions.textContent = data.instructions;
            const table = make('table'); table.className = 'bps-calib-table';
            const head = make('tr');
            for (const title of ['Receiver / scanner', 'Attenuation', 'Current offset (dB)', 'Current BPS factor',
                'Transfer current offset (dB)', 'Proposed factor', 'Proposed offset (dB)', 'Safety / copy']) head.appendChild(make('th', title));
            table.appendChild(head);
            for (const row of data.rows) {
                const tr = make('tr');
                for (const value of [row.receiver + '\n' + (row.scanner_name || '') + '\n' + (row.scanner_mac || 'Unknown scanner'),
                    show(row.attenuation), show(row.current_offset_db), show(row.current_bps_factor),
                    show(row.transfer_current_offset_db), show(row.proposed_bps_factor), show(row.proposed_offset_db)]) tr.appendChild(make('td', value));
                const actions = make('td');
                actions.appendChild(make('p', row.reasons.length ? row.reasons.join('; ').replace(/_/g, ' ') : 'Candidate eligible for manual review'));
                for (const mode of ['transfer_current', 'candidate']) {
                    const transfer = mode === 'transfer_current';
                    const action = make('button', transfer ? 'Copy current-factor transfer' : 'Copy proposed correction');
                    action.type = 'button'; action.disabled = !row[mode + '_eligible'];
                    action.title = transfer ? 'Preserves current raw distance scale; reset this BPS factor to 1 after manual transfer.'
                        : 'New calibration proposal: requires matching source and measured tracking improvement.';
                    action.addEventListener('click', async () => {
                        if (!preview || action.disabled) return;
                        await copyText(JSON.stringify({format: 'bps-bermuda-manual-row-v1', read_only: true, mode,
                            receiver: row.receiver, scanner_mac: row.scanner_mac, scanner_name: row.scanner_name,
                            bermuda_entry_id: row.bermuda_entry_id, attenuation: row.attenuation,
                            current_offset_db: row.current_offset_db, proposed_offset_db: transfer ? row.transfer_current_offset_db : row.proposed_offset_db,
                            current_bps_factor: row.current_bps_factor, proposed_bps_factor: transfer ? row.current_bps_factor : row.proposed_bps_factor,
                            bps_factor_after_manual_transfer: 1, units: data.units, instructions: data.instructions}, null, 2));
                    });
                    actions.appendChild(action);
                }
                tr.appendChild(actions); table.appendChild(tr);
            }
            results.appendChild(table);
        }
        button.addEventListener('click', async () => {
            if (busy) return;
            const selected = floor.value, token = generation;
            busy = true; button.disabled = true; preview = null; copy.disabled = true;
            results.replaceChildren(); text.hidden = true;
            status.textContent = 'Loading current Bermuda model and read-only correction evidence…';
            try {
                const response = await apiFetch('/api/bps/calibration', {method: 'POST',
                    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action: 'preview_bermuda', floor: selected})});
                const data = await response.json();
                if (token !== generation || selected !== floor.value) return;
                if (!response.ok) throw new Error(data.error || `Preview failed (${response.status})`);
                if (data.read_only !== true || !Array.isArray(data.rows)) throw new Error('Invalid read-only preview response');
                preview = data; render(data); copy.disabled = false;
                status.textContent = 'Read-only preview. Current-factor transfer preserves raw ranges; proposed corrections need measured validation. No settings saved.';
            } catch (error) {
                if (token === generation && selected === floor.value) status.textContent = error.message;
            } finally {
                busy = false; button.disabled = false;
            }
        });
    }
    window.BPSBermudaCalibration = {init};
})();
