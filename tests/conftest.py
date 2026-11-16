import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memory_server import admin, generations, hooks  # noqa: E402
from memory_server.config import Config  # noqa: E402
from memory_server.db import connect  # noqa: E402
from memory_server.migrate import migrate  # noqa: E402
from memory_server.service import MemoryService  # noqa: E402
from memory_server.worker import Worker  # noqa: E402

HMAC_KEY = "test-only-hmac-key-0123456789abcdef-synthetic"


@pytest.fixture
def make_db():
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL is not set (run scripts/verify.sh)")
    created = []

    def make(migrated=False):
        name = "memory_test_" + uuid.uuid4().hex[:8]
        conn = psycopg2.connect(admin_url)
        try:
            conn.autocommit = True
            conn.cursor().execute('CREATE DATABASE "%s" ENCODING \'UTF8\' TEMPLATE template0' % name)
        finally:
            conn.close()
        created.append(name)
        url = admin_url.rsplit("/", 1)[0] + "/" + name
        if migrated:
            migrate(url)
        return url

    yield make
    conn = psycopg2.connect(admin_url)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        for name in created:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,))
            cur.execute('DROP DATABASE "%s"' % name)
    finally:
        conn.close()


@pytest.fixture
def database_url(make_db):
    return make_db(migrated=True)


@pytest.fixture
def config(database_url):
    return Config(database_url=database_url, hmac_key=HMAC_KEY.encode())


@pytest.fixture(autouse=True)
def _clear_hooks():
    yield
    hooks.clear()


class World(object):
    """An isolated memory database, two writers, a reader and an unprivileged device."""

    def __init__(self, config):
        self.config = config
        self.dsn = config.database_url
        self.svc = MemoryService(config)
        self.a = admin.create_principal(self.dsn, "device-a")
        self.b = admin.create_principal(self.dsn, "device-b")
        self.reader = admin.create_principal(self.dsn, "reader")
        self.outsider = admin.create_principal(self.dsn, "unprivileged")
        admin.set_permissions(self.dsn, self.a["principal_id"], write=True)
        admin.set_permissions(self.dsn, self.b["principal_id"], write=True)
        admin.set_permissions(self.dsn, self.reader["principal_id"], read=True)

    @property
    def active_generation_id(self):
        return str(self.query("SELECT active_generation_id FROM system_control")[0]["active_generation_id"])

    def switch_engine(self, version="1", index_profile=None):
        active = self.active_generation_id
        pid = self.b["principal_id"]
        admin.set_permissions(self.dsn, pid, manage=True)
        candidate = str(uuid.uuid4())
        generations.start_generation(self.config, pid, candidate, active, version, index_profile)
        self.drain()
        comparison = generations.compare_generation(self.config, pid, candidate, ["PostgreSQL"])
        generations.activate_generation(self.config, pid, candidate, active, comparison["receipt_id"])
        generations.resume_ingest(self.config, pid, candidate)

    def pause_ingest(self):
        active = self.active_generation_id
        pid = self.b["principal_id"]
        admin.set_permissions(self.dsn, pid, manage=True)
        generations.start_generation(self.config, pid, str(uuid.uuid4()), active, "2")

    def body(self, text="배포 DB는 PostgreSQL을 사용한다.", **extra):
        doc = {"content": {"media_type": "text/plain", "text": text}}
        doc.update(extra)
        return doc

    def submit(self, token, key, body=None):
        return self.svc.submit(token, body or self.body(), key)

    def explicit(self, token, key, text, title=None, hints=None, actor="agent"):
        sub = {"intent": "explicit_memory", "actor_reported": actor}
        if title:
            sub["title"] = title
        if hints:
            sub["retrieval_hints"] = hints
        return self.svc.submit(token, self.body(text, submission=sub), key)[0]

    def worker(self, name="w"):
        return Worker(self.config, name)

    def drain(self):
        return self.worker().run_until_idle()

    def search(self, token, query, **extra):
        body = {"query": query}
        body.update(extra)
        return self.svc.search(token, body)

    def job(self, job_id):
        return self.query("SELECT * FROM jobs WHERE id = %s", (job_id,))[0]

    def make_due(self, job_id):
        self.query("UPDATE jobs SET not_before = now() WHERE id = %s", (job_id,))

    def expire_lease(self, job_id):
        self.query("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (job_id,))

    def artifacts(self, state="valid"):
        return self.query("SELECT * FROM artifacts WHERE state = %s ORDER BY local_key", (state,))

    def query(self, sql, params=()):
        conn = connect(self.dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() if cur.description else []
            conn.commit()
            return rows
        finally:
            conn.close()

    def counts(self):
        return {
            "experiences": self.query("SELECT count(*) AS n FROM experiences")[0]["n"],
            "jobs": self.query("SELECT count(*) AS n FROM jobs")[0]["n"],
            "receipts": self.query("SELECT count(*) AS n FROM ingest_receipts")[0]["n"],
        }


@pytest.fixture
def world(config):
    return World(config)
