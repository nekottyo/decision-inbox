namespace CheckRunner

/-- 1 回の check 実行の結果。優先順位:
    failedTimeout > aborted > failedExit > failedOutput > 正常系。 -/
inductive Outcome where
  | okSilent
  | fired
  | suppressed
  | mixed
  | failedExit
  | failedTimeout
  | failedOutput
  | aborted
  deriving DecidableEq, Repr

def Outcome.isSuccess : Outcome → Bool
  | .okSilent | .fired | .suppressed | .mixed => true
  | _ => false

def Outcome.isFailure : Outcome → Bool
  | .failedExit | .failedTimeout | .failedOutput => true
  | _ => false

/-- 優先順位に従う outcome の分類。
    command timeout の期限が tick 上限以下 (同時を含む) で支配的であれば failedTimeout、
    tick 上限が先に到来していれば観測時刻が遅れても aborted。 -/
def classifyOutcome (timedOut aborted exitNonZero badOutput : Bool)
    (firedCount suppressedCount : Nat) : Outcome :=
  if timedOut then .failedTimeout
  else if aborted then .aborted
  else if exitNonZero then .failedExit
  else if badOutput then .failedOutput
  else if firedCount > 0 && suppressedCount > 0 then .mixed
  else if firedCount > 0 then .fired
  else if suppressedCount > 0 then .suppressed
  else .okSilent

inductive TaskKind where
  | event
  | fault
  deriving DecidableEq, Repr

inductive TaskStatus where
  | incomplete
  | completed
  deriving DecidableEq, Repr

structure TaskRecord where
  id : Nat
  checkName : String
  kind : TaskKind
  dedupKey : String
  status : TaskStatus
  deriving DecidableEq, Repr

structure CheckState where
  lastStarted : Option Nat
  lastActualStartedAt : Option Nat
  lastOutcome : Option Outcome
  faultCount : Nat
  deriving DecidableEq, Repr

structure CheckConfig where
  name : String
  interval : Nat
  timeout : Nat
  faultThreshold : Nat
  command : String
  enabled : Bool
  deriving DecidableEq, Repr

/-- grace = min(30 秒, interval の 10%)。整数秒で floor(interval / 10)。 -/
def grace (interval : Nat) : Nat :=
  min 30 (interval / 10)

theorem grace_le_interval (interval : Nat) : grace interval ≤ interval := by
  unfold grace
  have h1 : min 30 (interval / 10) ≤ interval / 10 := Nat.min_le_right 30 (interval / 10)
  have h2 : interval / 10 ≤ interval := Nat.div_le_self interval 10
  exact Nat.le_trans h1 h2

/-- 時刻の巻き戻り判定: last_started と last_actual_started_at のいずれかが tick_started より未来。 -/
def isClockAnomaly (tickStarted : Nat) (s : CheckState) : Bool :=
  (match s.lastStarted with | some t => decide (tickStarted < t) | none => false) ||
  (match s.lastActualStartedAt with | some t => decide (tickStarted < t) | none => false)

/-- due 判定の基準時刻。成功系の後は last_started、失敗系の後は last_actual_started_at。
    aborted は checks の実行状態を変えないため last_outcome に現れない。 -/
def dueBase (s : CheckState) : Option Nat :=
  match s.lastOutcome with
  | some o =>
      if o.isSuccess then s.lastStarted
      else if o.isFailure then s.lastActualStartedAt
      else none
  | none => none

/-- due 判定。履歴なしまたは時刻巻き戻りは即 due。
    それ以外は tick_started >= 基準時刻 + interval - grace
    (Nat の引き算を避け、grace <= interval より同値な tick_started + grace >= base + interval で表現)。 -/
def isDue (tickStarted interval : Nat) (s : CheckState) : Bool :=
  match s.lastStarted, s.lastActualStartedAt, s.lastOutcome with
  | none, _, _ => true
  | _, none, _ => true
  | _, _, none => true
  | some _, some _, some _ =>
      if isClockAnomaly tickStarted s then true
      else
        match dueBase s with
        | none => true
        | some base => decide (base + interval ≤ tickStarted + grace interval)

/-- 重複抑制の単位 (check, kind, dedup_key) に未完了タスクが存在するか。 -/
def hasIncomplete (tasks : List TaskRecord) (chk : String) (kind : TaskKind) (key : String) : Bool :=
  tasks.any (fun t => t.checkName == chk && t.kind == kind && t.dedupKey == key && t.status == .incomplete)

/-- 未完了タスクの先頭 ID を返す (suppressed_by_task_id 用)。 -/
def findIncompleteId (tasks : List TaskRecord) (chk : String) (kind : TaskKind) (key : String) : Option Nat :=
  match tasks.find? (fun t => t.checkName == chk && t.kind == kind && t.dedupKey == key && t.status == .incomplete) with
  | some t => some t.id
  | none => none

