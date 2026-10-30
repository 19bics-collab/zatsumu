"""返信に付けるファイル (返信画面の「📎 ファイルを添付」) のテスト.

- 付ける (アップロード)・一覧・確かめる (ダウンロード)・外す。保存先のパスに元の名前を使わない
- 付けられる人: 管理者・メール担当者 (返信文の下書きと同じ。一般メンバーは 403、ほかのメールの
  ファイルは 404)。返信した後と、メール担当者の確認待ちの間は 409
- 上限: 1通に10個・合わせて13MB (メールで送っても 20MB に収まる)・中身の空のファイル。大きなファイルは
  受け取りの途中でやめる (丸ごとメモリに読まない)。DB に入れるときにロックを待っても、ほかの画面を止めない
- ファイル名を整える (フォルダの部分・制御文字・文字の向きを変える記号・Windows で使えない文字。日本語はそのまま)
- 確認待ち: 画面を開いた後に添付が変わっていたら「承認して送信」も 409 (見ていないファイルを送らない)
- 送信: 本文 + 添付の multipart/mixed。日本語の名前は RFC 2231 と =?utf-8?b?…?= の両方で書く。
  本文だけの返信は今までどおり text/plain の1つの部品。控え (送信済みフォルダ) も同じもの。
  送れたら送った時刻を入れ、送れなかったら付けたまま。送信サーバに大きすぎると断られたら日本語で知らせる。
  宛先に届かず CC にだけ届いたときは、届いたファイルを送信箱に残す
- 保存日数でメールを消すとファイルも消える・持ち主のいないファイルの片付け・DB の作り直し
- 画面 (ボタン・名前は esc()・送る前の確認・やり取りの 📎)

ネットワークには出ない (送信・控えの保存は偽物に差し替える)。
"""
import asyncio
import email
import email.header
import email.policy
import os
import re
import shutil
import sqlite3
import subprocess
import time
import tracemalloc

import pytest
from fastapi.testclient import TestClient

from tests.test_api import _add_mail, auth

PDF = b"%PDF-1.4\n" + bytes(range(256)) * 4
JPG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + os.urandom(3000)
HEX_NAME = re.compile(r"[0-9a-f]{32}")


@pytest.fixture(autouse=True)
def reset_tz():
    from server import tz
    tz.set_tz("Asia/Tokyo")
    yield
    tz.set_tz("Asia/Tokyo")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ZATSUMU_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ZATSUMU_LOGIN_VERIFY_EMAIL", raising=False)
    import importlib
    from server import app as app_module
    importlib.reload(app_module)
    return TestClient(app_module.app)


@pytest.fixture()
def users(client, tmp_path):
    """一般メンバー・メール担当者・管理者 (2人)."""
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        worker = db.create_user(conn, "tanaka")
        staff = db.create_user(conn, "田中")
        conn.execute("UPDATE users SET mail_role = 'staff' WHERE id = ?", (staff["id"],))
        admin = db.create_user(conn, "黒須", is_admin=True)
        admin2 = db.create_user(conn, "佐藤", is_admin=True)
    return worker, staff, admin, admin2


@pytest.fixture()
def notices(monkeypatch):
    """確認のお願いのお知らせを送らない."""
    from server import notify
    monkeypatch.setattr(notify, "deliver_async", lambda *a, **kw: None)


@pytest.fixture()
def smtp(client, users, monkeypatch):
    """送信 (notify.smtp_send) と控えの保存 (save_to_sent) を偽物にして、送ったものを覚える."""
    from server import mail, notify
    admin = users[2]
    r = client.patch("/api/settings", headers=auth(admin),
                     json={"smtp_host": "smtp.example.com", "mail_from": "info@bics.example",
                           "imap_host": "imap.example.com"})
    assert r.status_code == 200, r.text
    box = {"sent": [], "saved": [], "fail": False}

    def fake_send(s, sender, rcpts, message, **kw):
        if box["fail"]:
            raise OSError("smtp down")
        box["sent"].append({"rcpts": rcpts, "msg": message, "kw": kw})
        return {}

    monkeypatch.setattr(notify, "smtp_send", fake_send)
    monkeypatch.setattr(mail, "save_to_sent", lambda s, raw: box["saved"].append(raw) or "Sent")
    return box


def _db(tmp_path):
    from server import db
    return db.get_db(tmp_path / "zatsumu.db")


