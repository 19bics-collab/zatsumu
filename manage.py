#!/usr/bin/env python3
"""ユーザー管理 CLI.

    python manage.py add-user 田中        # 一般ユーザー作成(トークン表示)
    python manage.py add-user 管理者 --admin
    python manage.py list-users
"""
import argparse

from server import db


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add-user")
    add.add_argument("name")
    add.add_argument("--admin", action="store_true")
    sub.add_parser("list-users")
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


if __name__ == "__main__":
    main()
