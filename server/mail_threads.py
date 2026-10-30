"""受信箱のスレッド (返信どうしでつながった、同じお客様との同じ話のメールのまとまり).

まとめるのは、届いたメールどうしが返信の印 (In-Reply-To・References) でつながっていて、
相手 (スレッドの相手: party) が同じで空でないときだけ。件名が同じだけのメール・差出人が同じだけの
メール (HPのフォームは、どのお客様の分も差出人が wordpress@ で、件名も同じことが多い) はまとめない。
つながりは、次のどれかを B が指している (B の mail_refs に入っている) とき (A と B は届いたメール):
  (a) A の Message-ID (届いたメールへの返事)
  (b) A の reply_message_id (この画面から送った返信・Webメールなどの返信への返事)
  (c) 送信箱のメール S の Message-ID で、S が A への返信とつながっている (S.linked_mail_id = A)
さらに、両方が HPのフォームから届いたメール (contact_via_form) なら、つなげない (G2)。
こちらが送った返信は行にしない (受信箱では ↩・「↩ 返信済み」で出し、本文は右の本文で読む)。

スレッドの印は mails.thread_id (まとまりの中で一番小さい番号)。1通だけのメールは NULL のまま
(書き込まない。本文の大きい行を書き直さない)。新しく届いたメールは、入れる前に印を決めて INSERT に
入れる (入れた行をもう一度書き換えない)。相手が変わった・返信の Message-ID が分かった・送信箱の
メールがつながったときは、DB のトリガーが mail_thread_todo に入れ (前の版のコードが動いている
間も)、process_pending が作り直す。決まり (受信するアドレス・社内の人・フォームの送信元・この
ファイルの決まりの版) が変わったら、rebuild が全部を数え直し、変わった行だけ書き直す。
同じスレッドのメールは、いつも同じ空でない相手 (check で確かめ、違えばばらして作り直す)。
迷うときはまとめない (まとめ損ねても、今までどおり1通ずつ出るだけ)。
"""
import hashlib
from datetime import datetime, timedelta, timezone

from . import db, mail, tz

# 決まりの版。決まりを変えたら上げる (次の定期受信の回に、全部を数え直す)
THREAD_RULE_VERSION = "1"
# 1回の process_pending で作り直すメールの数の上限 (前の版に戻っていた間に何千通も届いていても、
# 書き込みのロックを長く持たない。残りは次の回)
PENDING_MAX = 2000
# rebuild で、この数の行を書き直すごとに確定する (書き込みのロックを長く持たない)
REBUILD_CHUNK = 2000
# /api/mail/threads/{key} で返すメールの数の上限 (最初のメールと、新しい方から残り)
MEMBERS_MAX = 100
# どの番号のメールまでスレッドを決め終えたか・数え直したときの決まり (settings の非公開キー)
UPTO_KEY = "mail_state_threads_upto"
RULE_KEY = "mail_state_threads_rule"
# 1回の問い合わせに入れる ? の数 (古い SQLite の 999 個より少なく)
_IN_MAX = 400
# まとめるかを決めるのに読む列 (本文は読まない)
_COLS = ("id, message_id, reply_message_id, contact_addr, contact_source, contact_via_form,"
         " list_mail, from_addr, to_addr, cc_addr, thread_id")
_XCOLS = ", ".join("x." + c.strip() for c in _COLS.split(","))
_OPEN = ("unhandled", "pending_approval")
# 決まり v2 (件名で補う) を使うかを決めるための数 (/api/mail/threads-check)
V2_RECENT_DAYS = 90
V2_WINDOW_DAYS = 60
V2_SCAN_MAX = 3000
# 起動してから一度、スレッドの相手がそろっているかを確かめたか (maintain)
_checked = False


def _marks(values) -> str:
    return ",".join("?" * len(values))


def _chunks(values, n: int = _IN_MAX):
    values = list(values)
    for i in range(0, len(values), n):
        yield values[i:i + n]


def _addr(value) -> str:
    return str(value or "").strip().lower()


