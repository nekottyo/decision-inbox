'use strict';

const storageKey = 'decision-inbox-demo:v1';
const priorities = { urgent: 'すぐ判断', normal: '通常', later: 'あとでよい' };
const ranks = { urgent: 0, normal: 1, later: 2 };
const cases = [
  {
    id: 'review', number: '01', title: 'レビュー指摘にどう対応するか', source: '開発チーム / 認証まわりの改善', priority: 'urgent', received: '本日 09:10', receivedAt: '2026-11-09T09:10:00+09:00',
    deadline: '本日 15:00 まで · 修正方針が決まるまでレビューが止まっています',
    reason: '今回の変更に権限チェックの共通化を含めるか、必要な修正に絞って先に進めるかを決めてください。',
    background: 'レビューで 3 か所の重複が指摘されました。現状でも権限チェックは動作しますが、共通化には呼び出し元の追加確認が必要です。今回のリリースは明日を予定しています。',
    recommended: 'minimal', recommendation: '必要な修正を先に反映し、共通化は次の変更へ。リリース前に広い影響範囲を持ち込まず、指摘も追跡できます。',
    options: [
      { id: 'minimal', title: '必要な修正に絞り、共通化は別の変更で行う', tradeoff: '利点：今回の確認範囲を限定できる。懸念：重複は残るため、次の作業として追跡が必要。' },
      { id: 'refactor', title: '今回の変更で共通化まで進める', tradeoff: '利点：重複と修正漏れを減らせる。懸念：呼び出し元の再確認でリリースが 1 日遅れる可能性。' },
      { id: 'discuss', title: 'レビュアーと先に方針をすり合わせる', tradeoff: '利点：指摘の意図を確認できる。懸念：打ち合わせまで修正に着手できない。' }
    ], diagram: true
  },
  {
    id: 'release', number: '02', title: '段階的な公開の範囲を決める', source: 'プロダクトチーム / 新しい検索画面', priority: 'normal', received: '本日 09:35', receivedAt: '2026-11-09T09:35:00+09:00',
    deadline: '明日 12:00 まで · 公開設定の準備に半日が必要です',
    reason: '新しい検索画面を、社内だけに公開するか、一部のお客様にも公開するかを決めてください。',
    background: '基本動作の確認は終わっています。大量のデータを持つお客様の操作感はまだ確認できていません。初回公開後はエラー率と問い合わせを確認します。',
    recommended: 'internal', recommendation: 'まず社内で公開。日常業務での利用を確認してからお客様への公開に進むと、問題の切り分けが容易です。',
    options: [
      { id: 'internal', title: '社内の利用者から始める', tradeoff: '利点：影響を限定して確認できる。懸念：お客様固有の使い方を知るまでに時間がかかる。' },
      { id: 'pilot', title: '協力いただけるお客様 5 社にも公開する', tradeoff: '利点：実際の業務で検証できる。懸念：問い合わせへの即時対応が必要。' }
    ]
  },
  {
    id: 'guide', number: '03', title: '導入ガイドの見せ方を選ぶ', source: 'サポートチーム / 初回設定ガイド', priority: 'later', received: '本日 10:20', receivedAt: '2026-11-09T10:20:00+09:00',
    deadline: '今週中で可 · 他の作業を止めていません',
    reason: '初回設定で迷うお客様向けに、手順を画面内に置くか、専用のガイドにまとめるかを決めてください。',
    background: '問い合わせの多い手順は 4 ステップです。画面内の説明はすぐ読めますが、詳しい補足まで載せると設定項目が見つけにくくなります。',
    recommended: 'linked', recommendation: '画面には短い案内を置き、詳しい手順はガイドにまとめます。設定画面の見通しを保ちながら、補足も届けられます。',
    options: [
      { id: 'linked', title: '短い案内と、詳しいガイドへのリンクを置く', tradeoff: '利点：画面をすっきり保てる。懸念：詳しい手順は別のページで読む必要がある。' },
      { id: 'inline', title: '4 ステップを設定画面に表示する', tradeoff: '利点：ページを移動せず読める。懸念：慣れた利用者にも説明が表示され続ける。' }
    ]
  },
  {
    id: 'export', number: '04', title: '出力ファイルの既定形式を選ぶ', source: '分析チーム / レポート出力', priority: 'normal', received: '昨日 16:40', receivedAt: '2026-11-08T16:40:00+09:00',
    deadline: '判断済み · 担当チームが対応しています',
    reason: '表計算ソフトで開く利用が多いため、既定の出力形式を選ぶ必要がありました。',
    background: '自動処理で使う利用者もいるため、どちらを既定にしても形式の切り替えは残します。',
    recommended: 'csv', recommendation: '現在の連携との互換性を保つため、既定は CSV のままにします。',
    options: [
      { id: 'csv', title: 'CSV を既定にする', tradeoff: '利点：既存の連携を保てる。懸念：表示形式は利用者側で整える必要がある。' },
      { id: 'xlsx', title: 'Excel 形式を既定にする', tradeoff: '利点：表の見た目を保てる。懸念：既存の自動処理では形式の指定が必要。' }
    ]
  }
];
const initialAnswers = { export: { choice: 'csv', memo: '既存の自動処理との互換性を優先。形式の切り替えは残す。', decidedAt: '2026-11-08T17:30:00+09:00', status: '対応中' } };
let answers = structuredClone(initialAnswers);
let view = 'pending';
let selectedId = 'review';
let readFailed = false;
const drafts = {};
const $ = selector => document.querySelector(selector);
const escapeHtml = value => String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));

