// Cue check runner の状態遷移モデル (設計書 issue-701-v9)
// 対象: due 判定 (成功系/失敗系の基準切替と時刻巻き戻り)、
//       (check, kind, dedup_key) 単位の重複抑制、
//       故障 counter と故障タスクの状態遷移、aborted の非決定遷移

open util/integer

abstract sig Outcome {}
one sig OkSilent, Fired, Suppressed, Mixed, FailedExit, FailedTimeout, FailedOutput, Aborted extends Outcome {}

fun SuccessOutcomes : set Outcome { OkSilent + Fired + Suppressed + Mixed }
fun FailureOutcomes : set Outcome { FailedExit + FailedTimeout + FailedOutput }

abstract sig TaskKind {}
one sig KindEvent, KindFault extends TaskKind {}

abstract sig DedupKey {}
one sig EmptyKey, KeyA, KeyB extends DedupKey {}

sig Check {
  interval : one Int,
  grace : one Int,
  faultThreshold : one Int,
  var enabled : one Int,          // 1 = enabled, 0 = disabled
  var commandVer : one Int,       // command の版 (変更検知用)
  var lastStarted : lone Int,
  var lastActualStarted : lone Int,
  var lastOutcome : lone Outcome,
  var faultCount : one Int,
  var lastRunOutcome : lone Outcome,
  var lastClockAnomaly : one Int  // 1 = anomaly, 0 = normal
}

sig Task {
  ownerCheck : one Check,
  kind : one TaskKind,
  key : one DedupKey,
  var exists : one Int,           // 1 = created, 0 = not yet created
  var incomplete : one Int        // 1 = incomplete, 0 = completed
}

one sig Clock {
  var tickStarted : one Int
}

fact StaticConstraints {
  all c : Check {
    c.interval = 3
    // Lean の grace(interval) = min(30, floor(interval / 10)) と整合 (interval=3 -> 0)
    c.grace = div[c.interval, 10]
    c.faultThreshold = 2
  }
}

pred hasIncomplete[c : Check, k : TaskKind, dk : DedupKey] {
  some t : Task | t.exists = 1 and t.incomplete = 1 and t.ownerCheck = c and t.kind = k and t.key = dk
}

pred isClockAnomaly[c : Check, now : Int] {
  (some c.lastStarted and now < c.lastStarted) or
  (some c.lastActualStarted and now < c.lastActualStarted)
}

pred isDue[c : Check, now : Int] {
  no c.lastStarted or
  no c.lastActualStarted or
  no c.lastOutcome or
  isClockAnomaly[c, now] or
  (some c.lastOutcome and c.lastOutcome in SuccessOutcomes and plus[now, c.grace] >= plus[c.lastStarted, c.interval]) or
  (some c.lastOutcome and c.lastOutcome in FailureOutcomes and plus[now, c.grace] >= plus[c.lastActualStarted, c.interval])
}

pred init {
  Clock.tickStarted = 0
  all c : Check {
    c.enabled = 1
    c.commandVer = 1
    no c.lastStarted
    no c.lastActualStarted
    no c.lastOutcome
    c.faultCount = 0
    no c.lastRunOutcome
    c.lastClockAnomaly = 0
  }
  all t : Task {
    t.exists = 0
    t.incomplete = 0
  }
}

pred checkUnchanged[c : Check] {
  c.enabled' = c.enabled
  c.commandVer' = c.commandVer
  c.lastStarted' = c.lastStarted
  c.lastActualStarted' = c.lastActualStarted
  c.lastOutcome' = c.lastOutcome
  c.faultCount' = c.faultCount
  c.lastRunOutcome' = c.lastRunOutcome
  c.lastClockAnomaly' = c.lastClockAnomaly
}

pred allTasksUnchanged {
  all t : Task {
    t.exists' = t.exists
    t.incomplete' = t.incomplete
  }
}

// 1. tick skip / config_error: どの check の状態も変えない
pred stepNoOpTick {
  Clock.tickStarted' >= 0
  Clock.tickStarted' <= 5
  all c : Check | checkUnchanged[c]
  allTasksUnchanged
}

