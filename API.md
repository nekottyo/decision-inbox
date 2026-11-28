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

必須は `title/source/reason/options/recommended/recommendation` (項目と型の一覧は `add --help` でも見られる)。`priority` の既定は `normal`、`background/urgency_reason` は空文字、`links` と `files` (後述) は空配列、`deadline/image` は null。候補は 2〜8 件で重複しない ID。priority は `urgent/normal/later`。deadline は timezone 付き ISO 8601。リンクは http(s) URL、kind は `pr/issue/other`、label 必須。image は `{"url":"https://example.com/diagram.png","alt":"図の説明"}` または許可済み `/assets/` 内の静的画像参照。任意 HTML は受け付けない。body 上限は 128 KiB。title/source は 200 文字、reason/recommendation は 4000 文字、background は 12000 文字、urgency_reason は 2000 文字、memo は 2000 文字、リンクは 20 件まで。未知の項目・重複 JSON キーは拒否。

任意の `request_key` は 1〜200 文字の英数字と `_ . : / -` で指定。同じキーかつ既定値補完後の同一 JSON 内容なら同じ ID を返す。同じキーで内容が異なれば `409`。JSON オブジェクトのキー順は問わず、候補やリンクの配列順は内容の一部として扱う。キーなしは従来どおり毎回登録する。update 済みの判断には、現在の内容に加えて update 前のどの内容で再送しても同じ ID を返す。取り下げ済みの判断と同じキーで add すると `409` (置き換えるときは新しい request_key で add する)。

## 判断に付けるファイル

任意の `files` に `[{"path":"/絶対パス/reply.draft.md","label":"返信の下書き"}]` を 20 件まで指定できる。path は `..` 要素を含まない絶対パス (4096 文字まで)、label は 1〜200 文字。登録時は形式だけを検査し、存在や中身は見ない (下書きを消した後の update や再送を通すため)。空配列と省略は同じ内容として扱い、保存する payload には入れない。依頼全体の `files` は常に配列で返す。update の `changes` にも指定できる。

- `GET /api/decisions/{id}/files/{index}` → `{"path":"...","label":"...","content":"本文"}`

表示のたびにファイルを読むので、登録後に直した内容が見える。任意のパスは受けず、登録済みの `files[index]` だけを読む。読めるのは server の `--files-root` (繰り返し指定できる。既定は環境変数 `CUE_FILES_ROOT` を `:` で区切ったもの、無ければ `~/.agents/state/runs` と `~/.ghq/github.com/nekottyo/obsidian-vault/nekottyo/claude-knowledge/review-repo`) のどれかの下だけで、path と root をそれぞれ realpath で解いてから比べるため、symlink や `..` で root の外へ出るものは `403`。ほかに読む権限が無いものも `403`、存在しない・index 範囲外は `404`、ディレクトリや FIFO など通常ファイル以外と symlink ループなど開けないパスは `400`、256 KiB 超は `413`、UTF-8 として読めないか NUL を含むものは `415`。エラー body の `error` に理由が入る。Web は `content` を等幅のテキストとしてそのまま表示し、HTML として描画しない。TUI は同じ読み取り処理 (`Store.file`) で本文を取り、制御文字を無害化して `$PAGER` (既定 less) に渡す。`tui --files-root` も同じ既定値を使う。

旧 release は `files` を未知の項目として `400` で拒否する。`files` を付けて登録する側は、この版へ入れ替えてから使う。

## 未回答の判断の更新と取り下げ

- `POST /api/decisions/{id}/update`、body `{"note":"更新の理由","changes":{"deadline":"2026-12-01T00:00:00+09:00"}}` → 更新後の依頼全体
- `POST /api/decisions/{id}/withdraw`、body `{"note":"取り下げの理由"}` → 取り下げ後の依頼全体

理由は依頼側の `reason` や report の `reason` と分けるため `note` (1〜2000 文字) と呼ぶ。`changes` は登録時の項目 (title/source/reason/background/priority/urgency_reason/deadline/options/recommended/recommendation/links/files/image) の部分集合で、現在の内容に重ねてから登録時と同じ検査をする。`request_key` は変更できず `400`。重ねた結果が現在と同じなら revision を上げずに現在の依頼全体を返す。回答済み・取り下げ済みの判断の update は `409`。変更前の内容は DB の `decision_changes` に残る。

withdraw は未回答の判断だけを対象にし、DB から行は消さない。取り下げ済みへの再送は同じ結果を `200` で返す。回答済み (claim 中・処理中・完了・停止を含む) は `409`。claim は回答済みしか拾わないため、未回答の取り下げと処理中の worker は重ならない。claim 中の判断を止めたいときは、token を持つ worker が report または complete で閉じる。

依頼全体には `revision` (update の回数、0 始まり)、`updated_at`/`update_note` (最新の update、無ければ null)、`withdrawn` (取り下げ済みなら `{"note":"...","withdrawn_at":"UTC ISO 8601"}`、それ以外は null) が付く。`GET /api/decisions` と CLI `list` は取り下げ済みを返さない (`answer:null` を未判断として扱えるようにするため)。CLI `list --include-withdrawn` で含める。`GET /api/decisions/{id}` は取り下げ済みも返す。

