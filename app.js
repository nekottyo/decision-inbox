'use strict';

const priorities = { urgent: 'すぐ判断', normal: '通常', later: 'あとでよい' };
const statuses = { pending: '処理待ち', processing: '対応中', completed: '完了' };
const ranks = { urgent: 0, normal: 1, later: 2 };
const drafts = new Map();
let cases = [];
let view = 'pending';
let selectedId = null;
let loaded = false;
let refreshing = false;
let saving = false;
let answerVersion = 0;
let selectedSnapshot = '';
const $ = selector => document.querySelector(selector);
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const dateText = value => new Intl.DateTimeFormat('ja-JP', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value));

function notify(message) { $('#notice').textContent = message; }
function showError(message) { $('#error').textContent = message; $('#error').hidden = false; }
function safeUrl(value, image = false) {
  if (typeof value !== 'string' || /[\s\\\u0000-\u001f]/.test(value)) return null;
  if (image && /^\/assets\/[a-zA-Z0-9_-]+\.(png|jpg|jpeg|gif|webp|svg)$/.test(value)) return value;
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

async function api(path, body) {
  const response = await fetch(path, body === undefined ? { cache: 'no-store' } : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || 'サーバーに接続できませんでした');
    error.status = response.status;
    throw error;
  }
  return data;
}

function visibleCases() {
  return cases.filter(item => (view === 'done') === Boolean(item.answer))
    .filter(item => $('#priority').value === 'all' || item.priority === $('#priority').value)
    .sort((a, b) => ranks[a.priority] - ranks[b.priority] || Date.parse(a.created_at) - Date.parse(b.created_at) || a.id.localeCompare(b.id));
}
function badge(item) { return `<span class="badge ${escapeHtml(item.priority)}">${escapeHtml(priorities[item.priority])}</span>`; }
function answerTitle(item) { return item.options.find(option => option.id === item.answer?.choice)?.title || ''; }

function renderList() {
  $('#pending-count').textContent = cases.filter(item => !item.answer).length;
  $('#done-count').textContent = cases.filter(item => item.answer).length;
  $('#list-title').textContent = view === 'pending' ? '判断待ち' : '判断済み';
  document.querySelectorAll('[data-view]').forEach(button => { button.setAttribute('aria-pressed', String(button.dataset.view === view)); });
  const items = visibleCases();
  $('#empty-list').hidden = items.length > 0;
  const list = $('#case-list');
  const activeId = document.activeElement?.dataset.case;
  const scroll = list.scrollLeft;
  list.innerHTML = items.map(item => `<li><button type="button" class="case-button" data-case="${escapeHtml(item.id)}" aria-current="${item.id === selectedId}"><span class="case-topline">${badge(item)}</span><span class="case-title">${escapeHtml(item.title)}</span><span class="case-meta">${escapeHtml(item.source)} · ${escapeHtml(dateText(item.created_at))}</span>${item.answer ? `<span class="case-answer">回答：${escapeHtml(answerTitle(item))}<br>後続処理：${escapeHtml(statuses[item.answer.status])}</span>` : ''}</button></li>`).join('');
  list.scrollLeft = scroll;
  if (activeId) [...list.querySelectorAll('[data-case]')].find(button => button.dataset.case === activeId)?.focus({ preventScroll: true });
}

