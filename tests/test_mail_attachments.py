"""受信メールの添付ファイルのテスト.

取り込みのときに添付ファイルを取り出し、メールの行を入れた後でデータフォルダの
mail_attachments/<メールの番号>/<添付の番号> に保存する。大きすぎるものは保存せず、
名前と理由だけを残す。返信画面からは管理者だけが受け取れ (画像以外は必ず
ダウンロード扱い)、保存期間を過ぎたメールと一緒にファイルも消える。
"""
import email
import email.policy
from email.header import Header
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from tests.test_api import _add_mail, auth

ROOT = Path(__file__).resolve().parent.parent

# 先頭が本物の PNG の印 (縮小画像を出してよいかは中身の先頭で決める)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
PDF = b"%PDF-1.4\n% fake pdf\n"


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
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        worker = db.create_user(conn, "tanaka")
        admin = db.create_user(conn, "boss", is_admin=True)
    return worker, admin


def mail_with_files(message_id="<att1@example.com>", files=None):
    """本文 + 添付ファイルのメール (RFC822 のバイト列)."""
    msg = EmailMessage()
    msg["From"] = "山田 <yamada@example.com>"
    msg["To"] = "info@bics.example"
    msg["Subject"] = "お見積もりの件"
    msg["Message-ID"] = message_id
    msg.set_content("本文です。見積書と間取り図を添付します。")
    for name, data, maintype, subtype in files if files is not None else [
            ("見積書.pdf", PDF, "application", "pdf"),
            ("間取り図.png", PNG, "image", "png")]:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


class FakeIMAP:
    """UID → メールの受信箱 (fetch_new_mail 用)."""

    def __init__(self, mails):
        self.mails = mails

    def select(self, folder, readonly=False):
        return ("OK", [str(len(self.mails)).encode()])

    def response(self, key):
        return ("UIDVALIDITY", [b"1"])

    def uid(self, cmd, *args):
        if cmd == "search":
            return ("OK", [b" ".join(str(u).encode() for u in sorted(self.mails))])
        return ("OK", [(b"header", self.mails[int(args[0])])])

    def logout(self):
        pass


def fetch(tmp_path, monkeypatch, mails, att_dir):
    from server import db, mail
    monkeypatch.setattr(mail, "_imap_connect", lambda s: FakeIMAP(mails))
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        s = {**db.get_settings(conn), "imap_host": "imap.example.com",
             "mail_auto_draft": 0}
        return mail.fetch_new_mail(conn, s, attachment_dir=att_dir)


def att_rows(tmp_path):
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM mail_attachments ORDER BY id")]


# ---------- 取り出し ----------

def test_extract_regular_attachments_keeps_body():
    from server import mail
    raw = mail_with_files()
    items = mail.extract_attachments(raw)
    assert [(i["filename"], i["content_type"], i["size"], i["skip"]) for i in items] == [
        ("見積書.pdf", "application/pdf", len(PDF), ""),   # RFC 2231 の名前も戻る
        ("間取り図.png", "image/png", len(PNG), ""),
    ]
    assert items[0]["data"] == PDF and items[1]["data"] == PNG
    # 本文は今までどおり (添付は本文に混ざらない)
    assert mail.parse_message(raw)["body"] == "本文です。見積書と間取り図を添付します。"


