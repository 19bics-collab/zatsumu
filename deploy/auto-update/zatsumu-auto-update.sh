#!/usr/bin/env bash
# zatsumu の自動更新係。systemd のタイマーで数分おきに動く (install.sh で登録)。
#
#   1. 前回の更新が途中で止まっていたら (再起動・時間切れなど)、動いていた版に戻す
#   2. GitHub から、いま使っているブランチの最新を取ってくる
#   3. 新しい版があり、GitHub のテスト (pytest) がすべて通っていれば反映する
#      (docker compose build → up。Caddy に関わるファイルが変わったときは、
#       先に新しい設定を検査してから Caddy も作り直す)
#   4. アプリや Caddy が動かなければ、元の版に戻して作り直す
#      - 動かしたのに healthy にならなかった版は「だめな版」として覚え、次の版が来るまで再挑戦しない
#      - 元の版に戻すのに失敗したら、次の回にもう一度戻す (MAX_TRIES 回まで)
#      - 作り直しの失敗 (通信・ディスク不足など一時的なことが多い) は次の回にもう一度試し、
#        同じ版で MAX_TRIES 回続けて失敗したら「だめな版」とする
#   5. 自動更新係そのもの (このスクリプトと systemd の設定) が新しくなっていたら入れ替える
#
# 次のときは何もしない: サーバ上で手で書き換えたファイルがある / 履歴が一直線でない
# (fast-forward できない) / 新しい版が、サーバにだけある (git で管理していない) ファイルと
# 同じ名前のファイルを持ち込む (.env の上書きを防ぐ)。.env は反映の前に STATE_DIR に控えを取る。
# 動きの記録: journalctl -u zatsumu-auto-update
#
# STATE_DIR に置くもの:
#   deployed   最後に反映に成功した版 (コミット番号と日時)。「いま動いている版」はこれで判断する
#   deploying  反映の途中の印 ("元の版 新しい版")。残っていたら前回が途中で止まった
#              (再起動・時間切れ・ディスク不足など) ということ。元の版に戻すのに失敗したときは
#              3つ目に失敗した回数を書いて残す ("元の版 新しい版 回数")。次の回にもう一度戻す
#   bad        だめな版 (次の版が来るまで再挑戦しない)
#   failures   同じ版が続けて一時的に失敗した回数   env.backup  .env の控え
set -euo pipefail

CONF="${ZATSUMU_AUTO_UPDATE_CONF:-/etc/default/zatsumu-auto-update}"
# 環境変数で渡された値は、設定ファイルより優先する (テストが本番の設定に引きずられないように)
saved_env=$(declare -p REPO_DIR STATE_DIR HEALTH_TIMEOUT HEALTH_INTERVAL CADDY_SETTLE \
  MAX_TRIES NO_CI_TIMEOUT INSTALL_BIN UNIT_DIR 2>/dev/null || true)
# shellcheck disable=SC1090
[ -r "$CONF" ] && . "$CONF"
eval "$saved_env"
REPO_DIR="${REPO_DIR:-/opt/desk}"
STATE_DIR="${STATE_DIR:-/var/lib/zatsumu-auto-update}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"   # 新しい版が healthy になるまで待つ秒数
HEALTH_INTERVAL="${HEALTH_INTERVAL:-5}"
CADDY_SETTLE="${CADDY_SETTLE:-15}"        # Caddy を作り直した後、動き続けるか見守る秒数
MAX_TRIES="${MAX_TRIES:-3}"               # 同じ版の一時的な失敗 (と、元に戻す失敗) を、続けて何回まで許すか
NO_CI_TIMEOUT="${NO_CI_TIMEOUT:-1800}"    # コミットからこの秒数たってもテストが無ければ異常とする
INSTALL_BIN="${INSTALL_BIN:-/usr/local/sbin/zatsumu-auto-update}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
HERE=deploy/auto-update

log() { echo "[auto-update] $*"; }
# docker compose に時間の上限を付けて呼ぶ (固まったら打ち切る)。例: dc 900 build
dc() { local secs=$1; shift; timeout -k 30 "$secs" docker compose "$@"; }
is_dirty() { ! git diff --quiet || ! git diff --cached --quiet; }
set_version() { ZATSUMU_VERSION=$(git rev-parse --short HEAD); export ZATSUMU_VERSION; }

