"""取引先 (取引先ごとに、過去のやり取りをまとめて見る).

メールの相手 (mails.contact_addr。HPのフォームから届いたメールは本文のお客様) から、
次の決まりで「取引先」にまとめる (いつ見ても同じ結果になり、なぜその取引先かを説明できるように):

1. 自分たちのアドレス・自社のドメイン・社内の人 (登録している人のアドレス)・フォームや自動送信の
   送信元 (wordpress@・noreply@ など) は取引先にしない。相手のアドレスが分からないメール
   (フォームで本文にアドレスが無い) も入れない
2. 管理者がアドレス (taro@abc.co.jp) を決めて入れた取引先があれば、その取引先
3. 管理者がドメイン (@abc.co.jp) を決めて入れた取引先があれば、その取引先。下のドメイン
   (tokyo.abc.co.jp) も入る。いくつも合うときは一番長く合うもの (@tokyo.abc.co.jp を別の取引先に
   入れていれば、そちら)
4. 決めていなければ、会社のドメインはドメインごとに1つ (tanaka@abc.co.jp と suzuki@abc.co.jp は
   「abc.co.jp」)。Gmail などの個人のメール・予約サイトの中継のアドレス (設定
   mail_client_personal_domains) は、アドレスごとに1人ずつ

名前を付けていない取引先は表に入れず、見るたびにメールから数える (メールが1通も無いドメイン・
アドレスの番号は「見つかりません」にする) (メールが何万通でも、
索引 idx_mails_clients だけを読む)。取引先を作る・まとめる・分ける・消すのはどれも
clients / client_rules の表だけを変え、メールは変えない (消すと、決まった分け方に戻るだけ)。

取引先の番号 (key) は URL にそのまま入れられる形:
  c:<番号>          名前を付けた取引先 (clients.id)
  d:<ドメイン>      名前を付けていない会社 (ドメインごと)
  a:<アドレス>      名前を付けていない個人 (アドレスごと)
ドメイン・アドレスの英小文字・数字・「.」「_」「+」「-」「@」以外の文字は「~」と16進の2桁にする。
"""
import re
import unicodedata
from datetime import datetime, timezone
from email.utils import parseaddr

from . import db, mail

# 取引先の名前・メモの最大文字数
NAME_MAX_CHARS = 60
NOTE_MAX_CHARS = 2000
# 1つの取引先に入れられるアドレス・ドメインの数と、取引先の数の上限 (打ち間違いの事故で
# 表が大きくならないように)
RULES_PER_CLIENT_MAX = 200
CLIENT_MAX_COUNT = 5000
# 設定「1人ずつに分けるドメイン」の最大文字数・件数
PERSONAL_DOMAINS_MAX_CHARS = 5000
PERSONAL_DOMAINS_MAX_COUNT = 300
# 一覧・やり取りの1回に出す数 (既定と上限)
LIST_LIMIT = 50
LIST_LIMIT_MAX = 200
TIMELINE_LIMIT = 30
TIMELINE_LIMIT_MAX = 100
# 一覧に出すアドレスの数 (残りは「ほか N 件」)・詳しい画面に出すアドレスの数
LIST_ADDRESSES = 3
DETAIL_ADDRESSES = 100
# やり取りの書き出しの文字数
SNIPPET_CHARS = 120
# 検索の言葉の最大文字数
QUERY_MAX_CHARS = 100
# 「@co.jp」のように、ドメインでまとめると関係の無い会社まで全部入ってしまうもの
# (「.jp」などの国の部分と、その前の co・ne・or などだけのドメイン)
_SHARED_SECOND_LEVEL = frozenset((
    "co", "ne", "or", "ac", "ad", "ed", "go", "gr", "lg", "com", "net", "org", "gov", "edu",
    "ltd", "plc", "biz", "info",
))

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def ascii_lower(value) -> str:
    """英字だけを小文字にする (SQLite の lower() と同じ。DB で比べた結果とそろえる)."""
    return str(value or "").strip().translate(_ASCII_LOWER)


def domain_of(addr) -> str:
    """アドレスの @ の後ろ (小文字)。db.CONTACT_DOMAIN_SQL と同じ結果 (@ が無ければ "")."""
    a = str(addr or "")
    i = a.find("@")
    return ascii_lower(a[i + 1:]) if i >= 0 else ""


def _in_domains(domain: str, domains) -> bool:
    """domain がどれかのドメインか、その下 (guest.airbnb.com は airbnb.com の下) か.

    domain の後ろの部分 (guest.airbnb.com → airbnb.com → com) を1つずつ探す
    (一覧が長くても、アドレスが何千あっても速い)。domains は set など。
    """
    parts = domain.split(".")
    return any(".".join(parts[i:]) in domains for i in range(len(parts)))


