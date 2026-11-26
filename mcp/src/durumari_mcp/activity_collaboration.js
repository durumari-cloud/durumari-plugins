(()=>{
 const d=window.durumariDomain;if(!d)return;const {el,card,badge,button,root,sourceButton}=d;
 const states={open:'진행 중',completed:'요청 완료',cancelled:'취소됨',confirmed:'사람이 확인함',declined:'반려됨',deferred:'보류됨',changes_requested:'수정 요청',reviewed:'검토됨'};
 d.handlers.push((source,meta)=>{
  if(meta.tool.startsWith('durumari_request_')){
   d.start('함께 검토하고 결정하기','Agent 메모와 사람의 실제 결정을 구분합니다.');const request=source.request||source;
   const panel=card(request.title||'검토·결정 요청',request.purpose||'');if(request.state)badge(panel,states[request.state]||request.state);if(request.revision)badge(panel,'버전 '+request.revision);if(source.applied_revision)badge(panel,'요청 저장 확인 · 버전 '+source.applied_revision);if(request.blocking_reason)panel.append(el('p',request.blocking_reason,'domain-error'));
   for(const item of request.items||[]){const row=card(item.question||'검토 항목',item.details||'',panel);if(item.needs_recheck)badge(row,'다시 확인 필요');if(item.decision){badge(row,states[item.decision.outcome]||'결정 상태 확인 필요');row.append(el('p',item.decision.reason||item.decision.text||''));}else badge(row,'사람의 판단 대기');for(const reference of item.references||[])sourceButton(row,meta.project_ref,reference);}
   const notes=request.latest_agent_note||request.latest_note;if(notes){const row=card('Agent의 검토 메모',notes.text||'',panel);badge(row,'보고 · 사람의 승인 아님');}
   if(source.web_url)button(panel,'검토·결정 화면 열기',()=>openLink(source.web_url));const id=request.id||source.request_id||meta.request_id;if(id){const token=d.epoch;button(panel,'현재 요청 다시 확인',async()=>{let value;try{value=await dataTool('durumari_request_read',{request_id:id,project_ref:meta.project_ref});if(value?.error)throw new Error('request_unavailable');}catch{// Access may have been revoked: hide the earlier request instead of keeping it as current.
    if(token!==d.epoch)return;d.start('요청 상태를 확인할 수 없습니다','현재 권한으로 요청을 다시 읽지 못해 이전에 표시한 내용을 숨겼습니다.');card('현재 요청을 표시하지 않습니다','권한과 연결을 확인한 뒤 요청을 다시 열어 주세요.');return;}
    if(token===d.epoch)d.accept({content:[{type:'text',text:JSON.stringify(value)}],_meta:{domain:{...meta,tool:'durumari_request_read'}}});});}
   return true;
  }
  if(meta.tool!=='durumari_context_explore'||!['resume','report'].includes(meta.view)||!Array.isArray(source.events))return false;
  d.start(meta.view==='resume'?'이어서 할 일을 살펴보기':'작업 결과와 근거 확인하기','현재 조회한 작업 기록입니다. 문서의 보고를 실제 실행·검증·배포 성공으로 바꾸어 표시하지 않습니다.');
  let state;try{state=workspaceRuntime.create(source.scope);}catch{card('조회한 작업이 없습니다','작업 기록이나 결과 보고를 선택해 주세요.');return true;}for(const event of source.events)state=workspaceRuntime.apply(state,event);
  const grid=el('div','','domain-grid');root.append(grid);
  const category=meta.view==='resume'?/목표|요약|현재|남음|남은|차단|진행|결정/:/구현|테스트|검증|리뷰|배포|결과|미충족|산출물/;
  const stages=['구현','테스트','리뷰','배포'],stagesOf=section=>stages.filter(stage=>(section.title||'').includes(stage));
  // A line written in a record is a report. Nothing here is executed, re-run, or independently verified by this view.
  function block(section,parent){const part=card(section.title||'조회한 기록',section.text||'',parent);for(const line of (section.text||'').split('\n')){const match=line.match(/^[-*] \[([ xX])\] (.+)$/);if(match){const label=el('label','');const box=el('input','');box.type='checkbox';box.disabled=true;box.checked=match[1].toLowerCase()==='x';label.append(box,document.createTextNode(match[2]+' (문서 기록)'));part.append(label);}}return part;}
  for(const doc of state.documents.values()){if(doc.state!=='read')continue;const panel=card(doc.title,'',grid);badge(panel,'기록된 보고 · 버전 '+doc.revision);
   const matched=doc.sections.filter(s=>category.test(s.title)),sections=matched.length?matched:doc.sections;let rest=sections;
   if(meta.view==='report'){
    const staged=card('단계별 보고','구현·테스트·리뷰·배포는 각각 따로 표시합니다. 한 단계의 보고가 다른 단계나 독립 검증을 뜻하지 않습니다.',panel);
    for(const stage of stages){const own=sections.filter(s=>{const found=stagesOf(s);return found.length===1&&found[0]===stage;});const row=card(stage,'',staged);row.dataset.stage=stage;badge(row,own.length?'문서의 보고 · 독립 검증 아님':'보고 기록 없음');for(const section of own)block(section,row);}
    rest=sections.filter(s=>stagesOf(s).length!==1);
    for(const section of rest.filter(s=>stagesOf(s).length>1)){const mixed=block(section,panel);badge(mixed,'여러 단계가 섞인 기록 · 단계별 상태로 나누지 않음');}
    rest=rest.filter(s=>stagesOf(s).length===0);
   }
   for(const section of rest)block(section,panel);
   sourceButton(panel,meta.project_ref,{context_id:doc.id,revision:doc.revision});}
  if(!grid.childNodes.length)card('읽은 작업 기록이 없습니다','문서를 먼저 읽어 주세요.');return true;
 });
})();
