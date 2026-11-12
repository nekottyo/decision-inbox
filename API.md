# 接続仕様

Python 3 の標準ライブラリのみを使用。すべての API は JSON、状態変更は `Content-Type: application/json`。ブラウザーからの状態変更は同一 origin のみ許可し、CLI 等の Origin なしの呼び出しは loopback Host 検査後に許可。CORS は提供しない。

## 依頼の登録

`POST /api/decisions` → `201` と依頼全体。`id` と `created_at` は server が付与。同じ `request_key` の再送でも `201` で既存案件を返し、新しい案件は作らない。以下が投入例。

```json
{
  "title": "レビュー指摘への対応を決める",
  "source": "開発チーム",
  "reason": "今回の変更に共通化まで含めるか判断が必要",
  "background": "リリースは明日の予定",
  "priority": "urgent",
  "urgency_reason": "修正方針が決まるまでレビューが停止中",
  "deadline": null,
  "options": [
    {"id": "minimal", "title": "必要な修正に絞る", "benefits": "確認範囲が小さい", "concerns": "重複が残る"},
    {"id": "refactor", "title": "共通化まで行う", "benefits": "重複を減らせる", "concerns": "公開が遅れる可能性"}
  ],
  "recommended": "minimal",
  "recommendation": "影響範囲を限定して先に公開するため",
  "links": [{"url": "https://github.com/example/repo/pull/1", "label": "対象の PR", "kind": "pr"}],
  "image": null,
  "request_key": "run-1/report-1"
}
```

必須は `title/source/reason/options/recommended/recommendation`。`priority` の既定は `normal`、`background/urgency_reason` は空文字、`links` は空配列、`deadline/image` は null。候補は 2〜8 件で重複しない ID。priority は `urgent/normal/later`。deadline は timezone 付き ISO 8601。リンクは http(s) URL、kind は `pr/issue/other`、label 必須。image は `{"url":"https://example.com/diagram.png","alt":"図の説明"}` または許可済み `/assets/` 内の静的画像参照。任意 HTML は受け付けない。body 上限は 128 KiB。title/source は 200 文字、reason/recommendation は 4000 文字、background は 12000 文字、urgency_reason は 2000 文字、memo は 2000 文字、リンクは 20 件まで。未知の項目・重複 JSON キーは拒否。

任意の `request_key` は 1〜200 文字の英数字と `_ . : / -` で指定。同じキーかつ既定値補完後の同一 JSON 内容なら同じ ID を返す。同じキーで内容が異なれば `409`。JSON オブジェクトのキー順は問わず、候補やリンクの配列順は内容の一部として扱う。キーなしは従来どおり毎回登録する。

## 一覧・詳細・判断

- `GET /api/decisions` → `{"decisions":[依頼全体...],"demo":false}`
- `GET /api/decisions/{id}` → 依頼全体
- `GET /api/tasks` → `{"lanes":[...]}` (`task list --json` と同じ形)
- `POST /api/decisions/{id}/answer`、body `{"choice":"minimal","memo":"任意のメモ"}` → 保存後の依頼全体

依頼全体は登録内容に `id/created_at/answer` を加えたもの。未判断の `answer` は null。判断済みは `{"choice":"minimal","memo":"...","decided_at":"UTC ISO 8601","status":"pending","reason":"","next_step":"","reported_at":null,"can_undo":true}`。status は `pending/processing/completed/blocked/failed`。`reason/next_step` は consumer の停止理由と次の操作で、依頼側の `reason` とは別。`reported_at` は停止報告の時刻で、停止していなければ null。二重判断は `409`、入力は上書きしない。

`POST /api/decisions/{id}/undo`、body `{"decided_at":"一覧で取得した値"}` で未着手の回答を撤回し、依頼全体を `answer:null` で返す。`can_undo:true` は pending かつ一度も claim されていない状態。処理開始済み・古い回答・他画面で撤回済みなら `409`。元の回答とメモは DB の `answer_history` に保持し、履歴の削除 API は提供しない。

## 回答済みの処理