/-- 1 回の check 実行に伴う checks 状態と故障タスク起票の遷移。
    - aborted: checks 状態を一切変更せず、故障タスクも作らない。
    - 成功系: last_* と lastOutcome を更新し faultCount := 0。
    - 失敗系: last_* と lastOutcome を更新。未完了の故障タスクがある間は faultCount := 0 のまま進めない。
      無ければ faultCount + 1 し、threshold 以上なら故障タスクを 1 件起票して faultCount := 0。 -/
def applyOutcome (chk : String) (threshold : Nat) (tickStarted actualStartedAt nextTaskId : Nat)
    (outcome : Outcome) (s : CheckState) (tasks : List TaskRecord)
    : CheckState × Option TaskRecord :=
  match outcome with
  | .aborted => (s, none)
  | o =>
      if o.isSuccess then
        ({ lastStarted := some tickStarted
           lastActualStartedAt := some actualStartedAt
           lastOutcome := some o
           faultCount := 0 }, none)
      else
        -- 失敗系 (failedExit / failedTimeout / failedOutput)
        if hasIncomplete tasks chk .fault "" then
          ({ lastStarted := some tickStarted
             lastActualStartedAt := some actualStartedAt
             lastOutcome := some o
             faultCount := 0 }, none)
        else
          let nextCount := s.faultCount + 1
          if threshold ≤ nextCount then
            let faultTask : TaskRecord :=
              { id := nextTaskId, checkName := chk, kind := .fault, dedupKey := "", status := .incomplete }
            ({ lastStarted := some tickStarted
               lastActualStartedAt := some actualStartedAt
               lastOutcome := some o
               faultCount := 0 }, some faultTask)
          else
            ({ lastStarted := some tickStarted
               lastActualStartedAt := some actualStartedAt
               lastOutcome := some o
               faultCount := nextCount }, none)

/-- TOML 同期時の fault_count 更新規則。
    command 変更または無効化で fault_count を 0 に戻す。env / timeout のみの変更では戻さない。 -/
def syncFaultCount (prevCommand newCommand : String) (newEnabled : Bool) (s : CheckState) : CheckState :=
  if prevCommand != newCommand || !newEnabled then
    { s with faultCount := 0 }
  else
    s

/-- 状態不変条件:
    1. lastOutcome は決して aborted にならない (aborted を除く直近の完了実行結果)。
    2. lastStarted / lastActualStartedAt / lastOutcome の有無は常に一致する。
    3. faultCount は常に threshold 未満 (threshold >= 1)。
    4. 未完了の故障タスクが存在するとき faultCount = 0。 -/
structure ValidState (chk : String) (threshold : Nat) (s : CheckState) (tasks : List TaskRecord) : Prop where
  thresholdPos : 1 ≤ threshold
  neverAborted : s.lastOutcome ≠ some .aborted
  historyAligned :
    (s.lastStarted.isSome = s.lastActualStartedAt.isSome) ∧
    (s.lastStarted.isSome = s.lastOutcome.isSome)
  faultBounded : s.faultCount < threshold
  faultZeroWhenOpen : hasIncomplete tasks chk .fault "" = true → s.faultCount = 0

/-- 初期状態 (履歴なし) は ValidState を満たす。 -/
theorem initial_valid (chk : String) (threshold : Nat) (hth : 1 ≤ threshold) (tasks : List TaskRecord) :
    ValidState chk threshold ⟨none, none, none, 0⟩ tasks where
  thresholdPos := hth
  neverAborted := by simp
  historyAligned := by simp
  faultBounded := hth
  faultZeroWhenOpen := fun _ => rfl

/-- outcome の優先順位: failedTimeout > aborted > failedExit > failedOutput。 -/
theorem priority_timeout_dominates (aborted exitNonZero badOutput : Bool) (f s : Nat) :
    classifyOutcome true aborted exitNonZero badOutput f s = .failedTimeout := rfl

theorem priority_aborted_dominates (exitNonZero badOutput : Bool) (f s : Nat) :
    classifyOutcome false true exitNonZero badOutput f s = .aborted := rfl

theorem priority_exit_dominates (badOutput : Bool) (f s : Nat) :
    classifyOutcome false false true badOutput f s = .failedExit := rfl

theorem priority_output_dominates (f s : Nat) :
    classifyOutcome false false false true f s = .failedOutput := rfl

/-- aborted は checks の実行状態カラムを一切変えず、故障タスクも起票しない。 -/
theorem aborted_preserves_state (chk : String) (threshold tickStarted actualStartedAt nextTaskId : Nat)
    (s : CheckState) (tasks : List TaskRecord) :
    applyOutcome chk threshold tickStarted actualStartedAt nextTaskId .aborted s tasks = (s, none) := rfl