def test_extract_inline_images_with_a_name_only():
    """本文に貼り付けた画像は、名前があるものだけ添付として扱う (HTML 版の本文は除く)."""
    from server import mail
    import base64
    b64 = base64.b64encode(PNG).decode()
    raw = (
        "From: a@example.com\r\nMessage-ID: <in1@example.com>\r\nMIME-Version: 1.0\r\n"
        "Content-Type: multipart/alternative; boundary=ALT\r\n\r\n"
        "--ALT\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n写真を見てください\r\n"
        "--ALT\r\nContent-Type: multipart/related; boundary=REL\r\n\r\n"
        "--REL\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        "<p>写真<img src=\"cid:p1\"><img src=\"cid:p2\"><img src=\"cid:logo\"></p>\r\n"
        # iPhone のメールなど: inline + filename
        "--REL\r\nContent-Type: image/jpeg\r\nContent-Disposition: inline; filename=\"IMG_0001.jpg\"\r\n"
        "Content-ID: <p1>\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 16).decode() + "\r\n"
        # 古いメールソフト: Content-Type の name だけ
        "--REL\r\nContent-Type: image/png; name=\"room.png\"\r\nContent-ID: <p2>\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n" + b64 + "\r\n"
        # 名前の無い飾りの画像 (署名のロゴなど) は除く
        "--REL\r\nContent-Type: image/png\r\nContent-Disposition: inline\r\nContent-ID: <logo>\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n" + b64 + "\r\n"
        "--REL--\r\n--ALT--\r\n"
    ).encode()
    items = mail.extract_attachments(raw)
    assert [(i["filename"], i["content_type"]) for i in items] == [
        ("IMG_0001.jpg", "image/jpeg"), ("room.png", "image/png")]
    assert mail.parse_message(raw)["body"] == "写真を見てください"


def test_extract_encoded_filenames():
    """文字コード付きの名前 (=?UTF-8?…?= / ISO-2022-JP / RFC 2231 の分割) を戻す."""
    from server import mail
    utf8 = Header("請求書.pdf", "utf-8").encode()
    jis = Header("見積書.xlsx", "iso-2022-jp").encode()
    parts = [
        f'Content-Type: application/pdf\r\nContent-Disposition: attachment; filename="{utf8}"',
        f'Content-Type: application/octet-stream; name="{jis}"\r\nContent-Disposition: attachment',
        "Content-Type: text/plain\r\nContent-Disposition: attachment;"
        " filename*0*=UTF-8''%E6%B8%85%E6%8E%83; filename*1*=%E8%A1%A8.txt",
        # 名前の無い添付は種類から拡張子を付ける
        "Content-Type: application/pdf\r\nContent-Disposition: attachment",
    ]
    raw = ("From: a@example.com\r\nMIME-Version: 1.0\r\n"
           "Content-Type: multipart/mixed; boundary=XX\r\n\r\n"
           "--XX\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
           + "".join(f"--XX\r\n{p}\r\nContent-Transfer-Encoding: base64\r\n\r\naGVsbG8=\r\n"
                     for p in parts)
           + "--XX--\r\n").encode()
    names = [i["filename"] for i in mail.extract_attachments(raw)]
    assert names == ["請求書.pdf", "見積書.xlsx", "清掃表.txt", "添付ファイル.pdf"]


def test_clean_filename_removes_paths_and_tricks():
    from server import mail
    assert mail.clean_filename("C:\\Users\\yamada\\見積書.pdf") == "見積書.pdf"
    assert mail.clean_filename("../../etc/passwd") == "passwd"
    assert mail.clean_filename("evil/") == "添付ファイル"
    assert mail.clean_filename("請求\r\n書\x00.pdf") == "請求書.pdf"
    # 文字の向きを変える記号で拡張子を偽装させない (「…fdp.exe」のまま見せる)
    assert mail.clean_filename("請求書\u202efdp.exe") == "請求書fdp.exe"
    # Mac から届く濁点の分かれた名前はまとめる
    assert mail.clean_filename("テ\u3099ータ.csv") == "データ.csv"
    assert mail.clean_filename("  ..  ") == "添付ファイル"
    assert mail.clean_filename("", "image/jpeg") == "添付ファイル.jpg"
    assert mail.clean_filename(None, "application/octet-stream") == "添付ファイル"
    long = mail.clean_filename("あ" * 300 + ".pdf")
    assert len(long) == mail.ATTACHMENT_NAME_MAX_CHARS and long.endswith(".pdf")