def _files(tmp_path, mid):
    with _db(tmp_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM mail_reply_files WHERE mail_id = ? ORDER BY id", (mid,))]


def _audits(tmp_path, action):
    with _db(tmp_path) as conn:
        return [r["detail"] for r in conn.execute(
            "SELECT detail FROM audit_log WHERE action = ? ORDER BY id", (action,))]


def _upload(client, user, mid, *files, seen=None):
    """files は (名前, 中身, 種類)。seen は画面を開いたときの確認のお願いの時刻."""
    params = {"seen_request": seen} if seen is not None else {}
    return client.post(f"/api/mail/{mid}/reply-files", headers=auth(user), params=params,
                       files=[("files", f) for f in files])


def _detail(client, user, mid):
    return client.get(f"/api/mail/{mid}", headers=auth(user)).json()


def _on_disk(tmp_path, mid=None):
    base = tmp_path / "mail_reply_files"
    if not base.exists():
        return []
    root = base / str(mid) if mid is not None else base
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


# ---------- 付ける・一覧・確かめる・外す ----------

def test_upload_list_download_and_remove(client, users, tmp_path):
    _, _, admin, _ = users
    mid = _add_mail(tmp_path)
    r = _upload(client, admin, mid, ("見積書（10月分）.pdf", PDF, "application/pdf"),
                ("IMG_0001.JPG", JPG, "image/jpeg"))
    assert r.status_code == 200, r.text
    files = r.json()["files"]
    assert [(f["filename"], f["content_type"], f["size"], f["uploaded_by"], f["sent"])
            for f in files] == [("見積書（10月分）.pdf", "application/pdf", len(PDF), "黒須", False),
                                ("IMG_0001.JPG", "image/jpeg", len(JPG), "黒須", False)]
    # 返信画面の詳細にも出る
    assert _detail(client, admin, mid)["reply_files"] == files
    # 保存先はメールの番号のフォルダの、でたらめな英数字の名前 (元の名前は使わない)
    disk = _on_disk(tmp_path, mid)
    assert len(disk) == 2 and all(HEX_NAME.fullmatch(p.name) for p in disk)
    assert sorted(p.read_bytes() for p in disk) == sorted([PDF, JPG])
    assert all(re.fullmatch(rf"{mid}/[0-9a-f]{{32}}", row["path"]) for row in _files(tmp_path, mid))
    # 確かめる: いつも「ただのデータ」のダウンロード (画面の中では開かない)
    pdf_id = files[0]["id"]
    d = client.get(f"/api/mail/{mid}/reply-files/{pdf_id}", headers=auth(admin))
    assert d.status_code == 200 and d.content == PDF
    assert d.headers["content-type"] == "application/octet-stream"
    assert d.headers["x-content-type-options"] == "nosniff"
    assert d.headers["content-disposition"].startswith("attachment;")
    assert "filename*=UTF-8''%E8%A6%8B%E7%A9%8D%E6%9B%B8" in d.headers["content-disposition"]
    assert "no-store" in d.headers["cache-control"]
    # 外す (サーバからも消える)
    r = client.delete(f"/api/mail/{mid}/reply-files/{pdf_id}", headers=auth(admin))
    assert r.status_code == 200
    assert [f["filename"] for f in r.json()["files"]] == ["IMG_0001.JPG"]
    assert [p.read_bytes() for p in _on_disk(tmp_path, mid)] == [JPG]
    assert client.delete(f"/api/mail/{mid}/reply-files/{pdf_id}",
                         headers=auth(admin)).status_code == 404
    assert client.get(f"/api/mail/{mid}/reply-files/{pdf_id}",
                      headers=auth(admin)).status_code == 404


def test_same_names_get_numbers(client, users, tmp_path):
    """iPhone で写真を何枚か選ぶと、どれも同じ名前のことがある → 「名前 (2).jpg」にする."""
    _, _, admin, _ = users
    mid = _add_mail(tmp_path)
    r = _upload(client, admin, mid, ("image.jpg", JPG, "image/jpeg"),
                ("image.jpg", JPG, "image/jpeg"))
    assert [f["filename"] for f in r.json()["files"]] == ["image.jpg", "image (2).jpg"]
    r = _upload(client, admin, mid, ("IMAGE.jpg", JPG, "image/jpeg"))
    assert [f["filename"] for f in r.json()["files"]][-1] == "IMAGE (3).jpg"


def test_content_type_from_browser_or_name(client, users, tmp_path):
    _, _, admin, _ = users
    mid = _add_mail(tmp_path)
    r = _upload(client, admin, mid,
                ("料金表.xlsx", b"PK\x03\x04data", "application/octet-stream"),
                ("メモ", b"hello", "text/plain"),
                ("転送.eml", b"From: a@b\r\n\r\nx", "message/rfc822"),
                ("謎のファイル", b"\x00\x01", ""))
    assert [(f["filename"], f["content_type"]) for f in r.json()["files"]] == [
        ("料金表.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("メモ", "text/plain"),
        # message/* は base64 で送ってはいけない決まりの種類なので「ただのデータ」として送る
        ("転送.eml", "application/octet-stream"),
        ("謎のファイル", "application/octet-stream")]


def test_filenames_are_cleaned_and_paths_are_not_used(client, users, tmp_path):
    from server import reply_files
    _, _, admin, _ = users
    mid = _add_mail(tmp_path)
    r = _upload(client, admin, mid, ("../../../evil.sh", b"#!/bin/sh", "text/plain"),
                ("C:\\Users\\kurosu\\Desktop\\見積書.pdf", PDF, "application/pdf"),
                ("a:b*c?d<e>f|.txt", b"x", "text/plain"))
    assert r.status_code == 200, r.text
    # Windows でファイル名に使えない文字は「_」にする (相手が Outlook などで保存するときに困らないよう)
    assert [f["filename"] for f in r.json()["files"]] == ["evil.sh", "見積書.pdf", "a_b_c_d_e_f_.txt"]
    # データフォルダの外にも、メールのフォルダの外にも書かない
    assert not (tmp_path / "evil.sh").exists()
    assert not any(p.name == "evil.sh" for p in tmp_path.rglob("*"))
    assert all(p.parent.name == str(mid) and HEX_NAME.fullmatch(p.name)
               for p in _on_disk(tmp_path))
    # 改行などの制御文字・文字の向きを変える記号を除く。日本語はそのまま。長い名前は拡張子を残して縮める
    assert reply_files.clean_name("請求書\u202efdp.exe") == "請求書fdp.exe"
    assert reply_files.clean_name("a\r\nb\tc.txt") == "abc.txt"
    assert reply_files.clean_name('「見積"書".pdf') == "「見積_書_.pdf"
    # 空白 (全角の空白も) は1つの半角の空白にまとめる (受信の添付と同じ)
    assert reply_files.clean_name("ﾃｽﾄ　　写真（１）.jpeg") == "ﾃｽﾄ 写真（１）.jpeg"
    long = reply_files.clean_name("長" * 300 + ".pdf")
    assert len(long) <= 150 and long.endswith(".pdf")
    assert reply_files.clean_name("", "image/jpeg") == "添付ファイル.jpg"
    assert reply_files.clean_name("  ..  ") == "添付ファイル"


# ---------- 上限 ----------

def test_empty_file_is_refused(client, users, tmp_path):
    _, _, admin, _ = users
    mid = _add_mail(tmp_path)
    r = _upload(client, admin, mid, ("空.txt", b"", "text/plain"))
    assert r.status_code == 400 and "空" in r.json()["detail"]
    assert _files(tmp_path, mid) == [] and _on_disk(tmp_path) == []
    # ファイルの欄が無い・ファイルでない送り方も 400
    r = client.post(f"/api/mail/{mid}/reply-files", headers=auth(admin), data={"x": "1"})
    assert r.status_code == 400 and "届きませんでした" in r.json()["detail"]
    r = client.post(f"/api/mail/{mid}/reply-files", headers=auth(admin), json={"x": 1})
    assert r.status_code == 400


def test_at_most_ten_files(client, users, tmp_path):
    _, _, admin, _ = users
    mid = _add_mail(tmp_path)
    # 1回で11個は全部断る (途中まで付けたりしない)
    r = _upload(client, admin, mid, *[(f"{i}.txt", b"x", "text/plain") for i in range(11)])
    assert r.status_code == 400 and "10個まで" in r.json()["detail"]
    assert _files(tmp_path, mid) == [] and _on_disk(tmp_path) == []
    r = _upload(client, admin, mid, *[(f"{i}.txt", b"x", "text/plain") for i in range(10)])
    assert r.status_code == 200 and len(r.json()["files"]) == 10
    r = _upload(client, admin, mid, ("11.txt", b"x", "text/plain"))
    assert r.status_code == 400 and "10個まで" in r.json()["detail"]
    assert len(_files(tmp_path, mid)) == 10 and len(_on_disk(tmp_path)) == 10


def test_total_size_limit(client, users, tmp_path):
    from server import reply_files
    _, _, admin, _ = users
    mid = _add_mail(tmp_path)
    assert reply_files.TOTAL_MAX_BYTES == 13 * 1024 * 1024
    big = b"a" * (10 * 1024 * 1024)
    assert _upload(client, admin, mid, ("1.bin", big, "application/octet-stream")).status_code == 200
    r = _upload(client, admin, mid, ("2.bin", b"b" * (4 * 1024 * 1024), "application/octet-stream"))
    assert r.status_code == 413
    assert "13MBまで" in r.json()["detail"] and "小さくして" in r.json()["detail"]
    assert len(_files(tmp_path, mid)) == 1 and len(_on_disk(tmp_path)) == 1
    # ちょうど残りの分までは付けられる
    rest = reply_files.TOTAL_MAX_BYTES - len(big)
    r = _upload(client, admin, mid, ("3.bin", b"c" * rest, "application/octet-stream"))
    assert r.status_code == 200 and len(r.json()["files"]) == 2


def test_huge_upload_is_refused_before_reading(client, users, tmp_path):
    """大きさ (Content-Length) が先に分かるときは、読まずに断る."""
    _, _, admin, _ = users
    mid = _add_mail(tmp_path)
    r = _upload(client, admin, mid, ("動画.mov", b"v" * (20 * 1024 * 1024), "video/quicktime"))
    assert r.status_code == 413 and "13MB" in r.json()["detail"]
    assert _on_disk(tmp_path) == []


def _multipart_stream(boundary: bytes, parts: list, chunk: int = 64 * 1024, endless=False):
    """multipart/form-data を少しずつ返す (送る側のふり)。読まれた数を counter[0] に数える."""
    counter = [0]

    async def gen():
        for name, size in parts:
            head = (b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"files\";"
                    b" filename=\"" + name.encode() + b"\"\r\nContent-Type: application/octet-stream\r\n\r\n")
            counter[0] += 1
            yield head
            sent = 0
            while endless or sent < size:
                counter[0] += 1
                yield b"z" * chunk
                sent += chunk
            yield b"\r\n"
        yield b"--" + boundary + b"--\r\n"

    return gen(), counter


def test_huge_file_is_cut_off_while_streaming(tmp_path):
    """大きさを知らせずに送り続けても、上限を超えたところで読むのをやめる (残りは読まない)."""
    from server import reply_files
    headers = {"content-type": "multipart/form-data; boundary=XyZ"}
    stream, counter = _multipart_stream(b"XyZ", [("big.bin", 0)], endless=True)
    folder = tmp_path / "rf" / "1"
    with pytest.raises(reply_files.UploadError) as e:
        asyncio.run(reply_files.receive(headers, stream, folder, 10, 1024 * 1024))
    assert e.value.status == 413
    # 1MB + 少しだけ読んで止めた (送り続けられた分を全部は読まない)
    assert counter[0] <= 1024 * 1024 // (64 * 1024) + 3
    # 書きかけのファイルは残さない
    assert not folder.exists() or list(folder.iterdir()) == []


def test_receiving_does_not_hold_the_file_in_memory(tmp_path):
    """12MB のファイルを受け取っても、メモリに丸ごとは持たない (届いた分からファイルに書く)."""
    from server import reply_files
    headers = {"content-type": "multipart/form-data; boundary=XyZ"}
    stream, _ = _multipart_stream(b"XyZ", [("12mb.bin", 12 * 1024 * 1024)])
    tracemalloc.start()
    try:
        items = asyncio.run(reply_files.receive(headers, stream, tmp_path / "rf" / "1", 10,
                                                15 * 1024 * 1024))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert [(i["filename"], i["size"]) for i in items] == [("12mb.bin", 12 * 1024 * 1024)]
    assert items[0]["path"].stat().st_size == 12 * 1024 * 1024
    assert peak < 3 * 1024 * 1024, peak


def test_broken_upload_leaves_nothing(tmp_path):
    """途中で切れた送り方 (最後のファイルの途中で終わった) は受け取らず、書いた分も消す."""
    from server import reply_files

    async def cut():
        yield (b"--XyZ\r\nContent-Disposition: form-data; name=\"files\"; filename=\"a.txt\"\r\n\r\n"
               b"hello")

    folder = tmp_path / "rf" / "1"
    with pytest.raises(reply_files.UploadError) as e:
        asyncio.run(reply_files.receive({"content-type": "multipart/form-data; boundary=XyZ"},
                                        cut(), folder, 10, 1024))
    assert e.value.status == 400
    assert not folder.exists() or list(folder.iterdir()) == []


def _hold_db_lock(db_path):
    """ほかの書き込みのふり (書き込みのロックを取ったまま、release が来るまで離さない)."""
    import threading
    locked, release = threading.Event(), threading.Event()

    def hold():
        other = sqlite3.connect(db_path)
        other.execute("BEGIN IMMEDIATE")
        locked.set()
        release.wait(30)
        other.rollback()
        other.close()

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert locked.wait(5)
    return release, t


def test_waiting_for_the_db_lock_does_not_stop_the_server(client, users, tmp_path, monkeypatch):
    """受け取ったファイルを DB に入れるとき、ほかの書き込みのロックを待っても、その間ほかの画面
    (ほかの要求) は止めない (待つのは別のスレッド)。ロックが空いたら付けられる."""
    import httpx
    from server import app as app_module, reply_files
    _, _, admin, _ = users
    mid = _add_mail(tmp_path)
    held = {}
    real_receive = reply_files.receive

    async def receive_then_lock(*a, **kw):
        items = await real_receive(*a, **kw)
        # 受け取った後、DB に入れる前に、ほかの書き込みが始まった
        held["release"], held["thread"] = _hold_db_lock(tmp_path / "zatsumu.db")
        return items

    monkeypatch.setattr(reply_files, "receive", receive_then_lock)

    async def scenario():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            up = asyncio.create_task(ac.post(
                f"/api/mail/{mid}/reply-files", headers=auth(admin),
                files=[("files", ("a.pdf", PDF, "application/pdf"))]))
            while "release" not in held:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.3)        # 付ける方は、いまロックを待っている
            health = await asyncio.wait_for(ac.get("/healthz"), 3)
            waiting = not up.done()
            held["release"].set()
            return health, waiting, await asyncio.wait_for(up, 10)

    try:
        health, waiting, r = asyncio.run(scenario())
    finally:
        if "release" in held:
            held["release"].set()
            held["thread"].join(5)
    assert health.status_code == 200
    # ほかの要求に答えたとき、付ける方はまだロックを待っていた (サーバ全体を止めていない)
    assert waiting
    assert r.status_code == 200, r.text
    assert [f["filename"] for f in r.json()["files"]] == ["a.pdf"]
    assert len(_files(tmp_path, mid)) == 1 and len(_on_disk(tmp_path, mid)) == 1


def test_db_stays_locked_gives_503_and_removes_the_file(client, users, tmp_path, monkeypatch):
    """ほかの書き込みが長く続いて DB に入れられないときは、日本語の 503 (もう一度付けてもらう)。
    受け取ったファイルは残さない."""
    from server import db, reply_files
    _, _, admin, _ = users
    mid = _add_mail(tmp_path)
    held = {}
    real_receive, real_connect = reply_files.receive, db.connect

    async def receive_then_lock(*a, **kw):
        items = await real_receive(*a, **kw)
        held["release"], held["thread"] = _hold_db_lock(tmp_path / "zatsumu.db")
        return items

    def quick_connect(*a, **kw):
        # ロックを待つ時間 (ふだんは5秒) をテストでは短くする
        conn = real_connect(*a, **kw)
        conn.execute("PRAGMA busy_timeout = 200")
        return conn

    monkeypatch.setattr(reply_files, "receive", receive_then_lock)
    monkeypatch.setattr(db, "connect", quick_connect)
    try:
        r = _upload(client, admin, mid, ("見積書.pdf", PDF, "application/pdf"))
    finally:
        if "release" in held:
            held["release"].set()
            held["thread"].join(5)
    assert r.status_code == 503
    assert r.json()["detail"] == reply_files.DB_BUSY and "もう一度付けて" in r.json()["detail"]
    assert _files(tmp_path, mid) == [] and _on_disk(tmp_path) == []
    # ロックが空けば、もう一度付けられる
    monkeypatch.setattr(reply_files, "receive", real_receive)
    r = _upload(client, admin, mid, ("見積書.pdf", PDF, "application/pdf"))
    assert r.status_code == 200 and len(_on_disk(tmp_path, mid)) == 1


# ---------- 付けられる人 ----------

def test_who_can_attach(client, users, tmp_path):
    worker, staff, admin, _ = users
    mid = _add_mail(tmp_path, n=1)
    other = _add_mail(tmp_path, n=2)
    # 一般メンバー・トークンなしは使えない
    assert _upload(client, worker, mid, ("a.txt", b"x", "text/plain")).status_code == 403
    assert client.post(f"/api/mail/{mid}/reply-files",
                       files=[("files", ("a.txt", b"x", "text/plain"))]).status_code == 401
    # メール担当者は付けられる (下書きと同じ)
    r = _upload(client, staff, mid, ("田中の写真.jpg", JPG, "image/jpeg"))
    assert r.status_code == 200 and r.json()["files"][0]["uploaded_by"] == "田中"
    fid = r.json()["files"][0]["id"]
    for user in (worker,):
        assert client.get(f"/api/mail/{mid}/reply-files/{fid}", headers=auth(user)).status_code == 403
        assert client.delete(f"/api/mail/{mid}/reply-files/{fid}", headers=auth(user)).status_code == 403
    # ほかのメールの番号では見られない・外せない
    assert client.get(f"/api/mail/{other}/reply-files/{fid}", headers=auth(admin)).status_code == 404
    assert client.delete(f"/api/mail/{other}/reply-files/{fid}", headers=auth(admin)).status_code == 404
    assert client.get(f"/api/mail/{mid}/reply-files/{fid}", headers=auth(staff)).content == JPG
    # 無いメールには付けられない
    assert _upload(client, admin, 9999, ("a.txt", b"x", "text/plain")).status_code == 404
    assert len(_files(tmp_path, mid)) == 1 and _files(tmp_path, other) == []


def test_pending_approval_locks_staff_but_not_admin(client, users, tmp_path, notices):
    _, staff, admin, _ = users
    mid = _add_mail(tmp_path)
    fid = _upload(client, staff, mid, ("a.pdf", PDF, "application/pdf")).json()["files"][0]["id"]
    seen = _detail(client, admin, mid)["requested_at"] or ""
    r = client.post(f"/api/mail/{mid}/request-approval", headers=auth(staff),
                    json={"body": "本文", "reply_files": [fid]})
    assert r.status_code == 200, r.text
    requested = r.json()["requested_at"]
    # 確認待ちの間、メール担当者は付けられない・外せない (下書きと同じ)
    r = _upload(client, staff, mid, ("b.pdf", PDF, "application/pdf"), seen=requested)
    assert r.status_code == 409 and "確認待ちの間は、添付ファイルを変えられません" in r.json()["detail"]
    r = client.delete(f"/api/mail/{mid}/reply-files/{fid}", headers=auth(staff))
    assert r.status_code == 409
    # 確認をお願いされる前に開いた管理者の画面からは変えない (下書きと同じ seen_request)
    r = _upload(client, admin, mid, ("c.pdf", PDF, "application/pdf"), seen=seen)
    assert r.status_code == 409 and "確認をお願いしました" in r.json()["detail"]
    r = client.delete(f"/api/mail/{mid}/reply-files/{fid}", headers=auth(admin),
                      params={"seen_request": seen})
    assert r.status_code == 409
    # 確認のお願いを見ている管理者は変えられる
    r = _upload(client, admin, mid, ("d.pdf", PDF, "application/pdf"), seen=requested)
    assert r.status_code == 200 and len(r.json()["files"]) == 2
    assert len(_on_disk(tmp_path)) == 2
    # 差し戻すと、メール担当者がまた変えられる
    assert client.post(f"/api/mail/{mid}/send-back", headers=auth(admin),
                       json={"note": "写真を足してください"}).status_code == 200
    r = _upload(client, staff, mid, ("e.jpg", JPG, "image/jpeg"), seen=requested)
    assert r.status_code == 200 and len(r.json()["files"]) == 3


def test_request_approval_checks_the_files_on_screen(client, users, tmp_path, notices):
    """「確認をお願いする」も、画面に出ていたファイルと同じときだけ (違えば 409)."""
    _, staff, admin, _ = users
    mid = _add_mail(tmp_path)
    a = _upload(client, staff, mid, ("a.pdf", PDF, "application/pdf")).json()["files"][0]["id"]
    # ほかの画面で付けた (この画面には出ていない)
    _upload(client, staff, mid, ("b.pdf", PDF, "application/pdf"))
    for seen in ([a], None):   # None = 添付を知らない古い画面 (何も付いていないとみなす)
        body = {"body": "本文"} if seen is None else {"body": "本文", "reply_files": seen}
        r = client.post(f"/api/mail/{mid}/request-approval", headers=auth(staff), json=body)
        assert r.status_code == 409 and "添付ファイルが、この画面を開いた後に" in r.json()["detail"]
    assert _detail(client, admin, mid)["status"] == "unhandled"
    ids = [f["id"] for f in _detail(client, staff, mid)["reply_files"]]
    r = client.post(f"/api/mail/{mid}/request-approval", headers=auth(staff),
                    json={"body": "本文", "reply_files": ids})
    assert r.status_code == 200
    (detail,) = _audits(tmp_path, "mail_approval_request")
    assert "attachments=2(a.pdf, b.pdf)" in detail


# ---------- 送信 ----------

def test_send_with_attachments(client, users, tmp_path, smtp):
    from server import mail
    _, _, admin, _ = users
    mid = _add_mail(tmp_path, subject="清掃のお見積り")
    files = _upload(client, admin, mid, ("見積書_2026年10月分_清掃とリネンの料金表.pdf", PDF, "application/pdf"),
                    ("IMG_0001.jpg", JPG, "image/jpeg")).json()["files"]
    ids = [f["id"] for f in files]
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "お見積りをお送りします。", "reply_files": ids})
    assert r.status_code == 200, r.text
    assert [f["filename"] for f in r.json()["reply_files"]] == [f["filename"] for f in files]
    assert all(f["sent"] for f in r.json()["reply_files"])
    (sent,) = smtp["sent"]
    # 大きなメールを送り切れるよう、待ち時間を長くする
    assert sent["kw"] == {"timeout": mail.SMTP_ATTACHMENT_TIMEOUT}
    raw = sent["msg"].encode("utf-8")
    # 控え (送信済みフォルダ) にも同じもの (添付も入る)
    assert smtp["saved"] == [raw]
    # メールの1行は78文字まで
    assert max(len(line) for line in sent["msg"].splitlines()) <= 78
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert msg.get_content_type() == "multipart/mixed"
    assert msg["To"] == "taro@example.com" and msg["In-Reply-To"] == "<m1@example.com>"
    parts = list(msg.iter_parts())
    assert [p.get_content_type() for p in parts] == ["text/plain", "application/pdf", "image/jpeg"]
    assert parts[0].get_content().strip() == "お見積りをお送りします。"
    assert parts[0].get_content_disposition() is None
    assert [p.get_filename() for p in parts[1:]] == ["見積書_2026年10月分_清掃とリネンの料金表.pdf",
                                                    "IMG_0001.jpg"]
    assert [p.get_content_disposition() for p in parts[1:]] == ["attachment", "attachment"]
    assert parts[1].get_content() == PDF and parts[2].get_content() == JPG
    # 日本語の名前は RFC 2231 (filename*=utf-8''…) と、Outlook などが読む =?utf-8?b?…?= の両方で書く
    pdf_head = sent["msg"][sent["msg"].index("Content-Type: application/pdf"):]
    pdf_head = pdf_head[:pdf_head.index("\n\n")]
    assert "filename*0*=utf-8''%E8%A6%8B%E7%A9%8D%E6%9B%B8" in pdf_head
    assert 'filename="=?utf-8?b?' in pdf_head and 'name="=?utf-8?b?' in pdf_head
    # =?utf-8?b?…?= の方だけを読んでも (古い読み方) 同じ名前になる
    old = email.message_from_bytes(raw)   # compat32 (RFC 2231 を先に読まない)
    pdf = old.get_payload()[1]
    raw_name = pdf.get_param("filename", header="content-disposition")
    assert raw_name.startswith("=?utf-8?b?")
    assert str(email.header.make_header(email.header.decode_header(raw_name))) == \
        "見積書_2026年10月分_清掃とリネンの料金表.pdf"
    assert str(email.header.make_header(email.header.decode_header(pdf.get_param("name")))) == \
        "見積書_2026年10月分_清掃とリネンの料金表.pdf"
    # 英数字だけの名前はそのまま
    assert 'filename="IMG_0001.jpg"' in sent["msg"] and "filename*" not in \
        sent["msg"][sent["msg"].index("Content-Type: image/jpeg"):]
    # 送ったファイルには送った時刻が入り、もう外せない・付けられない
    assert all(f["sent_at"] for f in _files(tmp_path, mid))
    assert client.delete(f"/api/mail/{mid}/reply-files/{ids[0]}", headers=auth(admin)).status_code == 409
    r = _upload(client, admin, mid, ("x.txt", b"x", "text/plain"))
    assert r.status_code == 409 and "返信済み" in r.json()["detail"]
    # 記録に数と名前が残る
    (detail,) = _audits(tmp_path, "mail_send")
    assert "attachments=2(見積書_2026年10月分_清掃とリネンの料金表.pdf, IMG_0001.jpg)" in detail
    # 返信画面には送ったファイルが出る (送った後も確かめられる)
    d = _detail(client, admin, mid)
    assert [(f["filename"], f["sent"]) for f in d["reply_files"]] == [
        (files[0]["filename"], True), ("IMG_0001.jpg", True)]
    assert client.get(f"/api/mail/{mid}/reply-files/{ids[1]}", headers=auth(admin)).content == JPG


