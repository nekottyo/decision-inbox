import { dateText, safeUrl } from './DecisionDesk.jsx';

// 人が手を動かす lane だけ強調する。正常に進んでいる lane は静かにしておく
const attention = new Set(['human_decision', 'human_action']);

function DecisionRef({ id, decision, onOpenDecision }) {
  if (!decision) return <span className="task-decision-missing">{id} (見つかりません)</span>;
  return <a className="task-decision" href={`/?decision=${encodeURIComponent(id)}`} onClick={event => { event.preventDefault(); onOpenDecision(id); }}>
    <span className="task-decision-state">{decision.answered ? '回答済み' : '未回答'}</span>
    <span>{decision.title}</span>
  </a>;
}

function TaskCard({ task, changed, onOpenDecision }) {
  const resultUrl = safeUrl(task.result);
  const owner = [task.pane, task.run].filter(Boolean).join(' ');
  return <li className={changed ? 'task-card changed' : 'task-card'} data-task={task.id}>
    <span className="task-title">{task.title}</span>
    <dl>
      {owner && <><dt>担当</dt><dd>{owner}</dd></>}
      {task.remaining && <><dt>残り</dt><dd>{task.remaining}</dd></>}
      {task.result && <><dt>結果</dt><dd>{resultUrl ? <a href={resultUrl} target="_blank" rel="noopener noreferrer">{task.result}</a> : task.result}</dd></>}
      {task.decision_id && <><dt>判断</dt><dd><DecisionRef id={task.decision_id} decision={task.decision} onOpenDecision={onOpenDecision} /></dd></>}
      <dt>更新</dt><dd><time dateTime={task.updated_at}>{dateText(task.updated_at)}</time></dd>
    </dl>
  </li>;
}

function TaskList({ lane, changes, onOpenDecision }) {
  if (!lane.tasks.length) return <p className="task-empty">なし</p>;
  // key に updated_at を含め、更新された card を作り直して印の animation をもう一度流す
  return <ul className="task-list">{lane.tasks.map(task => <TaskCard key={`${task.id}:${task.updated_at}`} task={task}
    changed={changes[task.id] === task.updated_at} onOpenDecision={onOpenDecision} />)}</ul>;
}

export default function TaskBoard({ lanes, changes, loading, onOpenDecision }) {
  if (!lanes.length) return <p className="task-empty">{loading ? 'task を読み込んでいます。' : 'task はありません。'}</p>;
  const done = lanes.find(lane => lane.id === 'done');
  return <div id="task-board" className="task-board">
    <div className="task-lanes">
      {lanes.filter(lane => lane !== done).map(lane => <section key={lane.id} className={attention.has(lane.id) && lane.tasks.length ? 'task-lane attention' : 'task-lane'} data-lane={lane.id} aria-labelledby={`lane-${lane.id}`}>
        <h2 id={`lane-${lane.id}`}>{lane.label}<span className="task-count">{lane.tasks.length}</span></h2>
        <TaskList lane={lane} changes={changes} onOpenDecision={onOpenDecision} />
      </section>)}
    </div>
    {done && <details className="task-done" data-lane="done">
      <summary>{done.label}<span className="task-count">{done.tasks.length}</span></summary>
      <TaskList lane={done} changes={changes} onOpenDecision={onOpenDecision} />
    </details>}
  </div>;
}