class Ctx:
    """まとめるのに使う、自分たちのアドレス・社内の人・フォームの送信元 (1回の処理ごとに作る).

    settings は全社の設定 (受信の1アドレス分に差し替えたものではなく)。渡さなければ DB から読む
    (受信したときと、後で数え直したときで、相手の決め方がずれないように)。
    """

    def __init__(self, conn, settings: dict | None = None):
        s = settings if settings is not None else db.get_settings(conn)
        self.rules = mail.contact_rules(conn, s)
        self.own = self.rules.own
        self.staff = self.rules.people

    def signature(self) -> str:
        """決まりの印 (版と、まとめ方に効くアドレスの一覧。変わったら数え直す)."""
        parts = sorted({f"own:{a}" for a in self.own} | {f"staff:{a}" for a in self.staff}
                       | {f"form:{a}" for a in self.rules.form_addrs}
                       | {f"fdom:{d}" for d in self.rules.form_domains})
        digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:10]
        return f"{THREAD_RULE_VERSION}:{digest}"

    def outside(self, addr: str) -> bool:
        """お客様として数えるアドレスか (自分たち・社内の人・自動送信 (noreply@ など) でない)."""
        return bool(addr) and addr not in self.own and addr not in self.staff \
            and not self.rules.automated(addr)


def party(ctx: Ctx, m) -> str:
    """メール m (dict か行) のスレッドの相手 (小文字のアドレス。まとめないメールは "").

    相手をまだ決めていない (前の版が入れた) メール・一斉配信のメールはまとめない。相手が
    お客様のアドレスならそれ。相手が分からないフォームのメール・自分たちや社内の人が相手の
    メール・自分たちや自動送信から届いたメール (問い合わせの自動返信の控えなど) は、宛先・CC の
    外の人 (自分たち・社内・自動送信・自社のドメインを除く) がちょうど1人なら、その人。
    """
    m = m if isinstance(m, dict) else dict(m)
    if m.get("contact_source") not in mail.CONTACT_SOURCES or m.get("list_mail"):
        return ""
    c = _addr(m.get("contact_addr"))
    frm = _addr(m.get("from_addr"))
    if ctx.outside(c):
        return c
    if (not c and m.get("contact_via_form")) or c in ctx.own or c in ctx.staff \
            or frm in ctx.own or ctx.rules.automated(frm):
        # 宛先の相手 (自動返信の控えなど、自分たちから1人のお客様に送ったメール)
        rcpt = {a.lower() for _n, a in mail._header_addrs(m.get("to_addr"), m.get("cc_addr"))}
        rcpt = {a for a in rcpt if ctx.outside(a)
                and not mail._in_domains(a.rpartition("@")[2], ctx.rules.own_domains)}
        if len(rcpt) == 1:
            return rcpt.pop()
    return ""


def _party_of(ctx: Ctx, m: dict) -> str:
    """party を1回だけ計算して m に覚えておく (m はこのファイルの中で作った dict だけ)."""
    if "_party" not in m:
        m["_party"] = party(ctx, m)
    return m["_party"]


def ok_edge(ctx: Ctx, a: dict, b: dict) -> bool:
    """返信の印でつながった2通を、同じスレッドにしてよいか (G1: 同じ空でない相手、
    G2: 両方ともフォームから届いたメールではない)."""
    pa = _party_of(ctx, a)
    return bool(pa) and pa == _party_of(ctx, b) and not (
        a.get("contact_via_form") and b.get("contact_via_form"))