旧 release は `decision_changes` を読まないため、旧 release に戻すと取り下げ済みの判断が未回答として再表示され、更新の履歴も表示されない。

## 一覧・詳細・判断

- `GET /api/decisions` → `{"decisions":[依頼全体...],"demo":false}`
- `GET /api/decisions/{id}` → 依頼全体
- `GET /api/tasks` → `{"lanes":[...]}` (`task list --json` と同じ形)

## Board (所属記録の観測と依頼 thread)

Web の board 画面が使う読み取りと、依頼 thread への投稿 (配送試行) の経路。task の id には `/` を含みうるため、パスの残り全体を id として受ける。

- `GET /api/board` → `{"runs":[runs の全行],"sessions":[sessions の全行],"posts":[posts の全行],"unread":{"posts":未受領の配送試行の件数,"decisions":未回答の判断待ちの件数},"demo":false}`。未読は既存状態の表示で、既読管理はしない
- `GET /api/tasks/{id}/thread` → `{"task":依頼 (判断の要約付き、tasks の一覧と同じ形),"messages":[その run の messages],"posts":[その task への投稿],"sessions":[その run の sessions]}`。messages は本文を持たない (メタデータと状態だけ)。task が無ければ `404`
- `POST /api/tasks/{id}/posts`、body `{"body":"投稿の本文"}` → `201` と投稿全体。投稿は配送試行として `posts` 表に記録する。分類 (`classification`) は投稿時点の task の lane が人待ち (人の判断待ち・人の操作待ち) なら `expected` (想定内)、それ以外は `unexpected` (想定外) と、文脈だけから導く (投稿者は選べない)。宛先 (`target_*`) は所属記録上の現在の担当 worker (対象 run の開いた worker / cue_worker session のうち最後に開いた行) を写し、担当不在なら空で、投稿は未受領 (`state: "unreceived"`) のまま残る。本文は 1〜4000 文字、未知の項目は `400`、task が無ければ `404`
- `POST /api/posts/{id}/receive`、body `{}` または `{"received_by":"受領した session の識別子"}` → 受領後の投稿全体。完全 Pull 配送側が宛先 worker の受領を確認したときに呼ぶ接続点で、worker inbox ファイルへの実体化は Cue の外にある。受領済みへの再受領は `409`、投稿が無ければ `404`

投稿全体は `{"id","task_id","body","classification","state","target_run","target_pane","target_socket","target_session","created_at","received_at","received_by"}`。状態遷移 (未受領からのみ受領でき、分類・宛先・投稿時刻は受領で変わらず、受領済みは終端) は `proofs/DeliveryAttempt.lean` で形式化済み。`posts` 表も足すだけで `PRAGMA user_version` は 2 のまま。
- `POST /api/decisions/{id}/answer`、body `{"choice":"minimal","memo":"任意のメモ","revision":0}` → 保存後の依頼全体。任意の `revision` は表示中の依頼全体の値で、現在の revision と違えば `409` (表示後に内容が更新された)。取り下げ済みは `409`

依頼全体は登録内容に `id/created_at/answer` を加えたもの。未判断の `answer` は null。判断済みは `{"choice":"minimal","memo":"...","decided_at":"UTC ISO 8601","status":"pending","reason":"","next_step":"","reported_at":null,"lease_until":null,"can_undo":true}`。status は `pending/processing/completed/blocked/failed`。`reason/next_step` は consumer の停止理由と次の操作で、依頼側の `reason` とは別。`reported_at` は停止報告の時刻で、停止していなければ null。`lease_until` は claim の lease 期限 (epoch 秒)。claim されていなければ null で、期限を過ぎた processing は再 claim できる。二重判断は `409`、入力は上書きしない。

`POST /api/decisions/{id}/undo`、body `{"decided_at":"一覧で取得した値"}` で未着手の回答を撤回し、依頼全体を `answer:null` で返す。`can_undo:true` は pending かつ一度も claim されていない状態。処理開始済み・古い回答・他画面で撤回済みなら `409`。元の回答とメモは DB の `answer_history` に保持し、履歴の削除 API は提供しない。

## 回答済みの処理

- `POST /api/claim`、body `{"worker":"front-desk","lease_seconds":300,"key_prefix":"任意"}` → `{"decision":依頼全体,"token":"秘密の claim token","worker":"front-desk","expires_at":"UTC ISO 8601"}`。対象がなければ `{"decision":null}`
- `POST /api/decisions/{id}/complete`、body `{"token":"claim 時の token"}` → 完了後の依頼全体
- `POST /api/decisions/{id}/renew`、body `{"token":"現在の token","lease_seconds":3600}` → `{"decision":依頼全体,"expires_at":"延長後の UTC ISO 8601"}`。現時点で有効な token だけ延長でき、期限切れ・古い token・完了後は `409`。更新しても token は変わらず、元の期限より短くしない
- `POST /api/decisions/{id}/report`、body `{"token":"現在の token","status":"blocked","reason":"承認が必要","next_step":"承認後に再試行"}` → 保存後の依頼全体。status は `blocked/failed` のみ、reason/next_step はそれぞれ必須の 1〜4000 文字。現時点で有効な処理中 token が必要。報告後は lease と token を解放し、自動 claim・回答通知の対象から外す
- `POST /api/decisions/{id}/retry`、body `{"decided_at":"現在の回答時刻","reported_at":"現在の停止報告時刻"}` → pending に戻した依頼全体。人が明示的に再試行するときだけ使用する。blocked/failed と両時刻の完全一致が必要で、古い報告や完了済みなら `409`。停止理由と次の操作は履歴に残して現在欄を空に戻す。過去の claim 記録は保持するため再試行後も取り消しは不可

