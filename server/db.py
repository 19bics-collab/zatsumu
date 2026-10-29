"""SQLite helpers for the zatsumu server."""
import os
import re
import sqlite3
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

DB_PATH = (
    Path(
        os.environ.get("ZATSUMU_DATA_DIR")
        or Path(__file__).resolve().parent.parent / "data"
    )
    / "zatsumu.db"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    token TEXT NOT NULL UNIQUE,
    is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    clock_in TEXT NOT NULL,
    clock_out TEXT
);
CREATE TABLE IF NOT EXISTS screenshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    taken_at TEXT NOT NULL,
    path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    target_user_id INTEGER,
    session_id INTEGER,
    detail TEXT,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    date TEXT NOT NULL,
    body TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    notified_at TEXT,             -- 日報通知を送った時刻 (NULL=未通知・重複通知防止)
    UNIQUE(user_id, date)
);
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS leave_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    date TEXT NOT NULL,
    leave_type TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by INTEGER,
    UNIQUE(user_id, date)
);
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    date TEXT NOT NULL,            -- 修正対象の日 (YYYY-MM-DD ローカル)
    requested_in TEXT,            -- 希望の着席時刻 "HH:MM" (任意)
    requested_out TEXT,           -- 希望の退席時刻 "HH:MM" (任意)
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',   -- pending/approved/rejected
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by INTEGER
);
CREATE TABLE IF NOT EXISTS mails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE, -- 重複取り込み防止 (Message-ID ヘッダ)
    imap_uid INTEGER,
    from_addr TEXT NOT NULL DEFAULT '',
    from_name TEXT NOT NULL DEFAULT '',
    to_addr TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,       -- UTC ISO
    fetched_at TEXT NOT NULL,        -- UTC ISO
    priority INTEGER NOT NULL DEFAULT 2,      -- 1=高 2=中 3=低
    priority_reason TEXT NOT NULL DEFAULT '',
    priority_source TEXT NOT NULL DEFAULT '', -- 'ai' / 'rule'
    status TEXT NOT NULL DEFAULT 'unhandled', -- unhandled/replied/archived/pending_approval(確認待ち)
    draft_reply TEXT NOT NULL DEFAULT '',
    draft_source TEXT NOT NULL DEFAULT '',    -- 'ai' / 'template'
    draft_generated_at TEXT,
    reply_subject TEXT NOT NULL DEFAULT '',
    reply_body TEXT NOT NULL DEFAULT '',      -- 実際に送信した本文
    replied_at TEXT,
    replied_by TEXT NOT NULL DEFAULT '',      -- 送信した管理者名(スナップショット)
    references_hdr TEXT NOT NULL DEFAULT '',  -- スレッド返信用 References ヘッダ
    cc_addr TEXT NOT NULL DEFAULT '',         -- 受信メールの Cc ヘッダ (全員に返信の候補)
    reply_cc TEXT NOT NULL DEFAULT '',        -- 実際に CC で送ったアドレス (", " 区切り)
    reply_bcc TEXT NOT NULL DEFAULT '',       -- 実際に BCC で送ったアドレス (", " 区切り)
    category TEXT NOT NULL DEFAULT '',        -- 問い合わせの種類 (空=未分類。この機能より前のメール)
    -- 担当の人 (NULL=担当なし)。その人を削除したら担当なしに戻す
    assignee_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    -- 送った返信の Message-ID (相手がその返信に返事をくれたとき、同じやり取りだと分かるように)
    reply_message_id TEXT NOT NULL DEFAULT '',
    -- 「返事をしていないメール」としてお知らせした時刻 (NULL=まだ。1通につき1回だけ知らせる)
    reminded_at TEXT,
    -- 最後に「対応不要」にした時刻 (週ごとのまとめで数える用。今も対応不要かは status で見る)
    archived_at TEXT,
    -- Notion のデータベース (案件DB) に登録したページ (空=まだ。二重に登録しないように残す。
    -- 「もう一度登録」したら新しいページの方にする) と、登録した時刻・人 (server/notion.py)
    notion_page_id TEXT NOT NULL DEFAULT '',
    notion_page_url TEXT NOT NULL DEFAULT '',
    notion_registered_at TEXT,
    notion_registered_by TEXT NOT NULL DEFAULT '',
    -- 届いたアドレス (mail_accounts)。NULL=そのアドレスを一覧から削除した
    -- (メールは残す。画面では「削除したアドレス」)
    account_id INTEGER REFERENCES mail_accounts(id) ON DELETE SET NULL,
    -- スタッフ (メール担当者) の下書きの「確認をお願いする」(status='pending_approval')。
    -- 頼んだ人 (名前のスナップショット)・時刻・ひとこと と、頼んだときの CC・BCC (", " 区切り)。
    -- 返信文は draft_reply に入る。管理者が「承認して送信」したら approved_by に名前を残す
    requested_by TEXT NOT NULL DEFAULT '',
    requested_at TEXT,
    approval_note TEXT NOT NULL DEFAULT '',
    draft_cc TEXT NOT NULL DEFAULT '',
    draft_bcc TEXT NOT NULL DEFAULT '',
    approved_by TEXT NOT NULL DEFAULT '',
    -- 管理者が「差し戻す」した人・時刻・コメント (スタッフの返信画面に出す。次に頼んだら消す)
    returned_by TEXT NOT NULL DEFAULT '',
    returned_at TEXT,
    return_note TEXT NOT NULL DEFAULT '',
    -- 相手 (お客様)。HPのお問い合わせフォームから届くメールは、どのお客様の分も差出人が同じ
    -- (wordpress@… など) なので、受信したときに Reply-To か本文の「メールアドレス: …」から
    -- お客様を決めて覚える (server/mail.py の decide_contact)。やり取りのまとまり・返信の宛先に使う。
    -- reply_to は届いたメールの Reply-To のアドレス。contact_source は相手をどこから決めたか
    -- ('reply_to' / 'body' / 'from'。'' = まだ決めていない。前の版が入れたメール)。
    -- contact_via_form はフォームから届いたメールか (受信箱の「HPフォーム経由」)。
    -- contact_addr が空のフォームのメール (本文にアドレスが無い) は、ほかのメールとまとめない
    reply_to TEXT NOT NULL DEFAULT '',
    contact_addr TEXT NOT NULL DEFAULT '',
    contact_name TEXT NOT NULL DEFAULT '',
    contact_source TEXT NOT NULL DEFAULT '',
    contact_via_form INTEGER NOT NULL DEFAULT 0,
    -- 実際に返信を送った宛先 (空 = 前の版が送った。そのときは差出人 from_addr に送っている) と、
    -- メール担当者が「確認をお願いする」ときに選んだ宛先 (管理者も同じ宛先に送る)
    reply_sent_to TEXT NOT NULL DEFAULT '',
    draft_to TEXT NOT NULL DEFAULT '',
    -- フォームの送信元に返信してしまっていた (お客様に届いていない) メールの注意を、
    -- 「お客様に連絡した」で消した時刻と人
    misdirect_checked_at TEXT,
    misdirect_checked_by TEXT NOT NULL DEFAULT '',
    -- 取引先ごとのまとめ用 (server/clients.py)。contact_domain は相手のアドレスの @ の後ろ
    -- (小文字)。DB のトリガーで、メールを入れたとき・相手を決め直したときに自動で入る
    -- (前の版のコードが入れたメールにも入る)。contact_company はHPのフォームの「会社名」
    -- (フォームから届いたメールだけ。取引先の名前の候補)
    contact_domain TEXT NOT NULL DEFAULT '',
    contact_company TEXT NOT NULL DEFAULT ''
);
-- 取引先 (管理者が名前を付けた・まとめたもの。server/clients.py)。名前を付けていない取引先は
-- 表に入れず、メールの相手のアドレスから決まった分け方で毎回まとめる (会社のドメインごと・
-- 個人のメールはアドレスごと)。取引先を消しても、メールは変えない (決まった分け方に戻るだけ)
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',    -- メモ (担当者・物件など。管理者が書く)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- 取引先にまとめるアドレス・ドメイン。pattern は「taro@abc.co.jp」(そのアドレス) か
-- 「@abc.co.jp」(そのドメインと、下の tokyo.abc.co.jp などのアドレス全部)。同じものを2つの
-- 取引先には入れない。アドレスの決まりはドメインの決まりより強く (会社の中の1人だけを別の
-- 取引先に分けられる)、ドメインの決まりは一番長く合うものが強い
CREATE TABLE IF NOT EXISTS client_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    pattern TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_client_rules_client ON client_rules(client_id);
-- 受信するメールアドレス (kurosu@・info@ など。server/mail.py)。1つずつ IMAP で受信して
-- 1つの受信箱にまとめ、返信は届いたアドレスを差出人にして送る。
-- パスワード (imap_pass・smtp_pass) は画面・API には出さない (入っているかだけ返す)
CREATE TABLE IF NOT EXISTS mail_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL DEFAULT '',            -- 受信箱に出す名前 (例: 黒須・info)
    imap_host TEXT NOT NULL DEFAULT '',
    imap_port TEXT NOT NULL DEFAULT '993',
    imap_ssl INTEGER NOT NULL DEFAULT 1,       -- SSL で接続する (993番ポートなど)
    imap_user TEXT NOT NULL DEFAULT '',
    imap_pass TEXT NOT NULL DEFAULT '',
    imap_folder TEXT NOT NULL DEFAULT 'INBOX',
    imap_sent_folder TEXT NOT NULL DEFAULT '', -- 返信の控えを保存するフォルダ (空なら自動で探す)
    save_sent INTEGER NOT NULL DEFAULT 1,      -- 送った返信を、このメールボックスの送信済みにも残すか
    from_addr TEXT NOT NULL DEFAULT '',        -- 返信の差出人 (「名前 <アドレス>」も可)
    -- 送信のログイン (空なら「返信の送信（SMTP）」の共通の設定で送る)
    smtp_user TEXT NOT NULL DEFAULT '',
    smtp_pass TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,        -- 受信するか (止めても、届いたメールは残る)
    sort_order INTEGER NOT NULL DEFAULT 0,     -- 並び順 (小さいほど上)
    created_at TEXT NOT NULL,
    -- 受信の続きの位置 (UIDVALIDITY と、最後に取り込んだ UID。差分だけ読むため)
    state_uidvalidity TEXT NOT NULL DEFAULT '',
    state_last_uid INTEGER NOT NULL DEFAULT 0,
    -- 受信の様子 (最後に受信できた時刻・直近の失敗の理由と時刻。受信できたら理由は空にする)
    fetch_ok_at TEXT,
    fetch_error TEXT NOT NULL DEFAULT '',
    fetch_error_at TEXT
);
-- 届いたメールが「どのメールへの返事か」を示す Message-ID (In-Reply-To・References の中身)。
-- 返信画面の「この相手とのやり取り」で、同じ話のメールを探すのに使う (server/mail.py)。
-- References は長くなることがあるので、新しい方から決まった数だけ残す
CREATE TABLE IF NOT EXISTS mail_refs (
    mail_id INTEGER NOT NULL REFERENCES mails(id) ON DELETE CASCADE,
    ref TEXT NOT NULL,
    PRIMARY KEY (mail_id, ref)
);
-- 受信メールの添付ファイル (server/mail.py)。中身はデータフォルダの
-- mail_attachments/<mail_id>/<id> に置く (元のファイル名は保存先のパスに使わない)。
-- 大きすぎるなどで保存しなかったものも、画面で知らせるため名前と理由 (skip_reason) を残す
CREATE TABLE IF NOT EXISTS mail_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mail_id INTEGER NOT NULL REFERENCES mails(id) ON DELETE CASCADE,
    filename TEXT NOT NULL DEFAULT '',      -- 画面に出す名前 (整えたもの)
    content_type TEXT NOT NULL DEFAULT '',  -- 画像は中身を見て決めた種類
    size INTEGER NOT NULL DEFAULT 0,        -- バイト数
    path TEXT NOT NULL DEFAULT '',          -- 保存先 (mail_attachments からの相対。保存しなかったものは空)
    skip_reason TEXT NOT NULL DEFAULT '',   -- 保存しなかった理由 (空=保存した)
    created_at TEXT NOT NULL
);
-- よく使う返信文 (定型文)。返信画面で選んで本文に入れる。
-- {相手の名前} などの差し込みは、入れるときにそのメールの内容へ置き換える (server/mail.py)
CREATE TABLE IF NOT EXISTS mail_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                     -- 返信画面の一覧に出す名前
    body TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,  -- 並び順 (小さいほど上)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- よく使うアドレス (アドレス帳。server/address_book.py)。返信画面の宛先・CC・BCC に入れるとき、
