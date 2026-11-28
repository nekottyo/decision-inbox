# claim の所有権と再試行

回答済みの判断 1 件について、claim による処理開始、期限切れ後の再取得、renew、report、明示的な retry、complete を [Lean](ClaimLease.lean) で検査する。1 件に同時に有効な token は 1 つまで。有効な claim を上書きできず、期限切れまたは別の token による complete・report・renew は拒否される。renew は token を維持し、期限を短くしない。停止後の再試行には現在の報告が必要で、完了は再試行や再取得の対象外。

## 実装との対応

| モデル | 実装 |
| --- | --- |
| `claim` と `processing` | `Store.claim` が書き込みロックを取得してから、pending または期限切れの行を選び、token と期限を更新 |
| `finishClaim` | commit 前の失敗では元の行に戻る。commit が成功したら更新後の行を保持し、その直後に現在時刻が期限未満のときだけ取得成功を返す。期限ちょうどから新しい token で再取得可能 |
| `active`、`complete`、`report`、`renew` | `Store.active` が状態・token・期限を照合。各書き込みは同じ transaction 内で行い、renew は現在の期限と要求期限の最大値を使用 |
| `retry` | `Store.retry` が現在の回答と停止報告の時刻を照合して pending に戻す。モデルでは回答を固定し、その回答に対する停止報告を claim の世代で表現 |

前提は、SQLite の `BEGIN IMMEDIATE` が書き込みを直列化し、発行された token が再利用されないこと。時刻は境界 `now < leaseUntil` を保つ自然数で表す。blocked と failed は所有権に関して同じ扱いなので一つの状態にまとめる。停止報告は 1 claim につき最大 1 回で、再試行後の新しい claim とは区別される。

検査対象は所有権に関する状態遷移。選択順序、人による close、時刻の巻き戻り、token ファイル、外部処理の冪等性は、API の規約と実装テストで扱う。

## 検証

リポジトリのルートで実行する。証明の型検査に加え、サンプルの 1 件が claim、期限切れ、再取得、短い renew、停止、再試行、完了まで進むことを `#guard` で検査する。

```sh
lean +leanprover/lean4:v4.36.0 proofs/ClaimLease.lean
lean +leanprover/lean4:v4.36.0 proofs/TaskAssignment.lean
lean +leanprover/lean4:v4.36.0 proofs/DecisionSnapshot.lean
lean +leanprover/lean4:v4.36.0 proofs/SessionRegistry.lean
lean +leanprover/lean4:v4.36.0 proofs/ReleaseSwitch.lean
lean +leanprover/lean4:v4.36.0 proofs/DeliveryAttempt.lean
lean +leanprover/lean4:v4.36.0 proofs/CheckRunner.lean
python3 -m unittest discover -s tests -p 'test_*.py'
```

## 配送試行の投稿と受領

[DeliveryAttempt.lean](DeliveryAttempt.lean) は、依頼 thread への人の投稿 (`posts` 表) について、投稿と受領の状態遷移を検査する。投稿は常に対象の依頼が人待ち理由で止まっているかという文脈だけから分類 (`expected` / `unexpected`、想定内 / 想定外) を導いて未受領で生まれ、投稿者は分類を選べない。受領は未受領からのみ起き、分類・宛先・投稿時刻を変えず、受領済みは終端で二度目の受領は拒否される。宛先が不在 (`target = none`) の投稿も未受領のまま残り、後から受領できることを `#guard` で検査する。

| モデル | 実装 |
| --- | --- |
| `classify` | `Store.create_post` が投稿時点の task の lane (人待ちの lane か) から分類を導く |
| `post` と `target` | `Store.create_post` が所属記録 (`sessions` 表) の現在の担当 worker を宛先に写し、担当不在なら宛先なしで挿入 |
| `receive` | `Store.receive_post` が `BEGIN IMMEDIATE` 内で未受領の行だけを受領済みに更新し、受領済みなら 409 で拒否 |

前提は、SQLite の `BEGIN IMMEDIATE` が書き込みを直列化すること。投稿の本文の検査、宛先の解決、HTTP の入力検査は `tests/test_board.py` で検査する。worker inbox ファイルへの実体化は Cue の外 (Pull 配送側) の接続点で、このモデルの対象外。

実装テストでは、SQLite の独立した接続から競合して claim したときの勝者、読み取りロックによる commit 待ちでの失効、commit 失敗時の元の行、再取得後の旧 token、古い停止報告、短い renew を照合する。モデルの時刻は commit 直後の照合時点で、応答の転送時間は含まない。関連ファイルの変更では、CI が上記と同じ証明と実装テストを実行する。