- `POST /api/decisions/{id}/close`、body `{"decided_at":"現在の回答時刻","reported_at":"現在の停止報告時刻"}` → 完了扱いにした依頼全体。もう再処理しない blocked/failed を閉じるときだけ使用する。blocked/failed と両時刻の完全一致が必要で、古い報告や blocked/failed 以外は `409` で何も変えない。status は `completed` になり、停止理由・次の操作・`reported_at` は残すため、通常の完了 (停止理由が空) と見分けられる。閉じる前の状態は `answer_history` に event `close` で残し、再開 API は提供しない

claim の body と CLI `--key-prefix` に任意の `key_prefix` を渡すと、request_key がその接頭辞で始まる判断だけを対象にする (request_key なしは対象外)。`watch --key-prefix` も同じ。文字種と最大長は request_key と同じで、未指定時の挙動は変わらない。複数 run が同じ DB を共有するときは、起票時の request_key に run 固有の接頭辞を付けて指定する。

lease_seconds は省略時 300、1〜3600 秒。front-desk は 3600 秒を指定。回答日時順 (同時刻は ID 順) の FIFO。期限切れの processing は再 claim 可能。有効かつ一致する token のみ complete 可能、それ以外は `409`。consumer は decision ID で冪等に処理し、外部副作用前に適用済みか確認。claim token は通常の一覧・詳細・履歴には含めない。

claim の commit が失敗した場合は元の状態に戻り、永続化エラーになる。commit 成功直後の時刻が lease 期限以上なら、期限切れの processing を残して `409` (CLI は exit 1) を返す。token は返さず、次の claim で再取得できる。`--token-file` で公開済みのファイルもエラー時に削除を試みる。

処理失敗や承認待ちは complete せず、期限内に report して停止理由を残す。report できなかった状態で lease が切れた場合だけ再配送される。renew だけで二重実行を防げるとは扱わず、再取得や再試行の前に適用済みか確認する。

## CLI

```sh
python3 app.py --db /tmp/decision-test.sqlite3 serve --port 4317 [--files-root DIR]
python3 app.py --db /tmp/decision-test.sqlite3 add --file request.json
python3 app.py --db /tmp/decision-test.sqlite3 list [--include-withdrawn]
python3 app.py --db /tmp/decision-test.sqlite3 update --id ID --note "更新の理由" --file changes.json
python3 app.py --db /tmp/decision-test.sqlite3 withdraw --id ID --note "取り下げの理由"
python3 app.py --db /tmp/decision-test.sqlite3 watch --interval 2 --timeout 1740
python3 app.py --db /tmp/decision-test.sqlite3 tui --web-url http://127.0.0.1:4317/ [--files-root DIR]
python3 app.py --db /tmp/decision-test.sqlite3 claim --worker front-desk --lease-seconds 300 [--token-file auto|PATH]
python3 app.py --db /tmp/decision-test.sqlite3 complete --id ID (--token=TOKEN | --token-file PATH)
python3 app.py --db /tmp/decision-test.sqlite3 claims [--id ID]
python3 app.py --db /tmp/decision-test.sqlite3 hooks
python3 app.py --db /tmp/decision-test.sqlite3 undo --id ID --decided-at ANSWER_TIMESTAMP
python3 app.py --db /tmp/decision-test.sqlite3 renew --id ID (--token=TOKEN | --token-file PATH) --lease-seconds 3600
python3 app.py --db /tmp/decision-test.sqlite3 report --id ID (--token=TOKEN | --token-file PATH) --status blocked --reason "承認が必要" --next-step "承認後に再試行"
python3 app.py --db /tmp/decision-test.sqlite3 retry --id ID --decided-at ANSWER_TIMESTAMP --reported-at REPORT_TIMESTAMP
python3 app.py --db /tmp/decision-test.sqlite3 close --id ID --decided-at ANSWER_TIMESTAMP --reported-at REPORT_TIMESTAMP
python3 app.py --db /tmp/decision-test.sqlite3 backup --output /tmp/decision-backup.sqlite3
python3 app.py --db /tmp/decision-test.sqlite3 run open --run-id RUN --role worker --pane P --socket SOCKET
python3 app.py --db /tmp/decision-test.sqlite3 run close --run-id RUN --role worker
python3 app.py --db /tmp/decision-test.sqlite3 run show (--run-id RUN | --pane P --socket SOCKET) [--all]
python3 app.py --db /tmp/decision-test.sqlite3 session open --run-id RUN --role frontdesk|worker|cue_worker --cli claude|codex --session-id UUID --pane P --socket SOCKET [--req REQ_ID]
python3 app.py --db /tmp/decision-test.sqlite3 session close (--session-id UUID --cli claude|codex | --pane P --socket SOCKET --run-id RUN [--role ROLE]) --reason normal|replaced|crashed|closed
python3 app.py --db /tmp/decision-test.sqlite3 session sample --id ID --observed-at ISO [--turn-count N] [--input-tokens N]
python3 app.py --db /tmp/decision-test.sqlite3 session list [--run-id RUN] [--cli claude|codex] [--open-only] [--since ISO] [--until ISO]
python3 app.py --db /tmp/decision-test.sqlite3 msg import|move --run-id RUN --msg NAME --state new|held|done [--wait decision=ID|pr=URL|msg=NAME] [--req KEY]
python3 app.py --db /tmp/decision-test.sqlite3 msg sync --run-id RUN --inbox PATH
python3 app.py --db /tmp/decision-test.sqlite3 msg list --run-id RUN [--state new|held|done]
python3 app.py --db /tmp/decision-test.sqlite3 task add --id ID --title 題名 --kind decision|monitor|research|implement --done-when 完了の条件 [--request R] [--pane P] [--run RUN] [--result R] [--remaining R] [--decision DECISION_ID]
python3 app.py --db /tmp/decision-test.sqlite3 task reconcile-lane --id ID --lane worker [--machine M] [--from LANE]   # reconciler だけが使う。lane と機械の欄を同時に書く。--from は今の lane が一致するときだけ書く
python3 app.py --db /tmp/decision-test.sqlite3 task update --id ID [--title T] [--kind K] [--done-when W] [--request R] [--pane P] [--run RUN] [--result R] [--remaining R] [--decision DECISION_ID]
python3 app.py --db /tmp/decision-test.sqlite3 task delete --id ID
python3 app.py --db /tmp/decision-test.sqlite3 task list [--lane L] [--json]
python3 app.py --db /tmp/decision-test.sqlite3 check tick --config /abs/path/to/checks.toml [--run-inbox PATH] [--run RUN_ID]
python3 app.py --db /tmp/decision-test.sqlite3 check list [--json]
python3 app.py --db /tmp/decision-test.sqlite3 check status [--limit N] [--json]
```

