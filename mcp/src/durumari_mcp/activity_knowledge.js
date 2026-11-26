(()=>{
 const d=window.durumariDomain;if(!d)return;const {el,card,badge,button,root,sourceButton}=d;
 const names={synthesis:'흩어진 근거를 함께 살펴보기',impact:'변경 전에 연결된 범위 살펴보기',decision:'지금의 결정에 이르기까지'};
 d.handlers.push((source,meta)=>{
  if(meta.tool!=='durumari_context_explore'||!Object.hasOwn(names,meta.view)||!Array.isArray(source.events))return false;
  d.start(names[meta.view],meta.view==='impact'?'연결 관계는 영향 검토의 출발점입니다. 연결만으로 실제 충돌을 단정하지 않습니다.':'반환된 원문에 기록된 내용과 근거입니다. Agent 보고와 독립 검증은 구분합니다.');
  if(source.outcome==='empty'){card('관련 Context가 없습니다',source.message||'검색어나 출발 문서를 다시 선택해 주세요.');return true;}let state;try{state=workspaceRuntime.create(source.scope);}catch{card('탐색 범위를 확인할 수 없습니다','다시 조회해 주세요.');return true;}for(const event of source.events)state=workspaceRuntime.apply(state,event);
  const documents=[...state.documents.values()],read=documents.filter(doc=>doc.state==='read'),grid=el('div','',meta.view==='decision'?'domain-timeline':'domain-grid');root.append(grid);
  for(const doc of read){
   const raw=source.events.filter(e=>e.type==='read'&&e.document?.id===doc.id).at(-1)?.document||{};
   const panel=card(doc.title,'',grid);badge(panel,'버전 '+doc.revision);badge(panel,'문서 기록');
   if(meta.view==='decision'){// Only the server's active status is current, and a recorded replacement relation withholds it.
    const replaced=[...state.edges.values()].some(edge=>edge.to===doc.id&&edge.label==='supersedes');
    badge(panel,raw.efficacy?.status==='active'&&replaced?'대체 관계 있음 · 효력 확인 필요':({active:'현재 효력',stale:'과거 판본',replaced:'대체됨',withdrawn:'철회됨'})[raw.efficacy?.status]||'효력 확인 필요');}
   const selected=doc.sections.filter(s=>/요약|결론|발견|근거|미결|질문|충돌|대안|결정/.test(s.title));const sections=selected.length?selected:doc.sections;
   for(const section of sections){const part=el('section','');if(section.title)part.append(el('h4',section.title));part.append(d.body(section.text,section.title));if(section.id||section.sha256){const details=el('details','');details.append(el('summary','출처 정보'),el('p',[section.id,section.sha256,'버전 '+doc.revision].filter(Boolean).join(' · ')));part.append(details);}if(section.id)sourceButton(part,meta.project_ref,{context_id:doc.id,revision:doc.revision,section_id:section.id,sha256:section.sha256||undefined});panel.append(part);}
   sourceButton(panel,meta.project_ref,{context_id:doc.id,revision:doc.revision});
  }
  if(meta.view==='impact'){
   const panel=card('연결된 검토 범위','');for(const edge of state.edges.values()){const from=state.documents.get(edge.from),to=state.documents.get(edge.to);const row=card((from?.title||edge.from)+' → '+(to?.title||edge.to),edge.label,panel);badge(row,'연결됨 · 영향 확인 필요');sourceButton(row,meta.project_ref,{context_id:to.id,revision:to.revision});}
  }
  const graph=card('근거 사이의 연결','');button(graph,'탐색 그래프 펼치기',()=>{d.clear();window.durumariExplorer.accept({content:[{type:'text',text:JSON.stringify(source)}],_meta:{exploration:{project_ref:meta.project_ref}}});});
  if(!read.length)card('읽은 근거가 없습니다','발견한 문서를 먼저 읽으면 해당 범위의 내용이 표시됩니다.');
  return true;
 });
})();
