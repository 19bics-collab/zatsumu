"""自動更新係 (deploy/auto-update) のテスト.

本物の docker / GitHub は使わず、PATH の先頭に偽の docker・curl を置いて、
手元の git リポジトリ2つ (GitHub 役と サーバ役) で更新の流れを通しで確かめる。
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "deploy" / "auto-update"
SCRIPT = HERE / "zatsumu-auto-update.sh"

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or not all(shutil.which(c) for c in ("bash", "git", "flock")),
    reason="bash / git / flock のある Linux 向け",
)


def _load_ci_status():
    spec = importlib.util.spec_from_file_location("ci_status", HERE / "ci_status.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(name="pytest (Python 3.12)", conclusion="success", status="completed",
         app="github-actions"):
    return {"name": name, "status": status, "conclusion": conclusion, "app": {"slug": app}}


def test_ci_status_evaluate():
    ev = _load_ci_status().evaluate
    assert ev({"check_runs": [_run("pytest (Python 3.10)"), _run()]}) == "success"
    assert ev({"check_runs": []}) == "missing"  # まだテストが始まっていない / 走っていない
    assert ev({"check_runs": [_run(), _run(conclusion=None, status="in_progress")]}) == "pending"
    # 片方が走っている途中でも、もう片方が落ちていれば反映しない
    assert ev({"check_runs": [_run(conclusion="failure"),
                              _run(conclusion=None, status="queued")]}) == "failure"
    assert ev({"check_runs": [_run(conclusion="cancelled")]}) == "failure"
    # pytest が飛ばされたのは「通った」ではない
    assert ev({"check_runs": [_run(conclusion="skipped")]}) == "failure"


def test_ci_status_counts_only_pytest_from_github_actions():
    ev = _load_ci_status().evaluate
    # ほかの仕組み (誰でも付けられる) の「成功」や、pytest 以外のジョブだけでは反映しない
    assert ev({"check_runs": [_run(app="someone-else"), _run("lint")]}) == "missing"
    # ほかの仕組みが「失敗」を付けても、pytest が通っていれば反映する
    assert ev({"check_runs": [_run(), _run(app="someone-else", conclusion="failure"),
                              _run("lint", conclusion="failure")]}) == "success"
    assert ev({"check_runs": [{"name": "pytest", "status": "completed",
                               "conclusion": "success"}]}) == "missing"  # app が無い


@needs_bash
@pytest.mark.parametrize("name", ["zatsumu-auto-update.sh", "install.sh"])
def test_scripts_parse(name):
    subprocess.run(["bash", "-n", str(HERE / name)], check=True)


def test_units_point_to_installed_script():
    service = (HERE / "zatsumu-auto-update.service").read_text(encoding="utf-8")
    install = (HERE / "install.sh").read_text(encoding="utf-8")
    assert "ExecStart=/usr/local/sbin/zatsumu-auto-update" in service
    assert "TimeoutStartSec=" in service  # 固まっても次の回に進めるように
    assert "/usr/local/sbin/zatsumu-auto-update" in install
    assert "zatsumu-auto-update.timer" in install
    assert "--force-recreate caddy" in install
    assert "ZATSUMU_AUTO_UPDATE_CONF" in install


def test_workflow_runs_on_push_to_tracked_branches():
    wf = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "'claude/**'" in wf
    assert "name: pytest" in wf  # 自動更新係は名前が pytest で始まる結果だけを数える


# ---------- 通しの確認 (偽の docker / curl / systemctl) ----------

# FAKE_RULES の各行 "<種類> <版の短い番号 または any>" で、偽の docker / curl の失敗を決める。
# 版は自動更新係が渡す ZATSUMU_VERSION で見分ける
FAKE_COMMON = """
rule() { grep -qxE "$1 (any|${ZATSUMU_VERSION:-none})" "$FAKE_RULES" 2>/dev/null; }
"""

FAKE_DOCKER = "#!/usr/bin/env bash" + FAKE_COMMON + """
# 途中で止められた (再起動・時間切れ) ことにする: 自動更新係 (新旧どちらでも) を強制終了する
kill_updater() {
  p=$PPID
  while [ "${p:-1}" -gt 1 ]; do
    if tr '\0' ' ' < "/proc/$p/cmdline" | grep -q zatsumu-auto-update; then kill -9 "$p"; exit 1; fi
    p=$(awk '{print $4}' "/proc/$p/stat")
  done
}
echo "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "compose ps") echo cid123 ;;
  "compose build")
    if rule kill-build; then kill_updater; fi
    ! rule buildfail ;;
  "compose up")
    if rule kill-up; then kill_updater; fi
    ! rule upfail ;;
  "compose run") ! rule validatefail ;;
  "inspect -f")
    case "$3" in
      *RestartCount*)
        if rule crash; then
          n=$(( $(cat "$FAKE_RULES.restarts" 2>/dev/null || echo 0) + 1 ))
          echo "$n" > "$FAKE_RULES.restarts"
          echo "running $n"
        elif rule restarting; then
          echo "restarting 1"   # 再起動を待っている間 (回数は増えない)
        else
          echo "running 0"
        fi ;;
      # Caddy が使っている設定ファイルと、compose が Caddy に渡した値
      *Mounts*) cat "$FAKE_CADDY_MOUNT" 2>/dev/null ;;
      *Config.Env*) cat "$FAKE_CADDY_ENV" 2>/dev/null ;;
      *) if rule unhealthy; then echo unhealthy; else echo healthy; fi ;;
    esac ;;