`task` は依頼の進み具合を管理する機能で、書き込みは CLI が行う。lane を書く経路は `reconcile-lane` だけで、`task move`、`add` の `--lane`、`add` / `update` の payload の `lane` と `machine` は Store が 400 (「lane は reconciler が書く。手で打っても次の reconcile が派生値に戻す」) で断る。判断への回答 (Web の `POST /api/decisions/{id}/answer` と TUI) は task を変えない。回答済みの task を todo へ戻すのは reconciler の仕事。`reconcile-lane` は lane と機械の欄 (`machine`、500 字まで、既定は空) を同時に書き、`--machine` を省くと空にする。remaining は変えない (窓口の文章だけ)。`--from LANE` を付けると今の lane が LANE のときだけ書き (1 文の UPDATE の WHERE に lane を足す)、違えば何も書かず JSON の error と exit 3 (競合) を返す。task が無いときは従来どおり exit 1 (404)、不正な LANE は exit 1 (400)。省くと lane を確かめず従来どおり書く。reconciler が古い snapshot で判定した lane を、その間に進んだ lane (窓口の done など) の上へ書かないための検査。tasks 表に `machine` 列が無い DB には、開いたときに列を足す (`PRAGMA user_version` は 2 のまま)。HTTP は一覧の `GET /api/tasks` (`list --json` と同じ形) だけを出し、Web の task 画面はこれを読み取り専用で表示する。lane は `todo` / `human_decision` / `worker` / `human_action` / `done`。`add` は `kind` (`decision` / `monitor` / `research` / `implement`、判断 / 監視 / 調査 / 実装) と `done_when` (完了の条件、1〜1000 字、空白だけは不可) が必須で、どちらかが無い・空・不正値なら 400 (CLI は JSON の error で exit 1)。`update` は指定した項目だけを検査し、`kind` が 4 値以外、`done_when` が空なら 400 で、指定しない項目は変えない。`kind` / `done_when` は再送判定 (下記) に入れない。tasks 表に両列が無い DB には、開いたときに `ALTER TABLE ... ADD COLUMN ... NOT NULL DEFAULT ''` で足し (`PRAGMA user_version` は 2 のまま)、旧 release に戻しても動く (旧 release の INSERT は列を指定せず、空になる)。列を足す前からある行 (legacy 行) は両方空のまま読め、`update` は `kind` を指定しなくても通る。登録時の検査 (ADR-321 Q19): `kind` が `implement` なら `done_when` に GitHub の PR か issue の URL (`https://github.com/<org>/<repo>/(pull|issues)/<番号>`) を含め、`decision` なら `decision_id` を必須にする。満たさなければ 400 (CLI は JSON の error で exit 1) で、task は増えない。検査は新しく作る行だけに行い、登録済みの id への再送は検査せず既存を返す。「PR 305」のように番号だけの書き方は断る。`monitor` / `research` は URL も decision_id も要らない。`update` は `kind` / `done_when` / `decision_id` のどれかを指定したときだけ、書き換え後の行で同じ検査をする (`--decision ''` で `decision` の task の関連付けを外すのも 400)。`result` などだけの更新と、`kind` が空の legacy 行の更新は検査しない。完了の条件の中身の検査はこれ以外にしない。`add` は常に todo で作り、同じ id で登録内容 (title / request / pane / run) が同じなら既存を返し、違えば 409。lane / machine / result / remaining / decision_id は reconcile-lane / update で進むため比べず、再送しても既存の値は変わらない。`--decision` は 32 桁 hex 以外を 400、存在しない判断を 404 にする。不正な `--lane` は `list` / `list --json` / `reconcile-lane` とも 400 (`add` / `move` の `--lane` は値によらず 400)。`update` は指定した項目だけを変え、`title` / `kind` / `done_when` 以外は空文字で消去する (`--decision` の空文字は関連付けの解除)。`list --json` は `{"lanes":[{"id","label","tasks":[...]}]}` を返し、各 task の `decision` は判断が無ければ null、あれば `{"title":"判断の題名","answered":false,"withdrawn":false}` (判断 ID が残っていて判断が見つからないときも null)。`--json` なしは lane ごとの文字の一覧を出す。task 系は DB を書き込み可で開くため、tasks 表が無い DB には表を足す (`PRAGMA user_version` は 2 のまま)。

