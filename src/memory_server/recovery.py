"""Backup, quarantined restore and policy re-application (P0-5; docs/operations/backup-recovery.md).

Invariant: a deleted source or a revoked/reduced credential must not come back because an older backup was
restored. A backup cannot know about later policy changes, so the policy ledger (ledger.py, kept outside the
database volume) is replayed onto the restored database, and the database serves only after verification and an
explicit operator release. Fail closed: restore always ends in `quarantined`; no ledger, no release.

Backups are encrypted (gpg symmetric AES-256) archives holding a pg_dump (custom format) and a manifest.
Nothing here is a guarantee of physical erasure: deleted text may persist in older backups; the ledger only
prevents it from being *served* after a restore.
"""
import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import parse_qs, unquote, urlparse

import psycopg2
from psycopg2.extras import Json

from . import ledger, operations
from .config import Config, _setting
from .db import connect
from .errors import ServiceError
from .migrate import migrate
from .service import legacy_namespace
from .validation import parse_uuid

FORMAT = 2


class RecoveryError(Exception):
    pass


# --- helpers ----------------------------------------------------------------
def _pg_env(dsn):
    """libpq environment for a postgresql:// URL, so the password never appears in a process listing."""
    u = urlparse(dsn)
    env = dict(os.environ)
    for key in [k for k in env if k.startswith("PG")]:
        del env[key]
    if u.hostname:
        env["PGHOST"] = u.hostname
    if u.port:
        env["PGPORT"] = str(u.port)
    if u.username:
        env["PGUSER"] = unquote(u.username)
    if u.password:
        env["PGPASSWORD"] = unquote(u.password)
    env["PGDATABASE"] = unquote(u.path.lstrip("/"))
    for k, v in parse_qs(u.query).items():
        if k == "sslmode":
            env["PGSSLMODE"] = v[0]
    return env