def test_text_only_send_is_unchanged(client, users, tmp_path, smtp):
    """ファイルを付けていない返信は、今までどおり本文だけ (text/plain の1つの部品・待ち時間も同じ)."""
    _, _, admin, _ = users
    mid = _add_mail(tmp_path)
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin), json={"body": "本文です"})
    assert r.status_code == 200, r.text
    assert r.json()["reply_files"] == []
    (sent,) = smtp["sent"]
    assert sent["kw"] == {}
    msg = email.message_from_string(sent["msg"])
    assert not msg.is_multipart()
    assert msg.get_content_type() == "text/plain" and msg.get_content_charset() == "utf-8"
    assert msg["MIME-Version"] == "1.0" and msg["Content-Transfer-Encoding"] == "base64"
    assert msg.get_payload(decode=True).decode() == "本文です"
    assert "multipart" not in sent["msg"] and "Content-Disposition" not in sent["msg"]
    assert smtp["saved"] == [sent["msg"].encode("utf-8")]
    assert "attachments=" not in _audits(tmp_path, "mail_send")[0]


def test_send_reply_without_attachments_keeps_the_same_shape(monkeypatch):
    """send_reply そのもの: attachments が無い・空なら、前の版と同じ形 (ヘッダの並びも同じ)."""
    from server import mail, notify
    out = []
    monkeypatch.setattr(notify, "smtp_send",
                        lambda s, sender, rcpts, message: out.append(message) or {})
    for kw in ({}, {"attachments": []}, {"attachments": None}):
        mail.send_reply({"smtp_host": "x", "mail_from": "info@bics.example"},
                        "a@example.com", "件名", "本文", **kw)
    heads = [[line.split(":")[0] for line in m.split("\n\n")[0].splitlines()] for m in out]
    assert heads[0] == heads[1] == heads[2] == [
        "Content-Type", "MIME-Version", "Content-Transfer-Encoding", "Subject", "From", "To",
        "Date", "Message-ID"]


