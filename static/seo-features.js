'use strict';

function seoFeaturesWorkspace(c){
  const config={enabled:false,interval_days:1,queries:[],country:'us',language:'en',location:'',device:'desktop',...(c.aio_settings||{})};
  const readiness=state.aio_status||{},rows=(state.aio_summaries||[]).filter(r=>r.client_id===c.id);
  const audit=(state.evidence_audits||[]).find(r=>r.client_id===c.id);
  const claims=(state.experience_claims||[]).filter(r=>r.client_id===c.id);
  const growth={enabled:false,interval_days:1,...(c.growth_settings||{})};
  const running=!!clientJob(c.id);
  const ready=readiness.status==='ready';
  const statusText=readiness.configured?`${readiness.calls_today??'—'} of ${readiness.daily_call_cap??'—'} free browser checks used today`:'Open local Chrome on the Mac to collect free observations';
  return `<section class="card seo-feature-card"><div class="card-head"><div><span class="eyebrow">GOOGLE SEARCH OBSERVATIONS</span><h2>AI Overview monitor</h2><p class="hint">Actual Google Search observations from local Chrome for each configured query. Country and language are search hints; physical location is not verified. History starts when collection begins.</p></div><div class="actions"><button class="button" data-seo-action="aio-config" data-id="${esc(c.id)}">Configure</button><button class="button primary" data-seo-action="aio-run" data-id="${esc(c.id)}" ${running||!config.queries.length||!ready||!!config.location||config.device!=='desktop'||config.language!=='en'?'disabled':''}>Check now</button></div></div><div class="actions">${badge(config.enabled?'active':'off')}${badge(ready?'ready':'unavailable')}<span class="hint">${esc(statusText)}</span></div>${readiness.reason&&readiness.reason!=='ready'?`<p class="hint section">Browser: ${esc(readiness.reason.replaceAll('_',' '))}. Manual observations can still be recorded with provenance.</p>`:''}<p class="hint section">${config.queries.length} configured ${config.queries.length===1?'query':'queries'} · ${esc(config.country.toUpperCase())}/${esc(config.language)} · ${esc(config.device)}${config.location?' · '+esc(config.location):''} · ${config.enabled?'checks every '+esc(config.interval_days)+' day(s)':'automatic checks off'}</p>${c.aio_error?`<div class="notice error section">${esc(c.aio_error)}</div>`:''}<div class="actions section"><button class="button small" data-seo-action="aio-import" data-id="${esc(c.id)}" ${config.queries.length?'':'disabled'}>Record a manual observation</button></div>${rows.length?rows.map(aioSummaryCard).join(''):'<div class="visibility-history-empty"><h3>No queries configured</h3><p class="muted">Choose the search questions you want to track. A missing observation is unknown, not an absent AI Overview.</p></div>'}</section>
  <section class="card seo-feature-card"><div class="card-head"><div><span class="eyebrow">AUDIT & GROWTH</span><h2>Evidence audit</h2><p class="hint">Checks observed public pages and connected Search Console data. Existing pages are reviewed before a new article is proposed.</p></div><div class="actions"><button class="button" data-seo-action="growth-config" data-id="${esc(c.id)}">Schedule</button><button class="button primary" data-seo-action="audit-run" data-id="${esc(c.id)}" ${running?'disabled':''}>${audit?'Run fresh audit':'Run first audit'}</button></div></div><p class="hint">${growth.enabled?`Recurring audit every ${esc(growth.interval_days)} day(s)`:'Recurring evidence audits off'} · Weekly article schedule remains separate.</p>${c.evidence_audit_error?`<div class="notice error">${esc(c.evidence_audit_error)}</div>`:''}${audit?`<p class="hint">Observed ${esc(time(audit.observed_at))}. ${esc(audit.audit?.summary?.pages_observed??'—')} pages and ${esc(audit.audit?.summary?.queries_observed??'—')} Search Console queries within available coverage.</p><div class="actions section">${[['md','Markdown'],['pdf','PDF'],['xlsx','Spreadsheet']].map(([fmt,label])=>`<button class="button small" data-seo-action="audit-download" data-id="${esc(c.id)}" data-format="${fmt}">Download ${label}</button>`).join('')}</div>${(audit.audit?.actions||[]).length?`<div class="seo-evidence-list">${audit.audit.actions.map(a=>`<div class="seo-evidence-row"><div><strong>${esc(a.title)}</strong><small>${esc(label(a.kind))} · ${esc(label(a.status||'needs_verification'))}</small></div>${a.existing_url?`<a href="${esc(safeURL(a.existing_url))}" target="_blank" rel="noopener noreferrer">Existing page ↗</a>`:''}</div>`).join('')}</div>`:'<p class="muted section">No supported growth action found in this audit sample. Missing data stays unknown.</p>'}`:'<p class="muted">Run an audit to collect a dated baseline and download the full evidence report.</p>'}</section>
  <section class="card seo-feature-card"><div class="card-head"><div><span class="eyebrow">VERIFIED BY THE OWNER</span><h2>Business experience</h2><p class="hint">Record concrete first-hand facts with their source. Only facts you confirm can inform a new article brief. Source verification is tracked separately.</p></div><button class="button" data-seo-action="experience-add" data-id="${esc(c.id)}">Add experience</button></div>${claims.length?`<div class="seo-evidence-list">${claims.map(r=>`<div class="seo-evidence-row"><div><strong>${esc(r.claim||r.facts?.[0]||'Experience')}</strong><small>${r.user_confirmed?'Confirmed by you':'Awaiting your confirmation'} · ${r.source_verification_status==='verified'?'Source checked':'Source not independently checked'}</small>${r.source_url?`<a href="${esc(safeURL(r.source_url))}" target="_blank" rel="noopener noreferrer">Source ↗</a>`:r.source_note?`<small>${esc(r.source_note)}</small>`:''}</div><div class="actions"><button class="button small" data-seo-action="experience-edit" data-id="${esc(r.id)}">Review</button><button class="button danger small" data-seo-action="experience-remove" data-id="${esc(r.id)}">Remove</button></div></div>`).join('')}</div>`:'<p class="muted">No experience facts recorded. Unsupported claims remain out of article briefs.</p>'}</section>`;
}

