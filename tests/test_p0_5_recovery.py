"""P0-5: encrypted backup, quarantined restore, ledger replay. Real PostgreSQL, real pg_dump/pg_restore/gpg,
synthetic data only. The central claim under test: restoring an OLD backup must not bring back deleted
sources, revoked credentials or old job attempts, and must stay closed unless the ledger proves otherwise."""
import json
import os
import subprocess
import sys
import uuid
from dataclasses import replace

import psycopg2
import pytest

from memory_server import admin, ledger, operations, recovery
from memory_server.config import Config
from memory_server.db import connect
from memory_server.errors import ServiceError
from memory_server.migrate import migrate
from memory_server.service import MemoryService
from memory_server.worker import Worker

from conftest import HMAC_KEY

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not set")


@pytest.fixture
def make_db():
    admin_url = os.environ["TEST_DATABASE_URL"]
    created = []

    def make(migrated=False):
        name = "rec_" + uuid.uuid4().hex[:8]
        conn = psycopg2.connect(admin_url)
        conn.autocommit = True
        conn.cursor().execute('CREATE DATABASE "%s" ENCODING \'UTF8\' TEMPLATE template0' % name)
        conn.close()
        url = admin_url.rsplit("/", 1)[0] + "/" + name
        created.append(name)
        if migrated:
            migrate(url)
        return url
    yield make
    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    cur = conn.cursor()
    for name in created:
        cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,))
        cur.execute('DROP DATABASE IF EXISTS "%s"' % name)
    conn.close()


@pytest.fixture
def pw(tmp_path):
    path = tmp_path / "passphrase"
    path.write_text("synthetic-test-passphrase-not-a-secret\n")
    os.chmod(str(path), 0o600)
    return str(path)


class Fixture:
    """Source database with a ledger; A and B share a namespace with an explicit memory each."""

    def __init__(self, make_db, tmp_path):
        self.make_db = make_db
        self.tmp = tmp_path
        self.ledger = str(tmp_path / "ledger" / "policy.jsonl")
        os.makedirs(os.path.dirname(self.ledger), mode=0o700)
        self.url = make_db(migrated=True)
        self.config = Config(database_url=self.url, hmac_key=HMAC_KEY.encode(), policy_ledger=self.ledger)
        self.svc = MemoryService(self.config)
        self.ns = admin.create_namespace(self.url, "rec-ns")["namespace_id"]
        self.a = admin.create_principal(self.url, "dev-a")
        self.b = admin.create_principal(self.url, "dev-b")
        for p in (self.a, self.b):
            admin.grant(self.url, p["principal_id"], self.ns, write=True, ledger_path=self.ledger)

    def remember(self, token, key, text):
        receipt, _ = self.svc.submit(token, {"namespace_id": self.ns, "content": {"media_type": "text/plain",
                                                                                   "text": text},
                                             "submission": {"intent": "explicit_memory",
                                                            "actor_reported": "user"}}, key)
        return receipt

    def drain(self, config=None):
        Worker(config or self.config, "w").run_until_idle()

    def search(self, token, query, svc=None):
        return (svc or self.svc).search(token, {"namespace_id": self.ns, "query": query})["results"]

    def archive(self, passphrase):
        return recovery.backup(self.config, self.url, str(self.tmp / "bk"), passphrase)["archive"]

    def restored(self, archive, passphrase):
        url = self.make_db()
        info = recovery.restore(self.config, url, archive, passphrase)
        return url, replace(self.config, database_url=url), info


@pytest.fixture
def fx(make_db, tmp_path):
    return Fixture(make_db, tmp_path)


