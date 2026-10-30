"""メール送受信 (SMTP / IMAP) の通信の安全性のテスト.

- サーバ証明書を検証する context が渡っていること
- 465番ポートは最初から暗号化 (SMTP_SSL)
- 暗号化できない接続ではパスワードを送らない
- エラー文に設定の秘密値を出さない
"""
import smtplib
import ssl

import pytest


class FakeSMTP:
    """smtplib.SMTP の代わり。呼ばれた操作を記録する."""

    instances: list = []
    starttls_error: Exception | None = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.init_context = context
        self.tls_context = None
        self.logged = None
        self.sent = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        if FakeSMTP.starttls_error:
            raise FakeSMTP.starttls_error
        self.tls_context = context

    def login(self, u, p):
        self.logged = (u, p)

    def sendmail(self, frm, to, msg):
        self.sent = (frm, to, msg)


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture()
def fake_smtp(monkeypatch):
    from server import notify

    FakeSMTP.instances = []
    FakeSMTP.starttls_error = None
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP


CFG = {"smtp_host": "smtp.e", "smtp_port": "587", "smtp_user": "u@e",
       "smtp_pass": "s3cret-pass", "mail_from": "from@e", "mail_to": "a@e.com"}


def _is_verifying(ctx) -> bool:
    return (isinstance(ctx, ssl.SSLContext)
            and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname)


def test_smtp_starttls_gets_verifying_context(fake_smtp):
    from server import notify

    notify.send_email(CFG, "件名", "本文")
    s = fake_smtp.instances[-1]
    assert type(s) is FakeSMTP and s.port == 587
    assert _is_verifying(s.tls_context)          # 証明書を検証する設定
    assert s.logged == ("u@e", "s3cret-pass") and s.sent


def test_smtp_port_465_uses_implicit_tls(fake_smtp):
    from server import notify

    notify.send_email({**CFG, "smtp_port": "465"}, "件名", "本文")
    s = fake_smtp.instances[-1]
    assert type(s) is FakeSMTPSSL and s.port == 465
    assert _is_verifying(s.init_context)
    assert s.tls_context is None                 # SMTP_SSL では STARTTLS しない
    assert s.logged and s.sent


def test_smtp_starttls_failure_with_login_does_not_send_password(fake_smtp):
    from server import notify

    fake_smtp.starttls_error = smtplib.SMTPNotSupportedError("no STARTTLS")
    with pytest.raises(notify.InsecureConnectionError) as ei:
        notify.send_email(CFG, "件名", "本文")
    s = fake_smtp.instances[-1]
    assert s.logged is None and s.sent is None   # パスワードもメールも送っていない
    assert "s3cret-pass" not in str(ei.value) and "u@e" not in str(ei.value)


def test_smtp_certificate_error_is_not_swallowed(fake_smtp):
    """証明書エラーは「STARTTLS 非対応」扱いにせず、ログインなしでも中止する."""
    from server import notify

    fake_smtp.starttls_error = ssl.SSLCertVerificationError("certificate verify failed")
    with pytest.raises(ssl.SSLError):
        notify.send_email({**CFG, "smtp_user": ""}, "件名", "本文")
    assert fake_smtp.instances[-1].sent is None


def test_smtp_starttls_failure_without_login_still_sends(fake_smtp):
    """ログインしない (パスワードが流れない) 送信は従来どおり続行する."""
    from server import notify

    fake_smtp.starttls_error = smtplib.SMTPNotSupportedError("no STARTTLS")
    notify.send_email({**CFG, "smtp_user": "", "smtp_pass": ""}, "件名", "本文")
    s = fake_smtp.instances[-1]
    assert s.logged is None and s.sent is not None


def test_mail_reply_uses_same_smtp_path(fake_smtp):
    """返信送信 (mail.py) も同じ接続処理を通る (465 対応・証明書検証)."""
    from server import mail

    mail.send_reply({**CFG, "smtp_port": "465"}, "to@e.com", "Re: x", "本文")
    s = fake_smtp.instances[-1]
    assert type(s) is FakeSMTPSSL and _is_verifying(s.init_context)
    assert s.sent[1] == ["to@e.com"]


def test_safe_error_hides_secret_settings():
    from server import notify

    cfg = {"smtp_pass": "p@ss-123", "imap_pass": "imap-pw-9", "anthropic_api_key": ""}
    msg = notify.safe_error(RuntimeError("login failed for p@ss-123 / imap-pw-9"), cfg)
    assert "p@ss-123" not in msg and "imap-pw-9" not in msg and "***" in msg


class FakeIMAP:
    instances: list = []
    starttls_error: Exception | None = None

    def __init__(self, host, port, ssl_context=None, timeout=None):
        self.host, self.port = host, port
        self.init_context = ssl_context
        self.tls_context = None
        self.logged = None
        self.closed = False
        FakeIMAP.instances.append(self)

    def starttls(self, ssl_context=None):
        if FakeIMAP.starttls_error:
            raise FakeIMAP.starttls_error
        self.tls_context = ssl_context

    def login(self, u, p):
        self.logged = (u, p)

    def shutdown(self):
        self.closed = True


class FakeIMAPSSL(FakeIMAP):
    pass


@pytest.fixture()
def fake_imap(monkeypatch):
    from server import mail

    FakeIMAP.instances = []
    FakeIMAP.starttls_error = None
    monkeypatch.setattr(mail.imaplib, "IMAP4", FakeIMAP)
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeIMAPSSL)
    return FakeIMAP


IMAP_CFG = {"imap_host": "imap.e", "imap_port": "993", "imap_user": "u@e",
            "imap_pass": "imap-secret", "imap_use_ssl": 1}


def test_imap_ssl_gets_verifying_context(fake_imap):
    from server import mail

    m = mail._imap_connect(IMAP_CFG)
    assert type(m) is FakeIMAPSSL and _is_verifying(m.init_context)
    assert m.logged == ("u@e", "imap-secret")


def test_imap_starttls_gets_verifying_context(fake_imap):
    from server import mail

    m = mail._imap_connect({**IMAP_CFG, "imap_use_ssl": 0, "imap_port": "143"})
    assert type(m) is FakeIMAP and _is_verifying(m.tls_context)
    assert m.logged


def test_imap_starttls_failure_does_not_send_password(fake_imap):
    from server import mail

    fake_imap.starttls_error = RuntimeError("STARTTLS not supported")
    with pytest.raises(RuntimeError) as ei:
        mail._imap_connect({**IMAP_CFG, "imap_use_ssl": 0, "imap_port": "143"})
    m = fake_imap.instances[-1]
    assert m.logged is None and m.closed
    assert "imap-secret" not in str(ei.value)
