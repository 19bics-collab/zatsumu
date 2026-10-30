"""よく使うアドレス (アドレス帳) と、宛先・CC・BCC の候補.

返信画面の宛先・CC・BCC の欄に打ち始めると、候補を出す (/api/mail/address-suggest):

1. アドレス帳 (管理者が登録した、よく使うアドレス)。名前・アドレス・メモと、そのアドレスを
   まとめた取引先の名前で探す
2. 前にやり取りした人。届いたメールの相手 (HPのフォームから届いたメールは本文のお客様) と、
   こちらが返信を送った宛先・CC・BCC。やり取りの多い人・最近やり取りした人ほど上に出す

自分たちのアドレス (受信するアドレス・差出人) と、フォーム・自動送信の送信元 (wordpress@・
noreply@ など。送っても人に届かない) は、前にやり取りした人の候補には出さない。
探すのは索引だけ (届いたメールは idx_mails_clients、送った返信は idx_mails_sent_to) なので、
メールが何万通あっても本文の大きい表は読まない。
"""
import re
import unicodedata
from datetime import datetime, timezone

from . import clients, mail

# 名前・メモの最大文字数と、登録できる数
NAME_MAX_CHARS = 60
NOTE_MAX_CHARS = 200
BOOK_MAX_COUNT = 2000
# 候補の数 (既定と最大) と、探す言葉の最大文字数
SUGGEST_LIMIT = 8
SUGGEST_LIMIT_MAX = 20
QUERY_MAX_CHARS = 100
# 取引先の名前で探すとき、合う取引先をいくつまで見るか (短い言葉で全部の取引先に合わないように)
CLIENT_MATCH_MAX = 20
# 最近やり取りした人を上にするときの目安 (この日数たつと、1回分の重みが半分になる)
RECENCY_DAYS = 30
# 前にやり取りした人を探すとき、見る数の上限 (届いたメールの相手の人数・送った返信の通数)
HISTORY_CANDIDATES = 300
SENT_ROWS_MAX = 2000


class AddressBookError(ValueError):
    """画面にそのまま出せる日本語の理由 (status は HTTP の番号)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- 登録する内容を確かめる ----------

def clean_email(value) -> tuple[str, str]:
    """アドレスの欄を確かめて (アドレス, 名前) を返す (おかしければ AddressBookError).

    返信画面の CC と同じ確かめ方。「山田 太郎 <taro@example.com>」の形 (メールソフトから
    コピーしたもの) も受け付け、そのときは名前も返す (名前の欄が空なら、それを使う)。
    """
    text = str(value or "")
    if re.search(r"[\r\n\x00]", text):
        raise AddressBookError("アドレスに改行は入れられません")
    text = unicodedata.normalize("NFKC", text).strip()
    if not text:
        raise AddressBookError("メールアドレスを入れてください")
    if re.search(r"[,;、]", re.sub(r'"[^"]*"', "", text)):
        raise AddressBookError("アドレスは1つだけ入れてください（何人もいるときは、1人ずつ追加してください）")
    addr, name = text, ""
    if "<" in text or ">" in text:
        mt = re.fullmatch(r"([^<>]*)<([^<>]*)>", text)
        addr = mt.group(2).strip() if mt else ""
        name = mt.group(1).strip().strip('"').strip() if mt else ""
    if len(text) > mail.RECIPIENT_MAX_CHARS or not mail.valid_address(addr):
        shown = text if len(text) <= 60 else text[:60] + "…"
        raise AddressBookError(
            f"アドレス「{shown}」が正しくありません。「name@example.com」の形で入れてください")
    return addr, name


def clean_name(value) -> str:
    """名前 (1行。空でもよい)。「名前 <アドレス>」の形で欄に入れるので < > " は使えない."""
    name = " ".join(str(value or "").replace("\x00", "").split())
    if re.search(r'[<>"]', name):
        raise AddressBookError('名前に < > " は使えません')
    if len(name) > NAME_MAX_CHARS:
        raise AddressBookError(f"名前は{NAME_MAX_CHARS}文字以内にしてください")
    return name


def clean_note(value) -> str:
    note = " ".join(str(value or "").replace("\x00", "").split())
    if len(note) > NOTE_MAX_CHARS:
        raise AddressBookError(f"メモは{NOTE_MAX_CHARS}文字以内にしてください")
    return note


