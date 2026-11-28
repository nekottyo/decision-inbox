# Changelog

## [v0.0.4](https://github.com/nekottyo-orgs/decision-inbox/compare/v0.0.3...v0.0.4) - 2026-11-28

- chore: Dependabot cooldown 7d と workflow lockfile への統一 by @nekottyo in https://github.com/nekottyo-orgs/decision-inbox/pull/63
- chore(deps): bump actions/create-github-app-token from 3.3.0 to 3.4.0 by @dependabot[bot] in https://github.com/nekottyo-orgs/decision-inbox/pull/66
- chore(deps): bump @stylexjs/stylex from 0.19.1 to 0.19.2 in /frontend by @dependabot[bot] in https://github.com/nekottyo-orgs/decision-inbox/pull/67
- check runner (tick) の実装 by @nekottyo in https://github.com/nekottyo-orgs/decision-inbox/pull/61

## [v0.0.3](https://github.com/nekottyo-orgs/decision-inbox/compare/v0.0.2...v0.0.3) - 2026-11-28

- feat: Board の依頼 thread に task の中身と msg の tag を出す by @nekottyo in https://github.com/nekottyo-orgs/decision-inbox/pull/56
- ci: PR 全件で動く集約 gate チェックを追加する by @nekottyo in https://github.com/nekottyo-orgs/decision-inbox/pull/59
- ci: main の push では path filter を外し verify を常時実行する by @nekottyo in https://github.com/nekottyo-orgs/decision-inbox/pull/60
- feat: セッションの累積計測を台帳に保存する by @nekottyo in https://github.com/nekottyo-orgs/decision-inbox/pull/62
- fix: task の完了を残件と判断の照合で保護する by @nekottyo in https://github.com/nekottyo-orgs/decision-inbox/pull/64

## [v0.0.2](https://github.com/nekottyo/decision-inbox/compare/v0.0.1...v0.0.2) - 2026-11-24

- feat: 公開リリースから Cue を切り替える by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/51
- feat: Cue Web に Board 画面を追加する (所属記録の観測と依頼 thread のチャット) by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/53
- fix: Board の投稿の宛先を担当 pane の worker に限る by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/55

## [v0.0.1](https://github.com/nekottyo/decision-inbox/commits/v0.0.1) - 2026-11-23

- feat: claim / watch を request_key 接頭辞で絞る by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/2
- feat: add task lanes to track request progress by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/3
- feat: web に task の lane 別一覧を足し、TUI から担当 pane へ移れるようにする by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/4
- feat: 未回答の判断の更新と取り下げを足す by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/5
- feat: 判断に付けた下書きファイルを Web と TUI で開けるようにする by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/6
- feat: 判断への回答で紐付く human_decision の task を todo へ移す by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/7
- feat: TUI の task 画面で完了の lane を既定で畳む by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/8
- feat: 停止した判断を完了扱いで閉じる close を足す by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/9
- feat: add のエラーに項目名を出し、add / list の --help に形を載せる by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/10
- chore: CodeRabbit が draft PR も自動 review するようにする by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/11
- feat: CLI の add に pre-add を挟み、hooks で対応版かを判定できるようにする by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/13
- feat: claim の token をファイルに保存し、run の開閉を runs 表に記録する by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/12
- feat: CLI の add で run の pane からの接頭辞無しの登録を断る by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/14
- fix: complete / report の token ファイル削除失敗と claim の一時ファイル残りを直す by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/15
- feat: list の answer に lease_until を出す by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/16
- feat: task reconcile-lane を足す (reconciler 専用の lane 書き込み) by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/17
- feat: move と add の --lane を断り、回答時の lane 更新を外し、機械の欄を足す by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/19
- test: CLI の list が claim 中の回答に status と lease_until を出すことを固定する by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/20
- feat: task add で種類と完了の条件を必須にする by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/21
- test: task add の入力の検査で kind と done_when を有効にする by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/22
- feat: task add で implement は PR の URL、decision は decision_id を必須にする by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/23
- feat: task reconcile-lane に --from を足し、書く前の lane が違えば exit 3 で断る by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/24
- feat: msg の受信状態を messages 表に記録する CLI を足す by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/25
- test: claim の所有権を Lean で検証する by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/26
- feat: tagpr で Cue の配布アーカイブをリリースする by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/28
- 読み取り時の状態を確かめて担当 pane と lane を更新する by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/30
- test: Cue の実機検証スキルを追加する by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/32
- fix: 確定待ちで失効した claim をエラーにする by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/38
- ci: PR の Python 回帰テストを 1 回にする by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/39
- docs: 確定後の token ファイル削除失敗時の扱いを記載する by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/40
- fix: 判断の本文と revision を同じ時点から返す by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/41
- perf: ready の取り下げ確認に部分索引を使う by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/42
- refactor: 停止した判断の再試行と終了処理を共通化する by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/45
- refactor: TUI の判断選択と初期化をまとめる by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/46
- feat: sessions 表と session open/close/list CLI を追加する by @nekottyo in https://github.com/nekottyo/decision-inbox/pull/47