def test_extract_skips_oversize_and_caps_the_total(monkeypatch):
    from server import mail
    monkeypatch.setattr(mail, "ATTACHMENT_MAX_BYTES", 100)
    monkeypatch.setattr(mail, "ATTACHMENT_TOTAL_MAX_BYTES", 250)
    files = [("big.bin", b"x" * 150, "application", "octet-stream"),
             ("a.bin", b"a" * 100, "application", "octet-stream"),
             ("b.bin", b"b" * 100, "application", "octet-stream"),
             ("c.bin", b"c" * 80, "application", "octet-stream"),   # 合計 280 > 250
             ("d.bin", b"d" * 50, "application", "octet-stream")]   # 合計 250 まで入る
    items = mail.extract_attachments(mail_with_files(files=files))
    assert [(i["filename"], i["size"], i["skip"]) for i in items] == [
        ("big.bin", 150, "too_large"), ("a.bin", 100, ""), ("b.bin", 100, ""),
        ("c.bin", 80, "total_too_large"), ("d.bin", 50, "")]
    assert all(i["data"] is None for i in items if i["skip"])
    assert "大きすぎるため保存していません" in mail.skip_reason_text("too_large")
    assert "大きすぎるため保存していません" in mail.skip_reason_text("total_too_large")


def test_real_limits():
    from server import mail
    assert mail.ATTACHMENT_MAX_BYTES == 15 * 1024 * 1024
    assert mail.ATTACHMENT_TOTAL_MAX_BYTES == 40 * 1024 * 1024
    assert "15MB" in mail.skip_reason_text("too_large")
    assert "40MB" in mail.skip_reason_text("total_too_large")


def test_extract_caps_the_number_of_files(monkeypatch):
    from server import mail
    monkeypatch.setattr(mail, "ATTACHMENT_MAX_COUNT", 3)
    files = [(f"{n}.txt", b"x", "text", "plain") for n in range(5)]
    items = mail.extract_attachments(mail_with_files(files=files))
    assert [(i["filename"], i["skip"]) for i in items] == [
        ("0.txt", ""), ("1.txt", ""), ("2.txt", ""), ("ほか2個のファイル", "too_many")]


def test_image_type_is_decided_by_content():
    """画像を名乗った別物は画像として扱わない。種類の書き間違いの写真は画像として扱う."""
    from server import mail
    files = [("fake.png", b"<html><script>alert(1)</script></html>", "image", "png"),
             ("photo.jpg", b"\xff\xd8\xff\xe1" + b"\x00" * 16, "application", "octet-stream"),
             ("logo.svg", b"<svg onload='alert(1)'/>", "image", "svg+xml")]
    items = mail.extract_attachments(mail_with_files(files=files))
    assert [(i["filename"], i["content_type"]) for i in items] == [
        ("fake.png", "application/octet-stream"), ("photo.jpg", "image/jpeg"),
        ("logo.svg", "image/svg+xml")]


def test_forwarded_mail_is_kept_as_one_eml_file():
    from server import mail
    inner = EmailMessage()
    inner["From"] = "owner@example.com"
    inner["Subject"] = "元のメール"
    inner.set_content("転送元の本文")
    inner.add_attachment(PDF, maintype="application", subtype="pdf", filename="inner.pdf")
    outer = EmailMessage()
    outer["From"] = "a@example.com"
    outer.set_content("転送します")
    outer.add_attachment(inner, filename="元のメール.eml")
    items = mail.extract_attachments(outer.as_bytes())
    assert [(i["filename"], i["content_type"]) for i in items] == [
        ("元のメール.eml", "message/rfc822")]
    back = email.message_from_bytes(items[0]["data"], policy=email.policy.default)
    assert back["Subject"] == "元のメール"


def test_broken_mail_has_no_attachments():
    from server import mail
    assert mail.extract_attachments(b"From: a@example.com\r\n\r\nplain body") == []


# ---------- 取り込みと保存先 ----------

