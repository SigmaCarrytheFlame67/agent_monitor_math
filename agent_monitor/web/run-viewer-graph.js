/* Read-only DAG views use the console's card vocabulary and graph semantics.
   All snapshot strings enter the DOM as text; node IDs never become DOM IDs. */
(function (root) {
  'use strict';
  const COLORS = Object.freeze({goal:'#06b6d4',plan:'#8b5cf6',attempt:'#3b82f6',proof_attempt:'#3b82f6',fact:'#22c55e',target:'#22c55e',verification:'#f59e0b',worker:'#64748b',orchestrator:'#a855f7',error:'#ef4444',assumption:'#0ea5e9',definition:'#8b5cf6',lemma:'#a855f7',claim:'#3b82f6',case:'#eab308',step:'#64748b',contradiction:'#ef4444',conclusion:'#14b8a6',example:'#eab308',counterexample:'#e11d48',obstacle:'#f97316',dead_end:'#94a3b8',theorem:'#22c55e',def:'#8b5cf6',variable:'#0ea5e9',sorry:'#ef4444'});
  const VERDICTS = Object.freeze({checked:'checked',gap:'gap',error:'error',unclear:'unclear',not_checked:'not checked'});
  const LANES = [
    ['root','Problem','root goal'], ['plan','Strategist','plans'], ['goal','Subgoals','AND groups'],
    ['proof_attempt','Workers','proof attempts'], ['verification','Verifier','independent verdicts'],
    ['fact','Facts','recorded facts'], ['finding','Findings','obstacles · examples · conclusions'],
  ];
  const LABELS = {outline:'Informal outline',explore:'Explore',facts:'Facts',formal:'Formal · Lean',compiler:'Compiler facts',manuscript:'Manuscript'};
  const INSTANCES = new WeakMap();
  let sequence = 0;
  const scalar = value => ['string','number','boolean'].includes(typeof value) ? String(value) : '';
  const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const own = (map, key) => Object.prototype.hasOwnProperty.call(map,key) ? map[key] : undefined;

  function canonicalVerdict(value) {
    const text=scalar(value).trim().toLowerCase();
    if(own(VERDICTS,text))return text;
    return own({'checked by a model verifier':'checked','not checked':'not_checked','not checked (no usable verifier output)':'not_checked'},text)||'';
  }

  function normalize(payload, mode) {
    const envelope = object(payload), graph = object(envelope.graph || envelope);
    const seen = new Set(), nodes = [];
    for (const item of Array.isArray(graph.nodes) ? graph.nodes : []) {
      const n = object(item), id = scalar(n.id);
      if (!id || seen.has(id)) continue;
      seen.add(id);
      nodes.push({...n,id,meta:object(n.meta)});
    }
    const edges = (Array.isArray(graph.edges) ? graph.edges : []).map(item => {
      const e = object(item);
      return {...e,from:scalar(e.from),to:scalar(e.to)};
    }).filter(e => seen.has(e.from) && seen.has(e.to));
    const role = scalar(envelope.graph_role) || ({explore:'workflow',facts:'fact-dependency',manuscript:'analysis'}[mode] || '');
    const roots = new Set((Array.isArray(envelope.selection?.target_ids) ? envelope.selection.target_ids : []).map(scalar));
    if (role === 'workflow' && !roots.size) {
      const incoming = new Set(edges.filter(e => e.label === 'decomposes-into').map(e => e.to));
      nodes.filter(n => (n.meta.workflow_kind || n.kind) === 'goal' && !incoming.has(n.id)).slice(0,1).forEach(n => roots.add(n.id));
    }
    return {envelope,graph,nodes,edges,mode,role,roots};
  }

  /* Condense strongly connected components before layering. Unlike repeated
     relaxation, a stray cycle cannot push every node into the final layer. */
  function layers(nodes, edges) {
    const byId = new Map(nodes.map(n => [n.id,n])), adj = new Map(nodes.map(n => [n.id,[]]));
    edges.forEach(e => { if (adj.has(e.from) && byId.has(e.to)) adj.get(e.from).push(e.to); });
    let next = 0;
    const index = new Map(), low = new Map(), active = new Set(), stack = [], components = [];
    function visit(id) {
      index.set(id,next); low.set(id,next++); stack.push(id); active.add(id);
      for (const to of adj.get(id)) {
        if (!index.has(to)) { visit(to); low.set(id,Math.min(low.get(id),low.get(to))); }
        else if (active.has(to)) low.set(id,Math.min(low.get(id),index.get(to)));
      }
      if (low.get(id) === index.get(id)) {
        const component = []; let item;
        do { item=stack.pop(); active.delete(item); component.push(item); } while(item !== id);
        components.push(component);
      }
    }
    nodes.forEach(n => { if (!index.has(n.id)) visit(n.id); });
    const membership = new Map(); components.forEach((group,i) => group.forEach(id => membership.set(id,i)));
    const outgoing = components.map(() => new Set()), indegree = components.map(() => 0), depth = components.map(() => 0);
    edges.forEach(e => {
      const a=membership.get(e.from), b=membership.get(e.to);
      if (a !== undefined && b !== undefined && a !== b && !outgoing[a].has(b)) { outgoing[a].add(b); indegree[b]++; }
    });
    const ready = []; indegree.forEach((count,i) => { if(!count) ready.push(i); });
    for(let cursor=0;cursor<ready.length;cursor++) for(const to of outgoing[ready[cursor]]) {
      depth[to]=Math.max(depth[to],depth[ready[cursor]]+1); if(--indegree[to]===0)ready.push(to);
    }
    const rows=[];
    nodes.forEach(n => { const d=depth[membership.get(n.id)]; (rows[d] ||= []).push(n); });
    // Stable barycenter ordering reduces crossings without altering topology.
    const parents = new Map(nodes.map(n => [n.id,[]])), positions = new Map();
    edges.forEach(e => { if(parents.has(e.to)) parents.get(e.to).push(e.from); });
    rows.forEach(row => row.forEach((n,i) => positions.set(n.id,i)));
    for(let pass=0;pass<2;pass++) rows.forEach(row => {
      const center = n => { const p=parents.get(n.id); return p.length ? p.reduce((sum,id)=>sum+(positions.get(id)||0),0)/p.length : positions.get(n.id); };
      row.sort((a,b)=>center(a)-center(b)); row.forEach((n,i)=>positions.set(n.id,i));
    });
    return rows;
  }

  function makeRows(view, columns) {
    const width = Math.max(1,Math.min(6,Math.floor(columns)||1));
    if(view.role !== 'workflow') return layers(view.nodes,view.edges).flatMap((nodes,depth) => {
      const rows=[]; for(let i=0;i<nodes.length;i+=width)rows.push({nodes:nodes.slice(i,i+width),depth,lane:null}); return rows;
    });
    const buckets = new Map(LANES.map(lane => [lane[0],[]]));
    view.nodes.forEach(n => {
      const kind=scalar(n.meta.workflow_kind||n.kind);
      const lane=kind==='goal' ? view.roots.has(n.id)?'root':'goal' : buckets.has(kind)?kind:'finding';
      buckets.get(lane).push(n);
    });
    return LANES.flatMap(lane => {
      const nodes=buckets.get(lane[0]), rows=[];
      for(let i=0;i<nodes.length;i+=width)rows.push({nodes:nodes.slice(i,i+width),lane:i===0?lane:null,depth:0});
      return rows;
    });
  }

  function layout(view, width, heights = new Map()) {
    const cardWidth=['formal','compiler'].includes(view.mode)?260:view.role==='workflow'?214:230;
    const laneWidth=view.role==='workflow'?112:0, gap=28, pad=28;
    const columns=Math.max(1,Math.min(5,Math.floor((Math.max(420,width)-laneWidth-pad*2+gap)/(cardWidth+gap))));
    const rows=makeRows(view,columns), count=Math.max(1,...rows.map(row=>row.nodes.length));
    const stageWidth=Math.max(420,laneWidth+count*(cardWidth+gap)-gap+pad*2), positions=new Map(), marks=[];
    let y=pad;
    rows.forEach((row,i) => {
      if(i)y+=row.lane||view.role!=='workflow'?66:28;
      if(row.lane)marks.push({lane:row.lane,y});
      let x=laneWidth+(stageWidth-laneWidth-(row.nodes.length*(cardWidth+gap)-gap))/2, height=0;
      row.nodes.forEach(n => { const h=heights.get(n.id)||145; positions.set(n.id,{x,y,w:cardWidth,h}); x+=cardWidth+gap; height=Math.max(height,h); });
      y+=height;
    });
    return {positions,marks,width:stageWidth,height:y+pad,cardWidth};
  }

  function visual(node, view) {
    const meta=node.meta, kind=scalar(meta.workflow_kind||node.lean_kind||node.kind)||'node';
    const out={kind,badge:kind.replace(/_/g,' '),color:own(COLORS,kind)||'#64748b',status:scalar(meta.status||node.status),classes:[],chips:[]};
    if(view.role==='analysis'||view.mode==='manuscript') {
      const raw=canonicalVerdict(meta.verdict||node.verdict), uninterpreted=meta.interpreted===false||node.uninterpreted===true;
      const verdict=!uninterpreted&&raw?raw:'not_checked';
      out.badge=scalar(meta.env||node.kind)||'result'; out.color='#64748b';
      out.verdict=verdict; out.status=uninterpreted?'no usable output':VERDICTS[verdict]; out.classes.push('analysis','v-'+verdict);
      if(meta.main_result===true||node.main_result===true)out.chips.push('main result');
      const closure=canonicalVerdict(meta.closure||node.closure);
      if(closure&&closure!==verdict)out.chips.push('with deps: '+VERDICTS[closure]);
    } else if(view.role==='workflow') {
      out.badge=(own({goal:'Goal',plan:'Plan',proof_attempt:'Attempt',verification:'Verifier',fact:'Fact'},kind)||out.badge);
      if(view.roots.has(node.id)){out.badge='Problem';out.classes.push('cgm-root');}
      const rootClosed=view.nodes.some(n=>view.roots.has(n.id)&&n.meta.status==='closed');
      if(kind==='goal'&&!view.roots.has(node.id)&&meta.and_group&&rootClosed&&!['closed','dead'].includes(out.status)) {
        out.status='not needed'; out.classes.push('cgm-superseded');
      } else if(['active','closed','blocked','dead','unverified'].includes(out.status))out.classes.push('cgm-'+out.status);
      if(out.status==='verified')out.status='model-verified';
      if(meta.verdict)out.chips.push('verdict '+scalar(meta.verdict));
      if(meta.technique)out.chips.push(scalar(meta.technique));
      if(meta.and_group)out.chips.push('AND '+scalar(meta.and_group));
    } else if(view.role==='fact-dependency') {
      const target=meta.is_target===true; out.badge=target?'Target':'Fact'; out.color=COLORS.fact;
      out.status=meta.verifier_accepted===true?'✓ model-verifier':'unattested';
      out.classes.push(target?'cgm-target':'cgm-fact');
      if(meta.verifier_accepted!==true)out.classes.push('cgm-unattested');
      if(meta.label)out.chips.push(scalar(meta.label));
      if(Number.isInteger(meta.depth))out.chips.push('depth '+meta.depth);
    }
    return out;
  }

  function meaning(view) {
    if(view.mode==='manuscript')return 'Colours show recorded model-verifier verdicts. With dependencies can differ from the local verdict. This is not a certificate.';
    if(view.role==='fact-dependency')return 'Fact dependencies · premise → result. Acceptance is by a model verifier, not a Lean certificate.';
    if(view.role==='workflow')return 'CGM search record · plans, attempts and verifier verdicts. Explore records the search, not proof logic.';
    if(view.role==='execution')return 'Agent execution record · activity and sequencing, not proof logic.';
    if(view.mode==='compiler')return 'Compiler-observed proof-body uses and type mentions · observational dependencies, not a logical proof certificate.';
    if(view.mode==='formal')return 'Structure of the saved Lean source. Open Lean for the recorded verification status.';
    return 'An outline of the reasoning · structure only. This view does not establish verification.';
  }

  function edgeRoute(a,b,positions,index=0) {
    const ax=a.x+a.w/2,bx=b.x+b.w/2;
    const down=a.y+a.h<=b.y,up=b.y+b.h<=a.y;
    if(!down&&!up){
      const lift=Math.min(a.y,b.y)-20-(index%3)*7;
      return `M${ax} ${a.y} C${ax} ${lift},${bx} ${lift},${bx} ${b.y-4}`;
    }
    const y1=down?a.y+a.h+1:a.y-1,y2=down?b.y-5:b.y+b.h+5,mid=(y1+y2)/2;
    const boxes=[...positions.values()].filter(p=>p!==a&&p!==b);
    const hits=boxes.filter(box=>{
      for(let i=1;i<24;i++){
        const t=i/24,u=1-t,x=u*u*u*ax+3*u*u*t*ax+3*u*t*t*bx+t*t*t*bx;
        const y=u*u*u*y1+3*u*u*t*mid+3*u*t*t*mid+t*t*t*y2;
        if(x>box.x-5&&x<box.x+box.w+5&&y>box.y-5&&y<box.y+box.h+5)return true;
      }
      return false;
    });
    if(!hits.length)return `M${ax} ${y1} C${ax} ${mid},${bx} ${mid},${bx} ${y2}`;
    // Route a skip-level dependency beside intervening cards, not through a
    // different card (which would misleadingly look like another dependency).
    const left=Math.min(...hits.map(p=>p.x))-17-(index%2)*5;
    const right=Math.max(...hits.map(p=>p.x+p.w))+17+(index%2)*5;
    const side=Math.abs((ax+bx)/2-left)<Math.abs(right-(ax+bx)/2)?left:right;
    const start=y1+(down?18:-18),end=y2+(down?-18:18);
    return `M${ax} ${y1} C${ax} ${start},${side} ${start},${side} ${start} L${side} ${end} C${side} ${end},${bx} ${end},${bx} ${y2}`;
  }

  function mount(container, options = {}) {
    if(!container || !container.ownerDocument)throw new TypeError('A graph container is required.');
    INSTANCES.get(container)?.destroy();
    const doc=container.ownerDocument, win=doc.defaultView||root, mode=options.mode||'outline';
    const payload=options.graph||options.payload?.graphs?.[mode]||{}, view=normalize(payload,mode);
    const make=(tag,cls,text)=>{const el=doc.createElement(tag);if(cls)el.className=cls;if(text!==undefined)el.textContent=text;return el;};
    const button=(text,label,action)=>{const el=make('button','sg-button',text);el.type='button';if(label)el.setAttribute('aria-label',label);el.addEventListener('click',action);return el;};
    const modeTitle=view.role==='execution'?'Engine trace':LABELS[mode]||'Saved graph';
    const shell=make('section','sg-shell'); shell.setAttribute('aria-label',modeTitle+' viewer');
    const toolbar=make('div','sg-toolbar'), heading=make('div','sg-heading');
    const countText=view.envelope.truncated===true&&Number.isFinite(view.envelope.counts?.nodes)
      ?`${view.nodes.length} of ${view.envelope.counts.nodes} nodes · ${view.edges.length} edges shown`
      :`${view.nodes.length} nodes · ${view.edges.length} edges`;
    heading.append(make('strong','sg-title',modeTitle),make('span','sg-count',countText));
    const search=make('input','sg-search');search.type='search';search.placeholder='Find a node…';search.setAttribute('aria-label','Find graph nodes');
    const matchCount=make('span','sg-matches','');matchCount.setAttribute('aria-live','polite');
    const zoomLabel=make('output','sg-zoom','100%');zoomLabel.setAttribute('aria-label','Graph zoom');
    const modeButton=button('List','Switch to node list',()=>{listMode=!listMode;modeButton.textContent=listMode?'Graph':'List';modeButton.setAttribute('aria-label',listMode?'Switch to graph':'Switch to node list');shell.classList.toggle('sg-list-mode',listMode);zoomControls.hidden=listMode;});
    const zoomControls=make('div','sg-zoom-controls');
    zoomControls.append(button('−','Zoom out',()=>setZoom(scale/1.2)),zoomLabel,button('+','Zoom in',()=>setZoom(scale*1.2)),button('Fit','Fit graph width',()=>fit()));
    toolbar.append(heading,search,matchCount,zoomControls,modeButton);
    const legend=make('div','sg-legend');legend.setAttribute('aria-label','Graph legend');
    const body=make('div','sg-body'), viewport=make('div','sg-viewport');viewport.tabIndex=0;viewport.setAttribute('role','region');viewport.setAttribute('aria-label','Scrollable graph. Use arrow keys to scroll; select a node to inspect it.');
    const sizer=make('div','sg-sizer'), stage=make('div','sg-stage'), nodeLayer=make('div','sg-nodes');
    const svg=doc.createElementNS('http://www.w3.org/2000/svg','svg');svg.classList.add('sg-edges');svg.setAttribute('aria-hidden','true');
    const inspector=make('aside','sg-inspector');inspector.hidden=true;inspector.setAttribute('aria-label','Node details');
    const list=make('div','sg-list');list.setAttribute('aria-label','Graph node list');
    const foot=make('div','sg-note',(view.envelope.truncated===true?'This snapshot includes a bounded excerpt of the graph. ':'')+meaning(view));
    stage.append(svg,nodeLayer);sizer.append(stage);viewport.append(sizer);body.append(viewport,list,inspector);shell.append(toolbar,legend,body,foot);container.replaceChildren(shell);
    let scale=1, geometry, selected=null, listMode=false, disposed=false, frame=0, searchTimer=0;
    const cards=new Map(), listCards=new Map(), paths=[], listeners=[];
    const renderText=(element,text)=>{if(typeof options.renderText==='function')options.renderText(element,scalar(text));else element.textContent=scalar(text);};
    const statusTone=value=>['closed','supported','model-verified','✓ model-verifier','active'].includes(value)?'ok':['blocked','challenged','unattested'].includes(value)?'warn':['dead','error','failed'].includes(value)?'bad':'';

    function buildCard(node, compact) {
      const v=visual(node,view), el=make('button','sg-node '+v.classes.join(' '));el.type='button';el.style.setProperty('--kc',v.color);el.setAttribute('aria-pressed','false');
      el.setAttribute('aria-label',`${v.badge}: ${scalar(node.label)||node.id}${v.status?' · '+v.status:''}`);
      const row=make('span','sg-kd-row');row.append(make('span','sg-kd',v.badge));
      if(v.status)row.append(make('span','sg-status '+statusTone(v.status),v.status));
      const label=make('span','sg-label',scalar(node.label)||node.id);el.append(row,label);
      if(node.statement){const statement=make('span','sg-statement',scalar(node.statement));el.append(statement);if(!['formal','compiler'].includes(mode))root.AnsatzSharedText?.math(statement,{manuscript:mode==='manuscript'});}
      if(!['formal','compiler'].includes(mode))root.AnsatzSharedText?.math(label,{manuscript:mode==='manuscript'});
      const chips=make('span','sg-chips');v.chips.slice(0,4).forEach(text=>chips.append(make('span','sg-chip',text)));
      if(chips.childNodes.length)el.append(chips);
      if(!compact&&Array.isArray(node.citations)&&node.citations.length){const cites=make('span','sg-citations');node.citations.slice(0,4).forEach(cite=>cites.append(make('span','',scalar(cite))));el.append(cites);}
      el.addEventListener('click',()=>select(node.id));
      el.addEventListener('mouseenter',()=>highlight(node.id));el.addEventListener('mouseleave',()=>highlight(selected));
      return el;
    }
    view.nodes.forEach(node=>{const card=buildCard(node,false), item=buildCard(node,true);cards.set(node.id,card);listCards.set(node.id,item);nodeLayer.append(card);list.append(item);});

    function legendItem(text,color,dashed=false){const item=make('span','sg-legend-item'),sw=make('i',dashed?'sg-swatch dashed':'sg-swatch');sw.style.setProperty('--kc',color);item.append(sw,doc.createTextNode(text));legend.append(item);}
    if(mode==='manuscript') {
      for(const [id,label]of Object.entries(VERDICTS))legendItem(label,({checked:'#3b82f6',gap:'#d97706',error:'#ef4444',unclear:'#64748b',not_checked:'#94a3b8'})[id],id==='not_checked');
      legend.append(make('span','sg-legend-text','solid: reference · dashed: inferred · dotted: model-proposed'));
    } else if(view.role==='workflow') {
      [['Goal',COLORS.goal],['Plan',COLORS.plan],['Attempt',COLORS.attempt],['Verifier',COLORS.verification],['Fact',COLORS.fact]].forEach(([label,color])=>legendItem(label,color));
      legend.append(make('span','sg-legend-text','dashed: blocked · faded: dead / superseded'));
    } else if(view.role==='fact-dependency') {
      legendItem('Fact',COLORS.fact);legendItem('Target',COLORS.fact);legendItem('Unattested',COLORS.fact,true);legend.append(make('span','sg-legend-text','arrow: premise → result'));
    } else if(mode==='compiler') {
      legendItem('proof uses','#06b6d4');legendItem('type mentions','#8b5cf6',true);
    } else {
      [...new Set(view.nodes.map(n=>scalar(n.lean_kind||n.kind)))].slice(0,8).forEach(kind=>legendItem(kind||'node',own(COLORS,kind)||'#64748b'));
    }

    function edgeStyle(edge) {
      const label=scalar(edge.label||edge.kind), provenance=scalar(edge.provenance);
      const result={color:'var(--dim)',dash:'',label};
      if(mode==='manuscript'){result.dash=provenance==='model'?'2 5':provenance==='inferred'?'7 5':'';return result;}
      if(['refutes','blocks','contradicts'].includes(label)){result.color='#ef4444';result.dash='6 4';}
      else if(label==='decomposes-into')result.color='#8b5cf6';
      else if(['supports','promoted-to','verifier-accepted','fact dependency'].includes(label))result.color='#22a567';
      else if(label==='verifier-input')result.color='#d99b26';
      else if(label==='alternative-of')result.dash='2 5';
      else if(label==='type mentions'){result.color='#8b5cf6';result.dash='5 4';}
      else if(label==='proof uses')result.color='#06b6d4';
      return result;
    }
    function drawEdges() {
      svg.replaceChildren();paths.length=0;
      const ns=svg.namespaceURI, defs=doc.createElementNS(ns,'defs'), marker=doc.createElementNS(ns,'marker'), markerId='sg-arrow-'+(++sequence), tip=doc.createElementNS(ns,'path');
      for(const[k,v]of Object.entries({id:markerId,markerWidth:'8',markerHeight:'8',refX:'7',refY:'4',orient:'auto',markerUnits:'userSpaceOnUse'}))marker.setAttribute(k,v);
      tip.setAttribute('d','M0 0 L8 4 L0 8 Z');tip.setAttribute('fill','context-stroke');marker.append(tip);defs.append(marker);svg.append(defs);
      svg.setAttribute('viewBox',`0 0 ${geometry.width} ${geometry.height}`);
      view.edges.forEach((edge,index)=>{
        const a=geometry.positions.get(edge.from),b=geometry.positions.get(edge.to);if(!a||!b)return;
        const path=doc.createElementNS(ns,'path'),style=edgeStyle(edge);
        const route=edgeRoute(a,b,geometry.positions,index);
        path.setAttribute('d',route);path.setAttribute('stroke',style.color);path.setAttribute('marker-end',`url(#${markerId})`);if(style.dash)path.setAttribute('stroke-dasharray',style.dash);path.classList.add('sg-edge');
        const title=doc.createElementNS(ns,'title');title.textContent=style.label;path.append(title);svg.append(path);paths.push({path,edge});
      });
      highlight(selected);
    }
    function redraw() {
      if(disposed||!view.nodes.length)return;
      const width=Math.max(420,viewport.clientWidth||container.clientWidth||900), nominal=layout(view,width);
      cards.forEach(card=>card.style.width=nominal.cardWidth+'px');
      const heights=new Map([...cards].map(([id,card])=>[id,card.offsetHeight||150]));
      geometry=layout(view,width,heights);
      cards.forEach((card,id)=>{const p=geometry.positions.get(id);card.style.left=p.x+'px';card.style.top=p.y+'px';});
      nodeLayer.querySelectorAll('.sg-lane').forEach(el=>el.remove());
      geometry.marks.forEach(({lane,y})=>{const el=make('div','sg-lane');el.style.top=y+'px';el.append(make('b','',lane[1]),make('small','',lane[2]));nodeLayer.append(el);});
      stage.style.width=geometry.width+'px';stage.style.height=geometry.height+'px';drawEdges();applyZoom();
    }
    function applyZoom(){if(!geometry)return;stage.style.transform=`scale(${scale})`;sizer.style.width=Math.max(geometry.width*scale,viewport.clientWidth)+'px';sizer.style.height=geometry.height*scale+'px';zoomLabel.textContent=Math.round(scale*100)+'%';}
    function setZoom(value){const before=scale;scale=Math.max(.3,Math.min(2,Number(value)||1));const x=(viewport.scrollLeft+viewport.clientWidth/2)/before,y=(viewport.scrollTop+viewport.clientHeight/2)/before;applyZoom();viewport.scrollLeft=x*scale-viewport.clientWidth/2;viewport.scrollTop=y*scale-viewport.clientHeight/2;}
    function fit(){if(geometry)setZoom(Math.min(1,(viewport.clientWidth-16)/geometry.width));viewport.scrollTop=0;viewport.scrollLeft=0;}
    function highlight(id){stage.classList.toggle('has-selection',id!==null);paths.forEach(({path,edge})=>path.classList.toggle('highlight',id!==null&&(edge.from===id||edge.to===id)));}
    function closeDetail(restoreFocus=false){const previous=selected;selected=null;inspector.hidden=true;inspector.replaceChildren();shell.classList.remove('sg-inspecting');cards.forEach(card=>{card.classList.remove('selected');card.setAttribute('aria-pressed','false');});listCards.forEach(card=>card.setAttribute('aria-pressed','false'));highlight(null);if(restoreFocus)(listMode?listCards:cards).get(previous)?.focus();}
    function addSection(body,title,value,code=false){const text=scalar(value);if(!text.trim())return;const section=make('section'),content=make(code?'pre':'div',code?'sg-code':'sg-prose');section.append(make('h5','',title),content);if(code)content.textContent=text;else renderText(content,text);body.append(section);}
    function select(id){
      const node=view.nodes.find(n=>n.id===id);if(!node)return;
      selected=id;shell.classList.add('sg-inspecting');const v=visual(node,view),meta=node.meta;
      cards.forEach((card,key)=>{card.classList.toggle('selected',key===id);card.setAttribute('aria-pressed',String(key===id));});listCards.forEach((card,key)=>card.setAttribute('aria-pressed',String(key===id)));highlight(id);
      const header=make('div','sg-detail-head'),title=make('div'),badge=make('span','sg-kd',v.badge);badge.style.setProperty('--kc',v.color);title.append(badge,make('h4','',scalar(node.label)||id));
      const close=button('×','Close node details',()=>closeDetail(true));header.append(title,close);
      const content=make('div','sg-detail-body'),facts=make('dl');
      const fact=(label,value)=>{const text=scalar(value);if(text)facts.append(make('dt','',label),make('dd','',text));};
      if(v.status)fact(mode==='manuscript'?'Local verdict':'Status',v.status);
      if(mode==='manuscript') {
        const closure=scalar(meta.closure||node.closure);fact('With dependencies',own(VERDICTS,canonicalVerdict(closure))||closure);
        if(view.envelope.dependency_basis==='model')fact('Dependency basis','Model-proposed links do not establish dependencies or change a verdict.');
      }
      if(view.role==='fact-dependency'){fact('Verifier',meta.verifier_accepted===true?'Accepted by the model verifier':'Not attested');fact('Target theorem',meta.is_target===true?'yes':'no');fact('Lemma',meta.label);if(Number.isInteger(meta.depth))fact('Depth',meta.depth);}
      if(view.role==='workflow'){fact('Verifier verdict',meta.verdict);fact('Technique',meta.technique);fact('Decomposition',meta.decomposition_technique);fact('AND group',meta.and_group);if(typeof meta.verifiable==='boolean')fact('Verifiable',meta.verifiable?'yes':'no');if(typeof meta.score==='number'&&Number.isFinite(meta.score))fact('Curator score',meta.score.toFixed(2));}
      if(facts.childNodes.length)content.append(facts);
      addSection(content,'Statement',node.statement,['formal','compiler'].includes(mode));
      addSection(content,meta.proof_truncated===true?'Proof excerpt (truncated)':'Proof excerpt',meta.proof_excerpt);
      addSection(content,'Intuition',meta.intuition);
      if(Array.isArray(node.citations))addSection(content,'Citations',node.citations.map(scalar).filter(Boolean).join('\n'),true);
      const links=view.edges.filter(e=>e.from===id||e.to===id);
      if(links.length){
        const section=make('section'),linkList=make('ul','sg-links');section.append(make('h5','','Connections'),linkList);
        links.forEach(edge=>{const outgoing=edge.from===id,other=view.nodes.find(n=>n.id===(outgoing?edge.to:edge.from));if(!other)return;const row=make('li'),relation=mode==='manuscript'?(outgoing?'used by':'uses')+' · '+({ref:'reference',inferred:'inferred',model:'model-proposed'}[edge.provenance]||'reference'):(outgoing?'→ ':'← ')+scalar(edge.label||edge.kind||'link');row.append(make('span','sg-relation',relation),button(scalar(other.label)||other.id,'Inspect '+(scalar(other.label)||other.id),()=>{select(other.id);reveal(other.id);}));linkList.append(row);});content.append(section);
      }
      addSection(content,'Node ID',id,true);content.append(make('p','sg-detail-note',meaning(view)));inspector.replaceChildren(header,content);inspector.hidden=false;
      if(typeof options.onSelect==='function')options.onSelect(node);
    }
    function reveal(id){const card=(listMode?listCards:cards).get(id);if(!card)return;if(listMode){card.scrollIntoView({block:'nearest',inline:'nearest'});return;}const p=geometry?.positions.get(id);if(p){viewport.scrollLeft=Math.max(0,(p.x+p.w/2)*scale-viewport.clientWidth/2);viewport.scrollTop=Math.max(0,p.y*scale-50);}}
    function searchNodes(){const term=search.value.trim().toLocaleLowerCase();let matches=0;view.nodes.forEach(node=>{const match=!term||[node.label,node.id,node.statement,node.kind,node.meta.label].map(scalar).join(' ').toLocaleLowerCase().includes(term);cards.get(node.id).classList.toggle('sg-unmatched',!match);listCards.get(node.id).hidden=!match;if(match)matches++;});matchCount.textContent=term?matches+' found':'';}
    search.addEventListener('input',()=>{win.clearTimeout(searchTimer);searchTimer=win.setTimeout(searchNodes,80);});
    search.addEventListener('keydown',event=>{if(event.key==='Enter'){searchNodes();const node=view.nodes.find(n=>!listCards.get(n.id).hidden);if(node){select(node.id);reveal(node.id);(listMode?listCards:cards).get(node.id).focus();}}});
    shell.addEventListener('keydown',event=>{if(event.key==='Escape'&&selected!==null){event.preventDefault();closeDetail(true);}});
    viewport.addEventListener('wheel',event=>{if(event.ctrlKey||event.metaKey){event.preventDefault();setZoom(scale*(event.deltaY>0?1/1.1:1.1));}},{passive:false});
    let drag=null;
    viewport.addEventListener('pointerdown',event=>{if(event.button!==0||event.target.closest('button'))return;drag={x:event.clientX,y:event.clientY,left:viewport.scrollLeft,top:viewport.scrollTop};viewport.setPointerCapture(event.pointerId);viewport.classList.add('sg-panning');});
    viewport.addEventListener('pointermove',event=>{if(drag){viewport.scrollLeft=drag.left-(event.clientX-drag.x);viewport.scrollTop=drag.top-(event.clientY-drag.y);}});
    const endDrag=()=>{drag=null;viewport.classList.remove('sg-panning');};viewport.addEventListener('pointerup',endDrag);viewport.addEventListener('pointercancel',endDrag);
    let observer;
    if(typeof win.ResizeObserver==='function'){observer=new win.ResizeObserver(()=>{win.cancelAnimationFrame(frame);frame=win.requestAnimationFrame(redraw);});observer.observe(viewport);}
    else{win.addEventListener('resize',redraw);listeners.push(()=>win.removeEventListener('resize',redraw));}
    if(!view.nodes.length){const empty=make('div','sg-empty'),reason=scalar(view.envelope.reason).trim()||'This saved snapshot has no '+modeTitle.toLowerCase()+'. A different graph is not substituted for it.';empty.append(make('div','sg-empty-icon','◇'),make('h3','',modeTitle+' was not included'),make('p','',reason));body.replaceChildren(empty);search.disabled=true;zoomControls.hidden=true;modeButton.hidden=true;legend.hidden=true;}
    else {redraw();frame=win.requestAnimationFrame(()=>{redraw();fit();});}
    const api={fit,select,destroy(){if(disposed)return;disposed=true;observer?.disconnect();win.cancelAnimationFrame(frame);win.clearTimeout(searchTimer);listeners.forEach(remove=>remove());container.replaceChildren();INSTANCES.delete(container);}};
    INSTANCES.set(container,api);return api;
  }
  const api={mount,normalize,layout,layers,visual,edgeRoute};
  if(typeof module==='object'&&module.exports)module.exports=api;
  root.AnsatzSharedGraphs=api;
})(typeof window==='undefined'?globalThis:window);
