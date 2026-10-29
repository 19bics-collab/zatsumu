"""メールの検索 (受信箱・送信箱の上の検索欄).

探すところ: 件名・本文・差出人と宛先 (名前とアドレス。CC も)・添付ファイルの名前と、
名前を付けた取引先の名前 (「ABC商事」で、その取引先にまとめた相手のメールが出る)。
空白 (全角の空白も) で区切った言葉は、全部を含むメールだけを出す (「清掃 10月」)。
英字の大文字・小文字は区別しない。日本語は言葉の途中でも見つかる (「鍵」「チェックイン」)。

速く探すしくみ (何万通あっても):
- SQLite の FTS5 (trigram = 3文字ずつに区切った索引) が使えるときは、メールの索引 (mail_fts) と
  送信箱に取り込んだメールの索引 (sent_fts) を作り、DB のトリガーで、メールを入れた・変えた・
  消したときにいつも合わせる (自動更新で前の版のコードに戻っても、DB が合わせ続ける)。
  前からあるメールの分は、起動した後に少しずつ入れる (build_index。入れ終わるまでは下の
  「1通ずつ見る」探し方を使う)。3文字以上の言葉は索引で探し、1〜2文字の言葉 (「鍵」「清掃」) は
  索引に入れた文字を1通ずつ見る (3文字の言葉と一緒なら、その結果の中だけを見る)
- FTS5 か trigram が使えない SQLite では、メールを1通ずつ見る (新しい方から SCAN_MAX 通まで。
  それより古いメールは探さない。画面に「古いメールは探していません」と出す)
- 本文は、長いメールでも先頭の BODY_CHARS 文字まで (引用の続きなどで索引が大きくなりすぎないように)

探す言葉は SQL の文に入れない (いつも ? で渡す。索引の探し方の文の中では "…" で囲む)。
"""
import sqlite3
import time
import unicodedata

from . import clients

# 検索の言葉の最大文字数・言葉の数
QUERY_MAX_CHARS = 100
TERMS_MAX = 10
# 索引に入れる本文の文字数 (長いメールは先頭から)
BODY_CHARS = 20000
# 索引が使えないとき、1通ずつ見るメールの数 (新しい方から)
SCAN_MAX = 20000
# 一覧に出す本文の抜き出しの文字数
SNIPPET_CHARS = 80
# 1つの言葉に名前が合う取引先の数の上限 (「株」などで、取引先が全部合うのを防ぐ)
CLIENT_HITS_MAX = 30
# trigram の索引で探せる言葉の長さ (これより短い言葉は1通ずつ見る)
TRIGRAM_MIN = 3

# 索引の作りの版 (列を変えたら上げる。上がったら作り直す) と、作り終えた印・作っている途中の位置
INDEX_VERSION = "1"
READY_KEY = "mail_state_search_index"
PROGRESS_KEY = "mail_state_search_build"
# この数より少なければ、DB を開いたときにその場で作る (新しい DB・テスト)。多ければ起動後に少しずつ
BUILD_NOW_MAX = 2000
BUILD_BATCH = 300

_SEP = " || char(10) || "


def _cat(*exprs: str) -> str:
    """列をつなげる SQL の式 (改行で区切る。言葉に空白は入らないので、列をまたいで合わない)."""
    return _SEP.join(f"COALESCE({e}, '')" for e in exprs)


# 索引の列と、その中身の SQL の式 ({t} は行の名前: NEW / OLD / m / s。{id} はメールの番号の式)
MAIL_COLS = ("subject", "body", "people", "files", "rsubject", "rbody", "rpeople")
_MAIL_EXPR = {
    "subject": "COALESCE({t}.subject, '')",
    "body": f"substr(COALESCE({{t}}.body, ''), 1, {BODY_CHARS})",
    # 差出人・相手 (フォームのお客様)・Reply-To・宛先 (To)・CC・フォームの会社名
    "people": _cat("{t}.from_name", "{t}.from_addr", "{t}.contact_name", "{t}.contact_addr",
                   "{t}.reply_to", "{t}.to_addr", "{t}.cc_addr", "{t}.contact_company"),
    "files": "COALESCE((SELECT group_concat(a.filename, char(10)) FROM mail_attachments a"
             " WHERE a.mail_id = {id}), '')",
    # この画面から送った返信 (送信箱で探す用)
    "rsubject": "COALESCE({t}.reply_subject, '')",
    "rbody": f"substr(COALESCE({{t}}.reply_body, ''), 1, {BODY_CHARS})",
    "rpeople": _cat("{t}.reply_sent_to", "{t}.reply_cc", "{t}.reply_bcc"),
}
# 索引に入れる列が変わったら、索引を合わせる mails の列
_MAIL_SOURCE_COLS = ("subject", "body", "from_name", "from_addr", "contact_name", "contact_addr",
                     "reply_to", "to_addr", "cc_addr", "contact_company", "reply_subject",
                     "reply_body", "reply_sent_to", "reply_cc", "reply_bcc")
