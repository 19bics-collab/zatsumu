"""返信に付けるファイル (返信画面の「📎 ファイルを添付」).

返信画面で選んだファイル (iPhone の写真・PDF・Excel など) は、選んだときにすぐサーバへ送り、
返信を送るまで置いておく (返信文の下書きと同じ。閉じて開き直しても残る)。中身はデータフォルダの
mail_reply_files/<メールの番号>/<でたらめな英数字の名前> に保存し (元のファイル名は保存先のパスに
使わない)、DB (mail_reply_files) に名前・種類・大きさ・付けた人を残す。返信を送ったら送った時刻
(sent_at) を入れ、やり取り・送信箱に 📎 で名前を出す。

受け取るときは、届いた分から少しずつファイルに書く (大きなファイルを丸ごとメモリに読まない)。
1通の返信に付けられる数と合わせた大きさには上限があり、超えた分は受け取りの途中でやめる。
メールが保存日数で消えるときは、ファイルも一緒に消す (mail.purge_old_mails)。
"""
import mimetypes
import re
import secrets
import shutil
import time
from pathlib import Path

try:
    from python_multipart.multipart import MultipartParser, parse_options_header
except ImportError:  # pragma: no cover  古い python-multipart (0.0.12 まで) は名前が multipart
    from multipart.multipart import MultipartParser, parse_options_header

from . import mail

_MB = 1024 * 1024
# 1通の返信に付けられるファイルの数と、合わせた大きさの上限。メールで送るときは中身を
# base64 という形にして76文字ごとに改行するので 1.37 倍ほどになる (13MB → 約19MB)。
# 多くのメールサーバが受け取る1通の大きさ (20〜25MB) に収まるようにする
# (15MB だと約21.5MB になり、20MB までの送信サーバに断られる)
MAX_COUNT = 10
TOTAL_MAX_BYTES = 13 * _MB
# 1回の受け取りで、ファイルの中身のほかに許す大きさ (区切りの行・名前など)
REQUEST_OVERHEAD = 256 * 1024
# 受け取りの途中で止まって DB に入らなかったファイルを、片付けで消すまでの時間 (秒)。
# 受け取っている最中のファイルを消さないよう、十分に長くする
_STALE_SECONDS = 24 * 3600
# 保存先の名前 (でたらめな英数字。DB の path は「<mail_id>/<名前>」)
_PATH_RE = re.compile(r"\d+/[0-9a-f]{32}")
# Windows でファイル名に使えない文字 (相手が Outlook などで保存するときに困らないよう「_」にする)
_WINDOWS_BAD_RE = re.compile(r'[:*?"<>|]')

TOO_MANY = f"添付できるファイルは、1通の返信に{MAX_COUNT}個までです"
TOO_LARGE = (f"添付ファイルは、1通の返信で合わせて{TOTAL_MAX_BYTES // _MB}MBまでです"
             "（大きい写真は枚数を減らすか、小さくしてから付けてください）")
EMPTY = "中身が空のファイル（0バイト）は添付できません"
NO_FILE = "ファイルが届きませんでした。もう一度選んでください"
BROKEN = "ファイルを受け取れませんでした。もう一度選んでください"
DISK_LOW = "サーバの空きが少ないため、ファイルを保存できません（管理者に知らせてください）"
SAVE_FAILED = "サーバにファイルを保存できませんでした（管理者に知らせてください）"
DB_BUSY = "サーバが混み合っていて、ファイルを付けられませんでした。少し待ってから、もう一度付けてください"
# 送信サーバに「メールが大きすぎる」と断られたとき (サーバの返事はこの後ろに添える)
SEND_TOO_LARGE = ("メールが大きすぎるため、送信サーバに断られました。"
                  "添付ファイルを減らすか、小さくしてから送り直してください")