def test_failed_send_keeps_files_attached(client, users, tmp_path, smtp):
    _, _, admin, _ = users
    mid = _add_mail(tmp_path)
    ids = [f["id"] for f in _upload(client, admin, mid, ("a.pdf", PDF, "application/pdf")).json()["files"]]
    smtp["fail"] = True
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "本文", "reply_files": ids})
    assert r.status_code == 502
    d = _detail(client, admin, mid)
    assert d["status"] == "unhandled"
    assert [(f["id"], f["sent"]) for f in d["reply_files"]] == [(ids[0], False)]
    assert _files(tmp_path, mid)[0]["sent_at"] is None
    # 直してから送り直せる
    smtp["fail"] = False
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "本文", "reply_files": ids})
    assert r.status_code == 200 and len(smtp["sent"]) == 1


def test_refused_recipient_keeps_files_attached(client, users, tmp_path, smtp, monkeypatch):
    """宛先に届かなかった (CC には届いた) ときも未対応に戻し、ファイルは付けたまま.

    CC の人に届いたファイルは、送信箱のその1通にも出る (📎 の数・名前・ダウンロード)。
    返信画面でファイルを外しても、送信箱の分は残る。
    """
    from server import mail, notify
    _, _, admin, _ = users
    mid = _add_mail(tmp_path)
    ids = [f["id"] for f in _upload(client, admin, mid, ("見積書.pdf", PDF, "application/pdf"),
                                    ("写真.jpg", JPG, "image/jpeg")).json()["files"]]
    sent = []
    monkeypatch.setattr(notify, "smtp_send", lambda s, sender, rcpts, message, **kw:
                        sent.append(message) or {"taro@example.com": (550, b"no")})
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "本文", "cc": ["sato@x.com"], "reply_files": ids})
    assert r.status_code == 502
    assert [f["sent_at"] for f in _files(tmp_path, mid)] == [None, None]
    assert _detail(client, admin, mid)["status"] == "unhandled"
    (detail,) = _audits(tmp_path, "mail_send_refused")
    assert "attachments=2(見積書.pdf, 写真.jpg)" in detail
    # 送信箱: CC の人に届いた1通に、届いたファイルが出る
    (row,) = [i for i in client.get("/api/mail/sent", headers=auth(admin)).json()["items"]
              if i["key"].startswith("i")]
    assert row["attachment_count"] == 2
    sd = client.get(f"/api/mail/sent/{row['key']}", headers=auth(admin)).json()
    assert "届きませんでした" in sd["note"]
    assert [a["filename"] for a in sd["attachments"]] == ["見積書.pdf", "写真.jpg"]
    assert sd["skipped_attachments"] == []
    # 返信画面で外しても、送信箱の分は残る (ダウンロードできる)
    assert client.delete(f"/api/mail/{mid}/reply-files/{ids[0]}",
                         headers=auth(admin)).status_code == 200
    dl = client.get(f"/api/mail/sent/{row['key']}/attachments/{sd['attachments'][0]['id']}",
                    headers=auth(admin))
    assert dl.status_code == 200 and dl.content == PDF
    # 書きかけの一時フォルダは残さない
    assert sorted(p.name for p in (tmp_path / "mail_attachments").iterdir()) == ["sent"]
    # 送信済みフォルダの控えを後から取り込んでも、同じ1通を2つにしない
    # (取り込みは、送信箱にある Message-ID を入れない)
    with _db(tmp_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sent_mails").fetchone()[0] == 1
        assert conn.execute("SELECT 1 FROM sent_mails WHERE message_id = ?",
                            (mail.sent_message_id(sent[0].encode()),)).fetchone()


def test_too_large_mail_is_explained_in_japanese(client, users, tmp_path, smtp, monkeypatch):
    """送信サーバに「メールが大きすぎる」と断られたら、何をすればよいかを日本語で先に出す
    (サーバの英語の返事は後ろに添える)。ファイルは付けたままで、減らして送り直せる."""
    import smtplib
    from server import notify, reply_files
    _, _, admin, _ = users
    mid = _add_mail(tmp_path)
    ids = [f["id"] for f in _upload(client, admin, mid, ("見積書.pdf", PDF, "application/pdf")).json()["files"]]
    for err in (
            # 大きさを先に知らせたとき (MAIL FROM への返事)
            smtplib.SMTPSenderRefused(552, b"5.3.4 Message size exceeds fixed limit",
                                      "info@bics.example"),
            # 本文を送った後に断られたとき
            smtplib.SMTPDataError(552, b"5.3.4 Your message exceeded Google's message size limits")):
        def refuse(s, sender, rcpts, message, err=err, **kw):
            raise err
        monkeypatch.setattr(notify, "smtp_send", refuse)
        r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                        json={"body": "本文", "reply_files": ids})
        assert r.status_code == 502
        detail = r.json()["detail"]
        assert detail.startswith(f"送信に失敗しました: {reply_files.SEND_TOO_LARGE}（送信サーバの返事: ")
        assert "5.3.4" in detail
        assert _detail(client, admin, mid)["status"] == "unhandled"
        assert _files(tmp_path, mid)[0]["sent_at"] is None
    # ファイルを付けていない返信には、添付の話を出さない
    other = _add_mail(tmp_path, n=2)
    r = client.post(f"/api/mail/{other}/send", headers=auth(admin), json={"body": "本文"})
    assert r.status_code == 502 and "添付ファイル" not in r.json()["detail"]