function linksMarkup(item) {
  const links = item.links.filter(link => safeUrl(link.url));
  if (!links.length) return '';
  return `<section class="related" aria-label="関連リンク"><h3 class="section-label">関連する PR・issue</h3><ul>${links.map(link => `<li><a href="${escapeHtml(safeUrl(link.url))}" target="_blank" rel="noopener noreferrer"><span class="link-kind">${escapeHtml({ pr: 'PR', issue: 'issue', other: '参考' }[link.kind])}</span>${escapeHtml(link.label)}<span class="optional">別タブで開く</span></a></li>`).join('')}</ul></section>`;
}
function imageMarkup(item) {
  if (!item.image?.alt || !safeUrl(item.image.url, true)) return '';
  return `<figure><img src="${escapeHtml(safeUrl(item.image.url, true))}" alt="${escapeHtml(item.image.alt)}" referrerpolicy="no-referrer"><figcaption>${escapeHtml(item.image.alt)}</figcaption></figure>`;
}
function optionContent(option, item) {
  return `<strong>${escapeHtml(option.title)}${item.recommended === option.id ? '<span class="recommended">推奨</span>' : ''}</strong><small>利点：${escapeHtml(option.benefits)}<br>懸念：${escapeHtml(option.concerns)}</small>`;
}
function resultMarkup(item) {
  const answer = item.answer;
  return `<section class="result" aria-labelledby="result-heading"><h3 id="result-heading">記録した判断</h3><dl><dt>回答</dt><dd>${escapeHtml(answerTitle(item))}</dd><dt>メモ</dt><dd>${escapeHtml(answer.memo || 'メモなし')}</dd><dt>判断時刻</dt><dd><time datetime="${escapeHtml(answer.decided_at)}">${escapeHtml(dateText(answer.decided_at))}</time></dd><dt>後続処理</dt><dd id="processing-status"><span class="badge">${escapeHtml(statuses[answer.status])}</span></dd></dl><p class="followup">判断の確定後に依頼元が処理を引き受け、完了を記録します。</p><details><summary>候補とトレードオフを確認</summary>${item.options.map(option => `<p>${optionContent(option, item)}</p>`).join('')}</details></section>`;
}

function renderDetail(focus = false) {
  const item = cases.find(item => item.id === selectedId);
  selectedSnapshot = JSON.stringify(item);
  if (!item) {
    $('#detail').innerHTML = `<div class="empty-detail"><h2>${view === 'pending' ? '判断待ちの案件はありません' : '判断済みの案件はありません'}</h2><p>${view === 'pending' ? '依頼が届くと、この一覧に表示されます。別の優先度の案件も確認できます。' : '判断を確定すると、回答と後続処理をここで確認できます。'}</p></div>`;
  } else {
    const draft = drafts.get(item.id) || { choice: '', memo: '' };
    const timing = [item.deadline ? `期限：${dateText(item.deadline)}` : '', item.urgency_reason].filter(Boolean).join(' · ');
    $('#detail').innerHTML = `<header class="detail-header"><div class="detail-kicker">${badge(item)}<span>${item.answer ? '判断済み' : '判断待ち'}</span></div><h2>${escapeHtml(item.title)}</h2><p class="source">依頼元：${escapeHtml(item.source)} <span>· 受付 ${escapeHtml(dateText(item.created_at))}</span></p>${timing ? `<p class="deadline ${escapeHtml(item.priority)}">${escapeHtml(timing)}</p>` : ''}</header><section class="context" aria-labelledby="why-heading"><h3 class="section-label" id="why-heading">判断が必要な理由</h3><p class="reason">${escapeHtml(item.reason)}</p></section>${item.background ? `<section class="background-section"><h3 class="section-label">背景</h3><p class="background">${escapeHtml(item.background)}</p></section>` : ''}${imageMarkup(item)}${linksMarkup(item)}<section class="recommendation" aria-labelledby="recommend-heading"><h3 id="recommend-heading">依頼元の推奨</h3><p>${escapeHtml(item.recommendation)}</p></section>${item.answer ? resultMarkup(item) : `<form id="decision-form"><fieldset><legend>どう進めますか？</legend>${item.options.map(option => `<label class="option"><input type="radio" name="choice" value="${escapeHtml(option.id)}" required ${draft.choice === option.id ? 'checked' : ''}><span>${optionContent(option, item)}</span></label>`).join('')}</fieldset><label class="memo-label" for="memo">判断のメモ<span class="optional">任意 · 2,000 文字まで</span></label><textarea id="memo" name="memo" maxlength="2000" placeholder="条件や、次に気をつけてほしいことなど">${escapeHtml(draft.memo)}</textarea><div class="form-actions"><button type="button" id="defer" class="button">あとで判断する</button><button type="submit" id="confirm" class="button primary" ${!draft.choice ? 'disabled' : ''}>この内容で確定する</button></div><p class="form-hint">候補を選ぶと確定できます</p></form>`}`;
    if (!item.answer) {
      const form = $('#decision-form');
      form.addEventListener('input', () => {
        drafts.set(item.id, { choice: form.elements.choice.value, memo: $('#memo').value });
        $('#confirm').disabled = !form.elements.choice.value || saving;
      });
      form.addEventListener('submit', async event => {
        event.preventDefault();
        if (saving || !form.elements.choice.value) return;
        const answer = { choice: form.elements.choice.value, memo: $('#memo').value };
        drafts.set(item.id, answer);
        saving = true;
        answerVersion += 1;
        $('#confirm').disabled = true;
        try {
          const updated = await api(`/api/decisions/${encodeURIComponent(item.id)}/answer`, answer);
          cases = cases.map(candidate => candidate.id === updated.id ? updated : candidate);
          drafts.delete(item.id);
          selectedId = visibleCases()[0]?.id || null;
          $('#error').hidden = true;
          renderList();
          renderDetail(true);
          notify(`「${item.title}」の判断を保存しました。後続処理は「処理待ち」です。`);
        } catch (error) {
          showError(error.status === 409 ? 'この案件は別の画面ですでに判断されています。入力はこの画面に残しています。一覧から案件を選び直して、記録済みの判断を確認してください。' : `判断を保存できませんでした。入力はこの画面に残っています。${error.message}。接続を確認して再試行してください。`);
        } finally {
          saving = false;
          if ($('#confirm')) $('#confirm').disabled = !$('#decision-form').elements.choice.value;
        }
      });
      $('#defer').addEventListener('click', () => {
        if (saving) return;
        const items = visibleCases();
        const index = items.findIndex(candidate => candidate.id === item.id);
        selectedId = items[(index + 1) % items.length]?.id || null;
        renderList(); renderDetail(true);
        notify(`「${item.title}」は判断待ちに残しました。入力途中の内容は、このページを開いている間は保持されます。`);
      });
    }
  }
  if (focus) $('#detail').focus();
}