mkdir -p "$STATE_DIR"
exec 9>"$STATE_DIR/lock"
flock -n 9 || { log "前回の更新がまだ動いているので今回は見送り"; exit 0; }

cd "$REPO_DIR"

branch=$(git symbolic-ref -q --short HEAD) || { log "ブランチに乗っていない (detached HEAD) ので更新しない"; exit 1; }
remote=$(git config "branch.$branch.remote" || true)
merge=$(git config "branch.$branch.merge" || true)
if [ -z "$remote" ] || [ -z "$merge" ]; then
  log "追いかける GitHub のブランチが決まっていません (git branch -u origin/<ブランチ名>)"
  exit 1
fi
upstream="$remote/${merge#refs/heads/}"   # 例: origin/main

# ---------- 作り直しの部品 ----------

# $1 と $2 の間で、Caddy に関わるファイル (Caddyfile* と docker-compose*.yml) が変わったら yes
caddy_changes() {
  if [ -n "$(git diff --name-only --no-renames "$1" "$2" -- 'Caddyfile*' 'docker-compose*.yml' 'docker-compose*.yaml')" ]; then
    echo yes
  else
    echo no
  fi
}

# 新しい Caddy の設定を、動いている Caddy には触らずに検査する (.env と compose の設定はそのまま使われる)
validate_caddy() {
  local out
  set_version
  if ! out=$(dc 120 run --rm --no-deps -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1); then
    tail -n 20 <<<"$out"
    log "Caddy (入口の門番) の新しい設定に誤りがあります"
    return 1
  fi
}

wait_healthy() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) cid status
  while [ "$SECONDS" -lt "$deadline" ]; do
    cid=$(dc 30 ps -q app || true)
    status=$(timeout 30 docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo missing)
    [ "$status" = healthy ] && return 0
    sleep "$HEALTH_INTERVAL"
  done
  return 1
}

# 外から見えるドメイン (localhost のままなら空)。$1 = Caddy のコンテナ。
# .env を自分で読むと docker compose と読み方 (行末のコメントなど) がずれるので、
# compose が Caddy に渡した値と、Caddy が使っている設定ファイルの名前を Caddy から読む
public_domain() {
  local file envs key d
  file=$(timeout 30 docker inspect -f '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}' "$1") || return 0
  envs=$(timeout 30 docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1") || return 0
  case "${file##*/}" in
    *mail*) key=ZATSUMU_MAIL_DOMAIN ;;
    *) key=ZATSUMU_DOMAIN ;;
  esac
  d=$(sed -n "s/^$key=//p" <<<"$envs" | tail -n 1)
  case "$d" in ""|localhost|*.localhost) ;; *) echo "$d" ;; esac
}

# Caddy が落ちては再起動する、を繰り返していないか。できれば Caddy 経由で /healthz も確かめる
caddy_ok() {
  local cid before after domain i
  cid=$(dc 30 ps -q caddy) && [ -n "$cid" ] || return 1
  # 再起動を待っている間も .State.Running は true なので、.State.Status (running / restarting など) で見る
  before=$(timeout 30 docker inspect -f '{{.State.Status}} {{.RestartCount}}' "$cid") || return 1
  sleep "$CADDY_SETTLE"
  after=$(timeout 30 docker inspect -f '{{.State.Status}} {{.RestartCount}}' "$cid") || return 1
  [ "${after%% *}" = running ] && [ "$after" = "$before" ] || return 1
  domain=$(public_domain "$cid")
  [ -n "$domain" ] || return 0
  # 証明書の中身までは問わず (-k)、Caddy を通ってアプリまで届くかだけを見る
  for i in 1 2 3 4 5 6; do
    curl -fsSk -o /dev/null --max-time 10 --resolve "$domain:443:127.0.0.1" "https://$domain/healthz" && return 0
    sleep "$HEALTH_INTERVAL"
  done
  return 1
}

