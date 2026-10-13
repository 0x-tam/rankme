'use strict';

const chatseoPendingRequests=new Map();
function chatseoReset(){chatseoPendingRequests.clear();}

// Provider text is always escaped; only explicit safe fields become dashboard content.
function chatseoReport(id){const siteId=client(id)?.chatseo_site_id;return siteId?(state.chatseo_reports||[]).find(row=>row.client_id===id&&row.site_id===siteId)||{}:{};}
function chatseoRuns(id){const siteId=client(id)?.chatseo_site_id;return siteId?(state.chatseo_runs||[]).filter(row=>row.client_id===id&&row.site_id===siteId).slice().sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||''))):[];}
function chatseoHas(tool){return (state.chatseo?.capabilities||[]).includes(tool);}
function chatseoButton(action,id,text,disabled=false,extra=''){return `<button class="button" data-chatseo-action="${action}" data-id="${esc(id)}" ${disabled?'disabled':''} ${extra}>${text}</button>`;}
function chatseoWorkspace(c){
  const connection=state.chatseo||{},report=chatseoReport(c.id),runs=chatseoRuns(c.id),settings=c.chatseo_settings||{};
  const connected=connection.connected===true,mapped=!!c.chatseo_site_id,busy=!!clientJob(c.id);
  const unresolved=runs.some(run=>['pending','sending','processing','uncertain'].includes(run.status));
  const sites=Array.isArray(connection.sites)?connection.sites:[],site=sites.find(row=>row.id===c.chatseo_site_id);
  const credit=connection.credits?.remaining,creditText=typeof credit==='number'&&Number.isFinite(credit)?credit.toLocaleString():'Unknown';
  const actions=Array.isArray(report.actions)?report.actions:[],artifacts=Array.isArray(report.artifacts)?report.artifacts:[],conversations=Array.isArray(report.conversations)?report.conversations:[];
  return `<section class="card chatseo-card"><div class="card-head"><div><span class="eyebrow">OPTIONAL RESEARCH</span><h2>ChatSEO</h2><p class="hint">Bring ChatSEO research into this website’s workspace. Search Console, Analytics, free AI Overview checks, and evidence audits remain available independently.</p></div><div class="actions">${badge(connected?'connected':'not_connected')}${connected?chatseoButton('sync',c.id,'Refresh connection',busy):hosted?'':chatseoButton('connect',c.id,'Connect ChatSEO')}</div></div>
  <p class="hint section">This connection is separate from ChatSEO in your chat apps. Your Mac must be awake with RankMe running to retrieve results or process requests.</p>
  ${!connected?`<p class="muted section">${hosted?'Connect ChatSEO from RankMe on your Mac first. Once connected, you can use its synced results here.':'Connect your ChatSEO account on this Mac. No API key needs to be pasted into the dashboard.'}</p>`:''}
  ${connection.error?`<div class="notice error section">${esc(connection.error)}</div>`:''}
  <dl class="chatseo-summary"><div><dt>Linked website</dt><dd>${esc(mapped?(site?.domain||site?.name||c.chatseo_site_id):'Not linked')}</dd></div><div><dt>Credits remaining</dt><dd>${esc(creditText)}</dd></div><div><dt>Connection checked</dt><dd>${esc(time(connection.last_sync))}</dd></div></dl>
  <div class="actions section">${chatseoButton('map',c.id,mapped?'Change linked website':'Link website',!connected||!sites.length)}${chatseoButton('client-sync',c.id,'Refresh saved research',!connected||!mapped||busy)}${chatseoButton('settings',c.id,'Research limits')}${chatseoButton('research',c.id,'Request research',!connected||!mapped||settings.enabled!==true||!chatseoHas('send_message')||busy||unresolved)}${connected?chatseoButton('disconnect',c.id,'Disconnect'):''}</div>
  <p class="hint section">Paid research is ${settings.enabled===true?'enabled':'off'}. ${esc(settings.daily_request_limit||1)} request(s) per day; minimum balance ${esc(settings.minimum_credits||1)} credits. These limits do not cap the credits one request can use. Each request needs your separate approval.</p>
  ${unresolved?'<p class="hint section">A research request is still unresolved. Check its status below before sending another.</p>':''}
  ${connected&&!sites.length?'<p class="hint section">No websites are available from ChatSEO. Refresh the connection or check your ChatSEO account.</p>':''}
  ${connected&&!chatseoHas('send_message')?'<div class="notice warning section">Research requests are unavailable for this connection. Saved results and the free RankMe tools can still be used.</div>':''}
  ${report.error?`<div class="notice error section">${esc(report.error)}</div>`:''}
  ${report.unavailable?.length?`<p class="hint section">Some ChatSEO features are unavailable: ${report.unavailable.map(value=>esc(label(value))).join(', ')}. Missing results are unknown, not zero.</p>`:''}
  <details class="disclosure" ${runs.length?'open':''}><summary>Research requests <span class="count">${runs.length}</span></summary><div class="disclosure-body">${runs.length?runs.slice(0,20).map(run=>chatseoRunHTML(run,connected)).join(''):'<p class="muted">No requests yet. Paid research starts only after you enable it and approve a specific request.</p>'}</div></details>
  <details class="disclosure"><summary>Saved recommendations & resources <span class="count">${actions.length+artifacts.length}</span></summary><div class="disclosure-body"><p class="hint">${report.synced_at?'Last retrieved '+esc(time(report.synced_at))+'. ':''}ChatSEO recommendations require your review. They do not publish or change your website.</p>${actions.length?`<div class="seo-evidence-list">${actions.map(action=>`<div class="seo-evidence-row"><div><strong>${esc(action.title||'Recommendation')}</strong><p>${esc(action.description||'No description supplied.')}</p>${action.status?badge(action.status):''}</div></div>`).join('')}</div>`:'<p class="muted section">No saved recommendations retrieved.</p>'}${artifacts.length?`<h3 class="section">Saved resources</h3><ul class="detail-list">${artifacts.map(artifact=>`<li>${esc(artifact.title||'Untitled resource')} <span class="hint">${esc(label(artifact.type||'resource'))}</span></li>`).join('')}</ul>`:''}${conversations.length?`<h3 class="section">Previous conversations</h3><ul class="detail-list">${conversations.map(row=>`<li>${esc(row.title||'Untitled conversation')}</li>`).join('')}</ul>`:''}</div></details></section>`;
}
function chatseoRunHTML(run,connected){
  const pending=['pending','sending','processing','uncertain'].includes(run.status),artifacts=Array.isArray(run.artifacts)?run.artifacts:[];
  return `<article class="chatseo-run"><div class="actions">${badge(run.status||'unknown')}<span class="hint">${esc(time(run.created_at))}</span></div><p class="chatseo-text">${esc(run.message||'Research request')}</p>${run.error?`<div class="notice error section">${esc(run.error)}</div>`:''}${run.status==='uncertain'?'<p class="hint section">The provider may have received this request. Check its status before making another request; repeating it could use more credits.</p>':''}${run.reply?`<details class="disclosure"><summary>${run.reply_truncated?'Preview research response':'Read research response'}</summary><div class="chatseo-text disclosure-body">${esc(run.reply)}</div>${run.reply_truncated?`<p class="hint section">This is a shortened preview. Read the full saved response below.</p>`:''}</details>`:''}${run.reply_truncated?`<div class="actions section">${chatseoButton('read',run.client_id,'Read full response',false,`data-run="${esc(run.id)}"`)}</div>`:''}${run.status==='uncertain'?`<div class="actions section">${chatseoButton('resolve',run.client_id,'I checked ChatSEO — allow another request',false,`data-run="${esc(run.id)}"`)}</div>`:''}${pending?`<div class="actions section">${chatseoButton('poll',run.client_id,'Check request status',!connected||!chatseoHas('get_conversation_messages'),`data-run="${esc(run.id)}"`)}</div>`:''}${artifacts.length?`<div class="seo-evidence-list">${artifacts.map(artifact=>`<div class="seo-evidence-row"><div><strong>${esc(artifact.title||'Research resource')}</strong><small>${esc(label(artifact.type||'resource'))}</small>${artifact.content?`<details class="disclosure"><summary>Preview content</summary><div class="chatseo-text disclosure-body">${esc(artifact.content)}</div></details>`:''}</div>${run.status==='complete'?chatseoButton('import',run.client_id,'Import for review',false,`data-run="${esc(run.id)}" data-artifact="${esc(artifact.id)}"`):''}</div>`).join('')}</div>`:''}</article>`;
}
function chatseoMapModal(c){
  const sites=state.chatseo?.sites||[];
  modal('Link this website to ChatSEO',`<form id="chatseo-map-form" data-id="${esc(c.id)}"><p class="muted">Choose the ChatSEO website that belongs to ${esc(c.name||c.url)}.</p><div class="field section"><label for="chatseo-site">ChatSEO website</label><select id="chatseo-site" name="site_id" required><option value="">Choose a website</option>${sites.map(site=>`<option value="${esc(site.id)}" ${site.id===c.chatseo_site_id?'selected':''}>${esc(site.domain||site.name||site.id)}</option>`).join('')}</select></div>${formActions('Link website')}</form>`);
}
function chatseoSettingsModal(c){
  const s=c.chatseo_settings||{};
  modal('ChatSEO research limits',`<form id="chatseo-settings-form" data-id="${esc(c.id)}"><p class="muted">Enable optional research for this website. Each request still requires explicit approval.</p><label class="check-label section"><input type="checkbox" name="enabled" ${s.enabled===true?'checked':''}>Allow paid ChatSEO research requests</label><div class="form-grid section"><div class="field"><label for="chatseo-daily">Maximum requests per day</label><input id="chatseo-daily" name="daily_request_limit" type="number" min="1" max="10" value="${esc(s.daily_request_limit||1)}" required></div><div class="field"><label for="chatseo-minimum">Minimum credit balance before a request</label><input id="chatseo-minimum" name="minimum_credits" type="number" min="1" max="100000" value="${esc(s.minimum_credits||1)}" required></div></div><div class="notice warning section">The exact credit cost is not available in advance. The daily limit counts requests, and the minimum balance is a starting requirement. Neither is a spending cap.</div>${formActions('Save limits')}</form>`);
}
function chatseoResearchModal(c){
  let pending=chatseoPendingRequests.get(c.id);
  if(pending&&chatseoRuns(c.id).some(run=>run.request_id===pending.request_id&&['complete','failed','acknowledged'].includes(run.status))){chatseoPendingRequests.delete(c.id);pending=null;}
  modal('Request ChatSEO research',`<form id="chatseo-research-form" data-id="${esc(c.id)}" data-request="${esc(pending?.request_id||crypto.randomUUID())}"><p class="muted">Send one research question about ${esc(c.name||c.url)} to its linked ChatSEO website. Your question is sent to ChatSEO, which can use services linked to that account. RankMe imports returned articles as drafts.</p>${pending?'<div class="notice warning section">A previous submission has no confirmed response. This form retries that same request with the same identity. Check the request history before trying again.</div>':''}<div class="field section"><label for="chatseo-message">Research question</label><textarea id="chatseo-message" name="message" rows="6" maxlength="8000" ${pending?'readonly':''} required placeholder="For example: Which existing pages should we improve, and what evidence supports each change?">${esc(pending?.message||'')}</textarea></div><div class="notice warning section">ChatSEO may use paid credits. The exact cost is unknown and there is no per-request credit cap. Current balance: ${esc(typeof state.chatseo?.credits?.remaining==='number'?state.chatseo.credits.remaining:'unknown')}.</div><label class="check-label section"><input type="checkbox" name="credit_acknowledged" required>I approve this request using ChatSEO credits with an unknown final cost.</label>${formActions('Send approved request')}</form>`);
}
function chatseoResolveModal(c,runId){modal('Resolve an uncertain request',`<form id="chatseo-resolve-form" data-id="${esc(c.id)}" data-run="${esc(runId)}"><p class="muted">Check the conversation in your ChatSEO account first. The previous request may still finish and use credits. Allowing another request does not cancel it or refund credits.</p><label class="check-label section"><input type="checkbox" name="checked_provider" required>I checked ChatSEO and understand another request may use additional credits.</label>${formActions('Allow another request')}</form>`);}
function chatseoDisconnectModal(){modal('Disconnect ChatSEO',`<form id="chatseo-disconnect-form"><p class="muted">Disconnect this workspace’s ChatSEO account. Saved research remains available for review. This affects every linked website in RankMe.</p>${formActions('Disconnect ChatSEO')}</form>`);}
async function chatseoConnect(){
  const epoch=sessionEpoch,result=await api('/api/chatseo/connect','POST',{});
  if(!authenticated||epoch!==sessionEpoch)return;
  const url=new URL(result.url);
  if(url.origin!=='https://api.chatseo.app'||url.pathname!=='/auth/oauth2/authorize'||url.username||url.password)throw Error('The ChatSEO connection link was not valid.');
  modal('Authorize ChatSEO',`<p class="muted">Continue to ChatSEO to authorize this Mac. After connecting, return here and refresh the connection.</p><a class="button primary section" href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">Continue to ChatSEO</a>`);
}