esac
"""

FAKE_CURL = "#!/usr/bin/env bash" + FAKE_COMMON + """
case "${@: -1}" in
  https://api.github.com/*) cat "$FAKE_CI" ;;
  *) echo "$*" >> "$FAKE_CURL_LOG"; ! rule httpsfail ;;
esac
"""

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_SYSTEMCTL_LOG"
"""


def _git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """GitHub 役 (origin) と、それを clone したサーバ役 (server) を用意する."""
    for k in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{k}_NAME", "t")
        monkeypatch.setenv(f"{k}_EMAIL", "t@example.com")
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    shutil.copytree(HERE, origin / "deploy" / "auto-update",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (origin / ".gitignore").write_text(".env\n")  # 本物と同じく .env は git に入れない
    (origin / "app.txt").write_text("v1\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "v1")
    server = tmp_path / "server"
    _git(tmp_path, "clone", "-q", str(origin), str(server))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL),
                       ("systemctl", FAKE_SYSTEMCTL)):
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
    w = {
        "origin": origin, "server": server, "state": tmp_path / "state",
        "log": tmp_path / "docker.log", "curl_log": tmp_path / "curl.log",
        "systemctl_log": tmp_path / "systemctl.log", "rules": tmp_path / "rules",
        "ci": tmp_path / "ci.json", "bin": tmp_path / "sbin" / "zatsumu-auto-update",
        "units": tmp_path / "units", "caddy_mount": tmp_path / "caddy_mount",
        "caddy_env": tmp_path / "caddy_env",
    }
    w["rules"].write_text("")
    w["ci"].write_text(json.dumps({"check_runs": [_run()]}))
    w["env"] = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        # 本番の設定ファイル (/etc/default/...) は決して読まない
        "ZATSUMU_AUTO_UPDATE_CONF": "/dev/null",
        "REPO_DIR": str(server), "STATE_DIR": str(w["state"]),
        "INSTALL_BIN": str(w["bin"]), "UNIT_DIR": str(w["units"]),
        "HEALTH_TIMEOUT": "1", "HEALTH_INTERVAL": "0.1", "CADDY_SETTLE": "0",
        "FAKE_LOG": str(w["log"]), "FAKE_CURL_LOG": str(w["curl_log"]),
        "FAKE_SYSTEMCTL_LOG": str(w["systemctl_log"]), "FAKE_RULES": str(w["rules"]),
        "FAKE_CI": str(w["ci"]), "FAKE_CADDY_MOUNT": str(w["caddy_mount"]),
        "FAKE_CADDY_ENV": str(w["caddy_env"]),
    }
    return w