class UploadError(Exception):
    """受け取れなかった (status は画面に返す HTTP の番号、文は画面にそのまま出す)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def clean_name(name, content_type: str = "") -> str:
    """相手に見えるファイル名に整える (日本語はそのまま).

    フォルダの部分・改行などの制御文字・文字の向きを変える記号を除き、長すぎれば拡張子を残して
    縮める (受信の添付と同じ mail.clean_filename)。Windows で使えない文字は「_」にする。
    名前が無ければ「添付ファイル.jpg」のように種類から付ける。
    """
    name = mail.clean_filename(name, content_type)
    return _WINDOWS_BAD_RE.sub("_", name)


def unique_name(name: str, taken: set) -> str:
    """同じ返信に同じ名前のファイルがあれば「名前 (2).jpg」のようにする.

    iPhone で写真を何枚か選ぶと、どれも「image.jpg」になることがあるため。
    taken は使っている名前 (小文字) で、決めた名前も足す。
    """
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, dot, ext = name, "", ""
    out, n = name, 1
    while out.lower() in taken:
        n += 1
        out = f"{stem} ({n}){dot}{ext}"
    taken.add(out.lower())
    return out


def content_type_for(name: str, declared) -> str:
    """メールに書くファイルの種類 (ブラウザが知らせた種類。無ければ名前の拡張子から).

    分からなければ「ただのデータ」(application/octet-stream)。message/* と multipart/* は、
    base64 で送ってはいけない決まりの種類なので「ただのデータ」として送る。
    """
    for value in (str(declared or "").split(";")[0], mimetypes.guess_type(name)[0] or ""):
        value = value.strip().lower()
        if (value and value != mail.OCTET_STREAM and mail._CONTENT_TYPE_RE.fullmatch(value)
                and value.split("/")[0] not in ("message", "multipart")):
            return value
    return mail.OCTET_STREAM


def mail_folder(reply_dir, mail_id: int) -> Path:
    return Path(reply_dir) / str(int(mail_id))


def file_path(reply_dir, rel_path: str) -> Path | None:
    """DB の path (「<mail_id>/<名前>」) から実際のファイルの場所を返す (形が違えば None)."""
    if not _PATH_RE.fullmatch(str(rel_path or "")):
        return None
    return Path(reply_dir) / rel_path


def file_info(row) -> dict:
    """画面に返すファイルの情報."""
    return {"id": row["id"], "filename": row["filename"], "content_type": row["content_type"],
            "size": row["size"], "uploaded_by": row["uploaded_by"],
            "sent": row["sent_at"] is not None}


def staged_rows(conn, mail_id: int) -> list:
    """まだ送っていない (返信の下書きに付けてある) ファイル (付けた順)."""
    return conn.execute("SELECT * FROM mail_reply_files WHERE mail_id = ? AND sent_at IS NULL"
                        " ORDER BY id", (mail_id,)).fetchall()


def sent_rows(conn, mail_id: int) -> list:
    """送った返信に付けたファイル (付けた順)."""
    return conn.execute("SELECT * FROM mail_reply_files WHERE mail_id = ? AND sent_at IS NOT NULL"
                        " ORDER BY id", (mail_id,)).fetchall()


def space_ok(reply_dir, size: int) -> bool:
    """この大きさのファイルを今保存してよいか (サーバの空きが少なくならないか).

    受信の添付と同じ歯止め (mail.min_free_bytes)。調べられないときは書いてみる。
    """
    try:
        usage = shutil.disk_usage(reply_dir)
    except OSError:
        return True
    return usage.free - size >= mail.min_free_bytes(usage.total)


class _Receiver:
    """multipart/form-data の部品を、届いた分から順にファイルへ書く (python-multipart の受け口)."""

    def __init__(self, folder: Path, max_count: int, max_bytes: int):
        self.folder, self.max_count, self.max_bytes = folder, max_count, max_bytes
        self.items: list[dict] = []   # 書き終えたファイル
        self.total = 0
        self._name, self._value, self._headers = b"", b"", {}
        self._cur: dict | None = None
        self._file = None

    def callbacks(self) -> dict:
        return {"on_part_begin": self.on_part_begin, "on_header_field": self.on_header_field,
                "on_header_value": self.on_header_value, "on_header_end": self.on_header_end,
                "on_headers_finished": self.on_headers_finished,
                "on_part_data": self.on_part_data, "on_part_end": self.on_part_end}

    def on_part_begin(self):
        self._headers, self._cur = {}, None

    def on_header_field(self, data, start, end):
        self._name += data[start:end]

    def on_header_value(self, data, start, end):
        self._value += data[start:end]

    def on_header_end(self):
        self._headers[self._name.lower()] = self._value
        self._name, self._value = b"", b""

    def on_headers_finished(self):
        _, opts = parse_options_header(self._headers.get(b"content-disposition", b""))
        if b"filename" not in opts:
            return   # ファイルではない欄 (読み飛ばす)
        if len(self.items) >= self.max_count:
            raise UploadError(400, TOO_MANY)
        raw_name = opts[b"filename"].decode("utf-8", "replace")
        declared = self._headers.get(b"content-type", b"").decode("latin-1")
        content_type = content_type_for(raw_name, declared)
        token = secrets.token_hex(16)
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            self._file = open(self.folder / token, "xb")
        except OSError as e:
            print(f"返信の添付ファイルを保存できませんでした: {e}")
            raise UploadError(507, SAVE_FAILED) from None
        self._cur = {"token": token, "path": self.folder / token, "raw_name": raw_name,
                     "filename": clean_name(raw_name, content_type),
                     "content_type": content_type, "size": 0}

    def on_part_data(self, data, start, end):
        if self._cur is None:
            return
        size = end - start
        if self.total + size > self.max_bytes:
            raise UploadError(413, TOO_LARGE)
        try:
            self._file.write(data[start:end])
        except OSError as e:   # ディスクがいっぱい等
            print(f"返信の添付ファイルを保存できませんでした: {e}")
            raise UploadError(507, SAVE_FAILED) from None
        self._cur["size"] += size
        self.total += size

    def on_part_end(self):
        if self._cur is None:
            return
        cur, self._cur = self._cur, None
        self._file.close()
        self._file = None
        if cur["size"]:
            self.items.append(cur)
            return
        cur["path"].unlink(missing_ok=True)
        if cur["raw_name"]:
            raise UploadError(400, EMPTY)
        # 名前も中身も無い部品は、ファイルを選んでいない欄 (読み飛ばす)

    def discard(self):
        """書いたファイルを全部消す (受け取れなかったとき)."""
        if self._file is not None:
            self._file.close()
            self._file = None
        for item in [*self.items, *([self._cur] if self._cur else [])]:
            try:
                item["path"].unlink(missing_ok=True)
            except OSError:
                pass
        self.items, self._cur = [], None


async def receive(headers, stream, folder: Path, max_count: int, max_bytes: int) -> list[dict]:
    """送られてきたファイル (multipart/form-data。1つでもいくつでも) を folder に書く.

    stream は届いた順のバイト列 (Starlette の request.stream())。少しずつ書くので、メモリには
    大きなファイルを丸ごと持たない。数が max_count を、合わせた大きさが max_bytes を超えたら、
    そこで受け取りをやめて (残りは読まない) UploadError にする。中身の空のファイルも断る。
    失敗したときは、この回に書いたファイルを全部消す。書いたものは
    {token, path, filename (整えた名前), content_type, size} の一覧で返す。
    """
    ctype, params = parse_options_header(headers.get("content-type", ""))
    if ctype != b"multipart/form-data" or not params.get(b"boundary"):
        raise UploadError(400, NO_FILE)
    rx = _Receiver(Path(folder), max_count, max_bytes)
    read, limit = 0, max_bytes + REQUEST_OVERHEAD
    try:
        parser = MultipartParser(params[b"boundary"], rx.callbacks())
        async for chunk in stream:
            read += len(chunk)
            if read > limit:   # 中身のほかに大きなものを付けて送ってきた
                raise UploadError(413, TOO_LARGE)
            parser.write(chunk)
        parser.finalize()
        if rx._cur is not None:   # 最後のファイルの途中で終わった (送るのをやめた)
            raise UploadError(400, BROKEN)
    except UploadError:
        rx.discard()
        raise
    except Exception as e:  # noqa: BLE001  壊れた送り方・途中で切れた
        rx.discard()
        print(f"返信の添付ファイルを受け取れませんでした: {e}")
        raise UploadError(400, BROKEN) from None
    if not rx.items:
        raise UploadError(400, NO_FILE)
    return rx.items


def remove_files(paths) -> None:
    """ファイルを消す (無ければ何もしない。消せなくても止めない。残りは片付けで消す)."""
    for path in paths:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            print(f"返信の添付ファイルを削除できませんでした ({path}): {e}")


def remove_mail_files(reply_dir, mail_id: int) -> None:
    """そのメールの返信の添付ファイルをフォルダごと消す (無ければ何もしない)."""
    folder = mail_folder(reply_dir, mail_id)
    try:
        shutil.rmtree(folder)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"返信の添付ファイルを削除できませんでした ({folder}): {e}")


def remove_orphans(conn, reply_dir) -> int:
    """持ち主のいないファイルを片付け、消したフォルダとファイルの数を返す.

    - メールの行が無くなったフォルダ (添付を付けられない前の版に戻した間に、その版が保存日数で
      メールを消した。DB の行は外部キーで一緒に消えている) はフォルダごと消す。取り込み中の
      メールと見誤らないよう、これまでに確定した一番大きい番号以下だけ (受信の添付と同じ)
    - DB に行の無いファイル (受け取りの途中でサーバが止まった) は、1日たったものだけ消す
      (受け取っている最中のものを消さないように)
    読むだけで、DB には書かない (定期受信のたびに呼ぶので、書き込みのロックを取らない)。
    """
    base = Path(reply_dir)
    try:
        folders = [p for p in base.iterdir() if p.is_dir() and p.name.isdigit()]
    except OSError:
        return 0
    if not folders:
        return 0
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'mails'").fetchone()
    newest = int(row[0]) if row else 0
    ids = {r[0] for r in conn.execute("SELECT id FROM mails")}
    known = {r[0] for r in conn.execute("SELECT path FROM mail_reply_files")}
    removed = 0
    for folder in sorted(folders, key=lambda p: int(p.name)):
        mail_id = int(folder.name)
        if mail_id <= newest and mail_id not in ids:
            remove_mail_files(base, mail_id)
            removed += 1
            continue
        try:
            files = [f for f in folder.iterdir() if f.is_file()]
        except OSError:
            continue
        for f in files:
            try:
                stale = time.time() - f.stat().st_mtime > _STALE_SECONDS
            except OSError:
                continue
            if stale and f"{mail_id}/{f.name}" not in known:
                remove_files([f])
                removed += 1
    return removed


def describe(rows) -> str:
    """操作の記録に残す添付ファイルの数と名前 (「2(見積書.pdf, 写真.jpg)」)."""
    return f"{len(rows)}({', '.join(r['filename'] for r in rows)})"