def test_old_backup_does_not_resurrect_deleted_or_revoked_state(fx, pw):
    keep = fx.remember(fx.a["token"], "k-keep", "keepmarker postgres is our deployment db")
    gone = fx.remember(fx.a["token"], "k-gone", "gonemarker wrong fact to be deleted")
    fx.drain()
    assert fx.search(fx.b["token"], "gonemarker")
    archive = fx.archive(pw)

    # Policy changes AFTER the backup: delete a memory, revoke device A.
    operations.delete_experience(fx.config, gone["experience_id"])
    operations.revoke_principal(fx.config, fx.a["principal_id"])
    assert fx.search(fx.b["token"], "gonemarker") == []

    url, rconfig, info = fx.restored(archive, pw)
    rsvc = MemoryService(rconfig)
    assert info["state"] == "quarantined" and info["warnings"] == []

    # 1. Quarantined: nothing is served, even to a credential that is valid in the backup.
    for token in (fx.a["token"], fx.b["token"]):
        with pytest.raises(ServiceError) as err:
            rsvc.search(token, {"namespace_id": fx.ns, "query": "gonemarker"})
        assert err.value.code == "RECOVERY_QUARANTINE"
    # 2. The raw restored data really would re-expose: verification catches it (proves the check has teeth).
    before = recovery.verify_restore(fx.config, url)
    assert not before["ok"] and any("ledger #" in f for f in before["failures"])
    # 3. Release before replay is impossible.
    with pytest.raises(recovery.RecoveryError):
        recovery.release(rconfig, url, attest_ledger_is_latest=True)
    # 4. Replay and verify.
    stats = recovery.reapply(rconfig, url)
    assert stats["delete_experience"] >= 1 and stats["revoke_principal"] == 1
    assert recovery.verify_restore(rconfig, url)["ok"]
    # 5. Release needs the explicit attestation.
    with pytest.raises(recovery.RecoveryError):
        recovery.release(rconfig, url, attest_ledger_is_latest=False)
    assert recovery.release(rconfig, url, attest_ledger_is_latest=True)["state"] == "serving"

    assert fx.search(fx.b["token"], "gonemarker", rsvc) == []            # deleted memory stays gone
    assert len(fx.search(fx.b["token"], "keepmarker", rsvc)) == 1         # shared data survives A's revocation
    with pytest.raises(ServiceError) as err:
        rsvc.search(fx.a["token"], {"namespace_id": fx.ns, "query": "keepmarker"})
    assert err.value.status == 401                                         # revoked credential stays revoked
    rows = _rows(url, "SELECT count(*) AS n FROM experiences WHERE id = %s AND raw_text IS NOT NULL",
                 (gone["experience_id"],))
    assert rows[0]["n"] == 0
    assert keep["experience_id"]


def test_grant_reduction_is_replayed_but_never_widened(fx, pw):
    fx.remember(fx.a["token"], "k1", "grantmarker content")
    fx.drain()
    archive = fx.archive(pw)
    admin.grant(fx.url, fx.b["principal_id"], fx.ns, read=True, write=False, ledger_path=fx.ledger)
    url, rconfig, _ = fx.restored(archive, pw)
    recovery.reapply(rconfig, url)
    rows = _rows(url, "SELECT can_read, can_write FROM namespace_grants WHERE principal_id = %s",
                 (fx.b["principal_id"],))
    assert rows[0]["can_read"] is True and rows[0]["can_write"] is False


def test_running_attempt_in_backup_is_fenced(fx, pw):
    fx.remember(fx.a["token"], "k1", "inflightmarker content")
    worker = Worker(fx.config, "old-worker")
    claim = worker.claim()                       # running, lease live; the worker "dies" here
    assert claim is not None
    archive = fx.archive(pw)
    url, rconfig, _ = fx.restored(archive, pw)
    recovery.reapply(rconfig, url)
    job = _rows(url, "SELECT state, attempt FROM jobs WHERE id = %s", (claim.job_id,))[0]
    assert job["state"] == "queued" and job["attempt"] == claim.attempt + 1
    # While quarantined even a finalize cannot run; after release the old attempt is stale.
    with pytest.raises(ServiceError) as err:
        Worker(rconfig, "old-worker").finalize(claim, drafts=[])
    assert err.value.code == "RECOVERY_QUARANTINE"
    recovery.release(rconfig, url, attest_ledger_is_latest=True)
    late = Worker(rconfig, "old-worker").finalize(claim, drafts=[])
    assert late.kind == "stale"
    assert _rows(url, "SELECT count(*) AS n FROM artifacts")[0]["n"] == 0