def plan(conn, ctx: Ctx, m: dict, refs) -> tuple[int | None, set]:
    """メール m (行か、入れる前の dict。id・thread_id は無ければ None) をどのスレッドに入れるか.

    (印, まとめるスレッドの印の集まり) を返す。つながる相手が無ければ (None, set())。
    つながるのは、m が指しているメール (返事の元) と、m を指しているメール (先に届いていた返事)。
    どれも索引で探す (メール全部を読むことはしない)。
    """
    m = dict(m)
    if not _party_of(ctx, m):
        return None, set()
    me = m.get("id")
    refs = list(dict.fromkeys(r for r in refs if r))[:_IN_MAX]
    found = []
    if refs:
        marks = _marks(refs)
        # 返事の元 (届いたメール・こちらが送った返信・送信箱の返信がつながっているメール)
        found += conn.execute(f"SELECT {_COLS} FROM mails WHERE message_id IN ({marks})", refs).fetchall()
        found += conn.execute(f"SELECT {_COLS} FROM mails WHERE reply_message_id IN ({marks})",
                              refs).fetchall()
        found += conn.execute(f"SELECT {_XCOLS} FROM sent_mails s JOIN mails x ON x.id = s.linked_mail_id"
                              f" WHERE s.message_id IN ({marks})", refs).fetchall()
    # 先に届いていた返事 (このメール・このメールに送った返信を指しているメール)
    own_refs = [m.get("message_id"), m.get("reply_message_id")]
    if me is not None:
        own_refs += [r[0] for r in conn.execute(
            "SELECT message_id FROM sent_mails WHERE linked_mail_id = ? LIMIT 50", (me,))]
    own_refs = list(dict.fromkeys(r for r in own_refs if r))
    if own_refs:
        found += conn.execute(
            f"SELECT {_XCOLS} FROM mail_refs r JOIN mails x ON x.id = r.mail_id"
            f" WHERE r.ref IN ({_marks(own_refs)}) LIMIT 500", own_refs).fetchall()
    mine = m.get("thread_id") or me
    keys = set()
    for row in found:
        x = dict(row)
        if me is not None and x["id"] == me:
            continue
        if ok_edge(ctx, m, x):
            keys.add(x["thread_id"] or x["id"])
    if not keys - {mine}:
        return None, set()
    if mine is not None:
        keys.add(mine)
    return min(keys), keys


def apply(conn, target: int, keys, me_id: int | None = None) -> int:
    """keys のスレッド (と1通だけのメール) を、印 target の1つのスレッドにする。書いた行の数を返す.

    同じ相手のまとまりどうしをつなぐだけなので、スレッドの相手はそろったまま。
    もう target になっている行は書かない。コミットは呼び出し側。
    """
    keys = sorted({int(k) for k in keys if k is not None})
    ids = sorted(set(keys) | ({int(me_id)} if me_id is not None else set()))
    n = 0
    for part in _chunks(keys):
        n += conn.execute(f"UPDATE mails SET thread_id = ? WHERE thread_id IN ({_marks(part)})"
                          " AND thread_id != ?", (target, *part, target)).rowcount
    for part in _chunks(ids):
        n += conn.execute(f"UPDATE mails SET thread_id = ? WHERE id IN ({_marks(part)})"
                          " AND thread_id IS NULL", (target, *part)).rowcount
    return n


def link(conn, ctx: Ctx, mail_id: int) -> int:
    """届いているメール1通を、つながるスレッドに入れる (何度呼んでも同じ。変わらなければ書かない)."""
    row = conn.execute(f"SELECT {_COLS} FROM mails WHERE id = ?", (mail_id,)).fetchone()
    if not row:
        return 0
    refs = [r[0] for r in conn.execute("SELECT ref FROM mail_refs WHERE mail_id = ?", (mail_id,))]
    target, keys = plan(conn, ctx, dict(row), refs)
    if target is None:
        return 0
    return apply(conn, target, keys, me_id=mail_id)


