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


def _run(name, conclusion="success", status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion}


def test_ci_status_evaluate():
    ev = _load_ci_status().evaluate
    assert ev({"check_runs": [_run("a"), _run("b", "skipped")]}) == "success"
    assert ev({"check_runs": []}) == "pending"  # まだテストが始まっていない
    assert ev({"check_runs": [_run("a"), _run("b", None, "in_progress")]}) == "pending"
    # 片方が走っている途中でも、もう片方が落ちていれば反映しない
    assert ev({"check_runs": [_run("a", "failure"), _run("b", None, "queued")]}) == "failure"
    assert ev({"check_runs": [_run("a", "cancelled")]}) == "failure"


@needs_bash
@pytest.mark.parametrize("name", ["zatsumu-auto-update.sh", "install.sh"])
def test_scripts_parse(name):
    subprocess.run(["bash", "-n", str(HERE / name)], check=True)


def test_units_point_to_installed_script():
    service = (HERE / "zatsumu-auto-update.service").read_text(encoding="utf-8")
    install = (HERE / "install.sh").read_text(encoding="utf-8")
    assert "ExecStart=/usr/local/sbin/zatsumu-auto-update" in service
    assert "/usr/local/sbin/zatsumu-auto-update" in install
    assert "zatsumu-auto-update.timer" in install


# ---------- 通しの確認 (偽の docker / curl) ----------

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "compose ps") echo cid123 ;;
  "inspect -f") cat "$FAKE_HEALTH" ;;
esac
"""

FAKE_CURL = """#!/usr/bin/env bash
cat "$FAKE_CI"
"""


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """GitHub 役 (origin) と、それを clone したサーバ役 (server) を用意する."""
    for k in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{k}_NAME", "t")
        monkeypatch.setenv(f"{k}_EMAIL", "t@example.com")
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "deploy" / "auto-update").mkdir(parents=True)
    shutil.copy(HERE / "ci_status.py", origin / "deploy" / "auto-update")
    (origin / "app.txt").write_text("v1\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "v1")
    server = tmp_path / "server"
    _git(tmp_path, "clone", "-q", str(origin), str(server))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
    w = {
        "origin": origin, "server": server, "state": tmp_path / "state",
        "log": tmp_path / "docker.log", "health": tmp_path / "health",
        "ci": tmp_path / "ci.json",
    }
    w["health"].write_text("healthy")
    w["ci"].write_text(json.dumps({"check_runs": [_run("pytest")]}))
    w["env"] = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "REPO_DIR": str(server), "STATE_DIR": str(w["state"]),
        "HEALTH_TIMEOUT": "1", "HEALTH_INTERVAL": "0.1",
        "FAKE_LOG": str(w["log"]), "FAKE_HEALTH": str(w["health"]), "FAKE_CI": str(w["ci"]),
    }
    return w


def _push(world, text, files=("app.txt",)):
    for f in files:
        p = world["origin"] / f
        p.write_text(text)
    _git(world["origin"], "add", "-A")
    _git(world["origin"], "commit", "-q", "-m", text)
    return _git(world["origin"], "rev-parse", "HEAD")


def _update(world):
    return subprocess.run(["bash", str(SCRIPT)], env=world["env"], capture_output=True,
                          text=True)


def _docker_calls(world):
    return world["log"].read_text().splitlines() if world["log"].exists() else []


@needs_bash
def test_nothing_new_does_nothing(world):
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _docker_calls(world) == []


@needs_bash
def test_deploys_new_version_after_ci_passes(world):
    new = _push(world, "v2\n")
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _git(world["server"], "rev-parse", "HEAD") == new
    calls = _docker_calls(world)
    assert "compose up -d --build" in calls
    # Caddyfile が変わっていなければ Caddy は作り直さない
    assert not any("force-recreate caddy" in c for c in calls)
    assert (world["state"] / "deployed").read_text().startswith(new[:7])


@needs_bash
def test_caddyfile_change_recreates_caddy(world):
    _push(world, "site {}\n", files=("Caddyfile.mail",))
    r = _update(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "compose up -d --force-recreate caddy" in _docker_calls(world)


@pytest.mark.parametrize("ci, word", [
    ({"check_runs": [_run("pytest", "failure")]}, "失敗"),
    ({"check_runs": [_run("pytest", None, "in_progress")]}, "まだ終わっていない"),
])
@needs_bash
def test_waits_or_skips_unless_ci_passed(world, ci, word):
    old = _git(world["server"], "rev-parse", "HEAD")
    world["ci"].write_text(json.dumps(ci))
    _push(world, "v2\n")
    r = _update(world)
    assert r.returncode == 0
    assert word in r.stdout
    assert _git(world["server"], "rev-parse", "HEAD") == old
    assert _docker_calls(world) == []


@needs_bash
def test_rolls_back_when_new_version_is_unhealthy(world):
    old = _git(world["server"], "rev-parse", "HEAD")
    world["health"].write_text("unhealthy")
    new = _push(world, "v2\n")
    r = _update(world)
    assert r.returncode == 1
    assert "元の版" in r.stdout
    assert _git(world["server"], "rev-parse", "HEAD") == old
    assert (world["state"] / "bad").read_text().strip() == new
    # 起動しなかった版には、次の版が来るまで再挑戦しない
    world["log"].unlink()
    assert _update(world).returncode == 0
    assert _docker_calls(world) == []


@needs_bash
def test_local_edits_block_update(world):
    old = _git(world["server"], "rev-parse", "HEAD")
    (world["server"] / "app.txt").write_text("edited on server\n")
    _push(world, "v2\n")
    r = _update(world)
    assert r.returncode == 1
    assert "書き換えた" in r.stdout
    assert _git(world["server"], "rev-parse", "HEAD") == old
    assert _docker_calls(world) == []