def _run(cmd, env, stdin_bytes=None):
    proc = subprocess.run(cmd, env=env, input=stdin_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RecoveryError("%s failed: %s" % (cmd[0], proc.stderr.decode("utf-8", "replace").strip()[-400:]))
    return proc.stdout


def _gpg(args, passphrase_file):
    if not os.path.isfile(passphrase_file):
        raise RecoveryError("passphrase file not found")
    return ["gpg", "--batch", "--yes", "--quiet", "--pinentry-mode", "loopback", "--passphrase-file",
            passphrase_file] + args


def _run_gpg(args, passphrase_file, stdin_bytes=None):
    # Throwaway GNUPGHOME: the container has a read-only root and no home directory.
    with tempfile.TemporaryDirectory(prefix="gnupg-") as home:
        os.chmod(home, 0o700)
        env = dict(os.environ, GNUPGHOME=home)
        return _run(_gpg(args, passphrase_file), env, stdin_bytes)


def _key_fingerprint(config):
    return hashlib.sha256(b"memory-hmac-key-id:" + config.hmac_key).hexdigest()[:16]


def _scalar(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


# --- backup -----------------------------------------------------------------
def backup(config, backup_database_url, out_dir, passphrase_file):
    """Encrypted archive = pg_dump + manifest. Uses a read-only role; records the ledger head it was taken at."""
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    ledger.ensure(config.policy_ledger)
    ledger_head = ledger.head(config.policy_ledger) if config.policy_ledger else None
    conn = connect(backup_database_url)
    try:
        migrations = [r["version"] for r in _all(conn, "SELECT version FROM schema_migrations ORDER BY version")]
        engines = [r["engine_spec"] for r in _all(conn, "SELECT DISTINCT engine_spec FROM generations")]
        policy_revision = _scalar(conn, "SELECT policy_revision FROM system_control")["policy_revision"]
        pg_version = _scalar(conn, "SHOW server_version")["server_version"]
    finally:
        conn.close()
    dump = _run(["pg_dump", "--format=custom", "--no-owner", "--no-acl"], _pg_env(backup_database_url))
    manifest = {"format": FORMAT, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "postgres_version": pg_version, "migrations": migrations, "engine_specs": engines,
                "policy_revision": policy_revision, "ledger_head": ledger_head,
                "hmac_key_id": _key_fingerprint(config), "dump_sha256": hashlib.sha256(dump).hexdigest()}
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, blob in (("manifest.json", json.dumps(manifest, sort_keys=True).encode()), ("db.dump", dump)):
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            tar.addfile(info, io.BytesIO(blob))
    encrypted = _run_gpg(["--symmetric", "--cipher-algo", "AES256", "--output", "-"], passphrase_file,
                         raw.getvalue())
    path = os.path.join(out_dir, "memory-%s.tar.gpg" % time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(encrypted)
        fh.flush()
        os.fsync(fh.fileno())
    return {"archive": path, "manifest": {k: manifest[k] for k in ("created_at", "ledger_head", "policy_revision")}}


def _all(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _open_archive(archive, passphrase_file):
    blob = _run_gpg(["--decrypt", archive], passphrase_file)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r") as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
        dump = tar.extractfile("db.dump").read()
    if manifest.get("format") not in (1, FORMAT) or hashlib.sha256(dump).hexdigest() != manifest.get("dump_sha256"):
        raise RecoveryError("backup archive is corrupt or has an unknown format")
    return manifest, dump


# --- restore ----------------------------------------------------------------
def restore(config, target_database_url, archive, passphrase_file, runtime_role=None, backup_role=None):
    """Restore into a NEW, EMPTY database and leave it quarantined. Never overwrites a database holding tables."""
    manifest, dump = _open_archive(archive, passphrase_file)
    conn = connect(target_database_url)
    try:
        existing = _scalar(conn, "SELECT count(*) AS n FROM pg_tables WHERE schemaname = 'public'")["n"]
    finally:
        conn.close()
    if existing:
        raise RecoveryError("refusing to restore: the target database is not empty (restore only into a fresh "
                            "database, never over live data)")
    env = _pg_env(target_database_url)
    # Commit the dump and the quarantine together. There is no serving-state
    # interval, even if a client already points at the target database.
    conn = connect(target_database_url)
    try:
        with conn.cursor() as cur:
            metadata = Json({"backup_created_at": manifest["created_at"], "ledger_head": manifest["ledger_head"],
                             "hmac_key_id": manifest["hmac_key_id"]})
            quarantine_sql = cur.mogrify(
                "UPDATE public.system_control SET service_state = 'quarantined', service_state_changed_at = now() "
                "WHERE id = 1; "
                "INSERT INTO public.audit_events (event_type, safe_details) VALUES ('recovery_restored', %s);",
                (metadata,))
    finally:
        conn.close()
    with tempfile.NamedTemporaryFile(prefix="memory-dump-") as archive_file, \
            tempfile.NamedTemporaryFile(prefix="memory-restore-sql-") as sql_file:
        archive_file.write(dump)
        archive_file.flush()
        _run(["pg_restore", "--no-owner", "--no-acl", "--file", sql_file.name, archive_file.name], env)
        with open(sql_file.name, "ab") as script:
            script.write(b"\n" + quarantine_sql + b"\n")
        _run(["psql", "-X", "--no-password", "--single-transaction", "--set=ON_ERROR_STOP=1", "--file", sql_file.name],
             env)
    try:
        migrate(target_database_url, runtime_role=runtime_role, backup_role=backup_role)
    except psycopg2.Error as exc:
        raise RecoveryError("restored database stays quarantined: %s" % exc.diag.message_primary) from exc
    warnings = []
    if manifest["hmac_key_id"] != _key_fingerprint(config):
        warnings.append("MEMORY_HMAC_KEY differs from the backup's key: release will be refused")
    if manifest["ledger_head"] is None:
        warnings.append("the backup was taken without a policy ledger: release will be refused")
    return {"state": "quarantined", "manifest": manifest, "warnings": warnings}


def _lock_restore(cur):
    control = operations.lock_policy(cur)
    if "active_generation_id" not in control:
        raise RecoveryError("schema upgrade incomplete: the restore cannot be verified or released")
    return control


def _restore_record(cur):
    cur.execute("SELECT safe_details FROM audit_events WHERE event_type = 'recovery_restored' "
                "ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    return row["safe_details"] if row else None


# --- ledger replay ----------------------------------------------------------
def reapply(config, target_database_url, ledger_path=None, revoke_all_principals=False):
    """Replay every ledger entry onto the (quarantined) restored database, idempotently, in one transaction."""
    ledger_path = ledger_path or config.policy_ledger
    if not ledger_path:
        raise RecoveryError("no policy ledger configured: the restored database stays quarantined")
    if not os.path.exists(ledger_path):
        raise RecoveryError("policy ledger file is missing: the restored database stays quarantined")
    try:
        entries, head_seq, head_hash = ledger.verify(ledger_path)
    except ledger.LedgerError as exc:
        raise RecoveryError("policy ledger is not trustworthy: %s" % exc)
    stats = {"delete_experience": 0, "revoke_principal": 0, "grant_reduce": 0,
             "permission_reduce": 0, "absent": 0}

    def fn(cur):
        control = _lock_restore(cur)
        if control["service_state"] != "quarantined":
            raise RecoveryError("reapply only runs on a quarantined database")
        recorded = _restore_record(cur)
        if recorded is None:
            raise RecoveryError("target database was not produced by restore; refusing to re-apply")
        bhead = recorded["ledger_head"]
        if bhead is None:
            raise RecoveryError("the backup recorded no ledger head: its policy history cannot be proven complete")
        if (head_seq < bhead["seq"]
                or (bhead["seq"] > 0 and entries[bhead["seq"] - 1]["hash"] != bhead["hash"])):
            raise RecoveryError("policy ledger is older than, or diverges from, the backup's recorded head")
        for entry in entries:
            kind, data = entry["type"], entry["data"]
            try:
                if kind == "delete_experience":
                    operations._delete_tx(cur, data["experience_id"], None)
                elif kind == "revoke_principal":
                    operations._revoke_tx(cur, data["principal_id"], None)
                else:
                    _reduce_permissions(cur, _permission_event(cur, kind, data))
                stats[kind] += 1
            except ServiceError as exc:
                if exc.code != "NOT_FOUND":
                    raise
                stats["absent"] += 1  # created after the backup, so it does not exist here
        if revoke_all_principals:
            cur.execute("SELECT id FROM principals WHERE revoked_at IS NULL")
            for row in cur.fetchall():
                operations._revoke_tx(cur, row["id"], None)
        # Old attempts must never publish into this database: bump every running attempt and requeue.
        cur.execute("UPDATE jobs SET state = 'queued', attempt = attempt + 1, lease_owner = NULL, lease_until = NULL, "
                    "not_before = now(), error_code = 'RECOVERY_FENCED' WHERE state = 'running'")
        stats["jobs_fenced"] = cur.rowcount
        operations.bump_policy(cur)
        operations._audit(cur, "recovery_reapplied", None, {}, {"ledger_head": {"seq": head_seq, "hash": head_hash},
                                                              "entries": len(entries), "revoke_all": revoke_all_principals})

    conn = connect(target_database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '5s'")
            fn(cur)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    stats["ledger_head"] = {"seq": head_seq, "hash": head_hash}
    return stats


def _permission_event(cur, kind, data):
    if kind == "grant_reduce":
        legacy_id = legacy_namespace(cur)
        if legacy_id is None or data.get("namespace_id") != legacy_id:
            raise RecoveryError("legacy permission event does not match the migrated database")
    flags = tuple(data.get(name) for name in ("read", "write", "manage"))
    if not all(isinstance(flag, bool) for flag in flags) or flags[2] > flags[1] or flags[1] > flags[0]:
        raise RecoveryError("invalid permission flags in policy ledger")
    try:
        pid = parse_uuid(data.get("principal_id"), "principal_id")
    except ServiceError as exc:
        raise RecoveryError("invalid principal ID in permission ledger entry") from exc
    return pid, *flags


def _reduce_permissions(cur, permission):
    """Intersect current permissions with the recorded maximum; never increase access on recovery."""
    pid, read, write, manage = permission
    cur.execute("UPDATE principals SET can_read = can_read AND %s, can_write = can_write AND %s, "
                "can_manage = can_manage AND %s, auth_epoch = auth_epoch + 1 WHERE id = %s AND "
                "((can_read AND NOT %s) OR (can_write AND NOT %s) OR (can_manage AND NOT %s))",
                (read, write, manage, pid, read, write, manage))
    if cur.rowcount:
        operations.fence_principal_jobs(cur, pid)


# --- verification and release ----------------------------------------------
def verify_restore(config, target_database_url, ledger_path=None):
    """Checks the restored database against the ledger and the invariants. Returns {ok, checks, failures}."""
    ledger_path = ledger_path or config.policy_ledger
    conn = connect(target_database_url)
    try:
        with conn.cursor() as cur:
            control = _lock_restore(cur)
            return _verify_locked(config, cur, control, ledger_path)[0]
    finally:
        conn.close()


def _verify_locked(config, cur, control, ledger_path):
    checks, failures = [], []

    def check(name, ok):
        checks.append(name)
        if not ok:
            failures.append(name)

    def count(query, params=()):
        cur.execute(query, params)
        return cur.fetchone()["n"]

    entries, current = [], None
    if not ledger_path or not os.path.exists(ledger_path):
        failures.append("policy ledger unavailable")
    else:
        try:
            entries, seq, digest = ledger.verify(ledger_path)
            current = {"seq": seq, "hash": digest}
            checks.append("ledger chain valid")
        except ledger.LedgerError as exc:
            failures.append("ledger chain: %s" % exc)
    recorded = _restore_record(cur)
    check("backup HMAC key matches", recorded is not None and recorded.get("hmac_key_id") == _key_fingerprint(config))
    check("active generation is servable", count(
        "SELECT count(*) AS n FROM generations WHERE id = %s AND state = 'servable'",
        (control["active_generation_id"],)) == 1)
    check("no valid artifact cites a deleted source", count(
        "SELECT count(*) AS n FROM artifacts a WHERE a.state = 'valid' AND (EXISTS (SELECT 1 FROM "
        "artifact_sources s JOIN experiences e ON e.id = s.experience_id WHERE s.artifact_id = a.id AND "
        "e.state = 'deleted') OR EXISTS (SELECT 1 FROM jobs j JOIN experiences e ON e.id = j.experience_id "
        "WHERE j.id = a.job_id AND e.state = 'deleted'))") == 0)
    check("deleted sources hold no content", count("SELECT count(*) AS n FROM experiences WHERE state = 'deleted' "
                                                  "AND (raw_text IS NOT NULL OR source_metadata IS NOT NULL)") == 0)
    check("no live leases or running jobs", count("SELECT count(*) AS n FROM jobs WHERE state = 'running' OR "
                                                "lease_owner IS NOT NULL") == 0)
    check("all open jobs have current write authority", count(
        "SELECT count(*) AS n FROM jobs j JOIN principals p ON p.id = j.requested_by "
        "WHERE j.state IN ('queued', 'running') AND "
        "(p.revoked_at IS NOT NULL OR NOT p.can_write OR p.auth_epoch <> j.authority_epoch)") == 0)
    for entry in entries:
        kind, data = entry["type"], entry["data"]
        if kind == "delete_experience":
            ok = count("SELECT count(*) AS n FROM experiences WHERE id = %s AND state <> 'deleted'",
                       (data["experience_id"],)) == 0
        elif kind == "revoke_principal":
            ok = count("SELECT count(*) AS n FROM principals WHERE id = %s AND revoked_at IS NULL",
                       (data["principal_id"],)) == 0
        else:
            try:
                permission = _permission_event(cur, kind, data)
            except RecoveryError as exc:
                failures.append("ledger #%d %s: %s" % (entry["seq"], kind, exc))
                continue
            ok = count("SELECT count(*) AS n FROM principals WHERE id = %s AND "
                       "((can_read AND NOT %s) OR (can_write AND NOT %s) OR (can_manage AND NOT %s))",
                       permission) == 0
        check("ledger #%d %s holds" % (entry["seq"], kind), ok)
    return {"ok": not failures, "state": control["service_state"], "checks": len(checks),
            "failures": failures}, current


def release(config, target_database_url, attest_ledger_is_latest, ledger_path=None):
    """Release requires verification, replay of the current head and operator attestation."""
    ledger_path = ledger_path or config.policy_ledger
    if not attest_ledger_is_latest:
        raise RecoveryError("release requires --attest-ledger-is-latest: only the operator can confirm the latest copy")
    conn = connect(target_database_url)
    try:
        with conn.cursor() as cur:
            control = _lock_restore(cur)
            if control["service_state"] != "quarantined":
                raise RecoveryError("database is not quarantined")
            report, current = _verify_locked(config, cur, control, ledger_path)
            if not report["ok"]:
                raise RecoveryError("verification failed: %s" % "; ".join(report["failures"]))
            cur.execute("SELECT safe_details FROM audit_events WHERE event_type = 'recovery_reapplied' AND "
                        "id > (SELECT max(id) FROM audit_events WHERE event_type = 'recovery_restored') "
                        "ORDER BY id DESC LIMIT 1")
            replay = cur.fetchone()
            if not replay:
                raise RecoveryError("this restore has not re-applied the ledger")
            if replay["safe_details"]["ledger_head"] != current:
                raise RecoveryError("the ledger changed since it was last re-applied: run reapply again")
            cur.execute("UPDATE system_control SET service_state = 'serving', service_state_changed_at = now()")
            operations._audit(cur, "recovery_released", None, {}, {"ledger_head": current, "attested": True})
        conn.commit()
    finally:
        conn.close()
    return {"state": "serving", "ledger_head": current}


def quarantine(config):
    """Manually close the service (also usable before maintenance); admin operations keep working."""
    def fn(cur):
        cur.execute("UPDATE system_control SET service_state = 'quarantined', service_state_changed_at = now()")
        operations._audit(cur, "service_quarantined", None, {})
        return {"state": "quarantined"}
    from .db import run_tx
    return run_tx(config, fn)


# --- CLI -----------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(prog="memory_server.recovery", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("backup", "restore", "reapply", "verify", "release", "quarantine", "ledger-verify"):
        sp = sub.add_parser(name)
        if name in ("restore", "reapply", "verify", "release"):
            sp.add_argument("--target-database-url-file", required=False,
                            help="file holding the restored database's URL (default: MEMORY_DATABASE_URL)")
        if name in ("backup", "restore"):
            sp.add_argument("--passphrase-file", required=True)
        if name == "backup":
            sp.add_argument("--out-dir", required=True)
        if name == "restore":
            sp.add_argument("--archive", required=True)
        if name == "reapply":
            sp.add_argument("--revoke-all-principals", action="store_true")
        if name == "release":
            sp.add_argument("--attest-ledger-is-latest", action="store_true")
    args = parser.parse_args(argv)
    env = os.environ
    config = Config.from_env()

    def target():
        path = getattr(args, "target_database_url_file", None)
        if path:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        return config.database_url
    try:
        if args.cmd == "backup":
            url = _setting(env, "MEMORY_BACKUP_DATABASE_URL", required=True)
            out = backup(config, url, args.out_dir, args.passphrase_file)
        elif args.cmd == "restore":
            out = restore(config, target(), args.archive, args.passphrase_file,
                          env.get("MEMORY_RUNTIME_ROLE") or None, env.get("MEMORY_BACKUP_ROLE") or None)
        elif args.cmd == "reapply":
            out = reapply(config, target(), revoke_all_principals=args.revoke_all_principals)
        elif args.cmd == "verify":
            out = verify_restore(config, target())
        elif args.cmd == "release":
            out = release(config, target(), args.attest_ledger_is_latest)
        elif args.cmd == "quarantine":
            out = quarantine(config)
        else:
            out = ledger.head(config.policy_ledger)
    except (RecoveryError, ledger.LedgerError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0 if out.get("ok", True) else 3


if __name__ == "__main__":
    sys.exit(main())