def test_fetch_saves_files_under_mail_and_attachment_ids(client, tmp_path, monkeypatch):
    att_dir = tmp_path / "mail_attachments"
    added = fetch(tmp_path, monkeypatch, {5: mail_with_files()}, att_dir)
    assert len(added) == 1
    rows = att_rows(tmp_path)
    assert [(r["filename"], r["content_type"], r["size"], r["skip_reason"]) for r in rows] == [
        ("見積書.pdf", "application/pdf", len(PDF), ""),
        ("間取り図.png", "image/png", len(PNG), "")]
    mail_id = rows[0]["mail_id"]
    for r, data in zip(rows, (PDF, PNG)):
        # 保存先は番号だけ (元のファイル名はパスに使わない)
        assert r["path"] == f"{mail_id}/{r['id']}"
        assert (att_dir / r["path"]).read_bytes() == data
    assert sorted(p.name for p in (att_dir / str(mail_id)).iterdir()) == sorted(
        str(r["id"]) for r in rows)


def test_fetch_without_attachment_dir_stores_no_files(client, tmp_path, monkeypatch):
    """保存先を渡さない呼び出し (今までの使い方) では添付を保存しない."""
    from server import db, mail
    monkeypatch.setattr(mail, "_imap_connect", lambda s: FakeIMAP({5: mail_with_files()}))
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        s = {**db.get_settings(conn), "imap_host": "imap.example.com", "mail_auto_draft": 0}
        assert len(mail.fetch_new_mail(conn, s)) == 1
    assert att_rows(tmp_path) == []


def test_duplicate_mail_does_not_duplicate_attachments(client, tmp_path, monkeypatch):
    """同じメールが2通 (同じ Message-ID) 届いても、添付は1通分だけ保存する."""
    att_dir = tmp_path / "mail_attachments"
    raw = mail_with_files()
    added = fetch(tmp_path, monkeypatch, {5: raw, 6: raw}, att_dir)
    assert len(added) == 1
    rows = att_rows(tmp_path)
    assert len(rows) == 2
    assert [p.name for p in att_dir.iterdir()] == [str(rows[0]["mail_id"])]
    assert len(list((att_dir / str(rows[0]["mail_id"])).iterdir())) == 2
    # 次の受信で同じメールがまた見えても増えない
    from server import db
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        db.set_setting(conn, "mail_state_last_uid", 0)
    assert fetch(tmp_path, monkeypatch, {5: raw}, att_dir) == []
    assert len(att_rows(tmp_path)) == 2


def test_fetch_failure_removes_written_files(client, tmp_path, monkeypatch):
    """メールを入れた後で失敗したら、DB を戻して書いたファイルも消す."""
    from server import db, mail
    att_dir = tmp_path / "mail_attachments"

    def boom(conn, key, value):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(mail, "_set_state", boom)
    with pytest.raises(RuntimeError):
        fetch(tmp_path, monkeypatch, {5: mail_with_files()}, att_dir)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM mails").fetchone()[0] == 0
    assert att_rows(tmp_path) == []
    assert not att_dir.exists() or list(att_dir.iterdir()) == []


def test_leftover_folder_from_an_interrupted_run_is_replaced(client, tmp_path, monkeypatch):
    """前回途中で止まったときの残りもの (同じ番号のフォルダ) は消してから保存する."""
    att_dir = tmp_path / "mail_attachments"
    stale = att_dir / "1" / "99"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    fetch(tmp_path, monkeypatch, {5: mail_with_files()}, att_dir)
    rows = att_rows(tmp_path)
    assert rows[0]["mail_id"] == 1
    assert not stale.exists()
    assert sorted(p.name for p in (att_dir / "1").iterdir()) == sorted(
        str(r["id"]) for r in rows)