reconciler が lane・pane・machine を同時に更新するときは、`--lane NEW --pane NEW --machine NEW --from OLD --from-pane OLD --from-machine OLD` を指定する。`--pane ''` で担当 pane を解除する。比較する 3 項目の省略は exit 1 (400) になり、`--from-pane ''` と `--from-machine ''` は現在値が空文字であることを要求する。依頼の所属や種類も照合するには `--from-run`、`--from-request`、`--from-kind` を一緒に指定する。3 項目とも省略でき、空文字を指定すると空文字を比較する (`--from-kind ''` は legacy 行に対応)。指定した比較条件はすべて満たす必要があり、1 項目でも違えば JSON の error と exit 3 を返す。task が無い場合は exit 1 (404)。比較と更新は SQLite の同じ書き込み transaction 内で行う。`--pane` を指定していた場合、比較に成功して更新値も現在と同じなら、行や `updated_at` に触れず現在の task を返す。

`--from-remaining`、`--from-decision-id`、`--from-updated-at` を一緒に指定すると、同じ transaction で照合時の残件・判断 ID・更新時刻も比較する。`--from-decision-id ''` は判断の紐付けが無い行に一致する。この 3 項目を指定し、移動先が `done` であれば、紐付いた判断が完了済みか取り下げ済みであることも要求する。条件に合わないと exit 3 を返す。

`run` は agent の実行と pane の対応を runs 表に記録する CLI 専用の機能で、HTTP API は無い。行は `{"id","run_id","role","pane","socket","opened_at","closed_at"}` で、開いた行は `closed_at` が null。`open` は 4 項目すべて必須 (1 から 200 文字) で、同じ `run_id` と `role` の開いた行があれば断る (閉じれば開き直せる)。`close` は `run_id` と `role` の開いた行に `closed_at` を入れ、無ければ断る。断ったときと、引数は解析できたが値が不正なとき (`show` で引く条件が無い、文字数超過など) は exit 1 で stderr に `{"error":"説明"}` を出す (HTTP の status は出ない)。必須のオプションが無い・知らないオプションがあるといった構文の誤りは、JSON の処理より前に argparse が exit 2 で stderr に usage を出す。`show` は `--run-id` か `--pane` と `--socket` の組のどちらか一方で引き、`{"runs":[...]}` を返す。既定は開いた行だけで、`--all` で閉じた行も出す。runs 表は tasks 表と同じく足すだけで既存の表を変えず、`PRAGMA user_version` は 2 のまま。

`session` は Claude・Codex の窓口 (`frontdesk`)・worker (`worker`)・Cue 専任 worker (`cue_worker`) の所属と累積計測を sessions 表に記録する CLI 専用の機能。行は `{"id","run_id","role","req_id","pane","socket","cli","native_session_id","opened_at","closed_at","close_reason","turn_count","input_tokens","elapsed_seconds"}` で、開いた行は `closed_at` が null、`close_reason` が空文字。`open` は `--run-id` / `--role` / `--cli` (`claude` / `codex`) / `--session-id` / `--pane` / `--socket` が必須、`--req` は任意 (既定は空文字)。`BEGIN IMMEDIATE` と部分一意索引により、開いた状態で `(cli, native_session_id)` と `(run_id, role, pane, socket)` が重複する `open` は 409 (exit 1、stderr に JSON の error) で断る (閉じれば開き直せる)。`close` は `--session-id` と `--cli` の組、または `--pane`・`--socket`・`--run-id` (任意で `--role`) の組のどちらか一方と `--reason` (`normal` / `replaced` / `crashed` / `closed`) を指定する。開いた行が無ければ 404 (既閉鎖・未登録)、複数一致すれば 409 で断り、閉じた行は変更しない。`list` は `{"sessions":[...]}` を id 順で返し、`--run-id` / `--cli` / `--open-only` / `--since` / `--until` (timezone 付き ISO 8601、区間 `[opened_at, closed_at]` との重なりで判定) で絞り込める。計測列は既存の sessions 表へ追加し、`PRAGMA user_version` は 2 のまま。