def _push(world, text, files=("app.txt",), env=None):
    for f in files:
        p = world["origin"] / f
        p.write_text(text)
    _git(world["origin"], "add", "-A", "-f")
    _git(world["origin"], "commit", "-q", "-m", text, env=env)
    return _git(world["origin"], "rev-parse", "HEAD")


def _rules(world, *lines):
    world["rules"].write_text("".join(f"{line}\n" for line in lines))


def _update(world, script=SCRIPT):
    return subprocess.run(["bash", str(script)], env=world["env"], capture_output=True,
                          text=True)


def _caddy_container(world, caddyfile, **env):
    """動いている Caddy の様子 (使っている設定ファイルと、compose が渡した値) を決める."""
    world["caddy_mount"].write_text(f"/opt/desk/{caddyfile}\n")
    world["caddy_env"].write_text("".join(f"{k}={v}\n" for k, v in env.items()))


def _docker_calls(world):
    return world["log"].read_text().splitlines() if world["log"].exists() else []


def _clear_calls(world):
    for k in ("log", "curl_log", "systemctl_log"):
        if world[k].exists():
            world[k].unlink()


def _head(world):
    return _git(world["server"], "rev-parse", "HEAD")


def _state(world, name):
    p = world["state"] / name
    return p.read_text().strip() if p.exists() else None


@needs_bash
def test_nothing_new_does_nothing(world):
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _docker_calls(world) == []
    # 初めての回は、いまの版を「動いている版」として完全な番号で覚える
    assert _state(world, "deployed").split()[0] == _head(world)


@needs_bash
def test_deploys_new_version_after_ci_passes(world):
    (world["server"] / ".env").write_text("SECRET=prod\n")
    new = _push(world, "v2\n")
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _head(world) == new
    calls = _docker_calls(world)
    # 作る (build) と動かす (up) を分け、新しい版で減ったコンテナも片付ける
    assert "compose build" in calls
    assert "compose up -d --remove-orphans" in calls
    # Caddy に関わるファイルが変わっていなければ Caddy は検査も作り直しもしない
    assert not any("caddy" in c for c in calls)
    assert _state(world, "deployed").split()[0] == new
    assert _state(world, "deploying") is None
    # .env の控えを、本人だけが読める形で取っておく
    backup = world["state"] / "env.backup"
    assert backup.read_text() == "SECRET=prod\n"
    assert backup.stat().st_mode & 0o777 == 0o600


@needs_bash
@pytest.mark.parametrize("changed", ["Caddyfile.mail", "docker-compose.yml"])
def test_caddy_related_change_validates_and_recreates_caddy(world, changed):
    _push(world, "site {}\n", files=(changed,))
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = _docker_calls(world)
    validate = ("compose run --rm --no-deps -T caddy caddy validate "
                "--config /etc/caddy/Caddyfile --adapter caddyfile")
    assert validate in calls
    assert "compose up -d --force-recreate caddy" in calls
    # 検査は作り直しより前
    assert calls.index(validate) < calls.index("compose build")


@needs_bash
def test_caddy_checked_through_https_when_domain_is_set(world):
    # .env は docker compose が読む (行末のコメントや Windows の改行があっても)。
    # 自動更新係は .env を自分では読まず、compose が Caddy に渡した値を使う
    (world["server"] / ".env").write_text(
        "ZATSUMU_CADDYFILE=Caddyfile.mail\r\n"
        "ZATSUMU_MAIL_DOMAIN=desk.example.com  # メール\r\n")
    _caddy_container(world, "Caddyfile.mail", ZATSUMU_DOMAIN="localhost",
                     ZATSUMU_MAIL_DOMAIN="desk.example.com")
    _push(world, "site {}\n", files=("Caddyfile.mail",))
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    curl = world["curl_log"].read_text()
    assert "--resolve desk.example.com:443:127.0.0.1 https://desk.example.com/healthz" in curl


