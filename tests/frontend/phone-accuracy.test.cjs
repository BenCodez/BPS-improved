const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {readFileSync} = require('node:fs');

function setup() {
    const nodes = new Map(), calls = [];
    for (const key of ['Enable','Phone','Load','Off','Controls','Status']) nodes.set('liveGPSHA'+key, {
        checked:false, disabled:false, value:'', hidden:false, textContent:'', children:[], handlers:{},
        addEventListener(event,fn){this.handlers[event]=fn;}, fire(event='click'){return this.handlers[event]();},
        replaceChildren(){this.children=[];},appendChild(item){this.children.push(item);},
    });
    const state = {clock:0, active:false, pending:null, failed:null, stops:0, renews:0};
    const el=key=>nodes.get('liveGPSHA'+key);
    const sandbox=vm.createContext({});
    vm.runInContext(readFileSync('custom_components/bps/frontend/phone-accuracy.js','utf8'),sandbox);
    let ctl;
    ctl=sandbox.BPSPhoneAccuracy.init({
        active:()=>state.active, changed:()=>ctl?.sync(state.active),
        stopSurvey(){state.active=false;state.stops++;ctl.stop();},
        async fetch(url, options){
            assert.equal(url,'/api/bps/phone_accuracy');
            const body=options?JSON.parse(options.body):null;calls.push({body,options});
            if(body?.action==='start' && state.pending)await state.pending;
            if(state.failed)return {ok:false,json:async()=>({error:state.failed})};
            if(!body)return {ok:true,json:async()=>({services:[{key:'mobile_app_synthetic_phone',name:'Synthetic Android'}]})};
            if(body.action==='heartbeat')state.renews++;
            return {ok:true,json:async()=>({session:'opaque_session',requested_phones:1,status:'requested'})};
        },
    },{document:{getElementById:id=>nodes.get(id),createElement:()=>({})},monotonic:()=>state.clock});
    return{ctl,el,state,calls};
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
async function select(p){p.el('Enable').checked=true;await p.el('Load').fire();p.el('Phone').value='mobile_app_synthetic_phone';}

test('phone commands are opt-in and require an explicit phone selection',async()=>{
    const p=setup();await p.ctl.start();assert.equal(p.calls.length,0);
    p.el('Enable').checked=true;assert.throws(()=>p.ctl.validate(),/select.*service/);
    await p.el('Load').fire();assert.equal(p.el('Phone').value,'');
    p.el('Phone').value='mobile_app_synthetic_phone';p.ctl.validate();await p.ctl.start();
    assert.deepEqual(p.calls.at(-1).body,{action:'start',service:'mobile_app_synthetic_phone'});
    assert.match(p.el('Status').textContent,/ON requested.*unverified/);
});

test('visible survey renews at most every20seconds and stop sends keepalive OFF',async()=>{
    const p=setup();await select(p);await p.ctl.start();
    p.state.clock=19000;p.ctl.heartbeat();await settle();assert.equal(p.state.renews,0);
    p.state.clock=20000;p.ctl.heartbeat();p.ctl.heartbeat();await settle();assert.equal(p.state.renews,1);
    p.ctl.stop();await settle();assert.deepEqual(p.calls.at(-1).body,{action:'stop',session:'opaque_session'});
    assert.equal(p.calls.at(-1).options.keepalive,true);
    p.state.clock=90000;p.ctl.heartbeat();await settle();assert.equal(p.state.renews,1);
});

test('stop during pending ON sends OFF as soon as the late session is acknowledged',async()=>{
    const p=setup();await select(p);let release;p.state.pending=new Promise(r=>release=r);
    const start=p.ctl.start();assert.equal(p.ctl.busy(),true);p.ctl.stop();release();await start;
    assert.deepEqual(p.calls.at(-1).body,{action:'stop',session:'opaque_session'});
    assert.equal(p.ctl.busy(),false);assert.match(p.el('Status').textContent,/OFF requested/);
});

test('failed commands are not called enabled and recovery button stops the survey',async()=>{
    const p=setup();await select(p);p.state.failed='unavailable';await p.ctl.start();
    assert.match(p.el('Status').textContent,/request failed.*Browser GPS collection can continue/);
    p.state.failed=null;await p.el('Off').fire();await settle();
    assert.equal(p.state.stops,1);assert.deepEqual(p.calls.at(-1).body,{action:'stop'});
});
