"""デモモード.

ZATSUMU_DEMO=1 で起動すると、DB が空の場合に固定トークンのデモユーザーと
架空の勤務データ・キャプチャ画像を自動投入する。本番では使わないこと
(トークンが公知のため)。

  管理者: demo-admin / メンバー: demo-tanaka, demo-suzuki, demo-sato
  (田中 はメール担当者。demo-tanaka でメール画面に入ると、下書きと「確認をお願いする」だけできる)
  送信箱には、Webメール・Gmail から送ったメール (取り込んだもの) も入る
"""
import io
import random
import sqlite3
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

from . import db, mail, mail_threads, tz

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
                 in_reply_to="", reply=None, to="info@example.com", rcpt=""):
        # reply: こちらが送った返信 (本文, 何分前に送ったか, 送った人, CC)。
        # 返信画面の「この相手とのやり取り」の確認用。to は届いたアドレス、rcpt は To ヘッダ
        # (空なら届いたアドレス。問い合わせの自動返信の控えは、お客様宛て)
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
             rcpt or to, cc, subject, body, received,
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
    add_mail(4400, "佐々木", "Sasaki@Sasaki-Shoji.example.co.jp", "物件の下見について",
             "来週、物件の下見をお願いできますでしょうか。\n"
             "火曜か水曜の午後ですと助かります。", 2, "通常",
             status="replied", category="運営代行のご相談", assignee=ids["管理者"],
             reply=("佐々木 様\n\nお世話になっております。\n"
                    "下見の件、承知いたしました。水曜日の14時はいかがでしょうか。\n"
                    "よろしくお願いいたします。", 4300, "管理者",
                    "佐々木 部長 <bucho@sasaki-shoji.example.co.jp>"))
    add_mail(4200, "佐々木 部長", "bucho@sasaki-shoji.example.co.jp", "Re: 物件の下見について",
             "水曜日14時で問題ございません。\n当日は私も同席いたします。", 2, "通常",
             status="archived", category="運営代行のご相談",
             in_reply_to="<demo-reply-4400@example.com>")

    # 種類を入れていないメール (この機能より前に取り込んだメール =「未分類」の表示の確認用)
    add_mail(20, "山本 商事", "yamamoto@yamamoto-shoji.example.co.jp",
             "【至急】納品物の不具合について",
             "本日納品いただいた製品に不具合がありました。\n"
             "至急ご確認のうえ、対応方法をご連絡ください。",
             1, "「至急」を含む",
             "山本 商事 様\n\nお世話になっております。\n"
             "納品物の不具合の件、大変申し訳ございません。\n"
             "至急状況を確認のうえ、本日中に対応方法をご連絡いたします。")
    estimate_id = add_mail(
        90, "佐々木", "sasaki@sasaki-shoji.example.co.jp",
        "お見積もりのご相談",
        "新規案件についてお見積もりをお願いしたく、"
        "ご都合の良い日程を教えてください。\n間取り図を添付します。",
        2, "通常",
        "佐々木 様\n\nお世話になっております。\n"
        "お見積もりのご相談ありがとうございます。\n"
        "候補日程を確認のうえ、改めてご連絡差し上げます。",
        # 全員に返信 (CC) の確認用
        cc="佐々木 部長 <bucho@sasaki-shoji.example.co.jp>, keiri@sasaki-shoji.example.co.jp",
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
    add_mail(2900, "山本 商事", "yamamoto@yamamoto-shoji.example.co.jp", "納品日程のご確認",
             "先日ご依頼した件の納品日程を教えてください。", 2, "通常",
             status="replied",
             reply=("山本 商事 様\n\nお世話になっております。\n"
                    "納品は今週金曜日を予定しております。\nよろしくお願いいたします。",
                    2880, "管理者", ""))
    # 「カレンダーに登録」の確認用: 清掃の日時と物件が書いてあるご依頼 (日付は3日後)
    day = (now_utc - timedelta(minutes=45)).astimezone(tz.TZ).date() + timedelta(days=3)
    md = f"{day.month}月{day.day}日（{'月火水木金土日'[day.weekday()]}）"
    add_mail(45, "鈴木 ホスト", "suzuki.host.demo@gmail.com", f"清掃のご依頼（{md}）",
             "いつもお世話になっております。\n下記の日程で清掃をお願いできますでしょうか。\n\n"
             f"日時：{md}\n時間：１０：００～１３：００\n物件名：ビックス渋谷 201号室\n"
             "住所：東京都渋谷区神南1-2-3\n\nよろしくお願いいたします。",
             2, "通常", category="清掃のご依頼", to="kurosu@example.com")
    # 一斉送信のメールの確認用: 大勢 (12人) に送られたお知らせ。全員に返信は最初は付けない
    add_mail(150, "渋谷民泊協会 事務局", "jimukyoku@minpaku-kyokai.example.jp",
             "年末年始の清掃業者の手配について（会員の皆さまへ）",
             "会員の皆さま\n\n年末年始の清掃業者の手配について、ご希望を12月10日までにお知らせください。",
             3, "お知らせ",
             cc=", ".join(f"kaiin{i:02d}@minpaku-kyokai.example.jp" for i in range(1, 13)),
             category="営業・宣伝")
    add_mail(240, "業界ニュース配信", "no-reply@newsletter.example.com",
             "【メルマガ】今週の業界トピックス",
             "今週の業界ニュースをお届けします。配信停止はこちら。",
             3, "自動配信・お知らせ", category="営業・宣伝")
    # 確認待ちの確認用: メール担当者の田中さんが下書きを作り、「確認をお願いする」を押したメール
    pending_id = add_mail(
        70, "伊藤", "ito.demo@icloud.com", "チェックインの時間について",
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
             "差出人: 加藤 真理 <mari.kato.demo@gmail.com>\n題名: 料金表がほしいです\n\n"
             "メッセージ本文:\n清掃の料金表を送っていただけますか。" + form_footer,
             2, "通常", status="replied", category="清掃のご依頼",
             reply=("加藤 様\n\nお問い合わせありがとうございます。\n料金表を添付いたします。",
                    5100, "管理者", ""))
    add_mail(1500, "高橋", "takahashi@takahashi-design.example.jp",
             "先日の打ち合わせのお礼",
             "先日はお時間をいただきありがとうございました。",
             2, "通常", status="replied", category="その他", to="kurosu@example.com")

    # 取引先の確認用: 同じ会社のドメインの2人 (さくら不動産。名前とメモを付けた取引先)、
    # 名前を付けていない会社 (青葉管理。HPのフォームの「会社名」が名前になる)、Gmail の個人
    add_mail(9000, "田中 一郎", "tanaka@sakura-fudosan.example.co.jp", "物件の清掃のご相談",
             "いつもお世話になっております。さくら不動産の田中です。\n"
             "管理している3物件の定期清掃をお願いできないか、ご相談させてください。", 2, "通常",
             status="replied", category="清掃のご依頼",
             reply=("田中 様\n\nお問い合わせありがとうございます。\n"
                    "3物件の定期清掃、承知いたしました。お見積もりをお送りします。",
                    8900, "管理者", ""))
    add_mail(6000, "鈴木 花子", "suzuki@sakura-fudosan.example.co.jp", "清掃の請求書について",
             "さくら不動産 経理の鈴木です。\n先月分の請求書を、PDFでお送りいただけますでしょうか。",
             2, "通常", status="replied", category="請求・お支払い",
             reply=("鈴木 様\n\nお世話になっております。\n請求書をお送りいたします。",
                    5950, "管理者", ""))
    add_mail(25, "田中 一郎", "tanaka@sakura-fudosan.example.co.jp", "来月の清掃日程",
             "来月の清掃日程を、第2・第4土曜でお願いできますか。", 2, "通常",
             category="清掃のご依頼")
    now_iso = now_utc.isoformat()
    sakura = conn.execute(
        "INSERT INTO clients (name, note, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("さくら不動産", "3物件の定期清掃（毎月第2・第4土曜）。請求書は経理の鈴木さんへPDFで。",
         now_iso, now_iso)).lastrowid
    conn.execute("INSERT INTO client_rules (client_id, pattern, created_at) VALUES (?, ?, ?)",
                 (sakura, "@sakura-fudosan.example.co.jp", now_iso))
    add_mail(3000, "WordPress", "wordpress@example.com", "【HPお問い合わせ】管理物件の清掃について",
             "会社名：青葉管理株式会社\nお名前：大野 健太\n"
             "メールアドレス：ohno@aoba-kanri.example.co.jp\n\n"
             "メッセージ本文:\n管理物件10室の清掃をお願いしたいです。" + form_footer,
             2, "通常", status="replied", category="清掃のご依頼",
             reply=("大野 様\n\nお問い合わせありがとうございます。\n担当よりご連絡いたします。",
                    2950, "管理者", ""))
    add_mail(15, "大野 健太", "ohno@aoba-kanri.example.co.jp", "Re: 管理物件の清掃について",
             "ご連絡ありがとうございます。来週の打ち合わせ、水曜の午後でいかがでしょうか。",
             2, "通常", category="清掃のご依頼")
    add_mail(4000, "青木 さくら", "demo.aoki@gmail.com", "宿泊の予約について",
             "来月の3連休に2泊で予約したいのですが、空いていますか。", 2, "通常",
             status="replied", category="予約・宿泊のお問い合わせ",
             reply=("青木 様\n\nお問い合わせありがとうございます。\nその日程は空いております。",
                    3990, "管理者", ""))
    add_mail(50, "青木 さくら", "demo.aoki@gmail.com", "チェックインの時間",
             "予約した青木です。チェックインは何時からできますか。", 2, "通常",
             category="予約・宿泊のお問い合わせ")
    # 長い本文の確認用: Outlook の個人 (取引先の印は「個人」)。前のメールの引用に、区切りの無い
    # 長い URL・「<mailto:…>」付きのアドレス・長い罫線が入る (スマホで横にはみ出さず、
    # 本文を最初から全部出す)。前のやり取り (返信済み) にも長い URL を入れる
    long_url = ("https://reserve.example.com/booking/confirm?reservation_id=RSV2026102800123456789"
                "&guest_token=AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIjKlMnOp&lang=ja")
    add_mail(3500, "木村 美咲", "kimura.demo@outlook.com", "宿泊の予約の確認",
             "予約の確認のページが開けません。\n" + long_url, 2, "通常",
             status="replied", category="予約・宿泊のお問い合わせ",
             reply=("木村 様\n\nお問い合わせありがとうございます。\n"
                    "こちらのページからご確認いただけます。\n" + long_url,
                    3480, "管理者", ""))
    add_mail(10, "木村 美咲", "kimura.demo@outlook.com", "Re: 宿泊の予約の確認",
             "ビックス 予約係 様\n\nお世話になっております。木村です。\n"
             "ご案内いただいたページから、予約の確認ができました。ありがとうございます。\n\n"
             "あわせて、次の点を教えていただけますでしょうか。\n"
             "1. チェックインの前に荷物を預けることはできますか。\n"
             "2. 近くに駐車場はありますか（車1台で伺う予定です）。\n"
             "3. 子ども用の布団を1組お借りすることはできますか。\n"
             "4. チェックアウトの時間を少し遅らせることはできますか。\n\n"
             "お忙しいところ恐れ入りますが、よろしくお願いいたします。\n\n"
             "木村 美咲\n"
             "____________________________________________________________________________________\n"
             "差出人: ビックス 予約係 <info@example.com<mailto:info@example.com>>\n"
             "送信日時: 2026年10月25日 10:12\n"
             "宛先: 木村 美咲 <kimura.demo@outlook.com<mailto:kimura.demo@outlook.com>>\n"
             "件名: Re: 宿泊の予約の確認\n\n"
             "木村 様\n\nお問い合わせありがとうございます。\n"
             "こちらのページからご確認いただけます。\n"
             "<" + long_url + ">\n\n"
             "--------------------------------------------------------------------------------------------\n"
             "ビックス 予約係 info@example.com<mailto:info@example.com> "
             "https://example.com/minpaku/contact/reservation-support/faq-and-guides\n"
             "--------------------------------------------------------------------------------------------",
             2, "通常", category="予約・宿泊のお問い合わせ")

    # 送信箱の確認用: Webメールから返信したメール (受信箱のメールは自動で「返信済み」になる)・
    # Gmail から送ったメール (Gmail は「送信済みだけ取り込む」アドレス。添付付き)・Webメールから新しく送ったメール
    gmail_id = conn.execute(
        "INSERT INTO mail_accounts (label, imap_host, imap_user, imap_pass, from_addr, enabled,"
        " sent_only, sent_import, sort_order, created_at, sent_state_folder, sent_fetch_ok_at)"
        " VALUES ('黒須Gmail', 'imap.gmail.com', 'kurosu.demo@gmail.com', 'demo-password',"
        " 'kurosu.demo@gmail.com', 0, 1, 1, 2, ?, '[Gmail]/&kAFP4W4IMH8w4TD8MOs-', ?)",
        (tz.utc_iso(now_utc), tz.utc_iso(now_utc))).lastrowid
    for acc_id in accounts.values():
        conn.execute("UPDATE mail_accounts SET sent_state_folder = 'INBOX.Sent',"
                     " sent_fetch_ok_at = ? WHERE id = ?", (tz.utc_iso(now_utc), acc_id))

    def add_sent(mins_ago, account_id, source, to, subject, body, in_reply_to="", cc="",
                 folder="INBOX.Sent", frm="info@example.com"):
        p = {"message_id": f"<demo-sent-{mins_ago}@example.com>", "to_addr": to, "cc_addr": cc,
             "subject": subject, "body": body, "in_reply_to": in_reply_to,
             "sent_at": (now_utc - timedelta(minutes=mins_ago)).isoformat()}
        name, addr = mail._header_addrs(to)[0]
        sent_id = conn.execute(
            "INSERT INTO sent_mails (account_id, message_id, folder, from_addr, to_addr, cc_addr,"
            " to_key, to_name, subject, body, sent_at, fetched_at, in_reply_to, references_hdr,"
            " source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, p["message_id"], folder, frm, to, cc, addr.lower(), name, subject, body,
             p["sent_at"], p["sent_at"], in_reply_to, in_reply_to, source)).lastrowid
        if in_reply_to:
            mail_id = conn.execute("SELECT id FROM mails WHERE message_id = ?",
                                   (in_reply_to,)).fetchone()[0]
            conn.execute("UPDATE sent_mails SET linked_mail_id = ?, link_kind = 'header'"
                         " WHERE id = ?", (mail_id, sent_id))
            mail._mark_replied(conn, mail_id, sent_id, p, source, "info", "header")
        return sent_id

    add_mail(300, "森 由紀", "mori.demo@yahoo.co.jp", "駐車場の有無について",
             "来週宿泊予定の森です。近くに駐車場はありますか。", 2, "通常",
             category="予約・宿泊のお問い合わせ")
    add_sent(280, accounts["info@example.com"], "webmail", "森 由紀 <mori.demo@yahoo.co.jp>",
             "Re: 駐車場の有無について",
             "森 様\n\nお問い合わせありがとうございます。\n"
             "建物の裏に1台分の駐車場がございます。ご到着の際にお使いください。\n\n"
             "ビックス 予約係\n\n> 来週宿泊予定の森です。近くに駐車場はありますか。",
             in_reply_to="<demo-300@example.com>")
    plan_id = add_sent(1200, gmail_id, "gmail", "山本 清掃チーム <yamamoto@clean-partner.example.jp>",
                       "来月の清掃の予定表",
                       "山本さん\n\nお疲れさまです。黒須です。\n来月の清掃の予定表と間取り図を送ります。\n"
                       "よろしくお願いします。", folder="[Gmail]/送信済みメール",
                       frm="kurosu.demo@gmail.com")
    if attachment_dir is not None:
        plan = _fake_floor_plan()
        mail.save_attachments(conn, attachment_dir, plan_id, [
            {"filename": "間取り図.png", "content_type": "image/png", "size": len(plan),
             "data": plan, "skip": ""}], now_utc.isoformat(), sent=True)
    add_sent(2000, accounts["kurosu@example.com"], "webmail",
             "tanaka@sakura-fudosan.example.co.jp", "定期清掃のお見積もり",
             "田中 様\n\nいつもお世話になっております。\n3物件の定期清掃のお見積もりをお送りします。",
             cc="suzuki@sakura-fudosan.example.co.jp", frm="kurosu@example.com")

    # スレッドの確認用 (返信どうしでつながったメールを1つにまとめる)。問い合わせの自動返信の控え
    # (自分たちの noreply@ からお客様宛て) と、そのお客様の「RE:」5通。こちらの返信 (返信済み) への
    # 返事・届いたメールへの返事でつながる。一番新しい1通は未対応、1通には添付ファイル
    ogawa = "小川 誠 <ogawa.makoto.demo@gmail.com>"
    subj = "【INBICS】お問い合わせありがとうございます"
    add_mail(7100, "INBICS", "noreply@example.com", subj,
             "小川 誠 様\n\nこの度は、お問い合わせいただき誠にありがとうございます。\n"
             "内容を確認のうえ、担当者より2営業日以内にご連絡いたします。\n\n"
             "※このメールは送信専用のアドレスから自動でお送りしています。", 2, "通常",
             status="archived", category="運営代行のご相談", rcpt=ogawa)
    add_mail(7000, "小川 誠", "ogawa.makoto.demo@gmail.com", "RE: " + subj,
             "ご連絡ありがとうございます。\n世田谷区の一戸建てで民泊を始めたいと考えています。\n"
             "運営代行のプランを教えてください。", 2, "通常",
             status="replied", category="運営代行のご相談", in_reply_to="<demo-7100@example.com>",
             reply=("小川 様\n\nお問い合わせありがとうございます。\n"
                    "運営代行は「清掃のみ」と「まるごと」の2つのプランがございます。\n"
                    "物件の間取りを教えていただけますでしょうか。", 6950, "管理者", ""))
    plan_mail = add_mail(5500, "小川 誠", "ogawa.makoto.demo@gmail.com", "RE: " + subj,
                         "間取り図をお送りします。4LDK・庭付きです。", 2, "通常",
                         status="archived", category="運営代行のご相談",
                         in_reply_to="<demo-reply-7000@example.com>")
    if attachment_dir is not None:
        floor = _fake_floor_plan()
        mail.save_attachments(conn, attachment_dir, plan_mail, [
            {"filename": "小川様_間取り図.png", "content_type": "image/png",
             "size": len(floor), "data": floor, "skip": ""}], now_utc.isoformat())
    add_mail(3300, "小川 誠", "ogawa.makoto.demo@gmail.com", "RE: " + subj,
             "先ほどの間取り図、見ていただけましたでしょうか。", 2, "通常",
             status="replied", category="運営代行のご相談", in_reply_to="<demo-5500@example.com>",
             reply=("小川 様\n\n間取り図をありがとうございます。\n"
                    "まるごとプランでお見積もりをお送りします。", 3280, "管理者", ""))
    add_mail(1100, "小川 誠", "ogawa.makoto.demo@gmail.com", "RE: " + subj,
             "お見積もりを拝見しました。開始日を相談させてください。", 2, "通常",
             status="archived", category="運営代行のご相談",
             in_reply_to="<demo-reply-3300@example.com>")
    add_mail(8, "小川 誠", "ogawa.makoto.demo@gmail.com", "RE: " + subj,
             "来月1日から始めたいです。契約書をお送りいただけますか。", 1, "「契約」を含む",
             category="運営代行のご相談", in_reply_to="<demo-reply-3300@example.com>")
    # HPのフォームから、別々のお客様が同じ件名で送ってきたメール (差出人も件名も同じだが、
    # 相手が違うのでまとめない)。片方のお客様が、もう片方のメールを指す返事を送ってきても同じ
    add_mail(400, "WordPress", "wordpress@example.com", "【HPお問い合わせ】お見積もりのご依頼",
             "お名前：井上 直樹\nメールアドレス：inoue.naoki.demo@example.net\n\n"
             "メッセージ本文:\n2部屋の清掃のお見積もりをお願いします。" + form_footer,
             2, "通常", category="清掃のご依頼")
    add_mail(380, "WordPress", "wordpress@example.com", "【HPお問い合わせ】お見積もりのご依頼",
             "お名前：松本 由美\nメールアドレス：matsumoto.yumi.demo@example.org\n\n"
             "メッセージ本文:\n民泊1部屋の清掃の料金を知りたいです。" + form_footer,
             2, "通常", category="清掃のご依頼")
    add_mail(360, "松本 由美", "matsumoto.yumi.demo@example.org", "Re: 【HPお問い合わせ】お見積もりのご依頼",
             "先ほどの問い合わせの補足です。土日の清掃もできますか。", 2, "通常",
             category="清掃のご依頼", in_reply_to="<demo-400@example.com>")
    # ★フラグの確認用: 重要度が低でも ★ を付けたメールは一覧に出す
    conn.execute("UPDATE mails SET flagged_at = ?, flagged_by = '管理者' WHERE message_id = ?",
                 ((now_utc - timedelta(minutes=100)).isoformat(), "<demo-150@example.com>"))

    # よく使うアドレス (アドレス帳)。返信画面の宛先・CC・BCC に打つと候補の一番上に出る
    for name, addr, note in (
            ("山本 清掃チーム", "yamamoto@clean-partner.example.jp", "清掃の外注先（平日のみ）"),
            ("鈴木 花子", "suzuki@sakura-fudosan.example.co.jp", "請求書の送り先（PDF）"),
            ("小林 税理士事務所", "kobayashi@tax-office.example.jp", "月末の書類")):
        conn.execute("INSERT INTO address_book (name, email, note, created_at, updated_at)"
                     " VALUES (?, ?, ?, ?, ?)", (name, addr, note, now_iso, now_iso))

    conn.commit()
    # 相手 (HPのフォームは本文のお客様) を決めてから、スレッドにまとめる (起動したときと同じ決め方)
    mail.fill_missing_contacts(conn)
    mail_threads.rebuild(conn, mail_threads.Ctx(conn))
    return True