function aioSummaryCard(row){
  const s=row.summary||{},days=s.daily_evidence||[],citations=s.citations||[],brief=row.brief||{};
  return `<details class="disclosure seo-aio-query"><summary>${esc(row.query)} <span class="hint">${esc(s.sample_days??0)} observed day(s) · ${s.enough_history?'7-day history available':'Collect 7 distinct days for a trend'}</span></summary><div class="disclosure-body"><p class="hint">${s.sample_days?`${esc(s.present_days)} observed with an AI Overview; ${esc(s.absent_days)} observed without one. ${esc(label(s.basis_source||'observation'))} evidence for this exact cohort.`:'No observation yet. Nothing has been inferred.'}</p>${days.length?`<div class="seo-day-grid">${days.map(day=>`<div><strong>${esc(date(day.date))}</strong>${badge(day.status)}<small>${esc(day.source==='manual'?'Manual evidence':day.source==='browser'?'Local Chrome observation':day.source==='provider'?'Provider observation':'Source unknown')}</small>${day.answer?`<p>${esc(day.answer)}</p>`:''}</div>`).join('')}</div>`:''}${citations.length?`<h3 class="section">Cited sources</h3><div class="seo-evidence-list">${citations.map(source=>`<div class="seo-evidence-row"><div><a href="${esc(safeURL(source.url))}" target="_blank" rel="noopener noreferrer">${esc(source.title||source.domain||source.url)}</a><small>${esc(source.days_cited)} observed day(s) cited; source content not verified by this monitor</small></div></div>`).join('')}</div>`:''}${s.citation_changes?.length?`<p class="hint section">Latest change: ${esc(s.citation_changes.at(-1).added?.length||0)} added and ${esc(s.citation_changes.at(-1).lost?.length||0)} lost cited URLs between observed days.</p>`:''}${brief.status&&brief.status!=='insufficient_evidence'?`<h3 class="section">Content brief from observed sources</h3>${evidenceHTML(brief)}`:''}<button class="button small section" data-seo-action="aio-history" data-id="${esc(row.client_id)}" data-query="${esc(row.query)}">Download this query’s history</button><p class="hint section">Manual observations are labeled and are not provider-verified. One sample per observed UTC day informs the trend; missing days are not filled in.</p></div></details>`;
}

