# m2m 読み取り係（m2m清掃管理 → ヤドツギ）

m2m清掃管理（manager-cleaning.m2msystems.cloud）の「清掃一覧」を毎朝ヘッドレスブラウザで読み、
ヤドツギの受け取り窓口（`POST /api/internal/m2m/cleanings`）に送る。
DB への反映ルール（新規作成・日付移動・キャンセル疑い）はヤドツギ側が持つ（ヤドツギ `docs/m2m-import.md`）。

- zatsumu 本体とは**別コンテナ**（`docker-compose.m2m.yml`）。メモリ上限 1GB。ブラウザが固まっても止まるのはこのコンテナだけ
- m2m の API を直接叩いたり認証情報を取り出したりはしない。画面を開いて、画面自身が受け取った一覧を読むだけ
- 送るのは清掃ID・物件名・清掃日・状態など最小限の項目だけ（清掃スタッフ名などは送らない）
- 1週でも取得に失敗した日・0件の日は送らない（読み漏れを「キャンセル」と誤判定させない）
- 2段階認証を求められたら止まる（無人では突破しない）

## 秘密情報の置き場所（重要）

**このリポジトリは公開（public）。** m2m のパスワードと合言葉は、サーバ上の `.env` にだけ書く。
`docker-compose.m2m.yml` や README に値を書かないこと。`.env` と `.env.*` は `.gitignore` 済み（ひな形の `*.example` だけ管理）。

## 導入（desk サーバ）

前提: ヤドツギ側の受け取り窓口が有効になっていること（ヤドツギ `.env` に `M2M_TENANT_ID` / `M2M_INGEST_KEY` / `M2M_INGEST_ALLOWED_IPS`=このサーバのIP）。

1. 合言葉を作る（ヤドツギ側と同じ値を使う）
   ```bash
   openssl rand -hex 32
   ```
2. `.env` に追記（`.env.mail.example` の m2m 節を参照）
   ```
   COMPOSE_FILE=docker-compose.yml:docker-compose.m2m.yml
   M2M_EMAIL=...
   M2M_PASSWORD=...
   M2M_INGEST_URL=https://<ヤドツギのドメイン>/api/internal/m2m/cleanings
   M2M_INGEST_KEY=...
   M2M_RUN_AT=06:00
   M2M_DRY_RUN=true
   ```
3. メモリ不足に備えてスワップを用意（2GB プランなら 2GB 程度。まだ無ければ）
   ```bash
   sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
   echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   ```
4. 試運転（ヤドツギの DB は変わらない）
   ```bash
   sudo docker compose build m2m
   sudo docker compose run --rm m2m python -m m2m_sync --once
   ```
   ログの `done`（作成予定件数）と「物件名がヤドツギと一致しない」の一覧を確認する。
5. 本番化: `.env` の `M2M_DRY_RUN=false` にして
   ```bash
   sudo docker compose up -d
   ```
   以後、毎日 `M2M_RUN_AT` に自動実行。取得・送信の一時的な失敗は10分後に1回だけ再挑戦する。

## 確認・トラブル時

- 最終結果: `sudo docker compose exec m2m cat /data/LAST_STATUS`（`OK …` / `FAIL …`）
- ログ: `sudo docker compose logs --tail=100 m2m`（`要確認` `物件名が` `failed` を検索）
- 失敗時の画面: `/data/*.png`（`sudo docker compose cp m2m:/data/login-failed.png .`）
- 終了コード: 2=設定・ログイン失敗 / 3=取得失敗・0件 / 4=2段階認証 / 5=ヤドツギが受け付けない・送信失敗
- 手動で1回: `sudo docker compose exec m2m python -m m2m_sync --once`

## 既知のリスク

- m2m の画面や URL が変わると止まる（止まっても誤った反映はしない。LAST_STATUS が FAIL になる）
- 自動取得が m2m（matsuri technologies）の利用規約に抵触しないかは運用者が確認すること
