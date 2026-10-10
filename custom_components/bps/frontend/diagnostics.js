/* Authenticated, opt-in diagnostic capture controls. No polling until requested. */
(function (root) {
    'use strict';
    function init(apiFetch) {
        const el = name => document.getElementById('diagnostic' + name);
        if (!el('Start')) return;
        let status = {active: false, available: false}, timer = null, busy = false;
        function controls() {
            ['Start', 'Stop', 'Download', 'Clear', 'Mark', 'Release', 'Note', 'Refresh'].forEach(name => {
                const enabled = name === 'Start' ? !status.active && !!el('Target').value
                    : ['Stop', 'Mark', 'Release', 'Note'].includes(name) ? status.active
                    : ['Download', 'Clear'].includes(name) ? status.available : true;
                el(name).disabled = busy || !enabled;
            });
            el('Target').disabled = busy || status.active;
            el('Duration').disabled = busy || status.active;
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
            status = next;
            if (Array.isArray(next.trackers)) {
                const items = next.trackers.slice();
                (next.targets || []).forEach(key => {if (!items.some(item => item.key === key)) items.push({key, name: key});});
                options(el('Target'), items);
                if (next.active && next.targets.length) el('Target').value = next.targets[0];
            }
            if (Array.isArray(next.floors)) options(el('Floor'), next.floors.map(key => ({key, name: key})));
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
            finally {busy = false; controls();}
        }
        el('Refresh').addEventListener('click', () => run(refresh));
        el('Start').addEventListener('click', () => run(() => post({action: 'start', targets: [el('Target').value], duration_s: Number(el('Duration').value)})));
        el('Stop').addEventListener('click', () => run(() => post({action: 'stop'})));
        el('Clear').addEventListener('click', () => run(() => post({action: 'clear'})));
        el('Mark').addEventListener('click', () => run(() => {
            if (!el('X').value.trim() || !el('Y').value.trim()) throw new Error('Enter the measured X and Y coordinates first.');
            return post({action: 'annotate', kind: 'known_position', target: el('Target').value,
                floor: el('Floor').value, x_m: Number(el('X').value), y_m: Number(el('Y').value), label: el('Label').value});
        }));
        el('Release').addEventListener('click', () => run(() => post({action: 'annotate', kind: 'clear_position', target: el('Target').value})));
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