def test_disk_error_keeps_the_mail(client, tmp_path, monkeypatch):
    """ディスクに書けなくてもメールは取り込み、添付は「保存できませんでした」と残す."""
    from server import mail
    att_dir = tmp_path / "blocked"
    att_dir.write_bytes(b"")   # フォルダを作れない (同じ名前のファイルがある)
    added = fetch(tmp_path, monkeypatch, {5: mail_with_files()}, att_dir)
    assert len(added) == 1
    rows = att_rows(tmp_path)
    assert [(r["skip_reason"], r["path"]) for r in rows] == [
        ("save_failed", ""), ("save_failed", "")]
    assert mail.skip_reason_text("save_failed") == "サーバに保存できませんでした"


def test_oversize_attachment_is_recorded_not_saved(client, tmp_path, monkeypatch):
    from server import mail
    monkeypatch.setattr(mail, "ATTACHMENT_MAX_BYTES", 10)
    att_dir = tmp_path / "mail_attachments"
    fetch(tmp_path, monkeypatch, {5: mail_with_files()}, att_dir)
    rows = att_rows(tmp_path)
    assert [(r["filename"], r["skip_reason"], r["path"]) for r in rows] == [
        ("見積書.pdf", "too_large", ""), ("間取り図.png", "too_large", "")]
    assert not (att_dir / str(rows[0]["mail_id"])).exists() or \
        list((att_dir / str(rows[0]["mail_id"])).iterdir()) == []


# ---------- 画面用の API ----------

def add_mail_with_atts(tmp_path, items, n=1, **kw):
    """受信メールを1件と、その添付ファイルを直接入れて (mail_id, 添付の id 一覧) を返す."""
    from server import db, mail
    mid = _add_mail(tmp_path, n=n, **kw)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        mail.save_attachments(conn, tmp_path / "mail_attachments", mid, items,
                              "2026-10-01T00:00:00+00:00")
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM mail_attachments WHERE mail_id = ? ORDER BY id", (mid,))]
    return mid, ids


def item(filename, data, content_type, skip=""):
    return {"filename": filename, "content_type": content_type,
            "size": len(data) if data else 52 * 1024 * 1024,
            "data": None if skip else data, "skip": skip}


def test_detail_and_list_show_attachments(client, users, tmp_path):
    _, admin = users
    mid, (pdf_id, png_id, _big) = add_mail_with_atts(tmp_path, [
        item("見積書.pdf", PDF, "application/pdf"),
        item("間取り図.png", PNG, "image/png"),
        item("動画.mov", None, "video/quicktime", skip="too_large")])
    plain = _add_mail(tmp_path, n=2)
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["attachments"] == [
        {"id": pdf_id, "filename": "見積書.pdf", "content_type": "application/pdf",
         "size": len(PDF), "previewable": False},
        {"id": png_id, "filename": "間取り図.png", "content_type": "image/png",
         "size": len(PNG), "previewable": True}]
    assert d["skipped_attachments"] == [
        {"filename": "動画.mov", "size": 52 * 1024 * 1024,
         "reason": "大きすぎるため保存していません（1つ15MBまで）"}]
    # 添付の無いメール (この機能より前に取り込んだメールも同じ)
    d2 = client.get(f"/api/mail/{plain}", headers=auth(admin)).json()
    assert d2["attachments"] == [] and d2["skipped_attachments"] == []
    counts = {m["id"]: m["attachment_count"]
              for m in client.get("/api/mail", headers=auth(admin)).json()}
    assert counts == {mid: 3, plain: 0}


def test_download_requires_admin(client, users, tmp_path):
    worker, _ = users
    mid, (aid,) = add_mail_with_atts(tmp_path, [item("見積書.pdf", PDF, "application/pdf")])
    url = f"/api/mail/{mid}/attachments/{aid}"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=auth(worker)).status_code == 403


