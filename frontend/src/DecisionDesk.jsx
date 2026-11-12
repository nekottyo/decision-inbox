import { useState } from 'react';
import { Button } from '@astryxdesign/core/Button';
import { Badge } from '@astryxdesign/core/Badge';
import { TabList, Tab } from '@astryxdesign/core/TabList';
import { RadioList, RadioListItem } from '@astryxdesign/core/RadioList';
import { Selector } from '@astryxdesign/core/Selector';
import { TextArea } from '@astryxdesign/core/TextArea';
import './desk.css';

const priorities = { urgent: 'すぐ判断', normal: '通常', later: 'あとでよい' };
const statuses = { pending: '処理待ち', processing: '対応中', completed: '完了', blocked: '承認・条件待ち', failed: '失敗' };
const priorityOptions = [
  { value: 'all', label: 'すべて' },
  ...Object.entries(priorities).map(([value, label]) => ({ value, label })),
];
export const dateText = value => new Intl.DateTimeFormat('ja-JP', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value));
const answerTitle = item => item.options.find(option => option.id === item.answer?.choice)?.title || '';

export function safeUrl(value, image = false) {
  if (typeof value !== 'string' || /[\s\\\u0000-\u001f]/.test(value)) return null;
  if (image && /^\/assets\/[a-zA-Z0-9_-]+\.(png|jpg|jpeg|gif|webp|svg)$/.test(value)) return value;
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

function PriorityBadge({ priority }) {
  return <Badge label={priorities[priority]} variant={priority === 'urgent' ? 'orange' : 'neutral'} />;
}

function OptionLabel({ option, recommended }) {
  return <span className="option-label"><strong>{option.title}</strong>{recommended === option.id && <Badge label="推奨" variant="green" />}</span>;
}

function OptionDescription({ option }) {
  return <span className="option-description"><span>利点：{option.benefits}</span><span>懸念：{option.concerns}</span></span>;
}

// 開くたびに server から読み直すので、登録後に直した下書きも最新が見える。本文は HTML として描画しない
function AttachedFiles({ item }) {
  const [opened, setOpened] = useState(null);
  async function open(index) {
    const request = { index, loading: true };
    setOpened(request);
    // 先に開いた別のファイルの応答で表示を上書きしない
    const settle = result => setOpened(current => current === request ? { index, ...result } : current);
    try {
      const response = await fetch(`/api/decisions/${encodeURIComponent(item.id)}/files/${index}`, { cache: 'no-store' });
      const data = await response.json();
      settle(response.ok ? { content: data.content } : { error: data.error || '読み込めませんでした' });
    } catch {
      settle({ error: 'サーバーに接続できませんでした' });
    }
  }
  return <section className="related files" aria-labelledby="files-heading"><h3 id="files-heading">ファイル</h3>
    <ul>{item.files.map((file, index) => <li key={`${file.path}-${index}`}>
      <button type="button" className="file-open" aria-expanded={opened?.index === index} onClick={() => opened?.index === index ? setOpened(null) : open(index)}>
        <span>{file.label}</span><span className="link-target">{file.path}</span>
      </button>
      {opened?.index === index && (opened.loading ? <p className="file-status">読み込んでいます</p>
        : opened.error ? <p className="file-status" role="alert">表示できません：{opened.error}</p>
        : <pre className="file-content" tabIndex={0}>{opened.content}</pre>)}
    </li>)}</ul>
  </section>;
}

function RecordedAnswer({ item, saving, onUndo, onRetry }) {
  const answer = item.answer;
  const stopped = ['blocked', 'failed'].includes(answer.status);
  return <section className="result" aria-labelledby="result-heading">
    <h3 id="result-heading">記録した判断</h3>
    <dl>
      <dt>後続処理</dt><dd id="processing-status"><Badge label={statuses[answer.status]} variant={stopped ? 'orange' : answer.status === 'processing' ? 'info' : 'neutral'} /></dd>
      {answer.reason && <><dt>止まった理由</dt><dd id="processing-reason">{answer.reason}</dd></>}
      {answer.next_step && <><dt>再開に必要なこと</dt><dd id="processing-next-step">{answer.next_step}</dd></>}
      <dt>回答</dt><dd>{answerTitle(item)}</dd>
      <dt>メモ</dt><dd>{answer.memo || 'メモなし'}</dd>
      <dt>判断時刻</dt><dd><time dateTime={answer.decided_at}>{dateText(answer.decided_at)}</time></dd>
    </dl>
    <div className="answer-actions">
      {answer.can_undo && <Button id="undo-answer" label="回答を取り消す" type="button" onClick={onUndo} isDisabled={saving} />}
      {stopped && <Button id="retry-answer" label="再処理を依頼する" type="button" onClick={onRetry} isDisabled={saving} />}
    </div>
    <p className="followup">{stopped ? '再開に必要な条件を確認してから再処理を依頼してください。自動では再試行しません。' : answer.can_undo ? '依頼元が処理を始めるまでは、回答を取り消せます。' : '処理開始後の回答は取り消せません。後続処理の状態をここで確認できます。'}</p>
    <details className="answered-options"><summary>候補とトレードオフを確認</summary>
      {item.options.map(option => <div className="answered-option" key={option.id}><OptionLabel option={option} recommended={item.recommended} /><OptionDescription option={option} /></div>)}
    </details>
  </section>;
}

export default function DecisionDesk({
  items, counts, selectedItem, view, priority, draft, loading, saving, demo, notice, error,
  detailRef, onViewChange, onPriorityChange, onSelect, onDraftChange, onSubmit, onDefer, onUndo, onRetry,
  screen, onScreenChange, board, waitingTasks, taskError,
}) {
  const item = selectedItem;
  const links = item?.links.filter(link => safeUrl(link.url)) || [];
  const imageUrl = item?.image?.alt && safeUrl(item.image.url, true);
  const timing = item && [item.deadline ? `期限：${dateText(item.deadline)}` : '', item.urgency_reason].filter(Boolean).join(' · ');

  return <div className="decision-desk">
    <a className="skip-link" href="#detail">案件の詳細へ</a>
    <header className="desk-topbar">
      <h1><a href="/">Cue</a></h1>
      <nav className="desk-screens" aria-label="画面の切り替え">
        <button type="button" id="screen-decisions" aria-current={screen === 'decisions' ? 'page' : undefined} disabled={saving} onClick={() => onScreenChange('decisions')}>判断<span className="tab-count">{counts.pending}</span></button>
        <button type="button" id="screen-tasks" aria-current={screen === 'tasks' ? 'page' : undefined} disabled={saving} onClick={() => onScreenChange('tasks')}>task<span className="tab-count" title="人の判断待ちと人の操作待ちの件数">人待ち {waitingTasks}</span></button>
      </nav>
      <span id="mode-label"><Badge label={demo ? 'ローカルデモ' : 'ローカル'} variant="neutral" /></span>
    </header>
    <main className="desk-main">
      <p id="mode-notice" className="mode-notice">{demo ? 'デモ専用の一時データです。実際の依頼や処理キューとは分離されています。' : '判断はこの端末に保存されます。回答後の処理は、依頼元が引き受けます。'}</p>
      <div id="notice" className="desk-notice" role="status" aria-live="polite" aria-atomic="true">{notice}</div>
      <div id="error" className="desk-error" role="alert" hidden={!error}>{error}</div>
      {screen === 'tasks' ? <>
        <div id="task-error" className="desk-error" role="alert" hidden={!taskError}>{taskError}</div>
        {board}
        <footer className="desk-footer">task は CLI (task add / move / update) で更新します。この画面は読み取り専用で、5 秒ごとに最新の状態を取り込みます。</footer>
      </> : <><div className="desk-workspace">
        <aside className="inbox" aria-label="案件一覧">
          <div className="desk-tabs"><TabList aria-label="一覧の切り替え" value={view} onChange={onViewChange} layout="fill">
            <Tab value="pending" data-view="pending" label="判断待ち" endContent={<span id="pending-count" className="tab-count">{counts.pending}</span>} />
            <Tab value="done" data-view="done" label="判断済み" endContent={<span id="done-count" className="tab-count">{counts.done}</span>} />
          </TabList></div>
          <div className="desk-filter"><Selector id="priority" label="優先度" value={priority} onChange={onPriorityChange} options={priorityOptions} hasSearch={false} isDisabled={saving} /></div>
          <div className="list-heading"><h2 id="list-title">{view === 'pending' ? '判断待ち' : '判断済み'}</h2><span>優先度順・受付順</span></div>
          <ul id="case-list" className="case-list" aria-busy={loading}>
            {items.map(candidate => <li key={candidate.id}><button type="button" className="case-button" data-case={candidate.id} aria-current={candidate.id === item?.id ? 'true' : undefined} disabled={saving} onClick={() => onSelect(candidate.id)}>
              <span className="case-topline"><PriorityBadge priority={candidate.priority} />{candidate.revision > 0 && <Badge label="更新あり" variant="info" />}</span>
              <span className="case-title">{candidate.title}</span>
              {candidate.revision > 0 && <span className="case-update">{candidate.update_note}</span>}
              <span className="case-meta">{candidate.source}<br /><time dateTime={candidate.created_at}>{dateText(candidate.created_at)}</time></span>
              {candidate.answer && <span className="case-answer">回答：{answerTitle(candidate)}<br />後続処理：{statuses[candidate.answer.status]}</span>}
            </button></li>)}
          </ul>
          {!items.length && <p id="empty-list" className="empty-list">{loading ? '案件を読み込んでいます。' : '該当する案件はありません。'}</p>}
        </aside>
        <article id="detail" className="desk-detail" ref={detailRef} tabIndex={-1} aria-label="選択した案件の詳細" aria-busy={saving}>
          {!item ? <div className="empty-detail"><h2>{loading ? '案件を読み込んでいます' : view === 'pending' ? '判断待ちの案件はありません' : '判断済みの案件はありません'}</h2><p>{view === 'pending' ? '依頼が届くと、この一覧に表示されます。別の優先度の案件も確認できます。' : '判断を確定すると、回答と後続処理をここで確認できます。'}</p></div> : <>
            <header className="detail-header">
              <div className="detail-kicker"><PriorityBadge priority={item.priority} /><span>{item.answer ? '判断済み' : '判断待ち'}</span></div>
              <h2>{item.title}</h2>
              <p className="source">依頼元：{item.source}<span>受付 <time dateTime={item.created_at}>{dateText(item.created_at)}</time></span></p>
              {timing && <p className={`deadline ${item.priority}`}>{timing}</p>}
              {item.revision > 0 && <p id="update-note" className="update-note"><Badge label="更新あり" variant="info" /><span>{item.update_note}</span><time dateTime={item.updated_at}>{dateText(item.updated_at)}</time></p>}
            </header>
            {item.answer && <RecordedAnswer item={item} saving={saving} onUndo={onUndo} onRetry={onRetry} />}
            <section className="context" aria-labelledby="why-heading"><h3 id="why-heading">判断が必要な理由</h3><p className="reason">{item.reason}</p></section>
            {item.background && <section className="background-section"><h3>背景</h3><p className="background">{item.background}</p></section>}
            {imageUrl && <figure className="reference-image"><img src={imageUrl} alt={item.image.alt} referrerPolicy="no-referrer" /><figcaption>{item.image.alt}</figcaption></figure>}
            {links.length > 0 && <section className="related" aria-label="関連リンク"><h3>関連する PR・issue</h3><ul>{links.map((link, index) => <li key={`${link.url}-${index}`}><a href={safeUrl(link.url)} target="_blank" rel="noopener noreferrer"><span className="link-kind">{{ pr: 'PR', issue: 'issue', other: '参考' }[link.kind]}</span><span>{link.label}</span><span className="link-target">別タブで開く</span></a></li>)}</ul></section>}
            {item.files?.length > 0 && <AttachedFiles key={item.id} item={item} />}
            <section className="recommendation" aria-labelledby="recommend-heading"><h3 id="recommend-heading">依頼元の推奨</h3><p>{item.recommendation}</p></section>
            {!item.answer && <form id="decision-form" onSubmit={event => { event.preventDefault(); if (!saving && draft.choice) onSubmit(); }}>
              <div className="option-list"><RadioList label="どう進めますか？" value={draft.choice} onChange={choice => onDraftChange({ ...draft, choice })} htmlName="choice" isRequired isDisabled={saving}>
                {item.options.map(option => <RadioListItem align="first-line" key={option.id} value={option.id} label={<OptionLabel option={option} recommended={item.recommended} />} description={<OptionDescription option={option} />} />)}
              </RadioList></div>
              <div className="memo-field"><TextArea id="memo" label="判断のメモ (任意)" value={draft.memo} onChange={memo => onDraftChange({ ...draft, memo })} rows={3} isReadOnly={saving} /><p className="memo-help">条件や、次に気をつけてほしいことなど。2,000 文字まで</p></div>
              <div className="form-actions"><Button id="defer" label="あとで判断する" type="button" onClick={onDefer} isDisabled={saving} /><Button id="confirm" label="この内容で確定する" variant="primary" type="submit" isDisabled={!draft.choice || saving} isLoading={saving} /></div>
              <p className="form-hint">{saving ? '判断を保存しています' : draft.choice ? '選択した候補とメモを記録します' : '候補を選ぶと確定できます'}</p>
            </form>}
          </>}
        </article>
      </div>
      <footer className="desk-footer">判断を記録した後は、「判断済み」で後続処理を確認できます。</footer></>}
    </main>
  </div>;
}
