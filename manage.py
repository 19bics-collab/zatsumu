#!/usr/bin/env python3
"""ユーザー管理 CLI.

    python manage.py add-user 田中        # 一般ユーザー作成(トークン表示)
    python manage.py add-user 管理者 --admin
    python manage.py list-users
    python manage.py reset-token 管理者    # トークンを再発行(紛失時の復旧用)
    python manage.py make-admin 田中        # 既存ユーザーを管理者に昇格
    python manage.py purge --days 30           # 30日より古いスクショを削除
"""
import argparse
import secrets

from server import db, retention


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
                print(f"{row['name']} のトークンを再発行しました:")
                print(f"トークン: {token}")
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


if __name__ == "__main__":
    main()