// 2. check 実行 (正常系 / 失敗系 / aborted)
pred stepRunCheck[c : Check, o : Outcome, now : Int, actual : Int] {
  c.enabled = 1
  now >= 0 and now <= 5
  actual >= now and actual <= 5
  isDue[c, now]
  Clock.tickStarted' = now
  all other : Check - c | checkUnchanged[other]
  c.enabled' = c.enabled
  c.commandVer' = c.commandVer
  c.lastRunOutcome' = o
  c.lastClockAnomaly' = (isClockAnomaly[c, now] implies 1 else 0)

  o = Aborted implies {
    // aborted: checks の実行状態カラムは一切更新しない
    c.lastStarted' = c.lastStarted
    c.lastActualStarted' = c.lastActualStarted
    c.lastOutcome' = c.lastOutcome
    c.faultCount' = c.faultCount
    allTasksUnchanged
  } else {
    c.lastStarted' = now
    c.lastActualStarted' = actual
    c.lastOutcome' = o

    o in SuccessOutcomes implies {
      c.faultCount' = 0
      // Fired / Mixed のとき、未完了でない (c, KindEvent, dk) のイベントタスクを 1 件起票できる
      (o = Fired or o = Mixed) implies {
        some t : Task |
          t.exists = 0 and t.ownerCheck = c and t.kind = KindEvent and
          not hasIncomplete[c, KindEvent, t.key] and
          t.exists' = 1 and t.incomplete' = 1 and
          (all other : Task - t | other.exists' = other.exists and other.incomplete' = other.incomplete)
      } else {
        allTasksUnchanged
      }
    } else {
      // FailureOutcomes
      hasIncomplete[c, KindFault, EmptyKey] implies {
        // 未完了の故障タスクが存在する間は faultCount を進めない (0 のまま維持)
        c.faultCount' = 0
        allTasksUnchanged
      } else {
        plus[c.faultCount, 1] >= c.faultThreshold implies {
          // 閾値到達で故障タスクを 1 件起票し faultCount を 0 に戻す
          c.faultCount' = 0
          some t : Task |
            t.exists = 0 and t.ownerCheck = c and t.kind = KindFault and t.key = EmptyKey and
            t.exists' = 1 and t.incomplete' = 1 and
            (all other : Task - t | other.exists' = other.exists and other.incomplete' = other.incomplete)
        } else {
          c.faultCount' = plus[c.faultCount, 1]
          allTasksUnchanged
        }
      }
    }
  }
}

// 3. タスクの完了 (抑制解除)
pred stepCompleteTask[t : Task] {
  t.exists = 1 and t.incomplete = 1
  Clock.tickStarted' = Clock.tickStarted
  all c : Check | checkUnchanged[c]
  t.exists' = 1 and t.incomplete' = 0
  all other : Task - t | other.exists' = other.exists and other.incomplete' = other.incomplete
}

// 4. TOML 設定同期 (command 変更または無効化で fault_count を 0 に戻す)
pred stepConfigSync[c : Check, newEnabled : Int, newCmdVer : Int] {
  (newEnabled = 0 or newEnabled = 1)
  (newCmdVer = 1 or newCmdVer = 2)
  Clock.tickStarted' = Clock.tickStarted
  all other : Check - c | checkUnchanged[other]
  allTasksUnchanged
  c.enabled' = newEnabled
  c.commandVer' = newCmdVer
  c.lastStarted' = c.lastStarted
  c.lastActualStarted' = c.lastActualStarted
  c.lastOutcome' = c.lastOutcome
  c.lastRunOutcome' = c.lastRunOutcome
  c.lastClockAnomaly' = c.lastClockAnomaly
  (newEnabled = 0 or newCmdVer != c.commandVer) implies {
    c.faultCount' = 0
  } else {
    c.faultCount' = c.faultCount
  }
}