## 担当を更新する条件

[TaskAssignment.lean](TaskAssignment.lean) は、旧 lane・pane・machine と、指定があれば旧 run・request・kind が一致したときだけ担当を更新できることを検査する。一致後に同じ担当を指定すると、モデル上の revision も含めて行はそのままになる。担当が変わる場合も、所属と remaining は保持する。

`Store.reconcile_lane` は `BEGIN IMMEDIATE` で SQLite の書き込みを直列化し、比較条件を `UPDATE` の `WHERE` に入れる。同値の確認も同じ transaction 内で行い、実装では `updated_at` を進めず既存の行を返す。実装テストでは 6 項目それぞれの不一致、空文字の比較、同じ行を観測した 2 つの CLI の競合を検査する。モデルの範囲は Cue 内の 1 行。Herdr の pane 起動・終了と一体の操作にはならない。

完了を照合した caller は remaining・decision_id・updated_at を同時に指定できる。Lean の revision は task の更新時刻に対応し、`decisionSettled` は紐付いた判断が完了済みか取り下げ済みであることを表す。変更前の照合値や未処理の判断で done に移せないことを検査する。実装は task を更新するたびに同じ行の更新時刻を進め、lane を書く `UPDATE` で判断の現在の状態も検査する。PR と pane の状態の確認は caller が行う。task を削除して同じ ID で再登録する操作はモデルの範囲外である。

## 判断の一覧と詳細の読み取り

[DecisionSnapshot.lean](DecisionSnapshot.lean) は、最初の読み取りから応答までに他の接続が何回確定しても、一覧 (履歴から先に読む) と詳細 (本文から先に読む) の本文・revision・履歴が最初の読み取り時点と一致することを検査する。revision は更新履歴の件数とする。

`Store.list` と `Store.get` の `BEGIN` から、SQLite が最初の SELECT のスナップショットをトランザクション終了まで保持することを前提とする。モデルは判断 1 件の本文と更新履歴を扱う。JSON の項目、並び順、取り下げ、古い revision への回答拒否と、実際の 2 接続の動作は Python の回帰テストで検査する。

## セッション所属の開閉と一意性

[SessionRegistry.lean](SessionRegistry.lean) は、Claude・Codex の窓口・worker・Cue 専任 worker の所属記録 (`sessions` 表) について、`open`・`close`・`sample` の状態遷移を検査する。開いた状態 (`closed_at IS NULL`) で `(cli, native_session_id)` と `(run_id, role, pane, socket)` がそれぞれ一意であること、重複する `open` が拒否されること、閉じた行が後続の `open`・`close` で書き換わらないこと、`close` が `id`・`run_id`・`role`・`req_id`・`pane`・`socket`・`cli`・`native_session_id`・`opened_at` を保持したまま終了時刻と理由だけを入れること、未登録・既閉鎖の `close` が拒否されることを証明し、`#guard` で一連の開閉を検査する。

| モデル | 実装 |
| --- | --- |
| `openSession` と `WellFormed` | `Store.open_session` が `BEGIN IMMEDIATE` で書き込みを直列化し、重複を検査して挿入。SQLite の部分一意索引 `sessions_open_native (cli, native_session_id) WHERE closed_at IS NULL` と `sessions_open_slot (run_id, role, pane, socket) WHERE closed_at IS NULL` でも一意性を担保 |
| `sampleRow` | `Store.sample_session` が `BEGIN IMMEDIATE` 内で行 ID・終了時刻・現在の UTC 時刻を照合し、計測列だけを単一の `UPDATE` で最大値に更新。別の行と所属・終了情報を保持し、終了後または現在より後の観測を拒否すること、累積値が減らないことを証明し、再試行と欠測は `#guard` でも検査 |
| `closeSession` (`byNative` / `bySlot`) | `Store.close_session` が `BEGIN IMMEDIATE` 内で `closed_at IS NULL` の一致行を引き、0 件なら 404、複数件 (`--pane` / `--socket` / `--run-id` で role が重複する場合) なら 409 で拒否し、1 件のときだけ `closed_at` と `close_reason` を更新 |
| `Closure` (`closedAt`, `reason`) | 開いた行は `closed_at IS NULL` かつ `close_reason = ''`、`close` 時にのみ `closed_at` (ISO 8601) と `close_reason` (`normal` / `replaced` / `crashed` / `closed`) を同時設定 |

