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
sudo docker compose up -d --force-recreate caddy
```

2行目は **Caddy（入口の門番。HTTPS と通してよい道を決める）の作り直し**です。
`Caddyfile` / `Caddyfile.mail`（門番の設定ファイル）は1行目では読み直されないため、
更新で通してよい道が増えても、作り直すまでは古い設定のまま 404（見つからない）を返します。
（`caddy reload` や設定の再読み込みでは足りません。`git pull` でファイルが別物に置き換わり、
動いている Caddy は古いファイルを見続けるためです。作り直しても証明書は消えません）

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

> パターンDでは A レコードを引く必要はありません（Render が発行するドメインを
> そのまま使うか、Render 側で独自ドメインを設定します）。画面の開き方は
> 下の「ドメインと DNS の設定」と同じで、**1つのドメインのパス違い**です。
>
> | URL | 画面 |
> |---|---|
> | `https://xxx.onrender.com/admin` | 勤怠管理 |
> | `https://xxx.onrender.com/me` | 打刻 |
> | `https://xxx.onrender.com/mail` | メール対応 |
>
> ただし下の「メール画面を別のドメインにしたい場合」は、振り分け役（Caddy）が
> 入っていないため Render では行えません。

---

## ドメインと DNS の設定

勤怠管理もメール対応も **同じサーバ・同じDB・同じドメイン**で動きます。
用意するドメインは **1つだけ**です。

1. 会社のドメイン管理画面（お名前.com、ムームードメインなど）を開く
2. **A レコード**を1本追加し、このサーバのグローバルIPに向ける
   （例: `kintai` → `.env` の `ZATSUMU_DOMAIN`）
3. サーバが社内にある場合は、ルーター/ファイアウォールで
   **80番・443番ポートをサーバに転送**する設定が必要（ネットワーク管理者に依頼）
4. 設定後、`https://kintai.example.com/healthz` で `{"status":"ok"}` が出れば完了

### 3つの画面

| URL | 画面 | 用途 |
|---|---|---|
| `https://kintai.example.com/admin` | 勤怠管理 | 稼働状況・日報・申請・メンバー管理・レポート |
| `https://kintai.example.com/me` | 打刻 | メンバーが自分で打刻・実績確認 |
| `https://kintai.example.com/mail` | メール対応 | 受信箱（優先度順）・返信文の生成/編集・送信 |

- ログインは**3画面とも同じ管理者トークン**です（`/admin` と `/mail` は管理者のみ）。
  同じドメインなので、**一度ログインすれば入り直す必要はありません**。
- `/mail` は管理者トークンが無いと中身を出しませんが、管理画面と同じ扱いです。
  メンバーには `/me` だけを案内してください。
- SMTP（送信）の設定は勤怠の通知とメール返信で共通です。
  IMAP（受信）とAI・署名の設定はメール画面側にあります。

### メール画面を別のドメインにしたい場合

メール対応だけ `mail.example.com` のような別ドメインで開きたい場合は、
A レコードをもう1本追加したうえで、Caddy の設定を2サイトに分けます。
`deploy/Caddyfile.host`（Docker なし）の例:

```
# 勤怠管理（メール画面はこちらには出さない）
kintai.example.com {
	handle /mail {
		respond "メール対応はメール用のドメインで開いてください" 404
	}
	handle {
		reverse_proxy 127.0.0.1:8000
	}
}

# メール対応
mail.example.com {
	redir / /mail
	handle /admin { respond "勤怠管理は勤怠用のドメインで開いてください" 404 }
	handle /me    { respond "勤怠管理は勤怠用のドメインで開いてください" 404 }
	handle /download/* { respond 404 }
	handle {
		reverse_proxy 127.0.0.1:8000
	}
}
```

Docker 構成（`Caddyfile`）なら転送先を `app:8000` にし、ドメイン名を
`{$ZATSUMU_DOMAIN}` / `{$ZATSUMU_MAIL_DOMAIN}` と書いて `docker-compose.yml` の
`caddy` に両方の環境変数を渡してください。

この構成にすると次の2点が変わります。

- ドメインごとにブラウザの保存先が分かれるため、**各ドメインで1回ずつログイン**が必要
- 管理画面の設定にある「メール対応画面」へのリンクは 404 になります

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

  トークンを作り直すと、そのユーザーが確認済みの端末（下の「新しい端末の確認」）も
  すべて取り消されます（もれた合言葉で確認した端末を残さないため）。

### 新しい端末からのログインに、メールの確認コードを必須にする（任意）

管理者トークン（合言葉）だけでは、もし合言葉がもれたときに誰でも管理画面・メール画面に
入れてしまいます。この機能を有効にすると、**初めて使うパソコンやスマホ（新しい端末）から
管理者がログインしたときに、決めたメールアドレスへ 6 桁の確認コードを送り、
それを入力しないと使えない**ようになります（いわゆる2段階認証）。
一度確認した端末は、**最後に使ってから 90 日**は確認コードなしで使えます。

