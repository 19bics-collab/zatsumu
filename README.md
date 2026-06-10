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
| `server/` | FastAPI サーバ（打刻 API・スクショ受信・管理画面） |
| `client/` | 常駐エージェント（mss でスクショ撮影、ランダム間隔で送信） |
| `manage.py` | ユーザー作成 CLI（トークン発行） |
| `tests/` | API テスト |

## セットアップ

```bash
pip install -r requirements.txt

# ユーザー作成（表示されるトークンを控える）
python manage.py add-user 田中
python manage.py add-user 管理者 --admin

# サーバ起動
uvicorn server.app:app --host 0.0.0.0 --port 8000
```

## 使い方

**メンバー側**（各自の PC で実行。Ctrl+C で退席）:

```bash
python -m client.agent --server http://<server>:8000 --token <自分のトークン>
```

オプション: `--min-interval/--max-interval`（スクショ間隔・秒）、
`--blur N`（プライバシー配慮のぼかし）。

**管理者側**: ブラウザで `http://<server>:8000/admin?token=<管理者トークン>` を開くと、
着席状況・本日の勤務時間・最新スクリーンショットが 30 秒ごとに自動更新されます。

## API

| メソッド | パス | 認証 | 説明 |
|---|---|---|---|
| POST | `/api/clock-in` | Bearer | 着席（重複は 409） |
| POST | `/api/clock-out` | Bearer | 退席 |
| POST | `/api/screenshots` | Bearer | スクショ送信（着席中のみ） |
| GET | `/api/status` | admin | 全員の着席状況・本日の勤務時間 |
| GET | `/api/screenshots` | admin | スクショ一覧 |
| GET | `/api/screenshots/{id}/image` | admin | 画像取得 |

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
