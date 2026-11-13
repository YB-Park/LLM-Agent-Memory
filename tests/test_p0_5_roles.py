"""P0-5: database privilege separation. The runtime role can run the whole service but cannot change the schema,
manage roles, rewrite the audit trail or touch migration bookkeeping."""
import os
import uuid

import psycopg2
import pytest

from memory_server import admin, operations
from memory_server.config import Config
from memory_server.migrate import migrate
from memory_server.service import MemoryService
from memory_server.worker import Worker

from conftest import HMAC_KEY


@pytest.fixture
def role_db():
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL is not set")
    tag = uuid.uuid4().hex[:8]
    owner, app, backup, db = ("own_" + tag, "app_" + tag, "bak_" + tag, "roles_" + tag)
    pw = "pw-" + tag
    su = psycopg2.connect(admin_url)
    su.autocommit = True
    cur = su.cursor()
    for role in (owner, app, backup):
        cur.execute('CREATE ROLE "%s" LOGIN PASSWORD \'%s\' NOSUPERUSER NOCREATEDB NOCREATEROLE' % (role, pw))
    cur.execute('CREATE DATABASE "%s" OWNER "%s" TEMPLATE template0 ENCODING \'UTF8\'' % (db, owner))
    base = admin_url.split("@", 1)[1]
    host = base.rsplit("/", 1)[0]

    def dsn(role):
        return "postgresql://%s:%s@%s/%s" % (role, pw, host, db)
    migrate(dsn(owner), runtime_role=app, backup_role=backup)
    yield {"owner": dsn(owner), "app": dsn(app), "backup": dsn(backup), "app_role": app}
    cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (db,))
    cur.execute('DROP DATABASE "%s"' % db)
    for role in (owner, app, backup):
        cur.execute('DROP ROLE "%s"' % role)
    su.close()


def _denied(dsn, statement):
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            conn.cursor().execute(statement)
    finally:
        conn.close()


def test_runtime_role_cannot_change_schema_roles_or_audit_trail(role_db):
    app = role_db["app"]
    for stmt in ("CREATE TABLE evil (x int)", "DROP TABLE experiences", "ALTER TABLE experiences ADD COLUMN x int",
                 "TRUNCATE experiences", "DELETE FROM experiences", "CREATE ROLE evil LOGIN",
                 "ALTER ROLE %s SUPERUSER" % role_db["app_role"], "UPDATE audit_events SET event_type = 'x'",
                 "DELETE FROM audit_events", "INSERT INTO schema_migrations (version) VALUES ('x')",
                 "UPDATE schema_migrations SET version = 'x'", "CREATE EXTENSION IF NOT EXISTS pg_trgm",
                 "COPY (SELECT 1) TO PROGRAM 'true'", "SELECT * FROM pg_authid"):
        _denied(app, stmt)


def test_backup_role_is_read_only(role_db):
    conn = psycopg2.connect(role_db["backup"])
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM experiences")
    for stmt in ("INSERT INTO audit_events (event_type) VALUES ('x')", "UPDATE system_control SET policy_revision = 9",
                 "CREATE TABLE t (x int)"):
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute(stmt)
    conn.close()


def test_full_service_flow_runs_under_the_runtime_role(role_db):
    dsn = role_db["app"]
    config = Config(database_url=dsn, hmac_key=HMAC_KEY.encode())
    ns = admin.create_namespace(dsn, "roles-ns")
    dev = admin.create_principal(dsn, "roles-dev")
    other = admin.create_principal(dsn, "roles-other")
    admin.grant(dsn, dev["principal_id"], ns["namespace_id"], write=True)
    admin.grant(dsn, other["principal_id"], ns["namespace_id"], write=True)
    svc = MemoryService(config)
    receipt, _ = svc.submit(dev["token"], {"namespace_id": ns["namespace_id"], "content": {
        "media_type": "text/plain", "text": "runtime role marker rolemarker"},
        "submission": {"intent": "explicit_memory", "actor_reported": "user"}}, "k1")
    Worker(config, "w").run_until_idle()
    found = svc.search(dev["token"], {"namespace_id": ns["namespace_id"], "query": "rolemarker"})
    assert len(found["results"]) == 1
    # delete, revoke and generation work all run without schema or superuser rights
    operations.delete_experience(config, receipt["experience_id"])
    assert svc.search(dev["token"], {"namespace_id": ns["namespace_id"], "query": "rolemarker"})["results"] == []
    operations.revoke_principal(config, other["principal_id"])
    svc.submit(dev["token"], {"namespace_id": ns["namespace_id"], "content": {
        "media_type": "text/plain", "text": "second memory rolemarker2"}}, "k2")
    Worker(config, "w").run_until_idle()
    admin.grant(dsn, dev["principal_id"], ns["namespace_id"], manage=True)
    gens = admin.generations
    g2 = gens.start_generation(config, dev["principal_id"], ns["namespace_id"], str(uuid.uuid4()),
                               ns["generation_id"], "2", "reference-keywords/v1")["generation_id"]
    Worker(config, "w").run_until_idle()
    report = gens.compare_generation(config, dev["principal_id"], g2, ["rolemarker2"])
    assert report["status"] == "ready"
    out = gens.activate_generation(config, dev["principal_id"], g2, ns["generation_id"], report["receipt_id"])
    assert out["active_generation_id"] == g2