SENT_COLS = ("subject", "body", "people", "files")
_SENT_EXPR = {
    "subject": "COALESCE({t}.subject, '')",
    "body": f"substr(COALESCE({{t}}.body, ''), 1, {BODY_CHARS})",
    "people": _cat("{t}.from_name", "{t}.from_addr", "{t}.to_name", "{t}.to_addr", "{t}.cc_addr"),
    "files": "COALESCE((SELECT group_concat(a.filename, char(10)) FROM sent_mail_attachments a"
             " WHERE a.sent_id = {id}), '')",
}
_SENT_SOURCE_COLS = ("subject", "body", "from_name", "from_addr", "to_name", "to_addr", "cc_addr")

# 受信箱で探す列 (届いたメール) と、送信箱で探す列 (この画面から送った返信・取り込んだメール)
INBOX_COLS = ("subject", "body", "people", "files")
APP_SENT_COLS = ("subject", "rsubject", "rbody", "rpeople", "people")

_TABLES = ("mail_fts", "sent_fts")
_TRIGGERS = ("trg_mail_fts_ins", "trg_mail_fts_upd", "trg_mail_fts_del",
             "trg_mail_att_fts_ins", "trg_mail_att_fts_del",
             "trg_sent_fts_ins", "trg_sent_fts_upd", "trg_sent_fts_del",
             "trg_sent_att_fts_ins", "trg_sent_att_fts_del")


class QueryError(ValueError):
    pass


# ---------- 使えるか ----------

_fts_ok: bool | None = None


def fts_supported() -> bool:
    """この SQLite で FTS5 の trigram が使えるか (1回だけ調べて覚える)."""
    global _fts_ok
    if _fts_ok is None:
        try:
            c = sqlite3.connect(":memory:")
            try:
                c.execute("CREATE VIRTUAL TABLE t USING fts5(a, tokenize='trigram')")
                c.execute("INSERT INTO t (a) VALUES ('abcd')")
                _fts_ok = c.execute("SELECT COUNT(*) FROM t WHERE t MATCH '\"bcd\"'").fetchone()[0] == 1
            finally:
                c.close()
        except sqlite3.Error:
            _fts_ok = False
    return _fts_ok


def _expr(table: dict, col: str, t: str, id_expr: str | None = None) -> str:
    return table[col].format(t=t, id=id_expr or f"{t}.id")


# ---------- 索引を作る・合わせる (db._migrate から毎回呼ばれる。済んでいれば読むだけ) ----------

def _create(conn) -> None:
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS mail_fts USING fts5("
                 + ", ".join(MAIL_COLS) + ", tokenize='trigram')")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS sent_fts USING fts5("
                 + ", ".join(SENT_COLS) + ", tokenize='trigram')")
    for prefix, table, exprs, cols, src, att, att_key in (
            ("mail", "mails", _MAIL_EXPR, MAIL_COLS, _MAIL_SOURCE_COLS, "mail_attachments", "mail_id"),
            ("sent", "sent_mails", _SENT_EXPR, SENT_COLS, _SENT_SOURCE_COLS,
             "sent_mail_attachments", "sent_id")):
        fts = f"{prefix}_fts"
        values = ", ".join(_expr(exprs, c, "NEW") for c in cols)
        put = (f"DELETE FROM {fts} WHERE rowid = NEW.id;"
               f" INSERT INTO {fts} (rowid, {', '.join(cols)}) VALUES (NEW.id, {values});")
        # 相手を決めずに入ったメールは、db.py の trg_mails_contact_default が相手を入れ直し、
        # そのときに下の「変えたとき」のトリガーが索引に入れるので、ここでは入れない (2回入れない)
        when = (" WHEN NOT (NEW.contact_source = '' AND NEW.contact_addr = '')"
                if table == "mails" else "")
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS trg_{prefix}_fts_ins AFTER INSERT ON {table}"
                     f"{when} BEGIN {put} END")
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS trg_{prefix}_fts_upd"
                     f" AFTER UPDATE OF {', '.join(src)} ON {table} BEGIN {put} END")
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS trg_{prefix}_fts_del AFTER DELETE ON {table}"
                     f" BEGIN DELETE FROM {fts} WHERE rowid = OLD.id; END")
        for when, row in (("INSERT", "NEW"), ("DELETE", "OLD")):
            files = _expr(exprs, "files", row, f"{row}.{att_key}")
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS trg_{prefix}_att_fts_{when[:3].lower()}"
                f" AFTER {when} ON {att} BEGIN UPDATE {fts} SET files = {files}"
                f" WHERE rowid = {row}.{att_key}; END")


