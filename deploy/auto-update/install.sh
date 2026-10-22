#!/usr/bin/env bash
# 自動更新係をサーバに登録する (最初の1回だけ)。リポジトリの置き場所で root が実行する:
#   bash deploy/auto-update/install.sh
# 登録後は、このブランチに新しい版が届きテストが通ると、5分以内に自動で反映される。
# やめるとき: systemctl disable --now zatsumu-auto-update.timer
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "root で実行してください"; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$HERE/../.." && pwd)

for cmd in git docker curl python3 flock; do
  command -v "$cmd" >/dev/null || { echo "$cmd が見つかりません"; exit 1; }
done
git -C "$REPO_DIR" rev-parse --abbrev-ref '@{u}' >/dev/null \
  || { echo "追いかける GitHub のブランチが決まっていません (git branch -u origin/<ブランチ名>)"; exit 1; }

install -m 0755 "$HERE/zatsumu-auto-update.sh" /usr/local/sbin/zatsumu-auto-update
install -m 0644 "$HERE/zatsumu-auto-update.service" "$HERE/zatsumu-auto-update.timer" /etc/systemd/system/
echo "REPO_DIR=$REPO_DIR" > /etc/default/zatsumu-auto-update
systemctl daemon-reload
systemctl enable --now zatsumu-auto-update.timer

# いまの版を、版の名前 (/healthz の version) 付きで作り直しておく
cd "$REPO_DIR"
ZATSUMU_VERSION=$(git rev-parse --short HEAD) docker compose up -d --build

echo
echo "自動更新係を登録しました (5分おきに GitHub の $(git rev-parse --abbrev-ref '@{u}') を確認)"
echo "動きの記録: journalctl -u zatsumu-auto-update"
