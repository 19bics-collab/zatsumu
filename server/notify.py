"""通知 (Slack Incoming Webhook / SMTP メール).

管理画面の設定に応じて、着席・退席・長時間在席アラートなどを通知する。
送信は呼び出し元をブロックしないようバックグラウンドスレッドで行う。
実際の送信失敗はログに出すのみで、アプリの動作は止めない。
"""
import json
import smtplib
import threading
import urllib.request
from email.mime.text import MIMEText


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


def send_email(settings: dict, subject: str, text: str) -> None:
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    sender = settings.get("mail_from") or settings.get("smtp_user")
    recipients = [a.strip() for a in settings["mail_to"].split(",") if a.strip()]
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    port = int(settings.get("smtp_port") or 587)
    with smtplib.SMTP(settings["smtp_host"], port, timeout=15) as s:
        s.ehlo()
        try:
            s.starttls()
            s.ehlo()
        except smtplib.SMTPException:
            pass  # TLS 非対応サーバはそのまま続行
        if settings.get("smtp_user"):
            s.login(settings["smtp_user"], settings.get("smtp_pass", ""))
        s.sendmail(sender, recipients, msg.as_string())


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
            print(f"メール通知失敗: {e}")
    return sent


def deliver_async(settings: dict, text: str) -> None:
    """呼び出し元をブロックせずに送信する (着席/退席など)."""
    if not channels(settings):
        return
    threading.Thread(
        target=deliver, args=(dict(settings), text), daemon=True
    ).start()
