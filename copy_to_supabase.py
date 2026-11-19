"""One-time copy of your local DeBet data (debet.db) into the online Supabase database.

Usage (from the debet folder, after adding DATABASE_URL to .env):
    python copy_to_supabase.py

It creates DeBet's tables in Supabase, copies users, settings, tips and bets (keeping their ids),
and refuses to run if the online database already has tips or users - so it can't overwrite anything.
"""

import os
import sqlite3
import sys

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

import database as db  # noqa: E402

TABLES = ("users", "settings", "tips", "user_bets")


def main():
    if not db.using_postgres():
        sys.exit("DATABASE_URL is missing from .env (it must start with postgresql://). Nothing copied.")
    local_path = db.DB_PATH
    if not os.path.exists(local_path):
        sys.exit(f"No local database at {local_path}. Nothing copied.")

    print("Creating tables in Supabase...")
    db.init_db(seed_admin=False)
    with db.get_connection() as conn:
        existing = {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in ("users", "tips")}
    if any(existing.values()):
        sys.exit(f"Supabase already has data ({existing}). Stopped so nothing is overwritten.")

    src = sqlite3.connect(local_path)
    src.row_factory = sqlite3.Row
    with db.get_connection() as conn:
        for table in TABLES:
            target_cols = db.table_columns(conn, table)
            rows = [dict(r) for r in src.execute(f"SELECT * FROM {table}")]
            for r in rows:
                cols = [c for c in r if c in target_cols]
                sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
                if table == "settings":
                    sql += " ON CONFLICT (key) DO UPDATE SET value = excluded.value"
                conn.execute(sql, [r[c] for c in cols])
            print(f"  {table}: {len(rows)} row(s)")
        for table in ("users", "tips", "user_bets"):  # new rows must continue after the copied ids
            conn.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                         f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")
    print("Done. Your data is now in Supabase.")


if __name__ == "__main__":
    main()
