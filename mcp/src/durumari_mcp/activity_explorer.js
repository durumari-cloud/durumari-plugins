/* Offline, view-local graph. No storage, network requests, or inferred chat IDs. */
(()=>{
 const style=document.createElement('style');style.textContent=`
 .exploration{display:grid;gap:12px;min-width:0}.explore-bar{display:flex;align-items:flex-end;justify-content:space-between;gap:var(--space-3);padding:4px 4px 0}.explore-bar h2{font-size:17px;line-height:1.4;font-weight:700}.explore-bar p{margin:2px 0 0;color:var(--text-muted);font-size:13px}.explore-layout{display:grid;gap:12px;grid-template-columns:minmax(0,1fr);min-width:0}.graph-panel,.graph-reader{min-width:0;background:var(--surface);border:1px solid var(--border-subtle);border-radius:var(--w-card-radius)}.graph-panel{overflow:hidden}.graph-legend{display:flex;gap:6px 12px;padding:12px 16px;font-size:var(--text-xs);color:var(--text-muted);flex-wrap:wrap}.graph-viewport{position:relative;height:320px;overflow:clip;touch-action:none;border-block:1px solid var(--border-subtle);background-color:var(--surface-app);background-image:radial-gradient(var(--neutral-border) 1px,transparent 1px);background-size:20px 20px}.graph-canvas{position:absolute;left:0;top:0;transform-origin:0 0;width:1800px;height:1400px}.graph-canvas svg{position:absolute;inset:0;width:100%;height:100%;overflow:visible;pointer-events:none}.graph-node{position:absolute;width:var(--node-w,190px);min-height:72px;text-align:left;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:12px;box-shadow:0 2px 8px color-mix(in srgb,var(--text) 6%,transparent);animation:context-appear .25s ease-out both}.graph-node[data-state=discovered]{background:var(--surface-app);border-style:dashed;color:var(--text-muted)}.graph-node[aria-pressed=true]{border:2px solid var(--brand);box-shadow:0 0 0 4px color-mix(in srgb,var(--brand) 12%,transparent)}.graph-node strong{display:block;font-size:var(--text-sm);line-height:1.5;overflow-wrap:anywhere}.graph-node small{display:block;font-size:11px;margin-top:4px;color:var(--text-muted)}.graph-controls{display:flex;gap:var(--space-2);padding:10px 16px}.graph-controls button{min-height:36px;padding:0 12px}.graph-reader{padding:16px;max-height:560px;overflow:auto;display:grid;gap:10px;align-content:start}.graph-reader h3{font-size:17px;line-height:1.5;margin:0;overflow-wrap:anywhere}.graph-reader p{white-space:pre-wrap;overflow-wrap:anywhere}.graph-reader .source-section{border-top:1px solid var(--border-subtle);padding:12px 0 0}.graph-reader h4{margin:0 0 var(--space-2)}.graph-mobile-back{display:inline-flex;align-items:center;justify-self:start;min-height:36px}.graph-message{padding:0 4px;margin:0;color:var(--text-muted);font-size:13px}.graph-message:empty{display:none}.graph-message[role=alert]{color:var(--w-danger-text)}.exploration[data-mobile-reader=true] .graph-panel{display:none}.exploration[data-mobile-reader=false] .graph-reader{display:none}@container widget (min-width:880px){.explore-layout{grid-template-columns:minmax(0,1.5fr) minmax(280px,1fr);align-items:start}.graph-viewport{height:440px}.exploration[data-mobile-reader=true] .graph-panel,.exploration[data-mobile-reader=false] .graph-reader{display:block}.exploration[data-mobile-reader=false] .graph-reader{display:grid}.graph-mobile-back{display:none}}@keyframes context-appear{from{opacity:0;translate:0 8px}to{opacity:1;translate:0 0}}@media(prefers-reduced-motion:reduce){.graph-node{animation:none}}
 `;document.head.append(style);
 const root=document.createElement('section');root.className='exploration';root.setAttribute('aria-label','Context 탐색 그래프');root.hidden=true;root.dataset.mobileReader='false';
 // All dynamic data below is inserted with textContent, never parsed as HTML.
 root.innerHTML='<div class="explore-bar"><div><h2>맥락을 연결하며 살펴보기</h2><p id="explore-count"></p></div><span class="muted">Context 탐색</span></div><div class="explore-layout"><div class="graph-panel"><div class="graph-legend"><span>● 읽은 문서</span><span>◌ 발견한 문서</span><span>― 문서 관계</span><span>┄ 탐색 경로</span></div><div class="graph-viewport" aria-label="그래프 이동 영역"><div class="graph-canvas"><svg aria-hidden="true"></svg></div></div><div class="graph-controls"><button aria-label="그래프 축소">−</button><button aria-label="그래프 확대">+</button><button>처음 위치</button></div></div><article class="graph-reader" aria-label="선택한 Context 원문"></article></div><p class="graph-message" role="status" aria-live="polite"></p>';
 document.querySelector('main').append(root);
 const canvas=root.querySelector('.graph-canvas'),svg=root.querySelector('svg'),viewport=root.querySelector('.graph-viewport'),pane=root.querySelector('.graph-reader'),message=root.querySelector('.graph-message'),countLabel=root.querySelector('#explore-count');
 let nodeW=190,step=250,x0=40,state=null,ref,selected=null,epoch=0,sequence=0,positions=new Map(),rows=new Map(),buttons=new Map(),drawn=new Map(),edgeNodes=new Map(),zoom=1,pan={x:0,y:0},drag=null,eventLimitHit=false,limitedDocuments=new Set();
 const ns='http://www.w3.org/2000/svg';
 function element(tag,text){const el=document.createElement(tag);el.textContent=text;return el;}
 function transform(){canvas.style.transform=`translate(${pan.x}px,${pan.y}px) scale(${zoom})`;}
 function clear(){epoch++;state=null;selected=null;positions.clear();rows.clear();buttons.forEach(button=>button.remove());buttons.clear();drawn.clear();edgeNodes.clear();svg.replaceChildren();pane.replaceChildren();root.hidden=true;message.textContent='';root.dataset.mobileReader='false';zoom=1;pan={x:0,y:0};eventLimitHit=false;limitedDocuments.clear();transform();}
 function apply(event){const next=workspaceRuntime.apply(state,event);if(next===state)throw new Error(state.events.size>=1000||state.documents.size>=200?'exploration_limit':'exploration_event_invalid');state=next;sequence=Math.max(sequence,event.sequence);}
 function emit(type,payload){apply({scope:state.scope,event_id:crypto.randomUUID(),sequence:++sequence,type,...payload});}
 // Layout by index: one pass builds the incoming relations of each document, and each level keeps its
 // own row counter, so placing costs documents + relations instead of documents x relations.
 // A document sits one column right of the first recorded relation whose source was placed before it
 // in this pass; a position, once given, never moves.
 function place(docs){
  if(docs.every(doc=>positions.has(doc.id)))return;
  const incoming=new Map();
  for(const edge of state.edges.values()){const list=incoming.get(edge.to);if(list)list.push(edge);else incoming.set(edge.to,[edge]);}
  const levels=new Map();
  for(const doc of docs){
   let level=0;
   for(const edge of incoming.get(doc.id)||[])if(levels.has(edge.from)){level=Math.min(5,levels.get(edge.from)+1);break;}
   levels.set(doc.id,level);
   if(!positions.has(doc.id)){const row=rows.get(level)||0;rows.set(level,row+1);positions.set(doc.id,{x:x0+level*step,y:50+row*135,level});}
  }
 }
 // Only what changed is written to the page: a node is created once and afterwards only its state,
 // selection and label are updated when they differ from what is already shown.
 function drawNodes(docs){
  if(buttons.size>docs.length)for(const [id,button] of buttons)if(!state.documents.has(id)){button.remove();buttons.delete(id);drawn.delete(id);}
  for(const doc of docs){
   let button=buttons.get(doc.id),shown=drawn.get(doc.id);
   if(!button){
    button=element('button','');button.className='graph-node';button.style.animationDelay=Math.min(buttons.size*45,300)+'ms';button.dataset.documentId=doc.id;
    button.onclick=()=>select(doc.id,true);
    button.onfocus=()=>{const p=positions.get(doc.id);if(!p)return;const x=p.x*zoom+pan.x;if(x<0||x+nodeW*zoom>viewport.clientWidth){pan.x=24-p.x*zoom;transform();}};
    const p=positions.get(doc.id);button.style.left=p.x+'px';button.style.top=p.y+'px';
    canvas.append(button);buttons.set(doc.id,button);shown={};drawn.set(doc.id,shown);
   }
   const pressed=selected===doc.id;
   if(shown.state!==doc.state)button.dataset.state=doc.state;
   if(shown.pressed!==pressed)button.setAttribute('aria-pressed',String(pressed));
   if(shown.state!==doc.state||shown.title!==doc.title||shown.revision!==doc.revision)button.replaceChildren(element('strong',doc.title),element('small',(doc.state==='read'?'읽음':'발견 · 눌러서 읽기')+' · 버전 '+doc.revision));
   shown.state=doc.state;shown.pressed=pressed;shown.title=doc.title;shown.revision=doc.revision;
  }
 }
 // A relation's key holds its two documents, kind and label, and positions never move, so a drawn
 // relation never changes: new ones are added and ones no longer in the state are removed.
 function drawEdges(){
  for(const [key,nodes] of edgeNodes)if(!state.edges.has(key)){nodes.line.remove();nodes.label.remove();edgeNodes.delete(key);}
  for(const [key,edge] of state.edges){
   if(edgeNodes.has(key))continue;
   const a=positions.get(edge.from),b=positions.get(edge.to);if(!a||!b)continue;
   const line=document.createElementNS(ns,'path');line.setAttribute('d',`M${a.x+nodeW} ${a.y+40} C${a.x+nodeW+30} ${a.y+40} ${b.x-30} ${b.y+40} ${b.x} ${b.y+40}`);line.setAttribute('fill','none');line.style.stroke=edge.kind==='traversal'?'var(--brand)':'var(--neutral-border)';line.setAttribute('stroke-width','1.5');if(edge.kind==='traversal')line.setAttribute('stroke-dasharray','5 5');
   const label=document.createElementNS(ns,'text');label.textContent=({references:'참조',supports:'근거',supersedes:'대체',contradicts:'충돌 관계'})[edge.label]||edge.label;label.setAttribute('x',String((a.x+b.x+nodeW)/2));label.setAttribute('y',String((a.y+b.y)/2+(edge.kind==='traversal'?58:28)));label.setAttribute('font-size','11');label.style.fill='var(--text-muted)';
   svg.append(line,label);edgeNodes.set(key,{line,label});
  }
 }
 function draw(){
  if(!state)return;const docs=[...state.documents.values()];
  let readCount=0;for(const doc of docs)if(doc.state==='read')readCount++;
  const count=readCount+'개 읽음 · '+docs.length+'개 문서 발견';if(countLabel.textContent!==count)countLabel.textContent=count;
  place(docs);drawNodes(docs);drawEdges();
  const limited=eventLimitHit||limitedDocuments.has(selected);message.setAttribute('role',limited?'alert':'status');message.textContent=limited?'이 탐색에서 표시할 수 있는 범위에 도달했습니다. 해당 문서를 새 조회로 열어 이어서 읽어 주세요.':state.incomplete?'일부 탐색 기록이 아직 도착하지 않았습니다.':'';transform();
 }
 // A narrow widget starts zoomed out so the first columns fit; the person can still pan and zoom afterwards.
 function fit(){const width=viewport.clientWidth;if(!width||width>=560)return;let right=0;for(const p of positions.values())right=Math.max(right,p.x+nodeW);zoom=Math.min(1,Math.max(.8,(width-16)/(right+12)));transform();}
 function renderReader(doc,unavailable=false){
  pane.replaceChildren();const back=element('button','그래프로 돌아가기');back.className='graph-mobile-back';back.onclick=()=>{root.dataset.mobileReader='false';buttons.get(selected)?.focus();};pane.append(back,element('h3',doc.title),element('small',(doc.state==='read'?(doc.coverage==='outline'?'문서 구성 조회':'읽은 범위'):'아직 읽지 않은 문서')+' · 버전 '+doc.revision));
  if(doc.state!=='read'){pane.append(element('p',unavailable?'이 탐색에서는 문서를 더 불러올 수 없습니다. 새 조회로 열어 주세요.':'문서를 불러오는 중입니다.'));return;}
  if(doc.partial)pane.append(element('p','일부 범위입니다.'));
  for(const section of doc.sections){const block=element('section','');block.className='source-section';if(section.title)block.append(element('h4',section.title));if(section.text)block.append(durumariMarkdown.section(section.text,section.title));if(section.id||section.sha256){const details=element('details','');details.append(element('summary','출처 정보'),element('p',[section.id,section.sha256].filter(Boolean).join(' · ')));block.append(details);}pane.append(block);}
  if(doc.after&&!eventLimitHit&&!limitedDocuments.has(doc.id)){const more=element('button','본문 더 읽기');more.onclick=()=>read(doc.id,doc.after);pane.append(more);}
 }
 async function read(id,after){
  const token=epoch,current=state?.documents.get(id);if(!current)return;
  try{
   const value=await tool('durumari_context_read',{project_ref:ref,context_id:id,revision:current.revision,budget_tokens:2000,after:after||undefined});
   if(token!==epoch)return;const source=canonical(value),doc=workspaceRuntime.document(source);
   if(!doc||doc.project_id!==state.scope.project_id||doc.id!==id||doc.revision!==current.revision)throw new Error('scope_changed');
   if(after){doc.sections=[...current.sections,...doc.sections];if(doc.sections.length>100)throw new Error('section_limit');}emit('read',{document:{...source,sections:doc.sections}});
   for(const n of (source.neighbors||[]).slice(0,12)){if(!n||typeof n.id!=='string'||!Number.isSafeInteger(n.revision))continue;emit('discovered',{document:{...n,project_id:state.scope.project_id,sections:[]}});if(typeof n.relation==='string')emit('relation',{edge:{from:n.direction==='in'?n.id:id,to:n.direction==='in'?id:n.id,kind:'document',label:n.relation}});}
   draw();if(selected===id)renderReader(state.documents.get(id));
  }catch(cause){if(token!==epoch)return;if(['exploration_limit','section_limit'].includes(cause.message)){showLimit(cause.message,id);return;}clear();root.hidden=false;message.setAttribute('role','alert');message.textContent='문서를 불러올 수 없습니다. 권한과 연결을 확인한 뒤 탐색을 다시 열어 주세요.';}
 }
 function showLimit(reason,id=selected){if(reason==='section_limit')limitedDocuments.add(id);else eventLimitHit=true;draw();const doc=state?.documents.get(selected);if(doc)renderReader(doc,true);message.setAttribute('role','alert');}
 function rejected(cause){if(cause.message==='exploration_limit'){showLimit(cause.message);return;}clear();root.hidden=false;message.setAttribute('role','alert');message.textContent='탐색 기록을 확인할 수 없습니다. 다시 조회해 주세요.';}
 function select(id,mobile){if(!state?.documents.has(id))return;const previous=selected;selected=id;if(mobile)root.dataset.mobileReader='true';const doc=state.documents.get(id);renderReader(doc);draw();if(doc.state==='discovered'){try{if(previous&&previous!==id)emit('relation',{edge:{from:previous,to:id,kind:'traversal',label:'이어서 탐색'}});}catch(cause){rejected(cause);return;}read(id);}}
 function accept(value){
  let source;try{source=canonical(value);}catch{return false;}
  if(source?.schema_version!==1||!Array.isArray(source.events))return false;
  clear();globalView=false;clearResultDocument();document.querySelector('.result').hidden=true;ws.hidden=true;root.hidden=false;message.removeAttribute('role');
  // A narrow widget draws smaller nodes in closer columns so two columns fit without panning.
  const narrow=viewport.clientWidth>0&&viewport.clientWidth<560;nodeW=narrow?140:190;step=narrow?164:250;x0=narrow?12:40;canvas.style.setProperty('--node-w',nodeW+'px');
  if(source.outcome==='empty'){message.textContent=source.message||'관련 Context가 없습니다.';return true;}
  try{state=workspaceRuntime.create(source.scope);}catch{message.textContent='탐색 범위를 확인할 수 없습니다.';return true;}
  ref=value?._meta?.exploration?.project_ref;sequence=0;try{for(const event of source.events)apply(event);}catch(cause){selected=[...state.documents.keys()][0];rejected(cause);return true;}
  if(state.documents.size===1){const original=source.events.find(e=>e.type==='read')?.document;if(original){root.hidden=true;showResultDocument({content:[{type:'text',text:JSON.stringify(original)}],_meta:{activity:{action:'read',outcome:'retrieved',context_id:original.id,project_id:original.project_id,revision:original.revision,project_ref:ref}}});return true;}}
  draw();fit();const first=[...state.documents.keys()][0];if(first)select(first,false);return true;
 }
 const controls=root.querySelectorAll('.graph-controls button');controls[0].onclick=()=>{zoom=Math.max(.35,zoom-.15);transform();};controls[1].onclick=()=>{zoom=Math.min(2.5,zoom+.15);transform();};controls[2].onclick=()=>{zoom=1;pan={x:0,y:0};transform();};
 viewport.onpointerdown=e=>{if(e.target.closest('button'))return;drag={x:e.clientX,y:e.clientY,pan:{...pan}};viewport.setPointerCapture(e.pointerId);};viewport.onpointermove=e=>{if(!drag)return;pan={x:drag.pan.x+e.clientX-drag.x,y:drag.pan.y+e.clientY-drag.y};transform();};viewport.onpointerup=()=>{drag=null;};viewport.onpointercancel=()=>{drag=null;};
 window.durumariExplorer={accept,clear};
})();
