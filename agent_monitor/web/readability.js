/* Readability (run_kind "readability") in the console: the view of a readable
   version. Loaded after the console's own script (#readability-loader) and
   built from its helpers (analyzeEl, escapeHtml, stashMath,
   sanitizeRenderedHtml, renderMathIn, analysisKatexMacros, analysisProse, …):
   model and manuscript text is set as text, or rendered as Markdown with raw
   HTML escaped and the result sanitized — never parsed as HTML.

   Sandbox: the tabs Readable version · Original · Notation · Map under the
   proof toolbar render into #md-view; the right panel asks questions about
   the paper (POST /api/run/<id>/readability/ask; questions and answers come
   back in run.readability.asks, the one in flight in run.readability.ask).
   DAG: the Paper map mode (GET /api/run/<id>/proof_graph?kind=paper-map). */
const READABILITY_TABS=['readable','original','notation','map'];
let readabilityTab='readable', readabilityTabRunId='';
const readabilityMapCache=new Map(), readabilityMapLoads=new Map(), readabilityMapMisses=new Map();
const readabilityOriginalCache=new Map(), readabilityOriginalLoads=new Map();
// The question being written (a selected passage or a result) and the ones
// sent but not yet in the run's record, by run.
// The run whose question is on its way ('' when none).
let readabilityAskContext=null, readabilityAskSending='';
const readabilityAskSent=new Map();

/* ── progress of a running readable version ──
   The runner's own notes ("Readability · 3 Rewrite · part 2 of 5"), never a
   model's raw reply, a model-call marker ("rewrite-2-low") or a log line. */