def _drop(conn) -> None:
    for name in _TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name in _TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {name}")


def _setting(conn, key: str) -> str | None:
    r = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


def ensure_index(conn) -> None:
    """索引の表とトリガーを用意する (何度呼んでも同じ。済んでいれば何も書かない).

    FTS5 が使えない SQLite で開いたときは、索引を合わせるトリガーを外す (メールを入れられなく
    ならないように)。そのときは作り終えた印も消し、次に FTS5 が使えるときに作り直す。
    """
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE name IN ("
        + ",".join("?" * (len(_TABLES) + len(_TRIGGERS))) + ")", (*_TABLES, *_TRIGGERS))}
    ready = _setting(conn, READY_KEY)
    if not fts_supported():
        for name in _TRIGGERS:
            if name in have:
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        if ready is not None or _setting(conn, PROGRESS_KEY) is not None:
            conn.execute("DELETE FROM settings WHERE key IN (?, ?)", (READY_KEY, PROGRESS_KEY))
        return
    if ready is not None and ready != INDEX_VERSION:
        # 作りが変わった (この先の版で列を変えたとき)。作り直す
        _drop(conn)
        conn.execute("DELETE FROM settings WHERE key IN (?, ?)", (READY_KEY, PROGRESS_KEY))
        ready, have = None, set()
    if not have.issuperset(_TABLES + _TRIGGERS):
        _create(conn)
    if ready is None:
        n = conn.execute("SELECT (SELECT COUNT(*) FROM mails) + (SELECT COUNT(*) FROM sent_mails)"
                         ).fetchone()[0]
        if n <= BUILD_NOW_MAX:
            build_index(conn)


def index_ready(conn) -> bool:
    """索引を作り終えていて、使えるか (作っている途中・使えないなら、1通ずつ見て探す)."""
    return fts_supported() and _setting(conn, READY_KEY) == INDEX_VERSION


def build_index(conn, batch: int = BUILD_BATCH, pause: float = 0.0, stop=None) -> int:
    """前からあるメールを索引に入れる (作り終えていれば何もしない)。入れた通数を返す.

    少しずつ (batch 通ずつ) 入れて、そのたびに確定する (ほかの書き込みを長く待たせない)。
    途中で止まっても (再起動など)、次は続きから入れる。入れている間に届いたメールは
    トリガーが入れる。いくつも同時に呼ばれても、同じ所を2回入れるだけで壊れない。
    stop (threading.Event) が立ったら、区切りのよい所でやめる (サーバを止めるとき)。
    """
    if not fts_supported():
        return 0
    conn.commit()
    done = 0
    while not (stop is not None and stop.is_set()):
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _setting(conn, READY_KEY) == INDEX_VERSION:
                conn.commit()
                return done
            prog = _setting(conn, PROGRESS_KEY)
            if prog is None:
                # 初めから作る: 前に作った索引 (トリガーを外していた間にずれたかもしれない) は捨てる
                _drop(conn)
                _create(conn)
                m_max, s_max = conn.execute(
                    "SELECT COALESCE((SELECT MAX(id) FROM mails), 0),"
                    " COALESCE((SELECT MAX(id) FROM sent_mails), 0)").fetchone()
                state = [0, m_max, 0, s_max]
            else:
                state = [int(x) for x in prog.split(":")]
            m_upto, m_max, s_upto, s_max = state
            if m_upto < m_max:
                lo, hi = m_upto + 1, min(m_upto + batch, m_max)
                done += _fill(conn, "mail_fts", "mails", _MAIL_EXPR, MAIL_COLS, lo, hi)
                state[0] = hi
            elif s_upto < s_max:
                lo, hi = s_upto + 1, min(s_upto + batch, s_max)
                done += _fill(conn, "sent_fts", "sent_mails", _SENT_EXPR, SENT_COLS, lo, hi)
                state[2] = hi
            else:
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                             (READY_KEY, INDEX_VERSION))
                conn.execute("DELETE FROM settings WHERE key = ?", (PROGRESS_KEY,))
                conn.commit()
                return done
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                         (PROGRESS_KEY, ":".join(str(x) for x in state)))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if pause:
            time.sleep(pause)
    return done