/-- applyOutcome は ValidState を保存する (aborted を含む全 outcome)。 -/
theorem applyOutcome_preserves_valid (chk : String) (threshold tickStarted actualStartedAt nextTaskId : Nat)
    (outcome : Outcome) (s : CheckState) (tasks : List TaskRecord)
    (hv : ValidState chk threshold s tasks) :
    let res := applyOutcome chk threshold tickStarted actualStartedAt nextTaskId outcome s tasks
    let tasks' := match res.2 with | some t => t :: tasks | none => tasks
    ValidState chk threshold res.1 tasks' := by
  cases outcome with
  | aborted =>
      simpa [applyOutcome] using hv
  | okSilent | fired | suppressed | mixed =>
      refine ⟨hv.thresholdPos, by simp [applyOutcome, Outcome.isSuccess], by simp [applyOutcome, Outcome.isSuccess], ?_, ?_⟩
      · simp [applyOutcome, Outcome.isSuccess]; have := hv.thresholdPos; omega
      · intro _; simp [applyOutcome, Outcome.isSuccess]
  | failedExit | failedTimeout | failedOutput =>
      by_cases hopen : hasIncomplete tasks chk .fault "" = true
      · refine ⟨hv.thresholdPos, ?_, ?_, ?_, ?_⟩
        · simp [applyOutcome, Outcome.isSuccess, hopen]
        · simp [applyOutcome, Outcome.isSuccess, hopen]
        · simp [applyOutcome, Outcome.isSuccess, hopen]; have := hv.thresholdPos; omega
        · intro _; simp [applyOutcome, Outcome.isSuccess, hopen]
      · by_cases hreach : threshold ≤ s.faultCount + 1
        · refine ⟨hv.thresholdPos, ?_, ?_, ?_, ?_⟩
          · simp [applyOutcome, Outcome.isSuccess, hopen, hreach]
          · simp [applyOutcome, Outcome.isSuccess, hopen, hreach]
          · simp [applyOutcome, Outcome.isSuccess, hopen, hreach]; have := hv.thresholdPos; omega
          · intro _; simp [applyOutcome, Outcome.isSuccess, hopen, hreach]
        · have hlt : s.faultCount + 1 < threshold := Nat.lt_of_not_le hreach
          refine ⟨hv.thresholdPos, ?_, ?_, ?_, ?_⟩
          · simp [applyOutcome, Outcome.isSuccess, hopen, hreach]
          · simp [applyOutcome, Outcome.isSuccess, hopen, hreach]
          · simpa [applyOutcome, Outcome.isSuccess, hopen, hreach] using hlt
          · intro hopen'
            simp [applyOutcome, Outcome.isSuccess, hopen, hreach] at hopen'

/-- 未完了の故障タスクが存在する間、失敗しても faultCount は 0 のままで二重起票されない。 -/
theorem incomplete_fault_blocks_counter_and_duplicate
    (chk : String) (threshold tickStarted actualStartedAt nextTaskId : Nat)
    (outcome : Outcome) (hfail : outcome.isFailure = true)
    (s : CheckState) (tasks : List TaskRecord)
    (hopen : hasIncomplete tasks chk .fault "" = true) :
    let res := applyOutcome chk threshold tickStarted actualStartedAt nextTaskId outcome s tasks
    res.1.faultCount = 0 ∧ res.2 = none := by
  cases outcome <;> simp [Outcome.isFailure] at hfail
  all_goals simp [applyOutcome, Outcome.isSuccess, hopen]

/-- 名前空間分離: event タスクだけが存在しても、fault タスクの有無 (hasIncomplete ... .fault "") は false のまま。 -/
theorem event_tasks_do_not_block_fault (tasks : List TaskRecord) (chk : String)
    (hall : ∀ t ∈ tasks, t.kind = .event) :
    hasIncomplete tasks chk .fault "" = false := by
  unfold hasIncomplete
  rw [List.any_eq_false]
  intro t ht
  have hk := hall t ht
  simp [hk]

/-- 名前空間分離: fault タスクだけが存在しても、event タスクの有無 (hasIncomplete ... .event key) は false のまま。 -/
theorem fault_tasks_do_not_suppress_event (tasks : List TaskRecord) (chk key : String)
    (hall : ∀ t ∈ tasks, t.kind = .fault) :
    hasIncomplete tasks chk .event key = false := by
  unfold hasIncomplete
  rw [List.any_eq_false]
  intro t ht
  have hk := hall t ht
  simp [hk]

/-- due 判定の不変条件 (成功系の後):
    時刻巻き戻りがなく直近が成功系で due なら、tick 開始時刻同士の間隔は interval - grace 以上。 -/