def test_message_too_large():
    import smtplib
    from server import mail
    for e in (smtplib.SMTPSenderRefused(552, b"5.3.4 Message size exceeds fixed limit", "a@x.com"),
              smtplib.SMTPSenderRefused(552, b"Requested mail action aborted", "a@x.com"),
              smtplib.SMTPDataError(552, b"5.3.4 Your message exceeded Google's message size limits"),
              smtplib.SMTPDataError(554, b"5.3.4 Message too big for system"),
              smtplib.SMTPDataError(550, b"5.2.3 Message length exceeds administrative limit")):
        assert mail.message_too_large(e), e
    for e in (smtplib.SMTPSenderRefused(550, b"5.7.1 Sender address rejected: not owned", "a@x.com"),
              smtplib.SMTPSenderRefused(452, b"4.3.1 Insufficient system storage", "a@x.com"),
              smtplib.SMTPDataError(550, b"5.7.1 Message rejected as spam"),
              # 送り先ごとの 552 は「相手のメールボックスがいっぱい」のこともある
              smtplib.SMTPRecipientsRefused({"a@x.com": (552, b"5.2.2 Mailbox full")}),
              OSError("smtp down")):
        assert not mail.message_too_large(e), e


def test_largest_reply_fits_in_20mb_on_the_wire(monkeypatch):
    """上限いっぱい (10個・合わせて13MB) 付けても、送るメールは 20MB (20,000,000 バイト) に収まる.

    多くの送信サーバの1通の上限は 20〜25MB。中身は base64 と改行で 1.37 倍ほどになる。
    """
    from server import mail, notify, reply_files
    out = []
    monkeypatch.setattr(notify, "smtp_send",
                        lambda s, sender, rcpts, message, **kw: out.append(message) or {})
    sizes = [reply_files.TOTAL_MAX_BYTES // reply_files.MAX_COUNT] * reply_files.MAX_COUNT
    sizes[0] += reply_files.TOTAL_MAX_BYTES - sum(sizes)
    files = [(f"清掃後の写真_{n:02d}_" + "お部屋" * 20 + ".jpg", "image/jpeg", os.urandom(size))
             for n, size in enumerate(sizes)]
    mail.send_reply({"smtp_host": "x", "mail_from": "info@bics.example"}, "a@example.com",
                    "清掃のご報告", "いつもお世話になっております。\n" * 500, cc=["b@example.com"],
                    attachments=files)
    # 送信サーバに送る形 (改行は CRLF)
    wire = out[0].replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii")
    assert len(wire) < 20_000_000, len(wire)


def test_send_refuses_files_not_on_screen(client, users, tmp_path, smtp):
    """画面を開いた後にほかの画面で付けた・外したファイルがあれば、何も送らずに 409."""
    _, _, admin, admin2 = users
    mid = _add_mail(tmp_path)
    a = _upload(client, admin, mid, ("a.pdf", PDF, "application/pdf")).json()["files"][0]["id"]
    # 佐藤さんがほかの画面で付けた
    b = _upload(client, admin2, mid, ("b.pdf", PDF, "application/pdf")).json()["files"][1]["id"]
    for body in ({"body": "本文", "reply_files": [a]},
                 {"body": "本文"},                        # 添付を知らない古い画面
                 {"body": "本文", "reply_files": [a, b, 999]}):
        r = client.post(f"/api/mail/{mid}/send", headers=auth(admin), json=body)
        assert r.status_code == 409 and "添付ファイルが" in r.json()["detail"], r.text
    assert smtp["sent"] == [] and _detail(client, admin, mid)["status"] == "unhandled"
    # 外した後の画面のまま送ろうとしても止まる
    client.delete(f"/api/mail/{mid}/reply-files/{b}", headers=auth(admin2))
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "本文", "reply_files": [a, b]})
    assert r.status_code == 409 and smtp["sent"] == []
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "本文", "reply_files": [a]})
    assert r.status_code == 200 and len(smtp["sent"]) == 1