@needs_bash
def test_caddy_https_check_skipped_for_localhost(world):
    _caddy_container(world, "Caddyfile", ZATSUMU_DOMAIN="localhost",
                     ZATSUMU_MAIL_DOMAIN="mail.localhost")
    _push(world, "site {}\n", files=("Caddyfile",))
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not world["curl_log"].exists()


@needs_bash
def test_caddy_validate_failure_does_not_deploy(world):
    old = _head(world)
    _rules(world, "validatefail any")
    new = _push(world, "broken {\n", files=("Caddyfile.mail",))
    r = _update(world)
    assert r.returncode == 1
    assert "設定に誤り" in r.stdout
    assert _head(world) == old
    calls = _docker_calls(world)
    assert not any(c.startswith(("compose build", "compose up")) for c in calls)
    assert _state(world, "deploying") is None
    assert _state(world, "failures") == f"{new} 1"


@needs_bash
@pytest.mark.parametrize("rule", ["crash", "restarting", "httpsfail"])
def test_caddy_not_running_rolls_back(world, rule):
    _caddy_container(world, "Caddyfile", ZATSUMU_DOMAIN="desk.example.com")
    old = _head(world)
    new = _push(world, "site {}\n", files=("Caddyfile",))
    _rules(world, f"{rule} {new[:7]}")
    r = _update(world)
    assert r.returncode == 1
    assert "元の版" in r.stdout and "戻しました" in r.stdout
    assert _head(world) == old
    # 戻すときも、古い設定で Caddy を作り直す
    assert _docker_calls(world).count("compose up -d --force-recreate caddy") == 2
    assert _state(world, "bad") == new
    assert _state(world, "deploying") is None


@pytest.mark.parametrize("ci, word", [
    ({"check_runs": [_run(conclusion="failure")]}, "失敗"),
    ({"check_runs": [_run(conclusion=None, status="in_progress")]}, "まだ終わっていない"),
    ({"check_runs": [_run(app="someone-else")]}, "まだ始まっていない"),
])
@needs_bash
def test_waits_or_skips_unless_ci_passed(world, ci, word):
    old = _head(world)
    world["ci"].write_text(json.dumps(ci))
    _push(world, "v2\n")
    r = _update(world)
    assert r.returncode == 0
    assert word in r.stdout
    assert _head(world) == old
    assert _docker_calls(world) == []


@needs_bash
def test_no_tests_for_a_long_time_is_an_error(world):
    old = _head(world)
    world["ci"].write_text(json.dumps({"check_runs": []}))
    # 1時間前のコミットなのに、pytest の結果が1件もない
    env = {**os.environ, "GIT_COMMITTER_DATE": f"@{int(time.time()) - 3600} +0900"}
    _push(world, "v2\n", env=env)
    r = _update(world)
    assert r.returncode == 1
    assert "テストが走っていません" in r.stdout
    assert _head(world) == old
    assert _docker_calls(world) == []


@needs_bash
def test_upstream_branch_deleted_is_an_error(world):
    _git(world["origin"], "checkout", "-q", "-b", "other")
    _git(world["origin"], "branch", "-q", "-D", "main")
    r = _update(world)
    assert r.returncode == 1
    assert "無くなりました" in r.stdout
    assert _docker_calls(world) == []


@needs_bash
def test_rolls_back_when_new_version_is_unhealthy(world):
    old = _head(world)
    new = _push(world, "v2\n")
    _rules(world, f"unhealthy {new[:7]}")
    r = _update(world)
    assert r.returncode == 1
    assert "元の版" in r.stdout and "戻しました" in r.stdout
    assert _head(world) == old
    # 動かしたのに healthy にならなかった版は、1回で「だめな版」
    assert _state(world, "bad") == new
    assert _state(world, "deploying") is None
    assert _state(world, "deployed").split()[0] == old
    # 起動しなかった版には、次の版が来るまで再挑戦しない
    _clear_calls(world)
    assert _update(world).returncode == 0
    assert _docker_calls(world) == []
    # 次の版は反映する
    newer = _push(world, "v3\n")
    assert _update(world).returncode == 0
    assert _head(world) == newer
    assert _state(world, "bad") is None