function aioConfigModal(c){
  const s={enabled:false,interval_days:1,queries:[],country:'us',language:'en',location:'',device:'desktop',...(c.aio_settings||{})};
  modal('Configure AI Overview monitoring',`<form id="seo-aio-config-form" data-id="${esc(c.id)}"><p class="muted">Track Google searches through local Chrome on the Mac. Automatic checks are free and run when the Mac is awake.</p><div class="field section"><label for="aio-queries">Queries, one per line (up to 6)</label><textarea id="aio-queries" name="queries" maxlength="1300" rows="5" required>${esc(s.queries.join('\n'))}</textarea></div><div class="form-grid"><div class="field"><label for="aio-country">Country code</label><input id="aio-country" name="country" maxlength="2" value="${esc(s.country)}" required></div><div class="field"><label for="aio-language">Language code</label><input id="aio-language" name="language" maxlength="2" value="${esc(s.language)}" required></div><div class="field"><label for="aio-location">Exact location (manual only)</label><input id="aio-location" name="location" maxlength="100" value="${esc(s.location)}"></div><div class="field"><label for="aio-device">Device</label><select id="aio-device" name="device">${['desktop','mobile','tablet'].map(v=>`<option value="${v}" ${s.device===v?'selected':''}>${label(v)}</option>`).join('')}</select></div><div class="field"><label for="aio-interval">Check every (days)</label><input id="aio-interval" type="number" name="interval_days" min="1" max="30" value="${esc(s.interval_days)}" required></div></div><label class="check-label section"><input type="checkbox" name="enabled" ${s.enabled?'checked':''}>Enable automatic checks</label><div class="notice section">Automatic Chrome checks currently support English desktop and no exact location. Country and language are search hints. Other cohorts can be recorded manually. The daily cap is six checks and missing days are never invented.</div>${formActions('Save monitoring settings')}</form>`);
}

function aioImportModal(c){
  const s=c.aio_settings||{};
  modal('Record a manual AI Overview observation',`<form id="seo-aio-import-form" data-id="${esc(c.id)}"><p class="muted">Use this only for an actual Google search you observed. The record will be marked manual, with its date and evidence. It will not be represented as a provider result.</p><div class="field section"><label for="aio-import-query">Configured query</label><select id="aio-import-query" name="query">${(s.queries||[]).map(q=>`<option value="${esc(q)}">${esc(q)}</option>`).join('')}</select></div><div class="form-grid"><div class="field"><label for="aio-import-time">Observed time</label><input id="aio-import-time" name="observed_at" type="datetime-local" required></div><div class="field"><label for="aio-import-status">Result</label><select id="aio-import-status" name="status"><option value="present">AI Overview appeared</option><option value="absent">No AI Overview observed</option></select></div></div><div class="field section"><label for="aio-import-answer">Observed answer text (when present)</label><textarea id="aio-import-answer" name="answer" rows="5" maxlength="12000"></textarea></div><div class="field section"><label for="aio-import-citations">Cited URLs, one per line (optional)</label><textarea id="aio-import-citations" name="citations" rows="3" maxlength="6000"></textarea></div><div class="field section"><label for="aio-import-evidence">Evidence URL (optional)</label><input id="aio-import-evidence" name="evidence_url" type="url" maxlength="2000"></div><div class="field section"><label for="aio-import-note">Evidence note</label><textarea id="aio-import-note" name="evidence_note" rows="2" maxlength="1000" placeholder="For example: recorded in a signed-in browser on this device"></textarea></div>${formActions('Record observation')}</form>`);
  $('#aio-import-time').value=new Date(Date.now()-new Date().getTimezoneOffset()*60000).toISOString().slice(0,16);
}