`sample` は `session list` が返す行の `id` と、計測に使った timezone 付き ISO 8601 の時刻を指定する。累積 turn 数と累積入力 token 数は少なくとも一方を非負の整数で指定する。経過時間は `opened_at` から観測時刻までの整数秒 (下限 0)。各値は従来値との最大値を同じ書き込みトランザクションで保持し、欠測の項目は従来値を保持する。未計測の列は `null`。閉じた行では `closed_at` までの観測を記録でき、所属や終了情報は保持される。成功時は更新後の行の JSON を返し、未知の行、現在時刻または終了時刻より後の観測は exit 1 と JSON のエラーで通知する。

`msg` は窓口が受けた msg の状態を messages 表に記録する CLI 専用の機能で、HTTP API は無い。msg の本文はファイルが運び役で、この表は窓口側がファイル名をキーに取り込む写し (本文は持たない)。行は `{"id","run_id","msg","tag","from_pane","req","state","wait","received_at","updated_at"}` で、`(run_id, msg)` が一意。`msg` は `<prio>-<micros>-<tag>-<from>-<rand>.msg` の形 (200 字以内) で、`tag` と `from_pane` は名前から入れる。`state` は `new` / `held` / `done`、`wait` は `held` のときだけ必須で、`held` 以外で渡すと断る (`decision=` / `pr=` / `msg=` で始まる。個別の `import` / `move` では 500 字以内)、`req` は任意 (200 字以内、request_key の文字種)。`import` は行が無いときだけ入れ (`{"imported":bool,"message":行}`)、`move` は無ければ入れ、あれば `state` / `wait` / `updated_at` を更新する (`held` から動かすと既存の `wait` は消え、`req` は渡したときだけ更新)。`list` は `{"messages":[...]}` を id 順に返す。値が不正なときは exit 1 で stderr に `{"error":"説明"}`。messages 表も足すだけで `PRAGMA user_version` は 2 のまま。

`msg sync` の `--inbox` には対象 run の inbox を指定する。必須の `new/` と `hold/` を走査し、`new/*.msg` を `new`、`hold/*.msg` を同名の `.wait` の待ち先付きで `held` として、同じ run の表に追加・更新する。待ち先は `decision=32 桁の小文字 hex`、`pr=GitHub の PR URL`、`msg=英数字と _ . : / - の組み合わせ` のいずれか。`hold` が残した 500 字を超える既存の待ち先も同期する (読み取り上限は OS の引数上限 `ARG_MAX`)。先頭 64 KiB のヘッダ内に形式に合う `req:` があれば記録する。どちらの置き場にもない既存の非 `done` 行を `done` にし、`wait` を消す。`cur/` は走査しないため、巡回の間に処理された msg は表に行が無ければ追加されない。行の内容に変化が無ければ時刻も変えない。`received_at` は初回登録時刻。成功時に `{"targets":N,"done":N}` (走査対象件数、この回に `done` にした件数) を返す。走査中に検出した置き場の変更、ディレクトリの欠損、不正な名前や待ち先、読み書きの失敗は exit 1 で stderr に JSON の error を出し、行の更新をまとめて取り消す。走査後の変更は次の同期で反映する。

`check` は定期監視スクリプトを実行し、発火イベントおよび連続故障をタスク化・`run-inbox` へ通知する CLI 専用の機能で、HTTP API は無い。テーブル (`checks` / `check_runs` / `check_run_events` / `check_ticks` / `check_tasks`) はすべて追加のみで既存の `tasks` 表は変更せず、`PRAGMA user_version` は 2 のまま。`check tick` は DB と同じディレクトリの固定 lockfile に `flock` を取り、取得できなければ WAL 切替・DDL を行わず `check_ticks` への `status=skipped` 1 行記録を best-effort で試みる (DB 未作成・`check_ticks` 表未作成・500 ms 以内に書き込めない競合時は stderr に警告を出すフォールバックを行い、いずれも exit 0 で終了する)。取得後は (1) WAL 切替・テーブル作成 (2) `check_ticks` に `running` で INSERT (3) 開始時 wake (回収 + `run-inbox` 投入) (4) `checks.toml` の checkout 検査 (main ブランチ・tracked clean・git 合計 5 秒上限) と TOML 全件検証 (5) `checks` 鏡同期 (6) due な check の実行 (累計上限 240 秒、JSONL 契約、20 件起票上限、`(check, kind, dedup_key)` 重複抑制、連続失敗の故障タスク化) (7) 終了時 wake (8) 30 日超過履歴のバッチ prune (9) `check_ticks` の最終 UPDATE を行う。`config_error` / `failed` 時は exit 1。`check list` / `check status` は読み取り専用で WAL 切替・DDL を行わず、鏡一覧や tick 履歴 (`running` 残留警告を含む)・未通知タスク数・24 時間遅延集計を出力する。状態遷移は `proofs/CheckRunner.lean` / `proofs/CheckRunner.als` で形式化済み。