document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-chatseo-action]');if(!button)return;
  event.preventDefault();event.stopImmediatePropagation();if(!authenticated||button.disabled)return;
  const action=button.dataset.chatseoAction,id=button.dataset.id,c=client(id),epoch=sessionEpoch;
  try{
    if(action==='map'&&c)return chatseoMapModal(c);
    if(action==='settings'&&c)return chatseoSettingsModal(c);
    if(action==='research'&&c)return chatseoResearchModal(c);
    if(action==='resolve'&&c)return chatseoResolveModal(c,button.dataset.run);
    if(action==='disconnect')return chatseoDisconnectModal();
    button.disabled=true;button.setAttribute('aria-busy','true');
    if(action==='connect'&&!hosted)await chatseoConnect();
    if(action==='sync')await mutation('/api/chatseo/sync','POST',{},'ChatSEO connection refreshed.');
    if(action==='client-sync')await mutation(`/api/clients/${encodeURIComponent(id)}/chatseo-sync`,'POST',{},'ChatSEO research refresh queued.');
    if(action==='read'){
      const run=await api(`/api/clients/${encodeURIComponent(id)}/chatseo-read`,'POST',{run_id:button.dataset.run});
      if(!authenticated||epoch!==sessionEpoch)return;
      if(run.id!==button.dataset.run||run.client_id!==id||!client(id)?.chatseo_site_id||run.site_id!==client(id).chatseo_site_id)throw Error('This saved response no longer belongs to the linked website. Refresh the dashboard.');
      modal('Full saved ChatSEO response',`<p class="hint">Saved research from ${esc(time(run.created_at))}. Reading this response does not start a new ChatSEO request.</p><div class="chatseo-text section">${esc(run.reply||'No response text was saved.')}</div>`,true);
    }
    if(action==='poll')await mutation(`/api/clients/${encodeURIComponent(id)}/chatseo-poll`,'POST',{run_id:button.dataset.run},'ChatSEO request status checked.');
    if(action==='import')await mutation(`/api/clients/${encodeURIComponent(id)}/chatseo-import`,'POST',{run_id:button.dataset.run,artifact_id:button.dataset.artifact},'Research imported into Content for review.');
  }catch(error){if(authenticated&&epoch===sessionEpoch)toast(error.message);}finally{button.disabled=false;button.removeAttribute('aria-busy');}
},true);

