"""Migration runner: applies migrations/*.sql in order, once, under an advisory lock."""
import os
import sys

from .config import Config
from .db import connect

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "migrations")
_LOCK_ID = 7_340_001


def migrate(dsn, directory=MIGRATIONS_DIR):
    files = sorted(f for f in os.listdir(directory) if f.endswith(".sql"))
    conn = connect(dsn)
    applied = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_ID,))
            cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                        "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            cur.execute("SELECT version FROM schema_migrations")
            done = {r["version"] for r in cur.fetchall()}
            for name in files:
                if name in done:
                    continue
                with open(os.path.join(directory, name), encoding="utf-8") as fh:
                    cur.execute(fh.read())
                cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (name,))
                applied.append(name)
        conn.commit()
    finally:
        conn.close()
    return applied


def main():
    applied = migrate(Config.from_env().database_url)
    print("applied: %s" % (", ".join(applied) if applied else "(none)"), file=sys.stderr)


if __name__ == "__main__":
    main()
