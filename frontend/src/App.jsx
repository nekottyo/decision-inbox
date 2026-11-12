import {useEffect, useLayoutEffect, useRef, useState} from 'react';
import DecisionDesk from './DecisionDesk.jsx';
import TaskBoard from './TaskBoard.jsx';

const ranks = {urgent: 0, normal: 1, later: 2};
const emptyDraft = () => ({choice: '', memo: ''});
const visibleItems = (cases, view, priority) => cases
  .filter(item => (view === 'done') === Boolean(item.answer))
  .filter(item => priority === 'all' || item.priority === priority)
  .sort((a, b) => ranks[a.priority] - ranks[b.priority] || Date.parse(a.created_at) - Date.parse(b.created_at) || a.id.localeCompare(b.id));

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {cache: 'no-store'} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || 'サーバーに接続できませんでした');
    error.status = response.status;
    throw error;
  }
  return data;
}

export default function App() {
  const [state, setState] = useState({
    cases: [], view: 'pending', priority: 'all', selectedItem: null,
    draft: emptyDraft(), loading: true, saving: false, demo: false,
    notice: '', error: '', errorKind: '',
    screen: new URLSearchParams(window.location.search).get('view') === 'tasks' ? 'tasks' : 'decisions',
    lanes: [], taskChanges: {}, tasksLoading: true, taskError: '',
  });
  const current = useRef(state);
  current.current = state;
  const drafts = useRef(new Map());
  const saving = useRef(false);
  const refreshing = useRef(false);
  const version = useRef(0);
  const focusDetail = useRef(false);
  const detailRef = useRef(null);
  const linkedDecision = useRef(new URLSearchParams(window.location.search).get('decision'));

  useLayoutEffect(() => {
    if (focusDetail.current) {
      detailRef.current?.focus({preventScroll: true});
      focusDetail.current = false;
    }
  });

  useEffect(() => {
    let active = true;
    async function refresh() {
      if (refreshing.current || saving.current) return;
      refreshing.current = true;
      const requestedVersion = version.current;
      try {
        const data = await api('/api/decisions');
        if (!active || saving.current || requestedVersion !== version.current) return;
        const requestedDecision = linkedDecision.current;
        linkedDecision.current = null;
        setState(previous => {
          const changed = JSON.stringify(previous.cases) !== JSON.stringify(data.decisions);
          let view = previous.view;
          let selectedItem = previous.selectedItem;
          if (!selectedItem) {
            const linked = data.decisions.find(item => item.id === requestedDecision);
            if (linked) view = linked.answer ? 'done' : 'pending';
            selectedItem = linked || visibleItems(data.decisions, view, previous.priority)[0] || null;
          } else if (selectedItem.answer) {
            selectedItem = data.decisions.find(item => item.id === selectedItem.id) || selectedItem;
            if (!selectedItem.answer) view = 'pending';
          }
          let notice = previous.notice;
          if (!previous.loading && changed) {
            const latest = data.decisions.find(item => item.id === selectedItem?.id);
            notice = selectedItem && !selectedItem.answer && JSON.stringify(latest) !== JSON.stringify(selectedItem)
              ? '表示中の案件に変更があります。入力を保持しています。一覧から案件を選び直すと最新の状態を確認できます。'
              : '新着または処理状態の変更があります。一覧を更新しました。';
          }
          return {...previous, cases: data.decisions, view, selectedItem, loading: false, demo: data.demo, notice,
            draft: selectedItem?.id === previous.selectedItem?.id ? previous.draft : drafts.current.get(selectedItem?.id) || emptyDraft(),
            error: previous.errorKind === 'load' ? '' : previous.error,
            errorKind: previous.errorKind === 'load' ? '' : previous.errorKind};
        });
      } catch {
        if (active) setState(previous => ({...previous, loading: false, errorKind: 'load',
          error: '案件を読み込めませんでした。表示中の内容と入力は保持しています。接続を確認してください。自動で再試行します。'}));
      } finally { refreshing.current = false; }
      // task の失敗で判断の表示を止めない
      try {
        const data = await api('/api/tasks');
        if (!active) return;
        setState(previous => {
          const before = new Map(previous.lanes.flatMap(lane => lane.tasks).map(task => [task.id, task.updated_at]));
          const taskChanges = {...previous.taskChanges};
          if (!previous.tasksLoading) {
            for (const task of data.lanes.flatMap(lane => lane.tasks)) {
              if (before.get(task.id) !== task.updated_at) taskChanges[task.id] = task.updated_at;
            }
          }
          return {...previous, lanes: data.lanes, taskChanges, tasksLoading: false, taskError: ''};
        });
      } catch {
        if (active) setState(previous => ({...previous, tasksLoading: false,
          taskError: 'task を読み込めませんでした。表示中の内容は保持しています。自動で再試行します。'}));
      }
    }
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => { active = false; clearInterval(timer); };
  }, []);

  function select(id, focus = true) {
    if (saving.current) return;
    focusDetail.current = focus;
    setState(previous => ({...previous,
      selectedItem: previous.cases.find(item => item.id === id) || null,
      draft: drafts.current.get(id) || emptyDraft(), error: '', errorKind: '',
    }));
  }

  function changeScreen(screen) {
    if (saving.current) return;
    window.history.replaceState(null, '', screen === 'tasks' ? '/?view=tasks' : '/');
    setState(previous => ({...previous, screen, error: '', errorKind: ''}));
  }

  function openDecision(id) {
    const item = current.current.cases.find(candidate => candidate.id === id);
    if (saving.current || !item) return;
    window.history.replaceState(null, '', '/');
    focusDetail.current = true;
    setState(previous => ({...previous, screen: 'decisions', view: item.answer ? 'done' : 'pending', priority: 'all', selectedItem: item,
      draft: drafts.current.get(id) || emptyDraft(), error: '', errorKind: ''}));
  }

  function changeView(view) {
    if (saving.current) return;
    focusDetail.current = true;
    setState(previous => {
      const selectedItem = visibleItems(previous.cases, view, previous.priority)[0] || null;
      return {...previous, view, selectedItem, draft: drafts.current.get(selectedItem?.id) || emptyDraft(), error: '', errorKind: ''};
    });
  }

  function changePriority(priority) {
    if (saving.current) return;
    setState(previous => {
      const items = visibleItems(previous.cases, previous.view, priority);
      const selectedItem = items.some(item => item.id === previous.selectedItem?.id) ? previous.selectedItem : items[0] || null;
      return {...previous, priority, selectedItem,
        draft: selectedItem?.id === previous.selectedItem?.id ? previous.draft : drafts.current.get(selectedItem?.id) || emptyDraft(),
        notice: `${items.length} 件を表示しています。`};
    });
  }

  function changeDraft(draft) {
    if (saving.current) return;
    drafts.current.set(current.current.selectedItem?.id, draft);
    setState(previous => ({...previous, draft}));
  }

  function defer() {
    if (saving.current) return;
    focusDetail.current = true;
    setState(previous => {
      const items = visibleItems(previous.cases, previous.view, previous.priority);
      const index = items.findIndex(item => item.id === previous.selectedItem?.id);
      const selectedItem = items[(index + 1) % items.length] || null;
      return {...previous, selectedItem, draft: drafts.current.get(selectedItem?.id) || emptyDraft(),
        notice: `「${previous.selectedItem?.title}」は判断待ちに残しました。入力途中の内容は、このページを開いている間は保持されます。`};
    });
  }

  async function submit() {
    const {selectedItem, draft} = current.current;
    if (saving.current || !selectedItem || selectedItem.answer || !draft.choice) return;
    if (Array.from(draft.memo).length > 2000) {
      setState(previous => ({...previous, errorKind: 'save', error: 'メモは 2,000 文字以内で入力してください。入力は保持しています。'}));
      return;
    }
    saving.current = true;
    version.current += 1;
    setState(previous => ({...previous, saving: true, error: '', errorKind: ''}));
    try {
      const updated = await api(`/api/decisions/${encodeURIComponent(selectedItem.id)}/answer`, draft);
      drafts.current.delete(updated.id);
      focusDetail.current = true;
      setState(previous => {
        const cases = previous.cases.map(item => item.id === updated.id ? updated : item);
        const nextItem = visibleItems(cases, previous.view, previous.priority)[0] || null;
        return {...previous, cases, selectedItem: nextItem, draft: drafts.current.get(nextItem?.id) || emptyDraft(),
          notice: `「${selectedItem.title}」の判断を保存しました。後続処理は「処理待ち」です。`};
      });
    } catch (error) {
      setState(previous => ({...previous, errorKind: 'save', error: error.status === 409
        ? 'この案件は別の画面ですでに判断されています。入力はこの画面に残しています。一覧から案件を選び直して、記録済みの判断を確認してください。'
        : `判断を保存できませんでした。入力はこの画面に残っています。${error.message}。接続を確認して再試行してください。`}));
    } finally {
      saving.current = false;
      setState(previous => ({...previous, saving: false}));
    }
  }

  async function revise(action) {
    const {selectedItem} = current.current;
    if (saving.current || !selectedItem?.answer) return;
    const answer = selectedItem.answer;
    if (action === 'undo' ? !answer.can_undo : !['blocked', 'failed'].includes(answer.status)) return;
    saving.current = true;
    version.current += 1;
    setState(previous => ({...previous, saving: true, error: '', errorKind: ''}));
    try {
      const body = {decided_at: answer.decided_at};
      if (action === 'retry') body.reported_at = answer.reported_at;
      const updated = await api(`/api/decisions/${encodeURIComponent(selectedItem.id)}/${action}`, body);
      const draft = action === 'undo' ? {choice: answer.choice, memo: answer.memo} : emptyDraft();
      if (action === 'undo') drafts.current.set(updated.id, draft);
      focusDetail.current = true;
      setState(previous => ({...previous,
        cases: previous.cases.map(item => item.id === updated.id ? updated : item),
        view: action === 'undo' ? 'pending' : 'done', selectedItem: updated, draft,
        notice: action === 'undo' ? '回答を取り消し、判断待ちへ戻しました。候補とメモは下書きに残しています。' : '再処理を依頼しました。依頼元の処理を待っています。',
      }));
    } catch (error) {
      setState(previous => ({...previous, errorKind: 'save',
        error: `${action === 'undo' ? '回答を取り消せませんでした' : '再処理を依頼できませんでした'}。${error.message}。表示と入力は保持しています。最新の状態を確認してください。`}));
    } finally {
      saving.current = false;
      setState(previous => ({...previous, saving: false}));
    }
  }

  return <DecisionDesk
    items={visibleItems(state.cases, state.view, state.priority)}
    counts={{pending: state.cases.filter(item => !item.answer).length, done: state.cases.filter(item => item.answer).length}}
    selectedItem={state.selectedItem} view={state.view} priority={state.priority} draft={state.draft}
    loading={state.loading} saving={state.saving} demo={state.demo} notice={state.notice} error={state.error}
    detailRef={detailRef} onViewChange={changeView} onPriorityChange={changePriority} onSelect={select}
    onDraftChange={changeDraft} onSubmit={submit} onDefer={defer}
    onUndo={() => revise('undo')} onRetry={() => revise('retry')}
    screen={state.screen} onScreenChange={changeScreen} taskError={state.taskError}
    waitingTasks={state.lanes.filter(lane => ['human_decision', 'human_action'].includes(lane.id)).reduce((sum, lane) => sum + lane.tasks.length, 0)}
    board={<TaskBoard lanes={state.lanes} changes={state.taskChanges} loading={state.tasksLoading} onOpenDecision={openDecision} />}
  />;
}
