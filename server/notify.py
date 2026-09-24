"""通知 (Slack Incoming Webhook / SMTP メール).

管理画面の設定に応じて、着席・退席・長時間在席アラートなどを通知する。
送信は呼び出し元をブロックしないようバックグラウンドスレッドで行う。
実際の送信失敗はログに出すのみで、アプリの動作は止めない。

SMTP への接続は smtp_send() の1か所にまとめてある (メール返信 mail.py も使う)。
"""
import json
import smtplib
import ssl
import threading
import urllib.request
from email.mime.text import MIMEText

# 最初から暗号化して話すポート (SMTPS)。それ以外は平文で繋いで STARTTLS で暗号化する
SMTPS_PORT = 465


class InsecureConnectionError(RuntimeError):
    """暗号化できない接続でパスワードを送りそうになったので中止した."""


# 画面やログに出すエラー文から伏せる設定 (パスワード・API キー)
SECRET_SETTING_KEYS = ("smtp_pass", "imap_pass", "anthropic_api_key")


def safe_error(e: BaseException, settings: dict) -> str:
    """例外の文面を返す。設定の秘密値がもし含まれていたら *** に置き換える.

    接続テストなどの失敗理由は管理者に見せたいが、ライブラリの例外文に
    サーバ応答などと一緒に秘密値が混ざる可能性をゼロにはできないため。
    """
    text = str(e)
    for key in SECRET_SETTING_KEYS:
        value = str(settings.get(key) or "")
        if value:
            text = text.replace(value, "***")
    return text


def channels(settings: dict) -> list[str]:
    """設定済みの通知チャネル名を返す (UI 表示・判定用)."""
    ch = []
    if settings.get("slack_webhook_url"):
        ch.append("slack")
    if settings.get("smtp_host") and settings.get("mail_to"):
        ch.append("email")
    return ch


def send_slack(url: str, text: str) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10).read()


def smtp_send(settings: dict, sender: str, recipients: list[str],
              message: str) -> None:
    """SMTP で1通送る (失敗時は例外を送出).

    - サーバ証明書を検証する (Python の既定の starttls() は検証しないため
      ssl.create_default_context() を明示的に渡す)
    - 465番ポートは最初から暗号化 (SMTP_SSL)、それ以外は STARTTLS で暗号化する
    - ログインする (smtp_user がある) 場合、暗号化できなければパスワードを
      送らずに中止する。盗み見や「暗号化なしに引き下げる」攻撃で漏れるため。
      ログインしない場合だけ、従来どおり暗号化なしでも送信を続ける
    - 例外メッセージに設定値 (パスワード等) を入れない
    """
    host = settings["smtp_host"]
    port = int(settings.get("smtp_port") or 587)
    user = settings.get("smtp_user")
    ctx = ssl.create_default_context()
    if port == SMTPS_PORT:
        conn = smtplib.SMTP_SSL(host, port, timeout=15, context=ctx)
    else:
        conn = smtplib.SMTP(host, port, timeout=15)
    with conn as s:
        if port != SMTPS_PORT:
            s.ehlo()
            try:
                s.starttls(context=ctx)
                s.ehlo()
            except smtplib.SMTPException:
                # STARTTLS 非対応など。証明書エラー (ssl.SSLError) はここで
                # 握りつぶさない (接続が壊れており、偽のサーバの可能性がある)
                if user:
                    raise InsecureConnectionError(
                        "SMTPサーバと暗号化(STARTTLS)した接続ができないため、"
                        "パスワードを送らずに中止しました"
                        "（ポートを465にするか、サーバの設定を確認してください）"
                    ) from None
                # ログインしないならパスワードは流れないので、そのまま続行
        if user:
            s.login(user, settings.get("smtp_pass", ""))
        s.sendmail(sender, recipients, message)


def send_email(settings: dict, subject: str, text: str) -> None:
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    sender = settings.get("mail_from") or settings.get("smtp_user")
    recipients = [a.strip() for a in settings["mail_to"].split(",") if a.strip()]
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    smtp_send(settings, sender, recipients, msg.as_string())


def deliver(settings: dict, text: str, subject: str = "zatsumu 通知") -> list[str]:
    """設定された全チャネルへ同期送信し、成功したチャネル名を返す."""
    sent = []
    if settings.get("slack_webhook_url"):
        try:
            send_slack(settings["slack_webhook_url"], text)
            sent.append("slack")
        except Exception as e:  # noqa: BLE001
            print(f"Slack通知失敗: {e}")
    if settings.get("smtp_host") and settings.get("mail_to"):
        try:
            send_email(settings, subject, text)
            sent.append("email")
        except Exception as e:  # noqa: BLE001
            print(f"メール通知失敗: {safe_error(e, settings)}")
    return sent


def deliver_async(settings: dict, text: str) -> None:
    """呼び出し元をブロックせずに送信する (着席/退席など)."""
    if not channels(settings):
        return
    threading.Thread(
        target=deliver, args=(dict(settings), text), daemon=True
    ).start()