pred step {
  stepNoOpTick or
  (some c : Check, o : Outcome, now : Int, actual : Int | stepRunCheck[c, o, now, actual]) or
  (some t : Task | stepCompleteTask[t]) or
  (some c : Check, en : Int, cv : Int | stepConfigSync[c, en, cv])
}

fact Trace {
  init
  always step
}

// --- Safety 検証 (すべて反例なし: expect 0) ---

assert LastOutcomeNeverAborted {
  always (all c : Check | c.lastOutcome != Aborted)
}

assert FaultCounterBoundedAndGated {
  always (all c : Check |
    c.faultCount >= 0 and
    c.faultCount < c.faultThreshold and
    (hasIncomplete[c, KindFault, EmptyKey] implies c.faultCount = 0))
}

assert NoDuplicateIncompleteTasks {
  always (all disj t1, t2 : Task |
    (t1.exists = 1 and t1.incomplete = 1 and t2.exists = 1 and t2.incomplete = 1) implies
    not (t1.ownerCheck = t2.ownerCheck and t1.kind = t2.kind and t1.key = t2.key))
}

assert AbortedLeavesCheckStateIntact {
  always (all c : Check |
    (some now : Int, actual : Int | stepRunCheck[c, Aborted, now, actual]) implies (
      c.lastStarted' = c.lastStarted and
      c.lastActualStarted' = c.lastActualStarted and
      c.lastOutcome' = c.lastOutcome and
      c.faultCount' = c.faultCount
    ))
}

assert FailureDueRespectsActualStart {
  always (all c : Check, o : Outcome, now : Int, actual : Int |
    (stepRunCheck[c, o, now, actual] and
     some c.lastOutcome and
     c.lastOutcome in FailureOutcomes and
     not isClockAnomaly[c, now]) implies
      plus[actual, c.grace] >= plus[c.lastActualStarted, c.interval])
}

assert IntervalThreeGraceBoundary {
  always (all c : Check |
    (c.lastStarted = 0 and c.lastActualStarted = 0 and some c.lastOutcome) implies
      (not isDue[c, 2] and isDue[c, 3]))
}

check LastOutcomeNeverAborted for 1 Check, 4 Task, 5 int, 8 steps expect 0
check FaultCounterBoundedAndGated for 1 Check, 4 Task, 5 int, 8 steps expect 0
check NoDuplicateIncompleteTasks for 1 Check, 4 Task, 5 int, 8 steps expect 0
check AbortedLeavesCheckStateIntact for 1 Check, 4 Task, 5 int, 8 steps expect 0
check FailureDueRespectsActualStart for 1 Check, 4 Task, 5 int, 8 steps expect 0
check IntervalThreeGraceBoundary for 1 Check, 4 Task, 5 int, 8 steps expect 0

// --- Witness (非自明性の確認: expect 1) ---

// 名前空間分離: 同一 check・EmptyKey で event タスクと fault タスクが同時に未完了になれる
run NamespaceIsolationWitness {
  eventually (some c : Check |
    hasIncomplete[c, KindEvent, EmptyKey] and hasIncomplete[c, KindFault, EmptyKey])
} for 1 Check, 3 Task, 5 int, 6 steps expect 1

// 故障タスク起票 → 完了 → 再起票のライフサイクルが到達可能
run FaultLifecycleWitness {
  eventually (some disj t1, t2 : Task |
    t1.kind = KindFault and t2.kind = KindFault and
    t1.exists = 1 and t1.incomplete = 0 and
    t2.exists = 1 and t2.incomplete = 1)
} for 1 Check, 3 Task, 5 int, 8 steps expect 1

// 失敗途中に Aborted が挟まっても faultCount が維持され、次の失敗で故障タスクが起票される
run AbortedDuringFailureStreakWitness {
  eventually (some c : Check |
    c.faultCount = 1 and
    (after (c.lastRunOutcome = Aborted and c.faultCount = 1 and
      (after (hasIncomplete[c, KindFault, EmptyKey] and c.faultCount = 0)))))
} for 1 Check, 3 Task, 5 int, 6 steps expect 1