# いまの作業フォルダの版で作り直して動かす。$1 = yes なら Caddy も作り直す。戻り値:
#   1 = 作れなかった (動いているコンテナには触っていない。通信・ディスクなど一時的なことが多い)
#   2 = 起動 (up) に失敗した   3 = 動かしたが healthy にならなかった / Caddy が動き続けない
# if の条件の中では set -e が効かないので、失敗はそれぞれ return で返す
deploy_tree() {
  set_version
  dc 900 build || { log "作り直し (build) に失敗しました"; return 1; }
  dc 300 up -d --remove-orphans || { log "起動 (up) に失敗しました"; return 2; }
  if [ "$1" = yes ]; then
    dc 120 up -d --force-recreate caddy || { log "Caddy の作り直しに失敗しました"; return 2; }
  fi
  wait_healthy || { log "アプリが healthy になりませんでした"; return 3; }
  if [ "$1" = yes ]; then
    caddy_ok || { log "Caddy が動き続けません (docker compose logs caddy で確認)"; return 3; }
  fi
  return 0
}

mark_bad() {
  echo "$1" > "$STATE_DIR/bad" || true
  rm -f "$STATE_DIR/failures" || true
}

# 一時的な失敗を数える。同じ版で MAX_TRIES 回続いたら「だめな版」にする
note_failure() {
  local prev="" n=0
  read -r prev n 2>/dev/null < "$STATE_DIR/failures" || true
  [ "$prev" = "$1" ] && [ -n "$n" ] || n=0
  n=$((n + 1))
  if [ "$n" -ge "$MAX_TRIES" ]; then
    mark_bad "$1"
    log "新しい版 ${1:0:7} は ${n} 回続けて反映できなかったので、あきらめます (次の版が来るまで再挑戦しない)"
  else
    echo "$1 $n" > "$STATE_DIR/failures" || true
    log "次の回にもう一度試します (${n}/${MAX_TRIES} 回目の失敗)"
  fi
}

# 元の版 ($1) に戻すのに失敗した。$3 = これまでに戻すのに失敗した回数。
# 新しい版 ($2) のコンテナが動いたままかもしれないので、MAX_TRIES 回までは次の回にもう一度戻す
rollback_failed() {
  local n=$(($3 + 1))
  if [ "$n" -lt "$MAX_TRIES" ]; then
    echo "$1 $2 $n" > "$STATE_DIR/deploying" || true
    log "元の版に戻しても起動しません (${n}/${MAX_TRIES} 回目)。次の回にもう一度戻してみます。サーバも確認してください (docker compose ps / docker compose logs)"
  else
    rm -f "$STATE_DIR/deploying" || true
    log "元の版に戻しても起動しません (${n} 回試したのでやめます)。サーバを確認してください (docker compose ps / docker compose logs)"
  fi
}

# $1 から $2 で増えたファイルのうち、git が書き出しただけで管理から外れて残ったものを消す。
# git merge がファイルを書いた後、記録する前に止められると残る (残ると、次からの
# 「サーバにだけあるファイル」の検査に引っかかって更新できなくなる)。
# 反映の前の検査で、これらの名前のファイルが無かったことは確かめてある。中身が同じものだけ消す
remove_leftovers() {
  local f
  while IFS= read -r -d '' f; do
    [ -f "$f" ] && [ ! -L "$f" ] || continue
    git ls-files --error-unmatch -- "$f" >/dev/null 2>&1 && continue
    if [ "$(git hash-object -- "$f")" = "$(git rev-parse -q --verify "$2:$f" || true)" ]; then
      rm -f -- "$f"
    fi
  done < <(git diff -z --name-only --no-renames --diff-filter=A "$1" "$2")
  return 0
}

# 自動更新係そのもの (このスクリプトと systemd の設定) が新しくなっていたら入れ替える。
# いまの作業フォルダが「反映に成功した版」のときだけ呼ぶ
refresh_self() {
  local u reload=no
  if [ -f "$HERE/zatsumu-auto-update.sh" ] && ! cmp -s "$HERE/zatsumu-auto-update.sh" "$INSTALL_BIN"; then
    # 動いているこの回に影響しないよう、別名で置いてから差し替える
    if install -D -m 0755 "$HERE/zatsumu-auto-update.sh" "$INSTALL_BIN.new" && mv -f "$INSTALL_BIN.new" "$INSTALL_BIN"; then
      log "自動更新係を新しくしました (次の回から使われます)"
    else
      log "自動更新係を新しくできませんでした"
    fi
  fi
  for u in zatsumu-auto-update.service zatsumu-auto-update.timer; do
    [ -f "$HERE/$u" ] && ! cmp -s "$HERE/$u" "$UNIT_DIR/$u" || continue
    if install -D -m 0644 "$HERE/$u" "$UNIT_DIR/$u"; then reload=yes; else log "$u を新しくできませんでした"; fi
  done
  if [ "$reload" = yes ]; then
    if timeout 60 systemctl daemon-reload; then
      log "自動更新係の systemd の設定 (時間の上限など) を新しくしました"
    else
      log "systemctl daemon-reload に失敗しました"
    fi
  fi
}