document.addEventListener('submit',async event=>{
  const form=event.target;if(!['chatseo-map-form','chatseo-settings-form','chatseo-research-form','chatseo-disconnect-form','chatseo-resolve-form'].includes(form.id))return;
  event.preventDefault();event.stopImmediatePropagation();if(!authenticated||form.dataset.submitting==='true')return;
  const values=Object.fromEntries(new FormData(form)),submit=form.querySelector('[type=submit]'),epoch=sessionEpoch;
  form.dataset.submitting='true';if(submit){submit.disabled=true;submit.setAttribute('aria-busy','true');}
  try{
    let path,body,message;
    const prefix=`/api/clients/${encodeURIComponent(form.dataset.id)}`;
    if(form.id==='chatseo-map-form'){path=prefix+'/chatseo-map';body={site_id:values.site_id};message='ChatSEO website linked.';}
    if(form.id==='chatseo-settings-form'){path=prefix+'/chatseo-settings';body={enabled:form.querySelector('[name=enabled]').checked,daily_request_limit:Number(values.daily_request_limit),minimum_credits:Number(values.minimum_credits)};message='ChatSEO research limits saved.';}
    if(form.id==='chatseo-research-form'){
      if(!form.querySelector('[name=credit_acknowledged]').checked)throw Error('Approve the credit use before sending this request.');
      if(!values.message.trim())throw Error('Enter a research question.');
      path=prefix+'/chatseo-send';body={message:values.message,credit_acknowledged:true,request_id:form.dataset.request};
      chatseoPendingRequests.set(form.dataset.id,{request_id:body.request_id,message:body.message});message='Research request saved. Follow its status in ChatSEO.';
    }
    if(form.id==='chatseo-resolve-form'){if(!form.querySelector('[name=checked_provider]').checked)throw Error('Confirm that you checked ChatSEO first.');path=prefix+'/chatseo-resolve';body={run_id:form.dataset.run,checked_provider:true};message='Previous request acknowledged. Its history is preserved.';}
    if(form.id==='chatseo-disconnect-form'){path='/api/chatseo/disconnect';body={};message='ChatSEO disconnected.';}
    await mutation(path,'POST',body,message);if(authenticated&&epoch===sessionEpoch){if(['chatseo-research-form','chatseo-resolve-form'].includes(form.id))chatseoPendingRequests.delete(form.dataset.id);close();}
  }catch(error){if(authenticated&&epoch===sessionEpoch){if(error.queued){toast(error.message);close();}else{const errorBox=$('#dialog-error');if(errorBox){errorBox.textContent=error.message;errorBox.tabIndex=-1;errorBox.focus();}}}}finally{form.dataset.submitting='false';if(submit){submit.disabled=false;submit.removeAttribute('aria-busy');}}
},true);