@needs_bash
def test_rollback_continues_when_state_cannot_be_written(world):
    """記録 (bad) が書けなくても、元の版に戻すことは最後までやる."""
    old = _head(world)
    new = _push(world, "v2\n")
    _rules(world, f"unhealthy {new[:7]}")
    (world["state"] / "bad").mkdir(parents=True)  # 書けない状態にする
    r = _update(world)
    assert r.returncode == 1
    assert "戻しました" in r.stdout
    assert _head(world) == old
    assert _docker_calls(world).count("compose up -d --remove-orphans") == 2


@needs_bash
def test_failed_rollback_is_not_repeated_forever(world):
    """元の版に戻しても動かないときは、MAX_TRIES 回まで戻し直して、あとはやめる."""
    old = _head(world)
    new = _push(world, "v2\n")
    _rules(world, "unhealthy any")
    r = _update(world)
    assert r.returncode == 1
    assert "戻しても起動しません (1/3 回目)" in r.stdout
    assert _head(world) == old
    assert _state(world, "bad") == new
    assert _state(world, "deploying") == f"{old} {new} 1"
    r = _update(world)
    assert r.returncode == 1
    assert "もう一度戻します" in r.stdout
    assert _state(world, "deploying") == f"{old} {new} 2"
    r = _update(world)
    assert r.returncode == 1
    assert "3 回試したのでやめます" in r.stdout
    assert _state(world, "deploying") is None
    assert _state(world, "bad") == new
    assert _state(world, "failures") is None  # 戻しのやり直しは、新しい版の失敗に数えない
    _clear_calls(world)
    assert _update(world).returncode == 0
    assert _docker_calls(world) == []


@needs_bash
def test_failed_rollback_is_retried_next_time(world):
    """戻しに失敗した (壊れた新しい版のコンテナが動いたまま) ら、次の回にもう一度戻す."""
    old = _head(world)
    new = _push(world, "v2\n")
    _rules(world, f"unhealthy {new[:7]}", f"buildfail {old[:7]}")
    r = _update(world)
    assert r.returncode == 1
    assert "次の回にもう一度戻してみます" in r.stdout
    assert _state(world, "bad") == new
    _rules(world, f"unhealthy {new[:7]}")  # 一時的な原因が消えた
    _clear_calls(world)
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "戻しました" in r.stdout
    assert _head(world) == old
    assert "compose up -d --remove-orphans" in _docker_calls(world)
    assert _state(world, "deploying") is None
    assert _state(world, "deployed").split()[0] == old
    assert _state(world, "bad") == new
    _clear_calls(world)
    assert _update(world).returncode == 0
    assert _docker_calls(world) == []


@needs_bash
@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="/proc のある Linux 向け")
def test_interrupted_rollback_keeps_bad_version(world):
    """だめな版から戻している途中で止まっても、その版をもう一度作り直さない."""
    old = _head(world)
    new = _push(world, "v2\n")
    _rules(world, f"unhealthy {new[:7]}", f"kill-build {old[:7]}")
    r = _update(world)
    assert r.returncode != 0
    assert _state(world, "bad") == new
    assert _state(world, "deploying") == f"{old} {new}"
    _rules(world, f"unhealthy {new[:7]}")
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "途中で止まっていた" in r.stdout
    assert _head(world) == old
    assert _state(world, "failures") is None
    assert _state(world, "bad") == new
    _clear_calls(world)
    assert _update(world).returncode == 0
    assert _docker_calls(world) == []


@needs_bash
def test_transient_build_failure_retries_then_gives_up(world):
    old = _head(world)
    new = _push(world, "v2\n")
    _rules(world, f"buildfail {new[:7]}")
    for n in (1, 2):
        _clear_calls(world)
        r = _update(world)
        assert r.returncode == 1
        assert "もう一度試します" in r.stdout
        assert _head(world) == old
        assert _state(world, "bad") is None
        assert _state(world, "failures") == f"{new} {n}"
        # 作れなかっただけなので、動いているコンテナには触らない
        assert not any(c.startswith("compose up") for c in _docker_calls(world))
    r = _update(world)
    assert r.returncode == 1
    assert "3 回続けて" in r.stdout
    assert _state(world, "bad") == new
    _clear_calls(world)
    assert _update(world).returncode == 0
    assert _docker_calls(world) == []


