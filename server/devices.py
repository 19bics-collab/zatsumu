"""新しい端末からの管理者ログインに、メールでの確認コードを必須にする (機能フラグ).

環境変数 ZATSUMU_LOGIN_VERIFY_EMAIL に宛先メールアドレスを設定したときだけ有効。
未設定・空なら何もしない (既存の挙動は一切変わらない)。Web の設定画面・API
からは変えられず、サーバに入れる人だけが切り替えられる。

流れ:
  1. 管理者トークンだけで来た (端末トークンが無い/無効) 管理者のリクエストは
     401 "device_verification_required" で止める (app.auth_user)
  2. POST /api/device/start で 6 桁の確認コードを宛先へメールする
  3. POST /api/device/verify でコードが合えば端末トークンを発行する
  4. 以降は X-Device-Token ヘッダで端末トークンを送る

端末トークン・確認コードは DB に sha256 のハッシュだけを保存する (DB が
漏れても使えない)。一般メンバー (PC アプリの打刻など) は対象外。
"""
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from . import db, notify, tz

ENV_KEY = "ZATSUMU_LOGIN_VERIFY_EMAIL"
# 画面側はこの固定文字列を見て確認コードの入力画面を出す
REQUIRED_DETAIL = "device_verification_required"

CODE_TTL = timedelta(minutes=10)        # 確認コードの有効時間
MAX_ATTEMPTS = 5                        # 1つのコードで試せる回数
RESEND_INTERVAL = timedelta(seconds=60)  # 同じユーザーの送信間隔
HOURLY_LIMIT = 5                        # 同じユーザーの1時間あたりの送信回数
IDLE_EXPIRY = timedelta(days=90)        # 最後に使ってからこの期間で端末トークン失効
TOUCH_INTERVAL = timedelta(minutes=10)  # last_used_at の更新間隔 (毎回書き込まない)
LABEL_MAX = 120                         # 端末名 (User-Agent) の保存文字数


class RateLimited(Exception):
    """送信回数の制限に当たった (画面向けの文言を持つ)."""


class SendUnavailable(Exception):
    """メールを送れない (SMTP 未設定・送信失敗)。中身は画面に出さない."""


def verify_email() -> str:
    """確認コードの送り先。空なら機能は無効."""
    return os.environ.get(ENV_KEY, "").strip()


def enabled() -> bool:
    return bool(verify_email())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _code_hash(request_id: str, code: str) -> str:
    # request_id を混ぜて、同じコードでも request ごとに別のハッシュにする
    return _sha256(f"{request_id}:{code}")


def mask_email(addr: str) -> str:
    """宛先を伏せ字にする (例: 19bics@gmail.com → 19***@gmail.com)."""
    local, _, domain = addr.partition("@")
    head = local[:2] if len(local) > 2 else local[:1]
    return f"{head}***@{domain}" if domain else f"{head}***"


