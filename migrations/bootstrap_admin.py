#!/usr/bin/env python3
"""Create the initial admin user.

Migrations create the empty `users` table; this script seeds the first row.
Idempotent — if a user named "admin" already exists, the script prints the
existing api_key and exits without changes.

Usage:
    python3 migrations/bootstrap_admin.py [--user admin]

Run AFTER `python3 migrations/apply.py`.
"""
import argparse
import os
import sys
import uuid

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="admin", help="user_name to create (default: admin)")
    parser.add_argument("--access-level", type=int, default=0, choices=[0, 1, 2],
                        help="0=Admin, 1=Read-Only, 2=Read+Write (default: 0)")
    parser.add_argument("--skip-if-any-admin-exists", action="store_true",
                        help="Exit cleanly if any user with access_level=0 already exists. "
                             "Use this when leaving the script in a deploy pipeline so it "
                             "doesn't create duplicate admin rows on every run.")
    args = parser.parse_args()

    conn = psycopg2.connect(
        host=os.getenv("DATABASE_IP"),
        dbname=os.getenv("DATABASE_USERNAME"),
        user=os.getenv("DATABASE_USERNAME"),
        password=os.getenv("DATABASE_PASSWORD"),
        port=os.getenv("DATABASE_PORT"),
    )
    conn.autocommit = True
    cur = conn.cursor()

    if args.skip_if_any_admin_exists:
        cur.execute("SELECT count(*) FROM users WHERE access_level = 0;")
        n = cur.fetchone()[0]
        if n > 0:
            print(f"At least one admin (access_level=0) already exists ({n} total). Nothing to do.")
            return

    cur.execute("SELECT api_key, access_level FROM users WHERE user_name = %s;", (args.user,))
    row = cur.fetchone()
    if row:
        api_key, access_level = row
        print(f"User '{args.user}' already exists (access_level={access_level}).")
        print(f"API key: {api_key}")
        return

    api_key = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO users (user_name, api_key, access_level) VALUES (%s, %s, %s);",
        (args.user, api_key, args.access_level),
    )

    print(f"Created user '{args.user}' with access_level={args.access_level}.")
    print()
    print(f"  API key: {api_key}")
    print()
    print("Save this key -- the api_key column stores it in plaintext but you need")
    print("DB access to read it back. Distribute via a secure channel; rotate periodically.")


if __name__ == "__main__":
    try:
        main()
    except psycopg2.Error as err:
        print(f"DB error: {err}", file=sys.stderr)
        sys.exit(1)
