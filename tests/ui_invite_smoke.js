'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/auth.js'), 'utf8');
const secret = 'A'.repeat(43);
const elements = new Map();
function element(name) {
  if (!elements.has(name)) elements.set(name, {
    hidden:false, disabled:false, value:'', textContent:'', listeners:{},
    addEventListener(kind, listener){this.listeners[kind]=listener;},
    querySelector(){return element(name + ' button');}
  });
  return elements.get(name);
}
let replaced = false, created = false, verified = false, unlocked = false;
const calls = [];
const encoded = value => Buffer.from(value).toString('base64url');
const bytes = value => Uint8Array.from(Buffer.from(value)).buffer;
const credential = {
  id:'new-passkey',rawId:bytes('new-passkey'),type:'public-key',
  response:{clientDataJSON:bytes('client-data'),attestationObject:bytes('attestation'),getTransports:()=>['internal']},
  getClientExtensionResults:()=>({})
};
const context = vm.createContext({
  URLSearchParams, Uint8Array, ArrayBuffer, atob, btoa,
  document:{querySelector:element},
  location:{hash:'#add-passkey='+secret,pathname:'/',search:'?safe=1',origin:'https://example.test'},
  history:{replaceState(_a,_b,url){replaced=true;assert.equal(url,'/?safe=1');}},
  window:{isSecureContext:true,PublicKeyCredential:function(){}},
  navigator:{credentials:{create:async ({publicKey})=>{
    created = true;
    assert.equal(publicKey.authenticatorSelection.authenticatorAttachment,'platform');
    assert.equal(publicKey.authenticatorSelection.userVerification,'required');
    return credential;
  },get:async()=>{throw Error('Existing-device login must not run');}}},
  fetch:async (route,options={})=>{
    assert(replaced,'invite fragment must be removed before any request');
    assert(!route.includes(secret),'secret must not enter URL');
    calls.push({route,options});
    let body;
    if (route==='/api/auth/status') body={enrolled:true,authenticated:false,csrf:'pre-csrf',preCsrf:'pre-csrf'};
    else if (route==='/api/auth/invite/options') body={publicKey:{challenge:encoded('challenge'),user:{id:encoded('owner-handle')},authenticatorSelection:{authenticatorAttachment:'platform',userVerification:'required'}}};
    else if (route==='/api/auth/invite/verify') { verified = true; body={verified:true}; }
    else if (route==='/api/session') body={csrf:'session-csrf'};
    else throw Error('Unexpected request: '+route);
    return {ok:true,status:200,json:async()=>body};
  }
});
vm.runInContext(source,context);
const auth = vm.runInContext('RankMeAuth',context);
(async()=>{
  await auth.start({onUnlock:()=>{unlocked=true;}});
  assert.equal(calls.length,1);
  assert.equal(element('#auth-invite').hidden,false);
  assert.equal(element('#auth-login').hidden,true);
  assert.equal(element('.app').hidden,true);
  assert.equal(auth.authenticated,false);
  element('#auth-invite-name').value='Phone';
  await element('#auth-invite').listeners.submit({preventDefault(){},currentTarget:element('#auth-invite')});
  assert(created && verified && unlocked);
  assert.equal(auth.authenticated,true);
  assert.equal(calls[1].route,'/api/auth/invite/options');
  assert.equal(calls[2].route,'/api/auth/invite/verify');
  assert.equal(JSON.parse(calls[1].options.body).invite_secret,secret);
  assert.equal(JSON.parse(calls[2].options.body).invite_secret,secret);
  assert.equal(calls[1].options.headers['X-RankMe-CSRF'],'pre-csrf');
  assert.equal(calls[2].options.headers['X-RankMe-CSRF'],'pre-csrf');
  assert.equal(calls[3].route,'/api/session');
  assert.equal(auth.csrf,'session-csrf');
  elements.clear();
  let secondReplaced = false, secondUnlocks = 0;
  const existing = vm.createContext({
    URLSearchParams, Uint8Array, ArrayBuffer, atob, btoa,
    document:{querySelector:element},
    location:{hash:'#add-passkey='+secret,pathname:'/',search:'',origin:'https://example.test'},
    history:{replaceState(){secondReplaced=true;}},
    window:{isSecureContext:true,PublicKeyCredential:function(){}},
    navigator:{credentials:{create:async()=>credential,get:async()=>credential}},
    fetch:async route=>{
      assert(secondReplaced);
      assert.equal(route,'/api/auth/status');
      return {ok:true,status:200,json:async()=>({enrolled:true,authenticated:true,csrf:'session-csrf',preCsrf:'pre-csrf'})};
    }
  });
  vm.runInContext(source,existing);
  const existingAuth = vm.runInContext('RankMeAuth',existing);
  await existingAuth.start({onUnlock:()=>{secondUnlocks++;}});
  assert.equal(secondUnlocks,0,'existing session must not skip the invitation ceremony');
  assert.equal(existingAuth.csrf,'pre-csrf');
  assert.equal(element('#auth-invite').hidden,false);
  assert.equal(element('.app').hidden,true);
  console.log('Invitation UI smoke checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
