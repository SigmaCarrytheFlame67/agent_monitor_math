/* Run sharing, loaded after the console. */
(()=>{
 const side=document.querySelector('.session-side');if(!side)return;const style=document.createElement('style');style.textContent='.session-top .session-side{max-width:100%;flex-wrap:wrap} @media(max-width:1100px){.session-top{display:flex;flex-wrap:wrap}.session-top .session-side{margin-left:auto}}';document.head.append(style);
 const button=document.createElement('button');button.className='sess-btn show';button.id='share-run-btn';button.textContent='Share';button.style.display='none';side.prepend(button);
 // Only the person who started a run publishes it (the operator also owns
 // legacy runs without an owner); guests and project members never see Share.
 const selected=()=>typeof lastSandboxRun!=='undefined'&&typeof runId!=='undefined'&&lastSandboxRun?.run_id===runId?lastSandboxRun:null;
 function syncButton(){const me=typeof CURRENT_USER!=='undefined'?CURRENT_USER:null,run=selected();const mine=!!me&&!me.guest&&!!run&&(run.owner_id==null?!!me.is_admin:run.owner_id===me.id);button.style.display=mine?'':'none';}
 const meta=document.getElementById('run-meta');if(meta)new MutationObserver(syncButton).observe(meta,{childList:true,characterData:true,subtree:true});
 // Re-check as soon as another run is selected, before its record arrives.
 if(typeof selectRun==='function'){const select=selectRun;window.selectRun=function(...args){const result=select.apply(this,args);syncButton();return result;};}
 const dialog=document.createElement('dialog');dialog.style.cssText='width:min(560px,92vw);padding:24px;border:1px solid var(--border);border-radius:12px;background:var(--bg);color:var(--fg);';
 dialog.innerHTML='<h2 style="margin-bottom:12px">Share this run</h2><p id="share-what">Anyone with the link sees this run in the console’s own read-only view: the problem, the proof, the agents’ messages and reasoning summaries, the commands their tools ran (not what the tools returned), the DAG views, and the Lean source with its checker record. Review those texts before sharing. Account settings, prompts, your chat messages, attachments, other workspace files and costs are excluded.</p><p style="margin:12px 0">The snapshot stays fixed until you update it. Revoking prevents future access; it cannot remove copies someone already saved.</p><label style="display:flex;gap:10px;margin:16px 0"><input type="checkbox" id="share-confirm"> I want to share this snapshot.</label><input id="share-url" readonly aria-label="Shareable run link" style="width:100%;padding:10px;margin-bottom:12px"><p id="share-status" role="status" style="margin:8px 0"></p><div style="display:flex;flex-wrap:wrap;gap:8px"><button id="share-create">Create / update snapshot</button><button id="share-copy">Copy link</button><button id="share-open">Preview</button><button id="share-revoke">Revoke link</button><button id="share-close">Close</button></div>';
 document.body.append(dialog);const $=id=>dialog.querySelector('#'+id);let rid='',url='',seq=0;
 function state(d){url=d.path?new URL(d.path,location.origin).href:'';$('share-url').value=url;$('share-copy').disabled=!url;$('share-open').disabled=!url;$('share-revoke').disabled=!url;}
 // Every request belongs to the run the dialog was opened for; a reply that
 // arrives after the dialog moved to another run is dropped.
 async function api(method,body){const target=rid,mine=++seq;const r=await fetch('/api/runs/'+encodeURIComponent(target)+'/share',{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});let d={};try{d=await r.json();}catch{}if(target!==rid||mine!==seq)throw Object.assign(new Error(''),{stale:true});if(!r.ok)throw new Error(d.error||`Sharing failed (HTTP ${r.status}). Try again.`);return d;}
 const report=e=>{if(!e.stale)$('share-status').textContent=e.message;};
 // An analysis shares its report and Manuscript DAG (model-verifier verdicts), never the manuscript.
 const what={proof:$('share-what').textContent,analysis:'Anyone with the link sees this analysis in the console’s own read-only view: the report, the Manuscript DAG with its model-verifier verdicts (not a certificate), the source link and any Lean statements. Review those texts before sharing. The manuscript itself, account settings, chat history, attachments, other workspace files and costs are excluded.'};
 const isAnalysis=id=>{const run=selected();if(run)return run.run_kind==='analyze'||run.engine==='analyze';return typeof RUN_KIND_BY_ID!=='undefined'&&RUN_KIND_BY_ID.get(String(id))==='analyze';};
 button.onclick=async()=>{if(typeof runId==='undefined'||!runId)return;rid=runId;$('share-what').textContent=isAnalysis(rid)?what.analysis:what.proof;state({});$('share-confirm').checked=false;$('share-status').textContent='Loading…';if(!dialog.open)dialog.showModal();try{state(await api('GET'));$('share-status').textContent='';}catch(e){report(e);}};
 $('share-create').onclick=async()=>{if(!$('share-confirm').checked){$('share-status').textContent='Select the checkbox to confirm sharing.';return;}const b=$('share-create');b.disabled=true;try{state(await api('POST',{confirm_share:true}));$('share-status').textContent='Snapshot updated. Copy the link or preview it.';}catch(e){report(e);}finally{b.disabled=false;}};
 $('share-copy').onclick=async()=>{try{await navigator.clipboard.writeText(url);$('share-status').textContent='Link copied.';}catch{$('share-url').focus();$('share-url').select();$('share-status').textContent='Select and copy the link above.';}};
 $('share-open').onclick=()=>window.open(url,'_blank','noopener,noreferrer');
 $('share-revoke').onclick=async()=>{try{state(await api('DELETE'));$('share-status').textContent='Link revoked.';}catch(e){report(e);}};
 $('share-close').onclick=()=>{seq++;dialog.close();};
 syncButton();
})();