def test_missing_file_on_disk_stops_the_send(client, users, tmp_path, smtp):
    _, _, admin, _ = users
    mid = _add_mail(tmp_path)
    ids = [f["id"] for f in _upload(client, admin, mid, ("a.pdf", PDF, "application/pdf")).json()["files"]]
    for p in _on_disk(tmp_path):
        p.unlink()
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "本文", "reply_files": ids})
    assert r.status_code == 409 and "a.pdf" in r.json()["detail"]
    assert smtp["sent"] == [] and _detail(client, admin, mid)["status"] == "unhandled"


def test_approve_refuses_files_changed_after_admin_opened(client, users, tmp_path, smtp, notices):
    """確認待ち: 管理者が開いた後に (ほかの管理者が) 添付を変えたら「承認して送信」は 409."""
    _, staff, admin, admin2 = users
    mid = _add_mail(tmp_path)
    a = _upload(client, staff, mid, ("田中の見積書.pdf", PDF, "application/pdf")).json()["files"][0]["id"]
    assert client.post(f"/api/mail/{mid}/request-approval", headers=auth(staff),
                       json={"body": "本文", "reply_files": [a]}).status_code == 200
    # 黒須さんが開いた (見ているのは a だけ)
    d = _detail(client, admin, mid)
    seen_files, requested = [f["id"] for f in d["reply_files"]], d["requested_at"]
    assert seen_files == [a]
    # その後に、佐藤さんが別の画面でファイルを足した
    b = _upload(client, admin2, mid, ("佐藤の写真.jpg", JPG, "image/jpeg"),
                seen=requested).json()["files"][1]["id"]
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "本文", "approve_request": requested, "reply_files": seen_files})
    assert r.status_code == 409 and "閉じて開き直し" in r.json()["detail"]
    assert smtp["sent"] == [] and _detail(client, admin, mid)["status"] == "pending_approval"
    # 外したときも同じ
    client.delete(f"/api/mail/{mid}/reply-files/{a}", headers=auth(admin2),
                  params={"seen_request": requested})
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "本文", "approve_request": requested, "reply_files": [a, b]})
    assert r.status_code == 409 and smtp["sent"] == []
    # 開き直して (b だけが出る) 承認すると、b だけを送る
    d = _detail(client, admin, mid)
    r = client.post(f"/api/mail/{mid}/send", headers=auth(admin),
                    json={"body": "本文", "approve_request": d["requested_at"],
                          "reply_files": [f["id"] for f in d["reply_files"]]})
    assert r.status_code == 200, r.text
    assert r.json()["approved_by"] == "黒須"
    msg = email.message_from_string(smtp["sent"][0]["msg"], policy=email.policy.default)
    assert [p.get_filename() for p in msg.iter_attachments()] == ["佐藤の写真.jpg"]