-- 候補の一番先に出す。同じアドレス (英字の大文字・小文字だけ違うものも) は1つだけ。
-- 変えるのは管理者だけ (メール担当者は候補に出るのを使うだけ)
CREATE TABLE IF NOT EXISTS address_book (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',          -- 名前 (例: 山田 太郎・ABC商事 経理)。空でもよい
    email TEXT NOT NULL,                    -- アドレスだけ (「名前 <…>」の形にはしない)
    note TEXT NOT NULL DEFAULT '',          -- メモ (例: 清掃の担当・平日のみ)。候補を探すときにも使う
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_address_book_email ON address_book(lower(email));
-- 新しい端末からの管理者ログインのメール確認 (server/devices.py)。
-- 秘密 (端末トークン・確認コード) は sha256 のハッシュだけを保存する
CREATE TABLE IF NOT EXISTS device_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',   -- User-Agent の先頭 (どの端末か見分ける用)
    ip TEXT NOT NULL DEFAULT '',      -- 確認したときのアクセス元 IP
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,       -- 最後に使った時刻 (90日使わなければ失効)
    revoked_at TEXT                   -- 取り消した時刻 (NULL=有効)
);
CREATE TABLE IF NOT EXISTS device_challenges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    code_hash TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,  -- 間違えた回数も含む試行回数
    used_at TEXT                          -- 使い切った/無効にした時刻
);
-- 集計・参照でよく使う列のインデックス(IF NOT EXISTS で冪等)
CREATE INDEX IF NOT EXISTS idx_device_tokens_user ON device_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_device_challenges_user ON device_challenges(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_mails_status ON mails(status);
CREATE INDEX IF NOT EXISTS idx_mail_templates_order ON mail_templates(sort_order, id);
CREATE INDEX IF NOT EXISTS idx_mail_attachments_mail ON mail_attachments(mail_id);
CREATE INDEX IF NOT EXISTS idx_mails_received ON mails(received_at);
-- 同じ相手 (差出人のアドレス。大文字・小文字は区別しない) のメールを新しい順に探す用
CREATE INDEX IF NOT EXISTS idx_mails_from_lower ON mails(lower(from_addr), received_at);
CREATE INDEX IF NOT EXISTS idx_mail_refs_ref ON mail_refs(ref);
CREATE INDEX IF NOT EXISTS idx_sessions_user_open ON sessions(user_id, clock_out);
CREATE INDEX IF NOT EXISTS idx_sessions_clock_in ON sessions(clock_in);
CREATE INDEX IF NOT EXISTS idx_screenshots_user_taken ON screenshots(user_id, taken_at);
CREATE INDEX IF NOT EXISTS idx_screenshots_taken ON screenshots(taken_at);
CREATE INDEX IF NOT EXISTS idx_leave_date ON leave_requests(date);
CREATE INDEX IF NOT EXISTS idx_corrections_status ON corrections(status);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);
"""

# 休暇の種別
LEAVE_TYPES = ["有給休暇", "半休", "欠勤", "特別休暇", "その他"]

# メール画面の役割 (users.mail_role)。'staff' = メール担当者 (受信箱を見て下書きを作り、
# 管理者に「確認をお願いする」。送信・設定はできない)。管理者はこの値に関係なく全部使える
MAIL_ROLE_STAFF = "staff"


def mail_role(user) -> str:
    """メール画面での役割: 'admin' (管理者) / 'staff' (メール担当者) / '' (使えない)."""
    if user["is_admin"]:
        return "admin"
    return "staff" if user["mail_role"] == MAIL_ROLE_STAFF else ""

# 全社設定の既定値 (整数)。settings テーブルの値で上書きされる
INT_SETTINGS = {
    "capture_min_interval": 300,   # 撮影の最短間隔(秒)
    "capture_max_interval": 900,   # 撮影の最長間隔(秒) 平均10分=約6回/時
    "capture_quality": 60,         # JPEG品質 (10-95)
    "capture_blur": 0,             # ぼかし強度 (0=なし)
    "capture_enabled": 1,          # 全社の撮影ON/OFF
    # キャプチャ保存日数 (0=自動削除なし)。環境変数は初期値として機能する
    "retention_days": int(os.environ.get("ZATSUMU_RETENTION_DAYS", "30")),
    "alert_hours": 6,              # 連続在席アラート(時間)
    "daily_target_minutes": 480,   # 1日の予定勤務時間(分) 既定8時間
    "notify_clock": 0,             # 着席/退席を通知するか
    "notify_alert": 1,             # 長時間在席を通知するか
    "notify_journal": 1,           # 日報が保存されたら通知先(mail_to)へ通知するか
    "notify_stall": 1,             # 画面が変化しない(停滞)場合に通知するか
    "stall_threshold": 95,         # 直前のキャプチャとの一致率がこの%以上で「同じ画面」
    "stall_alert_count": 3,        # 同じ画面が連続でこの回数続いたら通知
    "clockout_reminder": 1,        # 終業時刻を過ぎても未退勤の本人へリマインドするか
    "idle_threshold": 120,         # 無操作がこの秒数以上なら「離席/非稼働」とみなす(稼働率計算用)
    "imap_use_ssl": 1,             # IMAP接続にSSLを使う (993番ポートなど)
    "mail_fetch_enabled": 1,       # メールの自動受信ON/OFF (imap_host設定時のみ動作)
    # メールの自動受信の間隔(分) 1〜60。画面で変えたら、再起動しなくても次の受信から使う
    "mail_fetch_interval_min": 5,
    "mail_auto_draft": 1,          # 受信時にAIで返信下書きを自動生成するか
    "mail_retention_days": 180,    # 受信メールの保存日数 (0=自動削除なし)
    "mail_fetch_days": 7,          # 初回取り込み時に遡る日数
    "notify_mail_high": 1,         # 優先度[高]のメール受信を通知するか
    "mail_save_sent": 1,           # 送信した返信を IMAP の送信済みフォルダにも保存するか
    # 返事をしていない (未対応のままの) メールを Slack/メールでお知らせするか (server/mail.py)
    "mail_remind_enabled": 1,
    "mail_remind_hours_high": 2,     # 優先度[高]は、届いてから何時間でお知らせするか (0=しない)
    "mail_remind_hours_normal": 24,  # 優先度[中]・[低]は、何時間でお知らせするか (0=しない)
    # メールの週ごとのまとめを Slack/メールで送るか (server/mail.py)。使い始めるまでは送らない
    "mail_report_enabled": 0,
    "mail_report_weekday": 0,      # 送る曜日 (0=月曜 … 6=日曜)
    "mail_report_hour": 9,         # 送る時刻 (時。集計と同じタイムゾーン=既定は日本時間)
    # Notion に登録するとき、AI に案件名とほかの列も入れてもらうか (AI のキーがあるときだけ)
    "notion_ai_fill": 1,
    # メール担当者 (スタッフ) の返信は、管理者が確認してから送る (「確認をお願いする」を使う)。
    # オフでもスタッフは送信できない (下書きを保存して、管理者に伝える)
    "mail_require_approval": 1,
}

# 誰でも作れるメール (フリーメール・携帯電話の会社・プロバイダ・アドレスを隠す中継) のドメイン。
# 同じドメインでも別々の人なので、会社としてまとめない (取引先の最初の一覧・server/mail.py の
# 「自社のドメイン」「同じ会社」の見分けで使う。一覧はここ1か所だけ)
FREE_MAIL_DOMAINS = (
    # 誰でも作れるメール (国内)
    "gmail.com", "googlemail.com", "yahoo.co.jp", "yahoo.ne.jp", "ymail.ne.jp",
    "outlook.com", "outlook.jp", "hotmail.com", "hotmail.co.jp", "live.jp", "live.com",
    "msn.com", "icloud.com", "me.com", "mac.com", "excite.co.jp", "infoseek.jp", "livedoor.com",
    "goo.jp", "mail.goo.ne.jp",
    # 誰でも作れるメール (海外。民泊の海外のゲストが使うもの)
    "yahoo.com", "yahoo.co.uk", "yahoo.fr", "yahoo.de", "yahoo.com.tw", "yahoo.com.hk",
    "yahoo.com.sg", "yahoo.com.au", "yahoo.co.in", "hotmail.co.uk", "hotmail.fr", "hotmail.de",
    "hotmail.it", "outlook.fr", "outlook.de", "live.co.uk", "live.fr", "aol.com",
    "protonmail.com", "proton.me", "pm.me", "tutanota.com", "tuta.io", "fastmail.com",
    "gmx.com", "gmx.net", "gmx.de", "gmx.at", "web.de", "t-online.de", "freenet.de",
    "mail.com", "email.com", "zoho.com", "orange.fr", "wanadoo.fr", "free.fr", "laposte.net",
    "sfr.fr", "libero.it", "virgilio.it", "seznam.cz", "wp.pl", "o2.pl", "interia.pl",
    "btinternet.com", "sky.com", "comcast.net", "verizon.net", "att.net", "sbcglobal.net",
    "bigpond.com", "rediffmail.com",
    # 中国・台湾・香港・韓国・ロシアのフリーメール
    "qq.com", "foxmail.com", "163.com", "126.com", "yeah.net", "sina.com", "sina.cn",
    "sohu.com", "aliyun.com", "139.com", "hinet.net", "netvigator.com",
    "naver.com", "hanmail.net", "daum.net", "nate.com", "kakao.com",
    "yandex.ru", "yandex.com", "ya.ru", "mail.ru", "bk.ru", "inbox.ru", "list.ru", "rambler.ru",
    # 携帯電話の会社のメール
    "docomo.ne.jp", "mopera.net", "ezweb.ne.jp", "au.com", "softbank.ne.jp", "i.softbank.jp",
    "vodafone.ne.jp", "disney.ne.jp", "ymobile.ne.jp", "y-mobile.ne.jp", "emobile.ne.jp",
    "pdx.ne.jp", "willcom.com", "wcm.ne.jp", "uqmobile.jp", "rakumail.jp",
    # インターネットの接続の会社のメール (プロバイダ)
    "nifty.com", "biglobe.ne.jp", "ocn.ne.jp", "so-net.ne.jp", "plala.or.jp", "dion.ne.jp",
    "ybb.ne.jp", "asahi-net.or.jp", "zaq.ne.jp", "zag.ne.jp", "jcom.home.ne.jp", "eonet.ne.jp",
    "odn.ne.jp", "infoweb.ne.jp", "hi-ho.ne.jp", "commufa.jp", "k-opti.com", "wakwak.com",
    "tiki.ne.jp", "dti.ne.jp", "sannet.ne.jp", "gol.com",
    # アドレスを隠す中継 (Apple の「メールを非公開」など。1人ずつ別のアドレス)
    "privaterelay.appleid.com", "duck.com", "mozmail.com",
)
# 予約サイト (ゲストとのやり取りを中継するアドレス)
BOOKING_RELAY_DOMAINS = (
    "airbnb.com", "airbnb.jp", "booking.com", "expedia.com", "expediapartnercentral.com",
    "agoda.com", "agoda-messaging.com", "trip.com", "hotels.com", "vrbo.com", "homeaway.com",
    "travel.rakuten.co.jp", "rakuten.co.jp", "jalan.net", "ikyu.com",
)
# 取引先ごとのまとめで、1人ずつに分けるドメインの最初の一覧 (設定 mail_client_personal_domains)。
# 設定を一度も保存していなければ、この一覧がそのまま使われる
DEFAULT_PERSONAL_DOMAINS = FREE_MAIL_DOMAINS + BOOKING_RELAY_DOMAINS

# 全社設定の既定値 (文字列)
STR_SETTINGS = {
    "company_name": "zatsumu",     # ヘッダー等に表示する会社名/サービス名
    "timezone": os.environ.get("ZATSUMU_TZ", "Asia/Tokyo"),  # 集計の基準TZ
    "work_start": "09:00",         # 勤務時間帯の目安(開始) タイムライン表示用
    "work_end": "18:00",           # 勤務時間帯の目安(終了)
    "clockout_reminder_time": "20:00",  # この時刻以降、未退勤の本人へ退勤リマインド
    "work_categories": "事務作業,現場",  # 作業区分(カンマ区切り、先頭が既定)
    "slack_webhook_url": "",       # Slack Incoming Webhook URL
    "mail_to": "",                 # 通知メール宛先(カンマ区切り)
    "smtp_host": "",               # SMTPサーバ (空ならメール無効)
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_pass": "",
    "mail_from": "",               # 差出人 (空なら smtp_user を使用)
    "imap_host": "",               # IMAPサーバ (空ならメール受信無効)
    "imap_port": "993",
    "imap_user": "",
    "imap_pass": "",
    "imap_folder": "INBOX",
    "imap_sent_folder": "",        # 返信の控えを保存するフォルダ (空なら自動で探す)
    "anthropic_api_key": "",       # Claude APIキー (空ならルール分類+定型文で動作)
    # 返信文の下書きに使うモデル (品質重視。空なら既定値)
    "anthropic_model": "claude-opus-5-5",
    # 優先度の判定に使うモデル。受信したメール全部に走るので安くて速いものを既定にする
    "anthropic_classify_model": "claude-haiku-5-5",
    "mail_signature": "",          # 返信メール末尾に付ける署名
    "mail_reply_instructions": "", # AI返信文生成時の追加指示 (社の文体・注意事項など)
    "mail_vip_addresses": "",      # 常に優先度[高]にする差出人 (カンマ区切り)
    # 件名/本文に含まれると優先度[高]にするキーワード (カンマ区切り)
    "mail_urgent_keywords": "至急,緊急,大至急,クレーム,苦情,障害,トラブル,重要,締切,期限,本日中,urgent,asap",
    # 問い合わせの種類 (1行に1つ。カンマ区切りも可)。受信時に AI かキーワードでどれか1つに
    # 仕分ける。「その他」は一覧から消しても必ず最後に入る (server/mail.py)
    "mail_categories": "\n".join([
        "清掃のご依頼", "予約・宿泊のお問い合わせ", "請求・お支払い",
        "運営代行のご相談", "営業・宣伝", "その他",
    ]),
    # 種類を決める言葉 (1行に「種類: 言葉, 言葉」)。AI を使わないときは上の行から順に調べ、
    # 件名・本文・差出人に最初に当てはまった種類にする。AI を使うときは判定の目安として伝える
    "mail_category_keywords": "\n".join([
        "清掃のご依頼: 清掃, クリーニング, リネン, シーツ, タオル, 掃除",
        "予約・宿泊のお問い合わせ: 予約, 宿泊, チェックイン, チェックアウト, キャンセル,"
        " ゲスト, airbnb, booking.com",
        "請求・お支払い: 請求, 支払, 振込, 振り込み, 入金, 領収書, インボイス",
        "運営代行のご相談: 運営代行, 民泊運営, 管理代行, 開業, 届出",
        "営業・宣伝: セミナー, キャンペーン, 配信停止, メルマガ, 広告, unsubscribe",
    ]),
    # 種類ごとの担当 (1行に「種類: 名前」)。その種類のメールが届いたら、登録されている
    # 同じ名前の人を担当にする (空なら担当は自動で入らない)
    "mail_category_assignees": "",
    # HPのお問い合わせフォームの送信元 (1行に1つ。アドレスか「@ドメイン」)。ここから届いたメールは、
    # 本文の「メールアドレス: …」の人を相手にする (返信もその人に送る)。wordpress@・noreply@ などは
    # 書かなくても分かる (server/mail.py の form_sender_kind)
    "mail_form_senders": "",
    # 返事をしていないメールのお知らせを止めておく時間帯 (「始まりの時-終わりの時」。
    # 集計と同じタイムゾーン=既定は日本時間。日をまたいでよい。空なら止めない)。
    # 止めている間の分は、時間帯が終わった後にまとめて送る
    "mail_remind_quiet": "21-8",
    # お知らせに載せるメール画面のアドレス (空ならサーバの設定 ZATSUMU_MAIL_DOMAIN などから作る)
    "mail_page_url": "",
    # 取引先ごとのまとめで、会社ではなく1人ずつに分けるメールのドメイン (1行に1つ)。
    # Gmail などの誰でも作れるメールと、Airbnb などの予約サイトの中継のアドレス。
    # ここに無いドメインは会社とみなし、同じドメインの人を1つの取引先にまとめる (server/clients.py)。
    # 「airbnb.com」は guest.airbnb.com などその下のドメインも含む
    "mail_client_personal_domains": "\n".join(DEFAULT_PERSONAL_DOMAINS),
    # Notion の連携 (インテグレーション) のシークレット (空なら「Notionに登録」は使わない)
    "notion_token": "",
    # 登録する Notion のデータベースの番号 (32文字。画面で URL を貼っても番号にそろえる)
    "notion_database_id": "",
    # Notion の列に入れるもの (1行に「列の名前 = 件名」。「」で囲むと決まった文字)。
    # 既定は「運営代行 案件DB」の列に合わせてある。無い列は登録のときに飛ばして知らせるだけ
    "notion_property_map": "\n".join([
        "案件名 = 件名",
        "問い合わせ日 = 受信日",
        "問い合わせ元 = 差出人",
        "ステータス = 「問い合わせ受付」",
        "ボール = 「インビックス」",
    ]),
}

# 後方互換: 旧名を参照しているコード向け
SETTING_DEFAULTS = INT_SETTINGS

# 定型文の初期値 (名前, 本文)。最初の1回だけ入れる (_seed_mail_templates)。
# 【 】の中は物件などに合わせて設定画面で書き換えてもらう前提
DEFAULT_MAIL_TEMPLATES = [
    ("チェックイン方法のご案内", """{相手の名前} 様