theorem due_after_success_respects_interval
    (tickStarted interval ls la : Nat) (o : Outcome) (fc : Nat)
    (hsucc : o.isSuccess = true)
    (hnoanom : isClockAnomaly tickStarted ⟨some ls, some la, some o, fc⟩ = false)
    (hdue : isDue tickStarted interval ⟨some ls, some la, some o, fc⟩ = true) :
    ls + interval ≤ tickStarted + grace interval := by
  simp [isDue, hnoanom, dueBase, hsucc] at hdue
  exact hdue

/-- due 判定の不変条件 (失敗系の後):
    時刻巻き戻りがなく直近が失敗系で due なら、tick 開始時刻および実開始時刻 (>= tickStarted) と
    前回の実開始時刻の間隔は interval - grace 以上。 -/
theorem due_after_failure_respects_actual_interval
    (tickStarted actualStartedAt interval ls la : Nat) (o : Outcome) (fc : Nat)
    (hfail : o.isFailure = true)
    (hactual : tickStarted ≤ actualStartedAt)
    (hnoanom : isClockAnomaly tickStarted ⟨some ls, some la, some o, fc⟩ = false)
    (hdue : isDue tickStarted interval ⟨some ls, some la, some o, fc⟩ = true) :
    la + interval ≤ tickStarted + grace interval ∧
    la + interval ≤ actualStartedAt + grace interval := by
  have hnotSucc : o.isSuccess = false := by
    cases o <;> simp [Outcome.isFailure] at hfail <;> rfl
  simp [isDue, hnoanom, dueBase, hnotSucc, hfail] at hdue
  exact ⟨hdue, Nat.le_trans hdue (Nat.add_le_add_right hactual (grace interval))⟩

/-- TOML 同期: command 変更または無効化で faultCount は 0 に戻る。 -/
theorem sync_resets_on_command_change (oldCmd newCmd : String) (enabled : Bool) (s : CheckState)
    (hdiff : oldCmd ≠ newCmd) :
    (syncFaultCount oldCmd newCmd enabled s).faultCount = 0 := by
  have hne : (oldCmd != newCmd) = true := by simp [bne_iff_ne, hdiff]
  simp [syncFaultCount, hne]

theorem sync_resets_on_disable (oldCmd newCmd : String) (s : CheckState) :
    (syncFaultCount oldCmd newCmd false s).faultCount = 0 := by
  simp [syncFaultCount]

theorem sync_preserves_when_command_same_and_enabled (cmd : String) (s : CheckState) :
    syncFaultCount cmd cmd true s = s := by
  simp [syncFaultCount]

/-- 起票上限 (20 件) と重複抑制のモデル。
    入力は検証済みの重複キーなしイベント一覧 (dedupKey のリスト)。
    suppressed は上限枠を消費せず、上限到達後の新規イベントのみ deferred に数える。 -/
structure BatchResult where
  createdKeys : List String
  suppressedKeys : List String
  deferredKeys : List String
  deriving DecidableEq, Repr

def processEventsLoop (chk : String) (cap : Nat) (tasks : List TaskRecord)
    (keys : List String) (acc : BatchResult) : BatchResult :=
  match keys with
  | [] => acc
  | k :: rest =>
      if hasIncomplete tasks chk .event k then
        processEventsLoop chk cap tasks rest
          { acc with suppressedKeys := acc.suppressedKeys ++ [k] }
      else if acc.createdKeys.length < cap then
        processEventsLoop chk cap tasks rest
          { acc with createdKeys := acc.createdKeys ++ [k] }
      else
        processEventsLoop chk cap tasks rest
          { acc with deferredKeys := acc.deferredKeys ++ [k] }

def processEvents (chk : String) (cap : Nat) (tasks : List TaskRecord) (keys : List String) : BatchResult :=
  processEventsLoop chk cap tasks keys ⟨[], [], []⟩

theorem processEventsLoop_created_le_cap (chk : String) (cap : Nat) (tasks : List TaskRecord)
    (keys : List String) (acc : BatchResult) (hle : acc.createdKeys.length ≤ cap) :
    (processEventsLoop chk cap tasks keys acc).createdKeys.length ≤ cap := by
  induction keys generalizing acc with
  | nil => exact hle
  | cons k rest ih =>
      unfold processEventsLoop
      by_cases hs : hasIncomplete tasks chk .event k = true
      · simp [hs]
        exact ih _ hle
      · simp [hs]
        by_cases hlt : acc.createdKeys.length < cap
        · simp [hlt]
          apply ih
          simp only [List.length_append, List.length_singleton]
          exact hlt
        · simp [hlt]
          exact ih _ hle

/-- 新規起票件数は常に cap (20) 以下。 -/
theorem processEvents_created_le_cap (chk : String) (cap : Nat) (tasks : List TaskRecord)
    (keys : List String) :
    (processEvents chk cap tasks keys).createdKeys.length ≤ cap :=
  processEventsLoop_created_le_cap chk cap tasks keys ⟨[], [], []⟩ (Nat.zero_le cap)