def test_restore_refuses_a_non_empty_target(fx, pw):
    archive = fx.archive(pw)
    with pytest.raises(recovery.RecoveryError, match="not empty"):
        recovery.restore(fx.config, fx.url, archive, pw)  # the live database must never be overwritten
    assert _rows(fx.url, "SELECT count(*) AS n FROM principals")[0]["n"] == 2


@pytest.mark.parametrize("damage", ["missing", "truncated_before_head", "tampered", "backup_without_ledger"])
def test_untrustworthy_ledger_keeps_database_quarantined(fx, pw, damage, tmp_path):
    gone = fx.remember(fx.a["token"], "k1", "ledgercase content")
    fx.drain()
    operations.delete_experience(fx.config, gone["experience_id"])
    other = fx.remember(fx.a["token"], "k2", "second content")
    operations.delete_experience(fx.config, other["experience_id"])
    config = fx.config
    if damage == "backup_without_ledger":
        config = replace(fx.config, policy_ledger="")
    archive = recovery.backup(config, fx.url, str(tmp_path / "bk2"), pw)["archive"]
    url = fx.make_db()
    recovery.restore(fx.config, url, archive, pw)
    lines = open(fx.ledger).read().splitlines()
    if damage == "missing":
        os.remove(fx.ledger)
    elif damage == "truncated_before_head":
        open(fx.ledger, "w").write("\n".join(lines[:1]) + "\n")
    elif damage == "tampered":
        entry = json.loads(lines[0])
        entry["data"]["experience_id"] = str(uuid.uuid4())
        open(fx.ledger, "w").write("\n".join([json.dumps(entry)] + lines[1:]) + "\n")
    rconfig = replace(fx.config, database_url=url)
    with pytest.raises(recovery.RecoveryError):
        recovery.reapply(rconfig, url)
    with pytest.raises(recovery.RecoveryError):
        recovery.release(rconfig, url, attest_ledger_is_latest=True)
    assert _rows(url, "SELECT service_state FROM system_control")[0]["service_state"] == "quarantined"


def test_release_is_refused_if_ledger_grew_after_replay(fx, pw):
    victim = fx.remember(fx.a["token"], "k1", "growmarker content")
    fx.drain()
    archive = fx.archive(pw)
    url, rconfig, _ = fx.restored(archive, pw)
    recovery.reapply(rconfig, url)
    operations.delete_experience(fx.config, victim["experience_id"])  # a newer policy entry appears
    with pytest.raises(recovery.RecoveryError, match="verification failed"):  # its effect is not yet applied
        recovery.release(rconfig, url, attest_ledger_is_latest=True)
    recovery.reapply(rconfig, url)
    ledger.append(fx.ledger, "delete_experience", {"experience_id": str(uuid.uuid4())})  # no effect here, but newer
    with pytest.raises(recovery.RecoveryError, match="changed since"):
        recovery.release(rconfig, url, attest_ledger_is_latest=True)
    recovery.reapply(rconfig, url)
    assert recovery.release(rconfig, url, attest_ledger_is_latest=True)["state"] == "serving"


def test_revoke_all_principals_option_forces_reissue(fx, pw):
    archive = fx.archive(pw)
    url, rconfig, _ = fx.restored(archive, pw)
    recovery.reapply(rconfig, url, revoke_all_principals=True)
    recovery.release(rconfig, url, attest_ledger_is_latest=True)
    for token in (fx.a["token"], fx.b["token"]):
        with pytest.raises(ServiceError) as err:
            MemoryService(rconfig).authenticate(token)
        assert err.value.status == 401