- `POST /api/claim`、body `{"worker":"front-desk","lease_seconds":300,"key_prefix":"任意"}` → `{"decision":依頼全体,"token":"秘密の claim token","worker":"front-desk","expires_at":"UTC ISO 8601"}`。対象がなければ `{"decision":null}`
- `POST /api/decisions/{id}/complete`、body `{"token":"claim 時の token"}` → 完了後の依頼全体
- `POST /api/decisions/{id}/renew`、body `{"token":"現在の token","lease_seconds":3600}` → `{"decision":依頼全体,"expires_at":"延長後の UTC ISO 8601"}`。現時点で有効な token だけ延長でき、期限切れ・古い token・完了後は `409`。更新しても token は変わらず、元の期限より短くしない
- `POST /api/decisions/{id}/report`、body `{"token":"現在の token","status":"blocked","reason":"承認が必要","next_step":"承認後に再試行"}` → 保存後の依頼全体。status は `blocked/failed` のみ、reason/next_step はそれぞれ必須の 1〜4000 文字。現時点で有効な処理中 token が必要。報告後は lease と token を解放し、自動 claim・回答通知の対象から外す
- `POST /api/decisions/{id}/retry`、body `{"decided_at":"現在の回答時刻","reported_at":"現在の停止報告時刻"}` → pending に戻した依頼全体。人が明示的に再試行するときだけ使用する。blocked/failed と両時刻の完全一致が必要で、古い報告や完了済みなら `409`。停止理由と次の操作は履歴に残して現在欄を空に戻す。過去の claim 記録は保持するため再試行後も取り消しは不可

claim の body と CLI `--key-prefix` に任意の `key_prefix` を渡すと、request_key がその接頭辞で始まる判断だけを対象にする (request_key なしは対象外)。`watch --key-prefix` も同じ。文字種と最大長は request_key と同じで、未指定時の挙動は変わらない。複数 run が同じ DB を共有するときは、起票時の request_key に run 固有の接頭辞を付けて指定する。

lease_seconds は省略時 300、1〜3600 秒。front-desk は 3600 秒を指定。回答日時順 (同時刻は ID 順) の FIFO。期限切れの processing は再 claim 可能。有効かつ一致する token のみ complete 可能、それ以外は `409`。consumer は decision ID で冪等に処理し、外部副作用前に適用済みか確認。claim token は通常の一覧・詳細・履歴には含めない。

処理失敗や承認待ちは complete せず、期限内に report して停止理由を残す。report できなかった状態で lease が切れた場合だけ再配送される。renew だけで二重実行を防げるとは扱わず、再取得や再試行の前に適用済みか確認する。

## CLI

```sh
python3 app.py --db /tmp/decision-test.sqlite3 serve --port 4317
python3 app.py --db /tmp/decision-test.sqlite3 add --file request.json
python3 app.py --db /tmp/decision-test.sqlite3 list
python3 app.py --db /tmp/decision-test.sqlite3 watch --interval 2 --timeout 1740
python3 app.py --db /tmp/decision-test.sqlite3 tui --web-url http://127.0.0.1:4317/
python3 app.py --db /tmp/decision-test.sqlite3 claim --worker front-desk --lease-seconds 300
python3 app.py --db /tmp/decision-test.sqlite3 complete --id ID --token TOKEN
python3 app.py --db /tmp/decision-test.sqlite3 undo --id ID --decided-at ANSWER_TIMESTAMP
python3 app.py --db /tmp/decision-test.sqlite3 renew --id ID --token TOKEN --lease-seconds 3600
python3 app.py --db /tmp/decision-test.sqlite3 report --id ID --token TOKEN --status blocked --reason "承認が必要" --next-step "承認後に再試行"
python3 app.py --db /tmp/decision-test.sqlite3 retry --id ID --decided-at ANSWER_TIMESTAMP --reported-at REPORT_TIMESTAMP
python3 app.py --db /tmp/decision-test.sqlite3 backup --output /tmp/decision-backup.sqlite3
python3 app.py --db /tmp/decision-test.sqlite3 task add --id ID --title 題名 [--request R] [--pane P] [--run RUN] [--result R] [--remaining R] [--lane L] [--decision DECISION_ID]
python3 app.py --db /tmp/decision-test.sqlite3 task move --id ID --lane worker
python3 app.py --db /tmp/decision-test.sqlite3 task update --id ID [--title T] [--request R] [--pane P] [--run RUN] [--result R] [--remaining R] [--decision DECISION_ID]
python3 app.py --db /tmp/decision-test.sqlite3 task delete --id ID
python3 app.py --db /tmp/decision-test.sqlite3 task list [--lane L] [--json]
```

`task` は依頼の進み具合を管理する機能で、書き込みは CLI だけが行う。HTTP は一覧の `GET /api/tasks` (`list --json` と同じ形) だけを出し、Web の task 画面はこれを読み取り専用で表示する。lane は `todo` / `human_decision` / `worker` / `human_action` / `done`。`add` は同じ id で登録内容 (title / request / pane / run) が同じなら既存を返し、違えば 409。lane / result / remaining / decision_id は move / update で進むため比べず、再送しても既存の値は変わらない。`--decision` は 32 桁 hex 以外を 400、存在しない判断を 404 にする。不正な `--lane` は `list` / `list --json` / `add` / `move` とも 400。`update` は指定した項目だけを変え、空文字で消去する (`--decision` の空文字は関連付けの解除)。`list --json` は `{"lanes":[{"id","label","tasks":[...]}]}` を返し、各 task の `decision` は判断が無ければ null、あれば `{"title":"判断の題名","answered":false}` (判断 ID が残っていて判断が見つからないときも null)。`--json` なしは lane ごとの文字の一覧を出す。task 系は DB を書き込み可で開くため、tasks 表が無い DB には表を足す (`PRAGMA user_version` は 2 のまま)。