@needs_bash
def test_transient_failure_then_success(world):
    new = _push(world, "v2\n")
    _rules(world, f"upfail {new[:7]}")
    r = _update(world)
    assert r.returncode == 1
    assert "戻しました" in r.stdout
    assert _state(world, "bad") is None
    _rules(world)
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _head(world) == new
    assert _state(world, "failures") is None


@needs_bash
@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="/proc のある Linux 向け")
def test_interrupted_deploy_is_recovered(world):
    old = _head(world)
    new = _push(world, "v2\n")
    _rules(world, f"kill-build {new[:7]}")
    r = _update(world)
    assert r.returncode != 0
    # 途中で止まった: ファイルは新しい版、動いている版の記録は古いまま、途中の印が残る
    assert _head(world) == new
    assert _state(world, "deploying") == f"{old} {new}"
    _rules(world)
    _clear_calls(world)
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "途中で止まっていた" in r.stdout
    assert _head(world) == old
    assert "compose up -d --remove-orphans" in _docker_calls(world)
    assert _state(world, "deploying") is None
    assert _state(world, "failures") == f"{new} 1"
    # 次の回にやり直して反映する
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _head(world) == new
    assert _state(world, "deployed").split()[0] == new


@needs_bash
def test_transition_from_old_script_state(world):
    """古い自動更新係が残した状態 (短い番号の deployed・bad) から引き継げる."""
    head = _head(world)
    world["state"].mkdir()
    (world["state"] / "deployed").write_text(f"{head[:7]} 2026-10-01T00:00:00+09:00\n")
    # 古い版の設定がそのまま入っている
    world["units"].mkdir()
    (world["units"] / "zatsumu-auto-update.service").write_text("old\n")
    shutil.copy(HERE / "zatsumu-auto-update.timer", world["units"])
    world["bin"].parent.mkdir()
    world["bin"].write_text("old script\n")
    bad = _push(world, "v2\n")
    (world["state"] / "bad").write_text(bad + "\n")
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _docker_calls(world) == []  # だめな版のままなので反映しない
    assert _state(world, "deployed") == f"{head} 2026-10-01T00:00:00+09:00"
    # 新しい systemd の設定と自動更新係を自分で入れ、daemon-reload する
    assert (world["units"] / "zatsumu-auto-update.service").read_bytes() == \
        (HERE / "zatsumu-auto-update.service").read_bytes()
    assert world["bin"].read_bytes() == SCRIPT.read_bytes()
    assert os.access(world["bin"], os.X_OK)
    assert world["systemctl_log"].read_text().splitlines() == ["daemon-reload"]
    # 2回目は入れ替えるものが無いので何もしない
    _clear_calls(world)
    assert _update(world).returncode == 0
    assert not world["systemctl_log"].exists()


def _install_old_script(world):
    """本番に入っている古い自動更新係を、GitHub 役・サーバ役・入れ先に置く."""
    old_script = (ROOT / "tests" / "fixtures" / "zatsumu-auto-update.installed.sh").read_text(
        encoding="utf-8")
    # 本番の場所 (/etc/default, /usr/local/sbin) には触らせない
    old_script = old_script.replace("CONF=/etc/default/zatsumu-auto-update", "CONF=/dev/null")
    old_script = old_script.replace("/usr/local/sbin/zatsumu-auto-update", str(world["bin"]))
    # GitHub 役の最初の版を「古い係が入っている版」にする
    auto = world["origin"] / "deploy" / "auto-update"
    (auto / "zatsumu-auto-update.sh").write_text(old_script)
    (auto / "zatsumu-auto-update.service").write_text("old unit\n")
    _git(world["origin"], "commit", "-q", "-am", "old auto-update")
    _git(world["server"], "pull", "-q", "--ff-only")
    world["bin"].parent.mkdir()
    world["bin"].write_text(old_script)
    world["units"].mkdir()
    (world["units"] / "zatsumu-auto-update.service").write_text("old unit\n")
    # 新しい係を含む版を用意する (まだ push しない)
    shutil.copy(SCRIPT, auto / "zatsumu-auto-update.sh")
    shutil.copy(HERE / "zatsumu-auto-update.service", auto)
    return old_script, auto