theorem processEventsLoop_counts_exact (chk : String) (cap : Nat) (tasks : List TaskRecord)
    (keys : List String) (acc : BatchResult) :
    let r := processEventsLoop chk cap tasks keys acc
    r.createdKeys.length + r.suppressedKeys.length + r.deferredKeys.length =
      acc.createdKeys.length + acc.suppressedKeys.length + acc.deferredKeys.length + keys.length := by
  induction keys generalizing acc with
  | nil => simp [processEventsLoop]
  | cons k rest ih =>
      unfold processEventsLoop
      by_cases hs : hasIncomplete tasks chk .event k = true
      · simp only [hs, ↓reduceIte, List.length_cons]
        have h := ih { acc with suppressedKeys := acc.suppressedKeys ++ [k] }
        simp only [List.length_append, List.length_singleton] at h
        omega
      · simp only [hs, Bool.false_eq_true, ↓reduceIte, List.length_cons]
        by_cases hlt : acc.createdKeys.length < cap
        · simp only [hlt, ↓reduceIte]
          have h := ih { acc with createdKeys := acc.createdKeys ++ [k] }
          simp only [List.length_append, List.length_singleton] at h
          omega
        · simp only [hlt, ↓reduceIte]
          have h := ih { acc with deferredKeys := acc.deferredKeys ++ [k] }
          simp only [List.length_append, List.length_singleton] at h
          omega

/-- suppressed が先頭にあっても新規作成件数を圧迫しない:
    全イベント中の「非 suppressed の件数」が m なら、createdKeys.length = min m cap。 -/
theorem processEventsLoop_nonSuppressed_cap (chk : String) (cap : Nat) (tasks : List TaskRecord)
    (keys : List String) (acc : BatchResult) (hle : acc.createdKeys.length ≤ cap) :
    let nonSupp := (keys.filter (fun k => !hasIncomplete tasks chk .event k)).length
    (processEventsLoop chk cap tasks keys acc).createdKeys.length =
      min (acc.createdKeys.length + nonSupp) cap := by
  induction keys generalizing acc with
  | nil =>
      simp [processEventsLoop, Nat.min_eq_left hle]
  | cons k rest ih =>
      unfold processEventsLoop
      by_cases hs : hasIncomplete tasks chk .event k = true
      · simp [hs]
        simpa using ih { acc with suppressedKeys := acc.suppressedKeys ++ [k] } hle
      · have hsfalse : hasIncomplete tasks chk .event k = false := by
          cases h : hasIncomplete tasks chk .event k <;> simp_all
        simp [hsfalse]
        by_cases hlt : acc.createdKeys.length < cap
        · simp only [hlt, ↓reduceIte]
          have hle' : (acc.createdKeys ++ [k]).length ≤ cap := by
            simp only [List.length_append, List.length_singleton]; exact hlt
          have hih := ih { acc with createdKeys := acc.createdKeys ++ [k] } hle'
          simp only [List.length_append, List.length_singleton] at hih
          rw [hih]
          congr 1
          omega
        · simp only [hlt, ↓reduceIte]
          have heq : acc.createdKeys.length = cap := Nat.le_antisymm hle (Nat.le_of_not_lt hlt)
          have hih := ih { acc with deferredKeys := acc.deferredKeys ++ [k] } hle
          rw [hih, heq]
          omega

theorem suppressed_does_not_consume_cap (chk : String) (cap : Nat) (tasks : List TaskRecord)
    (keys : List String) :
    (processEvents chk cap tasks keys).createdKeys.length =
      min (keys.filter (fun k => !hasIncomplete tasks chk .event k)).length cap := by
  simpa [processEvents] using
    processEventsLoop_nonSuppressed_cap chk cap tasks keys ⟨[], [], []⟩ (Nat.zero_le cap)

/-- Liveness (故障検知への到達):
    公平性仮定の下で非 aborted な失敗が連続して起きたとき、未完了の故障タスクが無ければ
    k ステップ目 (k < threshold) は faultCount = k かつ未起票、threshold ステップ目で
    故障タスクが起票され faultCount = 0 に戻る。 -/
def runFailureStreak (chk : String) (threshold : Nat) (tasks : List TaskRecord)
    (s : CheckState) : Nat → CheckState × Option TaskRecord
  | 0 => (s, none)
  | n + 1 =>
      let (sPrev, _) := runFailureStreak chk threshold tasks s n
      applyOutcome chk threshold (1000 * (n + 1)) (1000 * (n + 1)) (n + 1) .failedExit sPrev tasks

