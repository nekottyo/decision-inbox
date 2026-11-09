# 接続仕様

Python 3 の標準ライブラリのみを使用。すべての API は JSON、状態変更は `Content-Type: application/json`。ブラウザーからの状態変更は同一 origin のみ許可し、CLI 等の Origin なしの呼び出しは loopback Host 検査後に許可。CORS は提供しない。

## 依頼の登録

`POST /api/decisions` → `201` と依頼全体。`id` と `created_at` は server が付与。以下が最小の投入例。

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
  "image": null
}
```

必須は `title/source/reason/options/recommended/recommendation`。`priority` の既定は `normal`、`background/urgency_reason` は空文字、`links` は空配列、`deadline/image` は null。候補は 2〜8 件で重複しない ID。priority は `urgent/normal/later`。deadline は timezone 付き ISO 8601。リンクは http(s) URL、kind は `pr/issue/other`、label 必須。image は `{"url":"https://example.com/diagram.png","alt":"図の説明"}` または許可済み `/assets/` 内の静的画像参照。任意 HTML は受け付けない。body 上限は 128 KiB。title/source は 200 文字、reason/recommendation は 4000 文字、background は 12000 文字、urgency_reason は 2000 文字、memo は 2000 文字、リンクは 20 件まで。未知の項目・重複 JSON キーは拒否。

## 一覧・詳細・判断

- `GET /api/decisions` → `{"decisions":[依頼全体...],"demo":false}`
- `GET /api/decisions/{id}` → 依頼全体
- `POST /api/decisions/{id}/answer`、body `{"choice":"minimal","memo":"任意のメモ"}` → 保存後の依頼全体

依頼全体は登録内容に `id/created_at/answer` を加えたもの。未判断の `answer` は null。判断済みは `{"choice":"minimal","memo":"...","decided_at":"UTC ISO 8601","status":"pending"}`。status は `pending/processing/completed` (UI は処理待ち / 対応中 / 完了)。二重判断は `409`、入力は上書きしない。

## 回答済みの処理

- `POST /api/claim`、body `{"worker":"front-desk","lease_seconds":300}` → `{"decision":依頼全体,"token":"秘密の claim token","worker":"front-desk","expires_at":"UTC ISO 8601"}`。対象がなければ `{"decision":null}`
- `POST /api/decisions/{id}/complete`、body `{"token":"claim 時の token"}` → 完了後の依頼全体

lease_seconds は省略時 300、1〜3600 秒。front-desk は 3600 秒を指定。lease 更新は提供しない。回答日時順 (同時刻は ID 順) の FIFO。期限切れは再 claim 可能。有効かつ一致する token のみ complete 可能、それ以外は `409`。consumer は decision ID で冪等に処理し、外部副作用前に適用済みか確認。失敗時は complete せず再配送を待つ。claim token は通常の一覧・詳細には含めない。

## CLI

```sh
python3 app.py --db /tmp/decision-test.sqlite3 serve --port 4317
python3 app.py --db /tmp/decision-test.sqlite3 add --file request.json
python3 app.py --db /tmp/decision-test.sqlite3 list
python3 app.py --db /tmp/decision-test.sqlite3 claim --worker front-desk --lease-seconds 300
python3 app.py --db /tmp/decision-test.sqlite3 complete --id ID --token TOKEN
```

CLI は対応する API と同じ JSON を stdout に返す。エラーは stderr と非ゼロ終了。既定 DB は `--db`、環境変数 `DECISION_INBOX_DB`、repo 外の user data directory の順で決定。`app.py` は executable で、PATH 上の symlink 経由でも同じ CLI を利用可能。`serve --demo` は別の一時 DB にデモだけを投入し、実 DB を使用しない。server は `127.0.0.1` のみ、`--port 0` は空きポートを選び起動 URL を出力。

エラー body は `{"error":"説明"}`。入力不正 `400`、未発見 `404`、競合 `409`、body 過大 `413`、JSON 以外 `415`、Host/origin 不正 `403`、永続化エラー `500`。削除・実データ初期化 API は提供しない。