@needs_bash
def test_transition_from_installed_old_script(world):
    """本番に入っている古い自動更新係が新しい版を反映し、次の回から新しい係が引き継ぐ."""
    _install_old_script(world)
    # 新しい係を含む版が届き、古い係がそれを反映して自分を入れ替える
    new = _push(world, "v2\n")
    r = _update(world, world["bin"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _head(world) == new
    assert world["bin"].read_bytes() == SCRIPT.read_bytes()
    assert _state(world, "deployed").split()[0] == new[:7]  # 古い係は短い番号で書く
    # 古い係は systemd の設定を入れ替えない
    assert (world["units"] / "zatsumu-auto-update.service").read_text() == "old unit\n"

    # 次の回 (新しい係): 作り直しはせず、systemd の設定を入れ替えて daemon-reload する
    _clear_calls(world)
    r = _update(world, world["bin"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _docker_calls(world) == []
    assert (world["units"] / "zatsumu-auto-update.service").read_bytes() == \
        (HERE / "zatsumu-auto-update.service").read_bytes()
    assert world["systemctl_log"].read_text().splitlines() == ["daemon-reload"]
    assert _state(world, "deployed").split()[0] == new
    # その次の版も、新しい係がふつうに反映する
    newer = _push(world, "v3\n")
    r = _update(world, world["bin"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _head(world) == newer


@needs_bash
@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="/proc のある Linux 向け")
def test_interrupted_handover_completes_when_script_changes_again(world):
    """古い係が入れ替えの版を反映する途中で止まると、古い係は自分では気づけない。
    次の版で zatsumu-auto-update.sh を少しでも変えれば、古い係がそれを見て入れ替える
    (DEPLOY.md の「古い自動更新係から入れ替わったかの確認」)."""
    old_script, auto = _install_old_script(world)
    new = _push(world, "v2\n")
    _rules(world, f"kill-up {new[:7]}")
    r = _update(world, world["bin"])
    assert r.returncode != 0
    assert _head(world) == new
    assert world["bin"].read_text() == old_script  # 入れ替わっていない
    _rules(world)
    _clear_calls(world)
    r = _update(world, world["bin"])
    assert r.returncode == 0
    assert _docker_calls(world) == []  # 古い係は「もう反映済み」と思っている
    # 自動更新係のファイルを変えた版を出すと、古い係が反映して入れ替える
    (auto / "zatsumu-auto-update.sh").write_text(SCRIPT.read_text(encoding="utf-8") + "\n")
    newer = _push(world, "v3\n")
    r = _update(world, world["bin"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _head(world) == newer
    assert world["bin"].read_text() == SCRIPT.read_text(encoding="utf-8") + "\n"
    # 次の回から新しい係: systemd の設定も入れ替わる
    r = _update(world, world["bin"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert (world["units"] / "zatsumu-auto-update.service").read_bytes() == \
        (HERE / "zatsumu-auto-update.service").read_bytes()
    assert world["systemctl_log"].read_text().splitlines() == ["daemon-reload"]


@needs_bash
def test_manual_git_pull_is_rolled_back_then_redeployed(world):
    """手で git pull した (印は無いが、ファイルだけ新しい版) ときは、いったん戻してから反映し直す."""
    old = _head(world)
    world["state"].mkdir()
    (world["state"] / "deployed").write_text(f"{old[:7]} 2026-10-01T00:00:00+09:00\n")
    new = _push(world, "v2\n")
    _git(world["server"], "pull", "-q", "--ff-only")
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "途中で止まっていた" in r.stdout
    assert _head(world) == old
    assert "compose build" in _docker_calls(world)
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _head(world) == new


@needs_bash
def test_env_file_collision_blocks_update(world):
    old = _head(world)
    env_file = world["server"] / ".env"
    env_file.write_text("SECRET=prod\n")
    _push(world, "SECRET=dummy\n", files=(".env",))
    r = _update(world)
    assert r.returncode == 1
    assert ".env" in r.stdout and "上書き" in r.stdout
    assert _head(world) == old
    assert env_file.read_text() == "SECRET=prod\n"
    assert _docker_calls(world) == []


@needs_bash
def test_env_file_renamed_in_is_blocked(world):
    """ほかのファイルを .env に名前を変えて持ち込む版も止める."""
    _push(world, "SECRET=dummy\n", files=("env.tmpl",))
    _git(world["server"], "pull", "-q", "--ff-only")
    assert _update(world).returncode == 0
    old = _head(world)
    env_file = world["server"] / ".env"
    env_file.write_text("SECRET=prod\n")
    _git(world["origin"], "rm", "-q", "env.tmpl")
    (world["origin"] / ".env").write_text("SECRET=dummy\n")
    _git(world["origin"], "add", "-f", ".env")
    _git(world["origin"], "commit", "-q", "-m", "rename")
    r = _update(world)
    assert r.returncode == 1
    assert ".env" in r.stdout and "上書き" in r.stdout
    assert _head(world) == old
    assert env_file.read_text() == "SECRET=prod\n"


@needs_bash
def test_files_left_by_interrupted_merge_are_cleaned_up(world):
    """git merge がファイルを書いた直後に止められて残った新しいファイルは、戻すときに消す."""
    old = _head(world)
    new = _push(world, "added\n", files=("app.txt", "newfile.txt"))
    world["state"].mkdir()
    (world["state"] / "deployed").write_text(f"{old} 2026-10-01T00:00:00+09:00\n")
    (world["state"] / "deploying").write_text(f"{old} {new}\n")
    _git(world["server"], "fetch", "-q")
    # 止められた git が書き出しただけのファイルと、サーバにだけある .env
    (world["server"] / "newfile.txt").write_text("added\n")
    (world["server"] / "other.txt").write_text("server only\n")
    (world["server"] / ".env").write_text("SECRET=prod\n")
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "途中で止まっていた" in r.stdout
    assert not (world["server"] / "newfile.txt").exists()
    assert (world["server"] / "other.txt").read_text() == "server only\n"
    assert (world["server"] / ".env").read_text() == "SECRET=prod\n"
    # 次の回は「サーバにだけあるファイル」に引っかからずに反映する
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _head(world) == new


@needs_bash
def test_files_with_other_content_are_not_removed_on_recovery(world):
    """中身が新しい版と違うファイルは、戻すときにも消さない."""
    old = _head(world)
    new = _push(world, "added\n", files=("newfile.txt",))
    world["state"].mkdir()
    (world["state"] / "deployed").write_text(f"{old} 2026-10-01T00:00:00+09:00\n")
    (world["state"] / "deploying").write_text(f"{old} {new}\n")
    _git(world["server"], "fetch", "-q")
    (world["server"] / "newfile.txt").write_text("someone else's\n")
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (world["server"] / "newfile.txt").read_text() == "someone else's\n"


@needs_bash
def test_env_values_win_over_conf_file(world, tmp_path):
    conf = tmp_path / "conf"
    conf.write_text(f"REPO_DIR={tmp_path / 'nowhere'}\nSTATE_DIR={tmp_path / 'nowhere'}\n")
    world["env"]["ZATSUMU_AUTO_UPDATE_CONF"] = str(conf)
    new = _push(world, "v2\n")
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _head(world) == new
    assert not (tmp_path / "nowhere").exists()


@needs_bash
def test_local_edits_block_update(world):
    old = _head(world)
    (world["server"] / "app.txt").write_text("edited on server\n")
    _push(world, "v2\n")
    r = _update(world)
    assert r.returncode == 1
    assert "書き換えた" in r.stdout
    assert _head(world) == old
    assert _docker_calls(world) == []
