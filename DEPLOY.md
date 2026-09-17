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
nano .env    # ZATSUMU_DOMAIN / ZATSUMU_MAIL_DOMAIN を実際のドメイン名に書き換える
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
2. PowerShell（管理者）で配置:

```powershell
git clone https://github.com/19bics-collab/zatsumu.git C:\zatsumu
cd C:\zatsumu
python -m venv .venv
.venv\Scripts\pip install -r server\requirements.txt
```

3. **先に `.bat` を2つ作ります。** タスクスケジューラーには環境変数を設定する
   項目が無いため、`.bat` の中で指定しないとデータの置き場所が渡らず、
   **空のデータベースを掴んで誰もログインできなくなります。**
   メモ帳で作り、文字コードは **ANSI（Shift_JIS）** で保存してください。

   `C:\zatsumu\start.bat`（サーバ本体）:

```
@echo off
cd /d C:\zatsumu
set ZATSUMU_DATA_DIR=C:\zatsumu-data
.venv\Scripts\uvicorn.exe server.app:app --host 127.0.0.1 --port 8000
```

   `C:\zatsumu\manage.bat`（ユーザー作成・トークン再発行に使う）:

```
@echo off
cd /d C:\zatsumu
set ZATSUMU_DATA_DIR=C:\zatsumu-data
.venv\Scripts\python manage.py %*
```

4. 管理者を作成（**必ず `manage.bat` 経由で**。直接 `python manage.py` を打つと
   別のデータベースを作ってしまいます）:

```powershell
C:\zatsumu\manage.bat add-user 管理者 --admin
```

5. 常駐化: タスクスケジューラーで新しいタスクを作り、次のとおり設定します。

   - **[全般]**「ユーザーがログオンしているかどうかにかかわらず実行する」を選ぶ
     （これを選ばないと、誰かがリモートデスクトップでログオンするまで起動しません＝
     再起動のたびに落ちたままになります。黒い窓も出なくなります）
   - **[全般]**「最上位の特権で実行する」にチェック
   - **[トリガー]**「スタートアップ時」
   - **[操作]** プログラム/スクリプト に `C:\zatsumu\start.bat`
     （引数・開始場所は空でOK。`cd` は `.bat` の中でやっています）
   - 保存時にパスワードを聞かれるので、**パスワード無期限のアカウント**を使ってください

6. **確認**（ここを飛ばすと、公開してから気づくことになります）:

```powershell
# タスクを右クリック →「実行」したあとで
curl.exe http://localhost:8000/healthz      # {"status":"ok"} が返ること
dir C:\zatsumu-data\zatsumu.db              # 存在すること
dir C:\zatsumu\data                         # 「見つかりません」になること（作られていたら環境変数が効いていない）
```

   さらに **Windows を一度再起動**し、ログオンしない状態でも
   `curl.exe http://localhost:8000/healthz` が返ることを確認してください。