def _state(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else ""


def _upto(conn) -> int:
    try:
        return int(_state(conn, UPTO_KEY) or 0)
    except ValueError:
        return 0


def process_pending(conn, settings: dict | None = None, limit: int = PENDING_MAX) -> int:
    """まだスレッドを決めていないメール (前の版が入れた・この版が入れた後) と、トリガーが入れた
    作り直すメールを、つながるスレッドに入れる。作り直したメールの数を返す.

    まだ一度も数えていない DB (この機能を入れた最初の回) は、全部を数える (rebuild)。
    失敗しても受信の結果は変えない (ログに出して 0。次の回にもう一度)。
    """
    try:
        ctx = Ctx(conn, settings)
        if not _state(conn, RULE_KEY):
            return rebuild(conn, ctx)
        upto = _upto(conn)
        new = [r[0] for r in conn.execute(
            "SELECT id FROM mails WHERE id > ? ORDER BY id LIMIT ?", (upto, limit))]
        todo = [r[0] for r in conn.execute("SELECT mail_id FROM mail_thread_todo LIMIT ?", (limit,))]
        ids = sorted(set(new) | set(todo))
        for mail_id in ids:
            link(conn, ctx, mail_id)
        for part in _chunks(todo):
            conn.execute(f"DELETE FROM mail_thread_todo WHERE mail_id IN ({_marks(part)})", part)
        if new:
            mail._set_state(conn, UPTO_KEY, max(new))
        conn.commit()
        return len(ids)
    except Exception as e:  # noqa: BLE001  受信・画面の処理は止めない
        conn.rollback()
        print(f"スレッドをまとめられませんでした: {e}")
        return 0


def _edges(conn) -> list[tuple[int, int]]:
    """返信の印でつながるメールの組 (返事, 返事の元) を全部 (索引で引くだけ。本文は読まない)."""
    return [(a, b) for a, b in conn.execute(
        "SELECT r.mail_id, p.id FROM mail_refs r JOIN mails p ON p.message_id = r.ref"
        " UNION SELECT r.mail_id, p.id FROM mail_refs r JOIN mails p"
        " ON p.reply_message_id = r.ref AND p.reply_message_id <> ''"
        " UNION SELECT r.mail_id, s.linked_mail_id FROM mail_refs r JOIN sent_mails s"
        " ON s.message_id = r.ref WHERE s.linked_mail_id IS NOT NULL") if a != b]


def _rows_by_id(conn, ids) -> dict:
    out = {}
    for part in _chunks(sorted(ids)):
        for r in conn.execute(f"SELECT {_COLS} FROM mails WHERE id IN ({_marks(part)})", part):
            out[r[0]] = dict(r)
    return out


def partition(conn, ctx: Ctx) -> dict:
    """今のメールを決まりどおりにまとめたときの印 {番号: 印} (2通以上のスレッドのメールだけ)."""
    edges = _edges(conn)
    rows = _rows_by_id(conn, {i for e in edges for i in e})
    parent: dict = {}

    def find(x):
        root = x
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(x, x) != root:   # 道を縮める
            parent[x], x = root, parent[x]
        return root

    for a, b in edges:
        ra, rb = rows.get(a), rows.get(b)
        if ra is None or rb is None or not ok_edge(ctx, ra, rb):
            continue
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        fa, fb = find(a), find(b)
        if fa != fb:
            parent[max(fa, fb)] = min(fa, fb)
    groups: dict = {}
    for x in list(parent):
        groups.setdefault(find(x), []).append(x)
    out = {}
    for members in groups.values():
        if len(members) >= 2:
            label = min(members)
            for x in members:
                out[x] = label
    return out


def rebuild(conn, ctx: Ctx) -> int:
    """全部のメールを数え直し、印が変わった行だけ書き直す。書き直した行の数を返す.

    決まりが変わったとき (と、この機能を入れた最初の回) に、定期受信の処理の中で1回だけ動く。
    REBUILD_CHUNK 行ごとに確定する (書き込みのロックを長く持たない)。数え直している間に、ほかの
    書き込み (トリガー) で印が変わった行は書かない (作り直すメールに入っているので、後で入れ直す)。
    1通だけのメールは書かない。途中で止まっても、決まりの印を最後に書くので、次の回にやり直す。
    """
    conn.commit()
    snap_max = conn.execute("SELECT COALESCE(MAX(id), 0) FROM mails").fetchone()[0]
    snap_todo = [r[0] for r in conn.execute("SELECT mail_id FROM mail_thread_todo")]
    want = partition(conn, ctx)
    now = {r[0]: r[1] for r in conn.execute(
        "SELECT id, thread_id FROM mails WHERE thread_id IS NOT NULL")}
    changes = [(want.get(i), i, now.get(i)) for i in sorted(set(want) | set(now))
               if want.get(i) != now.get(i)]
    written = 0
    for part in _chunks(changes, REBUILD_CHUNK):
        written += sum(conn.execute("UPDATE mails SET thread_id = ? WHERE id = ? AND thread_id IS ?",
                                    c).rowcount for c in part)
        conn.commit()
    mail._set_state(conn, UPTO_KEY, max(snap_max, _upto(conn)))
    for part in _chunks(snap_todo):
        conn.execute(f"DELETE FROM mail_thread_todo WHERE mail_id IN ({_marks(part)})", part)
    mail._set_state(conn, RULE_KEY, ctx.signature())
    conn.commit()
    return written


def check(conn, ctx: Ctx, repair: bool = True) -> list:
    """同じスレッドのメールが、みんな同じ空でない相手かを確かめる (違うスレッドの最初の20個を返す).

    repair なら、違うスレッドはばらして作り直すメールに入れ、ログと操作の記録に残す
    (別のお客様のメールを1つのスレッドに残さない)。
    """
    groups: dict = {}
    for r in conn.execute(f"SELECT {_COLS} FROM mails WHERE thread_id IS NOT NULL ORDER BY thread_id"):
        m = dict(r)
        groups.setdefault(m["thread_id"], []).append(m)
    bad = []
    for key, members in groups.items():
        parties = {_party_of(ctx, m) for m in members}
        if len(parties) > 1 or "" in parties:
            bad.append({"key": key, "ids": [m["id"] for m in members][:20],
                        "parties": sorted(parties)[:5]})
    if repair and bad:
        now = tz.utc_iso(datetime.now(timezone.utc))
        for v in bad:
            conn.execute("INSERT OR IGNORE INTO mail_thread_todo SELECT id FROM mails WHERE thread_id = ?",
                         (v["key"],))
            conn.execute("UPDATE mails SET thread_id = NULL WHERE thread_id = ?", (v["key"],))
            conn.execute("INSERT INTO audit_log (admin_id, action, detail, at)"
                         " VALUES (0, 'mail_thread_repair', ?, ?)",
                         (f"thread={v['key']} mails={','.join(map(str, v['ids']))}", now))
        conn.commit()
        print(f"相手のそろっていないスレッドを {len(bad)} 個ばらしました（まとめ直します）")
    return bad[:20]


def maintain(conn, settings: dict | None = None) -> None:
    """定期受信の回ごとに: 決まりが変わっていれば数え直し、作り直すメールを片付ける。
    起動してから最初の回だけ、スレッドの相手がそろっているかも確かめる.

    受信の途中 (今すぐ受信など) なら、この回は何もしない (入れているメールと重ならないように)。
    """
    global _checked
    if not mail._fetch_lock.acquire(blocking=False):
        return
    try:
        ctx = Ctx(conn, settings)
        if _state(conn, RULE_KEY) != ctx.signature():
            n = rebuild(conn, ctx)
            if n:
                print(f"スレッドを数え直しました（{n}通の印を書き直しました）")
        process_pending(conn, settings)
        if not _checked:
            check(conn, ctx)
            _checked = True
    finally:
        mail._fetch_lock.release()


def tree(conn, members: list) -> list[tuple[dict, int, int | None]]:
    """スレッドのメールを、返事のつながりの木の順 (最初のメールから、枝ごとに古い順) に並べる.

    members は sort_t (並べる時刻) の入った行。[(行, 深さ, 返事の元の番号)] を返す。
    返事の元は、指しているメールのうち、そのメールより前の一番新しいもの (無ければ最初のメール)。
    """
    rows = sorted((dict(m) for m in members), key=lambda r: (r.get("sort_t") or 0, r["id"]))
    if not rows:
        return []
    pos = {r["id"]: i for i, r in enumerate(rows)}
    ids = list(pos)
    by_ref: dict = {}
    for r in rows:
        for ref in (r.get("message_id"), r.get("reply_message_id")):
            if ref:
                by_ref.setdefault(ref, r["id"])
    refs: dict = {}
    for part in _chunks(ids):
        for s in conn.execute("SELECT message_id, linked_mail_id FROM sent_mails"
                              f" WHERE linked_mail_id IN ({_marks(part)})", part):
            by_ref.setdefault(s[0], s[1])
        for mail_id, ref in conn.execute(
                f"SELECT mail_id, ref FROM mail_refs WHERE mail_id IN ({_marks(part)})", part):
            refs.setdefault(mail_id, []).append(ref)
    root = rows[0]["id"]
    kids: dict = {}
    parent_of: dict = {}
    for r in rows[1:]:
        earlier = [by_ref[x] for x in refs.get(r["id"], []) if by_ref.get(x) in pos
                   and pos[by_ref[x]] < pos[r["id"]]]
        p = max(earlier, key=lambda i: pos[i]) if earlier else root
        parent_of[r["id"]] = p
        kids.setdefault(p, []).append(r["id"])
    out = []
    stack = [(root, 0)]
    while stack:
        i, depth = stack.pop()
        out.append((rows[pos[i]], depth, parent_of.get(i)))
        for k in sorted(kids.get(i, []), key=lambda x: pos[x], reverse=True):
            stack.append((k, depth + 1))
    return out


def aggregate(conn, keys) -> dict:
    """一覧に出すスレッドごとの {通数, 未対応と確認待ちの数, ★の数, 一番新しい番号, 最初の日時}.

    スレッドのメールだけの索引 (idx_mails_thread) だけで数える (表は読まない)。
    """
    out = {}
    marks_open = _marks(_OPEN)
    for part in _chunks(sorted({int(k) for k in keys if k})):
        for r in conn.execute(
                f"SELECT thread_id, COUNT(*), SUM(status IN ({marks_open})),"
                " SUM(flagged_at IS NOT NULL), MAX(id), MIN(received_at)"
                " FROM mails WHERE thread_id IS NOT NULL"
                f" AND thread_id IN ({_marks(part)}) GROUP BY thread_id", (*_OPEN, *part)):
            out[r[0]] = {"size": r[1], "open": r[2] or 0, "flagged": r[3] or 0,
                         "newest_id": r[4], "first_at": r[5]}
    return out


def _v2_candidates(conn, ctx: Ctx) -> dict:
    """件名で補う決まり (v2) なら、まとまったはずのメールの数と番号 (最初の10通).

    最近 V2_RECENT_DAYS 日の、件名が「Re:」などで始まるのにスレッドに入っていないメールで、
    同じ相手から V2_WINDOW_DAYS 日以内の前に、Re: などを除いた件名が同じメールが届いているもの。
    """
    now = datetime.now(timezone.utc)
    since = tz.utc_iso(now - timedelta(days=V2_RECENT_DAYS))
    rows = conn.execute(
        f"SELECT {_COLS}, subject, received_at FROM mails WHERE received_at >= ? AND thread_id IS NULL"
        " ORDER BY received_at DESC LIMIT ?", (since, V2_SCAN_MAX)).fetchall()
    ids = []
    for row in rows:
        m = dict(row)
        if not mail._reply_subject(m["subject"]):
            continue
        p = _party_of(ctx, m)
        key, forward = mail.subject_key(m["subject"])
        if not p or not key or forward:
            continue
        try:
            start = tz.utc_iso(datetime.fromisoformat(m["received_at"]) - timedelta(days=V2_WINDOW_DAYS))
        except ValueError:
            continue
        for e in conn.execute(
                f"SELECT {_COLS}, subject FROM mails WHERE lower(contact_addr) = ? AND received_at >= ?"
                " AND received_at < ? AND id != ? ORDER BY received_at DESC LIMIT 50",
                (p, start, m["received_at"], m["id"])):
            e = dict(e)
            if _party_of(ctx, e) == p and mail.subject_key(e["subject"])[0] == key and not (
                    e["contact_via_form"] and m["contact_via_form"]):
                ids.append(m["id"])
                break
    return {"count": len(ids), "ids": ids[:10]}


def report(conn, ctx: Ctx) -> dict:
    """スレッドの様子 (管理者の確かめ用。/api/mail/threads-check)。何も書き換えない."""
    threads, members = conn.execute(
        "SELECT COUNT(DISTINCT thread_id), COUNT(*) FROM mails WHERE thread_id IS NOT NULL").fetchone()
    big = conn.execute("SELECT thread_id, COUNT(*) AS n FROM mails WHERE thread_id IS NOT NULL"
                       " GROUP BY thread_id ORDER BY n DESC, thread_id LIMIT 1").fetchone()
    return {
        "threads": threads,
        "members": members,
        "largest": {"key": big[0], "size": big[1]} if big else None,
        "pending": conn.execute("SELECT COUNT(*) FROM mail_thread_todo").fetchone()[0],
        "upto": _upto(conn),
        "rule": _state(conn, RULE_KEY),
        "signature": ctx.signature(),
        "violations": check(conn, ctx, repair=False),
        "v2_candidates": _v2_candidates(conn, ctx),
    }
