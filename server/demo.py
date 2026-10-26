"""デモモード.

ZATSUMU_DEMO=1 で起動すると、DB が空の場合に固定トークンのデモユーザーと
架空の勤務データ・キャプチャ画像を自動投入する。本番では使わないこと
(トークンが公知のため)。

  管理者: demo-admin / メンバー: demo-tanaka, demo-suzuki, demo-sato
  (田中 はメール担当者。demo-tanaka でメール画面に入ると、下書きと「確認をお願いする」だけできる)
"""
import io
import random
import sqlite3
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

from . import db, mail, tz

DEMO_USERS = [
    ("管理者", "demo-admin", 1),
    ("田中", "demo-tanaka", 0),
    ("鈴木", "demo-suzuki", 0),
    ("佐藤", "demo-sato", 0),
]


def _fake_desktop(path: Path, accent: str) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1280, 720), "#1e1e2e")
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 1280, 28), fill="#11111b")
    d.rectangle((40, 60, 820, 660), fill="#181825")
    for i in range(10):
        d.rectangle(
            (60, 90 + i * 50, 60 + random.randint(220, 620), 110 + i * 50),
            fill=accent,
        )
    d.rectangle((860, 60, 1240, 400), fill="#313244")
    d.rectangle((0, 692, 1280, 720), fill="#11111b")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=65)