def _too_broad(domain: str) -> bool:
    """ドメインでまとめると、別々の会社まで入ってしまうか (「jp」「co.jp」「com.cn」など)."""
    parts = domain.split(".")
    return len(parts) < 2 or (len(parts) == 2 and parts[0] in _SHARED_SECOND_LEVEL)


class ClientError(ValueError):
    """画面にそのまま出せる日本語の理由 (status は API で返す番号)."""
    status = 400


class ClientNotFound(ClientError):
    status = 404


class ClientConflict(ClientError):
    status = 409


# ---------- 設定「1人ずつに分けるドメイン」 ----------

def _domain_items(text) -> list[str]:
    text = mail.normalize_newlines(unicodedata.normalize("NFKC", str(text or "")))
    out = []
    for item in re.split(r"[\n,，、\s]+", text):
        item = ascii_lower(item).lstrip("@").rstrip(".")
        if item and item not in out:
            out.append(item)
    return out


def personal_domains(settings: dict) -> frozenset:
    """設定の「1人ずつに分けるドメイン」(読めない行は飛ばす)."""
    return frozenset(d for d in _domain_items(settings.get("mail_client_personal_domains"))
                     if mail.valid_address("x@" + d))


def clean_personal_domains(text) -> str:
    """設定「1人ずつに分けるドメイン」を確かめて、1行に1つの形にそろえる.

    「gmail.com」「@gmail.com」のどちらでもよい。空にはできない (空だと Gmail の人が全員
    1つの取引先になってしまう)。おかしなものがあれば、画面にそのまま出せる日本語の ValueError。
    """
    if len(str(text or "")) > PERSONAL_DOMAINS_MAX_CHARS:
        raise ValueError(f"1人ずつに分けるドメインは{PERSONAL_DOMAINS_MAX_CHARS}文字以内にしてください")
    items = _domain_items(text)
    if not items:
        raise ValueError("1人ずつに分けるドメインを1つ以上入れてください"
                         "（最初の一覧に戻すときは「最初の一覧に戻す」を押してください）")
    for d in items:
        if not mail.valid_address("x@" + d):
            shown = d if len(d) <= 60 else d[:60] + "…"
            raise ValueError(f"ドメイン「{shown}」が正しくありません。"
                             "「gmail.com」のように、1行に1つ入れてください")
    if len(items) > PERSONAL_DOMAINS_MAX_COUNT:
        raise ValueError(f"1人ずつに分けるドメインは{PERSONAL_DOMAINS_MAX_COUNT}件までです")
    return "\n".join(items)


# ---------- 取引先の番号 (key) ----------

_SAFE = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._+-@")


def _enc(value: str) -> str:
    return "".join(c if c in _SAFE else "".join(f"~{b:02x}" for b in c.encode("utf-8"))
                   for c in value)


def _dec(value: str) -> str:
    out, i = bytearray(), 0
    while i < len(value):
        c = value[i]
        if c == "~":
            if not re.fullmatch(r"[0-9a-f]{2}", value[i + 1:i + 3]):
                raise ClientNotFound("取引先が見つかりません")
            out.append(int(value[i + 1:i + 3], 16))
            i += 3
        elif c in _SAFE:
            out += c.encode()
            i += 1
        else:
            raise ClientNotFound("取引先が見つかりません")
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        raise ClientNotFound("取引先が見つかりません") from None


def client_key(client_id: int) -> str:
    return f"c:{int(client_id)}"


def domain_key(domain: str) -> str:
    return "d:" + _enc(domain)


def address_key(addr: str) -> str:
    return "a:" + _enc(addr)


def parse_key(key) -> tuple[str, object]:
    """番号を (種類, 値) にする。種類は c (値は番号)・d (ドメイン)・a (アドレス)."""
    kind, sep, value = str(key or "").partition(":")
    if not sep or not value or len(value) > 1000:
        raise ClientNotFound("取引先が見つかりません")
    if kind == "c":
        if not (value.isascii() and value.isdigit()) or len(value) > 12:
            raise ClientNotFound("取引先が見つかりません")
        return "c", int(value)
    if kind in ("d", "a"):
        decoded = _dec(value)
        if kind == "a" and "@" not in decoded:
            raise ClientNotFound("取引先が見つかりません")
        return kind, decoded
    raise ClientNotFound("取引先が見つかりません")


# ---------- まとめ方 ----------

