"""Migration runner: applies migrations/*.sql in order, once, under an advisory lock."""
import os
import sys

from psycopg2 import sql

from .config import _setting
from .db import connect

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "migrations")
_LOCK_ID = 7_340_001


class RoleProvisionError(RuntimeError):
    pass


def provision_roles(cur, runtime_role=None, backup_role=None):
    """Least-privilege grants, re-applied on every run so tables added by later migrations are covered.

    The runtime role (API, worker, admin CLI) gets DML only: no DDL, no role management, no TRUNCATE/DELETE,
    and audit_events/schema_migrations stay append-only/read-only for it. The backup role is read-only.
    Roles themselves are created by the database administrator, never by this code.
    """
    if runtime_role:
        role = sql.Identifier(runtime_role)
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO {}").format(role))
        cur.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(role))
        cur.execute(sql.SQL("REVOKE UPDATE ON audit_events, schema_migrations FROM {}").format(role))
        cur.execute(sql.SQL("REVOKE INSERT ON schema_migrations FROM {}").format(role))
    if backup_role:
        role = sql.Identifier(backup_role)  # what pg_dump needs, and nothing that writes
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(role))
        cur.execute(sql.SQL("GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(role))
    # Fail closed: PostgreSQL < 15 grants CREATE on schema public to PUBLIC, and only the schema owner (the
    # database administrator) can revoke it. Refuse to report success while a service role can still run DDL.
    for name in (runtime_role, backup_role):
        if name:
            cur.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE') AS can_create", (name,))
            if cur.fetchone()["can_create"]:
                raise RoleProvisionError(
                    "role %s can CREATE in schema public; the database administrator must run "
                    "'REVOKE CREATE ON SCHEMA public FROM PUBLIC' (default on PostgreSQL < 15)" % name)


def migrate(dsn, directory=MIGRATIONS_DIR, runtime_role=None, backup_role=None):
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
            provision_roles(cur, runtime_role, backup_role)
        conn.commit()
    finally:
        conn.close()
    return applied


def main():
    env = os.environ
    url = _setting(env, "MEMORY_MIGRATION_DATABASE_URL") or _setting(env, "MEMORY_DATABASE_URL", required=True)
    applied = migrate(url,
                      runtime_role=env.get("MEMORY_RUNTIME_ROLE") or None,
                      backup_role=env.get("MEMORY_BACKUP_ROLE") or None)
    print("applied: %s" % (", ".join(applied) if applied else "(none)"), file=sys.stderr)


if __name__ == "__main__":
    main()