このたびはご予約いただき、誠にありがとうございます。
チェックインの方法について、下記のとおりご案内いたします。

■チェックイン：【15:00】以降
■チェックアウト：【10:00】まで
■お部屋への入り方：【玄関横のキーボックスに鍵が入っています。暗証番号はご到着の前日にお送りします】
■所在地：【物件の住所】

ご到着が遅くなる場合や、ご不明な点がございましたら、
このメールにご返信ください。
快適にお過ごしいただけますよう、心よりお待ちしております。"""),
    ("清掃のご依頼の受付", """{相手の名前} 様

いつも大変お世話になっております。
「{件名}」の件、{日付}付で清掃のご依頼を確かに承りました。

担当スタッフの手配を進め、作業日時が決まり次第、改めてご連絡いたします。
リネンの交換枚数や鍵の受け渡し方法などにご指定がございましたら、
このメールにご返信いただけますと幸いです。

引き続きどうぞよろしくお願いいたします。"""),
    ("お見積もりのお礼", """{相手の名前} 様

お世話になっております。
このたびは、弊社の清掃・リネン・運営代行サービスについて
お見積もりのご依頼をいただき、誠にありがとうございます。

「{件名}」の件、内容を拝見いたしました。
物件の所在地・間取り・ご希望の開始時期などを確認のうえ、
担当者よりお見積もりをお送りいたします。

