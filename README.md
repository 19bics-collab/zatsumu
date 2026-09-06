# zatsumu 🪑

テレワーク勤怠・稼働可視化ツールの MVP（F-Chair+ 風）。

着席／退席の打刻、ランダム間隔のスクリーンショット送信、管理者向けの
リアルタイム稼働状況ダッシュボードを提供します。

## 構成

```
[client/agent.py]  --HTTP-->  [server (FastAPI)]  -->  [SQLite + 画像ファイル]
 打刻 / スクショ撮影・送信       認証 / 集計 / 配信            data/
                                     |
                              [/admin ダッシュボード]
```

| ディレクトリ | 役割 |
|---|---|
| `server/` | FastAPI サーバ（打刻 API・スクショ受信・管理画面・月次レポート・保存期間管理） |
| `client/` | 常駐エージェント（`agent.py` CLI 版 / `tray.py` トレイ常駐版 / `widget.py` 常時表示バー版） |
| `manage.py` | ユーザー作成・スクショ削除 CLI |
| `tests/` | API テスト |
| `deploy/` + `Dockerfile` 等 | 本番デプロイ用（**[DEPLOY.md](DEPLOY.md)** 参照） |

## 5分でデモサイトを立ち上げる

`ZATSUMU_DEMO=1` を付けて起動すると、固定トークンのデモユーザーと架空の
勤務データ・キャプチャが自動投入されます（DB が空のときのみ）。

| 役割 | 名前 | トークン |
|---|---|---|
| 管理者 | 管理者 | `demo-admin` |
| メンバー | 田中 / 鈴木 / 佐藤 | `demo-tanaka` / `demo-suzuki` / `demo-sato` |