前提は、SQLite の `BEGIN IMMEDIATE` が書き込みを直列化すること、部分一意索引 `WHERE closed_at IS NULL` が開いた行にだけ効くこと、および `INTEGER PRIMARY KEY` が一意な単調 ID を割り当てること。時刻は自然数で表し、書き込みトランザクションの順序に対して UTC の壁時計が逆転しないと仮定する。transcript の集計と観測時刻の取得は収集側の前提とする。文字列長・サロゲート・ISO 8601 の解析、`session list` の絞り込み (`--run-id` / `--cli` / `--open-only` / `--since` / `--until`)、SQLite の 2 接続競合、および `PRAGMA user_version = 2` を保った旧 release DB 互換は `tests/test_sessions.py` で検査する。


## 公開 release の切り替え

[ReleaseSwitch.lean](ReleaseSwitch.lean) は事前検証の成功後にだけ候補を有効にできること、事前検証の失敗で現行 release が残ること、切り替え後の検査に失敗した場合に復旧が成功すれば旧 release に戻ることを扱う。更新コマンドではファイルロックにより一度に一件だけ更新を進め、`current` の入れ替えに同一ファイルシステム内の atomic rename を使う。モデルは処理段階とリンク先を別々に持つ。同じ rename の境界で旧 release、候補、外部のリンク先を観測でき、割り込み時はその観測から復元の要否を決める。再起動した PID と Web の待ち受けが一致しない観測は成功への遷移に進めない。SHA-256、アーカイブの検証、SQLite の互換性、Web と Herdr の確認は更新コマンドを入口とする Python の回帰テストで扱う。プロセスの強制終了、他の手段による同時書き換え、外部サービスの恒久障害はモデルの対象外。別の参照先を観測したときは処理を停止し、復元を含む後続の書き換えは行わない。

## check runner の due 判定・重複抑制・故障 counter

[CheckRunner.lean](CheckRunner.lean) と [CheckRunner.als](CheckRunner.als) は、`app.py check tick` が扱う due 判定・`(check, kind, dedup_key)` 単位の重複抑制・故障 counter と故障タスクの状態遷移・起票上限 (20 件) を検査する。

- **due 判定**: 履歴なし・時刻巻き戻り (`last_started` または `last_actual_started_at` が `tick_started` より未来) は即 due。通常時は成功系 (`ok_silent` / `fired` / `suppressed` / `mixed`) の後は `last_started`、失敗系 (`failed_exit` / `failed_timeout` / `failed_output`) の後は `last_actual_started_at` を基準とし、`tick_started + grace >= base + interval` (`grace = min(30, interval / 10)`) で判定する。失敗系では `actual_started_at >= tick_started` より実開始時刻同士の間隔も `interval - grace` を下回らないことを証明する
- **outcome 優先順位と aborted**: 優先順位は `failed_timeout > aborted > failed_exit > failed_output > 正常系`。期限切れ時の `failed_timeout` / `aborted` は観測時刻ではなく先に到来した deadline (`timeout_at <= tick_deadline` なら `failed_timeout`、そうでなければ `aborted`) で決定する。`aborted` は非決定的に発生し得る遷移として扱い、`checks` の実行状態カラム (`last_started` / `last_actual_started_at` / `last_outcome` / `fault_count`) を一切変更しないため、`last_outcome` が `aborted` になることは決してない
- **故障 counter と名前空間分離**: 成功系で `fault_count` を 0 に戻す。失敗系は未完了の故障タスク (`kind=fault`, `dedup_key=""`) がある間は `fault_count` を 0 のまま維持して二重起票せず、無いときは `fault_count + 1` が `fault_threshold` 以上で故障タスクを 1 件起票して `fault_count` を 0 に戻す。`kind=event` と `kind=fault` は別名前空間であり、空文字の `dedup_key` でも互いに抑制し合わない。TOML 同期で `command` 変更または無効化が起きたときは `fault_count` を 0 に戻し、`env` / `timeout` のみの変更では保持する
- **起票上限と Liveness**: 1 回の実行で新規起票するタスクは上限 (20 件) 以下で、`suppressed` なイベントは上限枠を消費しない。Liveness として、初期条件 (`fault_threshold >= 1`、`fault_count = 0`、および対象 check の未完了故障タスク (`kind=fault`, `dedup_key=""`) が存在しないこと) と、前処理時間が 60 秒未満に収まる tick が繰り返し発生し途中で別途故障タスクが増えないまま非 `aborted` な失敗が連続するという公平性仮定の下で、`fault_threshold` 回目の失敗で必ず故障タスクが起票されることを証明する (前処理時間の上限自体は公理にしない)。未完了の故障タスクが既に存在する間は上記の重複起票抑止により `fault_count` が 0 のまま保たれて新しい故障タスクは作られず、既存の故障タスクが完了して初期条件に戻った後に改めてカウントが始まる
- **4 段階 tick 予算と絶対期限**: [CheckRunner.lean](CheckRunner.lean) の `TickBudget` / `phaseDeadline` / `cappedMirrorBusy` により、`monoStart ≤ execDeadline ≤ commitStartDeadline ≤ endWorkDeadline ≤ tickDeadline` の単調順を証明する。cleanup の各フェーズ (SIGTERM 猶予 → SIGKILL 回収待ち → drain join) は開始時点の単一時刻サンプルと絶対期限 `commitStartDeadline` の最小値 `min(phaseStart + configured, cleanupDeadline)` で上限するため、定時開始では設定どおりの猶予を保ちつつ、任意の遅延が入っても `commitStartDeadline` を超えず、cleanup が `commitStartDeadline` 内に完了する限り `effectiveCommit` の全量が `endWorkDeadline` まで残ることを証明する。また `sync_checks_mirror` の DB 待ち時間も `endWorkDeadline` までの残り作業時間で上限 (`cappedMirrorBusy`) し、残り時間が無ければ残り予算が 0 になることを証明する。mirror 書き込みを実際に開始しない (skip する) 判断は証明ではなく実装 `run_check_tick` による: `mirror_rem <= 0` のとき `sync_checks_mirror` を呼ばずに `aborted` で終了する (`app.py:3264-3266`)