function growthConfigModal(c){
  const s={enabled:false,interval_days:1,jev_enabled:false,...(c.growth_settings||{})};
  modal('Schedule evidence audits',`<form id="seo-growth-config-form" data-id="${esc(c.id)}"><p class="muted">Run a dated public-page and Search Console audit while the Mac is awake. Existing weekly article scheduling stays separate. A new page is never created from a candidate gap alone.</p><div class="field section"><label for="growth-interval">Audit every (days)</label><input id="growth-interval" name="interval_days" type="number" min="1" max="30" required value="${esc(s.interval_days)}"></div><label class="check-label section"><input type="checkbox" name="enabled" ${s.enabled?'checked':''}>Enable recurring evidence audits</label><label class="check-label section"><input type="checkbox" name="jev_enabled" ${s.jev_enabled?'checked':''}>Use bounded Jev editorial judgments for ambiguous growth actions</label><div class="notice section">A one-day interval checks daily. Search data and PageSpeed remain unknown when those sources are unavailable. Jev does not write articles or override evidence checks.</div>${formActions('Save audit schedule')}</form>`);
}

function experienceModal(c,record){
  modal(record?'Review experience':'Add business experience',`<form id="seo-experience-form" data-id="${esc(record?.id||c.id)}" data-client="${esc(c.id)}"><p class="muted">Describe one concrete, first-hand fact. Review its provenance before allowing it into article briefs.</p><div class="field section"><label for="experience-claim">Experience fact</label><textarea id="experience-claim" name="claim" minlength="10" maxlength="2000" rows="4" required>${esc(record?.claim||'')}</textarea></div><div class="field section"><label for="experience-source">Public source URL (optional)</label><input id="experience-source" name="source_url" type="url" maxlength="2000" value="${esc(record?.source_url||'')}"></div><div class="field section"><label for="experience-note">Source note</label><textarea id="experience-note" name="source_note" maxlength="1000" rows="3">${esc(record?.source_note||'')}</textarea></div><label class="check-label section"><input type="checkbox" name="confirmed" ${record?.user_confirmed?'checked':''}>I confirm this fact is accurate and may be used in article briefs</label><div class="notice section">Confirmation records your review. It does not independently verify an external source. Editing a confirmed fact returns it to review.</div>${formActions(record?'Save experience':'Add experience')}</form>`);
}

async function seoDownload(id,format){
  const data=await api(`/api/clients/${encodeURIComponent(id)}/evidence-report`,'POST',{format});
  saveEncodedDownload(data);
}
async function seoHistoryDownload(id,query){
  const data=await api(`/api/clients/${encodeURIComponent(id)}/aio-history`,'POST',{query});
  saveEncodedDownload(data);
}
function saveEncodedDownload(data){
  if(!data?.base64||!data.filename)throw Error('Report download was unavailable');
  const binary=atob(data.base64),bytes=Uint8Array.from(binary,char=>char.charCodeAt(0));
  const blob=new Blob([bytes],{type:data.content_type||'application/octet-stream'}),url=URL.createObjectURL(blob);
  const link=document.createElement('a');link.href=url;link.download=data.filename;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}