class Grouping:
    """取引先にまとめる決まり (リクエストごとに作る。読むだけ).

    rules は自分たちのアドレス・社内の人・フォームの送信元 (server/mail.py の ContactRules)、
    personal は1人ずつに分けるドメイン、exact / domains は管理者が決めたアドレス・ドメインの
    取引先 (pattern → 取引先の番号)。
    """

    def __init__(self, conn, settings: dict | None = None):
        self.conn = conn
        s = settings if settings is not None else db.get_settings(conn)
        self.rules = mail.contact_rules(conn, s)
        self.personal = personal_domains(s)
        # 自社のドメイン (受信するアドレスなどのドメイン。Gmail などは入れない)
        self.own_domains = {d for d in self.rules.own_domains
                            if not _in_domains(d, self.personal)}
        self._personal_cache: dict[str, bool] = {}
        self._domain_cache: dict[str, int | None] = {}
        self.clients = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, name, note, created_at, updated_at FROM clients")}
        self.exact: dict[str, int] = {}
        self.domains: dict[str, int] = {}
        self.rules_of: dict[int, list[dict]] = {cid: [] for cid in self.clients}
        for r in conn.execute("SELECT id, client_id, pattern FROM client_rules ORDER BY id"):
            if r["client_id"] not in self.clients:
                continue
            p = r["pattern"]
            if p.startswith("@"):
                self.domains[p[1:]] = r["client_id"]
            else:
                self.exact[p] = r["client_id"]
            self.rules_of[r["client_id"]].append({"id": r["id"], "pattern": p})

    def personal_domain(self, domain: str) -> bool:
        hit = self._personal_cache.get(domain)
        if hit is None:
            hit = self._personal_cache[domain] = _in_domains(domain, self.personal)
        return hit

    def domain_client(self, domain: str) -> int | None:
        """ドメインで決めた取引先の番号 (下のドメインにも当てる。一番長く合う決まり)。無ければ None."""
        if domain in self._domain_cache:
            return self._domain_cache[domain]
        hit = None
        if domain:
            parts = domain.split(".")
            for i in range(len(parts)):
                hit = self.domains.get(".".join(parts[i:]))
                if hit is not None:
                    break
        self._domain_cache[domain] = hit
        return hit

    def excluded(self, addr: str) -> bool:
        """取引先にしないアドレスか (自分たち・自社のドメイン・社内の人・フォームや自動送信の送信元)."""
        a = ascii_lower(addr)
        if "@" not in a:
            return True
        if (a in self.rules.own or a in self.rules.people
                or _in_domains(domain_of(a), self.own_domains)):
            return True
        return self.rules.automated(a)

    def key_for(self, addr, domain: str | None = None) -> str | None:
        """アドレスの取引先の番号 (取引先にしないアドレスなら None)."""
        a = ascii_lower(addr)
        if not a or self.excluded(a):
            return None
        d = domain_of(a) if domain is None else domain
        if a in self.exact:
            return client_key(self.exact[a])
        cid = self.domain_client(d)
        if cid is not None:
            return client_key(cid)
        if self.personal_domain(d):
            return address_key(a)
        return domain_key(d)

    def canonical(self, key) -> str | None:
        """番号を、今の決まりでの番号にする (名前を付けた後の古い番号でも開けるように)。無ければ None.

        名前を付けていない取引先 (d:… / a:…) は、メールが1通以上あるときだけ (打ち間違えた URL や、
        メールの無いドメインで、空の取引先を出したり、意味の無い決まりを作ったりしないように)。
        """
        kind, value = parse_key(key)
        if kind == "c":
            return key if value in self.clients else None
        if kind == "a":
            canon = self.key_for(value)
        else:
            d = value
            if not d or "@" in d:
                return None
            cid = self.domain_client(d)
            if cid is not None:
                return client_key(cid)
            if self.personal_domain(d) or _in_domains(d, self.own_domains):
                return None
            canon = domain_key(d)
        if canon is None or canon.startswith("c:"):
            return canon
        return canon if self._has_mail(canon) else None

    def _has_mail(self, key: str) -> bool:
        """名前を付けていない取引先 (d:… / a:…) のメールが1通以上あるか (索引 idx_mails_clients で探す)."""
        kind, value = parse_key(key)
        if kind == "a":
            return self.conn.execute(
                "SELECT 1 FROM mails WHERE contact_domain = ? AND lower(contact_addr) = ? LIMIT 1",
                (domain_of(value), value)).fetchone() is not None
        return any(self.key_for(r[0], value) == key for r in self.conn.execute(
            "SELECT DISTINCT contact_addr FROM mails WHERE contact_domain = ?", (value,)))

    def rule_domains_of(self, client_id: int) -> list[str]:
        """名前を付けた取引先にドメインの決まりで入る、メールのあるドメイン (下のドメインも).

        メールのドメインの種類だけを索引から読む (何万通あっても速い)。
        """
        if not any(p["pattern"].startswith("@") for p in self.rules_of.get(client_id, [])):
            return []
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT contact_domain FROM mails WHERE contact_domain != ''")
            if self.domain_client(r[0]) == client_id]


