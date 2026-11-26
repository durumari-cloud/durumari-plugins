(()=>{
 const d=window.durumariDomain;if(!d)return;const {el,card,badge,button,root,sourceButton}=d;
 d.handlers.push((source,meta)=>{
  const receipt=source.result;
  if(meta.tool==='durumari_resource_status'&&!receipt?.kind){
   const state=source.state,token=d.start('브라우저 작업 결과 확인하기','동일한 작업의 현재 상태입니다. 완료 영수증이 없으면 저장 완료로 표시하지 않습니다.');
   const panel=card(({pending:'선택·검토 대기',expired:'작업 만료',canceled:'작업 취소'})[state]||'결과 확인 필요','');
   if(source.browser_url)button(panel,'진행 중인 작업 열기',()=>openLink(source.browser_url));
   const task=source.task_ref||meta.task_ref;if(task&&state==='pending')button(panel,'작업 결과 다시 확인',async()=>{const value=await dataTool('durumari_resource_status',{task_ref:task,wait_seconds:0});if(token===d.epoch)d.accept({content:[{type:'text',text:JSON.stringify(value)}],_meta:{domain:meta}});});return true;
  }
  if(meta.tool==='durumari_context_import'||(meta.tool==='durumari_resource_status'&&receipt?.kind==='import')){
   const state=source.state||'pending',completed=state==='completed'&&receipt?.kind==='import'&&Number.isInteger(receipt.resource_count)&&receipt.resource_count===(receipt.resources||[]).length;
   const token=d.start(completed?'선택한 자료를 Context로 가져왔어요':'자료를 미리 보고 가져오기','원문은 보존합니다. 원문과 정리 결과의 대응·중복·제외 항목은 검토 화면에서 확인합니다.');
   const panel=card(completed?'가져오기 완료':({pending:'선택·검토 대기',expired:'작업 만료',canceled:'작업 취소'})[state]||'결과 확인 필요','');
   if(completed){badge(panel,receipt.resource_count+'개 저장 확인');for(const resource of receipt.resources){const row=card('Context · 버전 '+resource.revision,'',panel);sourceButton(row,meta.project_ref,{context_id:resource.id,revision:resource.revision});if(resource.sha256){const details=el('details','');details.append(el('summary','원문 대응 식별 정보'),el('p',resource.sha256));row.append(details);}}}
   else{panel.append(el('p','검토 화면을 열거나 요청을 생성한 것만으로 가져오기가 완료되지는 않습니다.'));}
   if(typeof source.browser_url==='string')button(panel,'원문·정리 결과 검토하기',()=>openLink(source.browser_url));
   const task=source.task_ref||meta.task_ref;if(task&&state==='pending')button(panel,'가져오기 결과 다시 확인',async()=>{const value=await dataTool('durumari_resource_status',{task_ref:task,wait_seconds:0});if(token===d.epoch)d.accept({content:[{type:'text',text:JSON.stringify(value)}],_meta:{domain:{...meta,tool:'durumari_context_import',task_ref:task}}});});
   return true;
  }
  if(meta.tool!=='durumari_context_explore'||meta.view!=='meeting'||!Array.isArray(source.events))return false;
  d.start('회의에서 남긴 맥락 살펴보기','이미 저장된 회의 기록과 연결된 결정·할 일·용어 후보입니다. 이 화면에서 녹음이나 전사를 시작하지 않습니다.');
  let state;try{state=workspaceRuntime.create(source.scope);}catch{card('회의 기록을 찾지 못했습니다','저장된 회의 Context를 선택해 주세요.');return true;}for(const event of source.events)state=workspaceRuntime.apply(state,event);
  const grid=el('div','','domain-diff'),original=card('회의 원문','',grid),results=card('연결된 결정·작업·용어','',grid);root.append(grid);
  for(const doc of state.documents.values()){
   const raw=source.events.filter(e=>e.type==='read'&&e.document?.id===doc.id).at(-1)?.document;
   if(doc.state==='read'){const panel=card(doc.title,'',raw&&['decision','action','glossary'].includes(raw.kind)?results:original);panel.append(d.body(d.documentBody(doc)));badge(panel,'조회한 기록 · 버전 '+doc.revision);sourceButton(panel,meta.project_ref,{context_id:doc.id,revision:doc.revision});for(const section of doc.sections.slice(0,20))if(section.id)sourceButton(panel,meta.project_ref,{context_id:doc.id,revision:doc.revision,section_id:section.id,sha256:section.sha256||undefined});}
   else{const panel=card(doc.title,'아직 읽지 않은 관련 기록입니다.',results);sourceButton(panel,meta.project_ref,{context_id:doc.id,revision:doc.revision});}
  }
  if(!state.documents.size)card('저장된 회의 기록이 없습니다','추론한 회의 내용이나 미실행 전사 결과는 표시하지 않습니다.');return true;
 });
})();
