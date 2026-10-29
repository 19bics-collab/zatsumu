"""送信箱 (こちらから送ったメールを、新しい順にまとめて見る).

出すのは次の2つ (どちらも新しい順に混ぜて並べる):

1. この画面から送った返信 (mails の reply_*。前の版が送ったものも)。送信済みフォルダの返信を見て
   自動で返信済みにしたメール (reply_source が空でない) は、2 の方に出るので入れない
2. 受信するアドレスごとに、メールボックスの送信済みフォルダから取り込んだメール (sent_mails。
   お名前メールの Webメール・Outlook・スマホ・Gmail などから送ったもの。server/mail.py の
   fetch_sent_mail)

どちらも索引で新しい順に読むだけなので、何万通あっても一覧は速い (idx_mails_app_sent・
idx_sent_mails_sent_at・idx_sent_mails_account)。番号 (key) は「a<届いたメールの番号>」
(この画面から送った返信) と「i<sent_mails の番号>」(取り込んだメール)。
"""
import re

from . import clients, mail

# 一覧の1回に出す数 (既定と上限) と、「もっと見る」で進める位置の上限
LIST_LIMIT = 50
LIST_LIMIT_MAX = 200
OFFSET_MAX = 5000
# 一覧に出す本文の書き出しの文字数
FIRST_LINE_CHARS = 100

_KEY_RE = re.compile(r"([ai])([1-9][0-9]{0,15})")

_APP_COLS = (
    "m.id, m.replied_at AS at, m.reply_subject, m.subject, substr(m.reply_body, 1, 600) AS head,"
    " m.reply_sent_to, m.from_addr, m.from_name, m.contact_addr, m.contact_name,"
    " m.contact_source, m.reply_cc, m.reply_bcc, m.replied_by, m.account_id")
# 索引 idx_mails_app_sent の条件と同じにする (違うと索引を使わない)
_APP_WHERE = "m.replied_at IS NOT NULL AND m.reply_source = ''"
_IMPORTED_COLS = (
    "s.id, s.sent_at AS at, s.subject, substr(s.body, 1, 600) AS head, s.to_key, s.to_name,"
    " s.to_addr, s.cc_addr, s.account_id, s.source, s.note, s.linked_mail_id, s.marked_replied,"
    " (SELECT COUNT(*) FROM sent_mail_attachments a WHERE a.sent_id = s.id) AS attachment_count")


class SentNotFound(LookupError):
    pass


def first_line(text) -> str:
    """本文の書き出しを1行にしたもの (「◯◯ 様」の行だけにならないよう、続きの行もつなげる。長ければ縮める)."""
    line = " ".join(str(text or "").split())
    return line if len(line) <= FIRST_LINE_CHARS else line[:FIRST_LINE_CHARS].rstrip() + "…"


def _labels(conn) -> dict:
    return {r[0]: r[1] for r in conn.execute("SELECT id, label FROM mail_accounts")}


def _account_filter(account: str | None, col: str) -> tuple[str, list]:
    """届いたアドレス・取り込んだアドレスでの絞り込み (none で削除したアドレス)."""
    if not account:
        return "", []
    if account == "none":
        return f" AND {col} IS NULL", []
    if account.isascii() and account.isdigit():
        return f" AND {col} = ?", [int(account)]
    raise ValueError("account はアドレスの番号か none で指定してください")


def _app_to(r) -> tuple[str, str]:
    """この画面から送った返信の宛先 (名前, アドレス)。前の版が送ったもの (宛先が空) は差出人."""
    addr = str(r["reply_sent_to"] or r["from_addr"] or "").strip()
    name = ""
    c_name, c_addr = mail.counterpart(r)
    if addr and addr.lower() == c_addr.lower():
        name = c_name
    elif addr and addr.lower() == str(r["from_addr"] or "").lower():
        name = mail._one_line(r["from_name"])
    return name, addr


def _client_tag(g, named: dict, addr: str) -> dict:
    """名前を付けた取引先にまとめた相手なら、その取引先 (受信箱の一覧と同じ印)."""
    a = addr.lower()
    if a not in named:
        named[a] = clients.named_client_of(g, a) if a else None
    c = named[a]
    return {"client_key": c["key"] if c else "", "client_name": c["name"] if c else "",
            "client_kind": c["kind"] if c else ""}


