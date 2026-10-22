#!/usr/bin/env bash
# テスト用の控え: 本番に最初に入れた自動更新係 (コミット c789522 の deploy/auto-update/zatsumu-auto-update.sh)。
# 新しい自動更新係への引き継ぎを確かめるために使う (tests/test_auto_update.py)。書き換えないこと。
# zatsumu の自動更新係。systemd のタイマーで数分おきに動く (install.sh で登録)。
#
#   1. GitHub から、いま使っているブランチの最新を取ってくる
#   2. 新しい版があり、GitHub のテストがすべて通っていれば反映する
#      (docker compose up -d --build。Caddyfile が変わったときは Caddy も作り直す)
#   3. アプリが healthy にならなければ、元の版に戻して作り直す
#      (失敗した版は覚えておき、次の版が来るまで再挑戦しない)
#
# サーバ上で手で書き換えたファイルがある・履歴が一直線でない (fast-forward できない)
# ときは何もしない。.env などの git で管理していないファイルには触らない。
# 動きの記録: journalctl -u zatsumu-auto-update
set -euo pipefail

CONF=/etc/default/zatsumu-auto-update
# shellcheck disable=SC1090
[ -r "$CONF" ] && . "$CONF"
REPO_DIR="${REPO_DIR:-/opt/desk}"
STATE_DIR="${STATE_DIR:-/var/lib/zatsumu-auto-update}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"   # 新しい版が healthy になるまで待つ秒数
HEALTH_INTERVAL="${HEALTH_INTERVAL:-5}"
HERE=deploy/auto-update

log() { echo "[auto-update] $*"; }

mkdir -p "$STATE_DIR"
exec 9>"$STATE_DIR/lock"
flock -n 9 || { log "前回の更新がまだ動いているので今回は見送り"; exit 0; }

cd "$REPO_DIR"

upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}')   # 例: origin/main
remote=${upstream%%/*}
git fetch -q "$remote"

old=$(git rev-parse HEAD)
new=$(git rev-parse "$upstream")
[ "$old" = "$new" ] && exit 0

if [ "$(cat "$STATE_DIR/bad" 2>/dev/null)" = "$new" ]; then
  exit 0   # 前に反映して起動しなかった版。次の版が来るまで待つ
fi
if ! git merge-base --is-ancestor "$old" "$new"; then
  log "履歴が一直線でない (fast-forward できない) ので更新しない: $old -> $new"
  exit 1
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
  log "サーバ上で書き換えたファイルがあるので更新しない (git status で確認)"
  exit 1
fi

# GitHub のテスト結果を確かめる (公開リポジトリなので認証は不要)
slug=$(git remote get-url "$remote" | sed -E 's#^(https://github\.com/|git@github\.com:)##; s#\.git$##')
if ! ci=$(curl -fsS --max-time 20 -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$slug/commits/$new/check-runs?per_page=100" \
    | python3 "$REPO_DIR/$HERE/ci_status.py"); then
  log "テスト結果を取得できなかったので次の回にもう一度見る"
  exit 0
fi
case "$ci" in
  success) ;;
  pending) log "新しい版 ${new:0:7} のテストがまだ終わっていないので待つ"; exit 0 ;;
  *) log "新しい版 ${new:0:7} はテストに失敗しているので反映しない"; exit 0 ;;
esac

changed=$(git diff --name-only "$old" "$new")

# if の条件の中では set -e が効かないので、失敗はそれぞれ return 1 で返す
build() {
  ZATSUMU_VERSION=$(git rev-parse --short HEAD) docker compose up -d --build || return 1
  if grep -q '^Caddyfile' <<<"$changed"; then
    docker compose up -d --force-recreate caddy || return 1
  fi
}

wait_healthy() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) cid status
  while [ "$SECONDS" -lt "$deadline" ]; do
    cid=$(docker compose ps -q app)
    status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo missing)
    [ "$status" = healthy ] && return 0
    sleep "$HEALTH_INTERVAL"
  done
  return 1
}

log "反映します: ${old:0:7} -> ${new:0:7}"
git merge -q --ff-only "$upstream"
if build && wait_healthy; then
  rm -f "$STATE_DIR/bad"
  echo "${new:0:7} $(date -Is)" > "$STATE_DIR/deployed"
  docker image prune -f >/dev/null || true   # 古い版のイメージを片付ける (ディスクを食わないように)
  # 自動更新係そのものが新しくなっていたら入れ替える (動いているこの回には影響しない)
  if grep -q "^$HERE/zatsumu-auto-update.sh$" <<<"$changed"; then
    install -m 0755 "$HERE/zatsumu-auto-update.sh" /usr/local/sbin/zatsumu-auto-update
  fi
  log "反映しました: ${new:0:7}"
  exit 0
fi

log "新しい版 ${new:0:7} が起動しなかったので元の版 ${old:0:7} に戻します"
echo "$new" > "$STATE_DIR/bad"
git reset -q --hard "$old"
if build && wait_healthy; then
  log "元の版 ${old:0:7} に戻しました"
else
  log "元の版に戻しても起動しません。サーバを確認してください (docker compose logs app)"
fi
exit 1