`add --help` で必須・任意の項目と型の一覧・最小 JSON 例、`list --help` と `task list --help` で出力の形 (`list` は `{"decisions":[...],"demo":false}` の object、`task list --json` は `{"lanes":[...]}`) を確認できる。

`tui`、`watch`、`backup`、`task`、`hooks`、`run`、`session`、`msg`、`claims` と `claim --token-file` を除く CLI は対応する API と同じ JSON を stdout に返す (`claims` は HTTP に無く、`claim --token-file` は `token` の代わりに `token_file` を返す)。エラーは stderr と非ゼロ終了。既定 DB は `--db`、環境変数 `DECISION_INBOX_DB`、repo 外の user data directory の順で決定。`app.py` は executable で、PATH 上の symlink 経由でも同じ CLI を利用可能。`serve --demo` は別の一時 DB にデモだけを投入し、実 DB を使用しない。server は `127.0.0.1` のみ、`--port 0` は空きポートを選び起動 URL を出力。

エラー body は `{"error":"説明"}`。入力不正 `400`、未発見 `404`、競合 `409`、body 過大 `413`、JSON 以外 `415`、Host/origin 不正 `403`、永続化エラー `500`。削除・実データ初期化 API は提供しない。

入力不正のうち型・必須・未知の項目・許す値の問題は、項目名・期待する型 (または許す値)・受け取った型を全件 `; ` 区切りで 1 本の `error` に並べる。先頭は `JSON の項目を確認してください: ` で、項目名は `links[0].kind` のように添字・入れ子つき。例は `{"error":"JSON の項目を確認してください: links[0] は {url: string, label: string, kind: pr / issue / other} を指定してください (受け取った型: string)"}`。文字数・件数・URL の形・deadline・recommended の不正は従来どおり 1 件ずつ返す。`add` / `POST /api/decisions` / `update` のほか、claim / report などの入力も欠けた項目と未知の項目の名前を返す。

### pre-add と token ファイル (CLI のみ)

HTTP API は変わらない。

- **pre-add**: `add` は JSON を読んで検証した後・登録の前に pre-add を起動する。場所は `DECISION_INBOX_HOOKS_DIR`、無ければ `${XDG_CONFIG_HOME:-~/.config}/decision-inbox/hooks` の下の `pre-add`。stdin は検証済みの payload の JSON、env は呼び出し元を継承、hook の stdout は捨てる。exit 0 なら登録、非 0 なら登録せず exit 1 で stderr に `{"error":"pre-add が登録を断りました: <hook の stderr 先頭 500 字>"}`。起動に失敗したとき (interpreter が無い等) も登録せず同じ接頭辞で exit 1。10 秒で終わらないときは hook が起こした子を含めて止め、登録しない (hook が setsid などで別の process group に移した子は止められない)。hook は登録済みかを調べる前に走るので、登録済みの request_key で再送した add も hook が断れば exit 1 になる (API なら既存の判断が返る)。この拒否は前回の add が失敗した印にならない。ファイルが無い・実行可能でないなら何もしない。検証で弾かれた payload では hook を呼ばない。場所を呼び出し元の env で決めるので、付け忘れを防ぐ検査であって強制ではない (env の差し替えと HTTP で外せる)
- 対象外: `POST /api/decisions` は server の env が呼び出し元の pane を表さないので呼ばない。`update` は `request_key` を変えられない (変えると 400) ので、key で判定する hook は add の時点で済む。title や reason を見る hook は update の後には掛からない
- **run の接頭辞**: `add` は pre-add の後に、呼び出し元の pane (env `HERDR_PANE_ID` と `HERDR_SOCKET_PATH`、無ければ `~/.config/herdr/herdr.sock`) が runs 表の開いた行に当たるかを見る。当たるのに request_key が `frontdesk-<その run_id>/<1 字以上>` で始まらない (request_key 無しを含む) と、登録せず exit 1 で stderr に `{"error":"この pane は run ... に開いており、request_key は frontdesk-<run>/<名前> の形が要ります"}`。pane が複数の run を持つときはどれかの接頭辞なら通る。run_id が request_key の文字種 (英数字と `_ . : / -`) や 200 字の上限に収まらない run の pane は、どの request_key でも断られるので、run_id はこの範囲で付ける。行の無い pane、閉じた run、`HERDR_PANE_ID` 無し、HTTP の登録は対象外。pre-add が無い・別の場所にある場合も効く。runs 表は `--db` が指す DB のものを引くので、別の DB を指すと素通りする (登録先も同じ別 DB になる)。残る抜け道は env の差し替え、`--db` の指し替え、HTTP、SQLite 直接、この検査を持たない旧 release
- **hooks**: `{"pre_add":"<実行するパス>" | null}` を返す。DB を開かない。旧版はこのサブコマンドを知らず argparse の exit 2 になるので、呼び手は pre-add に対応した CLI かをこれで判定する
- **claims_dir**: `DECISION_INBOX_CLAIMS_DIR`、無ければ DB と同じディレクトリの `claims/`。`claim --token-file auto` が mode 0700 で作る。作るのは claims_dir だけで、親ディレクトリが無ければ claim せず exit 1 (中間のディレクトリは作らない)
- **claim --token-file auto|PATH**: token をファイルに保存し、stdout の JSON から `token` を外して `token_file` (絶対パス) を入れる。`auto` は claims_dir に `<decision id>-<16 桁 hex>.json`、PATH は指定したパスに作る。中身は `{"id","token","worker","expires_at"}`、mode 0600。claim ごとに別のファイル名にするのは、同じ worker 名で再 claim されたときに旧プロセスが新しい token を読んで照合を通らないようにするため
  - 順序は、一時ファイル (宛先と同じディレクトリ、O_EXCL) を開く → claim の transaction の中で token を書いて fsync → 宛先へ公開してディレクトリを fsync (`auto` は claims_dir の親も) → commit。一時ファイルを開けなければ claim せず exit 1。書き込みか公開に失敗したとき、書き込みの間に lease が切れたときは rollback して claim しない。commit の後に一時ファイルを消せなかったときは claim を成功のまま返し、stderr に `{"warning":"..."}` を 1 行出す (一時ファイルは token を含むので、表示されたパスのファイルを消す)。PATH が既にあれば上書きせず失敗する (exit 1、claim しない)。対象が無ければ一時ファイルを消して `{"decision":null}`
  - `auto` の claim のたびに、claims_dir の中で lease 期限から 24 時間過ぎたファイルを消す。期限はファイルの `expires_at` と、DB の同じ token の lease のうち遅いほう (`--token` や HTTP で renew した claim はファイルの `expires_at` が古いままのため)。消すのは `<decision id>-<16 桁 hex>.json` の名前で `{"id","token","worker","expires_at"}` を持ち、id がこの DB にあるファイルだけ