ご不明な点やご要望がございましたら、どうぞお気軽にお申し付けください。
何卒よろしくお願い申し上げます。"""),
]
# 定型文の初期値を入れ終えた印 (settings の非公開キー。画面・API には出ない)
MAIL_TEMPLATES_SEEDED_KEY = "mail_state_templates_seeded"


def get_settings(conn: sqlite3.Connection) -> dict:
    stored = {
        r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")
    }
    out: dict = {
        k: int(stored.get(k, default)) for k, default in INT_SETTINGS.items()
    }
    out.update(
        {k: stored.get(k, default) for k, default in STR_SETTINGS.items()}
    )
    return out


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(int(value)) if key in INT_SETTINGS else str(value)),
    )


def _migrate(conn: sqlite3.Connection) -> None:
    """既存DBへの後方互換マイグレーション."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
    if "active" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
        )
    if "capture_enabled" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN capture_enabled INTEGER NOT NULL DEFAULT 1"
        )
    if "notify_enabled" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN notify_enabled INTEGER NOT NULL DEFAULT 1"
        )
    if "email" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
    if "team_id" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN team_id INTEGER")
    # メール画面の役割 (''=使えない / 'staff'=メール担当者。管理者はこの値に関係なく使える)
    if "mail_role" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN mail_role TEXT NOT NULL DEFAULT ''")
    shcols = [r["name"] for r in conn.execute("PRAGMA table_info(screenshots)")]
    if "sig" not in shcols:
        conn.execute("ALTER TABLE screenshots ADD COLUMN sig TEXT")
    if "similarity" not in shcols:  # 直前のキャプチャとの一致率(%)
        conn.execute("ALTER TABLE screenshots ADD COLUMN similarity INTEGER")
    if "stall" not in shcols:       # 同じ画面が連続した回数
        conn.execute(
            "ALTER TABLE screenshots ADD COLUMN stall INTEGER NOT NULL DEFAULT 0"
        )
    if "idle" not in shcols:        # 撮影時点の無操作秒数 (null=未取得/Web撮影)
        conn.execute("ALTER TABLE screenshots ADD COLUMN idle INTEGER")
    scols = [r["name"] for r in conn.execute("PRAGMA table_info(sessions)")]
    if "category" not in scols:
        conn.execute("ALTER TABLE sessions ADD COLUMN category TEXT")
    if "alert_notified" not in scols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN alert_notified INTEGER NOT NULL DEFAULT 0"
        )
    if "clockout_reminded" not in scols:  # 退勤リマインド済みフラグ(重複通知防止)
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN clockout_reminded INTEGER NOT NULL DEFAULT 0"
        )
    jcols = [r["name"] for r in conn.execute("PRAGMA table_info(journals)")]
    if "notified_at" not in jcols:  # 日報通知済み時刻(重複通知防止・NULLなら未通知)
        conn.execute("ALTER TABLE journals ADD COLUMN notified_at TEXT")
    # 全員に返信 (CC)。以前に取り込んだメールは Cc が分からないので空のまま
    # (その場合の CC 候補は To の人だけになる)
    mcols = [r["name"] for r in conn.execute("PRAGMA table_info(mails)")]
    for col in ("cc_addr", "reply_cc", "reply_bcc"):
        if col not in mcols:
            conn.execute(f"ALTER TABLE mails ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    # 問い合わせの種類と担当。以前に取り込んだメールは種類が空 (画面では「未分類」)・担当なし
    if "category" not in mcols:
        conn.execute("ALTER TABLE mails ADD COLUMN category TEXT NOT NULL DEFAULT ''")
    if "assignee_user_id" not in mcols:
        conn.execute("ALTER TABLE mails ADD COLUMN assignee_user_id INTEGER"
                     " REFERENCES users(id) ON DELETE SET NULL")
    # 送った返信の Message-ID。以前に送った返信は分からないので空のまま
    # (その返信への返事も、References に元のメールが入っていれば同じやり取りだと分かる)
    if "reply_message_id" not in mcols:
        conn.execute("ALTER TABLE mails ADD COLUMN reply_message_id TEXT NOT NULL DEFAULT ''")
    # 返事をしていないメールのお知らせ。以前に取り込んだメールも NULL (まだ知らせていない) だが、
    # 古いものをまとめて知らせないよう、知らせるかどうかは取り込んだ時刻で決める (server/mail.py)
    if "reminded_at" not in mcols:
        conn.execute("ALTER TABLE mails ADD COLUMN reminded_at TEXT")
    # 週ごとのまとめの「対応不要にした」の数。前から対応不要のメールは、操作の記録から時刻を入れる
    if "archived_at" not in mcols:
        conn.execute("ALTER TABLE mails ADD COLUMN archived_at TEXT")
        _fill_archived_at(conn)
    # Notion に登録したページ。以前に取り込んだメールは「まだ登録していない」
    for col in ("notion_page_id", "notion_page_url", "notion_registered_by"):
        if col not in mcols:
            conn.execute(f"ALTER TABLE mails ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    if "notion_registered_at" not in mcols:
        conn.execute("ALTER TABLE mails ADD COLUMN notion_registered_at TEXT")
    # 届いたアドレス (複数のアドレスの受信)。以前に取り込んだメールは、旧設定の受信サーバから
    # 作る1つ目のアドレスにする (migrate_mail_accounts)
    if "account_id" not in mcols:
        conn.execute("ALTER TABLE mails ADD COLUMN account_id INTEGER"
                     " REFERENCES mail_accounts(id) ON DELETE SET NULL")
    # スタッフの下書きの確認 (確認のお願い・承認・差し戻し)。以前のメールは「頼んでいない」
    for col in ("requested_by", "approval_note", "draft_cc", "draft_bcc", "approved_by",
                "returned_by", "return_note"):
        if col not in mcols:
            conn.execute(f"ALTER TABLE mails ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    for col in ("requested_at", "returned_at"):
        if col not in mcols:
            conn.execute(f"ALTER TABLE mails ADD COLUMN {col} TEXT")
    # 相手 (お客様) の列。前からあるメールは、まず差出人を相手にしておく (すぐ終わる)。
    # フォームから届いたメールは、起動したときに本文から相手を読み直す
    # (server/mail.py の fill_missing_contacts。contact_source が空のものだけ)
    if "contact_addr" not in mcols:
        for col in ("reply_to", "contact_addr", "contact_name", "contact_source"):
            if col not in mcols:
                conn.execute(f"ALTER TABLE mails ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE mails SET contact_addr = from_addr, contact_name = from_name")
    if "contact_via_form" not in mcols:
        conn.execute("ALTER TABLE mails ADD COLUMN contact_via_form INTEGER NOT NULL DEFAULT 0")
    for col in ("reply_sent_to", "draft_to", "misdirect_checked_by"):
        if col not in mcols:
            conn.execute(f"ALTER TABLE mails ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    if "misdirect_checked_at" not in mcols:
        conn.execute("ALTER TABLE mails ADD COLUMN misdirect_checked_at TEXT")
    # 相手を決めずに入ったメール (自動更新で前の版に戻っていた間に受信したもの・手で入れたもの) は、
    # 入ったときに差出人を相手にしておく (まとまり・件数がすぐ正しく出るように)。
    # 相手の決め方 (contact_source) は空のままにし、次の起動のときに決め直す
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_mails_contact_default AFTER INSERT ON mails"
        " WHEN NEW.contact_source = '' AND NEW.contact_addr = ''"
        " BEGIN UPDATE mails SET contact_addr = NEW.from_addr, contact_name = NEW.from_name"
        " WHERE id = NEW.id; END")
    # 同じ相手のメールを新しい順に探す・数える用
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mails_contact_lower"
                 " ON mails(lower(contact_addr), received_at)")
    # フォームから届いたメールだけの索引 (フォームの送信元に送ってしまった返信を数える用。小さい)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mails_via_form ON mails(replied_at)"
                 " WHERE contact_via_form = 1")
    _migrate_client_columns(conn, mcols)
    # 列が用意できた後にインデックスを作成する (担当での絞り込み・人を削除したときの付け替え用)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mails_assignee ON mails(assignee_user_id)")
    # アドレスでの絞り込み・アドレスを削除したときの付け替え用
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mails_account ON mails(account_id)")
    # 種類での絞り込み (受信箱のタブ) と、1分ごとの「未分類のメールがあるか」の確かめ用
    # (未分類が1通もないと、索引がなければ表を全部読んでしまう)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mails_category ON mails(category)")
    # 送った返信への返事を探す用 (この列も後から足したので、ここで作る)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mails_reply_msgid ON mails(reply_message_id)")
    # 宛先・CC・BCC の候補 (前に返信を送った人) を探す用。返信を送ったメールだけの索引で、
    # 送った宛先・CC・BCC も入れておく (本文の大きい表を読まずに探せる。何万通あっても速い)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mails_sent_to"
                 " ON mails(replied_at, reply_sent_to, reply_cc, reply_bcc)"
                 " WHERE replied_at IS NOT NULL")
    # team_id 列が用意できた後にインデックスを作成する(SCHEMA時点では未追加のため)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_team ON users(team_id)")
    _seed_mail_templates(conn)
    migrate_mail_accounts(conn)