# ---------- 送った返信の 📎 (やり取り・プルダウン・取引先・送信箱) ----------

def test_sent_files_show_in_thread_conversation_client_and_sent_box(client, users, tmp_path, smtp):
    _, _, admin, _ = users
    first = _add_mail(tmp_path, n=1)
    later = _add_mail(tmp_path, n=2, subject="追加のご質問")
    ids = [f["id"] for f in _upload(client, admin, first, ("見積書.pdf", PDF, "application/pdf"),
                                    ("写真.jpg", JPG, "image/jpeg")).json()["files"]]
    # 付けただけ (まだ送っていない) のファイルは、どこにも出さない
    item = next(t for t in _detail(client, admin, later)["thread"] if t["id"] == first)
    assert item["reply_files"] == []
    assert client.post(f"/api/mail/{first}/send", headers=auth(admin),
                       json={"body": "お送りします", "reply_files": ids}).status_code == 200
    # 返信画面の「この相手とのやり取り」
    d = _detail(client, admin, later)
    item = next(t for t in d["thread"] if t["id"] == first)
    assert item["reply_files"] == ["見積書.pdf", "写真.jpg"]
    # 受信箱のプルダウン (この後のやり取り)
    conv = client.get(f"/api/mail/{first}/conversation", headers=auth(admin)).json()
    (out,) = [e for e in conv["after"] if e["kind"] == "out"]
    assert out["files"] == ["見積書.pdf", "写真.jpg"]
    # 取引先のやり取り
    cl = client.get(f"/api/mail/clients/{d['client']['key']}", headers=auth(admin)).json()
    (out,) = [t for t in cl["timeline"] if t["kind"] == "out"]
    assert out["files"] == ["見積書.pdf", "写真.jpg"]
    # 送信箱 (数・中身・ダウンロード)
    (row,) = [i for i in client.get("/api/mail/sent", headers=auth(admin)).json()["items"]
              if i["key"] == f"a{first}"]
    assert row["attachment_count"] == 2
    sd = client.get(f"/api/mail/sent/a{first}", headers=auth(admin)).json()
    assert [(a["filename"], a["previewable"]) for a in sd["attachments"]] == [
        ("見積書.pdf", False), ("写真.jpg", False)]
    dl = client.get(f"/api/mail/sent/a{first}/attachments/{ids[0]}", headers=auth(admin))
    assert dl.status_code == 200 and dl.content == PDF
    assert dl.headers["content-type"] == "application/octet-stream"
    assert dl.headers["x-content-type-options"] == "nosniff"
    # ほかのメールの番号・まだ送っていないファイルは 404
    assert client.get(f"/api/mail/sent/a{later}/attachments/{ids[0]}",
                      headers=auth(admin)).status_code == 404
    staged = _upload(client, admin, later, ("下書き.pdf", PDF, "application/pdf")).json()["files"][0]["id"]
    assert client.get(f"/api/mail/sent/a{later}/attachments/{staged}",
                      headers=auth(admin)).status_code == 404


# ---------- 片付け (保存日数・持ち主のいないファイル) ----------

def test_purge_removes_reply_files(client, users, tmp_path):
    from server import db, mail
    _, _, admin, _ = users
    old = _add_mail(tmp_path, n=1, received_at="2020-01-01T00:00:00+00:00")
    new = _add_mail(tmp_path, n=2, received_at="2099-01-01T00:00:00+00:00",
                    fetched_at="2099-01-01T00:00:00+00:00")
    _upload(client, admin, old, ("古い.pdf", PDF, "application/pdf"))
    _upload(client, admin, new, ("新しい.pdf", PDF, "application/pdf"))
    reply_dir = tmp_path / "mail_reply_files"
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.purge_old_mails(conn, 30, tmp_path / "mail_attachments", reply_dir) == 1
    assert _files(tmp_path, old) == [] and not (reply_dir / str(old)).exists()
    assert len(_files(tmp_path, new)) == 1 and len(_on_disk(tmp_path, new)) == 1


