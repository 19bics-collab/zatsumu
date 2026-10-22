#!/usr/bin/env bash
# 自動更新係をサーバに登録する (最初の1回だけ)。リポジトリの置き場所で root が実行する:
#   bash deploy/auto-update/install.sh
# 登録後は、このブランチに新しい版が届きテストが通ると、5分以内に自動で反映される。
# 以後、自動更新係そのものや systemd の設定が新しくなったときは、自動更新係が自分で入れ替える。
# やめるとき: systemctl disable --now zatsumu-auto-update.timer
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "root で実行してください"; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$HERE/../.." && pwd)
CONF="${ZATSUMU_AUTO_UPDATE_CONF:-/etc/default/zatsumu-auto-update}"
STATE_DIR="${STATE_DIR:-/var/lib/zatsumu-auto-update}"

for cmd in git docker curl python3 flock timeout; do
  command -v "$cmd" >/dev/null || { echo "$cmd が見つかりません"; exit 1; }
done
git -C "$REPO_DIR" rev-parse --abbrev-ref '@{u}' >/dev/null \
  || { echo "追いかける GitHub のブランチが決まっていません (git branch -u origin/<ブランチ名>)"; exit 1; }

install -m 0755 "$HERE/zatsumu-auto-update.sh" /usr/local/sbin/zatsumu-auto-update
install -m 0644 "$HERE/zatsumu-auto-update.service" "$HERE/zatsumu-auto-update.timer" /etc/systemd/system/
echo "REPO_DIR=$REPO_DIR" > "$CONF"
systemctl daemon-reload

# 作り直している間に自動更新係が動かないよう、同じ鍵を握っておく
mkdir -p "$STATE_DIR"
exec 9>"$STATE_DIR/lock"
flock 9

# いまの版を、版の名前 (/healthz の version) 付きで作り直しておく
cd "$REPO_DIR"
ZATSUMU_VERSION=$(git rev-parse --short HEAD) docker compose up -d --build --remove-orphans
# Caddy も作り直す (git pull で Caddyfile が変わっていても、作り直すまでは古い設定のまま。証明書は消えない)
docker compose up -d --force-recreate caddy

# いま動いている版を、自動更新係に覚えさせる
echo "$(git rev-parse HEAD) $(date -Is)" > "$STATE_DIR/deployed"
rm -f "$STATE_DIR/deploying" "$STATE_DIR/failures"
exec 9>&-

systemctl enable --now zatsumu-auto-update.timer

echo
echo "自動更新係を登録しました (5分おきに GitHub の $(git rev-parse --abbrev-ref '@{u}') を確認)"
echo "動きの記録: journalctl -u zatsumu-auto-update"