def _check_free(conn, email: str, entry_id: int | None = None) -> None:
    row = conn.execute("SELECT id, name FROM address_book WHERE lower(email) = lower(?)"
                       " AND id != ?", (email, entry_id or 0)).fetchone()
    if row:
        who = f"（{row['name']}）" if row["name"] else ""
        raise AddressBookError(f"{email} はもうアドレス帳にあります{who}", 409)


# ---------- 登録・変更・削除 (管理者だけ。app.py で確かめる) ----------

def get_entry(conn, entry_id: int):
    row = conn.execute("SELECT * FROM address_book WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise AddressBookError("アドレス帳に見つかりません（ほかの人が消したかもしれません）", 404)
    return row


def entry_info(row, g: "clients.Grouping | None" = None) -> dict:
    """1件分 (取引先にまとめたアドレスなら、その取引先の名前も付ける)."""
    named = clients.named_client_of(g, row["email"]) if g is not None else None
    return {"id": row["id"], "name": row["name"], "email": row["email"], "note": row["note"],
            "client": {"key": named["key"], "name": named["name"]} if named else None,
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def list_entries(conn) -> list[dict]:
    """全部 (名前の順。名前の無いものはアドレスの順で後ろ)."""
    g = clients.Grouping(conn)
    rows = conn.execute(
        "SELECT * FROM address_book ORDER BY name = '', name, lower(email)").fetchall()
    return [entry_info(r, g) for r in rows]


def create_entry(conn, name, email, note="") -> dict:
    addr, name_in_addr = clean_email(email)
    name = clean_name(name) or clean_name(name_in_addr)
    note = clean_note(note)
    _check_free(conn, addr)
    if conn.execute("SELECT COUNT(*) FROM address_book").fetchone()[0] >= BOOK_MAX_COUNT:
        raise AddressBookError(f"アドレス帳は{BOOK_MAX_COUNT}件までです。"
                               "使わないものを消してから追加してください")
    now = _now()
    cur = conn.execute(
        "INSERT INTO address_book (name, email, note, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)", (name, addr, note, now, now))
    return entry_info(get_entry(conn, cur.lastrowid), clients.Grouping(conn))


def update_entry(conn, entry_id: int, name=None, email=None, note=None) -> dict:
    row = get_entry(conn, entry_id)
    if name is None and email is None and note is None:
        raise AddressBookError("変更する内容がありません")
    fields = {"name": row["name"], "email": row["email"], "note": row["note"]}
    if email is not None:
        addr, name_in_addr = clean_email(email)
        _check_free(conn, addr, entry_id)
        fields["email"] = addr
        if name is None and not row["name"] and name_in_addr:
            fields["name"] = clean_name(name_in_addr)
    if name is not None:
        fields["name"] = clean_name(name)
    if note is not None:
        fields["note"] = clean_note(note)
    conn.execute("UPDATE address_book SET name = ?, email = ?, note = ?, updated_at = ?"
                 " WHERE id = ?", (fields["name"], fields["email"], fields["note"], _now(),
                                   entry_id))
    return entry_info(get_entry(conn, entry_id), clients.Grouping(conn))


def delete_entry(conn, entry_id: int) -> dict:
    row = get_entry(conn, entry_id)
    conn.execute("DELETE FROM address_book WHERE id = ?", (entry_id,))
    return dict(row)


def has_address(conn, addr) -> bool:
    """アドレス帳にあるか (返信画面の「アドレス帳に追加」を出すか決める用)."""
    addr = str(addr or "").strip()
    return bool(addr) and conn.execute(
        "SELECT 1 FROM address_book WHERE lower(email) = lower(?)", (addr,)).fetchone() is not None


# ---------- 候補 ----------

def _fold(value) -> str:
    """比べる形 (全角・半角と英字の大文字・小文字をそろえ、空白を除く)."""
    return "".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _words(value) -> list[str]:
    return [_fold(w) for w in re.split(r"[\s@._+\-・/()（）]+", unicodedata.normalize(
        "NFKC", str(value or ""))) if w]


def _match(needle: str, fields) -> int | None:
    """合い方 (0 = どれかの言葉の先頭から合う / 1 = 途中に入っている / None = 合わない)."""
    hit = None
    for f in fields:
        if not f:
            continue
        if needle in _fold(f):
            if _fold(f).startswith(needle) or any(w.startswith(needle) for w in _words(f)):
                return 0
            hit = 1
    return hit


def _escape(term: str) -> str:
    """LIKE の % と _ を、ただの文字として探す形にする (ESCAPE '\\' と使う)."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like(term: str) -> str:
    return "%" + _escape(term) + "%"


def _age_days(iso, now: datetime) -> float:
    try:
        at = datetime.fromisoformat(str(iso))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return max(0.0, (now - at).total_seconds() / 86400)
    except (TypeError, ValueError):
        return 3650.0


def _score(n: int, age_days: float) -> float:
    """やり取りの数 × 最近か (RECENCY_DAYS 日前なら半分、その倍なら3分の1 …)."""
    return n / (1.0 + age_days / RECENCY_DAYS)


def _client_terms(g: "clients.Grouping", needle: str) -> tuple[list, list]:
    """名前が合う、名前を付けた取引先の (アドレスの決まり, ドメインの決まり)."""
    addrs, domains, count = [], [], 0
    for cid, c in g.clients.items():
        if needle not in _fold(c["name"]):
            continue
        for p in g.rules_of.get(cid, []):
            (domains.append(p["pattern"][1:]) if p["pattern"].startswith("@")
             else addrs.append(p["pattern"]))
        count += 1
        if count >= CLIENT_MATCH_MAX:
            break
    return addrs, domains


def _history(conn, q: str, needle: str, c_addrs, c_domains) -> dict[str, dict]:
    """前にやり取りした人 {アドレス (小文字): {addr, name, n, last_at}} (言葉が合うものだけ).

    名前を付けた取引先の名前が合えば (c_addrs・c_domains)、その取引先にまとめたアドレス・
    ドメインの人も入れる。1〜2文字の言葉で何千人にも合うときに遅くならないよう、届いたメールの
    相手は「やり取りの数 × 最近か」の大きい順に HISTORY_CANDIDATES 人まで、送った返信は
    新しい順に SENT_ROWS_MAX 通までを見る。
    """
    terms = {q.strip(), unicodedata.normalize("NFKC", q).strip()} - {""}
    out: dict[str, dict] = {}

    def add(addr: str, n: int, last_at, name: str = ""):
        a = clients.ascii_lower(addr).strip()
        if not a:
            return
        st = out.get(a)
        if st is None:
            st = out[a] = {"addr": addr.strip(), "name": "", "n": 0, "last_at": None,
                           "name_at": ""}
        st["n"] += n
        if last_at and (st["last_at"] is None or last_at > st["last_at"]):
            st["last_at"] = last_at
        if name and (last_at or "") >= st["name_at"]:
            st["name"], st["name_at"] = name, last_at or ""

    # 届いたメールの相手 (索引 idx_mails_clients だけで数える。名前は一番新しいもの)
    where, params = [], []
    for t in terms:
        like = _like(t)
        where.append("contact_addr LIKE ? ESCAPE '\\' OR contact_name LIKE ? ESCAPE '\\'")
        params += [like, like]
    if c_domains:
        where.append("contact_domain IN (%s)" % ",".join("?" * len(c_domains)))
        params += c_domains
        for d in c_domains:   # 下のドメイン (tokyo.abc.co.jp) も
            where.append("contact_domain LIKE ? ESCAPE '\\'")
            params.append("%." + _escape(d))
    if c_addrs:
        where.append("lower(contact_addr) IN (%s)" % ",".join("?" * len(c_addrs)))
        params += c_addrs
    at = "MAX(min(received_at, fetched_at))"
    sql = (
        f"SELECT contact_addr AS a, COUNT(*) AS n, {at} AS last_at,"
        " MAX(CASE WHEN contact_name != '' THEN min(received_at, fetched_at) || char(31)"
        " || contact_name END) AS name_at"
        " FROM mails WHERE contact_addr != '' AND (" + " OR ".join(where) + ")"
        " GROUP BY contact_domain, contact_addr"
        # _score と同じ計算 (日付が読めないものは後ろ)
        f" ORDER BY COUNT(*) / (1.0 + max(0, julianday('now') - julianday({at})) / ?) DESC"
        " LIMIT ?")
    for r in conn.execute(sql, [*params, RECENCY_DAYS, HISTORY_CANDIDATES]):
        name_at = r["name_at"] or ""
        name = name_at.split("\x1f", 1)[1] if "\x1f" in name_at else ""
        add(r["a"], r["n"], r["last_at"], name)
    # こちらが返信を送った宛先・CC・BCC (索引 idx_mails_sent_to だけで探す)。
    # 宛先が空のものは前の版が差出人に送った返信 (差出人は上の相手として数えている)
    where, params = [], []
    for t in terms:
        like = _like(t)
        where.append("reply_sent_to LIKE ? ESCAPE '\\' OR reply_cc LIKE ? ESCAPE '\\'"
                     " OR reply_bcc LIKE ? ESCAPE '\\'")
        params += [like, like, like]
    for r in conn.execute(
            "SELECT replied_at, reply_sent_to, reply_cc, reply_bcc FROM mails"
            " WHERE replied_at IS NOT NULL AND (" + " OR ".join(where) + ")"
            " ORDER BY replied_at DESC LIMIT ?", [*params, SENT_ROWS_MAX]):
        for addr in [r["reply_sent_to"], *mail.split_addresses(r["reply_cc"]),
                     *mail.split_addresses(r["reply_bcc"])]:
            # 送った宛先はアドレスだけを残している (念のため「名前 <アドレス>」もアドレスにする)
            mt = re.search(r"<([^<>]*)>", addr)
            addr = mt.group(1).strip() if mt else addr.strip()
            if addr and needle in _fold(addr):
                add(addr, 1, r["replied_at"])
    return out


def suggest(conn, q: str, limit: int = SUGGEST_LIMIT) -> list[dict]:
    """宛先・CC・BCC の候補 (アドレス帳が先、次に前にやり取りした人).

    1件ずつ {name, email, note, client, source ("book" / "history"), count, last_at}。
    どちらも、名前・アドレスなどの言葉の先頭から合うものを、途中に入っているものより上にする。
    アドレス帳は名前の順、前にやり取りした人は「やり取りの数 × 最近か」(_score) の大きい順。
    前にやり取りした人がアドレス帳にもあれば、アドレス帳の名前・メモで出す。
    """
    needle = _fold(q)
    if not needle:
        return []
    g = clients.Grouping(conn)
    rules = g.rules
    c_addrs, c_domains = _client_terms(g, needle)
    client_hit = bool(c_addrs or c_domains)

    def client_name(addr) -> str:
        named = clients.named_client_of(g, addr)
        return named["name"] if named else ""

    out: list[dict] = []
    seen: set[str] = set()
    book: dict[str, object] = {}
    hits = []
    for r in conn.execute("SELECT * FROM address_book"):
        book[clients.ascii_lower(r["email"])] = r
        rank = _match(needle, [r["name"], r["email"], r["note"]])
        if rank is None and client_hit:
            rank = _match(needle, [client_name(r["email"])])
        if rank is not None:
            hits.append((rank, _fold(r["name"]) or "\uffff", r["email"].lower(), r))
    for _rank, _n, _e, r in sorted(hits, key=lambda x: x[:3]):
        if len(out) >= limit:
            return out
        seen.add(clients.ascii_lower(r["email"]))
        out.append({"name": r["name"], "email": r["email"], "note": r["note"],
                    "client": client_name(r["email"]), "source": "book", "count": 0,
                    "last_at": None})
    now = datetime.now(timezone.utc)
    ranked = []
    for a, st in _history(conn, q, needle, c_addrs, c_domains).items():
        if a in seen:
            continue
        entry = book.get(a)
        name = entry["name"] if entry is not None and entry["name"] else st["name"]
        # アドレス帳の名前・メールに書かれていた名前・アドレス・取引先の名前のどれかに合うもの
        rank = _match(needle, [name, st["name"], st["addr"]])
        if rank is None and client_hit:
            rank = _match(needle, [client_name(a)])
        if rank is None:
            continue
        age = _age_days(st["last_at"], now)
        ranked.append((rank, -_score(st["n"], age), age, a, st, entry, name))
    ranked.sort(key=lambda x: x[:4])
    for _rank, _s, _age, a, st, entry, name in ranked:
        if len(out) >= limit:
            break
        # 自分たちのアドレスと、フォーム・自動送信の送信元 (送っても人に届かない) は出さない
        if a in rules.own or rules.automated(a) or not mail.valid_address(st["addr"]):
            continue
        out.append({"name": name, "email": entry["email"] if entry is not None else st["addr"],
                    "note": entry["note"] if entry is not None else "",
                    "client": client_name(a), "source": "book" if entry is not None else "history",
                    "count": st["n"], "last_at": st["last_at"]})
    return out