function showError(message) {
  $('#error').textContent = message;
  $('#error').hidden = false;
}

function notify(message) {
  $('#notice').textContent = message;
}

function validAnswers(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  return Object.entries(value).every(([id, answer]) => {
    const item = cases.find(item => item.id === id);
    return item && answer && typeof answer === 'object' && !Array.isArray(answer)
      && item.options.some(option => option.id === answer.choice)
      && typeof answer.memo === 'string' && answer.memo.length <= 2000
      && typeof answer.decidedAt === 'string' && Number.isFinite(Date.parse(answer.decidedAt))
      && ['処理待ち', '対応中', '完了'].includes(answer.status);
  });
}

try {
  const saved = localStorage.getItem(storageKey);
  if (saved !== null) {
    const parsed = JSON.parse(saved);
    if (!validAnswers(parsed)) throw new Error('Invalid demo data');
    answers = parsed;
  }
} catch {
  readFailed = true;
  showError('保存済みのデモ回答を読み込めませんでした。既存の保存内容を守るため、確定を止めています。ブラウザーの保存設定を確認して再読み込みするか、デモの回答を初期化してください。');
}

function save(nextAnswers) {
  try {
    localStorage.setItem(storageKey, JSON.stringify(nextAnswers));
    answers = nextAnswers;
    $('#error').hidden = true;
    return true;
  } catch {
    showError('回答を保存できませんでした。判断は確定していません。入力内容はこの画面に残っています。ブラウザーの保存設定や空き容量を確認して、もう一度お試しください。');
    return false;
  }
}

function visibleCases() {
  return cases.filter(item => (view === 'done') === Boolean(answers[item.id]))
    .filter(item => $('#priority').value === 'all' || item.priority === $('#priority').value)
    .sort((a, b) => ranks[a.priority] - ranks[b.priority] || Date.parse(a.receivedAt) - Date.parse(b.receivedAt));
}

function badge(item) {
  return `<span class="badge ${item.priority}">${priorities[item.priority]}</span>`;
}