def test_download_headers_for_a_document(client, users, tmp_path):
    _, admin = users
    mid, (aid,) = add_mail_with_atts(tmp_path, [item("見積書.pdf", PDF, "application/pdf")])
    for q in ("", "?inline=1"):   # 画像以外は表示用を頼まれてもダウンロード扱い
        r = client.get(f"/api/mail/{mid}/attachments/{aid}{q}", headers=auth(admin))
        assert r.status_code == 200
        assert r.content == PDF
        assert r.headers["content-type"] == "application/octet-stream"
        cd = r.headers["content-disposition"]
        assert cd.startswith("attachment;")
        assert f"filename*=UTF-8''{quote('見積書.pdf', safe='')}" in cd
        assert 'filename="attachment.pdf"' in cd
        assert r.headers["x-content-type-options"] == "nosniff"
        assert "no-store" in r.headers["cache-control"]


def test_download_image_inline_for_preview(client, users, tmp_path):
    _, admin = users
    mid, (aid,) = add_mail_with_atts(tmp_path, [item("room.png", PNG, "image/png")])
    url = f"/api/mail/{mid}/attachments/{aid}"
    r = client.get(url + "?inline=1", headers=auth(admin))
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"].startswith("inline;")
    assert 'filename="room.png"' in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff"
    r = client.get(url, headers=auth(admin))
    assert r.headers["content-disposition"].startswith("attachment;")


@pytest.mark.parametrize("name,data,declared", [
    ("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>", "image/svg+xml"),
    ("page.html", b"<html><script>alert(1)</script></html>", "text/html"),
])
def test_svg_and_html_are_never_shown_inline(client, users, tmp_path, name, data, declared):
    _, admin = users
    from server import mail
    files = [(name, data, *declared.split("/"))]
    items = mail.extract_attachments(mail_with_files(files=files))
    mid, (aid,) = add_mail_with_atts(tmp_path, items)
    d = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()
    assert d["attachments"][0]["previewable"] is False
    r = client.get(f"/api/mail/{mid}/attachments/{aid}?inline=1", headers=auth(admin))
    assert r.status_code == 200 and r.content == data
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment;")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_download_checks_the_attachment_belongs_to_the_mail(client, users, tmp_path):
    _, admin = users
    mid_a, (aid,) = add_mail_with_atts(tmp_path, [item("a.pdf", PDF, "application/pdf")], n=1)
    mid_b, _ = add_mail_with_atts(tmp_path, [item("b.pdf", PDF, "application/pdf")], n=2)
    assert client.get(f"/api/mail/{mid_a}/attachments/{aid}",
                      headers=auth(admin)).status_code == 200
    r = client.get(f"/api/mail/{mid_b}/attachments/{aid}", headers=auth(admin))
    assert r.status_code == 404
    assert client.get(f"/api/mail/{mid_a}/attachments/99999",
                      headers=auth(admin)).status_code == 404


def test_download_of_skipped_or_missing_file_is_404(client, users, tmp_path):
    _, admin = users
    mid, (ok_id, skipped_id) = add_mail_with_atts(tmp_path, [
        item("a.pdf", PDF, "application/pdf"),
        item("big.mov", None, "video/quicktime", skip="too_large")])
    assert client.get(f"/api/mail/{mid}/attachments/{skipped_id}",
                      headers=auth(admin)).status_code == 404
    (tmp_path / "mail_attachments" / str(mid) / str(ok_id)).unlink()
    r = client.get(f"/api/mail/{mid}/attachments/{ok_id}", headers=auth(admin))
    assert r.status_code == 404 and "見つかりません" in r.json()["detail"]


def test_download_ignores_a_tampered_path(client, users, tmp_path):
    """DB の path が番号の形でなければ、データフォルダの外を読まない."""
    from server import db
    _, admin = users
    (tmp_path / "secret.txt").write_text("secret")
    mid, (aid,) = add_mail_with_atts(tmp_path, [item("a.pdf", PDF, "application/pdf")])
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE mail_attachments SET path = '../secret.txt' WHERE id = ?", (aid,))
    assert client.get(f"/api/mail/{mid}/attachments/{aid}",
                      headers=auth(admin)).status_code == 404


