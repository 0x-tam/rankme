'use strict';
// The server keeps the session in an HttpOnly cookie. This module keeps only the
// CSRF value and short-lived setup/invitation secrets in memory.
const RankMeAuth = (() => {
  let csrf = '', bootstrap = '', invite = '', authenticated = false, callbacks = {};
  const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('rankme-auth') : null;
  const $ = selector => document.querySelector(selector);
  const binary = value => {
    if (typeof value !== 'string' || !/^[A-Za-z0-9_-]+$/.test(value)) throw Error('Invalid passkey challenge.');
    const raw = atob(value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4));
    return Uint8Array.from(raw, char => char.charCodeAt(0)).buffer;
  };
  const base64url = value => {
    const bytes = new Uint8Array(value);
    let raw = '';
    for (let i = 0; i < bytes.length; i += 0x8000) raw += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return btoa(raw).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  };
  const browserOptions = value => Array.isArray(value) ? value.map(browserOptions) :
    value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).map(([key, item]) =>
      [key.replace(/_([a-z])/g, (_match, letter) => letter.toUpperCase()), browserOptions(item)])) : value;
  function creationOptions(options) {
    const normalized = browserOptions(options);
    const result = {...normalized, challenge:binary(normalized.challenge), user:{...normalized.user,id:binary(normalized.user.id)}};
    result.excludeCredentials = (normalized.excludeCredentials || []).map(item => ({...item,id:binary(item.id)}));
    result.authenticatorSelection = {...normalized.authenticatorSelection,userVerification:'required'};
    return result;
  }
  function requestOptions(options) {
    const normalized = browserOptions(options);
    const result = {...normalized,challenge:binary(normalized.challenge),userVerification:'required'};
    result.allowCredentials = (normalized.allowCredentials || []).map(item => ({...item,id:binary(item.id)}));
    return result;
  }
  function credentialJSON(credential) {
    const response = credential.response;
    const result = {
      id: credential.id,
      rawId: base64url(credential.rawId),
      type: credential.type,
      response: {clientDataJSON:base64url(response.clientDataJSON)},
      clientExtensionResults: credential.getClientExtensionResults?.() || {}
    };
    if (response.attestationObject) {
      result.response.attestationObject = base64url(response.attestationObject);
      result.response.transports = response.getTransports?.() || [];
    } else {
      result.response.authenticatorData = base64url(response.authenticatorData);
      result.response.signature = base64url(response.signature);
      result.response.userHandle = response.userHandle ? base64url(response.userHandle) : null;
    }
    return result;
  }
  function supported() {
    return Boolean(window.isSecureContext && window.PublicKeyCredential && navigator.credentials?.create && navigator.credentials?.get);
  }
  function errorMessage(error) {
    if (!supported()) return 'This browser cannot use passkeys here. Open ' + location.origin + ' in Safari or Chrome.';
    if (['NotAllowedError','AbortError'].includes(error?.name)) return 'Passkey verification was cancelled or timed out. Try again when you are ready.';
    if (error?.name === 'InvalidStateError') return 'This passkey is already registered. Try another passkey.';
    return error?.publicMessage || 'Passkey verification did not finish. Please try again.';
  }
  function showError(message) {
    const box = $('#auth-error');
    box.textContent = message;
    box.hidden = !message;
  }
  async function request(path, method='GET', body) {
    const response = await fetch(path, {
      method, credentials:'same-origin',
      headers: method === 'GET' ? {} : {'Content-Type':'application/json','X-RankMe-CSRF':csrf},
      ...(body === undefined ? {} : {body:JSON.stringify(body)})
    });
    let data = {};
    try { data = await response.json(); } catch { /* Do not display response bodies. */ }
    if (!response.ok) {
      if (response.status === 401 && authenticated) await lock('Your session expired. Unlock the workspace again.', true);
      const error = Error('Auth request failed');
      error.status = response.status;
      error.publicMessage = response.status === 429 ? 'Too many attempts. Wait a moment and try again.' :
        response.status === 401 ? 'Your session expired. Unlock the workspace again.' :
        response.status === 409 ? 'That passkey action cannot be completed. Refresh and try again.' : undefined;
      throw error;
    }
    return data;
  }
  function showLocked(enrolled) {
    authenticated = false;
    $('#auth-screen').hidden = false;
    $('.app').hidden = true;
    $('#auth-login').hidden = !enrolled || Boolean(invite);
    $('#auth-invite').hidden = !enrolled || !invite;
    $('#auth-enroll').hidden = enrolled;
    $('#auth-title').textContent = enrolled && invite ? 'Add a passkey.' : enrolled ? 'Welcome back.' : 'Make this workspace yours.';
    $('#auth-description').textContent = enrolled && invite ? 'Save a passkey to this device or its password manager. Your existing passkeys stay active.' :
      enrolled ? 'Unlock your content workspace with your passkey.' : 'Create the first passkey to secure your workspace.';
    if (!supported()) showError(errorMessage());
  }
  async function status() {
    const result = await request('/api/auth/status');
    csrf = (invite ? result.preCsrf : result.csrf) || '';
    return result;
  }
  async function unlock() {
    const session = await request('/api/session');
    csrf = session.csrf || session.token || '';
    authenticated = true;
    bootstrap = '';
    invite = '';
    $('#auth-bootstrap').value = '';
    showError('');
    await callbacks.onUnlock?.();
    if (authenticated) $('#auth-screen').hidden = true;
  }
  async function lock(message='', broadcast=false) {
    authenticated = false;
    csrf = '';
    bootstrap = '';
    invite = '';
    $('#auth-bootstrap').value = '';
    callbacks.onLock?.();
    showLocked(true);
    showError(message);
    if (broadcast) channel?.postMessage({type:'lock'});
    try {
      const result = await status();
      if (!authenticated) showLocked(result.enrolled);
      if (message) showError(message);
    } catch {
      showError('RankMe is unavailable. Check your connection and try again.');
    }
  }
  async function signIn() {
    if (!supported()) throw Error('Passkeys unavailable');
    const options = await request('/api/auth/login/options','POST',{});
    const credential = await navigator.credentials.get({publicKey:requestOptions(options.publicKey)});
    await request('/api/auth/login/verify','POST',{credential:credentialJSON(credential)});
    await unlock();
  }
  async function enroll(code) {
    if (!supported()) throw Error('Passkeys unavailable');
    const secret = bootstrap || String(code || '').trim();
    if (!secret) {
      const error = Error('Setup code required');
      error.publicMessage = 'Open the one-time setup link or enter the setup code from Terminal.';
      throw error;
    }
    const options = await request('/api/auth/enroll/options','POST',{bootstrap_secret:secret});
    const credential = await navigator.credentials.create({publicKey:creationOptions(options.publicKey)});
    await request('/api/auth/enroll/verify','POST',{credential:credentialJSON(credential)});
    await unlock();
  }
  async function acceptInvite(name) {
    if (!supported()) throw Error('Passkeys unavailable');
    if (!invite) throw Error('Invitation unavailable');
    const options = await request('/api/auth/invite/options','POST',{invite_secret:invite,name:String(name || '').trim()});
    const credential = await navigator.credentials.create({publicKey:creationOptions(options.publicKey)});
    await request('/api/auth/invite/verify','POST',{invite_secret:invite,credential:credentialJSON(credential)});
    await unlock();
  }
  async function stepUp() {
    if (!supported()) throw Error('Passkeys unavailable');
    const options = await request('/api/auth/step-up/options','POST',{});
    const credential = await navigator.credentials.get({publicKey:requestOptions(options.publicKey)});
    await request('/api/auth/step-up/verify','POST',{credential:credentialJSON(credential)});
  }
  async function addCredential(name) {
    await stepUp();
    const options = await request('/api/auth/credentials/options','POST',{name:String(name || '').trim()});
    const credential = await navigator.credentials.create({publicKey:creationOptions(options.publicKey)});
    return request('/api/auth/credentials/verify','POST',{credential:credentialJSON(credential)});
  }
  async function removeCredential(id) {
    await stepUp();
    return request('/api/auth/credentials/' + encodeURIComponent(id),'DELETE',{});
  }
  async function logout() {
    let message = '';
    try { await request('/api/auth/logout','POST',{}); }
    catch { message = 'Could not confirm sign-out with the server. Close this browser tab before leaving this computer.'; }
    await lock(message, true);
  }
  async function revalidate() {
    authenticated = false;
    callbacks.onLock?.();
    showLocked(true);
    try {
      const result = await status();
      if (result.authenticated && !invite) await unlock();
      else showLocked(result.enrolled);
    } catch {
      showError('RankMe is unavailable. Check your connection and reload this page.');
    }
  }
  async function start(handlers) {
    callbacks = handlers;
    if (channel) channel.onmessage = event => { if (event.data?.type === 'lock') lock(); };
    const fragment = location.hash;
    if (fragment.startsWith('#bootstrap=')) {
      bootstrap = new URLSearchParams(fragment.slice(1)).get('bootstrap') || '';
      history.replaceState(null,'',location.pathname + location.search);
    } else if (fragment.startsWith('#add-passkey=')) {
      invite = new URLSearchParams(fragment.slice(1)).get('add-passkey') || '';
      history.replaceState(null,'',location.pathname + location.search);
    }
    $('#auth-bootstrap-field').hidden = Boolean(bootstrap);
    $('#auth-address').textContent = location.origin;
    $('#auth-sign-in').addEventListener('click', async event => {
      const button = event.currentTarget;
      button.disabled = true;
      showError('');
      try { await signIn(); } catch (error) { showError(errorMessage(error)); }
      finally { button.disabled = false; }
    });
    $('#auth-enroll').addEventListener('submit', async event => {
      event.preventDefault();
      const button = event.currentTarget.querySelector('button');
      button.disabled = true;
      showError('');
      try { await enroll($('#auth-bootstrap').value); }
      catch (error) {
        if ([403,409].includes(error.status)) { bootstrap = ''; $('#auth-bootstrap-field').hidden = false; }
        showError(errorMessage(error));
      }
      finally { button.disabled = false; }
    });
    $('#auth-invite').addEventListener('submit', async event => {
      event.preventDefault();
      const button = event.currentTarget.querySelector('button');
      button.disabled = true;
      showError('');
      try { await acceptInvite($('#auth-invite-name').value); }
      catch (error) { showError(errorMessage(error)); }
      finally { button.disabled = false; }
    });
    try {
      const result = await status();
      if (result.authenticated && !invite) await unlock();
      else showLocked(result.enrolled);
    } catch {
      showLocked(true);
      showError('RankMe is unavailable. Check your connection and reload this page.');
    }
  }
  return {start,lock,logout,revalidate,addCredential,removeCredential,credentials:() => request('/api/auth/credentials'),errorMessage,get csrf(){return csrf;},get authenticated(){return authenticated;},binary,base64url,creationOptions,requestOptions,credentialJSON};
})();