theorem streak_before_threshold (chk : String) (threshold : Nat) (tasks : List TaskRecord)
    (hnoFault : hasIncomplete tasks chk .fault "" = false)
    (s0 : CheckState) (h0 : s0.faultCount = 0) :
    ∀ k, k < threshold →
      (runFailureStreak chk threshold tasks s0 k).1.faultCount = k ∧
      (runFailureStreak chk threshold tasks s0 k).2 = none := by
  intro k
  induction k with
  | zero =>
      intro _
      exact ⟨h0, rfl⟩
  | succ m ih =>
      intro hlt
      have hm : m < threshold := Nat.lt_of_succ_lt hlt
      have ⟨hfc, _⟩ := ih hm
      have hnotle : ¬ (threshold ≤ m + 1) := Nat.not_le_of_gt hlt
      simp [runFailureStreak, applyOutcome, Outcome.isSuccess, hnoFault, hfc, hnotle]

theorem fault_detection_at_threshold (chk : String) (threshold : Nat) (hth : 1 ≤ threshold)
    (tasks : List TaskRecord) (hnoFault : hasIncomplete tasks chk .fault "" = false)
    (s0 : CheckState) (h0 : s0.faultCount = 0) :
    let res := runFailureStreak chk threshold tasks s0 threshold
    res.1.faultCount = 0 ∧ res.2.isSome = true := by
  cases threshold with
  | zero => cases hth
  | succ m =>
      have ⟨hfc, _⟩ := streak_before_threshold chk (m + 1) tasks hnoFault s0 h0 m (Nat.lt_succ_self m)
      simp [runFailureStreak, applyOutcome, Outcome.isSuccess, hnoFault, hfc]

/-- 4 段階 tick 予算 (exec → cleanup → commit → finalize)。
    時刻・時間はすべて単調時計の自然数 (例: ms) で表す。 -/
structure TickBudget where
  monoStart : Nat
  tickBudget : Nat
  finalizeReserve : Nat
  commitReserve : Nat
  cleanupReserve : Nat
  deriving DecidableEq, Repr

def TickBudget.tickDeadline (b : TickBudget) : Nat :=
  b.monoStart + b.tickBudget

def TickBudget.effectiveFinalize (b : TickBudget) : Nat :=
  min b.finalizeReserve b.tickBudget

def TickBudget.endWorkDeadline (b : TickBudget) : Nat :=
  b.tickDeadline - b.effectiveFinalize

def TickBudget.workBudget (b : TickBudget) : Nat :=
  b.endWorkDeadline - b.monoStart

def TickBudget.effectiveCommit (b : TickBudget) : Nat :=
  min b.commitReserve b.workBudget

def TickBudget.commitStartDeadline (b : TickBudget) : Nat :=
  b.endWorkDeadline - b.effectiveCommit

def TickBudget.preCommitBudget (b : TickBudget) : Nat :=
  b.commitStartDeadline - b.monoStart

def TickBudget.effectiveCleanup (b : TickBudget) : Nat :=
  min b.cleanupReserve b.preCommitBudget

def TickBudget.execDeadline (b : TickBudget) : Nat :=
  b.commitStartDeadline - b.effectiveCleanup

/-- 4 段階の絶対期限は常に単調に並ぶ:
    monoStart ≤ execDeadline ≤ commitStartDeadline ≤ endWorkDeadline ≤ tickDeadline。 -/
theorem budget_stages_ordered (b : TickBudget) :
    b.monoStart ≤ b.execDeadline ∧
    b.execDeadline ≤ b.commitStartDeadline ∧
    b.commitStartDeadline ≤ b.endWorkDeadline ∧
    b.endWorkDeadline ≤ b.tickDeadline := by
  unfold TickBudget.execDeadline TickBudget.effectiveCleanup TickBudget.preCommitBudget
    TickBudget.commitStartDeadline TickBudget.effectiveCommit TickBudget.workBudget
    TickBudget.endWorkDeadline TickBudget.effectiveFinalize TickBudget.tickDeadline
  omega

/-- 単一フェーズの絶対期限: フェーズ開始時刻 phaseStart に設定時間 configured を足した時刻と
    絶対上限 absDeadline の小さい方。開始遅延や後から取った時刻サンプルへの加算で absDeadline を超えない。 -/
def phaseDeadline (phaseStart configured absDeadline : Nat) : Nat :=
  min (phaseStart + configured) absDeadline

/-- 任意の開始時刻・遅延であっても、フェーズ期限は絶対上限 absDeadline を超えない。 -/
theorem phaseDeadline_le_abs (phaseStart configured absDeadline : Nat) :
    phaseDeadline phaseStart configured absDeadline ≤ absDeadline :=
  Nat.min_le_right _ _

/-- 定時開始 (phaseStart + configured ≤ absDeadline) なら設定どおりの猶予を保つ。 -/
theorem phaseDeadline_preserves_configured (phaseStart configured absDeadline : Nat)
    (hOnTime : phaseStart + configured ≤ absDeadline) :
    phaseDeadline phaseStart configured absDeadline = phaseStart + configured :=
  Nat.min_eq_left hOnTime

