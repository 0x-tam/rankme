'use strict';
// Run with: node tests/ui_auth_smoke.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/auth.js'), 'utf8');
const elements = new Map();
function element(name) {
  if (!elements.has(name)) elements.set(name, {
    hidden:false, disabled:false, value:'', textContent:'', listeners:{},
    addEventListener(kind, listener){this.listeners[kind]=listener;},
    querySelector(){return element(name + ' button');}
  });
  return elements.get(name);
}
let replaced = false, unlocks = 0, locks = 0, createOptions, verified;
const calls = [];
const bytes = value => Uint8Array.from(Buffer.from(value));
const encoded = value => Buffer.from(value).toString('base64url');
const challenge = encoded('challenge');
const credential = {
  id:'credential-id',rawId:bytes('credential-id').buffer,type:'public-key',
  response:{clientDataJSON:bytes('client-data').buffer,attestationObject:bytes('attestation').buffer,getTransports:()=>['internal']},
  getClientExtensionResults:()=>({})
};
const context = vm.createContext({
  console, URLSearchParams, Uint8Array, ArrayBuffer, atob, btoa,
  document:{querySelector:element},
  location:{hash:'#bootstrap=one-use-code',pathname:'/',search:''},
  history:{replaceState(_a,_b,url){replaced=true;assert.equal(url,'/');}},
  window:{isSecureContext:true,PublicKeyCredential:function(){}},
  navigator:{credentials:{create:async ({publicKey})=>{createOptions=publicKey;return credential;},get:async()=>credential}},
  fetch:async (route,options={})=>{
    assert(replaced,'bootstrap fragment must be removed before any request');
    assert(!route.includes('one-use-code'),'setup secret must not enter the URL');
    calls.push({route,options});
    let body;
    if(route==='/api/auth/status')body={enrolled:false,authenticated:false,csrf:'preauth-csrf'};
    else if(route==='/api/auth/enroll/options')body={publicKey:{challenge,user:{id:encoded('user-id'),name:'owner',display_name:'Owner'},rp:{name:'RankMe',id:'localhost'},pub_key_cred_params:[{type:'public-key',alg:-7}],authenticator_selection:{resident_key:'required',user_verification:'required'}}};
    else if(route==='/api/auth/enroll/verify'){verified=JSON.parse(options.body);body={ok:true};}
    else if(route==='/api/session')body={csrf:'session-csrf'};
    else body={ok:true};
    return {ok:true,status:200,json:async()=>body};
  }
});
vm.runInContext(source,context);
const auth = vm.runInContext('RankMeAuth',context);
(async()=>{
  await auth.start({onUnlock:()=>{unlocks++;},onLock:()=>{locks++;}});
  assert.equal(unlocks,0);
  assert.equal(calls.length,1);
  assert.equal(calls[0].route,'/api/auth/status');
  assert.equal(element('#auth-screen').hidden,false);
  assert.equal(element('.app').hidden,true);
  assert.equal(element('#auth-bootstrap-field').hidden,true);
  assert.equal(auth.csrf,'preauth-csrf');
  await element('#auth-enroll').listeners.submit({preventDefault(){},currentTarget:element('#auth-enroll')});
  assert.equal(unlocks,1);
  assert.equal(auth.csrf,'session-csrf');
  assert.equal(element('#auth-screen').hidden,true);
  assert.equal(calls[1].route,'/api/auth/enroll/options');
  assert.equal(calls[1].options.headers['X-RankMe-CSRF'],'preauth-csrf');
  assert.equal(JSON.parse(calls[1].options.body).bootstrap_secret,'one-use-code');
  assert.equal(Buffer.from(createOptions.challenge).toString(),'challenge');
  assert.equal(Buffer.from(createOptions.user.id).toString(),'user-id');
  assert.equal(createOptions.user.displayName,'Owner');
  assert.equal(createOptions.pubKeyCredParams[0].type,'public-key');
  assert.equal(createOptions.authenticatorSelection.residentKey,'required');
  assert.equal(createOptions.authenticatorSelection.userVerification,'required');
  assert.equal(verified.credential.response.attestationObject,encoded('attestation'));
  assert.equal(verified.credential.response.clientDataJSON,encoded('client-data'));
  assert.equal(verified.credential.rawId,encoded('credential-id'));
  assert(!calls.some(call=>call.route==='/api/state'));
  await auth.lock();
  assert.equal(locks,1);
  assert.equal(auth.authenticated,false);
  assert.equal(element('.app').hidden,true);
  assert.equal(element('#auth-bootstrap').value,'');
  assert.throws(()=>auth.binary('not+base64'));
  assert.equal(auth.base64url(bytes('round trip').buffer),encoded('round trip'));
  console.log('Auth UI smoke checks passed: locked startup, fragment clearing, CSRF, passkey encoding, and state clearing.');
})().catch(error=>{console.error(error);process.exitCode=1;});