# ---------- 1. 前回の更新が途中で止まっていないか ----------

record="" record_rest=""
read -r record record_rest 2>/dev/null < "$STATE_DIR/deployed" || true
running=""
# 古い自動更新係は短い番号 (7文字) を書いていたので、完全な番号に直して使う
[ -n "$record" ] && running=$(git rev-parse -q --verify "$record^{commit}" || true)
marker=$(cat "$STATE_DIR/deploying" 2>/dev/null || true)
m_old="" m_new="" m_tries=""
read -r m_old m_new m_tries <<<"$marker" || true
head=$(git rev-parse HEAD)
running=${running:-${m_old:-$head}}   # 記録が無ければ、いまのファイルの版が動いているとみなす

if [ -n "$marker" ] && [ "$m_new" = "$running" ]; then
  rm -f "$STATE_DIR/deploying"   # 反映は済んでいて、印を消す前に止まっただけ
  marker=""
fi
if [ -n "$marker" ] || [ "$head" != "$running" ]; then
  target=${m_new:-$head}
  if [ -z "$marker" ] && is_dirty; then
    log "サーバ上で書き換えたファイルがあるので更新しない (git status で確認)"
    exit 1
  fi
  if [ -n "$m_tries" ]; then
    log "前回、元の版 ${running:0:7} に戻せなかったので、もう一度戻します"
  else
    log "前回の更新 (${running:0:7} -> ${target:0:7}) が途中で止まっていたので、動いていた版 ${running:0:7} に戻します"
  fi
  [ -z "$marker" ] || rm -f "$(git rev-parse --git-dir)/index.lock"   # 止められた git が残した鍵
  caddy=$(caddy_changes "$running" "$target")
  git reset -q --hard "$running"
  [ -z "$marker" ] || remove_leftovers "$running" "$target"
  ok=yes
  deploy_tree "$caddy" || ok=no
  echo "$running $(date -Is)" > "$STATE_DIR/deployed" || true
  # 途中で止まった反映は、一時的な失敗として数える (戻しのやり直しと、だめな版と決まっている版は数えない)
  if [ -n "$marker" ] && [ -z "$m_tries" ] && [ "$(cat "$STATE_DIR/bad" 2>/dev/null)" != "$target" ]; then
    note_failure "$target"
  fi
  if [ "$ok" = no ]; then
    rollback_failed "$running" "$target" "${m_tries:-0}"
    exit 1
  fi
  rm -f "$STATE_DIR/deploying" || true
  log "元の版 ${running:0:7} に戻しました。次の回に、もう一度新しい版を確かめます"
  exit 0
fi
if [ "$record" != "$running" ]; then
  echo "$running ${record_rest:-$(date -Is)}" > "$STATE_DIR/deployed" || true
fi

refresh_self

# ---------- 2. 新しい版を確かめる ----------

if ! timeout 120 git fetch -q --prune "$remote"; then
  log "GitHub から最新を取ってこられませんでした (通信の不調など)。次の回にもう一度試します"
  exit 1
fi
if ! new=$(git rev-parse -q --verify "refs/remotes/$upstream"); then
  log "追いかけている GitHub のブランチ ($upstream) が無くなりました。更新を止めています。"
  log "別のブランチを追いかけるには DEPLOY.md の「追いかけるブランチを変えるとき」を見てください"
  exit 1
fi
old=$running
[ "$old" = "$new" ] && exit 0

if [ "$(cat "$STATE_DIR/bad" 2>/dev/null)" = "$new" ]; then
  exit 0   # 前に反映して動かなかった版。次の版が来るまで待つ