def test_content_disposition_fallback_names():
    from server import mail
    assert mail.content_disposition("attachment", "report.pdf") == (
        "attachment; filename=\"report.pdf\"; filename*=UTF-8''report.pdf")
    cd = mail.content_disposition("attachment", 'a"b;c.pdf')
    assert 'filename="attachment.pdf"' in cd and "a%22b%3Bc.pdf" in cd
    assert 'filename="attachment"' in mail.content_disposition("inline", "写真")


def test_attachment_route_is_not_shadowed(client, users, tmp_path):
    """/api/mail/{id}/attachments/{id} が他の道と取り違えられない (Caddy の許可リストにも入る)."""
    _, admin = users
    mid, (aid,) = add_mail_with_atts(tmp_path, [item("a.pdf", PDF, "application/pdf")])
    assert client.get(f"/api/mail/{mid}/attachments/{aid}",
                      headers=auth(admin)).status_code == 200
    caddy = (ROOT / "Caddyfile.mail").read_text(encoding="utf-8")
    assert " /api/mail/* " in caddy


# ---------- 保存期間で消す ----------

def test_purge_removes_files_and_rows(client, tmp_path):
    from datetime import datetime, timezone
    from server import db, mail
    att_dir = tmp_path / "mail_attachments"
    old, (old_att,) = add_mail_with_atts(
        tmp_path, [item("old.pdf", PDF, "application/pdf")], n=1,
        received_at="2020-01-01T00:00:00+00:00")
    now = datetime.now(timezone.utc).isoformat()
    new, (new_att,) = add_mail_with_atts(
        tmp_path, [item("new.pdf", PDF, "application/pdf")], n=2,
        received_at=now, fetched_at=now)
    no_att = _add_mail(tmp_path, n=3, received_at="2020-01-01T00:00:00+00:00")
    assert (att_dir / str(old) / str(old_att)).exists()
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.purge_old_mails(conn, 180, att_dir) == 2
        assert [r["id"] for r in conn.execute("SELECT id FROM mails")] == [new]
        assert [r["mail_id"] for r in conn.execute("SELECT mail_id FROM mail_attachments")] == [new]
        # もう一度消しても何も起きない (消えているファイルは飛ばす)
        assert mail.purge_old_mails(conn, 180, att_dir) == 0
    assert not (att_dir / str(old)).exists()
    assert not (att_dir / str(no_att)).exists()
    assert (att_dir / str(new) / str(new_att)).read_bytes() == PDF


def test_purge_when_files_are_already_gone(client, tmp_path):
    """ファイルを先に消してから DB を消す。途中で止まった後の2回目も失敗しない."""
    import shutil
    from server import db, mail
    att_dir = tmp_path / "mail_attachments"
    old, _ = add_mail_with_atts(tmp_path, [item("old.pdf", PDF, "application/pdf")],
                                n=1, received_at="2020-01-01T00:00:00+00:00")
    shutil.rmtree(att_dir / str(old))
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        assert mail.purge_old_mails(conn, 180, att_dir) == 1
        assert conn.execute("SELECT COUNT(*) FROM mail_attachments").fetchone()[0] == 0


def test_background_tick_saves_and_purges_attachments(client, users, tmp_path, monkeypatch):
    """定期受信は添付を保存し、保存期間を過ぎたら一緒に消す (app の保存先を使う)."""
    from server import app as app_module, db, mail
    _, admin = users
    client.patch("/api/settings", headers=auth(admin),
                 json={"imap_host": "imap.example.com", "mail_auto_draft": False})
    monkeypatch.setattr(mail, "_imap_connect", lambda s: FakeIMAP({5: mail_with_files()}))
    app_module._mail_tick()
    rows = att_rows(tmp_path)
    assert len(rows) == 2
    files = [app_module.MAIL_ATTACHMENT_DIR / r["path"] for r in rows]
    assert app_module.MAIL_ATTACHMENT_DIR == tmp_path / "mail_attachments"
    assert all(f.exists() for f in files)
    with db.get_db(tmp_path / "zatsumu.db") as conn:
        conn.execute("UPDATE mails SET fetched_at = '2020-01-01T00:00:00+00:00'")
    monkeypatch.setattr(mail, "_imap_connect", lambda s: FakeIMAP({}))
    app_module._mail_tick()
    assert att_rows(tmp_path) == []
    assert not any(f.exists() for f in files)