function renderList() {
  $('#pending-count').textContent = cases.length - Object.keys(answers).length;
  $('#done-count').textContent = Object.keys(answers).length;
  $('#list-title').textContent = view === 'pending' ? '判断待ち' : '判断済み';
  document.querySelectorAll('[data-view]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.view === view)));
  const items = visibleCases();
  $('#empty-list').hidden = items.length > 0;
  $('#case-list').innerHTML = items.map(item => {
    const answer = answers[item.id];
    return `<li><button type="button" class="case-button" data-case="${item.id}" aria-current="${item.id === selectedId}"><span class="case-topline">${badge(item)}</span><span class="case-title">${item.title}</span><span class="case-meta">${item.source.split(' / ')[0]} · ${item.received}</span>${answer ? `<span class="case-answer">回答：${item.options.find(option => option.id === answer.choice).title}<br>後続処理：${answer.status}</span>` : ''}</button></li>`;
  }).join('');
}

function diagram() {
  return `<figure><svg viewBox="0 0 520 88" role="img" aria-labelledby="flow-title flow-description"><title id="flow-title">推奨する修正の進め方</title><desc id="flow-description">必要な修正を確認して今回のリリースへ進めます。共通化は別の変更として追跡します。</desc><defs><marker id="arrow" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6" fill="var(--muted)"/></marker></defs><g fill="var(--surface)" stroke="var(--line)"><rect x="4" y="8" width="142" height="66" rx="5"/><rect x="191" y="8" width="138" height="66" rx="5"/><rect x="374" y="8" width="142" height="66" rx="5"/></g><g stroke="var(--muted)" marker-end="url(#arrow)"><path d="M150 41 H182"/><path d="M333 41 H365"/></g><g fill="currentColor" text-anchor="middle" font-size="12" font-family="sans-serif"><text x="75" y="36">必要な修正を確認</text><text x="75" y="55" fill="var(--muted)" font-size="10">今回の範囲を限定</text><text x="260" y="36">リリース</text><text x="260" y="55" fill="var(--muted)" font-size="10">明日の予定を維持</text><text x="445" y="36">共通化を別途実施</text><text x="445" y="55" fill="var(--muted)" font-size="10">追跡用の依頼を残す</text></g></svg><figcaption>進め方の比較資料：推奨案では、共通化を切り分けて今回の確認範囲を抑えます。</figcaption></figure>`;
}

function renderDetail(focus = false) {
  const item = cases.find(item => item.id === selectedId);
  if (!item) {
    $('#detail').innerHTML = `<div class="empty-detail"><h2>${view === 'pending' ? 'この一覧の判断は終わりました' : '判断済みの案件はありません'}</h2><p>${view === 'pending' ? '別の優先度の案件も確認できます。' : '判断を確定すると、回答と後続処理の状態をここで確認できます。'}</p></div>`;
  } else {
    const answer = answers[item.id];
    const draft = drafts[item.id] || { choice: '', memo: '' };
    $('#detail').innerHTML = `<header class="detail-header"><div class="detail-kicker">${badge(item)}<span>${answer ? '判断済み' : '判断待ち'}</span></div><h2>${item.title}</h2><p class="source">依頼元：${item.source} <span>· 受付 ${item.received}</span></p><p class="deadline ${item.priority}">${item.deadline}</p></header><section class="context" aria-labelledby="why-heading"><h3 class="section-label" id="why-heading">何を決めるか</h3><p class="reason">${item.reason}</p></section><details class="background-details"><summary>背景${item.diagram ? 'と参考図' : ''}を確認</summary><p class="background">${item.background}</p>${item.diagram ? diagram() : ''}</details><section class="recommendation" aria-labelledby="recommend-heading"><h3 id="recommend-heading">依頼元の推奨</h3><p>${item.recommendation}</p></section>${answer ? resultMarkup(item, answer) : `<form id="decision-form"><fieldset><legend>どう進めますか？</legend>${item.options.map(option => `<label class="option"><input type="radio" name="choice" value="${option.id}" required ${draft.choice === option.id ? 'checked' : ''}><span><strong>${option.title}${item.recommended === option.id ? '<span class="recommended">推奨</span>' : ''}</strong><small>${option.tradeoff}</small></span></label>`).join('')}</fieldset><label class="memo-label" for="memo">判断のメモ<span class="optional">任意 · 2,000 文字まで</span></label><textarea id="memo" name="memo" maxlength="2000" placeholder="条件や、次に気をつけてほしいことなど">${escapeHtml(draft.memo)}</textarea><div class="form-actions"><button type="button" id="defer" class="button">あとで判断する</button><button type="submit" id="confirm" class="button primary" ${!draft.choice || readFailed ? 'disabled' : ''}>この内容で確定する</button></div><p class="form-hint">${readFailed ? '保存内容を読み込めないため確定できません' : '候補を選ぶと確定できます'}</p></form>`}`;
    if (!answer) {
      $('#decision-form').addEventListener('input', () => {
        drafts[item.id] = { choice: $('#decision-form').elements.choice.value, memo: $('#memo').value };
        $('#confirm').disabled = !drafts[item.id].choice || readFailed;
      });
      $('#decision-form').addEventListener('submit', event => {
        event.preventDefault();
        const choice = $('#decision-form').elements.choice.value;
        if (readFailed || !item.options.some(option => option.id === choice)) return;
        const next = { ...answers, [item.id]: { choice, memo: $('#memo').value, decidedAt: new Date().toISOString(), status: '処理待ち' } };
        if (!save(next)) return;
        delete drafts[item.id];
        selectedId = visibleCases()[0]?.id || null;
        renderList();
        renderDetail(true);
        notify(`「${item.title}」の判断を保存しました。後続処理は「処理待ち」です。実際の依頼元への送信はありません。`);
      });
      $('#defer').addEventListener('click', () => {
        const items = visibleCases();
        const index = items.findIndex(candidate => candidate.id === item.id);
        selectedId = items[(index + 1) % items.length]?.id || null;
        renderList();
        renderDetail(true);
        notify(`「${item.title}」は判断待ちに残しました。${items.length === 1 ? 'この一覧の残りは 1 件です。' : '次の案件を表示しています。'}入力途中の内容は、このページを開いている間は保持されます。`);
      });
    }
  }
  if (focus) $('#detail').focus();
}

function resultMarkup(item, answer) {
  const choice = item.options.find(option => option.id === answer.choice);
  const decidedAt = new Intl.DateTimeFormat('ja-JP', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(answer.decidedAt));
  return `<section class="result" aria-labelledby="result-heading"><h3 id="result-heading">記録した判断</h3><dl><dt>回答</dt><dd>${choice.title}</dd><dt>メモ</dt><dd>${escapeHtml(answer.memo || 'メモなし')}</dd><dt>判断時刻</dt><dd><time datetime="${escapeHtml(answer.decidedAt)}">${decidedAt}</time></dd><dt>後続処理</dt><dd><span class="badge">${answer.status}</span></dd></dl><p class="followup">判断の確定と作業の完了は別です。表示中の後続処理状態はデモで、実際の作業には連動しません。</p><details><summary>候補とトレードオフを確認</summary>${item.options.map(option => `<p><strong>${option.title}</strong><br>${option.tradeoff}</p>`).join('')}</details></section>`;
}

$('#case-list').addEventListener('click', event => {
  const button = event.target.closest('[data-case]');
  if (!button) return;
  selectedId = button.dataset.case;
  renderList();
  renderDetail(true);
});

document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => {
  view = button.dataset.view;
  selectedId = visibleCases()[0]?.id || null;
  renderList();
  renderDetail(true);
}));

$('#priority').addEventListener('change', () => {
  const items = visibleCases();
  if (!items.some(item => item.id === selectedId)) selectedId = items[0]?.id || null;
  renderList();
  renderDetail();
  notify(`${priorities[$('#priority').value] || 'すべての優先度'}：${items.length} 件を表示しています。`);
});

$('#reset').addEventListener('click', () => {
  if (!window.confirm('このデモで記録した回答と入力途中のメモを初期状態に戻します。実際の案件や、他の保存データには影響しません。続けますか？')) return;
  try {
    localStorage.removeItem(storageKey);
  } catch {
    showError('デモの回答を初期化できませんでした。保存内容は変更していません。ブラウザーの保存設定を確認してください。');
    return;
  }
  answers = structuredClone(initialAnswers);
  readFailed = false;
  Object.keys(drafts).forEach(key => delete drafts[key]);
  view = 'pending';
  $('#priority').value = 'all';
  selectedId = 'review';
  $('#error').hidden = true;
  renderList();
  renderDetail(true);
  notify('デモの回答を初期状態に戻しました。他の保存データは変更していません。');
});

selectedId = visibleCases()[0]?.id || null;
renderList();
renderDetail();
