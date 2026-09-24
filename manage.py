#!/usr/bin/env python3
"""ユーザー管理 CLI.

    python manage.py add-user 田中        # 一般ユーザー作成(トークン表示)
    python manage.py add-user 管理者 --admin
    python manage.py list-users
    python manage.py reset-token 管理者    # トークンを再発行(紛失時の復旧用)
    python manage.py make-admin 田中        # 既存ユーザーを管理者に昇格
    python manage.py purge --days 30           # 30日より古いスクショを削除

新しい端末のメール確認 (.env の ZATSUMU_LOGIN_VERIFY_EMAIL を設定したとき) 用:
    python manage.py devices                   # 確認済みの端末の一覧
    python manage.py device-revoke 3           # 端末 3 番を取り消す (all で全部)
    python manage.py issue-device 管理者       # メールが届かないときの復旧用に
                                               # 端末トークンを発行して表示する
"""
import argparse
import secrets
from datetime import datetime, timezone

from server import db, devices, retention


def _audit_cli(conn, user_id: int, action: str, detail: str) -> None:
    """サーバ上の操作も監査ログに残す (操作者は対象の管理者本人として記録)."""
    conn.execute(
        "INSERT INTO audit_log (admin_id, action, target_user_id, detail, at)"
        " VALUES (?, ?, ?, ?, ?)",
        (user_id, action, user_id, f"manage.py {detail}".strip(),
         datetime.now(timezone.utc).isoformat()),
    )


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add-user")
    add.add_argument("name")
    add.add_argument("--admin", action="store_true")
    sub.add_parser("list-users")
    rt = sub.add_parser("reset-token")
    rt.add_argument("name")
    ma = sub.add_parser("make-admin")
    ma.add_argument("name")
    purge = sub.add_parser("purge")
    purge.add_argument("--days", type=int, default=30)
    sub.add_parser("devices")
    dr = sub.add_parser("device-revoke")
    dr.add_argument("target", help="端末の番号 (devices で表示) または all")
    idv = sub.add_parser("issue-device")
    idv.add_argument("name")
    args = p.parse_args()

    with db.get_db() as conn:
        if args.cmd == "add-user":
            user = db.create_user(conn, args.name, is_admin=args.admin)
            print(f"作成しました: {user['name']} (admin={user['is_admin']})")
            print(f"トークン: {user['token']}")
        elif args.cmd == "list-users":
            for r in conn.execute("SELECT id, name, is_admin FROM users"):
                role = "admin" if r["is_admin"] else "user"
                print(f"{r['id']}\t{r['name']}\t{role}")
        elif args.cmd in ("reset-token", "make-admin"):
            row = conn.execute(
                "SELECT id, name FROM users WHERE name = ?", (args.name,)
            ).fetchone()
            if not row:
                print(f"ユーザーが見つかりません: {args.name}")
                raise SystemExit(1)
            if args.cmd == "reset-token":
                token = secrets.token_urlsafe(24)
                conn.execute(
                    "UPDATE users SET token = ? WHERE id = ?", (token, row["id"])
                )
                # 古いトークンで確認した端末もすべて使えなくする
                n = devices.revoke_all(conn, row["id"])
                if n:
                    _audit_cli(conn, row["id"], "device_revoke_all", "reset-token")
                print(f"{row['name']} のトークンを再発行しました:")
                print(f"トークン: {token}")
                if n:
                    print(f"確認済みの端末 {n} 台を取り消しました。")
            else:
                conn.execute(
                    "UPDATE users SET is_admin = 1, active = 1 WHERE id = ?",
                    (row["id"],),
                )
                print(f"{row['name']} を管理者にしました。")
        elif args.cmd == "purge":
            screenshot_dir = db.DB_PATH.parent / "screenshots"
            n = retention.purge_old_screenshots(conn, screenshot_dir, args.days)
            print(f"{n} 件のスクリーンショットを削除しました (>{args.days}日)")
        elif args.cmd == "devices":
            rows = devices.list_devices(conn)
            if not rows:
                print("確認済みの端末はありません。")
            for r in rows:
                print(f"{r['id']}\t{r['user_name']}\t最終利用 {r['last_used_at'][:16]}"
                      f"\t{r['ip']}\t{r['label']}")
        elif args.cmd == "device-revoke":
            if args.target == "all":
                owners = [r["user_id"] for r in devices.list_devices(conn)]
                n = devices.revoke_all(conn)
                for uid in sorted(set(owners)):
                    _audit_cli(conn, uid, "device_revoke_all", "device-revoke all")
                print(f"端末 {n} 台を取り消しました。")
            else:
                if not args.target.isdigit():
                    print("端末の番号 (devices で表示) か all を指定してください")
                    raise SystemExit(1)
                row = devices.revoke_device(conn, int(args.target))
                if not row:
                    print(f"有効な端末が見つかりません: {args.target}")
                    raise SystemExit(1)
                _audit_cli(conn, row["user_id"], "device_revoke",
                           f"device-revoke id={row['id']}")
                print(f"端末 {row['id']} を取り消しました。")
        elif args.cmd == "issue-device":
            row = conn.execute(
                "SELECT id, name, is_admin FROM users WHERE name = ?", (args.name,)
            ).fetchone()
            if not row:
                print(f"ユーザーが見つかりません: {args.name}")
                raise SystemExit(1)
            if not row["is_admin"]:
                print(f"{row['name']} は管理者ではありません（端末の確認は管理者だけ）")
                raise SystemExit(1)
            token = devices.issue_device(conn, row["id"], "manage.py issue-device")
            _audit_cli(conn, row["id"], "device_issue", "issue-device")
            print(f"{row['name']} の端末トークンを発行しました（メールが届かないときの復旧用）。")
            print("画面のアドレスの後ろに次を付けて開くと、この端末が確認済みになります:")
            print(f"#device={token}")
            print("  例: https://<メール画面のドメイン>/mail#device=...")
            print("この文字列は合言葉と同じく秘密です。使い終わったら控えを消してください。")


if __name__ == "__main__":
    main()