| モデル | 実装 |
| --- | --- |
| `grace` / `isClockAnomaly` / `dueBase` / `isDue` | `check_is_due` が `tick_started` 固定で due 判定・巻き戻り検知を実施 |
| `classifyOutcome` | `run_check_command` が支配的な deadline (`timeout_at <= tick_deadline` か) で `timed_out` / `aborted` を確定し、`commit_check_run` が `failed_timeout > aborted > failed_exit > failed_output > 正常系` で決定 |
| `hasIncomplete` / `processEvents` | `commit_check_run` が `BEGIN IMMEDIATE` 内で `(check_name, kind, dedup_key)` 単位の未完了タスク (`tasks.lane != 'done'`) を照合し、上限 20 件で新規起票・`suppressed`・`events_deferred` を確定 |
| `applyOutcome` | `commit_check_run` が 1 つの `BEGIN IMMEDIATE` 内で `check_runs` / `check_run_events` / `tasks` / `check_tasks` / `checks` を更新 (`aborted` は `check_runs` のみ) |
| `syncFaultCount` | `sync_checks_mirror` が `command` 変更または無効化時に `fault_count = 0` へリセット |
| `TickBudget` / `phaseDeadline` / `cappedMirrorBusy` | `run_check_tick` が 4 段階期限 (`exec_deadline` / `commit_start_deadline` / `end_work_deadline` / `tick_deadline`) を算出し、`_terminate_process_group` と `run_check_command` が絶対期限 `cleanup_deadline` と同一時計 `mono_fn` で各フェーズを上限。mirror 同期は `end_work_deadline` までの残り作業時間で `busy_timeout_ms` を上限し、残量 0 以下なら書き込みを開始せず終了処理へ進む |

前提は、flock により tick が直列化されること、SQLite の `BEGIN IMMEDIATE` が 1 check 実行の確定を原子的に直列化すること、および単調時計が逆行しないこと。モデルの期限検証は各フェーズが計算上の期限を厳密に守る理想時計上の境界を扱い、OS スケジューリングによる待ち復帰オーバーヘッドや `SIGKILL` 後の uninterruptible sleep (D-state)、finalization 中も別 writer が DB ロックを保持し続けるケース (best-effort 終了処理の限界) は対象外。flock の競合・skip 記録、git checkout 検査、TOML 検証、JSONL 64KiB/スキーマ検証、プロセスグループ SIGTERM/SIGKILL・割り込み時 cleanup、run-inbox への wake (20 秒上限・CB・回収)、実プロセス遅延・SQLite ロック競合下の予算境界テスト、30 日 prune、`PRAGMA user_version = 2` 互換は `tests/test_checks.py` で検査する。