# 届いた時刻。差出人が付けた日付 (received_at) が、このサーバが取り込んだ時刻 (fetched_at) より
# 先なら取り込んだ時刻にする (迷惑メールや時計のずれた送り主の先の日付が、一覧の一番上に
# 居座らないように。受信箱の並び (server/app.py の _MAIL_SORT_TIME) と同じ考え)。
# どちらも UTC の ISO の文字なので、文字のまま比べられる
_AT_SQL = "min(received_at, fetched_at)"
# 相手のアドレスごとの数 (索引 idx_mails_clients だけで数える)。名前は一番新しい
# 名前の入ったメールの名前 (届いた時刻と名前をつなげた文字の一番大きいもの)
_SEP = "\x1f"
_STATS_SQL = (
    "SELECT contact_domain AS d, contact_addr AS a, COUNT(*) AS n,"
    " COALESCE(SUM(status = 'unhandled'), 0) AS unhandled,"
    " COALESCE(SUM(status = 'pending_approval'), 0) AS pending,"
    " COALESCE(SUM(replied_at IS NOT NULL), 0) AS replies,"
    f" MIN({_AT_SQL}) AS first_at, MAX({_AT_SQL}) AS last_at,"
    " MAX(replied_at) AS last_reply_at,"
    f" MAX(CASE WHEN contact_name != '' THEN {_AT_SQL} || char(31) || contact_name END) AS name_at,"
    f" MAX(CASE WHEN contact_company != '' THEN {_AT_SQL} || char(31) || contact_company END)"
    " AS company_at FROM mails"
)