# 相手のアドレスのドメイン (@ の後ろを小文字にしたもの。@ が無ければ空) を出す SQL の式。
# トリガー・最初の1回の書き込みで同じ式を使う (server/clients.py の domain_of と同じ結果)
CONTACT_DOMAIN_SQL = (
    "CASE WHEN instr(contact_addr, '@') > 0"
    " THEN lower(trim(substr(contact_addr, instr(contact_addr, '@') + 1))) ELSE '' END")
# 前からあるメールの contact_domain を入れ終えた印 (settings の非公開キー)
CONTACT_DOMAIN_FILLED_KEY = "mail_state_contact_domain_filled"


def _migrate_client_columns(conn: sqlite3.Connection, mcols: list[str]) -> None:
    """取引先ごとのまとめ用の列・トリガー・索引を足す (何度呼んでも同じ。足すだけ).

    contact_domain は DB のトリガーで入れる (メールを入れたとき・相手のアドレスを変えたとき)。
    自動更新で前の版のコードに戻っても、前の版が入れたメールにも入る (前の版はこの列を知らない
    ので、書くのは DB だけ)。前からあるメールの分は、最初の1回だけまとめて入れる
    (1回の UPDATE なので、何万通あってもすぐ終わる。終えたら印を残す)。
    """
    for col in ("contact_domain", "contact_company"):
        if col not in mcols:
            conn.execute(f"ALTER TABLE mails ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    # その行の今の contact_addr から計算する (NEW の値ではなく)。受信したとき相手を決めずに
    # 入ったメールは、別のトリガー (trg_mails_contact_default) が相手を入れるので、どちらが
    # 先に動いても最後は今の相手のドメインになる
    for name, when in (("trg_mails_contact_domain_ins", "AFTER INSERT ON mails"),
                       ("trg_mails_contact_domain_upd", "AFTER UPDATE OF contact_addr ON mails")):
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {name} {when}"
            f" BEGIN UPDATE mails SET contact_domain = {CONTACT_DOMAIN_SQL}"
            f" WHERE id = NEW.id AND contact_domain != {CONTACT_DOMAIN_SQL}; END")
    if not conn.execute("SELECT 1 FROM settings WHERE key = ?",
                        (CONTACT_DOMAIN_FILLED_KEY,)).fetchone():
        conn.execute(f"UPDATE mails SET contact_domain = {CONTACT_DOMAIN_SQL}"
                     f" WHERE contact_addr != '' AND contact_domain != {CONTACT_DOMAIN_SQL}")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, '1')",
                     (CONTACT_DOMAIN_FILLED_KEY,))
        # すぐ確定する (この接続でこの後に行う通信の間、書き込みロックを持ち越さない)
        conn.commit()
    # 取引先の一覧で、相手のアドレスごとの数・最後の日時・名前を、表を読まずに数える用
    # (この索引だけで足りる。本文の大きい表を読まないので、何万通あっても速い)。
    # ドメインで始まるので、取引先 (ドメイン) ごとのメールを探すのにも使う。
    # 取り込んだ時刻 (fetched_at) は、先の日付のメールを取り込んだ時刻で数えるのに使う。
    # 開発中の版で fetched_at の無い索引を作った DB は、作り直す (前の版のコードはこの索引を
    # 名前で使わないので、作り直しても前の版はそのまま動く)
    cols = [r[2] for r in conn.execute("PRAGMA index_info(idx_mails_clients)")]
    if cols and "fetched_at" not in cols:
        conn.execute("DROP INDEX idx_mails_clients")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mails_clients ON mails(contact_domain, contact_addr,"
        " received_at, fetched_at, status, replied_at, contact_name, contact_company)")


