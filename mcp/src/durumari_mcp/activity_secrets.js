/* Secret cards: names, versions, states and request progress only. A value is never drawn or typed here;
   every change that needs one happens on the secure screen the buttons open. */
(()=>{
 const d=window.durumariDomain;if(!d)return;const {el,card,badge,button}=d;
 // Server operation states fold into four groups the person reads:
 // waiting for them, done, declined (cancelled on the secure screen) and expired.
 const states={awaiting_user:'승인 대기',ready_for_review:'승인 대기',needs_review:'확인 대기',queued:'처리 중',running:'처리 중',completed:'승인됨',cancelled:'거절됨',expired:'만료됨',failed:'실패',
  pending:'승인 대기',approval_required:'승인 대기',approved:'승인됨',rejected:'거절됨',denied:'거절됨',canceled:'거절됨'};
 const waitingStates=['awaiting_user','ready_for_review','needs_review','pending','approval_required'];
 const terminal=['completed','cancelled','canceled','expired','failed','approved','rejected','denied'];
 const stateOf=source=>{const raw=text(source.state,40)||text(source.status,40),at=Date.parse(source.expires_at||'');
  return !terminal.includes(raw)&&Number.isFinite(at)&&at<=Date.now()?'expired':raw;};
 const kinds={create:'새 비밀값 등록',replace:'비밀값 교체',metadata:'정보 변경',delete:'비밀값 삭제',read:'값 확인',import:'가져오기'};
 const text=(value,max=120)=>typeof value==='string'?value.slice(0,max):'';
 // the secure screen opens outside the host. When the card regains focus after it, the card reads the
 // state again (names and states only, the same tool and filter as the card it replaces) so a save there shows here.
 let refresh=null;
 function reread(meta,source){
  const args=meta.project_ref?{project_ref:meta.project_ref}:{};
  if(meta.tool==='durumari_secret_list')return ()=>dataTool('durumari_secret_list',typeof source.id==='string'?{...args,secret_id:source.id}:args);
  if(typeof source.operation_id==='string')return ()=>dataTool('durumari_secret_status',{...args,operation_id:source.operation_id});
  return null;
 }
 function armRefresh(meta,source){
  const load=reread(meta,source);if(!load)return;const token=d.epoch;
  refresh=async()=>{refresh=null;if(token!==d.epoch)return;try{const value=await load();if(token!==d.epoch)return;
   if(meta.tool==='durumari_secret_list')listCard(value.items!==undefined?value:{items:[value]},meta,source);else statusCard(value,{...meta,tool:'durumari_secret_status'});}catch{}};
 }
 const wake=()=>{if(document.visibilityState==='visible'&&refresh)refresh();};
 window.addEventListener('focus',wake);document.addEventListener('visibilitychange',wake);
 let current={meta:{},source:{}};
 const open=async url=>{await openLink(url);armRefresh(current.meta,current.source);};
 function opener(parent,label,secretId,meta){
  if(!meta.project_ref)return;
  button(parent,label,async()=>{const value=await dataTool('durumari_secret_browser',{project_ref:meta.project_ref,request_key:crypto.randomUUID(),...(secretId?{secret_id:secretId}:{})});if(!value.browser_url)throw new Error('url');await open(value.browser_url);});
 }
 function listCard(source,meta,origin=source){
  current={meta,source:origin};
  const items=Array.isArray(source.items)?source.items:[];
  d.start('비밀값','이름과 상태만 보여 줘요. 값은 보안 화면에서만 확인해요.');
  const head=card('','');opener(head,'보안 화면 열기','',meta);
  if(!items.length){card('등록된 비밀값이 없어요','보안 화면에서 새 비밀값을 등록할 수 있어요.');return true;}
  const tiers={recovery:'복구용',runtime:'실행용',development:'개발용'},envs={live:'Live',preview:'Preview',development:'개발'};
  for(const item of items){
   const label=text(item.label)||'이름 없음',c=card(label,'');
   if(Number.isSafeInteger(item.revision))badge(c,'버전 '+item.revision);
   if(text(item.kind,40))badge(c,text(item.kind,40));
   if(tiers[item.tier])badge(c,tiers[item.tier]);
   if(envs[item.environment])badge(c,envs[item.environment]);
   if(item.expires_on)badge(c,'만료 '+text(item.expires_on,40));
   if(typeof item.id==='string')opener(c,'보안 화면 열기',item.id,meta);
  }
  if(source.next_after)card('더 있어요','다음 목록은 도구를 다시 호출해 주세요.');
  return true;
 }
 function statusCard(source,meta){
  current={meta,source};
  const state=stateOf(source);
  d.start('비밀값 요청','요청 진행 상태입니다. 승인과 값 입력은 보안 화면에서 해요.');
  const panel=card(text(source.label)||kinds[source.kind]||'요청 결과','');
  badge(panel,states[state]||'확인 필요');
  if(Number.isSafeInteger(source.revision))badge(panel,'버전 '+source.revision);
  if(Number.isSafeInteger(source.total)&&source.total>0)panel.append(el('p','진행 '+(Number.isSafeInteger(source.processed)?source.processed:0)+' / '+source.total,'domain-meta'));
  if(state==='failed'&&text(source.error_code,40))panel.append(el('p','실패 사유 '+text(source.error_code,40),'domain-meta'));
  if(source.expires_at&&!terminal.includes(state))panel.append(el('p','만료 '+text(source.expires_at,40),'domain-meta'));
  const waiting=waitingStates.includes(state);
  if(waiting&&typeof source.secure_url==='string')button(panel,'보안 화면 열기',()=>open(source.secure_url));
  else if(waiting)opener(panel,'보안 화면 열기','',meta);
  return true;
 }
 d.handlers.push((source,meta)=>{
  if(meta.tool==='durumari_secret_list')return source.items!==undefined&&!source.id?listCard(source,meta):source.id?listCard({items:[source]},meta,source):false;
  if(meta.tool==='durumari_secret_status'||meta.tool==='durumari_secret_request')return statusCard(source,meta);
  return false;
 });
})();