def test_orphan_files_are_cleaned_up(client, users, tmp_path):
    from server import db, reply_files
    _, _, admin, _ = users
    mid = _add_mail(tmp_path, n=1)
    gone = _add_mail(tmp_path, n=2)
    _upload(client, admin, mid, ("a.pdf", PDF, "application/pdf"))
    _upload(client, admin, gone, ("b.pdf", PDF, "application/pdf"))
    reply_dir = tmp_path / "mail_reply_files"
    # 前の版に戻した間に、その版が保存日数でメールを消した (DB の行は外部キーで一緒に消える)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("DELETE FROM mail_attachments WHERE mail_id = ?", (gone,))
        conn.execute("DELETE FROM mails WHERE id = ?", (gone,))
    assert _files(tmp_path, gone) == []
    # 受け取りの途中でサーバが止まった (DB に行が無い) ファイル: 古いものと、受け取り中かもしれない新しいもの
    stale, fresh = reply_dir / str(mid) / ("a" * 32), reply_dir / str(mid) / ("b" * 32)
    stale.write_bytes(b"x")
    fresh.write_bytes(b"y")
    past = time.time() - 2 * 24 * 3600
    os.utime(stale, (past, past))
    # まだ確定していない番号のフォルダ (取り込み中のメール) は消さない
    future = reply_dir / "9999"
    future.mkdir()
    (future / ("c" * 32)).write_bytes(b"z")
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert reply_files.remove_orphans(conn, reply_dir) == 2
    assert not (reply_dir / str(gone)).exists()
    assert not stale.exists() and fresh.exists() and future.exists()
    assert len(_files(tmp_path, mid)) == 1
    tracked = reply_dir / _files(tmp_path, mid)[0]["path"]
    assert tracked.read_bytes() == PDF
    # 保存先が無くても止まらない
    shutil.rmtree(reply_dir)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert reply_files.remove_orphans(conn, reply_dir) == 0


def test_table_is_added_to_an_old_db_and_migration_is_idempotent(tmp_path):
    from server import db
    path = tmp_path / "old.db"
    # この機能の前の DB (表が無い)
    with db.get_db(path) as conn:
        conn.execute("DROP TABLE mail_reply_files")
    raw = sqlite3.connect(path)
    assert not raw.execute("SELECT 1 FROM sqlite_master WHERE name = 'mail_reply_files'").fetchone()
    raw.close()
    for _ in range(2):
        with db.get_db(path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(mail_reply_files)")]
            assert cols == ["id", "mail_id", "filename", "content_type", "size", "path",
                            "uploaded_by", "created_at", "sent_at"]
    # 前の版のコードがメールを消しても (外部キーで) 行が残らない
    with db.get_db(path) as conn:
        mid = conn.execute("INSERT INTO mails (message_id, from_addr, subject, body, received_at,"
                           " fetched_at) VALUES ('<x@y>', 'a@b', 's', 'b', 'now', 'now')").lastrowid
        conn.execute("INSERT INTO mail_reply_files (mail_id, filename, path, created_at)"
                     " VALUES (?, 'a.txt', ?, 'now')", (mid, f"{mid}/{'0' * 32}"))
        conn.execute("DELETE FROM mails WHERE id = ?", (mid,))
        assert conn.execute("SELECT COUNT(*) FROM mail_reply_files").fetchone()[0] == 0


# ---------- 画面 ----------

def test_mail_page_has_reply_file_ui(client):
    page = client.get("/mail").text
    # ボタン (iPhone では写真ライブラリ・写真を撮る・ファイルを選択が出る。種類を絞らない)
    assert '<input type="file" id="m-rf-input" class="rf-input" multiple>' in page
    assert '<label for="m-rf-input" class="rf-add" id="m-rf-add">📎 ファイルを添付</label>' in page
    assert "1通に10個・合わせて13MBまで" in page and "15MB" not in page
    assert "const RF_MAX_COUNT = 10, RF_MAX_MB = 13;" in page
    # ファイル名は外から来た文字なので esc() を通す
    for marker in ("${esc(f.filename)}</button>", "${esc(q.file.name)}",
                   'aria-label="${esc(f.filename)} を外す"',
                   '📎 ${esc(t.reply_files.join("、"))}', '📎 添付: " + esc(t.reply_files.join("、"))',
                   '📎 添付: ${esc(e.files.join("、"))}', '📎 ${esc(t.files.join("、"))}'):
        assert marker in page, marker
    assert "${f.filename}" not in page
    # 付けられなかったわけ (名前を含む) は textContent で出す
    errors = page[page.index("function rfShowErrors"):]
    assert "el.textContent = text;" in errors[:errors.index("\n}\n")]
    # アップロード中…・送るときに画面のファイルの番号を渡す・送る前の確認に出す・アップロード中は送らない
    assert "・アップロード中…" in page
    assert "reply_files: rfList.map(f => f.id)" in page
    assert page.count("rfConfirmText() +") == 2
    assert "ファイルをアップロードしています。終わってから送ってください" in page
    assert "$(\"m-send\").disabled = m.status === \"replied\" || rfBusy();" in page
    # ダウンロードはトークンを付けて受け取り、「ただのデータ」として保存する
    assert "await api(`/api/mail/${id}/reply-files/${Number(f.id)}`)" in page
    # 送れた・確認をお願いできた後は、前の「付けられません」の赤い知らせを消す
    send = page[page.index('$("m-send").onclick'):]
    send = send[:send.index("\n};\n")]
    assert "rfList = r.reply_files || [];\n    rfShowErrors([]);" in send
    req = page[page.index('$("m-request").onclick'):]
    req = req[:req.index("\n};\n")]
    assert -1 < req.index("rfShowErrors([]);") < req.index("renderControls(modal.mail);")
    # PC はファイルを持ってきても付けられる (ブラウザがファイルを開かないように止める)
    assert '$("mail-modal").addEventListener("drop"' in page
    # 使い方
    assert "返信画面の「📎 ファイルを添付」で、写真・PDF・Excel・Word などを返信に付けて送れます" in page


def test_confirm_text_lists_the_files(client):
    node = shutil.which("node")
    if not node:
        pytest.skip("node が無い")
    page = client.get("/mail").text
    i = page.index("function rfConfirmText()")
    fn = page[i:page.index("\n}\n", i) + 3]
    script = fn + """
let rfList = [];
const out = [rfConfirmText()];
rfList = [{ filename: "見積書.pdf" }, { filename: "写真.jpg" }];
out.push(rfConfirmText());
rfList = [1, 2, 3, 4].map(n => ({ filename: `${n}.pdf` }));
out.push(rfConfirmText());
console.log(JSON.stringify(out));
"""
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60,
                       encoding="utf-8")
    assert r.returncode == 0, r.stderr
    import json
    assert json.loads(r.stdout) == ["", "\n添付: 2件（見積書.pdf、写真.jpg）",
                                    "\n添付: 4件（1.pdf、2.pdf、3.pdf ほか）"]