- **complete / report / renew --token-file PATH**: `--token` と排他でどちらか必須 (両方・どちらも無しは argparse の exit 2)。ファイルの `id` が `--id` と違えば 400 (exit 1)。complete / report は DB の確定後にファイルの削除を試みる。renew は DB の transaction の中でファイルの `expires_at` を書き換え (一時ファイルから置き換え、0600)、その後に commit する。ファイルが読めない、別の claim の token に置き換わっていた、または書き換えの間に lease が切れたら renew 自体を rollback する。token が無効なら従来どおり 409 (exit 1) でファイルは残る。通常ファイル以外と 64 KiB を超えるファイルは読まない (400)
- **claims [--id ID]**: `{"claims":[{"id","token_file","worker","expires_at","active"}]}`。active は DB の現在の token と一致し lease 内か (ファイルの expires_at ではなく DB で判定)。token の値は出さない。読めないファイルは飛ばす
- `--token-file` を付けない claim は従来どおり token を stdout に出す。complete / report / renew は token を `--token` で受け、結果に token は含めない
- **complete / report の確定後に token ファイルを消せなかったとき**: DB の状態は `completed` (complete) または `blocked` / `failed` (report) に確定したまま。CLI は stdout に通常の結果 JSON を返して exit 0 になる。stderr に書き込める場合は、ファイルのパスと削除エラーを含む `{"warning":"token ファイルを消せませんでした: ..."}` を stderr に出す
  - 回収を始める前に、対象 ID の `retry` と、残存ファイルと同じ明示 PATH を使う `claim` を、回収が終わるまで止める。先に `retry` が必要なら、古い PATH を回収までどの `claim` にも使わせず、次の `claim` には `--token-file auto` または未使用の別の PATH を使う。古い PATH がすでに再利用された可能性がある場合は、残存ファイルと確認できるまで削除を保留する
  - `list` で対象 ID の状態を確認する。`auto` の残存ファイルは `claims --id ID` で `active: false` の行からパスを確認できる。claims_dir の外に指定した PATH は `claims` に出ないので、警告のパスとファイル内の `id` を照合する。先の complete / report で残ったファイルと確認できたら、token を表示・共有せず、そのファイルだけを削除する
  - 残ったファイルを使って同じ token で complete / report を再実行すると、claim の競合 (409 に相当) として exit 1 になり、stderr に `{"error":"claim token が無効または期限切れです"}` を返す。DB の確定状態と残存ファイルはそのまま
- **renew の失敗からの戻し方**: renew が失敗したときは DB も rollback されるので、lease は延びていない。ただし置き換えの後 (ディレクトリの fsync、lease の再確認、commit) で失敗したときは、ファイルの `expires_at` が試みた新しい期限のまま残り、DB より先に進む。ファイルの `expires_at` を再試行の期限に使わない。例外は commit の後に lease 切れを見つけたときで、延長は保存したまま 409 (exit 1) で `延長は保存しましたが lease は既に切れています` を返す。どの場合も所有権が残っているかは `claims` の active で確かめる (`claims` は DB の lease の期限を返さない)。active が true なら元の lease はまだ有効なので renew をやり直す。false なら同じ token では lease を戻せないので、その token での作業を止め、通常の claim からやり直し、前の作業の副作用が適用済みでないかを確かめてから再開する

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