fi
if ! git merge-base --is-ancestor "$old" "$new"; then
  log "履歴が一直線でない (fast-forward できない) ので更新しない: $old -> $new"
  exit 1
fi
if is_dirty; then
  log "サーバ上で書き換えたファイルがあるので更新しない (git status で確認)"
  exit 1
fi

# GitHub のテスト結果を確かめる (公開リポジトリなので認証は不要)
slug=$(git remote get-url "$remote" | sed -E 's#^(https://github\.com/|git@github\.com:)##; s#\.git$##')
if ! ci=$(curl -fsS --max-time 20 -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$slug/commits/$new/check-runs?per_page=100" \
    | python3 "$HERE/ci_status.py"); then
  log "テスト結果を取得できなかったので次の回にもう一度見る"
  exit 0
fi
case "$ci" in
  success) ;;
  pending) log "新しい版 ${new:0:7} のテストがまだ終わっていないので待つ"; exit 0 ;;
  missing)
    age=$(( $(date +%s) - $(git log -1 --format=%ct "$new") ))
    if [ "$age" -ge "$NO_CI_TIMEOUT" ]; then
      log "新しい版 ${new:0:7} にテストが走っていません ($((age / 60)) 分たっても GitHub に pytest の結果がありません)。"
      log ".github/workflows/tests.yml が、このブランチへの push で動くか確かめてください"
      exit 1
    fi
    log "新しい版 ${new:0:7} のテストがまだ始まっていないので待つ"
    exit 0 ;;
  *) log "新しい版 ${new:0:7} はテストに失敗しているので反映しない"; exit 0 ;;
esac

# サーバにだけあるファイル (.env など) を、新しい版が同じ名前で持ち込むなら止める
collisions=""
while IFS= read -r -d '' f; do
  if [ -e "$f" ] || [ -L "$f" ]; then collisions+=" $f"; fi
done < <(git diff -z --name-only --no-renames --diff-filter=A "$old" "$new")   # 名前の変更も「追加」として数える
if [ -n "$collisions" ]; then
  log "新しい版 ${new:0:7} が、サーバにだけある (git で管理していない) ファイルを上書きしてしまうので更新しない:$collisions"
  exit 1
fi

# ---------- 3. 反映する ----------

log "反映します: ${old:0:7} -> ${new:0:7}"
if [ -f .env ]; then install -m 0600 .env "$STATE_DIR/env.backup"; fi
echo "$old $new" > "$STATE_DIR/deploying"   # これを書けなければ (ディスク不足など) ここで止まる
git merge -q --ff-only "$new"
caddy=$(caddy_changes "$old" "$new")

rc=0
if [ "$caddy" = yes ] && ! validate_caddy; then
  rc=1
else
  deploy_tree "$caddy" || rc=$?
fi

case $rc in
  0)
    echo "$new $(date -Is)" > "$STATE_DIR/deployed"
    rm -f "$STATE_DIR/deploying" "$STATE_DIR/bad" "$STATE_DIR/failures"
    timeout 300 docker image prune -f >/dev/null || true   # 古い版のイメージを片付ける (ディスクを食わないように)
    refresh_self
    log "反映しました: ${new:0:7}"
    exit 0 ;;
  1)
    # 動いているコンテナには触っていないので、ファイルを戻すだけでよい
    git reset -q --hard "$old"
    rm -f "$STATE_DIR/deploying" || true
    log "新しい版 ${new:0:7} を反映できなかったので、元の版 ${old:0:7} のままにします"
    note_failure "$new" ;;
  *)
    log "新しい版 ${new:0:7} が動かなかったので元の版 ${old:0:7} に戻します"
    # だめな版の記録は、戻す前に書く (戻している途中で止まっても、同じ版を作り直さないように)
    [ "$rc" != 3 ] || mark_bad "$new"
    git reset -q --hard "$old"
    if deploy_tree "$caddy"; then
      rm -f "$STATE_DIR/deploying" || true
      log "元の版 ${old:0:7} に戻しました"
    else
      rollback_failed "$old" "$new" 0
    fi
    if [ "$rc" = 3 ]; then
      log "新しい版 ${new:0:7} には、次の版が来るまで再挑戦しません"
    else
      note_failure "$new"
    fi ;;
esac
exit 1
