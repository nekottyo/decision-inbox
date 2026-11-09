#!/usr/bin/env bash
set -euo pipefail

check_url=${1:-http://127.0.0.1:4317}
check_session="decision-inbox-selfcheck-$$"
ab() { agent-browser --session "$check_session" "$@"; }
js() {
  { printf '%s\n' '(() => { const assert = (ok, message) => { if (!ok) throw new Error(message); };'; cat; printf '\n})();\n'; } | ab eval --stdin >/dev/null
}
cleanup() {
  check_result=$?
  trap - EXIT
  if ! js <<'JS'
if (window.__originalSetItem) Storage.prototype.setItem = window.__originalSetItem;
const backup = JSON.parse(window.name || '{}');
if (backup.selfCheck) {
  if (backup.answer === null) localStorage.removeItem('decision-inbox-demo:v1');
  else localStorage.setItem('decision-inbox-demo:v1', backup.answer);
  assert(localStorage.getItem('decision-inbox-demo:v1') === backup.answer, '元のデモ回答の復元');
  localStorage.removeItem(backup.sentinel);
  window.name = backup.windowName;
}
JS
  then
    printf '%s\n' '失敗：デモ回答の復元に失敗しました' >&2
    check_result=1
  fi
  ab close >/dev/null || check_result=1
  exit "$check_result"
}
ab open "$check_url" >/dev/null
ab wait '#detail' >/dev/null
trap cleanup EXIT
js <<'JS'
const sentinel = 'decision-inbox-selfcheck:' + crypto.randomUUID();
window.name = JSON.stringify({ selfCheck: true, answer: localStorage.getItem('decision-inbox-demo:v1'), sentinel, windowName: window.name });
localStorage.setItem(sentinel, '保護対象');
localStorage.removeItem('decision-inbox-demo:v1');
JS
ab reload >/dev/null
ab wait '#confirm' >/dev/null
js <<'JS'
assert(document.querySelector('#confirm').disabled, '未選択では確定不可');
assert(document.querySelector('[data-case="review"]').getAttribute('aria-current') === 'true', 'すぐ判断を先頭に選択');
assert(document.querySelector('#pending-count').textContent === '3', '初期の判断待ちは 3 件');
JS
ab check 'input[value="minimal"]' >/dev/null
ab fill '#memo' '検証用メモ <安全に表示>' >/dev/null
js <<'JS'
assert(!document.querySelector('#confirm').disabled, '選択後は確定可能');
window.__originalSetItem = Storage.prototype.setItem;
Storage.prototype.setItem = function (key, value) {
  if (key === 'decision-inbox-demo:v1') throw new DOMException('検証用の保存失敗', 'QuotaExceededError');
  return window.__originalSetItem.call(this, key, value);
};
JS
ab click '#confirm' >/dev/null
js <<'JS'
assert(!document.querySelector('#error').hidden, '保存失敗を通知');
assert(document.querySelector('#pending-count').textContent === '3', '保存失敗時は未確定');
assert(document.querySelector('#memo').value === '検証用メモ <安全に表示>', '失敗時も入力を保持');
assert(localStorage.getItem('decision-inbox-demo:v1') === null, '失敗した回答を書き込まない');
Storage.prototype.setItem = window.__originalSetItem;
delete window.__originalSetItem;
JS
ab click '#confirm' >/dev/null
js <<'JS'
assert(document.querySelector('#pending-count').textContent === '2', '確定後に件数を更新');
assert(document.querySelector('#detail h2').textContent === '段階的な公開の範囲を決める', '次の未判断へ移動');
assert(document.activeElement.id === 'detail', '次の判断票へフォーカス移動');
const saved = JSON.parse(localStorage.getItem('decision-inbox-demo:v1'));
assert(saved.review.choice === 'minimal' && saved.review.status === '処理待ち', '判断と後続処理を分離');
JS
ab reload >/dev/null
ab wait '#detail' >/dev/null
ab click '[data-view="done"]' >/dev/null
ab click '[data-case="review"]' >/dev/null
js <<'JS'
const detail = document.querySelector('#detail');
assert(detail.textContent.includes('検証用メモ <安全に表示>'), '再読み込み後も回答とメモを保持');
assert(detail.querySelector('time'), '判断時刻を表示');
assert(detail.textContent.includes('処理待ち'), '後続処理の状態を表示');
assert(document.querySelector('#case-list [data-case]').dataset.case === 'review', '判断済みも優先度順');
JS
ab click '[data-view="pending"]' >/dev/null
ab select '#priority' later >/dev/null
js <<'JS'
assert(document.querySelectorAll('#case-list li').length === 1, '優先度で絞り込み');
assert(document.querySelector('#detail h2').textContent === '導入ガイドの見せ方を選ぶ', '絞り込んだ案件の表示');
JS
ab select '#priority' all >/dev/null
ab click '[data-case="release"]' >/dev/null
ab click '#defer' >/dev/null
js <<'JS'
assert(document.querySelector('#pending-count').textContent === '2', '保留で確定しない');
assert(document.querySelector('#detail h2').textContent === '導入ガイドの見せ方を選ぶ', '保留後は次の案件を表示');
window.confirm = () => true;
JS
ab click '#reset' >/dev/null
js <<'JS'
const backup = JSON.parse(window.name);
assert(localStorage.getItem(backup.sentinel) === '保護対象', '初期化で他のキーを保護');
assert(localStorage.getItem('decision-inbox-demo:v1') === null, 'デモ回答だけを初期化');
assert(document.querySelector('#pending-count').textContent === '3', '初期状態に復帰');
localStorage.setItem('decision-inbox-demo:v1', '{破損データ');
JS
ab reload >/dev/null
ab wait '#confirm' >/dev/null
ab check 'input[value="minimal"]' >/dev/null
js <<'JS'
assert(!document.querySelector('#error').hidden, '破損データの読み込み失敗を通知');
assert(document.querySelector('#confirm').disabled, '破損データの上書きを防止');
assert(localStorage.getItem('decision-inbox-demo:v1') === '{破損データ', '破損データも勝手に消さない');
JS
ab set viewport 390 844 >/dev/null
js <<'JS'
assert(document.documentElement.scrollWidth <= innerWidth, 'モバイルで横にはみ出さない');
const list = document.querySelector('.inbox').getBoundingClientRect();
const detail = document.querySelector('#detail').getBoundingClientRect();
assert(detail.top >= list.bottom - 1, 'モバイルで 1 列に配置');
JS
printf '%s\n' '合格：候補・保存失敗・確定・フォーカス・再読込・優先度・保留・初期化・破損データ保護・モバイル'