# 受信するメールアドレスの一覧 (mail_accounts) を使い始めた印 (settings の非公開キー)。
# これがあれば、旧設定 (受信サーバが1つだけの imap_host など) からアドレスを作らない
# (アドレスを全部消した後に、旧設定から勝手に作り直さないように)
MAIL_ACCOUNTS_MIGRATED_KEY = "mail_state_accounts_migrated"
# 旧設定から作ったアドレスの番号 (自動更新で前の版に戻っていた間に取り込んだメールを、
# このアドレスにする。server/mail.py の fill_missing_accounts)
MAIL_LEGACY_ACCOUNT_KEY = "mail_state_legacy_account_id"
# どの番号のメールまで、届いたアドレスを確かめ終えたか (同じく fill_missing_accounts)
MAIL_ACCOUNTS_FILLED_KEY = "mail_state_accounts_filled_upto"
# 旧設定のときの受信の続きの位置と受信の様子 (server/mail.py の _get_state で読んでいたキー)
_LEGACY_FETCH_STATE_KEYS = ("mail_state_uidvalidity", "mail_state_last_uid",
                            "mail_state_fetch_ok_at", "mail_state_fetch_error",
                            "mail_state_fetch_error_at")
# 受信箱に出す名前の最大文字数 (server/mail.py の ACCOUNT_LABEL_MAX_CHARS と同じ)
_ACCOUNT_LABEL_MAX_CHARS = 20