document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-seo-action]');if(!button)return;
  event.preventDefault();event.stopImmediatePropagation();if(!authenticated)return;
  const action=button.dataset.seoAction,id=button.dataset.id;
  try{
    if(action==='aio-config')return aioConfigModal(client(id));
    if(action==='aio-import')return aioImportModal(client(id));
    if(action==='growth-config')return growthConfigModal(client(id));
    if(action==='experience-add')return experienceModal(client(id));
    if(action==='experience-edit'){const row=(state.experience_claims||[]).find(r=>r.id===id);if(row)return experienceModal(client(row.client_id),row);}
    button.disabled=true;
    if(action==='aio-run')await mutation(`/api/clients/${encodeURIComponent(id)}/aio-collect`,'POST',{},'AI Overview check queued.');
    if(action==='audit-run')await mutation(`/api/clients/${encodeURIComponent(id)}/evidence-audit`,'POST',{},'Evidence audit queued.');
    if(action==='audit-download')await seoDownload(id,button.dataset.format);
    if(action==='aio-history')await seoHistoryDownload(id,button.dataset.query);
    if(action==='experience-remove'){await mutation(`/api/experience/${encodeURIComponent(id)}/remove`,'POST',{},'Experience removed.');}
  }catch(error){toast(error.message);}finally{button.disabled=false;}
},true);

document.addEventListener('submit',async event=>{
  const form=event.target;if(!['seo-aio-config-form','seo-aio-import-form','seo-experience-form','seo-growth-config-form'].includes(form.id))return;
  event.preventDefault();event.stopImmediatePropagation();if(!authenticated)return;
  const values=Object.fromEntries(new FormData(form));const submit=form.querySelector('[type=submit]');if(submit)submit.disabled=true;
  try{
    if(form.id==='seo-aio-config-form'){
      const queries=values.queries.split('\n').map(v=>v.trim()).filter(Boolean);
      await mutation(`/api/clients/${encodeURIComponent(form.dataset.id)}`,'PATCH',{aio_settings:{enabled:form.querySelector('[name=enabled]').checked,interval_days:Number(values.interval_days),queries,country:values.country.trim().toLowerCase(),language:values.language.trim().toLowerCase(),location:values.location.trim(),device:values.device}},'AI Overview settings saved.');
    }else if(form.id==='seo-growth-config-form'){
      await mutation(`/api/clients/${encodeURIComponent(form.dataset.id)}`,'PATCH',{growth_settings:{enabled:form.querySelector('[name=enabled]').checked,interval_days:Number(values.interval_days),jev_enabled:form.querySelector('[name=jev_enabled]').checked}},'Evidence audit schedule saved.');
    }else if(form.id==='seo-aio-import-form'){
      const c=client(form.dataset.id),s=c.aio_settings||{};
      const observation={query:values.query,country:s.country,language:s.language,location:s.location||'',device:s.device,
        observed_at:new Date(values.observed_at).toISOString(),status:values.status,answer:values.answer.trim(),
        citations:values.citations.split('\n').map(url=>url.trim()).filter(Boolean).slice(0,20).map(url=>({url})),
        evidence_url:values.evidence_url.trim(),evidence_note:values.evidence_note.trim()};
      await mutation(`/api/clients/${encodeURIComponent(c.id)}/aio-import`,'POST',{observation},'Manual observation saved with its source.');
    }else{
      const body={claim:values.claim.trim(),source_url:values.source_url.trim(),source_note:values.source_note.trim(),confirmed:form.querySelector('[name=confirmed]').checked};
      const record=(state.experience_claims||[]).find(r=>r.id===form.dataset.id);
      if(record)await mutation(`/api/experience/${encodeURIComponent(record.id)}/edit`,'PATCH',body,'Experience saved.');
      else await mutation(`/api/clients/${encodeURIComponent(form.dataset.client)}/experience`,'POST',body,'Experience added.');
    }
    close();
  }catch(error){if(error.queued){toast(error.message);close();}else $('#dialog-error').textContent=error.message;}finally{if(submit)submit.disabled=false;}
},true);