/-- 既に絶対上限に到達・超過している場合、待ち時間 (phaseDeadline - phaseStart) は 0。 -/
theorem phaseDeadline_zero_wait_when_past (phaseStart configured absDeadline : Nat)
    (hPast : absDeadline ≤ phaseStart) :
    phaseDeadline phaseStart configured absDeadline - phaseStart = 0 := by
  unfold phaseDeadline
  omega

/-- cleanup の 3 フェーズ (SIGTERM 猶予 → SIGKILL 回収待ち → drain join) に任意の遅延が入っても、
    各フェーズの絶対期限はいずれも cleanupDeadline (= commitStartDeadline) 以下に収まる。 -/
theorem cleanup_phases_bounded_under_arbitrary_delays
    (cleanupStart cleanupDeadline graceCfg killWaitCfg drainCfg dTerm dKill dDrain : Nat) :
    let termStart := cleanupStart + dTerm
    let termDl := phaseDeadline termStart graceCfg cleanupDeadline
    let killStart := termDl + dKill
    let killDl := phaseDeadline killStart killWaitCfg cleanupDeadline
    let drainStart := killDl + dDrain
    let drainDl := phaseDeadline drainStart drainCfg cleanupDeadline
    termDl ≤ cleanupDeadline ∧ killDl ≤ cleanupDeadline ∧ drainDl ≤ cleanupDeadline :=
  ⟨phaseDeadline_le_abs _ _ _, phaseDeadline_le_abs _ _ _, phaseDeadline_le_abs _ _ _⟩

/-- cleanup が commitStartDeadline までに完了すれば、commit 予約 (effectiveCommit) の全量が
    endWorkDeadline まで残る。特に effectiveCommit > 0 なら初回 commit は時間切れにならない。 -/
theorem commit_window_preserved_when_cleanup_bounded (b : TickBudget) (cleanupDone : Nat)
    (hDone : cleanupDone ≤ b.commitStartDeadline) :
    b.effectiveCommit ≤ b.endWorkDeadline - cleanupDone ∧
    (0 < b.effectiveCommit → cleanupDone < b.endWorkDeadline) := by
  unfold TickBudget.commitStartDeadline TickBudget.effectiveCommit TickBudget.workBudget
    TickBudget.endWorkDeadline TickBudget.effectiveFinalize TickBudget.tickDeadline at *
  omega

/-- mirror 同期の DB busy timeout 上限: endWorkDeadline までの残り作業時間で cap する。 -/
def cappedMirrorBusy (now endWorkDeadline configuredBusy : Nat) : Nat :=
  min configuredBusy (endWorkDeadline - now)

/-- 残り作業時間がある (now < endWorkDeadline) とき、mirror の最大待ち完了時刻は endWorkDeadline を超えない。 -/
theorem mirror_wait_bounded_by_endWorkDeadline (now endWorkDeadline configuredBusy : Nat)
    (hRem : now < endWorkDeadline) :
    now + cappedMirrorBusy now endWorkDeadline configuredBusy ≤ endWorkDeadline := by
  unfold cappedMirrorBusy
  omega

/-- 残り作業時間が無い (endWorkDeadline ≤ now) とき、mirror の残り予算 (`cappedMirrorBusy`) は 0 になる。
この定理が示すのは残り予算が 0 になることだけであり、mirror 書き込みを実際に skip するのは
実装 `run_check_tick` (`mirror_rem <= 0` のとき `sync_checks_mirror` を呼ばず `aborted` へ進む) である。 -/
theorem mirror_budget_zero_when_exhausted (now endWorkDeadline configuredBusy : Nat)
    (hPast : endWorkDeadline ≤ now) :
    cappedMirrorBusy now endWorkDeadline configuredBusy = 0 := by
  unfold cappedMirrorBusy
  omega