def _legacy_sender(s: dict) -> str:
    """旧設定で返信の差出人にしていたもの (差出人メール、無ければ SMTP ユーザー。アドレスの形のものだけ)."""
    for key in ("mail_from", "smtp_user"):
        value = re.sub(r"[\r\n\x00]+", " ", str(s.get(key) or "")).strip()
        addr = parseaddr(value)[1]
        if "@" in addr and not re.search(r"\s", addr):
            return value
    return ""


def account_label_from(address: str) -> str:
    """アドレスから受信箱に出す名前を作る (info@example.com → info)."""
    local = parseaddr(str(address or ""))[1].partition("@")[0].strip()
    if not local:
        local = str(address or "").partition("@")[0].strip()
    return local[:_ACCOUNT_LABEL_MAX_CHARS] or "メール"


def migrate_mail_accounts(conn: sqlite3.Connection) -> None:
    """旧設定 (受信サーバが1つだけ) から、受信するメールアドレスの1つ目を作る (最初の1回だけ).

    旧設定の受信サーバが入っていて、アドレスがまだ1つも無いときだけ作る。パスワードも
    そのまま移し、受信の続きの位置 (UIDVALIDITY・最後の UID) と受信の様子も引き継ぐので、
    取り込み済みのメールを読み直したり、取り逃したりしない。前からあるメールはこのアドレスにする。
    旧設定のキーは消さない (自動更新で前の版に戻ったときも、そのまま受信できるように)。
    受信をまだ使っていない (旧設定も空) なら印も付けない (後で旧設定が入ったら、そのときに作る)。
    """
    if conn.execute("SELECT 1 FROM settings WHERE key = ?",
                    (MAIL_ACCOUNTS_MIGRATED_KEY,)).fetchone():
        return
    has_accounts = conn.execute("SELECT 1 FROM mail_accounts LIMIT 1").fetchone()
    s = get_settings(conn)
    if not has_accounts and not str(s.get("imap_host") or "").strip():
        return
    # 印を先に書く。同時に開いた別の接続とは書き込みロックで順番になり、
    # 後の方は印が既にあるので (rowcount=0) 作らない (二重に作らない)
    cur = conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, '1')",
                       (MAIL_ACCOUNTS_MIGRATED_KEY,))
    if cur.rowcount and not conn.execute("SELECT 1 FROM mail_accounts LIMIT 1").fetchone():
        _account_from_legacy(conn, s)
    # すぐ確定する (この接続でこの後に行う通信の間、書き込みロックを持ち越さない)
    conn.commit()