def _fill(conn, fts: str, table: str, exprs: dict, cols: tuple, lo: int, hi: int) -> int:
    conn.execute(f"DELETE FROM {fts} WHERE rowid BETWEEN ? AND ?", (lo, hi))
    values = ", ".join(_expr(exprs, c, "x") for c in cols)
    cur = conn.execute(f"INSERT INTO {fts} (rowid, {', '.join(cols)})"
                       f" SELECT x.id, {values} FROM {table} x WHERE x.id BETWEEN ? AND ?", (lo, hi))
    return max(cur.rowcount, 0)


# ---------- 探す言葉 ----------

def parse_query(q) -> list[str]:
    """検索欄の文字を言葉に分ける (空白・全角の空白で区切る。同じ言葉は1つに)。長すぎれば QueryError."""
    text = str(q or "")
    if len(text) > QUERY_MAX_CHARS:
        raise QueryError(f"検索の言葉は{QUERY_MAX_CHARS}文字以内にしてください")
    # 制御文字 (改行・タブなど) は区切りにする
    text = "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in text)
    terms: list[str] = []
    seen = set()
    for t in text.split():
        k = clients.ascii_lower(t)
        if k not in seen:
            seen.add(k)
            terms.append(t)
    if len(terms) > TERMS_MAX:
        raise QueryError(f"検索の言葉は{TERMS_MAX}個までにしてください")
    return terms


def variants(term: str) -> list[str]:
    """言葉の探す形 (英字は小文字にしたもの)。全角の英数字・記号や半角のカタカナを含むときは、
    ふつうの形 (NFKC。「１０月」→「10月」、「ｶｷﾞ」→「カギ」) も足す."""
    out = [clients.ascii_lower(term)]
    norm = clients.ascii_lower(unicodedata.normalize("NFKC", term))
    if norm and norm not in out and not any(ch.isspace() for ch in norm):
        out.append(norm)
    return out


def _phrase(term: str) -> str:
    """索引の探し方の文の中の言葉 ("…" で囲む。中の " は "" にする)."""
    return '"' + term.replace('"', '""') + '"'


def _instr_any(exprs) -> str:
    return "(" + " OR ".join(f"instr(lower({e}), ?) > 0" for e in exprs) + ")"