def list_sent(conn, offset: int = 0, limit: int = LIST_LIMIT, account: str | None = None) -> dict:
    """送信箱の一覧 (新しい順)。{"items": [...], "total": 全部の数, "has_more": 続きがあるか}."""
    offset = max(0, min(int(offset), OFFSET_MAX))
    limit = max(1, min(int(limit), LIST_LIMIT_MAX))
    want = offset + limit + 1
    app_sql, app_params = _account_filter(account, "m.account_id")
    imp_sql, imp_params = _account_filter(account, "s.account_id")
    app_rows = conn.execute(
        f"SELECT {_APP_COLS} FROM mails m WHERE {_APP_WHERE}{app_sql}"
        " ORDER BY m.replied_at DESC LIMIT ?", (*app_params, want)).fetchall()
    imp_rows = conn.execute(
        f"SELECT {_IMPORTED_COLS} FROM sent_mails s WHERE 1 = 1{imp_sql}"
        " ORDER BY s.sent_at DESC LIMIT ?", (*imp_params, want)).fetchall()
    total = conn.execute(
        f"SELECT (SELECT COUNT(*) FROM mails m WHERE {_APP_WHERE}{app_sql})"
        f" + (SELECT COUNT(*) FROM sent_mails s WHERE 1 = 1{imp_sql})",
        (*app_params, *imp_params)).fetchone()[0]
    merged = sorted([("a", r) for r in app_rows] + [("i", r) for r in imp_rows],
                    key=lambda x: (x[1]["at"] or "", x[0] == "i", x[1]["id"]), reverse=True)
    page = merged[offset:offset + limit]
    labels = _labels(conn)
    linked = sorted({r["linked_mail_id"] for k, r in page if k == "i" and r["linked_mail_id"]})
    subjects = {}
    if linked:
        subjects = {r[0]: r[1] for r in conn.execute(
            f"SELECT id, subject FROM mails WHERE id IN ({','.join('?' * len(linked))})", linked)}
    g = clients.Grouping(conn)
    named: dict = {}
    items = []
    for kind, r in page:
        if kind == "a":
            name, addr = _app_to(r)
            others = len(mail.split_addresses(r["reply_cc"])) + len(mail.split_addresses(r["reply_bcc"]))
            item = {
                "key": f"a{r['id']}", "kind": "app", "sent_at": r["at"],
                "to_addr": addr, "to_name": name, "more_to": others,
                "subject": r["reply_subject"] or mail.reply_subject(r["subject"]),
                "first_line": first_line(r["head"]),
                "source": "app", "source_label": mail.SENT_SOURCE_LABELS["app"],
                "sent_by": r["replied_by"] or "",
                "mail_id": r["id"], "mail_subject": r["subject"], "marked_replied": False,
                "attachment_count": 0, "note": "",
            }
        else:
            to = mail._header_addrs(r["to_addr"])
            others = max(0, len(to) - 1) + len(mail._header_addrs(r["cc_addr"]))
            item = {
                "key": f"i{r['id']}", "kind": "imported", "sent_at": r["at"],
                "to_addr": to[0][1] if to else (r["to_key"] or ""),
                "to_name": to[0][0] if to else (r["to_name"] or ""), "more_to": others,
                "subject": r["subject"], "first_line": first_line(r["head"]),
                "source": r["source"],
                "source_label": mail.SENT_SOURCE_LABELS.get(r["source"], "Webメールなど"),
                "sent_by": "",
                "mail_id": r["linked_mail_id"],
                "mail_subject": subjects.get(r["linked_mail_id"], ""),
                "marked_replied": bool(r["marked_replied"]),
                "attachment_count": r["attachment_count"], "note": r["note"],
            }
        acc = r["account_id"]
        item["account_id"] = acc
        item["account_label"] = labels.get(acc, mail.DELETED_ACCOUNT_LABEL) if acc else \
            mail.DELETED_ACCOUNT_LABEL
        item.update(_client_tag(g, named, item["to_addr"]))
        items.append(item)
    return {"items": items, "total": total, "has_more": len(merged) > offset + limit,
            "offset": offset}


def parse_key(key: str) -> tuple[str, int]:
    mt = _KEY_RE.fullmatch(str(key or ""))
    if not mt:
        raise SentNotFound(key)
    return mt.group(1), int(mt.group(2))


def _client_for(conn, addr: str, name: str):
    """宛先の取引先 (取引先の画面へのつながり用。取引先にしない相手・メールの無い相手なら None)."""
    if not addr:
        return None
    g = clients.Grouping(conn)
    key = g.key_for(addr)
    if key is None or g.canonical(key) is None:
        return None
    return clients.client_for_mail(
        conn, {"contact_addr": addr, "contact_name": name, "contact_source": "from"}, g)