def _account_from_legacy(conn: sqlite3.Connection, s: dict) -> None:
    """旧設定の受信サーバを1つ目のアドレスにして、前からあるメールをそのアドレスにする."""
    state = {r["key"]: r["value"] for r in conn.execute(
        f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(_LEGACY_FETCH_STATE_KEYS))})",
        _LEGACY_FETCH_STATE_KEYS)}
    try:
        last_uid = max(int(state.get("mail_state_last_uid") or 0), 0)
    except ValueError:
        last_uid = 0
    error = state.get("mail_state_fetch_error") or ""
    sender = _legacy_sender(s)
    user = str(s.get("imap_user") or "").strip()
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO mail_accounts (label, imap_host, imap_port, imap_ssl, imap_user,"
        " imap_pass, imap_folder, imap_sent_folder, save_sent, from_addr, enabled,"
        " sort_order, created_at, state_uidvalidity, state_last_uid, fetch_ok_at,"
        " fetch_error, fetch_error_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?)",
        (account_label_from(sender or user), str(s.get("imap_host") or "").strip(),
         str(s.get("imap_port") or "").strip() or "993", int(bool(s.get("imap_use_ssl"))),
         user, str(s.get("imap_pass") or ""),
         str(s.get("imap_folder") or "").strip() or "INBOX",
         str(s.get("imap_sent_folder") or "").strip(), int(bool(s.get("mail_save_sent"))),
         sender, now, state.get("mail_state_uidvalidity") or "", last_uid,
         state.get("mail_state_fetch_ok_at") or None, error,
         (state.get("mail_state_fetch_error_at") or None) if error else None),
    )
    account_id = cur.lastrowid
    conn.execute("UPDATE mails SET account_id = ? WHERE account_id IS NULL", (account_id,))
    newest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM mails").fetchone()[0]
    for key, value in ((MAIL_LEGACY_ACCOUNT_KEY, account_id),
                       (MAIL_ACCOUNTS_FILLED_KEY, newest)):
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, str(value)))


# 操作の記録 (audit_log) の「メールを変えた」の書き方 (server/app.py の mail_patch)。
# 例: "mail=12 priority=1, status=archived"
_AUDIT_MAIL_RE = re.compile(r"mail=(\d+) ")
_AUDIT_ARCHIVED_RE = re.compile(r"(?:^| |, )status=archived(?:,|$)")


def _fill_archived_at(conn: sqlite3.Connection) -> None:
    """前から「対応不要」のメールに、対応不要にした時刻 (archived_at) を入れる (列を足したときだけ).

    時刻は操作の記録 (audit_log) の、そのメールを最後に対応不要にした時刻。記録が無いもの
    (デモで入れたもの等) は空のまま (週ごとのまとめの「対応不要にした」に数えないだけ)。
    記録を1回だけ順に読む (メールごとに記録全体を探すと、記録が多いときに起動が遅くなる)。
    """
    last: dict[int, str] = {}
    for r in conn.execute(
            "SELECT detail, at FROM audit_log WHERE action = 'mail_update'"
            " AND detail LIKE '%status=archived%'"):
        mt = _AUDIT_MAIL_RE.match(r["detail"] or "")
        if mt and _AUDIT_ARCHIVED_RE.search(r["detail"][mt.end():]):
            mail_id = int(mt.group(1))
            last[mail_id] = max(last.get(mail_id, ""), r["at"] or "")
    conn.executemany(
        "UPDATE mails SET archived_at = ? WHERE id = ? AND status = 'archived'"
        " AND archived_at IS NULL",
        [(at, mail_id) for mail_id, at in last.items() if at])
    # すぐ確定する (この後の起動の処理の間、書き込みのロックを持ち越さない)
    conn.commit()


def _seed_mail_templates(conn: sqlite3.Connection) -> None:
    """定型文の初期値を入れる (最初の1回だけ).

    入れたら settings に印を残すので、利用者が全部消しても入れ直さない。
    既に定型文がある DB (手で入れた等) には足さず、印だけ付ける。
    """
    if conn.execute("SELECT 1 FROM settings WHERE key = ?",
                    (MAIL_TEMPLATES_SEEDED_KEY,)).fetchone():
        return
    # 印を先に書く。同時に開いた別の接続とは書き込みロックで順番になり、
    # 後の方は印が既にあるので (rowcount=0) 入れない (二重に入らない)
    cur = conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, '1')",
                       (MAIL_TEMPLATES_SEEDED_KEY,))
    if cur.rowcount and not conn.execute(
            "SELECT 1 FROM mail_templates LIMIT 1").fetchone():
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "INSERT INTO mail_templates (name, body, sort_order, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [(name, body, i, now, now)
             for i, (name, body) in enumerate(DEFAULT_MAIL_TEMPLATES)])
    # すぐ確定する (この接続でこの後に行う通信の間、書き込みロックを持ち越さない)
    conn.commit()


def work_categories(conn: sqlite3.Connection) -> list[str]:
    """設定された作業区分のリスト (先頭が既定)."""
    raw = get_settings(conn)["work_categories"]
    cats = [c.strip() for c in raw.split(",") if c.strip()]
    return cats or ["事務作業"]


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # FastAPI runs sync deps in a threadpool but async endpoints on the
    # event loop thread, so the same connection crosses threads.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


@contextmanager
def get_db(db_path: Path | str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_user(conn: sqlite3.Connection, name: str, is_admin: bool = False) -> dict:
    token = secrets.token_urlsafe(24)
    cur = conn.execute(
        "INSERT INTO users (name, token, is_admin) VALUES (?, ?, ?)",
        (name, token, int(is_admin)),
    )
    return {"id": cur.lastrowid, "name": name, "token": token, "is_admin": is_admin}


def user_by_token(conn: sqlite3.Connection, token: str):
    return conn.execute(
        "SELECT * FROM users WHERE token = ? AND active = 1", (token,)
    ).fetchone()


def open_session(conn: sqlite3.Connection, user_id: int):
    return conn.execute(
        "SELECT * FROM sessions WHERE user_id = ? AND clock_out IS NULL "
        "ORDER BY clock_in DESC LIMIT 1",
        (user_id,),
    ).fetchone()