class Search:
    """1回の検索 (受信箱か送信箱の一覧の条件を作る)。terms は parse_query の結果."""

    def __init__(self, conn, terms: list[str], grouping=None):
        self.conn = conn
        self.terms = terms
        self.keys = [clients.ascii_lower(t) for t in terms]
        # 言葉ごとの探す形 (全角の英数字・半角のカタカナで打った言葉は、ふつうの形でも探す)
        self.forms = [variants(t) for t in terms]
        self.indexed = index_ready(conn)
        self.partial = False
        self._g = grouping
        self._hits: dict[str, list[str]] = {}
        self._sent_keys: list[str] | None = None

    @property
    def mode(self) -> str:
        return "index" if self.indexed else "scan"

    @property
    def grouping(self):
        if self._g is None:
            self._g = clients.Grouping(self.conn)
        return self._g

    # 取引先の名前に合う言葉
    def client_hits(self, i: int) -> list[str]:
        """名前に i 番目の言葉を含む、名前を付けた取引先の番号 (c:…)."""
        key = self.keys[i]
        if key not in self._hits:
            g = self.grouping
            forms = self.forms[i]
            hits = sorted(cid for cid, c in g.clients.items()
                          if any(f in clients.ascii_lower(c["name"]) for f in forms))
            self._hits[key] = [clients.client_key(cid) for cid in hits[:CLIENT_HITS_MAX]]
        return self._hits[key]

    def _mail_client_sql(self, keys: list[str]) -> tuple[str, list]:
        parts, params = [], []
        for key in keys:
            addrs, domains, other = clients._members(self.conn, self.grouping, key)
            where, p = clients.member_where(addrs, domains, other)
            if where != "0":
                parts.append(where)
                params += p
        if not parts:
            return "", []
        return "m.id IN (SELECT id FROM mails WHERE " + " OR ".join(parts) + ")", params

    def _sent_client_sql(self, keys: list[str]) -> tuple[str, list]:
        if self._sent_keys is None:
            self._sent_keys = [r[0] for r in self.conn.execute(
                "SELECT DISTINCT to_key FROM sent_mails WHERE to_key != ''")]
        want = set(keys)
        g = self.grouping
        addrs = [a for a in self._sent_keys if g.key_for(a) in want][:900]
        if not addrs:
            return "", []
        return f"s.to_key IN ({','.join('?' * len(addrs))})", addrs

    # 条件
    def _fts_cond(self, fts: str, cols, idx: list[int]) -> tuple[str, list]:
        """索引の表の中で、言葉 (idx の番号) を全部含む行の条件 (fts の行に対して)."""
        long_ = [i for i in idx if min(map(len, self.forms[i])) >= TRIGRAM_MIN]
        short = [i for i in idx if i not in long_]
        where, params = [], []
        if long_:
            colset = "{" + " ".join(cols) + "}"
            where.append(f"{fts} MATCH ?")
            params.append(" AND ".join(
                "(" + " OR ".join(f"{colset} : {_phrase(f)}" for f in self.forms[i]) + ")"
                for i in long_))
        for i in short:
            cond, p = self._scan_term(cols, i)
            where.append(cond)
            params += p
        return " AND ".join(where), params

    def _scan_term(self, exprs, i: int) -> tuple[str, list]:
        """言葉 (i 番目) のどれかの形を、どれかの列に含む条件 (1通ずつ見る)."""
        forms = self.forms[i]
        return (_instr_any([e for e in exprs for _ in forms]),
                [f for _ in exprs for f in forms])

    def _build(self, alias: str, fts: str, cols, scan_exprs: list[str], client_sql) -> tuple[str, list]:
        plain, with_client = [], []
        for i in range(len(self.terms)):
            hits = self.client_hits(i)
            csql, cparams = client_sql(hits) if hits else ("", [])
            (with_client if csql else plain).append((i, csql, cparams))
        where, params = [], []
        if self.indexed:
            if plain:
                cond, p = self._fts_cond(fts, cols, [i for i, _, _ in plain])
                where.append(f"{alias}.id IN (SELECT rowid FROM {fts} WHERE {cond})")
                params += p
            for i, csql, cparams in with_client:
                cond, p = self._fts_cond(fts, cols, [i])
                where.append(f"({alias}.id IN (SELECT rowid FROM {fts} WHERE {cond}) OR {csql})")
                params += p + cparams
        else:
            for i, csql, cparams in plain + with_client:
                cond, p = self._scan_term(scan_exprs, i)
                if csql:
                    cond, p = f"({cond} OR {csql})", p + cparams
                where.append(cond)
                params += p
        return " AND ".join(where), params

    def inbox_where(self) -> tuple[str, list]:
        """受信箱 (mails m) の条件 (WHERE に AND でつなげる)."""
        exprs = [_expr(_MAIL_EXPR, c, "m") for c in INBOX_COLS]
        where, params = self._build("m", "mail_fts", INBOX_COLS, exprs, self._mail_client_sql)
        return self._limit_scan(where, params, "m", "mails")

    def app_sent_where(self) -> tuple[str, list]:
        """送信箱の「この画面から送った返信」(mails m) の条件."""
        exprs = [_expr(_MAIL_EXPR, c, "m") for c in APP_SENT_COLS]
        where, params = self._build("m", "mail_fts", APP_SENT_COLS, exprs, self._mail_client_sql)
        return self._limit_scan(where, params, "m", "mails")

    def imported_where(self) -> tuple[str, list]:
        """送信箱の「取り込んだメール」(sent_mails s) の条件."""
        exprs = [_expr(_SENT_EXPR, c, "s") for c in SENT_COLS]
        where, params = self._build("s", "sent_fts", SENT_COLS, exprs, self._sent_client_sql)
        return self._limit_scan(where, params, "s", "sent_mails")

    def _limit_scan(self, where: str, params: list, alias: str, table: str) -> tuple[str, list]:
        """索引が使えないときは、新しい方から SCAN_MAX 通だけを見る."""
        if self.indexed:
            return where, params
        r = self.conn.execute(f"SELECT id FROM {table} ORDER BY id DESC LIMIT 1 OFFSET ?",
                              (SCAN_MAX,)).fetchone()
        if r is None:
            return where, params
        self.partial = True
        return f"{alias}.id > ? AND {where}", [r[0], *params]

    # 画面に出すもの
    def snippet(self, text) -> str:
        """本文から、言葉が最初に出てくるあたりを抜き出す (無ければ空)."""
        flat = " ".join(str(text or "")[:BODY_CHARS].split())
        low = clients.ascii_lower(flat)
        pos = min((p for p in (low.find(k) for fs in self.forms for k in fs) if p >= 0),
                  default=-1)
        if pos < 0:
            return ""
        start = max(0, pos - 12)   # 言葉が2行に収まって見えるよう、前は少しだけ
        end = start + SNIPPET_CHARS
        return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")

    def matching_names(self, names) -> list[str]:
        """添付ファイルの名前のうち、言葉を含むもの."""
        return [n for n in names
                if any(k in clients.ascii_lower(n) for fs in self.forms for k in fs)]
