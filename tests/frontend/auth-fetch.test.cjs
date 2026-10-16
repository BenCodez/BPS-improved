const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

function setup() {
    const source = readFileSync('custom_components/bps/frontend/script.js', 'utf8');
    const start = source.indexOf('    async function bpsFetch('), end = source.indexOf("    const upload =",start);
    const calls=[], state={now:10000,status:403};
    const context=vm.createContext({bpsAuthToken:'synthetic_token',_authDeadToken:null,_authFails:0,_authFailUntil:0,_authBackoff:0,
        Date:{now:()=>state.now},Response,window:{parent:null},
        async fetch(url,options){calls.push({url,options});return new Response('{}',{status:state.status});}});
    vm.runInContext(source.slice(start,end),context);
    return{state,calls,request:(url,options)=>context.bpsFetch(url,options)};
}

test('expected administrator denial leaves ordinary GPS fetches usable',async()=>{
    const p=setup();
    assert.equal((await p.request('/api/bps/phone_accuracy',{bpsBackoffOnForbidden:false})).status,403);
    p.state.status=200;
    assert.equal((await p.request('/api/bps/ground_truth',{method:'POST'})).status,200);
    assert.equal(p.calls.length,2);
    assert.equal(p.calls[0].options.headers.Authorization,'Bearer synthetic_token');
    assert.equal('bpsBackoffOnForbidden' in p.calls[0].options,false);
});

test('expired authentication still backs off phone requests',async()=>{
    const p=setup();p.state.status=401;
    assert.equal((await p.request('/api/bps/phone_accuracy',{bpsBackoffOnForbidden:false})).status,401);
    assert.equal((await p.request('/api/bps/ground_truth')).status,503);
    assert.equal(p.calls.length,1);
});

test('existing forbidden-response backoff remains enabled for other endpoints',async()=>{
    const p=setup();await p.request('/api/bps/other');
    assert.equal((await p.request('/api/bps/other')).status,503);
    assert.equal(p.calls.length,1);
});