def _max(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _min(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def address_stats(conn, where: str = "contact_addr != ''", params=()) -> dict[str, dict]:
    """相手のアドレス (英字は小文字) ごとの数・日時・名前 {アドレス: {...}}.

    大文字・小文字だけ違うアドレスは1つにまとめる (DB では別々に数えてから足す。
    索引の順に読むので、並べ替えの作業が要らない)。
    """
    out: dict[str, dict] = {}
    for r in conn.execute(_STATS_SQL + " WHERE " + where + " GROUP BY contact_domain, contact_addr",
                          params):
        a = ascii_lower(r["a"])
        if not a:
            continue
        st = out.get(a)
        if st is None:
            out[a] = {"addr": a, "domain": r["d"], "n": r["n"], "unhandled": r["unhandled"],
                      "pending": r["pending"], "replies": r["replies"], "first_at": r["first_at"],
                      "last_at": r["last_at"], "last_reply_at": r["last_reply_at"],
                      "name_at": r["name_at"], "company_at": r["company_at"]}
            continue
        for k in ("n", "unhandled", "pending", "replies"):
            st[k] += r[k]
        st["first_at"] = _min(st["first_at"], r["first_at"])
        for k in ("last_at", "last_reply_at", "name_at", "company_at"):
            st[k] = _max(st[k], r[k])
    return out


def _latest_value(stamped) -> str:
    return stamped.split(_SEP, 1)[1] if stamped and _SEP in stamped else ""


def _new_group(key: str) -> dict:
    return {"key": key, "addresses": {}, "n": 0, "unhandled": 0, "pending": 0, "replies": 0,
            "first_at": None, "last_at": None, "last_reply_at": None,
            "name_at": None, "company_at": None}


def _add(group: dict, st: dict) -> None:
    group["addresses"][st["addr"]] = st
    for k in ("n", "unhandled", "pending", "replies"):
        group[k] += st[k]
    group["first_at"] = _min(group["first_at"], st["first_at"])
    for k in ("last_at", "last_reply_at", "name_at", "company_at"):
        group[k] = _max(group[k], st[k])


def _finish(g: Grouping, group: dict, address_max: int = LIST_ADDRESSES) -> dict:
    """まとめた数から、画面に出す取引先の項目を作る."""
    key = group["key"]
    kind, value = parse_key(key)
    patterns = []
    if kind == "c":
        client = g.clients[value]
        patterns = g.rules_of.get(value, [])
        domains = [p["pattern"][1:] for p in patterns if p["pattern"].startswith("@")]
        ctype = "company" if domains else "person"
    elif kind == "d":
        domains, ctype = [value], "company"
    else:
        domains, ctype = [], "person"
    # 名前を付けていなければ: フォームの「会社名」(一番新しいもの)、相手の名前 (一番新しいもの)、
    # ドメインかアドレス
    default_name = (_latest_value(group["company_at"]) or _latest_value(group["name_at"])
                    or (value if kind != "c" else ""))
    addrs = sorted(group["addresses"].values(), key=lambda st: (-st["n"], st["addr"]))
    last = _max(group["last_at"], group["last_reply_at"])
    return {
        "key": key,
        "name": client["name"] if kind == "c" else default_name,
        "default_name": default_name,
        "named": kind == "c",
        "client_id": value if kind == "c" else None,
        "kind": ctype,
        "domains": domains,
        "addresses": [{"addr": st["addr"], "name": _latest_value(st["name_at"]),
                       "count": st["n"], "last_at": _max(st["last_at"], st["last_reply_at"])}
                      for st in addrs[:address_max]],
        "address_count": len(addrs),
        "total": group["n"],
        "unhandled": group["unhandled"],
        "pending": group["pending"],
        "replies": group["replies"],
        "first_at": group["first_at"],
        "last_received_at": group["last_at"],
        "last_at": last,
        "rules": patterns if kind == "c" else [],
    }


def _matches(item: dict, needle: str) -> bool:
    hay = [item["name"], item["default_name"], *item["domains"],
           *(a["addr"] for a in item["all_addresses"]), *(a["name"] for a in item["all_addresses"]),
           *(p["pattern"] for p in item["rules"])]
    return any(needle in unicodedata.normalize("NFKC", str(h)).casefold() for h in hay if h)


def list_clients(conn, q: str = "", offset: int = 0, limit: int = LIST_LIMIT,
                 settings: dict | None = None, named_only: bool = False) -> dict:
    """取引先の一覧 (最後にやり取りした日時の新しい順)。q で名前・アドレス・ドメインを探す.

    名前を付けた取引先は、メールがまだ無くても出す (後で消せるように)。named_only なら
    名前を付けた取引先だけ (「ほかの取引先にまとめる」の選択肢用)。
    """
    g = Grouping(conn, settings)
    groups: dict[str, dict] = {}
    for st in address_stats(conn).values():
        k = g.key_for(st["addr"], st["domain"])
        if k:
            group = groups.get(k)
            if group is None:
                group = groups[k] = _new_group(k)
            _add(group, st)
    for cid in g.clients:
        groups.setdefault(client_key(cid), _new_group(client_key(cid)))
    items = []
    for group in groups.values():
        if named_only and not group["key"].startswith("c:"):
            continue
        item = _finish(g, group, address_max=10 ** 9)
        item["all_addresses"] = item["addresses"]
        items.append(item)
    needle = unicodedata.normalize("NFKC", str(q or "")).strip().casefold()[:QUERY_MAX_CHARS]
    if needle:
        items = [it for it in items if _matches(it, needle)]
    items.sort(key=lambda it: (it["last_at"] or "", it["key"]), reverse=True)
    page = items[offset:offset + limit]
    for it in page:
        it.pop("all_addresses")
        it["addresses"] = it["addresses"][:LIST_ADDRESSES]
    return {"clients": page, "total": len(items), "offset": offset, "limit": limit}


# ---------- 1つの取引先 ----------

def _members(conn, g: Grouping, key: str) -> tuple[list, list, list]:
    """取引先のメールの条件 (A, D, E): 相手のアドレスが A のどれか、または
    相手のドメインが D のどれかで、アドレスが E (ほかの取引先・取引先にしないアドレス) でないもの.

    名前を付けた取引先の D は、ドメインの決まりに合う (下のドメインも入れた) メールのあるドメイン。
    """
    kind, value = parse_key(key)
    if kind == "a":
        return [value], [], []
    if kind == "d":
        addrs, domains = [], [value]
    else:
        addrs = [p["pattern"] for p in g.rules_of.get(value, [])
                 if not p["pattern"].startswith("@") and g.key_for(p["pattern"]) == key]
        domains = g.rule_domains_of(value)
    other = []
    if domains:
        seen = {ascii_lower(r[0]) for r in conn.execute(
            "SELECT DISTINCT contact_addr FROM mails WHERE contact_domain IN"
            f" ({_marks(domains)})", domains)}
        other = sorted(a for a in seen if a and g.key_for(a) != key)
    return addrs, domains, other


def _marks(values) -> str:
    return ",".join("?" * len(values))


def member_where(addrs, domains, other) -> tuple[str, list]:
    """_members の条件を SQL にする (どちらも無ければ、どのメールにも合わない条件)."""
    parts, params = [], []
    if domains:
        part = f"(contact_domain IN ({_marks(domains)})"
        params += domains
        if other:
            part += f" AND lower(contact_addr) NOT IN ({_marks(other)})"
            params += other
        parts.append(part + ")")
    if addrs:
        parts.append(f"lower(contact_addr) IN ({_marks(addrs)})")
        params += addrs
    if not parts:
        return "0", []
    return "(" + " OR ".join(parts) + ")", params


def _snippet(text) -> str:
    return mail._snippet(text, SNIPPET_CHARS)


def timeline(conn, where: str, params: list, offset: int, limit: int) -> list[dict]:
    """取引先とのやり取り (届いたメールと、こちらが送った返信) を新しい順に1ページ分.

    本文は書き出しだけ (全文は画面が /api/mail/{番号}?thread=false で読む)。届いたメールは、
    取り込んだ時刻より先の日付を取り込んだ時刻にして並べる (出す日時は差出人が付けた日付のまま)。
    """
    events = conn.execute(
        f"SELECT kind, id, at FROM (SELECT 'in' AS kind, id, {_AT_SQL} AS at FROM mails"
        f" WHERE {where} UNION ALL SELECT 'out' AS kind, id, replied_at AS at FROM mails"
        f" WHERE {where} AND replied_at IS NOT NULL)"
        " ORDER BY at DESC, id DESC, kind DESC LIMIT ? OFFSET ?",
        (*params, *params, limit, offset)).fetchall()
    ids = sorted({e["id"] for e in events})
    rows = {}
    if ids:
        rows = {r["id"]: r for r in conn.execute(
            "SELECT id, subject, substr(body, 1, 600) AS head, received_at, status, category,"
            " from_addr, from_name, contact_addr, contact_name, contact_source, contact_via_form,"
            " reply_subject, substr(reply_body, 1, 600) AS reply_head, replied_at, replied_by,"
            " reply_sent_to, reply_cc"
            f" FROM mails WHERE id IN ({_marks(ids)})", ids)}
    out = []
    for e in events:
        r = rows.get(e["id"])
        if r is None:
            continue
        name, addr = mail.counterpart(r)
        if e["kind"] == "in":
            out.append({"kind": "in", "mail_id": r["id"], "at": r["received_at"],
                        "subject": r["subject"], "snippet": _snippet(r["head"]),
                        "contact_addr": addr, "contact_name": name,
                        "via_form": bool(r["contact_via_form"]), "status": r["status"],
                        "category": r["category"]})
        else:
            out.append({"kind": "out", "mail_id": r["id"], "at": r["replied_at"],
                        "subject": r["reply_subject"] or mail.reply_subject(r["subject"]),
                        "snippet": _snippet(r["reply_head"]), "replied_by": r["replied_by"],
                        "sent_to": r["reply_sent_to"] or r["from_addr"],
                        "cc": mail.split_addresses(r["reply_cc"]),
                        "contact_name": name, "contact_addr": addr})
    return out


def _summary(conn, g: Grouping, key: str, address_max: int) -> tuple[dict, str, list]:
    addrs, domains, other = _members(conn, g, key)
    where, params = member_where(addrs, domains, other)
    group = _new_group(key)
    if where != "0":
        for st in address_stats(conn, where, params).values():
            _add(group, st)
    return _finish(g, group, address_max), where, params


def client_detail(conn, key: str, offset: int = 0, limit: int = TIMELINE_LIMIT,
                  settings: dict | None = None) -> dict:
    """1つの取引先のまとめ (数・種類ごとの数・アドレス・メモ) と、やり取りの1ページ.

    古い番号 (名前を付ける前の d:… など) でも、今の取引先を返す (key は今の番号)。
    取引先が無ければ ClientNotFound。
    """
    g = Grouping(conn, settings)
    canon = g.canonical(key)
    if canon is None:
        kind, _value = parse_key(key)
        raise ClientNotFound("この取引先は削除されたか、見つかりません" if kind == "c" else
                             "この取引先は見つかりません（メールが1通も無いか、自分たちのアドレス・"
                             "社内の人・フォームの送信元など、取引先にならない相手です）")
    item, where, params = _summary(conn, g, canon, DETAIL_ADDRESSES)
    cats = []
    if where != "0":
        cats = [{"category": r[0], "count": r[1]} for r in conn.execute(
            f"SELECT category, COUNT(*) AS n FROM mails WHERE {where}"
            " GROUP BY category ORDER BY n DESC, category", params)]
    kind, value = parse_key(canon)
    item["note"] = g.clients[value]["note"] if kind == "c" else ""
    item["categories"] = cats
    item["timeline"] = timeline(conn, where, params, offset, limit) if where != "0" else []
    item["timeline_total"] = item["total"] + item["replies"]
    item["offset"], item["limit"] = offset, limit
    return item


def client_for_mail(conn, row, g: Grouping | None = None) -> dict | None:
    """メールの相手の取引先 {key, name, named} (取引先にしない相手なら None).

    返信画面の「取引先」の印用。名前を付けていない取引先の名前は、その取引先のメールから決める。
    """
    g = g or Grouping(conn)
    key = g.key_for(mail.counterpart(row)[1])
    if key is None:
        return None
    kind, value = parse_key(key)
    if kind == "c":
        return {"key": key, "name": g.clients[value]["name"], "named": True}
    item, _w, _p = _summary(conn, g, key, 0)
    return {"key": key, "name": item["name"], "named": False}


def named_client_of(g: Grouping, addr) -> dict | None:
    """名前を付けた取引先にまとめたアドレスなら {key, name} (受信箱の一覧用。DB は読まない)."""
    key = g.key_for(addr)
    if key and key.startswith("c:"):
        return {"key": key, "name": g.clients[int(key[2:])]["name"]}
    return None


# ---------- 管理者が変える (名前・メモ・まとめるアドレスとドメイン) ----------

def clean_name(value) -> str:
    name = " ".join(unicodedata.normalize("NFKC", str(value or "")).split())
    if not name:
        raise ClientError("取引先の名前を入れてください")
    if len(name) > NAME_MAX_CHARS:
        raise ClientError(f"取引先の名前は{NAME_MAX_CHARS}文字以内にしてください")
    return name


def _name_key(name) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(name or "")).split()).casefold()