**クラウドで（無料・PC へのインストール不要）** — [Render](https://render.com) の無料プランを使用:

1. GitHub アカウントで Render にサインアップ
2. New + → Web Service → このリポジトリを接続
3. Runtime: **Docker** / Instance Type: **Free** を選択
4. Environment Variables に `ZATSUMU_DEMO` = `1` を追加して Create Web Service
5. 発行された `https://xxx.onrender.com/admin` を開き、`demo-admin` でログイン

無料プランはアクセスが無いとスリープし、データは再起動のたびにリセット
されます（デモ用途には十分）。**固定トークンのため本番では絶対に
`ZATSUMU_DEMO=1` を使わないでください。**

**ローカルで（一発起動）** — Python 3.10+ が入っていれば1コマンド:

```bash
python demo.py     # Windows は demo.bat をダブルクリックでも可
```

ライブラリの自動インストール → デモデータ投入 → サーバ起動 → ブラウザで
管理画面を自動オープン、までやってくれます。終了は Ctrl+C。

## 本番公開

社内サーバやクラウドへのデプロイ手順は **[DEPLOY.md](DEPLOY.md)** にまとめてあります
（Docker / systemd / Windows / Render の各パターンと、HTTPS・DNS の設定方法）。

## ローカルでのセットアップ（開発・お試し用）

```bash
pip install -r requirements.txt

# ユーザー作成（表示されるトークンを控える）
python manage.py add-user 田中
python manage.py add-user 管理者 --admin

# サーバ起動
uvicorn server.app:app --host 0.0.0.0 --port 8000
```

## 使い方

**メンバー側 — Webで打刻・実績確認（インストール不要・スマホ対応）**:
`http://<server>:8000/me` を開き、自分のトークンでログイン。大きなボタンを
タップして着席/退席を切り替えます。**作業区分**（例：事務作業／現場）の
ボタンで、在席中に区分をワンタップで切り替えられます（現場に出た／事務に
戻った、を記録）。同じページで**自分の勤務実績**（月別の日別タイムライン、
区分ごとに色分け・合計時間）と**自分のキャプチャ**を確認できます
（キャプチャの閲覧は本人と管理者のみ、削除は管理者のみ）。
※ Web 打刻では PC 画面のキャプチャは記録されません（キャプチャが必要な場合は
下記の PC 用クライアントを使用）。

**メンバー側 — PC 用クライアント**（スクショ送信あり。Ctrl+C で退席）:

```bash
# CLI 版
python -m client.agent --server http://<server>:8000 --token <自分のトークン>

# システムトレイ常駐版（トレイアイコンから着席/退席をワンクリック）
python -m client.tray --server http://<server>:8000 --token <自分のトークン>

# 常時表示バー版（画面隅に小さなバー。着席中=青+経過時間 / 退席中=赤。クリックで切替）
python -m client.widget --server http://<server>:8000 --token <自分のトークン>
```

スクショは既定で 1 時間に約 6 回（5〜15 分のランダム間隔）撮影されます。
オプション: `--min-interval/--max-interval`（間隔・秒）、
`--blur N`（プライバシー配慮のぼかし）。

`--server`/`--token` は省略可能で、環境変数（`ZATSUMU_SERVER`/`ZATSUMU_TOKEN`）や
同じフォルダの `zatsumu_config.json` からも読み込みます。社員に配布する場合は、
**Python 不要の単体 `.exe`**（`build_client.ps1` でビルド、初回だけトークン入力）が
便利です。手順は **[DEPLOY.md](DEPLOY.md)** の「メンバー側の設定」を参照してください。

**管理者側**: ブラウザで `http://<server>:8000/admin` を開き、管理者トークンで
ログインします。

- **稼働状況**: 着席状況・本日の在席時間・本日のミニタイムライン・最新キャプチャを
  30 秒ごとに自動更新。退席し忘れたメンバーは**強制退席**ボタンで退席にできます。
  名前をクリックすると**個人ページ**（日別タイムライン）が開きます
- **個人ページ**: 青いバー（在席）をクリックすると**打刻の修正・削除**、
  「＋打刻を追加」で手動追加ができます（操作はすべて修正履歴に記録）。
  キャプチャの点はクリックで拡大表示、**← → キーまたはボタンで前後に移動**できます
- **メンバー管理**: 画面からメンバー追加（トークンは作成時に一度だけ表示）、
  トークン再発行、無効化/有効化、管理者権限の付与/解除
- **月次レポート**: 全員の勤務日数・合計時間の一覧と、
  月次集計 / **日別集計** / 在席データ / **修正履歴**の CSV ダウンロード
- **作業区分**: 「事務作業／現場」などの区分を設定でき、メンバーが在席中に
  切り替え可能。個人ページのタイムラインは区分ごとに色分けされ、区分別の
  合計時間も集計されます（在席データ CSV にも区分列が出力されます）
- **チーム（部署）**: メンバーをチームに分け、稼働状況をチームで絞り込み表示。
  メンバー管理タブでチームの作成・改名・削除と所属の割り当てができます
- **休暇・欠勤の申請／承認**: メンバーは `/me` から休暇（有給・半休・欠勤など）を
  申請でき、管理者は「申請」タブで承認/却下します（承認待ち件数をバッジ表示、
  通知が有効なら申請を Slack/メールへ周知）。承認状況は個人ページの該当日に表示
- **日報（業務報告）**: メンバーは `/me` で当日の業務内容を記録でき、管理者は
  「日報」タブで日付ごとに全員分を確認（未提出者も分かります）。個人ページの
  タイムラインには日報のある日に 📝 が付き、クリックで内容を表示できます
- **設定**: 会社名／サービス名（ヘッダー表示）・タイムゾーン・勤務時間帯の目安・
  作業区分・撮影間隔（頻度）・画質・ぼかし・全社撮影 ON/OFF・キャプチャ保存日数・
  連続在席アラート閾値を画面から変更。撮影設定はクライアントの次の撮影
  サイクルから自動反映されます
- **勤務時間帯の帯**: 設定した勤務時間帯（既定 9:00〜18:00）がタイムライン上に
  薄い帯で表示され、在席が時間内/時間外かひと目で分かります
- **予定勤務時間・残業/不足**: 1日の予定勤務時間（既定 8 時間）を設定でき、
  月次レポートに残業・不足を集計、個人ページの各日に過不足を表示します
- **通知（Slack / メール）**: 着席・退席や長時間在席アラートを Slack の Incoming
  Webhook や SMTP メールへ通知できます（設定画面で「テスト送信」可）
- **メール対応（受信箱の優先度分類・AI返信）**: 共有の受信箱（例: info@…）を IMAP で
  定期取得し、**専用画面 `/mail`** に**優先度順（高→中→低）**で表示します。
  Claude API キーを設定すると優先度判定と**返信文の下書き生成**を AI が行い
  （未設定でもキーワード分類＋定型文で動作）、内容を編集して画面から
  **そのまま返信を送信**できます（送信は SMTP 設定を使用、スレッドが繋がる
  ヘッダ付き）。優先度[高]の受信は Slack/メールに通知でき、
  送信操作は監査ログに記録されます。
  勤怠管理とは**別のUI**で、同じサーバ・同じDB・同じ管理者トークンのまま
  **サブドメインで出し分け**られます（`mail.example.com` → メール、
  `kintai.example.com` → 勤怠。[DEPLOY.md](DEPLOY.md) 参照）
- **連続在席アラート**: 閾値（既定 6 時間）を超えて着席し続けているメンバーに
  稼働状況で ⚠ を表示し、設定により Slack/メール通知も送ります
- メンバー管理から**個人ごとの撮影停止/再開**も可能（本家 F-Chair+ と同様、
  会社全体・ユーザー個人の両方で頻度変更・停止ができる設計）

日付・月の集計境界は管理画面の**タイムゾーン設定**（既定 `Asia/Tokyo`、環境変数
`ZATSUMU_TZ` で初期値を指定）で判定します。

**月次レポート / CSV 出力**:

```bash
# JSON
curl "http://<server>:8000/api/reports/monthly?month=2026-05" -H "Authorization: Bearer <管理者トークン>"
# CSV ダウンロード（Excel 対応の BOM 付き）
curl "http://<server>:8000/api/reports/monthly.csv?month=2026-05" -H "Authorization: Bearer <管理者トークン>" -o report.csv
```

`month` を省略すると当月を集計します。

**スクリーンショットの保存期間（自動削除）**:

- サーバ起動中、`ZATSUMU_RETENTION_DAYS`（既定 30 日）より古いスクショを
  `ZATSUMU_PURGE_INTERVAL_HOURS`（既定 6 時間）ごとに自動削除します（0 で無効化）。
- 手動実行: `python manage.py purge --days 30`、または `POST /api/admin/purge`（管理者）。

## API

| メソッド | パス | 認証 | 説明 |
|---|---|---|---|
| GET | `/api/me` | Bearer | 自分の現在状態（Web打刻ページ用） |
| GET | `/api/me/monthly` | Bearer | 自分の月次詳細（日別タイムライン用） |
| GET | `/api/me/settings` | Bearer | クライアント用の実効撮影設定 |
| GET/PUT | `/api/me/journal` | Bearer | 自分の日報の取得 / 保存 |
| GET | `/api/journals` | admin | 指定日の全メンバーの日報 |
| GET | `/api/users/{id}/journal` | admin | メンバーの日報を取得 |
| GET/PATCH | `/api/settings` | admin | 全社設定の取得 / 変更 |
| POST | `/api/settings/test-notify` | admin | 通知のテスト送信 |
| GET/POST | `/api/teams` | admin | チーム一覧 / 作成 |
| PATCH/DELETE | `/api/teams/{id}` | admin | チーム改名 / 削除 |
| GET/POST/DELETE | `/api/me/leave` | Bearer | 自分の休暇申請の一覧 / 申請 / 取消 |
| GET | `/api/leave` | admin | 休暇申請の一覧（status で絞り込み） |
| POST | `/api/leave/{id}/decision` | admin | 休暇申請の承認 / 却下 |
| GET | `/api/config` | なし | 表示用の公開設定(会社名・勤務時間帯) |
| POST | `/api/clock-in` | Bearer | 着席（任意で作業区分を指定。重複は 409） |
| POST | `/api/switch-category` | Bearer | 在席中に作業区分を切り替え |
| POST | `/api/clock-out` | Bearer | 退席 |
| POST | `/api/screenshots` | Bearer | スクショ送信（着席中のみ） |
| GET | `/api/status` | admin | 全員の着席状況・本日の勤務時間 |
| GET | `/api/screenshots` | admin | スクショ一覧 |
| GET | `/api/screenshots/{id}/image` | admin | 画像取得 |
| DELETE | `/api/screenshots/{id}` | admin | キャプチャ削除 |
| GET/POST | `/api/users` | admin | メンバー一覧 / 追加(トークン発行) |
| PATCH | `/api/users/{id}` | admin | 有効/無効・管理者権限の変更 |
| POST | `/api/users/{id}/token` | admin | トークン再発行 |
| POST | `/api/users/{id}/clock-out` | admin | 強制退席 |
| POST | `/api/users/{id}/sessions` | admin | 打刻の手動追加 |
| PATCH/DELETE | `/api/sessions/{id}` | admin | 打刻の修正 / 削除 |
| GET | `/api/users/{id}/monthly` | admin | 個人の月次詳細(日別タイムライン用) |
| GET | `/api/reports/monthly` | admin | 月次レポート(JSON) |
| GET | `/api/reports/monthly.csv` | admin | 月次レポート(CSV) |
| GET | `/api/reports/daily.csv` | admin | 日別集計(日付×メンバーの在席時間) |
| GET | `/api/reports/sessions.csv` | admin | 在席データ(全打刻のCSV) |
| GET | `/api/reports/audit.csv` | admin | 修正履歴(管理者操作の監査ログCSV) |
| POST | `/api/admin/purge` | admin | 古いスクショを即時削除 |
| GET | `/api/mail` | admin | 受信メール一覧（優先度順、status/priority で絞り込み） |
| GET | `/api/mail/{id}` | admin | メール詳細（本文・返信下書き） |
| POST | `/api/mail/fetch` | admin | 今すぐ IMAP から新着を取り込み |
| POST | `/api/mail/{id}/draft` | admin | 返信下書きの(再)生成（AI / 定型文） |
| PATCH | `/api/mail/{id}` | admin | 下書き保存・優先度/状態の変更 |
| POST | `/api/mail/{id}/send` | admin | 返信を送信（宛先は元メールの差出人） |
| POST | `/api/settings/test-imap` | admin | IMAP 設定の疎通確認 |
| POST | `/api/settings/test-email` | admin | 指定アドレスへメールのテスト送信 |
| GET | `/admin` | — | 勤怠管理の画面（データ取得は Bearer 認証の API 経由） |
| GET | `/me` | — | メンバー用の打刻ページ |
| GET | `/mail` | — | メール対応の専用画面（勤怠とは別UI） |
| GET | `/healthz` | なし | 死活監視用ヘルスチェック |

## テスト

```bash
python -m pytest tests/
```

## 運用上の注意（重要）

- **本人同意が必須**: 従業員の画面を撮影するツールです。導入には就業規則への
  明記と本人への事前説明・同意が必要です。
- **機密情報**: スクリーンショットには機密情報が写り込みます。本番運用では
  HTTPS 化・ストレージ暗号化・保存期間の設定を必ず行ってください。
- これは MVP です。本番化にはユーザー管理画面、撮影中のトレイ通知、
  自動起動、月次レポート／CSV 出力などの追加が想定されます。
