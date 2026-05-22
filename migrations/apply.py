#!/usr/bin/env python3
"""Apply versioned SQL migrations in this directory.

Tracks applied versions in a schema_migrations table so each file runs at most once.
Safe to re-run — migrations are expected to be idempotent (CREATE TABLE IF NOT EXISTS, etc.).

Usage:  python3 migrations/apply.py
"""
import glob
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def main():
    conn = psycopg2.connect(
        host=os.getenv("DATABASE_IP"),
        dbname=os.getenv("DATABASE_USERNAME"),
        user=os.getenv("DATABASE_USERNAME"),
        password=os.getenv("DATABASE_PASSWORD"),
        port=os.getenv("DATABASE_PORT"),
    )
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version TEXT PRIMARY KEY,"
        "  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
        ");"
    )
    cur.execute("SELECT version FROM schema_migrations;")
    applied = {row[0] for row in cur.fetchall()}

    here = os.path.dirname(os.path.abspath(__file__))
    files = sorted(glob.glob(os.path.join(here, "*.sql")))

    applied_now = 0
    for path in files:
        version = os.path.basename(path)
        if version in applied:
            continue
        print(f"Applying {version}...", flush=True)
        with open(path, "r", encoding="utf-8") as f:
            sql_text = f.read()
        try:
            cur.execute(sql_text)
        except Exception as err:
            print(f"  FAILED: {err}", file=sys.stderr)
            sys.exit(1)
        cur.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
        )
        print("  OK", flush=True)
        applied_now += 1

    print(f"Done. {applied_now} new migration(s) applied; {len(files) - applied_now} already up-to-date.")


if __name__ == "__main__":
    main()
