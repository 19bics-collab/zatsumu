# zatsumu デプロイ手順書（社内サーバでのインターネット公開）

社内の既存サーバで zatsumu を動かし、インターネット経由でアクセスできるように
する手順です。**サーバの種類が分からない場合は STEP 0 から順に**進めてください。

---

## 公開前チェックリスト（必読）

スクリーンショットには画面の機密情報が写ります。公開前に必ず確認してください。

- [ ] **HTTPS で公開する**（この手順書の構成なら自動で HTTPS になります）
- [ ] 従業員へ**事前説明と同意取得**を済ませ、就業規則に明記した
- [ ] トークンは本人にのみ伝え、チャットなどに平文で残さない
- [ ] スクショ保存日数（`ZATSUMU_RETENTION_DAYS`、既定30日）を社内規程に合わせた
- [ ] データのバックアップ先を決めた（「運用」の章参照）

---

## 必要なもの

| もの | 説明 |
|---|---|
| サーバ | 既存のもので可。メモリ512MB程度でも動く軽量アプリです |
| ドメイン名 | 例: `kintai.example.com`。会社のドメインのサブドメインでOK |
| DNS設定の権限 | ドメインのAレコードをサーバのIPに向ける作業 |
| ポート開放 | サーバの 80番・443番 をインターネットから到達可能に（社内ネットワーク管理者に依頼） |

---

## STEP 0: サーバ環境の確認

サーバにログインして以下を実行し、どのパターンに該当するか確認します。

```bash
uname -a            # "Linux ..." と出れば Linux
docker --version    # バージョンが出れば Docker あり → パターンA
python3 --version   # 3.10 以上なら パターンB も可
```

- コマンドが打てる黒い画面（ターミナル/SSH）に入れて Docker がある → **パターンA（推奨）**
- Linux だが Docker がない → Docker を入れて A、無理なら **パターンB**
- リモートデスクトップで入る Windows のサーバ → **パターンC**
- サーバに入れない・スペック不明・OSが古すぎる → **パターンD（クラウド）**

---

## パターンA: Linux + Docker（推奨）

### 1. Docker のインストール（まだ無い場合）

```bash
curl -fsSL https://get.docker.com | sudo sh
```

### 2. アプリの配置と設定

```bash
git clone https://github.com/19bics-collab/zatsumu.git
cd zatsumu
cp .env.example .env
nano .env    # ZATSUMU_DOMAIN を実際のドメイン名に書き換える
```

### 3. 起動

```bash
sudo docker compose up -d --build
```

DNS が正しく向いていれば、Caddy が Let's Encrypt の証明書を自動取得し、
`https://kintai.example.com/admin` で管理画面が開きます。

### 4. ユーザー作成

```bash
sudo docker compose exec app python manage.py add-user 管理者 --admin
sudo docker compose exec app python manage.py add-user 田中
```

表示されたトークンを各メンバーに渡します（管理者トークンは管理画面のログインに使用）。

### 5. 更新するとき

```bash
git pull && sudo docker compose up -d --build
```

---

## パターンB: Linux（Docker なし）

### 1. アプリの配置

```bash
sudo useradd --system --create-home zatsumu
sudo git clone https://github.com/19bics-collab/zatsumu.git /opt/zatsumu
cd /opt/zatsumu
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r server/requirements.txt
sudo mkdir -p /var/lib/zatsumu && sudo chown zatsumu:zatsumu /var/lib/zatsumu
sudo chown -R zatsumu:zatsumu /opt/zatsumu
```

### 2. サービス登録（自動起動）

```bash
sudo cp deploy/zatsumu.service /etc/systemd/system/
sudo systemctl enable --now zatsumu
curl http://localhost:8000/healthz   # {"status":"ok"} が出れば起動成功
```

### 3. HTTPS リバースプロキシ（Caddy）

```bash
# Caddy のインストール: https://caddyserver.com/docs/install の手順に従う
sudo cp deploy/Caddyfile.host /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile   # ドメイン名を書き換える
sudo systemctl reload caddy
```

### 4. ユーザー作成

```bash
sudo -u zatsumu ZATSUMU_DATA_DIR=/var/lib/zatsumu /opt/zatsumu/.venv/bin/python /opt/zatsumu/manage.py add-user 管理者 --admin
```

---

## パターンC: Windows Server