def _fake_floor_plan() -> bytes:
    """デモのメールに添付する、小さな間取り図の画像 (PNG)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (360, 240), "#ffffff")
    d = ImageDraw.Draw(img)
    d.rectangle((20, 20, 340, 220), outline="#374151", width=4)   # 外壁
    d.line((200, 20, 200, 150), fill="#374151", width=3)          # 部屋の仕切り
    d.line((20, 150, 340, 150), fill="#374151", width=3)
    d.rectangle((215, 165, 325, 205), fill="#bfdbfe")            # 浴室
    d.rectangle((35, 165, 120, 205), fill="#fde68a")             # キッチン
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def seed(conn: sqlite3.Connection, screenshot_dir: Path,
         attachment_dir: Path | None = None) -> bool:
    """DB が空なら投入して True を返す.

    attachment_dir を渡すと、デモのメールに添付ファイルも付ける。
    """
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return False

    ids = {}
    for name, token, is_admin in DEMO_USERS:
        cur = conn.execute(
            "INSERT INTO users (name, token, is_admin) VALUES (?, ?, ?)",
            (name, token, is_admin),
        )
        ids[name] = cur.lastrowid

    # デモ用チームと所属
    team_ids = {}
    for tname in ("営業部", "開発部"):
        team_ids[tname] = conn.execute(
            "INSERT INTO teams (name) VALUES (?)", (tname,)
        ).lastrowid
    conn.execute("UPDATE users SET team_id = ? WHERE id IN (?, ?)",
                 (team_ids["営業部"], ids["田中"], ids["佐藤"]))
    conn.execute("UPDATE users SET team_id = ? WHERE id = ?",
                 (team_ids["開発部"], ids["鈴木"]))
    # メール担当者 (メール画面で下書きを作り、管理者に「確認をお願いする」) の確認用
    conn.execute("UPDATE users SET mail_role = ? WHERE id = ?",
                 (db.MAIL_ROLE_STAFF, ids["田中"]))

    now_utc = datetime.now(timezone.utc)
    now = now_utc.astimezone(tz.TZ)
    accents = {"田中": "#45475a", "鈴木": "#3a5a40", "佐藤": "#5a3a50"}

    def add_session(name, start, end, category="事務作業"):
        conn.execute(
            "INSERT INTO sessions (user_id, clock_in, clock_out, category) "
            "VALUES (?, ?, ?, ?)",
            (
                ids[name],
                start.astimezone(timezone.utc).isoformat(),
                end.astimezone(timezone.utc).isoformat() if end else None,
                category,
            ),
        )

    def add_shots(name, start, end):
        t = start
        while True:
            t += timedelta(minutes=random.randint(6, 14))
            if t >= end:
                break
            rel = f"{ids[name]}/{t.strftime('%Y%m%d_%H%M%S')}.jpg"
            _fake_desktop(screenshot_dir / rel, accents[name])
            conn.execute(
                "INSERT INTO screenshots (user_id, taken_at, path) VALUES (?, ?, ?)",
                (ids[name], t.astimezone(timezone.utc).isoformat(), rel),
            )

    # 田中: 過去5日間 午前=事務作業 / 午後=現場
    for back in range(5, 0, -1):
        day = (now - timedelta(days=back)).date()
        add_session("田中", datetime.combine(day, dtime(9, 0), tz.TZ),
                    datetime.combine(day, dtime(12, 0), tz.TZ), "事務作業")
        # 午後は日により 18:00 / 19:00 で終わり、残業・不足が分かるように
        end_h = 19 if back % 2 else 17
        add_session("田中", datetime.combine(day, dtime(13, 0), tz.TZ),
                    datetime.combine(day, dtime(end_h, 0), tz.TZ), "現場")

    # 今日: 田中=2時間前から着席中(現場) / 鈴木=45分前から着席中(事務作業)
    t_start = now - timedelta(hours=2)
    add_session("田中", t_start, None, "現場")
    add_shots("田中", t_start, now)
    s_start = now - timedelta(minutes=45)
    add_session("鈴木", s_start, None, "事務作業")
    add_shots("鈴木", s_start, now)
    add_session("佐藤", now - timedelta(hours=6), now - timedelta(hours=3),
                "事務作業")
    add_shots("佐藤", now - timedelta(hours=6), now - timedelta(hours=3))

    # デモ用の日報
    today = now.strftime("%Y-%m-%d")
    yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    journals = [
        ("田中", today, "・午前: 請求書の処理\n・午後: A社現場で設備点検\n明日は見積書を作成予定。"),
        ("田中", yday, "月次レポートの作成と、B社向け提案資料のレビュー対応。"),
        ("鈴木", today, "問い合わせ対応(5件)と、マニュアルの更新作業を実施。"),
        ("佐藤", today, "午前のみ勤務。経費精算をまとめて処理しました。"),
    ]
    for name, date, body in journals:
        conn.execute(
            "INSERT INTO journals (user_id, date, body, updated_at) VALUES (?,?,?,?)",
            (ids[name], date, body, now_utc.isoformat()),
        )

    # デモ用の休暇申請 (承認待ち1件・承認済み1件)
    d5 = (now + timedelta(days=5)).strftime("%Y-%m-%d")
    d10 = (now + timedelta(days=10)).strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO leave_requests (user_id, date, leave_type, reason, status, "
        "created_at) VALUES (?,?,?,?, 'pending', ?)",
        (ids["鈴木"], d5, "有給休暇", "私用のため", now_utc.isoformat()),
    )
    conn.execute(
        "INSERT INTO leave_requests (user_id, date, leave_type, reason, status, "
        "created_at, decided_at, decided_by) VALUES (?,?,?,?, 'approved', ?, ?, ?)",
        (ids["田中"], d10, "有給休暇", "通院", now_utc.isoformat(),
         now_utc.isoformat(), ids["管理者"]),
    )

    # デモ用の受信メール (優先度分類・返信下書き画面の確認用)。
    # 宛先の info@ は自分たちの受信箱なので、全員に返信の CC 候補から外れるよう差出人に入れる
    db.set_setting(conn, "mail_from", "info@example.com")

    # 種類ごとの担当の確認用 (運営代行のご相談は管理者が担当)
    db.set_setting(conn, "mail_category_assignees", "運営代行のご相談: 管理者")

    # 受信するメールアドレスの確認用 (info@ と kurosu@ の2つ。受信箱に「宛先」と「アドレス」の
    # 絞り込みが出る)。デモの受信サーバは無いので、自動受信は止めておく
    # (「今すぐ受信」や「接続テスト」を押すと、受信できなかった理由が出る)
    db.set_setting(conn, "mail_fetch_enabled", 0)
    accounts = {}
    for i, (label, addr) in enumerate((("info", "info@example.com"),
                                       ("黒須", "kurosu@example.com"))):
        accounts[addr] = conn.execute(
            "INSERT INTO mail_accounts (label, imap_host, imap_user, imap_pass, from_addr,"
            " sort_order, created_at, fetch_ok_at) VALUES (?, 'imap.example.com', ?,"
            " 'demo-password', ?, ?, ?, ?)",
            (label, addr, addr, i, tz.utc_iso(now_utc), tz.utc_iso(now_utc))).lastrowid
    db.set_setting(conn, db.MAIL_ACCOUNTS_MIGRATED_KEY, "1")

    def add_mail(mins_ago, from_name, from_addr, subject, body, prio, reason,
                 draft="", status="unhandled", cc="", category="", assignee=None,
                 in_reply_to="", reply=None, to="info@example.com"):
        # reply: こちらが送った返信 (本文, 何分前に送ったか, 送った人, CC)。
        # 返信画面の「この相手とのやり取り」の確認用
        received = (now_utc - timedelta(minutes=mins_ago)).isoformat()
        reply_body, reply_mins, replied_by, reply_cc = reply or ("", None, "", "")
        # 対応不要にした時刻 (週ごとのまとめの確認用。届いてから30分後に対応不要にした)
        archived = ((now_utc - timedelta(minutes=mins_ago - 30)).isoformat()
                    if status == "archived" else None)
        mail_id = conn.execute(
            "INSERT INTO mails (message_id, from_addr, from_name, to_addr,"
            " cc_addr, subject, body, received_at, fetched_at, priority,"
            " priority_reason, priority_source, status, draft_reply,"
            " draft_source, draft_generated_at, category, assignee_user_id,"
            " references_hdr, reply_subject, reply_body, reply_cc, replied_at,"
            " replied_by, reply_message_id, archived_at, account_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            # 取り込んだ時刻は届いた時刻と同じにする (週ごとのまとめ・お知らせの待ち時間の確認用)
            (f"<demo-{mins_ago}@example.com>", from_addr, from_name,
             to, cc, subject, body, received,
             received, prio, reason, "rule", status, draft,
             "template" if draft else "",
             now_utc.isoformat() if draft else None, category, assignee,
             in_reply_to, mail.reply_subject(subject) if reply else "", reply_body,
             reply_cc,
             (now_utc - timedelta(minutes=reply_mins)).isoformat() if reply else None,
             replied_by, f"<demo-reply-{mins_ago}@example.com>" if reply else "",
             archived, accounts[to]),
        ).lastrowid
        mail.save_refs(conn, mail_id, in_reply_to, in_reply_to)
        return mail_id

    # 「この相手とのやり取り」の確認用: 佐々木さんとの前のやり取り (返信済み) と、
    # その返信に別のアドレス (佐々木 部長) から届いた返事 (ヘッダでつながる)
    add_mail(4400, "佐々木", "Sasaki@Example.com", "物件の下見について",
             "来週、物件の下見をお願いできますでしょうか。\n"
             "火曜か水曜の午後ですと助かります。", 2, "通常",
             status="replied", category="運営代行のご相談", assignee=ids["管理者"],
             reply=("佐々木 様\n\nお世話になっております。\n"
                    "下見の件、承知いたしました。水曜日の14時はいかがでしょうか。\n"
                    "よろしくお願いいたします。", 4300, "管理者",
                    "佐々木 部長 <sasaki-bucho@example.com>"))
    add_mail(4200, "佐々木 部長", "sasaki-bucho@example.com", "Re: 物件の下見について",
             "水曜日14時で問題ございません。\n当日は私も同席いたします。", 2, "通常",
             status="archived", category="運営代行のご相談",
             in_reply_to="<demo-reply-4400@example.com>")

    # 種類を入れていないメール (この機能より前に取り込んだメール =「未分類」の表示の確認用)
    add_mail(20, "山本 商事", "yamamoto@example.com",
             "【至急】納品物の不具合について",
             "本日納品いただいた製品に不具合がありました。\n"
             "至急ご確認のうえ、対応方法をご連絡ください。",
             1, "「至急」を含む",
             "山本 商事 様\n\nお世話になっております。\n"
             "納品物の不具合の件、大変申し訳ございません。\n"
             "至急状況を確認のうえ、本日中に対応方法をご連絡いたします。")
    estimate_id = add_mail(
        90, "佐々木", "sasaki@example.com",
        "お見積もりのご相談",
        "新規案件についてお見積もりをお願いしたく、"
        "ご都合の良い日程を教えてください。\n間取り図を添付します。",
        2, "通常",
        "佐々木 様\n\nお世話になっております。\n"
        "お見積もりのご相談ありがとうございます。\n"
        "候補日程を確認のうえ、改めてご連絡差し上げます。",
        # 全員に返信 (CC) の確認用
        cc="佐々木 部長 <sasaki-bucho@example.com>, keiri@example.com",
        category="運営代行のご相談", assignee=ids["管理者"])
    if attachment_dir is not None:
        # 添付ファイルの確認用 (画像は縮小表示される・大きすぎる動画は保存していない例)
        plan = _fake_floor_plan()
        mail.save_attachments(conn, attachment_dir, estimate_id, [
            {"filename": "間取り図.png", "content_type": "image/png",
             "size": len(plan), "data": plan, "skip": ""},
            {"filename": "物件の動画.mov", "content_type": "video/quicktime",
             "size": 52 * 1024 * 1024, "data": None, "skip": "too_large"},
        ], now_utc.isoformat())
    add_mail(2900, "山本 商事", "yamamoto@example.com", "納品日程のご確認",
             "先日ご依頼した件の納品日程を教えてください。", 2, "通常",
             status="replied",
             reply=("山本 商事 様\n\nお世話になっております。\n"
                    "納品は今週金曜日を予定しております。\nよろしくお願いいたします。",
                    2880, "管理者", ""))
    # 「カレンダーに登録」の確認用: 清掃の日時と物件が書いてあるご依頼 (日付は3日後)
    day = (now_utc - timedelta(minutes=45)).astimezone(tz.TZ).date() + timedelta(days=3)
    md = f"{day.month}月{day.day}日（{'月火水木金土日'[day.weekday()]}）"
    add_mail(45, "鈴木 ホスト", "suzuki-host@example.com", f"清掃のご依頼（{md}）",
             "いつもお世話になっております。\n下記の日程で清掃をお願いできますでしょうか。\n\n"
             f"日時：{md}\n時間：１０：００～１３：００\n物件名：ビックス渋谷 201号室\n"
             "住所：東京都渋谷区神南1-2-3\n\nよろしくお願いいたします。",
             2, "通常", category="清掃のご依頼", to="kurosu@example.com")
    add_mail(240, "業界ニュース配信", "no-reply@newsletter.example.com",
             "【メルマガ】今週の業界トピックス",
             "今週の業界ニュースをお届けします。配信停止はこちら。",
             3, "自動配信・お知らせ", category="営業・宣伝")
    # 確認待ちの確認用: メール担当者の田中さんが下書きを作り、「確認をお願いする」を押したメール
    pending_id = add_mail(
        70, "伊藤", "ito@example.com", "チェックインの時間について",
        "来週の土曜日に宿泊予定の伊藤です。\n到着が20時ごろになりそうですが、"
        "チェックインはできますでしょうか。", 2, "通常",
        "伊藤 様\n\nお問い合わせいただき、ありがとうございます。\n"
        "20時ごろのご到着でも、チェックインいただけます。\n"
        "玄関横のキーボックスの暗証番号は、ご到着の前日にお送りいたします。\n"
        "どうぞよろしくお願いいたします。",
        category="予約・宿泊のお問い合わせ")
    conn.execute(
        "UPDATE mails SET status = 'pending_approval', requested_by = ?, requested_at = ?,"
        " approval_note = ? WHERE id = ?",
        ("田中", (now_utc - timedelta(minutes=10)).isoformat(),
         "遅い時間のチェックインで大丈夫か、確かめてください", pending_id))
    # HPのお問い合わせフォーム (WordPress の Contact Form 7) から届いたメールの確認用。
    # 差出人はどれも wordpress@ だが、相手は本文に書かれたお客様 (別々の人としてまとめない)
    form_footer = ("\n\n-- \nこのメールは ビックス (https://example.com) の"
                   "お問い合わせフォームから送信されました")
    add_mail(35, "WordPress", "wordpress@example.com", "【HPお問い合わせ】民泊の運営代行について",
             "差出人: 山田 花子 <hanako.yamada@example.net>\n題名: 民泊の運営代行について\n\n"
             "会社名：株式会社やまだ不動産\nお名前：山田 花子\n"
             "メールアドレス：hanako.yamada@example.net\n電話番号：03-1234-5678\n\n"
             "メッセージ本文:\n都内で2部屋の民泊を始めたいと考えています。\n"
             "運営代行の料金を教えてください。" + form_footer,
             2, "通常", category="運営代行のご相談")
    add_mail(60, "WordPress", "wordpress@example.com", "【HPお問い合わせ】清掃のご依頼",
             "【お名前】\n小林 健\n【メールアドレス】\nken.kobayashi@example.org\n"
             "【お問い合わせ内容】\n来月から週2回の清掃をお願いしたいです。" + form_footer,
             2, "通常", category="清掃のご依頼")
    add_mail(130, "WordPress", "wordpress@example.com", "【HPお問い合わせ】お電話でのご相談希望",
             "お名前：中村 様\n電話番号：090-0000-1111\n\n"
             "メッセージ本文:\nメールより電話で相談したいです。夕方に電話をください。" + form_footer,
             2, "通常", category="その他")
    # 前の版で、返信がフォームの送信元 (wordpress@) に送られていたメール (お客様に届いていない)
    add_mail(5200, "WordPress", "wordpress@example.com", "【HPお問い合わせ】料金表がほしいです",
             "差出人: 加藤 真理 <mari.kato@example.net>\n題名: 料金表がほしいです\n\n"
             "メッセージ本文:\n清掃の料金表を送っていただけますか。" + form_footer,
             2, "通常", status="replied", category="清掃のご依頼",
             reply=("加藤 様\n\nお問い合わせありがとうございます。\n料金表を添付いたします。",
                    5100, "管理者", ""))
    add_mail(1500, "高橋", "takahashi@example.com",
             "先日の打ち合わせのお礼",
             "先日はお時間をいただきありがとうございました。",
             2, "通常", status="replied", category="その他", to="kurosu@example.com")

    conn.commit()
    return True