7. HTTPS: [Caddy の Windows 版](https://caddyserver.com/download) をダウンロードし、
   `deploy/Caddyfile.host` の内容（ドメイン名を書き換え）を
   `C:\zatsumu\caddy\Caddyfile` として保存。`C:\zatsumu\caddy\start-caddy.bat` を作り、

```
@echo off
cd /d C:\zatsumu\caddy
caddy.exe run --config C:\zatsumu\caddy\Caddyfile
```

   これを手順5と同じ条件でタスク登録します。

---

## パターンD: 既存サーバが使えない場合（クラウド）

[Render](https://render.com) にアカウントを作り、この GitHub リポジトリを連携する
だけでデプロイできます（リポジトリ直下の `render.yaml` を自動で読み込みます）。

- 費用: Starter プラン + 1GB ディスクで月 $7～8 程度
- ドメインは Render が `xxx.onrender.com` を無料で発行（独自ドメインも設定可）
- ユーザー作成は Render ダッシュボードの「Shell」タブから
  `python manage.py add-user 管理者 --admin`

> ⚠️ **パターンDだけは、下の「ドメインと DNS の設定」が当てはまりません。**
> Render 構成には振り分け役（Caddy）が入っていないため、**サブドメインでの
> 出し分けはできません。** A レコードを2本引く必要もありません
> （Render は固定IPを出さないため、そもそも A レコードでは向けられません）。
>
> 画面は**1つのドメインのパス違い**で開きます。
>
> | URL | 画面 |
> |---|---|
> | `https://xxx.onrender.com/admin` | 勤怠管理 |
> | `https://xxx.onrender.com/me` | 打刻 |
> | `https://xxx.onrender.com/mail` | メール対応 |
>
> どちらの画面も同じドメインで開けてしまうので、URLを知っている管理者以外に
> 教えない運用にしてください。

---

## ドメインと DNS の設定

勤怠管理とメール対応は **同じサーバ・同じDB** で動きますが、画面を分けるため
**サブドメインを2つ**使います。

1. 会社のドメイン管理画面（お名前.com、ムームードメインなど）を開く
2. **A レコード**を2本追加。どちらも同じサーバのグローバルIPに向ける
   - `kintai` → 勤怠管理（`.env` の `ZATSUMU_DOMAIN`）
   - `mail` → メール対応（`.env` の `ZATSUMU_MAIL_DOMAIN`）
3. サーバが社内にある場合は、ルーター/ファイアウォールで
   **80番・443番ポートをサーバに転送**する設定が必要（ネットワーク管理者に依頼）
4. 設定後、`https://kintai.example.com/healthz` で `{"status":"ok"}` が出れば完了

### 2つの画面

| URL | 画面 | 用途 |
|---|---|---|
| `https://kintai.example.com/admin` | 勤怠管理 | 稼働状況・日報・申請・メンバー管理・レポート |
| `https://kintai.example.com/me` | 打刻 | メンバーが自分で打刻・実績確認 |
| `https://mail.example.com/` | メール対応 | 受信箱（優先度順）・返信文の生成/編集・送信 |

- ログインは**どちらも同じ管理者トークン**です（メール画面は管理者のみ）。
  ただしブラウザの保存先はドメインごとに分かれるため、**各サブドメインで1回ずつログイン**します。
- 互いの画面はドメインをまたいで開けません（Caddy が 404 を返します）。
- SMTP（送信）の設定は両画面で共通です。IMAP（受信）とAI・署名の設定はメール画面側にあります。

メール対応を使わない場合は、`ZATSUMU_MAIL_DOMAIN` の A レコードを作らなければ
そのサブドメインは公開されません（勤怠側の動作には影響しません）。

---

## メンバー側の設定（各自のPC）

### 方法A: フォルダ版アプリを配布する（社員配布におすすめ・Python不要）

社員のPCに Python を入れずに済む配布方法です。**管理者が一度だけビルド**し、
できた `勤怠管理.zip` を各メンバーに配ります。

> ⚠️ **接続先の既定値に注意**
> `client/config.py` の `DEFAULT_SERVER` に既定の接続先が埋め込まれています。
> **これと違うドメインで運用する場合は、手順2の `zatsumu_config.json` を必ず同梱してください。**
> 同梱を忘れると、配った全員のアプリが既定の接続先に打刻とスクリーンショットを
> 送ろうとし、自社の管理画面には誰も表示されません（全員が退席のままに見えます）。

1. ビルド用の1台（Windows）で:

   ```powershell
   git clone https://github.com/19bics-collab/zatsumu.git
   cd zatsumu
   .\build_client.ps1
   ```

   できるもの: `dist\勤怠管理\`（フォルダ版。中に `勤怠管理.exe`）と、
   それを固めた `dist\勤怠管理.zip`。
   **`zatsumu.exe` や `dist\zatsumu_config.json` は作られません。**

2. **ZIP を作り直す前に**、接続先を書いたファイルをフォルダの中に入れます。
   `dist\勤怠管理\zatsumu_config.json` を新規作成（`勤怠管理.exe` と同じ階層）:

   ```json
   { "server": "https://kintai.example.com" }
   ```

   入れたら ZIP を作り直します:

   ```powershell
   Compress-Archive -Path "dist\勤怠管理" -DestinationPath "dist\勤怠管理.zip" -Force
   ```

   > ビルド直後の ZIP にはこのファイルが入っていません。**必ず作り直してください。**
   > 先に配ってしまうと意味がありません。

3. `dist\勤怠管理.zip` を各メンバーに配ります
   （下の「打刻画面から各自にダウンロードしてもらう」も使えます）。
4. メンバーは ZIP を右クリック →「すべて展開」→ 中の **`勤怠管理.exe`** を実行
   → **初回だけ自分のトークンを入力**（以降は `%APPDATA%\zatsumu\config.json` に
   保存され、ダブルクリックだけで起動）。
   トレイアイコンから「着席する/退席する」を切り替えます。

> **exe だけ取り出すと動きません。** フォルダごと置いて使ってください。
> 自動起動したい場合は、`勤怠管理.exe` のショートカットを
> `shell:startup`（スタートアップフォルダ）に置きます。

#### 打刻画面の「PCアプリをダウンロード」ボタンを有効にする

打刻画面（`/me`）には「🖥 PCアプリをダウンロード（ZIP）」ボタンがありますが、
**サーバに ZIP を置くまでは、押すと 404 になります。** 置き場所はデータ
ディレクトリの `downloads/` です。

まず `dist\勤怠管理.zip` をサーバへ転送し（WinSCP や `scp`）、パターン別に配置します。

```bash
# パターンA (Docker) — 実体はボリュームの中。docker cp は途中のフォルダを作らないので先に mkdir
cd ~/zatsumu
sudo docker compose exec app mkdir -p /data/downloads
sudo docker compose cp ~/勤怠管理.zip app:/data/downloads/勤怠管理.zip

# パターンB (Docker なし)
sudo mkdir -p /var/lib/zatsumu/downloads
sudo cp ~/勤怠管理.zip /var/lib/zatsumu/downloads/
sudo chown -R zatsumu:zatsumu /var/lib/zatsumu/downloads
```

- パターンC（Windows）: `C:\zatsumu-data\downloads\勤怠管理.zip` に置く（フォルダが無ければ作る）
- パターンD（Render）: Shell タブからファイルを上げる手段が無いため、
  社内ファイルサーバ等の URL から取得する
  （`mkdir -p /data/downloads && curl -L -o /data/downloads/勤怠管理.zip <URL>`）。
  難しければこのボタンは使わず、手順3の手渡し配布にしてください

確認: `curl -I https://kintai.example.com/download/client` が **200** を返せば成功
（404 なら未配置）。パターンAはボリュームに入るので、
`docker compose up -d --build` で作り直しても消えません。

**このボタンを使わない運用**（ZIP を手渡しで配る）なら、置かなくて構いません。
その場合ボタンは常に 404 のままです。

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

> ⚠️ **バックアップの取り扱いに注意**
> このバックアップの中のデータベースには、次のものが**そのまま読める形で**入ります。
>
> - メール受信のパスワード（`imap_pass`）と送信のパスワード（`smtp_pass`）
> - Claude API キー（`anthropic_api_key`）
> - 取り込んだ**お客様からのメール本文**と、作成した返信文
> - 全メンバーのログイン用トークン
>
> 置き場所は社内の限られた人しか触れない場所にし、外部のクラウドに上げる場合は
> 暗号化してください。退職者のPCや共有フォルダに残さないよう注意してください。

### 保存期間（自動削除）

古いデータは自動で消えます。**勤怠とメールで設定が別々**です。

| 対象 | 設定 | 既定 | 変え方 |
|---|---|---|---|
| 打刻・スクリーンショット | `ZATSUMU_RETENTION_DAYS` | 30日 | `.env` で指定して再起動 |
| 受信したメール・返信文 | `mail_retention_days` | **180日** | メール画面の「設定」から変更（`0` で自動削除なし） |

> ⚠️ `ZATSUMU_RETENTION_DAYS` を短くしても、**メールの本文は消えません。**
> お客様のメールを長く残したくない場合は、メール画面の設定で別途短くしてください。

### トークンの再発行・管理者の復旧

- 通常は管理画面の「メンバー管理 → トークン再発行」で再発行できます。
- **管理者トークンを紛失し、他に管理者がいない場合**は、サーバ上で CLI を使います
  （パターンAは `sudo docker compose exec app`、Bは venv の python を前に付けて実行）:

  ```bash
  python manage.py list-users              # ユーザー名を確認
  python manage.py reset-token 管理者       # トークンを再発行して表示
  python manage.py make-admin 田中          # 既存ユーザーを管理者に昇格
  ```

### うまくいかないとき

- `https://ドメイン/healthz` が開かない → DNS・ポート開放・サービス起動を順に確認
- 証明書エラー → DNS がサーバに向く前に起動した可能性。Caddy を再起動
- ログ確認: パターンAは `sudo docker compose logs -f`、Bは `journalctl -u zatsumu -f`
