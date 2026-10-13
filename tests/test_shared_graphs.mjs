import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {normalize,layers,layout,visual,edgeRoute}=require('../agent_monitor/web/run-viewer-graph.js');

test('cycles stay in one component without swallowing downstream layers',()=>{
  const nodes=['a','b','c','d'].map(id=>({id}));
  const rows=layers(nodes,[{from:'a',to:'b'},{from:'b',to:'a'},{from:'b',to:'c'},{from:'c',to:'d'}]);
  assert.deepEqual(rows.map(row=>row.map(n=>n.id)),[['a','b'],['c'],['d']]);
});

test('wide graphs wrap to readable cards without losing edges or nodes',()=>{
  const input={graph:{nodes:Array.from({length:60},(_,i)=>({id:String(i),label:'Lemma '+i})),edges:[]}};
  const view=normalize(input,'outline'), geometry=layout(view,950);
  assert.equal(geometry.positions.size,60);
  assert.ok(geometry.width<=950);
  assert.ok(geometry.cardWidth>=214);
  assert.ok(new Set([...geometry.positions.values()].map(p=>p.y)).size>1);
});

test('Explore uses console semantic lanes, including explicit root targets',()=>{
  const view=normalize({graph_role:'workflow',selection:{target_ids:['root']},graph:{nodes:[
    {id:'fact',meta:{workflow_kind:'fact'}},{id:'try',meta:{workflow_kind:'proof_attempt'}},
    {id:'root',meta:{workflow_kind:'goal'}},{id:'plan',meta:{workflow_kind:'plan'}}],edges:[]}},'explore');
  const geo=layout(view,1200),y=id=>geo.positions.get(id).y;
  assert.ok(y('root')<y('plan')&&y('plan')<y('try')&&y('try')<y('fact'));
  assert.equal(visual(view.nodes.find(n=>n.id==='root'),view).badge,'Problem');
});

test('an absent Facts snapshot never borrows outline data',()=>{
  const view=normalize({},'facts');
  assert.equal(view.nodes.length,0);
  assert.equal(view.edges.length,0);
});

test('model acceptance and local versus closure verdicts stay distinct',()=>{
  let view=normalize({graph:{nodes:[{id:'f',meta:{verifier_accepted:true,is_target:true}}]}},'facts');
  assert.equal(visual(view.nodes[0],view).status,'✓ model-verifier');
  view=normalize({graph:{nodes:[{id:'m',verdict:'checked',closure:'error',main_result:true}]}},'manuscript');
  const v=visual(view.nodes[0],view);
  assert.equal(v.status,'checked');assert.ok(v.chips.includes('with deps: error'));
  view.nodes[0].uninterpreted=true;
  assert.equal(visual(view.nodes[0],view).verdict,'not_checked');
});

test('shared descriptive closure verdicts preserve unchecked dependencies',()=>{
  const view=normalize({graph:{nodes:[
    {id:'local-checked',verdict:'checked',closure:'not checked'},
    {id:'all-checked',verdict:'checked',closure:'checked by a model verifier'},
    {id:'local-gap',verdict:'gap',closure:'checked by a model verifier'},
  ]}},'manuscript');
  const [localChecked,allChecked,localGap]=view.nodes.map(n=>visual(n,view));
  assert.equal(localChecked.status,'checked');
  assert.ok(localChecked.chips.includes('with deps: not checked'));
  assert.equal(allChecked.chips.length,0,'equivalent canonical and descriptive verdicts do not create a false distinction');
  assert.equal(localGap.status,'gap');
  assert.ok(localGap.chips.includes('with deps: checked'));
});

test('IDs are data and prototype-like kinds cannot set presentation values',()=>{
  const view=normalize({graph:{nodes:[{id:'__proto__',kind:'constructor'},{id:'x'},{id:'x'}],edges:[{from:'__proto__',to:'x'},{from:'missing',to:'x'}]}},'outline');
  assert.equal(view.nodes.length,2);assert.equal(view.edges.length,1);
  assert.equal(visual(view.nodes[0],view).color,'#64748b');
  assert.equal(layout(view,800).positions.size,2);
});

test('skip-level arrows route around an intermediate card',()=>{
  const a={x:50,y:0,w:230,h:100}, middle={x:50,y:200,w:230,h:100}, b={x:50,y:400,w:230,h:100};
  const positions=new Map([['a',a],['middle',middle],['b',b]]);
  const route=edgeRoute(a,b,positions);
  assert.match(route,/ L/,'the route has a side corridor past the intervening node');
  assert.doesNotMatch(edgeRoute(a,middle,positions),/ L/,'adjacent nodes use a simple curve');
});
