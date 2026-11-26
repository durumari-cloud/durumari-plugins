(()=>{
 const d=window.durumariDomain;if(!d)return;const {el,card,badge,button,root,sourceButton}=d;
 const css=document.createElement('style');css.textContent='.term-expressions{display:flex;gap:6px;flex-wrap:wrap}.term-expressions:empty{display:none}.term-expression{padding:2px 10px;border:1px solid var(--border);border-radius:var(--radius-pill);background:var(--surface-app);font-size:13px}.term-definition{font-size:15px;line-height:1.7}.term-relations{display:flex;gap:6px;flex-wrap:wrap}.term-relations:empty{display:none}.term-related{border-left:3px solid var(--brand);padding:4px 10px;background:var(--brand-faint);border-radius:var(--radius-sm);font-size:13px}.term-candidate{border-style:dashed}';document.head.append(css);
 const statuses={current:'현재 채택',candidate:'후보',review:'검토 중',deprecated:'이전 용어',pending:'판단 대기',deferred:'보류',accepted:'채택됨',rejected:'반려',reviewed:'이 언어 표현 확인됨','agent-draft':'AI 번역 초안','human-draft':'사람 작성 초안',source:'원문 표현'};
 const kinds={create:'새 용어 제안',update:'정의 변경 제안',delete:'용어 삭제 제안',pronunciation:'발음 표현 추가 제안',misheard:'오인식 표현 추가 제안',translation:'번역 추가 제안',discovery:'반복 관찰된 표현'};
 function term(value,parent,meta){
  const translations=value.translations||{},languages=Object.keys(translations);let language=languages.includes(value.source_language)?value.source_language:languages[0];
  const panel=card('','',parent),header=el('div',''),body=el('div','','w-stack');panel.append(header,body);
  function render(){
   const expression=translations[language]||{};header.replaceChildren(el('h3',expression.name||'이름 미확인 용어'));body.replaceChildren();badge(body,statuses[value.state]||'상태 미확인');badge(body,statuses[expression.status]||'표현 확인 여부 미확인');if(language)badge(body,language);
   body.append(el('p',expression.definition||'정의가 아직 없습니다.','term-definition'));if(value.expansion)body.append(el('p',value.expansion,'domain-meta'));if(value.scopes?.length)body.append(el('p','적용 범위 · '+value.scopes.join(' · '),'domain-meta'));
   const names=el('div','','term-expressions');for(const name of expression.aliases||[])names.append(el('span',name,'term-expression'));body.append(names);if(expression.example)body.append(el('p','사용 예 · '+expression.example));
   for(const key of ['transliterations','pronunciation','misheard'])if(expression[key]?.length){const details=el('details','');details.append(el('summary',({transliterations:'음역',pronunciation:'보고된 발음',misheard:'오인식 표현'})[key]),el('p',expression[key].join(' · ')));body.append(details);}
   const related=el('div','','term-relations');for(const item of value.related_names||[])related.append(el('span',item.names?.[language]||item.name||item.id,'term-related'));body.append(related);
   for(const evidence of value.evidence||[]){const block=card('근거',evidence.excerpt||'',body);sourceButton(block,meta.project_ref,{context_id:evidence.context_id,revision:evidence.context_revision});}
  }
  const choose=el('div','');for(const lang of languages)button(choose,lang,()=>{language=lang;render();});panel.append(choose);render();return panel;
 }
 function candidate(value,parent,meta){
  // Titles follow the server's candidate kinds (glossary Proposal.kind plus the deterministic `discovery`); an unknown kind is never shown raw.
  const panel=card(kinds[value.kind]||'정리 제안',value.reason||'정리 이유가 아직 없습니다.',parent);panel.classList.add('term-candidate');badge(panel,statuses[value.status]||'후보');if(value.blocked)badge(panel,'선행 확인 필요');
  if(value.term)term(value.term,panel,meta);if(value.expression)panel.append(el('p',value.expression));for(const evidence of value.evidence||[])sourceButton(panel,meta.project_ref,{context_id:evidence.context_id,revision:evidence.context_revision});
  if(value.term_id&&value.term){const token=d.epoch;button(panel,'현재 정의와 비교',async()=>{const current=await dataTool('durumari_glossary_read',{view:'term',project_ref:meta.project_ref,options:{id:value.term_id}});if(token!==d.epoch)return;if(current.id!==value.term_id||current.revision!==value.base_revision){panel.append(el('p','기준 판본이 바뀌었습니다. 최신 정의를 다시 검토해 주세요.','domain-meta'));return;}const compare=el('div','','domain-diff');panel.append(compare);const before=card('현재 정의','',compare),after=card('제안한 정의','',compare);term(current,before,meta);term(value.term,after,meta);});}
  if(typeof value.web_url==='string')button(panel,'검토 화면 열기',()=>openLink(value.web_url));return panel;
 }
 d.handlers.push((source,meta)=>{
  if(!['durumari_glossary_read','durumari_glossary_submit'].includes(meta.tool))return false;
  if(meta.tool==='durumari_glossary_submit'||meta.view==='operation'){
   if(source.status!=='stored')return false;d.start('용어사전 변경을 기록했어요','서버 저장 결과입니다. 후보 저장과 현재 용어 채택은 구분합니다.');
   const panel=card(source.candidate?'정리 제안 저장':'용어 항목 저장','');if(source.candidate){badge(panel,statuses[source.candidate.status]||'후보');badge(panel,'버전 '+source.candidate.revision);}if(source.term)badge(panel,'항목 버전 '+source.term.revision);
   const token=d.epoch;button(panel,'현재 결과 다시 확인',async()=>{let value=await dataTool('durumari_glossary_read',{view:source.candidate?'candidates':'term',project_ref:meta.project_ref,options:{id:source.candidate?.id||source.term?.id}});if(token!==d.epoch)return;if(source.candidate)value={...value,items:(value.items||[]).filter(row=>row.id===source.candidate.id)};else if(value.id!==source.term?.id)throw new Error('term_changed');render(value,{...meta,tool:'durumari_glossary_read',view:source.candidate?'candidates':'term'});});return true;
  }
  return render(source,meta);
 });
 function render(source,meta){
  d.start(meta.view==='candidates'?'표현을 개념별로 정리하기':'우리 공간의 용어 살펴보기','정의·표현·적용 범위와 실제 확인 상태를 함께 봅니다.');const grid=el('div','','domain-grid');root.append(grid);
  if(source.counts){const counts=card('정리 현황','');for(const [state,count]of Object.entries(source.counts))if(Number.isInteger(count))badge(counts,(statuses[state]||state)+' '+count);}
  const rows=Array.isArray(source.items)?source.items:[source];for(const row of rows.slice(0,50)){if(meta.view==='candidates')candidate(row,grid,meta);else if(row.translations)term(row,grid,meta);else{const panel=card(row.expression||('버전 '+(row.revision||'')),row.reason||'',grid);if(Number.isFinite(row.occurrences))badge(panel,'보고된 언급 '+row.occurrences+'회');if(row.status)badge(panel,statuses[row.status]||row.status);}}
  if(!rows.length)card('표시할 항목이 없습니다','현재 검색·적용 범위에서 반환된 항목이 없습니다.');if(source.next_after)root.append(el('p','일부 항목을 표시했습니다. 다음 범위는 용어사전에서 이어서 조회해 주세요.','domain-meta'));return true;
 }
})();
