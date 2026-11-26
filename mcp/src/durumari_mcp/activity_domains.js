/* Only server-returned data enters these views. No decision or restore is submitted. */
(()=>{
 const css=document.createElement('style');css.textContent=`.domain-view{display:grid;gap:var(--space-3);align-content:start;min-width:0}.domain-view p{white-space:pre-wrap;overflow-wrap:anywhere}.domain-grid{display:grid;grid-template-columns:minmax(0,1fr);gap:var(--space-3);min-width:0}.domain-meta{color:var(--text-muted);font-size:13px}.domain-error{color:var(--w-danger-text);font-size:13px}.domain-diff{display:grid;grid-template-columns:minmax(0,1fr);gap:var(--space-3);min-width:0}.domain-diff>.before{border-top:3px solid var(--danger-border)}.domain-diff>.after{border-top:3px solid var(--success-fill)}.domain-diff pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.7;margin:0}.domain-timeline{display:grid;gap:var(--space-3);margin-left:6px;padding-left:var(--space-4);border-left:2px solid var(--neutral-border);min-width:0}.domain-timeline>.w-card{position:relative}.domain-timeline>.w-card::before{content:'';position:absolute;left:-24px;top:20px;width:10px;height:10px;border-radius:var(--radius-pill);background:var(--surface);border:2px solid var(--brand)}.w-btnrow{display:flex;flex-wrap:wrap;gap:var(--space-2)}.domain-view .w-card>label{display:flex;align-items:center;gap:var(--space-2);margin:0;font-size:13px}@container widget (min-width:640px){.domain-grid{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}.domain-diff{grid-template-columns:1fr 1fr}}`;document.head.append(css);
 const root=document.createElement('section');root.className='domain-view';root.setAttribute('aria-label','작업 결과 상세');root.hidden=true;document.querySelector('main').append(root);
 const handlers=[];let epoch=0;
 const el=(tag,text,cls)=>{const n=document.createElement(tag);n.textContent=typeof text==='string'?text:'';if(cls)n.className=cls;return n;};
 function clear(){epoch++;root.hidden=true;root.replaceChildren();}
 function start(title,subtitle){clear();globalView=false;clearResultDocument();window.durumariExplorer?.clear();document.querySelector('.result').hidden=true;ws.hidden=true;root.hidden=false;const header=el('header','','w-view-head');header.append(el('h2',title),el('p',subtitle));root.append(header);return epoch;}
 // A card inside a card would nest boxes: it becomes a ruled row of the outer card instead.
 function card(title,body,parent=root){const nested=parent.classList?.contains('w-card')||parent.classList?.contains('w-row');const n=el('section','',nested?'w-row domain-card':'w-card domain-card');if(title)n.append(el('h3',title));if(body)n.append(el('p',body));parent.append(n);return n;}
 function button(parent,title,fn){const b=el('button',title);b.onclick=async()=>{b.disabled=true;try{await fn();}catch{if(parent.isConnected)parent.append(el('p','결과를 확인하지 못했습니다. 현재 권한과 연결을 확인해 주세요.','domain-error'));}finally{b.disabled=false;}};let row=parent.lastElementChild?.classList.contains('w-btnrow')?parent.lastElementChild:null;if(!row){row=el('div','','w-btnrow');parent.append(row);}row.append(b);return b;}
 // Status words pick the chip tone; the word is always written, so the colour never carries the meaning alone.
 const toneOf=text=>/실패|거절|반려|만료됨|작업 만료|취소|철회|충돌|오류|불가/.test(text)?'danger':/대기|필요|일부|보고|후보|검토|보류|미확인|아님|미리보기|처리 중|확인 중|대체/.test(text)?'warning':/완료|저장|현재 효력|채택|승인|확인함|확인됨|읽음|가져왔/.test(text)?'success':'brand';
 function badge(parent,text){let row=[...parent.children].find(child=>child.classList.contains('w-chips'));if(!row){row=el('div','','w-chips');const title=[...parent.children].find(child=>/^H[1-6]$/.test(child.tagName));if(title)title.after(row);else parent.prepend(row);}const chip=el('span',text,'w-chip');chip.dataset.tone=toneOf(text);row.append(chip);}
 function documentBody(doc){return(doc.sections||[]).map(s=>s.text||'').join('\n\n');}
 // Document text is drawn with the shared Markdown rules; a title the view already shows is not repeated.
 function body(text,title){return durumariMarkdown.section(typeof text==='string'?text:'',title);}
 async function read(ref,id,revision,view,after){const args={project_ref:ref,context_id:id,revision,view:view||'document',budget_tokens:4000};if(after!=null&&after!=='')args.after=after;return dataTool('durumari_context_read',args);}
 function sourceButton(parent,ref,source){if(!source?.context_id)return;button(parent,source.section_id?'이 구간 원문 보기':'근거 원문 보기',async()=>{const token=epoch;const doc=await read(ref,source.context_id,source.revision);if(token!==epoch)return;if(doc.id!==source.context_id||(source.revision&&doc.revision!==source.revision))throw new Error('source_changed');
  if(source.section_id){// Move to the exact cited section; a changed section hash is never shown as the cited text.
   let page=doc,section=(page.sections||[]).find(item=>item?.id===source.section_id);const seen=new Set();
   // The cited section may sit beyond the first budgeted page; follow the same revision's cursor.
   while(!section&&page.next_after!=null&&page.next_after!==''&&!seen.has(page.next_after)&&seen.size<50){seen.add(page.next_after);page=await read(ref,source.context_id,doc.revision,'document',page.next_after);if(token!==epoch)return;if(page.id!==source.context_id||page.revision!==doc.revision)throw new Error('source_changed');section=(page.sections||[]).find(item=>item?.id===source.section_id);}
   if(!section){parent.append(el('p','인용한 구간을 원문에서 찾지 못했습니다.','domain-error'));return;}
   if(source.sha256&&section.sha256!==source.sha256){parent.append(el('p','인용한 구간의 내용이 바뀌어 원문을 표시하지 않습니다.','domain-error'));return;}
   const block=card(doc.title||'근거 원문','',parent);block.append(body(section.text));const details=el('details','');details.append(el('summary','출처 정보'),el('p',[section.id,section.sha256,'버전 '+doc.revision].filter(Boolean).join(' · ')));block.append(details);return;}
  card(doc.title||'근거 원문','',parent).append(body(documentBody(doc)));});}
 function accept(value){const meta=value?._meta?.domain;if(!meta)return false;let source;try{source=canonical(value);}catch{return false;}if(source?.error)return false;for(const handler of handlers)if(handler(source,meta,value))return true;return false;}
 function history(source,meta){
  if(meta.tool!=='durumari_context_version'||meta.action!=='history')return false;
  start('문서의 변화 살펴보기','판본과 당시 상태를 확인합니다. 조회만으로 복원되지 않습니다.');
  const list=el('div','','domain-timeline');root.append(list);
  for(const item of source.items||[]){const c=card('버전 '+item.revision,[item.state,item.created_at,item.summary].filter(x=>typeof x==='string').join(' · '),list);sourceButton(c,meta.project_ref,{context_id:meta.context_id,revision:item.revision});}
  if(!list.childNodes.length)card('표시할 이력이 없습니다','현재 권한에서 반환된 이력이 없습니다.');return true;
 }
 function changes(source,meta){
  if(!['durumari_context_save','durumari_context_revise','durumari_context_operation','durumari_context_version'].includes(meta.tool)||meta.action==='history')return false;
  const valid=source?.status==='stored'&&typeof source.project_id==='string'&&typeof source.context_id==='string'&&Number.isSafeInteger(source.revision)&&source.revision>0;
  if(!valid)return false;
  const action=meta.action||({'durumari_context_save':'create','durumari_context_revise':'patch'}[meta.tool])||'recover';
  const title=({create:'새 Context를 저장했어요',patch:'Context를 수정했어요',delete:'Context를 삭제했어요',restore:'과거 판본을 복원했어요',recover:'저장 결과를 확인했어요'})[action]||'저장 결과';
  const token=start(title,'서버 저장 확인 · 버전 '+source.revision),status=card('저장 완료','해당 작업 시점의 결과입니다.');badge(status,'버전 '+source.revision);
  if(meta.project_ref&&source.request_key)button(status,'브라우저에서 결과 보기',async()=>{const task=await dataTool('durumari_context_result_browser',{project_ref:meta.project_ref,stored_request_key:source.request_key,request_key:crypto.randomUUID()});if(token!==epoch)return;await openLink(task.browser_url);if(token===epoch)status.append(el('p','같은 저장 결과를 브라우저에서 열었습니다.','domain-meta'));});
  if(action==='delete'){button(status,'문서 이력 보기',async()=>{const value=await dataTool('durumari_context_version',{action:'history',context_id:source.context_id,project_ref:meta.project_ref});if(token===epoch)history(value,{...meta,action:'history',context_id:source.context_id});});return true;}
  const content=card('저장된 문서','원문을 불러오는 중입니다.');
  (async()=>{try{
   const after=await read(meta.project_ref,source.context_id,source.revision);if(token!==epoch)return;
   if(after.id!==source.context_id||after.project_id!==source.project_id||after.revision!==source.revision)throw new Error('receipt_mismatch');content.replaceChildren(el('h3',after.title||'Context'),body(documentBody(after)));if(after.truncated)badge(content,'일부 범위');
   if(action==='patch'&&Number.isSafeInteger(meta.base_revision)&&meta.base_revision<source.revision){
    const before=await read(meta.project_ref,source.context_id,meta.base_revision);if(token!==epoch)return;
    if(before.id!==source.context_id||before.project_id!==source.project_id||before.revision!==meta.base_revision)throw new Error('before_unverified');
    const comparison=el('div','','domain-diff');root.append(comparison);const a=card('변경 전 · 버전 '+before.revision,'',comparison),b=card('변경 후 · 버전 '+after.revision,'',comparison);a.classList.add('before');b.classList.add('after');a.append(body(documentBody(before)));b.append(body(documentBody(after)));if(before.truncated||after.truncated)root.append(el('p','반환된 일부 범위를 비교합니다. 전체 변경 내역은 아닙니다.','domain-meta'));
   }
  }catch{if(token===epoch)content.append(el('p','저장은 확인됐지만 원문 또는 이전 판본을 확인하지 못했습니다. 비교 내용은 표시하지 않습니다.','domain-meta'));}})();return true;
 }
 handlers.push(history,changes);
 window.durumariDomain={accept,clear,handlers,start,card,button,badge,el,root,read,sourceButton,documentBody,body,get epoch(){return epoch;}};
})();