- 対象は**管理者だけ**です。メンバーの打刻（PC アプリ・`/me`）は今までどおりです。
  ※ 管理者のトークンで PC アプリを動かしている場合は、有効にすると打刻できなくなります。
  管理者の打刻には、別にメンバー用のユーザーを作ってください。
- 設定はサーバの `.env`（設定ファイル）だけで行います。画面からは変えられません
  （画面を乗っ取られても機能を切られないようにするため）。

**有効にする手順**

1. メールを送れるようにしておく（メール画面の「設定 → 返信の送信（SMTP）」を保存し、
   「テスト送信」で届くことを確認）。確認コードはこの送信設定で送られます。
2. **Caddy（入口の門番）を作り直して、確認用の道が通るか確かめる**（パターンA）。
   更新（`git pull`）のあと Caddy を作り直していないと、確認用の道（`/api/device`）が
   404（見つからない）のままになり、有効にした途端に**誰もログインできなくなります**:

   ```bash
   sudo docker compose up -d --force-recreate caddy
   curl -s -o /dev/null -w '%{http_code}\n' -X POST https://desk.example.com/api/device/start
   ```

   2行目の `desk.example.com` は自分のメール画面のドメインに置き換えます。
   **`401` と出れば準備できています**（合言葉なしで呼んだので断られた＝道は通っている）。
   `404` と出たら Caddy がまだ古い設定なので、ここで止めて 1行目をやり直してください。
3. サーバの `.env` に、確認コードの送り先を1行足す:

   ```
   ZATSUMU_LOGIN_VERIFY_EMAIL=you@example.com
   ```

4. 反映する: `sudo docker compose up -d`（パターンA）。
   Docker を使わない場合は、サービスの環境変数に同じ値を設定して再起動します。
5. 画面を開き直すと「新しい端末の確認」画面になり、`yo***@example.com に確認コードを
   送りました` と出ます。届いたメールの 6 桁を入れて「確認」を押します。
   （「サーバの設定（Caddy）が古い可能性があります」と出たら、2 をやり直してください）

- コードの有効時間は **10 分**、間違えてよいのは **5 回**まで。送信は **1 分に 1 回・
  1 時間に 5 回**までです（届かないときは少し待って「再送」）。
- 確認メールには、日時・アクセス元の IP アドレス（インターネット上の住所）・ブラウザの
  種類が書かれています。**心当たりが無いメールが届いたら、コードを誰にも教えず、
  すぐに上の `reset-token` で合言葉を作り直してください。**
- 無効に戻すには、`.env` のその行を消して（または空にして）もう一度 `up -d` します。

**確認済みの端末の一覧・取り消し**

- 画面: 管理画面・メール画面の「設定」にある「確認済みの端末」で「取り消し」を押す
- サーバ上: `python manage.py devices`（一覧）、`python manage.py device-revoke 3`
  （3 番を取り消し）、`python manage.py device-revoke all`（全部取り消し）

**メールが届かなくてログインできないとき（復旧）**

サーバに入れる人が、次のコマンドで端末用の合言葉（端末トークン）を発行します:

```bash
sudo docker compose exec app python manage.py issue-device 管理者
```

`#device=...` という長い文字列が表示されるので、ブラウザでメール画面のアドレスの
後ろに付けて開きます（例: `https://desk.example.com/mail#device=...`）。
管理者トークンの入力を求められたら、いつもどおり入力します。
これでその端末は確認済みになります（`#` から後ろはアドレス欄から自動で消えます）。
表示された文字列は合言葉と同じく秘密なので、使い終わったら控えを消してください。

### うまくいかないとき

- `https://ドメイン/healthz` が開かない → DNS・ポート開放・サービス起動を順に確認
- 証明書エラー → DNS がサーバに向く前に起動した可能性。Caddy を再起動
- ログ確認: パターンAは `sudo docker compose logs -f`、Bは `journalctl -u zatsumu -f`
- メールの送信・受信で「暗号化(STARTTLS)した接続ができないため、パスワードを送らずに
  中止しました」と出る → 送信サーバ（SMTP）はポートを **465**（最初から暗号化する方式）に、
  受信サーバ（IMAP）は「SSLで接続」を ON（ポート 993）にしてみてください。
  パスワードが盗み見られないよう、暗号化できない相手にはパスワードを送りません。
- 「certificate verify failed」（証明書の確認に失敗）と出る → サーバ名が証明書と
  合っていない可能性があります。メール業者が案内しているサーバ名をそのまま入れてください
  （IP アドレスや別名では確認に失敗します）。
