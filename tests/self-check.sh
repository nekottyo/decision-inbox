#!/usr/bin/env bash
set -euo pipefail

check_url=${1:?専用の空テスト DB を使用する server URL を指定してください}
check_session="decision-inbox-api-selfcheck-$$"
ab() { agent-browser --session "$check_session" "$@"; }
js() {
  { printf '%s\n' '(async () => { const assert = (ok, message) => { if (!ok) throw new Error(message); };'; cat; printf '\n})();\n'; } | ab eval --stdin
}
trap 'ab close >/dev/null' EXIT
ab open "$check_url" >/dev/null
ab wait '#detail' >/dev/null
js <<'JS'
const initial = await fetch('/api/decisions').then(r => r.json());
assert(initial.demo === false && initial.decisions.length === 0, '専用の空テスト DB のみ使用');
const request = (title, priority) => ({title, priority, source:'操作検証', reason:'選択する方針の確認', background:'背景を最初から表示する <確認>', options:[{id:'a',title:'小さく直す',benefits:'確認範囲が小さい',concerns:'重複が残る'},{id:'b',title:'まとめて直す',benefits:'重複を減らす',concerns:'公開が遅れる'}], recommended:'a',recommendation:'影響範囲を限定',links:[{url:'https://github.com/example/repo/pull/1',label:'対象 PR <確認>',kind:'pr'},{url:'https://github.com/example/repo/issues/2',label:'関連 issue',kind:'issue'}],image:{url:'/assets/demo-flow.svg',alt:'修正から公開までの参考図'}});
const add = async payload => {const r=await fetch('/api/decisions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}); assert(r.ok,'依頼登録'); return r.json();};
const urgent=await add(request('緊急の判断','urgent'));
const normal=await add(request('通常の判断','normal'));
window.name=JSON.stringify({urgent:urgent.id,normal:normal.id,request:request('新着の判断','urgent')});
return '合格：API に検証用の依頼を登録';
JS
ab eval 'window.location.href = "/?decision=" + JSON.parse(window.name).normal' >/dev/null
ab wait --fn "document.querySelector('#detail h2')?.textContent === '通常の判断'" >/dev/null
js <<'JS'
assert(document.querySelector('#case-list [aria-current="true"]').dataset.case===JSON.parse(window.name).normal,'Web deep link で対象の未判断を表示');
return '合格：未判断の deep link';
JS
ab eval 'window.location.href = "/?decision=missing"' >/dev/null
ab wait --fn "document.querySelector('#detail h2')?.textContent === '緊急の判断'" >/dev/null
ab open "$check_url" >/dev/null
ab wait '#confirm' >/dev/null
js <<'JS'
assert(document.querySelector('#detail h2').textContent==='緊急の判断','緊急順');
assert(document.querySelector('#confirm').disabled || document.querySelector('#confirm').getAttribute('aria-disabled')==='true','候補未選択では確定不可');
assert(document.querySelector('.background-section')?.textContent.includes('背景を最初から表示する <確認>'),'背景を初期表示して安全に描画');
assert(!document.querySelector('.background-section details'),'背景を折り畳まない');
const link=document.querySelector('.related a');
assert(link.href==='https://github.com/example/repo/pull/1' && link.target==='_blank' && link.rel.includes('noopener') && link.rel.includes('noreferrer'),'安全な PR リンク');
assert(link.textContent.includes('対象 PR <確認>'),'リンクラベルの安全な描画');
assert(document.querySelector('figure img').alt==='修正から公開までの参考図','画像の代替テキスト');
return '合格：優先度・候補必須・背景・関連リンク・画像';
JS
ab check 'input[value="a"]' >/dev/null
ab fill '#memo' '入力中のメモ <保持>' >/dev/null
js <<'JS'
window.originalFetch=window.fetch;
window.fetch=async function(path,options){if(String(path).endsWith('/answer')) throw new Error('保存失敗の検証'); return window.originalFetch.call(this,path,options);};
JS
ab scrollintoview '#confirm' >/dev/null
ab click '#confirm' >/dev/null
ab wait --fn "document.querySelector('#error').hidden === false && !document.querySelector('#confirm').disabled && document.querySelector('#confirm').getAttribute('aria-disabled') !== 'true'" >/dev/null
js <<'JS'
assert(document.querySelector('#memo').value==='入力中のメモ <保持>','保存失敗時にメモを保持');
assert(document.querySelector('#pending-count').textContent==='2','保存失敗を確定扱いにしない');
window.fetch=window.originalFetch;
const state=JSON.parse(window.name);
const r=await fetch('/api/decisions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(state.request)});
assert(r.ok,'新着を登録');
return '合格：保存失敗時の入力維持';
JS
ab wait --fn "document.querySelector('#pending-count').textContent === '3'" >/dev/null
js <<'JS'
assert(document.querySelector('#detail h2').textContent==='緊急の判断','新着時に案件を入れ替えない');
assert(document.querySelector('#memo').value==='入力中のメモ <保持>' && document.querySelector('input[value="a"]').checked,'更新時の候補とメモを保持');
return '合格：新着と入力中の案件を同時に保持';
JS
ab scrollintoview '#confirm' >/dev/null
ab click '#confirm' >/dev/null
ab wait --fn "document.querySelector('#pending-count').textContent === '2'" >/dev/null
js <<'JS'
assert(document.querySelector('#detail h2').textContent==='新着の判断','確定後は次件へ');
assert(document.activeElement.id==='detail','次件の判断票にフォーカス');
return '合格：確定・次件・フォーカス';
JS
ab click '#defer' >/dev/null
ab click '#priority' >/dev/null
ab find role option click --name '通常' >/dev/null
js <<'JS'
assert(document.querySelector('#detail h2').textContent==='通常の判断','あとで判断する操作');
assert(document.querySelectorAll('#case-list li').length===1 && document.querySelector('#pending-count').textContent==='2','優先度の絞り込みと保留件数');
return '合格：保留・優先度の絞り込み';
JS
ab click '#priority' >/dev/null
ab find role option click --name 'すべて' >/dev/null
ab reload >/dev/null
ab wait '[data-view="done"]' >/dev/null
ab click '[data-view="done"]' >/dev/null
ab wait '.result' >/dev/null
js <<'JS'
assert(document.querySelector('.result').textContent.includes('入力中のメモ <保持>'),'reload 後の回答保持');
assert(document.querySelector('#processing-status').textContent.includes('処理待ち'),'確定だけで完了にしない');
assert(document.querySelector('.related a') && document.querySelector('.background-section'),'判断済みでも背景とリンクを表示');
assert(document.querySelector('#undo-answer'),'処理開始前の回答を取り消せる');
return '合格：永続回答・判断済み表示';
JS
ab click '#undo-answer' >/dev/null
ab wait '#confirm' >/dev/null
js <<'JS'
assert(document.querySelector('#detail h2').textContent==='緊急の判断','取り消した案件を選択保持');
assert(document.querySelector('#memo').value==='入力中のメモ <保持>' && document.querySelector('input[value="a"]').checked,'取り消した回答を下書きに保持');
assert(document.querySelector('#pending-count').textContent==='3','判断待ちへ復帰');
return '合格：回答の取り消しと下書き保持';
JS
ab scrollintoview '#confirm' >/dev/null
ab click '#confirm' >/dev/null
ab wait --fn "document.querySelector('#pending-count').textContent === '2'" >/dev/null
ab click '[data-view="done"]' >/dev/null
ab wait '.result' >/dev/null
js <<'JS'
const claim=await fetch('/api/claim',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({worker:'ui-self-check'})}).then(r=>r.json());
assert(claim.decision.id===JSON.parse(window.name).urgent,'回答済みを claim');
const r=await fetch('/api/decisions/'+claim.decision.id+'/complete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:claim.token})});
assert(r.ok,'後続処理を完了');
return '合格：永続回答・判断済み表示・後続処理';
JS
ab wait --fn "document.querySelector('#processing-status').textContent.includes('完了')" >/dev/null
ab eval 'window.location.href = "/?decision=" + JSON.parse(window.name).urgent' >/dev/null
ab wait '.result' >/dev/null
js <<'JS'
assert(document.querySelector('#detail h2').textContent==='緊急の判断','判断済みへの deep link');
assert(document.querySelector('#processing-status').textContent.includes('完了'),'deep link でも後続処理状態を表示');
return '合格：判断済みの deep link';
JS
js <<'JS'
assert(!document.querySelector('#undo-answer'),'処理開始後は取り消せない');
const payload={...JSON.parse(window.name).request,title:'停止した判断',priority:'normal'};
const stopped=await fetch('/api/decisions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(r=>r.json());
await fetch('/api/decisions/'+stopped.id+'/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({choice:'b',memo:'停止確認'})});
const claim=await fetch('/api/claim',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({worker:'ui-stop-check'})}).then(r=>r.json());
assert(claim.decision.id===stopped.id,'停止対象を claim');
const reported=await fetch('/api/decisions/'+stopped.id+'/report',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:claim.token,status:'blocked',reason:'承認待ち <確認>',next_step:'条件を確認して再処理'})});
assert(reported.ok,'承認待ちを報告');
window.name=JSON.stringify({...JSON.parse(window.name),stopped:stopped.id});
window.location.href='/?decision='+stopped.id;
JS
ab wait '#retry-answer' >/dev/null
js <<'JS'
assert(document.querySelector('#processing-status').textContent.includes('承認・条件待ち'),'停止状態の表示');
assert(document.querySelector('#processing-reason').textContent==='承認待ち <確認>','停止理由を安全に表示');
assert(document.querySelector('#processing-next-step').textContent==='条件を確認して再処理','再開条件の表示');
assert(document.querySelector('.result').getBoundingClientRect().top < document.querySelector('.context').getBoundingClientRect().top,'停止理由を背景より先に表示');
assert(!document.querySelector('#undo-answer'),'一度処理を開始した案件の取り消し禁止');
return '合格：停止状態・理由・再開条件';
JS
ab click '#retry-answer' >/dev/null
ab wait --fn "document.querySelector('#processing-status').textContent.includes('処理待ち') && !document.querySelector('#retry-answer')" >/dev/null
js <<'JS'
assert(!document.querySelector('#undo-answer'),'再処理待ちでも取り消し禁止');
const item=await fetch('/api/decisions/'+JSON.parse(window.name).stopped).then(r=>r.json());
assert(item.answer.status==='pending' && !item.answer.reason,'明示操作で再処理待ちへ');
return '合格：再処理の明示依頼';
JS
ab click '[data-view="pending"]' >/dev/null
ab check 'input[value="a"]' >/dev/null
ab fill '#memo' '競合時にも保持するメモ' >/dev/null
js <<'JS'
const id=document.querySelector('#case-list [aria-current="true"]').dataset.case;
const r=await fetch('/api/decisions/'+id+'/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({choice:'b',memo:'別の画面で判断'})});
assert(r.ok,'別 client から先に確定');
JS
ab scrollintoview '#confirm' >/dev/null
ab click '#confirm' >/dev/null
ab wait --fn "document.querySelector('#error').textContent.includes('すでに判断') && !document.querySelector('#confirm').disabled && document.querySelector('#confirm').getAttribute('aria-disabled') !== 'true'" >/dev/null
js <<'JS'
assert(document.querySelector('#memo').value==='競合時にも保持するメモ' && document.querySelector('input[value="a"]').checked,'409 で入力を保持');
return '合格：二重判断の競合通知と入力保持';
JS
ab set viewport 390 844 >/dev/null
js <<'JS'
assert(document.documentElement.scrollWidth<=innerWidth,'モバイルで横にはみ出さない');
const list=document.querySelector('.inbox').getBoundingClientRect();
assert(document.querySelector('#detail').getBoundingClientRect().top>=list.bottom-1,'モバイルの 1 列配置');
assert(!document.querySelector('#reset'),'実データを削除するボタンなし');
return '合格：処理状態の定期更新・モバイル・削除操作なし';
JS
printf '%s\n' '合格：API 接続の UI self-check 全項目'