1. [python.org](https://www.python.org/downloads/) から Python 3.12 をインストール
   （「Add python.exe to PATH」にチェック）
2. PowerShell（管理者）で:

```powershell
git clone https://github.com/19bics-collab/zatsumu.git C:\zatsumu
cd C:\zatsumu
python -m venv .venv
.venv\Scripts\pip install -r server\requirements.txt
$env:ZATSUMU_DATA_DIR = "C:\zatsumu-data"
.venv\Scripts\python manage.py add-user 管理者 --admin
```

3. 常駐化: タスクスケジューラで「スタートアップ時」に以下を実行するタスクを登録
   （環境変数 `ZATSUMU_DATA_DIR=C:\zatsumu-data` をタスクに設定）:

```
C:\zatsumu\.venv\Scripts\uvicorn.exe server.app:app --host 127.0.0.1 --port 8000
```

4. HTTPS: [Caddy の Windows 版](https://caddyserver.com/download) をダウンロードし、
   `deploy/Caddyfile.host` の内容（ドメイン名を書き換え）で起動。こちらも
   タスクスケジューラで常駐化します。

---

## パターンD: 既存サーバが使えない場合（クラウド）

[Render](https://render.com) にアカウントを作り、この GitHub リポジトリを連携する
だけでデプロイできます（リポジトリ直下の `render.yaml` を自動で読み込みます）。

- 費用: Starter プラン + 1GB ディスクで月 $7〜8 程度
- ドメインは Render が `xxx.onrender.com` を無料で発行（独自ドメインも設定可）
- ユーザー作成は Render ダッシュボードの「Shell」タブから
  `python manage.py add-user 管理者 --admin`

---

## ドメインと DNS の設定

1. 会社のドメイン管理画面（お名前.com、ムームードメインなど）を開く
2. **A レコード**を追加: `kintai`（サブドメイン名）→ サーバのグローバルIP
3. サーバが社内にある場合は、ルーター/ファイアウォールで
   **80番・443番ポートをサーバに転送**する設定が必要（ネットワーク管理者に依頼）
4. 設定後、`https://kintai.example.com/healthz` で `{"status":"ok"}` が出れば完了

---

## メンバー側の設定（各自のPC）

### 方法A: `.exe` を配布する（社員配布におすすめ・Python不要）

社員のPCに Python を入れずに済む配布方法です。**管理者が一度だけ exe をビルド**し、
できた `zatsumu.exe` と `zatsumu_config.json` を各PCに配ります。

1. ビルド用の1台（Windows）で:

   ```powershell
   git clone https://github.com/19bics-collab/zatsumu.git
   cd zatsumu
   .\build_client.ps1      # dist\zatsumu.exe と dist\zatsumu_config.json ができる
   ```

2. `dist\zatsumu_config.json` の `server` を実際のドメインに変更:

   ```json
   { "server": "https://kintai.example.com" }
   ```

3. `dist\zatsumu.exe` と `zatsumu_config.json` を**セットで**各メンバーのPCに配布
   （任意のフォルダ、またはスタートアップに置く）。
4. メンバーは `zatsumu.exe` をダブルクリック → **初回だけ自分のトークンを入力**
   （以降は `%APPDATA%\zatsumu\config.json` に保存され、ダブルクリックだけで起動）。
   トレイアイコンから「着席する/退席する」を切り替えます。

> 自動起動したい場合は、`zatsumu.exe` のショートカットを
> `shell:startup`（スタートアップフォルダ）に置きます。

### 方法B: Python から直接動かす（開発・少人数向け）

```bash
git clone https://github.com/19bics-collab/zatsumu.git
cd zatsumu
pip install -r client/requirements.txt
python -m client.tray --server https://kintai.example.com --token <自分のトークン>
```

トレイのアイコンから「着席する/退席する」を切り替えます。
`--server` / `--token` を省略した場合は、環境変数（`ZATSUMU_SERVER` /
`ZATSUMU_TOKEN`）や同じフォルダの `zatsumu_config.json` から読み込みます。

---

## 運用

### バックアップ

保存が必要なのは DB とスクショの入ったデータディレクトリだけです。

```bash
# パターンA (Docker ボリューム)
sudo docker run --rm -v zatsumu_app-data:/data -v $(pwd):/backup alpine tar czf /backup/zatsumu-backup.tar.gz /data

# パターンB
sudo tar czf zatsumu-backup.tar.gz /var/lib/zatsumu
```

cron などで毎日実行し、別の場所に保管してください。

### トークンの再発行

現状は再発行コマンドが無いため、`add-user` で新しい名前のユーザーを作るか、
DB を直接更新します（必要なら再発行コマンドを追加実装できます）。

### うまくいかないとき

- `https://ドメイン/healthz` が開かない → DNS・ポート開放・サービス起動を順に確認
- 証明書エラー → DNS がサーバに向く前に起動した可能性。Caddy を再起動
- ログ確認: パターンAは `sudo docker compose logs -f`、Bは `journalctl -u zatsumu -f`