async function refresh() {
  if (refreshing || saving) return;
  refreshing = true;
  const version = answerVersion;
  try {
    const data = await api('/api/decisions');
    if (saving || version !== answerVersion) return;
    const previous = JSON.stringify(cases);
    cases = data.decisions;
    $('#mode-label').textContent = data.demo ? 'ローカルデモ' : 'ローカル';
    $('#mode-notice').textContent = data.demo ? 'デモ専用の一時データです。実際の依頼や処理キューとは分離されています。' : '判断はこの端末に保存されます。回答後の処理は、依頼元が引き受けます。';
    if (!loaded) {
      loaded = true;
      selectedId = visibleCases()[0]?.id || null;
      $('#error').hidden = true;
      renderList(); renderDetail();
    } else if (previous !== JSON.stringify(cases)) {
      renderList();
      const item = cases.find(candidate => candidate.id === selectedId);
      if (item?.answer && $('#processing-status')) {
        $('#processing-status').textContent = statuses[item.answer.status];
      }
      if (!selectedId) renderDetail();
      if (item && JSON.stringify(item) !== selectedSnapshot && $('#decision-form')) {
        notify('表示中の案件に変更があります。入力を保持しています。一覧から案件を選び直すと最新の状態を確認できます。');
      } else {
        notify('新着または処理状態の変更があります。一覧を更新しました。');
      }
    }
  } catch {
    showError('案件を読み込めませんでした。表示中の内容と入力は保持しています。接続を確認してください。自動で再試行します。');
  } finally { refreshing = false; }
}

$('#case-list').addEventListener('click', event => {
  const button = event.target.closest('[data-case]');
  if (!button || saving) return;
  selectedId = button.dataset.case;
  renderList(); renderDetail(true);
});
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => {
  if (saving) return;
  view = button.dataset.view;
  selectedId = visibleCases()[0]?.id || null;
  renderList(); renderDetail(true);
}));
$('#priority').addEventListener('change', () => {
  if (saving) return;
  const items = visibleCases();
  if (!items.some(item => item.id === selectedId)) selectedId = items[0]?.id || null;
  renderList(); renderDetail();
  notify(`${priorities[$('#priority').value] || 'すべての優先度'}：${items.length} 件を表示しています。`);
});
refresh();
setInterval(refresh, 5000);