def test_archive_is_encrypted_and_needs_the_passphrase(fx, pw, tmp_path):
    fx.remember(fx.a["token"], "k1", "plaintextcanary-should-not-appear")
    archive = fx.archive(pw)
    blob = open(archive, "rb").read()
    assert b"plaintextcanary" not in blob and b"PGDMP" not in blob
    assert oct(os.stat(archive).st_mode & 0o777) == "0o600"
    wrong = tmp_path / "wrong"
    wrong.write_text("another-passphrase\n")
    with pytest.raises(recovery.RecoveryError):
        recovery.restore(fx.config, fx.make_db(), archive, str(wrong))


def test_restore_warns_when_hmac_key_differs(fx, pw):
    archive = fx.archive(pw)
    other = replace(fx.config, hmac_key=b"a-different-key-0123456789abcdefghijklmn")
    info = recovery.restore(other, fx.make_db(), archive, pw)
    assert any("HMAC" in w for w in info["warnings"])


def test_cli_roundtrip_and_no_secret_in_output(fx, pw, tmp_path):
    victim = fx.remember(fx.a["token"], "k1", "climarker content")
    fx.drain()
    target = fx.make_db()
    env = dict(os.environ, MEMORY_DATABASE_URL=fx.url, MEMORY_HMAC_KEY=HMAC_KEY, MEMORY_POLICY_LEDGER=fx.ledger,
               MEMORY_BACKUP_DATABASE_URL=fx.url)
    env["PYTHONPATH"] = os.path.join(os.path.dirname(__file__), "..", "src")
    urlfile = tmp_path / "target-url"
    urlfile.write_text(target)

    def cli(*args):
        proc = subprocess.run([sys.executable, "-m", "memory_server.recovery"] + list(args), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        return proc
    out = cli("backup", "--passphrase-file", pw, "--out-dir", str(tmp_path / "cli"))
    assert out.returncode == 0, out.stderr
    archive = json.loads(out.stdout)["archive"]
    operations.delete_experience(fx.config, victim["experience_id"])
    for args in (["restore", "--archive", archive, "--passphrase-file", pw], ["reapply"], ["verify"]):
        proc = cli(*(args + (["--target-database-url-file", str(urlfile)] if args[0] != "backup" else [])))
        assert proc.returncode == 0, (args, proc.stderr, proc.stdout)
    refused = cli("release", "--target-database-url-file", str(urlfile))
    assert refused.returncode == 2 and "attest" in refused.stderr
    ok = cli("release", "--target-database-url-file", str(urlfile), "--attest-ledger-is-latest")
    assert ok.returncode == 0, ok.stderr
    for proc in (out, refused, ok):
        assert HMAC_KEY not in proc.stdout + proc.stderr and "passphrase-not-a-secret" not in proc.stdout + proc.stderr


def test_require_ledger_refuses_to_start_without_one():
    env = {"MEMORY_DATABASE_URL": "postgresql://x/y", "MEMORY_HMAC_KEY": HMAC_KEY, "MEMORY_REQUIRE_LEDGER": "1"}
    with pytest.raises(SystemExit):
        Config.from_env(env)
    env["MEMORY_POLICY_LEDGER"] = "/tmp/ledger.jsonl"
    assert Config.from_env(env).require_ledger is True


def test_ledger_write_failure_aborts_the_delete(fx):
    victim = fx.remember(fx.a["token"], "k1", "failclosedmarker content")
    fx.drain()
    bad = replace(fx.config, policy_ledger="/proc/nonexistent/ledger.jsonl")
    with pytest.raises(Exception):
        operations.delete_experience(bad, victim["experience_id"])
    assert fx.search(fx.b["token"], "failclosedmarker")  # nothing was half-applied; the delete simply did not happen


def _rows(url, sql, params=()):
    conn = connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()