const READABILITY_CALL_MARKER=/^(?:guide|rewrite-\d+|(?:re)?check-\d+|repair-\d+|ask)(?:-retry)?(?:-low)?$/;
const READABILITY_STAGE_LINE=/^(\d) (Ingest|Guide|Rewrite|Check|Publish)(?: · (.*))?$/;
function readabilityProgressLines(text){
  const out=[];
  String(text||'').split('\n').forEach(raw=>{
    let line=raw.trim();
    // A rewrite's "=== READABLE ===" delimiters are reply text, never a note.
    if(!line||line==='---'||/^```/.test(line)||/^[{}[\]"]/.test(line)||/^\$ /.test(line)||/^===/.test(line))return;
    if(/^(?:◦ session|assistant · |thinking · |stage · |edit · |error · |· usage )/.test(line))return;
    line=line.replace(/^Readability · /,'');
    // The runner's banner and closing line, not "Ansatz Readability could not start: …".
    if(!line||READABILITY_CALL_MARKER.test(line)||/^Ansatz Readability(?: ·| completed\b)/.test(line))return;
    out.push(line.slice(0,600));
  });
  return out.slice(-600);
}
// The lines of the run's progress: the server's (sandbox preview) or, before
// it arrives, the notes among the run's agents.
function readabilityRunProgress(run){
  const box=run?.sandbox_preview;
  if(box&&!box.has_readable&&typeof box.text==='string')return readabilityProgressLines(box.text);
  return readabilityProgressLines((Array.isArray(run?.agents)?run.agents:[]).map(agent=>/\(error\)$/.test(String(agent?.stage_name||''))||/^Readability · /.test(String(agent?.output||''))?String(agent.output||'').split('\n')[0]:'').join('\n'));
}
// One short line for the panel header (the stage name is shown next to it).
function readabilityProgressDetail(lines){
  const line=String(lines[lines.length-1]||''), match=READABILITY_STAGE_LINE.exec(line);
  if(match)return String(match[3]&&match[2]==='Ingest'?ANALYZE_SOURCE_WORDS[match[3]]||match[3]:match[3]||'');
  return line.replace(/^(?:Ingest|Guide|Rewrite|Check|Publish) · /,'');
}
function readabilityProgressView(lines,run){
  const live=run?.status==='running', ended=['failed','interrupted','stopped'].includes(String(run?.status||''));
  const list=analyzeEl('ol','analysis-progress'); list.setAttribute('aria-label','Readability progress');
  lines.forEach((line,index)=>{
    const last=index===lines.length-1, match=READABILITY_STAGE_LINE.exec(line);
    if(match){
      const item=analyzeEl('li','ap-stage'), detail=match[2]==='Ingest'?ANALYZE_SOURCE_WORDS[match[3]]||match[3]:match[3];
      item.append(analyzeEl('b','',match[2]));
      if(detail)item.append(analyzeEl('span','',detail));
      list.append(item); return;
    }
    const plain=line.replace(/^(?:Ingest|Guide|Rewrite|Check|Publish) · /,'');
    list.append(analyzeEl('li',ended&&last&&plain===line?'ap-error':live&&last?'ap-now':'ap-line',plain.charAt(0).toUpperCase()+plain.slice(1)));
  });
  return list;
}

/* ── the run view ── */
// The Sandbox preview of a readable version (applySandboxPreview's branch).
function applyReadabilityPreview(run){
  const box=run?.sandbox_preview||{}, live=run?.status==='running', rid=String(run?.run_id||'');
  if(readabilityTabRunId!==rid){
    // The Ask panel and its notes belong to the run that was shown.
    readabilityTabRunId=rid; readabilityTab='readable'; readabilityAskContext=null; renderReadabilityAskContext();
    for(const id of ['ask-status','readability-tab-status']){ const el=$(id); if(el)el.textContent=''; }
    const input=$('ask-input'); if(input)input.value='';
  }
  // Nothing in a readable version is editable in the Sandbox.
  sandboxEditing=false; sandboxText=''; sandboxRevision=String(box.revision||'missing');
  syncSandboxEditor();
  const stage=live?analysisStageStates(run,READABILITY_STAGES).find(item=>item.state==='running'):null;
  $('proof-label').textContent=box.has_readable?'readability/readable.md':live?(stage?'running · '+stage.label:'starting…'):String(box.text||'').trim()?'progress (not published)':'not published';
  syncReadabilitySource(run);
  if(box.has_readable&&rid)setDownloadUrl('/api/workspace/'+encodeURIComponent(rid)+'/file?path='+encodeURIComponent('readability/readable.md')+'&download=1','readable.md');
  syncReadabilityTabs();
  renderReadabilityTab(run);
  renderReadabilityAsks(run);
  // The paper map names the results the readable version marks.
  if(box.has_readable&&rid&&!readabilityMapCache.has(rid))ensurePaperMap(rid).then(payload=>{
    if(!payload||runId!==rid||lastSandboxRun?.run_id!==rid)return;
    $('md-view')._proofRenderKey=''; renderReadabilityTab(lastSandboxRun); renderReadabilityAsks(lastSandboxRun);
  });
  return !!(box.has_readable||String(box.text||'').trim());
}
// Source ↗ opens the manuscript where it was published; the Original tab
// replaces the stored-copy toggle.
function syncReadabilitySource(run){
  const link=$('analysis-source-link'), toggle=$('analysis-source-toggle'); if(!link||!toggle)return;
  const url=String(run?.sandbox_preview?.source_url||'');
  link.hidden=!/^https:\/\/\S+$/.test(url);
  if(!link.hidden){ link.href=url; link.title='Open the manuscript where it was published: '+url; }
  toggle.hidden=true;
}
function syncReadabilityTabs(){
  document.querySelectorAll('#readability-tabs [role="tab"]').forEach(tab=>{
    const on=tab.dataset.rdView===readabilityTab;
    tab.setAttribute('aria-selected',String(on)); tab.tabIndex=on?0:-1;
  });
  const panel=$('md-view');
  if(panel?.getAttribute('role')==='tabpanel')panel.setAttribute('aria-labelledby','rd-tab-'+readabilityTab);
}
function setReadabilityTab(view,{focus=false}={}){
  if(!READABILITY_TABS.includes(view))return;
  readabilityTab=view;
  syncReadabilityTabs();
  if(focus)document.querySelector('#readability-tabs [aria-selected="true"]')?.focus();
  $('readability-tab-status').textContent='';
  $('md-view')._proofRenderKey='';
  // A failed load of the original text is tried again when its tab is chosen.
  if(view==='original'&&readabilityOriginalCache.get(runId)?.content===null)readabilityOriginalCache.delete(runId);
  const run=lastSandboxRun?.run_id===runId?lastSandboxRun:null;
  if(run&&isReadabilityRun(run))renderReadabilityTab(run);
}
function renderReadabilityTab(run){
  const view=$('md-view');
  $('pdf-pages').style.display='none';
  setProofEmpty(false); view.style.display='block';
  if(readabilityTab==='original')return renderReadabilityOriginal(run,view);
  if(readabilityTab==='notation')return renderReadabilityNotation(run,view);
  if(readabilityTab==='map')return renderReadabilityMap(run,view);
  return renderReadableVersion(run,view);
}
function readabilityMacros(run){
  const map=readabilityMapCache.get(String(run?.run_id||''));
  return analysisKatexMacros({macros:run?.sandbox_preview?.macros||map?.macros||map?.graph?.macros});
}
// Printed labels ("Lemma 3.2") of the results, by id, from the paper map.
function readabilityLabels(rid){
  const labels=new Map();
  paperMapRefLabels(readabilityMapCache.get(String(rid||''))?.graph).forEach((value,id)=>labels.set(id,value.name));
  return labels;
}

/* ── Readable version ── */
let readableMarkdownParser=null;
const READABLE_MARKER_LINE=/^[ \t]{0,3}\[result:([^\]\s]{1,80})\]:[ \t]*#[ \t]*$/gm;
const READABLE_RESULT_LINK=/\[\[\s*result\s*:\s*([^\]\s]{1,80})\s*\]\]/gi;
/* Readable text (and answers) are Markdown with TeX, written by a model from
   someone else's manuscript: raw HTML is shown as text and the rendered HTML
   is sanitized like any run output, last. The runner's result markers (a line
   "[result:<id>]: #", which Markdown renders as nothing) become anchor badges
   and an answer's "[[result:<id>]]" a button that jumps to that anchor. Each
   crosses Markdown and the sanitizer as a token of plain text that no input
   can contain (it carries a fresh nonce), and placeReadableMarks builds its
   element in place of the token: in text nodes only, never from markup. */
function readableMarkdown(text){
  const nonce=Math.random().toString(36).slice(2,10).padEnd(8,'0'), marks=[];
  const tokenFor=mark=>{ marks.push(mark); return '@@RD'+nonce+'x'+(marks.length-1)+'@@'; };
  const source=String(text||'').replace(/\r\n?/g,'\n')
    .replace(READABLE_MARKER_LINE,(raw,id)=>'\n'+tokenFor({kind:'anchor',id:id.toLowerCase(),raw})+'\n');
  // Math is set aside first: a "[[result:<id>]]" inside TeX stays TeX.  The
  // display environments go first, as the shared page holds them: Markdown
  // read inside one would drop its \{ and \\ and could make a heading of it.
  const environments=[];
  let held='', at=0;
  for(const [start,end] of mathSpans(source,['equation','align','alignat','gather'].map(name=>mathEnvironment(source,name)))){
    held+=source.slice(at,start)+'@@RDENV'+nonce+'x'+environments.length+'@@'; environments.push(source.slice(start,end)); at=end;
  }
  const stashed=stashMath(held+source.slice(at));
  const markdown=stashed.s.replace(READABLE_RESULT_LINK,(raw,id)=>tokenFor({kind:'ref',id:id.toLowerCase(),raw}));
  let html=null;
  try{
    const md=window.marked;
    if(md&&!readableMarkdownParser&&typeof md.Marked==='function'){
      const parser=new md.Marked({gfm:true,breaks:false});
      parser.use({renderer:{html(token){ return escapeHtml(typeof token==='string'?token:String(token?.raw??token?.text??'')); }}});
      readableMarkdownParser=parser;
    }
    if(readableMarkdownParser)html=readableMarkdownParser.parse(markdown);
  }catch(e){ html=null; }
  if(typeof html!=='string'||!html)return {html:'<pre>'+escapeHtml(text)+'</pre>',marks:[],nonce};
  const restored=html.replace(/@@MATH(\d+)@@/g,(_,i)=>escapeHtml(stashed.slots[Number(i)]||''))
    .replace(new RegExp('@@RDENV'+nonce+'x(\\d+)@@','g'),(_,i)=>escapeHtml(environments[Number(i)]||''));
  return {html:sanitizeRenderedHtml(restored),marks,nonce};
}
// Readable Markdown in `container`, with its anchor badges and result links.
function renderReadableMarkdown(container,text,labels){
  const {html,marks,nonce}=readableMarkdown(text);
  container.innerHTML=html;
  placeReadableMarks(container,marks,nonce,labels);
}
function readableTextNodes(node,out=[]){
  node.childNodes.forEach(child=>{ if(child.nodeType===3)out.push(child); else readableTextNodes(child,out); });
  return out;
}
// Each token's element in its place. A token in code reads as it was
// written, in a link's text as the result's name (no button in a link), and
// in a link's address or title as it was written.
function placeReadableMarks(root,marks,nonce,labels){
  if(!marks.length)return;
  const pattern=new RegExp('@@RD'+nonce+'x(\\d+)@@','g'), name=id=>labels instanceof Map&&labels.get(id)||id;
  readableTextNodes(root).filter(node=>node.nodeValue.includes('@@RD'+nonce)).forEach(node=>{
    const text=node.nodeValue, parent=node.parentElement, literal=!!parent?.closest('code,pre'), inLink=!!parent?.closest('a'), pieces=[];
    let at=0;
    for(const match of text.matchAll(pattern)){
      const mark=marks[Number(match[1])];
      if(!mark)continue;
      const before=text.slice(at,match.index);
      if(before)pieces.push(before);
      at=match.index+match[0].length;
      // "Lemma 3.9 [[result:lem]]": the name is written already, the link is a jump.
      const named=mark.kind==='ref'&&before.trimEnd().toLowerCase().endsWith(String(name(mark.id)).toLowerCase());
      pieces.push(literal?mark.raw:inLink?name(mark.id):mark.kind==='anchor'?readableAnchorNode(mark.id,name(mark.id)):readableRefNode(mark.id,labels,named));
    }
    if(at<text.length)pieces.push(text.slice(at));
    node.replaceWith(...pieces);
  });
  // A marker line is a row of its own.
  root.querySelectorAll('.rd-anchor').forEach(anchor=>{
    const row=anchor.parentElement;
    if(row?.tagName==='P'&&row.childNodes.length===1)row.classList.add('rd-anchor-row');
  });
  root.querySelectorAll('a').forEach(link=>['href','title'].forEach(attribute=>{
    const value=link.getAttribute(attribute);
    if(value?.includes('@@RD'+nonce))link.setAttribute(attribute,value.replace(pattern,(match,index)=>marks[Number(index)]?.raw??''));
  }));
}
// The result's id goes into a data attribute only, never into a DOM id.
function readableAnchorNode(id,label){
  const badge=analyzeEl('span','rd-anchor',label);
  badge.dataset.rdAnchor=id; badge.tabIndex=-1; badge.title=label===id?id:label+' · '+id;
  return badge;
}
// A result the paper map does not list stays plain text; right after its
// own name the link is a compact jump.
function readableRefNode(id,labels,named=false){
  const label=labels instanceof Map&&labels.get(id)||id;
  if(labels instanceof Map&&labels.size&&!labels.has(id))return named?'':label;
  const button=analyzeEl('button',named?'rd-ref rd-ref-jump':'rd-ref',named?'↗':label);
  button.type='button'; button.dataset.rdRef=id; button.title='Show '+label+' in the readable version';
  if(named)button.setAttribute('aria-label','Show '+label+' in the readable version');
  return button;
}
// Part counts ({parts, faithful, issues, unchecked, …}): the preview's, else the run's summary.
function readabilityCounts(run){
  const summary=run?.readability?.summary&&typeof run.readability.summary==='object'?run.readability.summary:{};
  const parts=run?.sandbox_preview?.parts&&typeof run.sandbox_preview.parts==='object'?run.sandbox_preview.parts:{};
  const count=key=>Number(parts[key]??summary[key])||0;
  return {total:count('parts'),faithful:count('faithful'),issues:count('issues'),unchecked:count('unchecked')};
}
function readabilityProvenanceText(run){
  const counts=readabilityCounts(run);
  const checked=counts.total?` An independent model call compared each part with the original: ${counts.faithful} of ${counts.total} parts faithful, ${counts.issues} with issues (marked ⚠), ${counts.unchecked} unchecked.`:'';
  return 'Rewritten by a model to be easier to follow; every statement and argument is meant to be kept.'+checked+' Not a certificate of correctness.';
}
function renderReadableVersion(run,view){
  const box=run?.sandbox_preview||{}, rid=String(run?.run_id||''), text=String(box.text||'');
  const body=$('proof-body');
  if(!box.has_readable){
    const lines=readabilityRunProgress(run), live=run?.status==='running';
    const key=JSON.stringify(['rd-progress',rid,lines,String(run?.status||'')]);
    if(view._proofRenderKey===key)return;
    view._proofRenderKey=key;
    const stick=body.scrollHeight-body.scrollTop-body.clientHeight<80;
    const doc=analyzeEl('div','md-doc analysis-doc live');
    doc.append(analyzeEl('p','analysis-live-note',live?'The readable version appears here when it is published. Progress so far:':'This run published no readable version. What it recorded:'));
    if(lines.length)doc.append(readabilityProgressView(lines,run));
    else doc.append(analyzeEl('p','analysis-live-foot',live?'Starting…':'No progress was recorded.'));
    view.replaceChildren(doc);
    renderMathIn(doc,readabilityMacros(run));
    if(stick&&live)body.scrollTop=body.scrollHeight;
    return;
  }
  const labels=readabilityLabels(rid), macros=readabilityMacros(run), notes=readabilityCheckNotes(run);
  const key=JSON.stringify(['rd-readable',rid,text,macros,labels.size,readabilityCounts(run),[...notes]]);
  if(view._proofRenderKey===key)return;
  view._proofRenderKey=key;
  const previousTop=body.scrollTop;
  const doc=analyzeEl('div','md-doc analysis-doc rd-doc');
  // Each part is drawn on its own, so that its check notes can follow it.
  readableParts(text).forEach(part=>{
    if(part.index===null){ if(part.text.trim())renderReadableMarkdown(doc,part.text,labels); return; }
    const box=analyzeEl('section','rd-part'); box.dataset.rdPart=String(part.index);
    if(part.text.trim())renderReadableMarkdown(box,part.text,labels);
    if(notes.has(part.index))box.append(readabilityCheckNotesBox(notes.get(part.index)));
    doc.append(box);
  });
  markReadableBlocks(doc);
  // The document's own provenance note is its banner; a document without one gets the console's.
  if(!doc.querySelector('.rd-provenance'))doc.prepend(analyzeEl('p','rd-banner',readabilityProvenanceText(run)));
  doc.querySelectorAll('a[href]').forEach(link=>{ link.target='_blank'; link.rel='noopener noreferrer'; });
  view.replaceChildren(doc);
  renderMathIn(doc,macros);
  body.scrollTop=previousTop;
}
// The provenance note under the title, "⚠ Check" notes of parts with
// unresolved issues, and the rewrite's "Reader's note" gaps.
function markReadableBlocks(doc){
  const title=doc.firstElementChild, note=title?.nextElementSibling;
  if(title?.tagName==='H1'&&note?.tagName==='BLOCKQUOTE')note.classList.add('rd-provenance');
  doc.querySelectorAll('blockquote').forEach(quote=>{
    const text=String(quote.textContent||'').trim();
    if(/^⚠/.test(text)){ quote.classList.add('rd-check'); quote.setAttribute('role','note'); }
    else if(/^(?:Reader['’]s note|Not checked|Not rewritten)\b/i.test(text))quote.classList.add('rd-note');
  });
}
// readable.md split at the runner's "[part:<n>]: #" marks (a rewrite cannot
// write one, and the original text it repeats has them indented): the text
// before the first part (index null), then each part with its number.
const READABLE_PART_LINE=/^\[part:(\d{1,4})\]: #$/gm;
function readableParts(text){
  const source=String(text||'').replace(/\r\n?/g,'\n'), parts=[];
  let at=0, index=null;
  for(const match of source.matchAll(READABLE_PART_LINE)){
    parts.push({index,text:source.slice(at,match.index)});
    index=Number(match[1]); at=match.index+match[0].length;
  }
  parts.push({index,text:source.slice(at)});
  return parts;
}
// The minor points the independent check raised about each part, by part
// number (a part's major problems are its "⚠ Check" note in the text), with
// the check's suggested fix when the record has one.
function readabilityCheckNotes(run){
  const notes=new Map(), parts=run?.sandbox_preview?.check;
  (Array.isArray(parts)?parts:[]).slice(0,400).forEach(part=>{
    if(!part||typeof part!=='object'||!Number.isInteger(part.index))return;
    const minor=(Array.isArray(part.issues)?part.issues:[])
      .filter(issue=>issue&&typeof issue==='object'&&issue.severity==='minor'&&dagDetailText(issue.problem,600).trim()).slice(0,12);
    if(minor.length)notes.set(part.index,minor.map(issue=>({location:dagDetailText(issue.location,200).trim(),
      problem:dagDetailText(issue.problem,600).trim(),fix:dagDetailText(issue.fix,600).trim()})));
  });
  return notes;
}
// A part's check notes, collapsed under it; the checker's words are text.
function readabilityCheckNotesBox(notes){
  const box=analyzeEl('details','rd-check-notes'), list=analyzeEl('ul');
  box.append(analyzeEl('summary','','Check notes ('+notes.length+')'));
  notes.forEach(note=>{
    const item=analyzeEl('li');
    if(note.location&&note.location!=='unspecified')item.append(analyzeEl('span','rd-check-where',note.location+': '));
    item.append(note.problem);
    if(note.fix)item.append(analyzeEl('span','rd-check-fix',' Suggested fix: '+note.fix));
    list.append(item);
  });
  box.append(list,analyzeEl('p','rd-check-foot','Points the independent model check raised when it compared this part with the original, judged not to change the meaning — a model check, not a certificate.'));
  return box;
}

/* ── Original ── */
function readabilityPdfText(run){
  const source=run?.readability?.source&&typeof run.readability.source==='object'?run.readability.source:{};
  return source.math_fidelity==='pdf-text'||run?.sandbox_preview?.math_fidelity==='pdf-text';
}
// The reading-order text as numbered lines, as text (never markup).
function readabilityNumberedLines(content){
  const lines=String(content||'').split(/\r\n?|\n/).slice(0,20000), pre=analyzeEl('pre','analysis-lines');
  lines.forEach((line,index)=>{ const row=analyzeEl('span','ln-row'); row.append(analyzeEl('span','ln',String(index+1)),document.createTextNode(line+'\n')); pre.append(row); });
  return pre;
}
function renderReadabilityOriginal(run,view){
  const rid=String(run?.run_id||''), box=run?.sandbox_preview||{}, cached=readabilityOriginalCache.get(rid);
  const key=JSON.stringify(['rd-original',rid,!!box.original_available,cached?String(cached.content??'').length:-1]);
  if(view._proofRenderKey===key)return;
  view._proofRenderKey=key;
  const doc=analyzeEl('div','md-doc analysis-doc rd-original');
  if(!box.original_available&&!cached){
    doc.append(analyzeEl('p','analysis-live-note',run?.status==='running'?'The original text appears here once the manuscript has been read.':'No original text was stored for this run.'));
    return view.replaceChildren(doc);
  }
  if(!cached){
    doc.append(analyzeEl('p','analysis-live-note','Loading the original text…'));
    view.replaceChildren(doc);
    return loadReadabilityOriginal(rid);
  }
  doc.append(analyzeEl('p','analysis-live-note','The original in reading order (readability/original.txt) · read-only · never compiled or run.'));
  if(readabilityPdfText(run))doc.append(analyzeEl('p','rd-pdf-note','This text was extracted from a PDF: formulas may be garbled.'));
  if(cached.content===null)doc.append(analyzeEl('p','','The original text could not be loaded.'));
  else{
    if(cached.content.length>=400000)doc.append(analyzeEl('p','rd-pdf-note','Only the first 400,000 characters are shown.'));
    doc.append(readabilityNumberedLines(cached.content));
  }
  view.replaceChildren(doc);
}
function loadReadabilityOriginal(rid){
  if(!rid||readabilityOriginalLoads.has(rid))return;
  readabilityOriginalLoads.set(rid,fetch('/api/workspace/'+encodeURIComponent(rid)+'/file?path='+encodeURIComponent('readability/original.txt'),{cache:'no-store'})
    .then(response=>response.ok?response.json():null).then(data=>typeof data?.content==='string'?data.content:null).catch(()=>null)
    .then(content=>{
      readabilityOriginalCache.set(rid,{content});
      if(readabilityOriginalCache.size>3)readabilityOriginalCache.delete(readabilityOriginalCache.keys().next().value);
      readabilityOriginalLoads.delete(rid);
      if(runId===rid&&readabilityTab==='original'&&lastSandboxRun?.run_id===rid){ $('md-view')._proofRenderKey=''; renderReadabilityTab(lastSandboxRun); }
    }));
}

/* ── Notation ── */
// The preview's notation: {rows, terms} (or the rows alone).
function readabilityNotation(run){
  const box=run?.sandbox_preview||{}, value=box.notation;
  const rows=(Array.isArray(value)?value:Array.isArray(value?.rows)?value.rows:[]).filter(row=>row&&typeof row==='object').slice(0,300);
  const terms=(Array.isArray(value?.terms)?value.terms:Array.isArray(box.terms)?box.terms:[]).filter(term=>term&&typeof term==='object').slice(0,200);
  return {rows,terms};
}
// A symbol in TeX, typeset by KaTeX (no \href, \url or HTML extensions without
// trust), or shown as text.
function readabilityTex(el,tex,macros){
  el.textContent=tex;
  if(!tex||typeof window.katex?.render!=='function')return;
  try{ window.katex.render(katexReferenceText(tex),el,{throwOnError:false,displayMode:false,trust:false,macros:withKatexFallbacks(macros,tex),maxExpand:200}); }catch(e){ el.textContent=tex; }
}
function readabilityTable(headings){
  const table=analyzeEl('table','rd-table'), head=analyzeEl('thead'), row=analyzeEl('tr'), body=analyzeEl('tbody');
  headings.forEach(text=>{ const cell=analyzeEl('th','',text); cell.setAttribute('scope','col'); row.append(cell); });
  head.append(row); table.append(head,body);
  return {table,body};
}
// Where a symbol is introduced, with a jump to that result when the guide names it.
function readabilityWhereCell(entry){
  const cell=analyzeEl('td','',dagDetailText(entry.where,200));
  const id=typeof entry.id==='string'?entry.id.trim():'';
  if(id){
    const jump=analyzeEl('button','rd-ref','Read'); jump.type='button'; jump.dataset.rdRef=id; jump.title='Show this result in the readable version';
    cell.append(' ',jump);
  }
  return cell;
}
function renderReadabilityNotation(run,view){
  const {rows,terms}=readabilityNotation(run), macros=readabilityMacros(run);
  const key=JSON.stringify(['rd-notation',String(run?.run_id||''),rows,terms,macros]);
  if(view._proofRenderKey===key)return;
  view._proofRenderKey=key;
  const doc=analyzeEl('div','md-doc analysis-doc rd-notation');
  if(!rows.length&&!terms.length){
    doc.append(analyzeEl('p','analysis-live-note',run?.status==='running'?'The notation table appears when the readable version is published.':'No notation table was published for this run.'));
    return view.replaceChildren(doc);
  }
  if(rows.length){
    const {table,body}=readabilityTable(['Symbol','Meaning','Where introduced']);
    rows.forEach(row=>{
      const line=analyzeEl('tr'), symbol=analyzeEl('td','rd-symbol'), meaning=analyzeEl('td','',dagDetailText(row.meaning,600));
      readabilityTex(symbol,dagDetailText(row.symbol,200),macros);
      line.append(symbol,meaning,readabilityWhereCell(row)); body.append(line);
    });
    doc.append(analyzeEl('h2','','Notation'),table);
  }
  if(terms.length){
    const {table,body}=readabilityTable(['Term','Meaning','Where introduced']);
    terms.forEach(term=>{
      const line=analyzeEl('tr');
      line.append(analyzeEl('td','rd-term',dagDetailText(term.term,200)),analyzeEl('td','',dagDetailText(term.meaning,600)),readabilityWhereCell(term));
      body.append(line);
    });
    doc.append(analyzeEl('h2','','Terms'),table);
  }
  doc.append(analyzeEl('p','analysis-live-foot','Collected by a model from the manuscript — check a meaning against the original when it matters.'));
  view.replaceChildren(doc);
  // Meanings may use inline math; the symbols are typeset already.
  doc.querySelectorAll('td:not(.rd-symbol)').forEach(cell=>renderMathIn(cell,macros));
}

/* ── Map (a list) and the DAG's Paper map ── */
function rememberPaperMap(rid,payload){
  if(!rid||payload?.kind!=='paper-map')return;
  readabilityMapCache.set(String(rid),payload);
  if(readabilityMapCache.size>8)readabilityMapCache.delete(readabilityMapCache.keys().next().value);
}
function ensurePaperMap(rid){
  rid=String(rid||'');
  if(!rid||readabilityMapCache.has(rid))return Promise.resolve(readabilityMapCache.get(rid)||null);
  // A map that is not there yet is asked for again at most every 30 s.
  if(Date.now()-(readabilityMapMisses.get(rid)||0)<30000&&!readabilityMapLoads.has(rid))return Promise.resolve(null);
  if(!readabilityMapLoads.has(rid))readabilityMapLoads.set(rid,fetch('/api/run/'+encodeURIComponent(rid)+'/proof_graph?kind=paper-map')
    .then(response=>response.ok?response.json():null).then(payload=>{
      if(payload?.status==='ready')rememberPaperMap(rid,payload); else readabilityMapMisses.set(rid,Date.now());
      return readabilityMapCache.get(rid)||null;
    })
    .catch(()=>{ readabilityMapMisses.set(rid,Date.now()); return null; }).finally(()=>readabilityMapLoads.delete(rid)));
  return readabilityMapLoads.get(rid);
}
// Importance 1 (routine) to 5 (central to the main argument).
function paperMapImportance(node){
  const value=Math.round(Number(node?.importance));
  return value>=1&&value<=5?value:1;
}
function paperMapCardTitle(node){
  const label=dagDetailText(node?.label||node?.id,160).trim(), title=dagDetailText(node?.title,200).trim();
  return analysisTextAccents(dagMathSafeClip(title&&title!==label?label+' · '+title:label,120));
}
// A section as one line: "3 Main estimates" (a structured section names its
// outermost level).
function paperMapSectionLabel(section){
  if(Array.isArray(section)){
    const top=section.find(item=>item&&typeof item==='object');
    return top?[top.number,top.title].map(value=>dagDetailText(value,120).trim()).filter(Boolean).join(' '):'';
  }
  return dagDetailText(section,160).trim();
}
// \ref{<id>} in a statement reads as the result's number (its name for \cref);
// an unnumbered result reads as its title, "Definition (Valid diagrams)", as
// in the readable version.
function paperMapRefLabels(graph){
  const labels=new Map();
  (Array.isArray(graph?.nodes)?graph.nodes:[]).forEach(node=>{
    const id=String(node?.id||''), label=dagDetailText(node?.label,160).trim(), title=dagDetailText(node?.title,120).trim();
    if(!id||!label||labels.has(id))return;
    const number=/(\d+(?:\.\d+)*[a-z]?)$/i.exec(label)?.[1];
    const titled=!number&&title&&title!==label?'('+title+')':'';
    labels.set(id,{number:number||titled||label,name:titled?label+' '+titled:label});
  });
  return labels;
}
const PAPER_MAP_BOXED_KINDS=new Set(['definition','notation','convention','setting','assumption','hypothesis','axiom']);
// Definitions are boxes, results rounded; the darker a card, the more the main argument needs it.
function paperMapNodeVisual(n,out){
  const importance=paperMapImportance(n), kind=String(n?.kind||'result').replace(/_/g,' ').slice(0,40);
  out.badge=kind; out.color='#64748b';
  out.classes.push('paper-map','imp-'+importance,PAPER_MAP_BOXED_KINDS.has(kind.toLowerCase())?'pm-definition':'pm-result');
  out.status={label:'importance '+importance,tone:'pm'};
  if(n?.main===true){ out.classes.push('pm-main'); out.chips.push({text:'main result',color:'var(--accent)',title:'A main result of the manuscript'}); }
  return out;
}
// A use in a proof is drawn solid, a use in a statement dashed.
const PAPER_MAP_EDGE_BASIS={ref:'a reference in the manuscript',inferred:'inferred from the text',model:'proposed by the reading guide'};
function paperMapEdge(edge){
  const statement=edge?.kind==='statement', basis=dagLookup(PAPER_MAP_EDGE_BASIS,edge?.provenance)||PAPER_MAP_EDGE_BASIS.ref;
  return {cls:(statement?'e-statement':'e-proof')+(edge?.provenance==='model'?' e-model':''),title:(statement?'Used in the statement':'Used in the proof')+' · '+basis};
}
function paperMapLegendHtml(){
  const item=(cls,label)=>`<span class="lg"><span class="${cls}"></span>${escapeHtml(label)}</span>`;
  const sep='<span class="sep" aria-hidden="true"></span>';
  return item('sw pm-sw imp-1','routine')+item('sw pm-sw imp-3','supporting')+item('sw pm-sw imp-5','central')
    +sep+item('sw pm-box','definition')+item('sw pm-round','result')
    +sep+item('ln pm-ln','used in a proof')+item('ln pm-ln dash','used in a statement')
    +sep+'<span class="lg note">Arrows point from a result to the results that use it · importance from a reading guide written by a model — not a verification</span>';
}
// "3 statements · 1 dependency": how big a paper map is.
function paperMapSizeText(nodes,edges){
  return `${nodes} statement${nodes===1?'':'s'} · ${edges} ${edges===1?'dependency':'dependencies'}`;
}
function paperMapStatusText(d){
  const graph=d?.graph||{}, title=dagDetailText(graph.title||d?.title,120).trim();
  return ['Paper map',title,paperMapSizeText((graph.nodes||[]).length,(graph.edges||[]).length)].filter(Boolean).join(' · ');
}
function paperMapActions(node){
  const row=analyzeEl('div','analysis-action-row');
  const read=analyzeEl('button','','Read in readable version'); read.type='button';
  read.onclick=()=>readabilityJumpTo(String(node.id));
  // Asking is a change only the run's owner may make.
  const ask=analyzeEl('button','run-change','Ask about this'); ask.type='button';
  ask.onclick=()=>askAboutNode(node);
  row.append(read,ask);
  return row;
}
function renderPaperMapDetail(node,context){
  const panel=$('dag-detail'); if(!panel)return;
  const view=context.view, graph=context.graph||{}, importance=paperMapImportance(node);
  const head=analyzeEl('div','dag-detail-head'), titleBox=analyzeEl('div');
  titleBox.style.minWidth='0';
  const badge=analyzeEl('span','kd',String(node.kind||'result').replace(/_/g,' ').slice(0,40)); badge.style.setProperty('--kc','#64748b');
  titleBox.append(badge,analyzeEl('h4','',paperMapCardTitle(node)));
  const close=analyzeEl('button','','×'); close.type='button'; close.setAttribute('aria-label','Close result details'); close.onclick=closeDagDetail;
  head.append(titleBox,close);
  const body=analyzeEl('div','dag-detail-body');
  const facts=analyzeEl('dl'), add=(label,value)=>{ const text=dagDetailText(value,500); if(text.trim())facts.append(analyzeEl('dt','',label),analyzeEl('dd','',text)); };
  add('Importance',importance+' of 5'+(node.main===true?' · a main result':''));
  add('Section',paperMapSectionLabel(node.section));
  if(facts.childElementCount){ const box=analyzeEl('section'); box.append(facts); body.append(box); }
  const section=(title,text)=>{
    if(!text.trim())return;
    const box=analyzeEl('section'), content=analyzeEl('div','txt',text);
    box.append(analyzeEl('h5','',title),content); body.append(box);
    renderMathIn(content,view.macros);
  };
  section('Role in the argument',analysisModelProse(dagDetailText(node.role,600),view.refLabels));
  section('Statement',analysisProse(dagDetailText(node.statement,1200),view.refLabels));
  const links=[];
  (graph.edges||[]).forEach(edge=>{
    const outgoing=String(edge?.from)===String(node.id), incoming=String(edge?.to)===String(node.id);
    if(!outgoing&&!incoming)return;
    const other=(graph.nodes||[]).find(item=>String(item.id)===String(outgoing?edge.to:edge.from));
    if(other)links.push({rel:(outgoing?'used by':'uses')+' · in the '+(edge.kind==='statement'?'statement':'proof'),other});
  });
  if(links.length){
    const box=analyzeEl('section'), list=analyzeEl('ul','links');
    links.slice(0,40).forEach(link=>{
      const row=analyzeEl('li'), jump=analyzeEl('button','',paperMapCardTitle(link.other));
      jump.type='button'; jump.onclick=()=>selectDagNode(link.other);
      row.append(analyzeEl('span','rel',link.rel),jump); list.append(row);
    });
    box.append(analyzeEl('h5','','Links'),list); body.append(box);
  }
  const actions=analyzeEl('section','analysis-actions');
  actions.append(analyzeEl('h5','','Actions'),paperMapActions(node));
  body.append(actions);
  body.append(analyzeEl('div','honest','From the manuscript’s references and a reading guide written by a model — not a verification of the result.'));
  panel.replaceChildren(head,body);
  panel.hidden=false;
}
function readabilityMapRow(node){
  const importance=paperMapImportance(node), label=paperMapCardTitle(node);
  const row=analyzeEl('li','rd-map-row imp-'+importance);
  const dots=analyzeEl('span','rd-imp','●'.repeat(importance)+'○'.repeat(5-importance));
  dots.setAttribute('role','img'); dots.setAttribute('aria-label','Importance '+importance+' of 5'); dots.title='Importance '+importance+' of 5';
  const main=analyzeEl('div','rd-map-main'), title=analyzeEl('div','rd-map-label',label);
  if(node.main===true)title.append(' ',analyzeEl('span','rd-main-tag','main result'));
  main.append(title);
  const role=dagDetailText(node.role,400).trim();
  if(role)main.append(analyzeEl('p','rd-map-role',analysisModelProse(role,null)));
  const actions=analyzeEl('div','rd-map-actions');
  const read=analyzeEl('button','','Read'); read.type='button'; read.title='Show '+label+' in the readable version';
  read.onclick=()=>readabilityJumpTo(String(node.id));
  const ask=analyzeEl('button','run-change','Ask'); ask.type='button'; ask.title='Ask about '+label;
  ask.onclick=()=>askAboutNode(node);
  actions.append(read,ask);
  row.append(dots,main,actions);
  return row;
}
function renderReadabilityMap(run,view){
  const rid=String(run?.run_id||''), payload=readabilityMapCache.get(rid), graph=payload?.graph;
  const key=JSON.stringify(['rd-map',rid,!!graph,String(payload?.generated_at||''),(graph?.nodes||[]).length,(graph?.edges||[]).length]);
  if(view._proofRenderKey===key)return;
  view._proofRenderKey=key;
  const doc=analyzeEl('div','md-doc analysis-doc rd-map');
  if(!graph){
    const published=!!run?.sandbox_preview?.has_readable;
    doc.append(analyzeEl('p','analysis-live-note',published?'Loading the paper map…':'The paper map appears when the readable version is published.'));
    view.replaceChildren(doc);
    if(published)ensurePaperMap(rid).then(found=>{
      if(found&&runId===rid&&readabilityTab==='map'&&lastSandboxRun?.run_id===rid){ view._proofRenderKey=''; renderReadabilityTab(lastSandboxRun); }
      else if(!found&&runId===rid&&readabilityTab==='map'){ view._proofRenderKey=''; doc.replaceChildren(analyzeEl('p','analysis-live-note','The paper map could not be loaded.')); }
    });
    return;
  }
  const nodes=(Array.isArray(graph.nodes)?graph.nodes:[]).filter(node=>node&&typeof node==='object').slice(0,400);
  if(!nodes.length){
    doc.append(analyzeEl('p','analysis-live-note','This manuscript has no numbered definitions or results to map.'));
    view.replaceChildren(doc);
    return;
  }
  const head=analyzeEl('div','rd-map-head');
  head.append(analyzeEl('p','analysis-live-note',paperMapSizeText(nodes.length,(graph.edges||[]).length)+' · more dots, more central to the main argument.'));
  const open=analyzeEl('button','rd-map-graph','Open as a graph'); open.type='button';
  open.onclick=()=>{ if(dagMode!=='paper-map')setDagMode('paper-map',{refresh:false}); setWorkspaceView('dag'); };
  head.append(open); doc.append(head);
  const groups=new Map();
  nodes.forEach(node=>{ const section=paperMapSectionLabel(node.section)||'Other results'; if(!groups.has(section))groups.set(section,[]); groups.get(section).push(node); });
  groups.forEach((items,section)=>{
    const box=analyzeEl('section','rd-map-group'), list=analyzeEl('ul','rd-map-list');
    items.forEach(node=>list.append(readabilityMapRow(node)));
    box.append(analyzeEl('h3','',analysisTextAccents(section)),list); doc.append(box);
  });
  doc.append(analyzeEl('p','analysis-live-foot','Importance and roles come from a reading guide written by a model; dependencies from the manuscript’s references and that guide — not a verification.'));
  view.replaceChildren(doc);
  renderMathIn(doc,readabilityMacros(run));
}
// Shows a result's anchor in the Readable version.
function readabilityJumpTo(nodeId){
  const id=String(nodeId||''); if(!id)return;
  if(workspaceView!=='sandbox')setWorkspaceView('sandbox');
  if(readabilityTab!=='readable')setReadabilityTab('readable');
  const anchor=[...document.querySelectorAll('#md-view .rd-anchor')].find(el=>el.dataset.rdAnchor===id);
  const status=$('readability-tab-status');
  if(!anchor){ if(status)status.textContent=(readabilityLabels(runId).get(id)||id)+' is not marked in the readable version.'; return; }
  if(status)status.textContent='';
  anchor.scrollIntoView({block:'center',behavior:'smooth'});
  anchor.focus({preventScroll:true});
  anchor.classList.remove('flash'); void anchor.offsetWidth; anchor.classList.add('flash');
}

/* ── Ask about this paper ── */
function readabilityAskRunning(item){ return ['running','queued'].includes(String(item?.status||'')); }
// The run's questions, oldest first; the one in flight, and one just sent
// that the record does not list yet, come last.
function readabilityAsks(run){
  const rid=String(run?.run_id||''), block=run?.readability&&typeof run.readability==='object'?run.readability:{};
  const asks=(Array.isArray(block.asks)?block.asks:[]).filter(item=>item&&typeof item==='object').slice(-50);
  const listed=new Set(asks.map(item=>String(item.ask_id||'')).filter(Boolean));
  const current=block.ask&&typeof block.ask==='object'&&readabilityAskRunning(block.ask)?block.ask:null;
  if(current&&!listed.has(String(current.ask_id||''))){ asks.push(current); listed.add(String(current.ask_id||'')); }
  const sent=readabilityAskSent.get(rid);
  // Once the record shows a question in flight (one at a time) or lists it,
  // or after a while without, the record speaks for it.
  if(sent&&(current||listed.has(sent.ask_id)||Date.now()-sent.sent_at>20000))readabilityAskSent.delete(rid);
  else if(sent)asks.push(sent);
  return asks;
}
// A guest's questions left for this readable version (the record counts the
// ones asked), or null (no limit shown).
function readabilityAskAllowance(run,asks){
  if(!viewerIsGuest())return null;
  const limit=Number(readabilityPolicy()?.asks?.per_run)||0, asked=Number(run?.readability?.asks_started);
  return limit?{limit,left:Math.max(0,limit-(Number.isFinite(asked)&&asked>0?asked:asks.length))}:null;
}
function readabilityAskItem(item,labels){
  const running=readabilityAskRunning(item), answer=typeof item.answer==='string'?item.answer.slice(0,20000):'';
  const li=analyzeEl('li','ask-item'+(running?' pending':!answer.trim()?' failed':''));
  const question=analyzeEl('div','ask-q');
  question.append(analyzeEl('span','who','You'),analyzeEl('span','',dagDetailText(item.question,2000)));
  li.append(question);
  const node=dagDetailText(item.node_id,200).trim();
  if(node)li.append(analyzeEl('p','ask-about','About '+(item.label||labels.get(node)||node)));
  const passage=dagDetailText(item.selection,4000).trim();
  if(passage)li.append(analyzeEl('blockquote','ask-passage',passage.length>600?passage.slice(0,600)+'…':passage));
  const reply=analyzeEl('div','ask-a md-doc');
  if(running)reply.append(analyzeEl('p','ask-pending','Answering…'));
  else if(!answer.trim())reply.append(analyzeEl('p','ask-error','✗ '+(dagDetailText(item.error,300)||'No answer was written.')));
  else renderReadableMarkdown(reply,answer,labels);
  li.append(reply);
  const at=item.finished_at||item.started_at, meta=[dagDetailText(item.model,80),at?fmtTs(at):''].filter(Boolean);
  if(meta.length&&!running)li.append(analyzeEl('p','ask-meta',meta.join(' · ')));
  return li;
}
function renderReadabilityAsks(run){
  const list=$('ask-list'); if(!list)return;
  const rid=String(run?.run_id||''), asks=readabilityAsks(run), labels=readabilityLabels(rid), macros=readabilityMacros(run);
  const key=JSON.stringify([rid,asks.map(item=>[item.ask_id,item.status,String(item.answer||'').length,item.error]),labels.size,macros]);
  if(list._askKey!==key){
    const grew=list._askRunId===rid&&asks.length>Number(list._askCount||0);
    const stick=grew||list._askRunId!==rid||list.scrollHeight-list.scrollTop-list.clientHeight<80;
    list._askKey=key; list._askRunId=rid; list._askCount=asks.length;
    list.replaceChildren(...(asks.length?asks.map(item=>readabilityAskItem(item,labels))
      :[analyzeEl('li','ask-empty','Ask about a step, a symbol or a result. Select a passage in the readable version or the original and choose “Ask about this”, or ask about a result from the Map.')]));
    list.querySelectorAll('a[href]').forEach(link=>{ link.target='_blank'; link.rel='noopener noreferrer'; });
    renderMathIn(list,macros);
    if(stick)list.scrollTop=list.scrollHeight;
  }
  syncReadabilityAskCompose(run,asks);
}
function syncReadabilityAskCompose(run,asks){
  const button=$('ask-send'), hint=$('ask-hint'); if(!button||!hint)return;
  const published=!!run?.sandbox_preview?.has_readable, allowance=readabilityAskAllowance(run,asks);
  const reason=!published?(run?.status==='running'?'You can ask once the readable version is published.':'This run published no readable version to ask about.')
    :readabilityAskSending===String(run?.run_id||'')||asks.some(readabilityAskRunning)?'One question at a time — the answer is on its way.'
    :allowance&&allowance.left<=0?`Guests can ask ${allowance.limit} questions about a readable version — sign in to ask more.`:'';
  button.disabled=!!reason; button.title=reason;
  hint.textContent=reason||(allowance?`${allowance.left} of ${allowance.limit} guest questions left · Enter to ask`:'Enter to ask · Shift+Enter newline');
}
function renderReadabilityAskContext(){
  const box=$('ask-context'); if(!box)return;
  const context=readabilityAskContext;
  if(!context){ box.hidden=true; box.replaceChildren(); return; }
  const passage=String(context.selection||'').replace(/\s+/g,' ').trim();
  const text=passage?'About this passage'+(context.view==='original'?' of the original':'')+': “'+(passage.length>240?passage.slice(0,240)+'…':passage)+'”':'About '+(context.label||context.node_id);
  const clear=analyzeEl('button','','×'); clear.type='button'; clear.setAttribute('aria-label','Ask without this context');
  clear.onclick=()=>{ readabilityAskContext=null; renderReadabilityAskContext(); $('ask-input')?.focus(); };
  box.replaceChildren(analyzeEl('span','',text),clear);
  box.hidden=false;
}
// The tab a selected passage comes from, when it can be asked about.
function readabilitySelectionView(){ return readabilityTab==='readable'||readabilityTab==='original'?readabilityTab:''; }
// A selected passage as it is written, so that it can be found in
// readable.md or original.txt: none of the view's own labels (the Original's
// line numbers, the anchor badges, the notes above the text), result links
// as "[[result:<id>]]" and typeset math as its TeX ("$n^2$"); a formula
// selected in part is taken whole.
function readabilitySelectionText(selection){
  if(!selection?.rangeCount)return '';
  const box=document.createElement('div');
  for(let index=0;index<selection.rangeCount;index++){
    const range=selection.getRangeAt(index).cloneRange();
    const start=readabilityFormulaAt(range.startContainer), end=readabilityFormulaAt(range.endContainer);
    if(start)range.setStartBefore(start);
    if(end)range.setEndAfter(end);
    box.append(range.cloneContents());
  }
  box.querySelectorAll('.ln,.rd-anchor,.rd-banner,.analysis-live-note,.rd-pdf-note,.rd-check-notes').forEach(label=>label.remove());
  box.querySelectorAll('.rd-ref').forEach(ref=>ref.replaceWith(ref.dataset.rdRef?'[[result:'+ref.dataset.rdRef+']]':''));
  box.querySelectorAll('.katex').forEach(math=>{
    const tex=math.querySelector('annotation[encoding="application/x-tex"]')?.textContent;
    if(tex==null)return;
    const delimiter=math.closest('.katex-display')?'$$':'$';
    math.replaceWith(delimiter+tex.trim()+delimiter);
  });
  return box.textContent.replace(/\s+/g,' ').trim();
}
// The typeset formula (displayed or inline) that a node of a selection is in.
function readabilityFormulaAt(node){
  const element=node?.nodeType===1?node:node?.parentElement;
  return element?.closest('.katex-display')||element?.closest('.katex')||null;
}
function askAboutSelection(text){
  readabilityAskContext={selection:String(text||'').trim().slice(0,4000),view:readabilitySelectionView()||'readable'};
  window.getSelection()?.removeAllRanges();
  renderReadabilityAskContext();
  $('ask-input')?.focus();
}
function askAboutNode(node){
  readabilityAskContext={node_id:String(node?.id||''),label:paperMapCardTitle(node),view:'readable'};
  if(workspaceView!=='sandbox')setWorkspaceView('sandbox');
  renderReadabilityAskContext();
  $('ask-input')?.focus();
}
async function sendReadabilityAsk(){
  const rid=runId, run=lastSandboxRun?.run_id===rid?lastSandboxRun:null, input=$('ask-input'), status=$('ask-status');
  const question=String(input?.value||'').trim();
  if(!rid||!isReadabilityRun(run)||!question||readabilityAskSending===rid||$('ask-send').disabled)return;
  if(question.length>2000){ status.textContent='Questions are limited to 2,000 characters.'; return; }
  const context=readabilityAskContext||{};
  const request={question,selection:String(context.selection||'').slice(0,4000),view:context.view==='original'?'original':'readable'};
  if(context.node_id)request.node_id=String(context.node_id);
  readabilityAskSending=rid; status.textContent='Sending…';
  syncReadabilityAskCompose(run,readabilityAsks(run));
  try{
    const response=await fetch('/api/run/'+encodeURIComponent(rid)+'/readability/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request)});
    const body=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(response.status===429?limitMessage(body.error||'Limit reached',response):String(body.error||('HTTP '+response.status)));
    if(runId!==rid)return;
    readabilityAskSent.set(rid,{...request,ask_id:String(body.ask_id||''),status:'running',label:context.label||'',sent_at:Date.now()});
    input.value=''; readabilityAskContext=null; renderReadabilityAskContext(); status.textContent='';
    startPolling();
  }catch(err){
    if(runId===rid)status.textContent='✗ '+String(err.message||err).slice(0,300);
  }finally{
    if(readabilityAskSending===rid)readabilityAskSending='';
    // The run shown now, which may be another one, is told where it stands.
    if(lastSandboxRun?.run_id===runId&&isReadabilityRun(lastSandboxRun))renderReadabilityAsks(lastSandboxRun);
  }
}

function initReadabilityUi(){
  document.querySelectorAll('#readability-tabs [role="tab"]').forEach(tab=>{
    tab.onclick=()=>setReadabilityTab(tab.dataset.rdView);
    // Arrow keys move between the tabs (and wrap); Home and End jump.
    tab.onkeydown=event=>{
      const keys={ArrowRight:1,ArrowLeft:-1,Home:'first',End:'last'};
      if(!(event.key in keys))return;
      event.preventDefault();
      const index=READABILITY_TABS.indexOf(tab.dataset.rdView), step=keys[event.key];
      const next=step==='first'?0:step==='last'?READABILITY_TABS.length-1:(index+step+READABILITY_TABS.length)%READABILITY_TABS.length;
      setReadabilityTab(READABILITY_TABS[next],{focus:true});
    };
  });
  $('ask-send').onclick=sendReadabilityAsk;
  $('ask-input').addEventListener('keydown',event=>{ if(event.key==='Enter'&&!event.shiftKey){ event.preventDefault(); sendReadabilityAsk(); } });
  // A result named in an answer, or in the notation table, opens at its anchor.
  for(const id of ['ask-list','md-view'])$(id).addEventListener('click',event=>{
    const ref=event.target.closest?.('.rd-ref');
    if(ref&&document.body.classList.contains('readability-run')){ event.preventDefault(); readabilityJumpTo(ref.dataset.rdRef); }
  });
  // A readable version selected before this file loaded is drawn now, and
  // polled again: the console's status line and log of it need this file too.
  const run=lastSandboxRun?.run_id===runId?lastSandboxRun:null;
  if(runId&&(isReadabilityRun(run)||RUN_KIND_BY_ID.get(String(runId))==='readability')){
    if(run)applySandboxPreview(run);
    startPolling();
    if(workspaceView==='dag'&&dagMode==='paper-map')refreshDagView();
  }
}
if(document.getElementById('readability-tabs'))initReadabilityUi();