def _mail_link(conn, mail_id) -> dict | None:
    """元の届いたメール (送信箱の詳しい画面の「元のメール」用)."""
    if not mail_id:
        return None
    r = conn.execute(
        "SELECT id, subject, received_at, status, from_addr, from_name, contact_addr,"
        " contact_name, contact_source FROM mails WHERE id = ?", (mail_id,)).fetchone()
    if not r:
        return None
    name, addr = mail.counterpart(r)
    return {"id": r["id"], "subject": r["subject"], "received_at": r["received_at"],
            "status": r["status"], "contact_name": name, "contact_addr": addr}


def sent_detail(conn, key: str, settings: dict) -> dict:
    """送信箱の1通の詳しい中身 (本文は全部)。無ければ SentNotFound."""
    kind, num = parse_key(key)
    labels = _labels(conn)
    if kind == "a":
        r = conn.execute(f"SELECT {_APP_COLS}, m.reply_body, m.approved_by, m.requested_by"
                         f" FROM mails m WHERE m.id = ? AND {_APP_WHERE}", (num,)).fetchone()
        if not r:
            raise SentNotFound(key)
        name, addr = _app_to(r)
        acc = mail.get_account(conn, r["account_id"])
        sent_by = r["replied_by"] or ""
        if r["approved_by"] and r["requested_by"]:
            sent_by = f"{r['requested_by']} さんの下書きを {r['approved_by']} が確認して送信"
        elif sent_by:
            sent_by = f"{sent_by} が送信"
        return {
            "key": key, "kind": "app", "sent_at": r["at"],
            "subject": r["reply_subject"] or mail.reply_subject(r["subject"]),
            "body": r["reply_body"],
            "from": mail.reply_sender(settings, acc),
            "to": [{"name": name, "addr": addr}] if addr else [],
            "cc": [{"name": "", "addr": a} for a in mail.split_addresses(r["reply_cc"])],
            "bcc": [{"name": "", "addr": a} for a in mail.split_addresses(r["reply_bcc"])],
            "account_id": r["account_id"],
            "account_label": labels.get(r["account_id"], mail.DELETED_ACCOUNT_LABEL)
            if r["account_id"] else mail.DELETED_ACCOUNT_LABEL,
            "source": "app", "source_label": mail.SENT_SOURCE_LABELS["app"],
            "sent_by": sent_by, "folder": "", "note": "",
            "attachments": [], "skipped_attachments": [],
            "mail": _mail_link(conn, r["id"]), "marked_replied": False, "link_kind": "",
            "client": _client_for(conn, addr, name),
        }
    r = conn.execute("SELECT * FROM sent_mails WHERE id = ?", (num,)).fetchone()
    if not r:
        raise SentNotFound(key)
    to = mail._header_addrs(r["to_addr"])
    atts = conn.execute("SELECT * FROM sent_mail_attachments WHERE sent_id = ? ORDER BY id",
                        (num,)).fetchall()
    first = to[0] if to else ("", "")
    return {
        "key": key, "kind": "imported", "sent_at": r["sent_at"], "subject": r["subject"],
        "body": r["body"],
        "from": mail._display_address(mail._one_line(r["from_name"]), r["from_addr"])
        if r["from_addr"] else "",
        "to": [{"name": n, "addr": a} for n, a in to],
        "cc": [{"name": n, "addr": a} for n, a in mail._header_addrs(r["cc_addr"])],
        # 取り込んだメールの BCC は出さない (送ったソフトによって控えに残ったり残らなかったりする)
        "bcc": [],
        "account_id": r["account_id"],
        "account_label": labels.get(r["account_id"], mail.DELETED_ACCOUNT_LABEL)
        if r["account_id"] else mail.DELETED_ACCOUNT_LABEL,
        "source": r["source"],
        "source_label": mail.SENT_SOURCE_LABELS.get(r["source"], "Webメールなど"),
        "sent_by": "", "folder": r["folder"], "note": r["note"],
        "attachments": [mail.attachment_info(a) for a in atts if not a["skip_reason"]],
        "skipped_attachments": [mail.skipped_attachment_info(a) for a in atts if a["skip_reason"]],
        "mail": _mail_link(conn, r["linked_mail_id"]),
        "marked_replied": bool(r["marked_replied"]), "link_kind": r["link_kind"],
        "client": _client_for(conn, first[1], first[0]),
    }


def newest_mark(conn) -> str:
    """送信箱が変わったかを見る印 (開いたままの画面が取り直すか決める用。索引だけを読む)."""
    sent_id, replied = conn.execute(
        "SELECT (SELECT MAX(id) FROM sent_mails),"
        f" (SELECT MAX(m.replied_at) FROM mails m WHERE {_APP_WHERE})").fetchone()
    return f"{sent_id or 0}|{replied or ''}"
