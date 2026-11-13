import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memory_server import admin, hooks  # noqa: E402
from memory_server.config import Config  # noqa: E402
from memory_server.db import connect  # noqa: E402
from memory_server.migrate import migrate  # noqa: E402
from memory_server.service import MemoryService  # noqa: E402
from memory_server.worker import Worker  # noqa: E402

HMAC_KEY = "test-only-hmac-key-0123456789abcdef-synthetic"


@pytest.fixture(scope="session")
def database_url():
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL is not set (run scripts/verify-ubuntu2004.sh)")
    name = "memory_test_" + uuid.uuid4().hex[:8]
    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    conn.cursor().execute('CREATE DATABASE "%s" ENCODING \'UTF8\' TEMPLATE template0' % name)
    conn.close()
    base = admin_url.rsplit("/", 1)[0]
    url = "%s/%s" % (base, name)
    migrate(url)
    yield url
    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,))
    cur.execute('DROP DATABASE "%s"' % name)
    conn.close()


@pytest.fixture
def config(database_url):
    return Config(database_url=database_url, hmac_key=HMAC_KEY.encode())


@pytest.fixture(autouse=True)
def _clear_hooks():
    yield
    hooks.clear()


class World(object):
    """Synthetic fixture: two namespaces (personal/work), devices A/B, a read-only device."""

    def __init__(self, config):
        self.config = config
        self.dsn = config.database_url
        self.svc = MemoryService(config)
        # The queue is global and the session DB is shared: park jobs left over from earlier tests so this
        # test's worker only ever sees its own jobs.
        self.query("UPDATE jobs SET state = 'cancelled', error_code = 'TEST_LEFTOVER', lease_owner = NULL, "
                   "lease_until = NULL, completed_at = now() WHERE state IN ('queued', 'running')")
        tag = uuid.uuid4().hex[:6]
        self.personal = admin.create_namespace(self.dsn, "personal-" + tag)
        self.work = admin.create_namespace(self.dsn, "work-" + tag)
        self.a = admin.create_principal(self.dsn, "device-a-" + tag)
        self.b = admin.create_principal(self.dsn, "device-b-" + tag)
        self.reader = admin.create_principal(self.dsn, "reader-" + tag)
        admin.grant(self.dsn, self.a["principal_id"], self.personal["namespace_id"], write=True)
        admin.grant(self.dsn, self.a["principal_id"], self.work["namespace_id"], write=True)
        admin.grant(self.dsn, self.b["principal_id"], self.personal["namespace_id"], write=True)
        admin.grant(self.dsn, self.reader["principal_id"], self.personal["namespace_id"], read=True)

    @property
    def ns(self):
        return self.personal["namespace_id"]

    def body(self, text="배포 DB는 PostgreSQL을 사용한다.", namespace_id=None, **extra):
        doc = {"namespace_id": namespace_id or self.ns,
               "content": {"media_type": "text/plain", "text": text}}
        doc.update(extra)
        return doc

    def submit(self, token, key, body=None):
        return self.svc.submit(token, body or self.body(), key)

    def explicit(self, token, key, text, namespace_id=None, title=None, hints=None, actor="agent"):
        sub = {"intent": "explicit_memory", "actor_reported": actor}
        if title:
            sub["title"] = title
        if hints:
            sub["retrieval_hints"] = hints
        return self.svc.submit(token, self.body(text, namespace_id, submission=sub), key)[0]

    def worker(self, name="w"):
        return Worker(self.config, name)

    def drain(self):
        return self.worker().run_until_idle()

    def search(self, token, query, namespace_id=None, **extra):
        body = {"namespace_id": namespace_id or self.ns, "query": query}
        body.update(extra)
        return self.svc.search(token, body)

    def job(self, job_id):
        return self.query("SELECT * FROM jobs WHERE id = %s", (job_id,))[0]

    def make_due(self, job_id):
        self.query("UPDATE jobs SET not_before = now() WHERE id = %s", (job_id,))

    def expire_lease(self, job_id):
        self.query("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (job_id,))

    def artifacts(self, namespace_id=None, state="valid"):
        return self.query("SELECT * FROM artifacts WHERE namespace_id = %s AND state = %s ORDER BY local_key",
                          (namespace_id or self.ns, state))

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

    def counts(self, namespace_id=None):
        ns = namespace_id or self.ns
        return {
            "experiences": self.query("SELECT count(*) AS n FROM experiences WHERE namespace_id=%s", (ns,))[0]["n"],
            "jobs": self.query("SELECT count(*) AS n FROM jobs WHERE namespace_id=%s", (ns,))[0]["n"],
            "receipts": self.query("SELECT count(*) AS n FROM ingest_receipts WHERE namespace_id=%s", (ns,))[0]["n"],
        }


@pytest.fixture
def world(config):
    return World(config)