-- 具体的なトレース検証 (#guard)
#guard (do
  let init : CheckState := ⟨none, none, none, 0⟩
  -- 履歴なしは即 due
  guard (isDue 1000 300 init == true)
  -- grace 境界: interval=3 -> grace=0、interval=65 -> grace=6
  guard (grace 3 == 0)
  guard (grace 65 == 6)
  let (s65, _) := applyOutcome "c65" 3 1000 1000 100 .okSilent init []
  guard (isDue 1058 65 s65 == false)
  guard (isDue 1059 65 s65 == true)
  -- 成功実行 (tick=1000, actual=1050): 基準は last_started=1000, grace=min(30,30)=30
  let (s1, ft1) := applyOutcome "c1" 3 1000 1050 1 .okSilent init []
  guard (ft1 == none)
  guard (s1.faultCount == 0)
  guard (isDue 1269 300 s1 == false)
  guard (isDue 1270 300 s1 == true)
  -- 失敗実行 1 回目 (tick=1300, actual=1340): 基準は last_actual_started_at=1340
  let (s2, ft2) := applyOutcome "c1" 3 1300 1340 2 .failedExit s1 []
  guard (ft2 == none)
  guard (s2.faultCount == 1)
  -- tick=1600 は 1340 + 300 - 30 = 1610 未満なので due でない (設計書の 31 秒以降失敗の記述と一致)
  guard (isDue 1600 300 s2 == false)
  guard (isDue 1610 300 s2 == true)
  -- aborted は checks 状態を一切変えない
  let (s2a, ft2a) := applyOutcome "c1" 3 1610 1620 3 .aborted s2 []
  guard (s2a == s2 && ft2a == none)
  -- 時刻の巻き戻り (tick=1200 < last_started=1300) は clock_anomaly かつ due
  guard (isClockAnomaly 1200 s2 == true)
  guard (isDue 1200 300 s2 == true)
  -- 失敗 2 回目 → faultCount = 2、失敗 3 回目 → 故障タスク起票 & faultCount = 0
  let (s3, ft3) := applyOutcome "c1" 3 1610 1610 4 .failedTimeout s2 []
  guard (s3.faultCount == 2 && ft3 == none)
  let (s4, ft4) := applyOutcome "c1" 3 1910 1910 5 .failedOutput s3 []
  guard (s4.faultCount == 0)
  let faultTask ← ft4
  guard (faultTask.kind == .fault && faultTask.dedupKey == "")
  -- 未完了の故障タスクがある間は失敗しても faultCount=0 のまま & 再起票なし
  let (s5, ft5) := applyOutcome "c1" 3 2210 2210 6 .failedExit s4 [faultTask]
  guard (s5.faultCount == 0 && ft5 == none)
  -- 故障タスクと通常タスク (dedup_key="") の名前空間分離
  guard (hasIncomplete [faultTask] "c1" .event "" == false)
  let evTask : TaskRecord := ⟨7, "c1", .event, "", .incomplete⟩
  guard (hasIncomplete [evTask] "c1" .fault "" == false)
  -- 故障タスク完了後は再度 3 回で起票される
  let doneFault : TaskRecord := { faultTask with status := .completed }
  let (s6, _) := applyOutcome "c1" 3 2510 2510 8 .failedExit s5 [doneFault]
  let (s7, _) := applyOutcome "c1" 3 2810 2810 9 .failedExit s6 [doneFault]
  let (s8, ft8) := applyOutcome "c1" 3 3110 3110 10 .failedExit s7 [doneFault]
  guard (s8.faultCount == 0 && ft8.isSome)
  -- 起票上限 2 件のテスト: 先頭 3 件が suppressed でも後続 2 件が新規起票、6 件目が deferred
  let existing : List TaskRecord :=
    [⟨101, "c1", .event, "s1", .incomplete⟩,
     ⟨102, "c1", .event, "s2", .incomplete⟩,
     ⟨103, "c1", .event, "s3", .incomplete⟩]
  let batch := processEvents "c1" 2 existing ["s1", "s2", "s3", "n1", "n2", "n3"]
  guard (batch.suppressedKeys == ["s1", "s2", "s3"])
  guard (batch.createdKeys == ["n1", "n2"])
  guard (batch.deferredKeys == ["n3"])
  -- command 変更・無効化で fault_count リセット
  guard ((syncFaultCount "a.sh" "b.sh" true s3).faultCount == 0)
  guard ((syncFaultCount "a.sh" "a.sh" false s3).faultCount == 0)
  guard ((syncFaultCount "a.sh" "a.sh" true s3).faultCount == 2)
  -- 4 段階 tick 予算 (既定値: 240s / finalize 2s / commit 2s / cleanup 6.5s を ms で表現)
  let b : TickBudget := ⟨0, 240000, 2000, 2000, 6500⟩
  guard (b.tickDeadline == 240000)
  guard (b.endWorkDeadline == 238000)
  guard (b.commitStartDeadline == 236000)
  guard (b.execDeadline == 229500)
  -- 定時開始 (229.5s) では 5s の SIGTERM 猶予をそのまま維持
  guard (phaseDeadline 229500 5000 b.commitStartDeadline == 234500)
  -- 開始遅延 (233.5s) でも cleanup 期限 (236s) を超えず commit 予算 [236s, 238s] を保全
  guard (phaseDeadline 233500 5000 b.commitStartDeadline == 236000)
  -- 既に cleanup 期限超過 (236.5s) なら追加の待ち時間なし
  guard (phaseDeadline 236500 5000 b.commitStartDeadline - 236500 == 0)
  -- mirror 同期の busy timeout は endWorkDeadline (238s) で cap され、超過時は 0
  guard (cappedMirrorBusy 237000 b.endWorkDeadline 5000 == 1000)
  guard (cappedMirrorBusy 238500 b.endWorkDeadline 5000 == 0)
  pure true) == some true

end CheckRunner
