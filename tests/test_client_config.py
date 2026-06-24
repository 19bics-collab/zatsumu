"""client.config の接続設定解決ロジックのテスト (GUI/ネットワーク不要)."""
import json

import pytest

from client import config


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """ユーザー設定・同梱設定・環境変数・CWD を毎テスト隔離する."""
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.delenv(config.ENV_SERVER, raising=False)
    monkeypatch.delenv(config.ENV_TOKEN, raising=False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return tmp_path


def _write_bundled(server):
    (config.Path.cwd() / "zatsumu_config.json").write_text(
        json.dumps({"server": server}), encoding="utf-8"
    )


def test_bundled_config_supplies_server():
    _write_bundled("https://bundled.example.com")
    cfg = config.load()
    assert cfg["server"] == "https://bundled.example.com"
    assert "token" not in cfg


def test_priority_cli_over_env_over_file(monkeypatch):
    _write_bundled("https://file.example.com")
    monkeypatch.setenv(config.ENV_SERVER, "https://env.example.com")
    # 環境変数はファイルより優先
    assert config.load()["server"] == "https://env.example.com"
    # CLI 引数は環境変数より優先
    assert config.load(server="https://cli.example.com")["server"] == (
        "https://cli.example.com"
    )


def test_env_supplies_token(monkeypatch):
    monkeypatch.setenv(config.ENV_TOKEN, "tok-from-env")
    assert config.load()["token"] == "tok-from-env"


def test_save_token_roundtrip_and_preserves_other_keys():
    config.save_token("my-secret-token")
    saved = json.loads(config.user_config_path().read_text(encoding="utf-8"))
    assert saved["token"] == "my-secret-token"
    # 既存のキーを壊さずトークンだけ更新する
    config.save_token("updated-token")
    saved = json.loads(config.user_config_path().read_text(encoding="utf-8"))
    assert saved["token"] == "updated-token"


def test_user_config_overrides_bundled_for_token():
    _write_bundled("https://bundled.example.com")
    config.save_token("user-token")
    cfg = config.load()
    assert cfg["server"] == "https://bundled.example.com"
    assert cfg["token"] == "user-token"


def test_resolve_returns_server_and_token():
    _write_bundled("https://bundled.example.com")
    config.save_token("user-token")
    server, token = config.resolve(allow_prompt=False)
    assert server == "https://bundled.example.com"
    assert token == "user-token"


def test_resolve_missing_token_raises_without_prompt():
    _write_bundled("https://bundled.example.com")
    with pytest.raises(SystemExit):
        config.resolve(allow_prompt=False)


def test_default_server_used_when_no_config():
    # 同梱config等が無くても、埋め込みの既定接続先で解決できる(exe単体配布用)
    config.save_token("user-token")
    server, token = config.resolve(allow_prompt=False)
    assert server == config.DEFAULT_SERVER
    assert token == "user-token"


def test_corrupt_config_files_are_ignored():
    (config.Path.cwd() / "zatsumu_config.json").write_text("{ not json", "utf-8")
    config.user_config_path().parent.mkdir(parents=True, exist_ok=True)
    config.user_config_path().write_text("also broken", "utf-8")
    # 壊れたファイルは無視され、既定接続先のみが残る (例外を投げない)
    assert config.load() == {"server": config.DEFAULT_SERVER}
