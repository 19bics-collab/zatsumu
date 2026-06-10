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

**ローカルで**:

```bash
pip install -r server/requirements.txt
ZATSUMU_DEMO=1 uvicorn server.app:app --port 8000
# → http://localhost:8000/admin を開いて demo-admin でログイン
```

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

**メンバー側 — Webで打刻（インストール不要・スマホ対応）**:
`http://<server>:8000/me` を開き、自分のトークンでログイン。大きなボタンを
タップして着席/退席を切り替えます。
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

**管理者側**: ブラウザで `http://<server>:8000/admin` を開き、管理者トークンで
ログインすると、着席状況・本日の在席時間・最新キャプチャが 30 秒ごとに自動更新
されます。メンバーの行をクリックすると**個人ページ**（日別タイムライン上に在席
時間とキャプチャを表示、月送り、CSV ダウンロード）が開きます。キャプチャは
クリックで拡大表示でき、削除は管理者のみ可能です。

日付・月の集計境界は `ZATSUMU_TZ`（既定 `Asia/Tokyo`）で判定します。

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
| POST | `/api/clock-in` | Bearer | 着席（重複は 409） |
| POST | `/api/clock-out` | Bearer | 退席 |
| POST | `/api/screenshots` | Bearer | スクショ送信（着席中のみ） |
| GET | `/api/status` | admin | 全員の着席状況・本日の勤務時間 |
| GET | `/api/screenshots` | admin | スクショ一覧 |
| GET | `/api/screenshots/{id}/image` | admin | 画像取得 |
| DELETE | `/api/screenshots/{id}` | admin | キャプチャ削除 |
| GET | `/api/users/{id}/monthly` | admin | 個人の月次詳細(日別タイムライン用) |
| GET | `/api/reports/monthly` | admin | 月次レポート(JSON) |
| GET | `/api/reports/monthly.csv` | admin | 月次レポート(CSV) |
| GET | `/api/reports/sessions.csv` | admin | 在席データ(全打刻のCSV) |
| POST | `/api/admin/purge` | admin | 古いスクショを即時削除 |
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