def test_fetch_endpoint_saves_attachments(client, users, tmp_path, monkeypatch):
    from server import mail
    _, admin = users
    client.patch("/api/settings", headers=auth(admin), json={"imap_host": "imap.example.com"})
    monkeypatch.setattr(mail, "_imap_connect", lambda s: FakeIMAP({5: mail_with_files()}))
    r = client.post("/api/mail/fetch", headers=auth(admin))
    assert r.status_code == 200 and r.json() == {"fetched": 1}
    mid = client.get("/api/mail", headers=auth(admin)).json()[0]["id"]
    atts = client.get(f"/api/mail/{mid}", headers=auth(admin)).json()["attachments"]
    assert [a["filename"] for a in atts] == ["見積書.pdf", "間取り図.png"]
    got = client.get(f"/api/mail/{mid}/attachments/{atts[1]['id']}?inline=1",
                     headers=auth(admin))
    assert got.content == PNG and got.headers["content-type"] == "image/png"


# ---------- DB・デモ・画面 ----------

def test_existing_db_gets_the_attachment_table(tmp_path):
    from server import db
    path = tmp_path / "old.db"
    conn = db.connect(path)
    conn.execute("DROP TABLE mail_attachments")   # この機能より前の DB
    conn.commit()
    conn.close()
    for _ in range(2):   # 何度開いても壊れない
        conn = db.connect(path)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(mail_attachments)")}
        conn.close()
        assert {"id", "mail_id", "filename", "content_type", "size", "path",
                "skip_reason", "created_at"} <= cols


def test_demo_seed_adds_an_attachment(tmp_path):
    from server import db, demo
    att_dir = tmp_path / "mail_attachments"
    with db.get_db(tmp_path / "demo.db") as conn:
        assert demo.seed(conn, tmp_path / "shots", att_dir) is True
        rows = [dict(r) for r in conn.execute("SELECT * FROM mail_attachments ORDER BY id")]
    assert [(r["filename"], r["content_type"], r["skip_reason"]) for r in rows] == [
        ("間取り図.png", "image/png", ""), ("物件の動画.mov", "video/quicktime", "too_large")]
    assert (att_dir / rows[0]["path"]).read_bytes().startswith(b"\x89PNG")


def test_mail_page_has_attachment_ui(client):
    page = client.get("/mail").text
    for marker in ('id="m-atts"', 'id="att-viewer"', "data-att-dl", "fmtSize(a.size)",
                   "m.skipped_attachments", "m.attachment_count",
                   "Webメールで元のメールを開くと見られます"):
        assert marker in page, marker
    # ファイル名・理由は外から来た文字なので esc() を通す
    assert "${esc(a.filename)}" in page and "${esc(a.reason)}" in page
    assert "${a.filename}" not in page and "${a.reason}" not in page
    # トークンを付けて受け取る (リンクで直接開かない) → 一時的な URL で保存・表示し、閉じたら消す
    assert "await api(`/api/mail/${mailId}/attachments/" in page
    assert 'href="/api/mail' not in page
    assert "URL.createObjectURL" in page and "URL.revokeObjectURL" in page
    assert "link.download = a.filename" in page
    # 画像の表示用以外は「ただのデータ」として扱う
    assert '"application/octet-stream"' in page
    assert 'const PREVIEW_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"]' in page
    # メールを閉じたら一時的な URL を消す
    close = page[page.index("async function closeModal"):]
    assert "clearAttachments();" in close[:close.index("\n}\n")]
