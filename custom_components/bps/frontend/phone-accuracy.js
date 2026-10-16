/* Android Companion App commands are opt-in; the server owns expiry and OFF recovery. */
(function (root) {
    'use strict';
    function init(hooks, env = {}) {
        const doc = env.document || root.document;
        const el = name => doc.getElementById('liveGPSHA' + name);
        if (!el('Enable')) return null;
        const clock = env.monotonic || (() => root.performance.now());
        let token = null, generation = 0, pending = 0, nextRenew = 0;
        const status = text => {el('Status').textContent = text;};
        async function request(body, keepalive = false) {
            pending++; hooks.changed();
            const controller = root.AbortController ? new root.AbortController() : null;
            const timeout = controller ? root.setTimeout(() => controller.abort(), 15000) : null;
            try {
                const response = await hooks.fetch('/api/bps/phone_accuracy', body ? {method: 'POST',
                    bpsBackoffOnForbidden: false,
                    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), keepalive,
                    ...(controller ? {signal: controller.signal} : {})} : {bpsBackoffOnForbidden: false,
                        ...(controller ? {signal: controller.signal} : {})});
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || 'Phone accuracy request failed.');
                return data;
            } finally {if (timeout !== null) root.clearTimeout(timeout); pending--; hooks.changed();}
        }
        function sync(active) {
            el('Enable').disabled = el('Phone').disabled = el('Load').disabled = active || pending > 0;
            el('Controls').hidden = !el('Enable').checked;
            el('Off').disabled = pending > 0;
        }
        async function off(session) {
            try {
                const data = await request({action: 'stop', ...(session ? {session} : {})}, true);
                status(data.requested_phones === 0 ? 'No active BPS phone accuracy requests found. Saved surveys remain in Home Assistant.'
                    : 'HA high-accuracy OFF requested. Delivery to the phone is not confirmed. Saved surveys remain in Home Assistant.');
            } catch (error) {status('OFF not confirmed: ' + error.message + ' The server will retry; use Turn off HA high accuracy when reconnected.');}
        }
        function stop() {
            generation++;
            const prior = token; token = null;
            if (prior) void off(prior);
        }
        el('Enable').addEventListener('change', () => sync(hooks.active()));
        el('Load').addEventListener('click', async () => {
            try {
                const data = await request(), selected = el('Phone').value;
                if (!Array.isArray(data.services)) throw new Error('Invalid phone service list.');
                el('Phone').replaceChildren();
                for (const item of [{key: '', name: 'Select your Android phone'}, ...data.services]) {
                    const option = doc.createElement('option'); option.value = item.key; option.textContent = item.name;
                    el('Phone').appendChild(option);
                }
                el('Phone').value = data.services.some(item => item.key === selected) ? selected : '';
                status('Select your own Android Companion App service. Requests turn high accuracy ON for the survey and OFF afterward.');
            } catch (error) {status(error.message);}
        });
        el('Off').addEventListener('click', () => {hooks.stopSurvey(); generation++; token = null; void off();});
        return {sync, busy: () => pending > 0,
            validate() {
                if (el('Enable').checked && !/^mobile_app_[a-z0-9_]{1,128}$/.test(el('Phone').value))
                    throw new Error('Load phones and select your Android Companion App service first.');
            },
            async start() {
                if (!el('Enable').checked) return;
                const version = ++generation;
                try {
                    const data = await request({action: 'start', service: el('Phone').value});
                    if (typeof data.session !== 'string' || !data.session) throw new Error('Phone accuracy session was not confirmed.');
                    if (version !== generation) {await off(data.session); return;}
                    token = data.session; nextRenew = clock() + 20000;
                    status('HA high-accuracy ON requested. Phone state is unverified; browser GPS quality is checked separately.');
                } catch (error) {status('HA request failed: ' + error.message + ' Browser GPS collection can continue.');}
            },
            heartbeat() {
                if (!token || clock() < nextRenew) return;
                nextRenew = clock() + 20000;
                const session = token;
                void request({action: 'heartbeat', session}).catch(error => {
                    if (token !== session) return;
                    stop(); status('HA high-accuracy renewal failed: ' + error.message + ' Browser GPS collection can continue.');
                });
            }, stop,
        };
    }
    root.BPSPhoneAccuracy = {init};
})(globalThis);