def clean_label(user_agent: str | None) -> str:
    """User-Agent を端末名として保存・表示できる形にする (制御文字除去・先頭120文字)."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", user_agent or "").strip()[:LABEL_MAX]


def client_ip(request) -> str:
    """アクセス元 IP.

    Caddy 経由なので X-Forwarded-For の左端 (Caddy が付けた本当の接続元) を使う。
    ただし直接の接続元がプライベート/ループバック (= 手前の Caddy) のときだけ信用する。
    インターネットから直接来たリクエストの X-Forwarded-For は偽装できるため使わない。
    """
    direct = request.client.host if request.client else ""
    try:
        d = ipaddress.ip_address(direct)
        trusted = d.is_private or d.is_loopback
    except ValueError:
        trusted = False
    if trusted:
        xff = request.headers.get("x-forwarded-for", "")
        left = xff.split(",")[0].strip()
        try:
            return str(ipaddress.ip_address(left))
        except ValueError:
            pass
    return direct


# ---------- 端末トークン ----------

def issue_device(conn, user_id: int, label: str = "", ip: str = "") -> str:
    """端末トークンを発行して返す (平文はこの戻り値だけ。DB にはハッシュを保存)."""
    token = secrets.token_urlsafe(32)
    now = tz.utc_iso(_now())
    conn.execute(
        "INSERT INTO device_tokens (user_id, token_hash, label, ip, created_at,"
        " last_used_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, _sha256(token), clean_label(label), ip, now, now),
    )
    return token


def _is_expired(row, now: datetime) -> bool:
    return datetime.fromisoformat(row["last_used_at"]) < now - IDLE_EXPIRY


def check_device(conn, user_id: int, token: str | None):
    """このユーザーの有効な端末トークンなら行を返す (使った時刻も更新)."""
    if not token:
        return None
    h = _sha256(token)
    row = conn.execute(
        "SELECT * FROM device_tokens WHERE token_hash = ?", (h,)
    ).fetchone()
    # ハッシュで引いた後も定数時間比較で確かめる (比較の時間差で推測されない)
    if not row or not hmac.compare_digest(row["token_hash"], h):
        return None
    now = _now()
    if row["user_id"] != user_id or row["revoked_at"] or _is_expired(row, now):
        return None
    if datetime.fromisoformat(row["last_used_at"]) < now - TOUCH_INTERVAL:
        conn.execute(
            "UPDATE device_tokens SET last_used_at = ? WHERE id = ?",
            (tz.utc_iso(now), row["id"]),
        )
    return row


def list_devices(conn, include_inactive: bool = False) -> list[dict]:
    """端末の一覧 (既定は有効なものだけ・最近使った順)."""
    now = _now()
    out = []
    for r in conn.execute(
        "SELECT d.*, u.name AS user_name FROM device_tokens d"
        " JOIN users u ON u.id = d.user_id ORDER BY d.last_used_at DESC"
    ):
        expired = _is_expired(r, now)
        active = not r["revoked_at"] and not expired
        if not active and not include_inactive:
            continue
        out.append({
            "id": r["id"], "user_id": r["user_id"], "user_name": r["user_name"],
            "label": r["label"], "ip": r["ip"], "created_at": r["created_at"],
            "last_used_at": r["last_used_at"], "revoked_at": r["revoked_at"],
            "expired": expired, "active": active,
        })
    return out


def revoke_device(conn, device_id: int):
    """1台取り消す。取り消した行を返す (見つからない・取り消し済みなら None)."""
    row = conn.execute(
        "SELECT * FROM device_tokens WHERE id = ? AND revoked_at IS NULL", (device_id,)
    ).fetchone()
    if not row:
        return None
    conn.execute(
        "UPDATE device_tokens SET revoked_at = ? WHERE id = ?",
        (tz.utc_iso(_now()), device_id),
    )
    return row


def revoke_all(conn, user_id: int | None = None) -> int:
    """全端末 (user_id 指定時はそのユーザーの全端末) を取り消し、件数を返す."""
    sql = "UPDATE device_tokens SET revoked_at = ? WHERE revoked_at IS NULL"
    params: list = [tz.utc_iso(_now())]
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    return conn.execute(sql, params).rowcount


# ---------- 確認コード ----------

def _check_rate(conn, user_id: int, now: datetime) -> None:
    last = conn.execute(
        "SELECT MAX(created_at) FROM device_challenges WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    if last and datetime.fromisoformat(last) > now - RESEND_INTERVAL:
        raise RateLimited("確認コードの再送は1分に1回までです。少し待ってからお試しください")
    n = conn.execute(
        "SELECT COUNT(*) FROM device_challenges WHERE user_id = ? AND created_at > ?",
        (user_id, tz.utc_iso(now - timedelta(hours=1))),
    ).fetchone()[0]
    if n >= HOURLY_LIMIT:
        raise RateLimited(
            "確認コードの送信は1時間に5回までです。しばらく待ってからお試しください"
        )


def _build_mail(settings: dict, user, code: str, ip: str, label: str,
                now: datetime) -> tuple[str, str]:
    """確認メールの件名と本文。管理者トークン・端末トークンなどの秘密は入れない."""
    company = settings.get("company_name") or "zatsumu"
    when = now.astimezone(tz.TZ).strftime("%Y年%m月%d日 %H:%M")
    # 件名にはコードを入れない (スマホのロック画面の通知に出てしまうため)
    subject = f"【{company}】管理画面ログインの確認コード"
    body = (
        f"新しい端末から管理画面へのログインがありました。\n"
        f"画面に次の確認コードを入力してください。\n"
        f"\n"
        f"  確認コード: {code}\n"
        f"  （{int(CODE_TTL.total_seconds() // 60)}分以内・1回だけ有効）\n"
        f"\n"
        f"日時: {when}（{tz.TZ.key}）\n"
        f"ユーザー: {user['name']}\n"
        f"アクセス元IP: {ip or '不明'}\n"
        f"ブラウザ: {label or '不明'}\n"
        f"\n"
        f"心当たりが無ければ、このコードは誰にも教えないでください。\n"
        f"合言葉（管理者トークン）が他人に知られている可能性があります。\n"
        f"サーバの担当者に連絡して、合言葉を作り直してください"
        f"（python manage.py reset-token {user['name']}）。\n"
    )
    return subject, body


def start_challenge(conn, user, ip: str, user_agent: str | None) -> dict:
    """確認コードを作って宛先へメールし、{request_id, sent_to} を返す.

    制限超過は RateLimited、送れないときは SendUnavailable を送出する。
    送信前に一度コミットする (メール送信中に DB の書き込みロックを持たないため)。
    """
    to = verify_email()
    settings = db.get_settings(conn)
    sender = settings.get("mail_from") or settings.get("smtp_user")
    if not settings.get("smtp_host") or not sender:
        raise SendUnavailable()
    # 回数の確認から記録までを他のリクエストと重ならないようにする
    # (同時に押されて制限をすり抜けないよう、先に書き込みロックを取る)
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    now = _now()
    try:
        _check_rate(conn, user["id"], now)
    except RateLimited:
        conn.rollback()
        raise
    request_id = secrets.token_urlsafe(16)
    code = f"{secrets.randbelow(10**6):06d}"
    label = clean_label(user_agent)
    cur = conn.execute(
        "INSERT INTO device_challenges (request_id, user_id, code_hash, ip,"
        " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (request_id, user["id"], _code_hash(request_id, code), ip,
         tz.utc_iso(now), tz.utc_iso(now + CODE_TTL)),
    )
    challenge_id = cur.lastrowid
    # 送信に失敗しても回数制限には数える (SMTP を連打させない)
    conn.commit()
    subject, body = _build_mail(settings, user, code, ip, label, now)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    try:
        notify.smtp_send(settings, sender, [to], msg.as_string())
    except Exception as e:  # noqa: BLE001
        # 送れなかったコードは使えないようにする。原因はサーバのログにだけ残す
        conn.execute(
            "UPDATE device_challenges SET used_at = ? WHERE id = ?",
            (tz.utc_iso(_now()), challenge_id),
        )
        conn.commit()
        print(f"確認コードのメール送信に失敗: {type(e).__name__}: "
              f"{notify.safe_error(e, settings)}")
        raise SendUnavailable() from None
    return {"request_id": request_id, "sent_to": mask_email(to)}


class VerifyError(Exception):
    """コードが違う・期限切れなど (画面向けの文言を持つ)."""


def verify_challenge(conn, user, request_id: str, code: str) -> None:
    """確認コードを照合する。合わなければ VerifyError (試行回数は必ず記録される).

    呼び出し側は失敗時もコミットすること (試行回数を残すため)。
    """
    row = conn.execute(
        "SELECT * FROM device_challenges WHERE request_id = ?", (request_id or "",)
    ).fetchone()
    # 別ユーザーの request_id は存在しないものとして扱う
    if not row or row["user_id"] != user["id"]:
        raise VerifyError("確認コードが無効です。もう一度コードを送ってください")
    now = _now()
    if row["used_at"] or row["attempts"] >= MAX_ATTEMPTS:
        raise VerifyError("このコードはもう使えません。もう一度コードを送ってください")
    if datetime.fromisoformat(row["expires_at"]) <= now:
        raise VerifyError("確認コードの有効期限（10分）が切れました。もう一度コードを送ってください")
    # 先に試行回数を1つ使う (同時に送られても上限を超えて試せない)
    cur = conn.execute(
        "UPDATE device_challenges SET attempts = attempts + 1"
        " WHERE id = ? AND used_at IS NULL AND attempts < ?",
        (row["id"], MAX_ATTEMPTS),
    )
    if not cur.rowcount:
        raise VerifyError("このコードはもう使えません。もう一度コードを送ってください")
    ok = hmac.compare_digest(row["code_hash"],
                             _code_hash(row["request_id"], str(code or "").strip()))
    if not ok:
        left = MAX_ATTEMPTS - row["attempts"] - 1
        if left <= 0:
            raise VerifyError(
                "確認コードを5回間違えたため、このコードは使えなくなりました。"
                "もう一度コードを送ってください"
            )
        raise VerifyError(f"確認コードが違います（あと{left}回）")
    # 1回で使い切り (同時に正しいコードが2回来ても片方だけ通す)
    cur = conn.execute(
        "UPDATE device_challenges SET used_at = ? WHERE id = ? AND used_at IS NULL",
        (tz.utc_iso(now), row["id"]),
    )
    if not cur.rowcount:
        raise VerifyError("このコードはもう使えません。もう一度コードを送ってください")