`tui`、`watch`、`backup`、`task` を除く CLI は対応する API と同じ JSON を stdout に返す。エラーは stderr と非ゼロ終了。既定 DB は `--db`、環境変数 `DECISION_INBOX_DB`、repo 外の user data directory の順で決定。`app.py` は executable で、PATH 上の symlink 経由でも同じ CLI を利用可能。`serve --demo` は別の一時 DB にデモだけを投入し、実 DB を使用しない。server は `127.0.0.1` のみ、`--port 0` は空きポートを選び起動 URL を出力。

エラー body は `{"error":"説明"}`。入力不正 `400`、未発見 `404`、競合 `409`、body 過大 `413`、JSON 以外 `415`、Host/origin 不正 `403`、永続化エラー `500`。削除・実データ初期化 API は提供しない。

`tui` は対話端末で同じ Store を利用し、選択した回答を既存の回答保存処理に渡す。非 TTY では DB を作らずエラー終了。`--web-url` / `CUE_WEB_URL` は任意で loopback の http(s) URL のみ。Web の `?decision=<id>` は対象の未判断または判断済みを初期表示し、不明な ID では通常の優先度順表示になる。

## 回答の通知

`watch --interval 2 --timeout 1740` は DB を読み取り専用で開き、処理可能になった回答の ID だけを stdout へ JSONL で即時出力する。HTTP server の起動は不要で、未作成の DB を新規作成しない。長行を分割する通知先でも各 JSON が欠けないよう、1 行あたり最大 100 ID に分ける。

```json
{"event":"cue.ready","decision_ids":["回答済みの ID"]}
```

対象は pending または lease の期限が切れた processing。未回答・完了・blocked/failed・有効な lease の処理中案件を除外し、ID は claim と同じ回答日時順。起動時に既存の対象も通知し、同じ対象が処理可能なままなら繰り返し通知しない。対象外になった後に再び処理可能になると再通知する。ポーリング間に完了した状態遷移は検知できないため、通知を完全なイベント履歴として扱わない。再起動時は処理可能な対象を再通知する。

`--key-prefix` を指定すると request_key がその接頭辞で始まる判断だけを通知する。`--interval` と `--timeout` は正の有限秒数で、既定は 2 秒と 1740 秒。timeout または Ctrl+C で正常終了し、DB エラーは stderr の JSON と非ゼロ終了で通知する。timeout は通知先が終了を受けて張り直すための境界。watch 自体は agent を起動せず、claim・complete・再送・ハーネス連携を行わない。本文・回答内容・メモ・token を通知に含めない。通知先はその時点の状態を CLI で再取得し、有効な claim の取得後にだけ処理する。

## 保存形式の更新とバックアップ

書き込み可能な Store を初めて開くと、旧形式の DB を schema version 2 へ移行する。旧形式の全行・ID・回答・worker・有効 token・lease を保持し、移行と履歴テーブル作成を 1 transaction で完了させる。失敗時は元の schema と行をロールバックする。未知の schema / version は自動変換しない。

移行の前に `<元 DB>.pre-v2-<UUID>.sqlite3` へ SQLite online backup を作る。バックアップを作成できなければ移行しない。ファイルは mode 0600 で、元の回答や処理 token も含むため秘密として扱う。移行済みの DB を開き直しても再移行や追加の自動バックアップは行わない。

`backup --output NEW_PATH` は source を読み取り専用で開き、稼働中の server や WAL にある commit 済みデータを含めて整合した単一 SQLite ファイルを作る。既存ファイル・symlink・元 DB と同じ宛先は拒否し、上書きしない。出力先の親ディレクトリは先に作成しておく。mode 0600 の一時ファイルへ保存し、整合性確認後に新しい宛先へ原子的に公開する。結果は `{"backup":"絶対パス"}`。この CLI は DB の新規作成や schema 移行を行わない。

復元は server と consumer を停止し、既存 DB と WAL/SHM を退避してからバックアップを新しい DB パスへ配置し、同じ `--db` で開く。元 DB を直接上書きする復元コマンドは提供しない。