def _check_name_free(g: Grouping, name: str, client_id: int | None = None) -> None:
    """同じ名前の取引先がほかに無いか (「まとめる先」の選択肢で、どれのことか分からなくならないように).

    全角・半角、英字の大文字・小文字、空白の数の違いは同じ名前とみなす。
    """
    want = _name_key(name)
    for cid, c in g.clients.items():
        if cid != client_id and _name_key(c["name"]) == want:
            raise ClientConflict(
                f"同じ名前の取引先「{c['name']}」がもうあります。同じ会社なら、名前を付けずに"
                "「ほかの取引先にまとめる」でまとめてください。別の会社なら、支店名などを足して"
                "見分けられる名前にしてください")


def clean_note(value) -> str:
    note = mail.normalize_newlines(str(value or "")).strip()
    if len(note) > NOTE_MAX_CHARS:
        raise ClientError(f"メモは{NOTE_MAX_CHARS}文字以内にしてください")
    return note


def clean_pattern(g: Grouping, value) -> str:
    """まとめるアドレス (taro@abc.co.jp) かドメイン (@abc.co.jp。「abc.co.jp」でもよい) を確かめる."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if "<" in text:
        text = parseaddr(text)[1]
    text = ascii_lower(text)
    if not text:
        raise ClientError("アドレスか「@ドメイン」を入れてください")
    if "@" not in text:
        text = "@" + text
    local, _, domain = text.rpartition("@")
    shown = text if len(text) <= 60 else text[:60] + "…"
    if not mail.valid_address((local or "x") + "@" + domain):
        raise ClientError(f"「{shown}」が正しくありません。「tanaka@abc.co.jp」か"
                          "「@abc.co.jp」の形で入れてください")
    if local:
        if g.excluded(text):
            raise ClientError(f"「{shown}」は自分たちのアドレスか、社内の人、HPのフォーム・"
                              "自動送信の送信元なので、取引先にはまとめられません")
    else:
        if _too_broad(domain):
            raise ClientError(f"「{shown}」は広すぎて、別々の会社が全部入ってしまいます。"
                              "「@abc.co.jp」のように、会社のドメインを入れてください")
        if _in_domains(domain, g.own_domains):
            raise ClientError(f"「{shown}」は自分たちのドメインなので、取引先にはまとめられません")
        if g.personal_domain(domain):
            raise ClientError(f"「{shown}」は個人のメールか予約サイトのドメインなので、"
                              "ドメインごとにはまとめられません。アドレスを1つずつ足してください")
    return text


def pattern_for_key(key) -> str:
    """名前を付けていない取引先の番号から、その取引先をまとめる決まり (@ドメインかアドレス)."""
    kind, value = parse_key(key)
    if kind == "d":
        return "@" + value
    if kind == "a":
        return value
    raise ClientError("名前を付けた取引先は、ほかの取引先に入れられません"
                      "（アドレス・ドメインを1つずつ足してください）")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _owner(g: Grouping, pattern: str) -> int | None:
    """今そのアドレス・ドメインが入っている、名前を付けた取引先の番号 (どこにも入っていなければ None).

    アドレスは今の決まりでの取引先 (ドメインでまとめた取引先に入っていることもある)。
    ドメインは、そのドメインか上のドメイン (@abc.co.jp に対する tokyo.abc.co.jp) の決まり。
    """
    if pattern.startswith("@"):
        return g.domain_client(pattern[1:])
    key = g.key_for(pattern)
    return parse_key(key)[1] if key and key.startswith("c:") else None


def _check_patterns(conn, g: Grouping, client_id: int | None, patterns, move: bool) -> list:
    """入れようとしているアドレス・ドメインが、ほかの取引先に入っていないか確かめる.

    同じ決まりがほかの取引先にあるときだけでなく、ほかの取引先にドメインでまとめられている人の
    アドレスも (黙ってその取引先から抜けないように)。move なら、ほかの取引先から移す
    (移す元の (pattern, 取引先の名前) の一覧を返す)。
    """
    moved = []
    for p in patterns:
        r = conn.execute("SELECT client_id FROM client_rules WHERE pattern = ?", (p,)).fetchone()
        if r is not None and r[0] == client_id:
            raise ClientConflict(f"「{p}」は、もうこの取引先に入っています")
        same_rule = r is not None and r[0] in g.clients
        owner = r[0] if same_rule else _owner(g, p)
        if owner is None or owner == client_id or owner not in g.clients:
            continue
        other = g.clients[owner]["name"]
        if not move:
            if same_rule:
                text = f"「{p}」は、取引先「{other}」に入っています"
            else:
                via = next((x["pattern"] for x in g.rules_of.get(owner, [])
                            if x["pattern"].startswith("@")
                            and _in_domains(domain_of(p) if not p.startswith("@") else p[1:],
                                            {x["pattern"][1:]})), "")
                text = (f"「{p}」は今、取引先「{other}」に入っています"
                        + (f"。「{via}」でまとめているためです" if via else ""))
            raise ClientConflict(text + "（こちらに移すときは「移す」を選んでください）")
        moved.append((p, other))
    return moved


def create_client(conn, g: Grouping, name, note="", patterns=(), move: bool = False) -> dict:
    """取引先を作る (名前を付ける)。patterns はまとめるアドレス・ドメイン (確かめてから入れる)."""
    name, note = clean_name(name), clean_note(note)
    _check_name_free(g, name)
    cleaned = list(dict.fromkeys(clean_pattern(g, p) for p in patterns))
    if len(cleaned) > RULES_PER_CLIENT_MAX:
        raise ClientError(f"1つの取引先に入れられるアドレス・ドメインは{RULES_PER_CLIENT_MAX}件までです")
    if conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] >= CLIENT_MAX_COUNT:
        raise ClientError(f"取引先は{CLIENT_MAX_COUNT}件までです")
    moved = _check_patterns(conn, g, None, cleaned, move)
    now = _now()
    cid = conn.execute("INSERT INTO clients (name, note, created_at, updated_at) VALUES (?, ?, ?, ?)",
                       (name, note, now, now)).lastrowid
    for p in cleaned:
        conn.execute("DELETE FROM client_rules WHERE pattern = ?", (p,))
        conn.execute("INSERT INTO client_rules (client_id, pattern, created_at) VALUES (?, ?, ?)",
                     (cid, p, now))
    return {"id": cid, "key": client_key(cid), "name": name, "patterns": cleaned,
            "moved": moved}


def _client_or_404(g: Grouping, key) -> int:
    kind, value = parse_key(key)
    if kind != "c":
        raise ClientError("名前を付けていない取引先です（先に「名前を付ける」を押してください）")
    if value not in g.clients:
        raise ClientNotFound("この取引先は削除されたか、見つかりません")
    return value


def update_client(conn, g: Grouping, key, name=None, note=None) -> dict:
    cid = _client_or_404(g, key)
    changes = {}
    if name is not None:
        changes["name"] = clean_name(name)
        _check_name_free(g, changes["name"], cid)
    if note is not None:
        changes["note"] = clean_note(note)
    if not changes:
        raise ClientError("変更内容がありません")
    conn.execute("UPDATE clients SET " + ", ".join(f"{k} = ?" for k in changes)
                 + ", updated_at = ? WHERE id = ?", (*changes.values(), _now(), cid))
    return {"id": cid, **changes}


def add_rule(conn, g: Grouping, key, pattern, move: bool = False) -> dict:
    cid = _client_or_404(g, key)
    p = clean_pattern(g, pattern)
    if len(g.rules_of.get(cid, [])) >= RULES_PER_CLIENT_MAX:
        raise ClientError(f"1つの取引先に入れられるアドレス・ドメインは{RULES_PER_CLIENT_MAX}件までです")
    moved = _check_patterns(conn, g, cid, [p], move)
    conn.execute("DELETE FROM client_rules WHERE pattern = ?", (p,))
    rid = conn.execute("INSERT INTO client_rules (client_id, pattern, created_at) VALUES (?, ?, ?)",
                       (cid, p, _now())).lastrowid
    conn.execute("UPDATE clients SET updated_at = ? WHERE id = ?", (_now(), cid))
    return {"id": cid, "rule_id": rid, "pattern": p,
            "moved_from": moved[0][1] if moved else None}


def delete_rule(conn, g: Grouping, key, rule_id: int) -> dict:
    cid = _client_or_404(g, key)
    r = conn.execute("SELECT pattern FROM client_rules WHERE id = ? AND client_id = ?",
                     (rule_id, cid)).fetchone()
    if r is None:
        raise ClientNotFound("このアドレス・ドメインは、もうこの取引先に入っていません")
    conn.execute("DELETE FROM client_rules WHERE id = ?", (rule_id,))
    conn.execute("UPDATE clients SET updated_at = ? WHERE id = ?", (_now(), cid))
    return {"id": cid, "pattern": r[0]}


def delete_client(conn, g: Grouping, key) -> dict:
    cid = _client_or_404(g, key)
    patterns = [p["pattern"] for p in g.rules_of.get(cid, [])]
    conn.execute("DELETE FROM client_rules WHERE client_id = ?", (cid,))
    conn.execute("DELETE FROM clients WHERE id = ?", (cid,))
    return {"id": cid, "name": g.clients[cid]["name"], "patterns": patterns}
