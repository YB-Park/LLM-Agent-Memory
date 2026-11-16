"""Upgrade the original schema on PostgreSQL using synthetic legacy records."""
import uuid
from dataclasses import replace
from pathlib import Path

import psycopg2.errors
import pytest
from psycopg2.extras import Json

from conftest import HMAC_KEY
from memory_server import ledger, recovery
from memory_server.config import Config
from memory_server.db import connect
from memory_server.engine import Snapshot, engine_for_spec, reference_spec, spec_digest
from memory_server.errors import ServiceError
from memory_server.migrate import MIGRATIONS_DIR, SINGLE_MEMORY_MIGRATION, migrate
from memory_server.service import MemoryService, credential_digest
from memory_server.validation import canonical_json, hmac_hex, job_input_digest, sha256_hex, stored_metadata
from memory_server.worker import Claim, Worker

BODY = {"content": {"media_type": "text/plain", "text": "legacy PostgreSQL fixture"},
        "source": {"kind": "note", "source_ref": "synthetic:legacy"},
        "submission": {"intent": "explicit_memory", "actor_reported": "user"}}
DOC = {"text": BODY["content"]["text"], "source": BODY["source"],
       "submission": {**BODY["submission"], "title": None, "retrieval_hints": []}}


def new_id():
    return str(uuid.uuid4())


def query(dsn, statement, params=()):
    conn = connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


@pytest.fixture
def legacy(make_db, tmp_path):
    dsn = make_db()
    migrations = tmp_path / "legacy-migrations"
    migrations.mkdir()
    for name in ("0001_init.sql", "0002_service_state.sql"):
        (migrations / name).write_bytes((Path(MIGRATIONS_DIR) / name).read_bytes())
    migrate(dsn, migrations)
    key = HMAC_KEY.encode()
    config = Config(database_url=dsn, hmac_key=key, policy_ledger=str(tmp_path / "policy.jsonl"))
    ledger.ensure(config.policy_ledger)
    token = "mem_legacy_synthetic"
    pid = new_id()
    query(dsn, "INSERT INTO principals (id, name, credential_digest, kind) VALUES (%s, 'legacy', %s, 'device')",
          (pid, credential_digest(token)))
    return config, pid, token


def legacy_generation(dsn, pid, version="1"):
    ns, gen = new_id(), new_id()
    spec = reference_spec(version)
    query(dsn, "INSERT INTO namespaces (id, name, active_generation_id, ingest_revision) VALUES (%s, %s, %s, 4); "
          "INSERT INTO generations (id, namespace_id, engine_spec, spec_digest, index_profile, state) "
          "VALUES (%s, %s, %s, %s, 'reference-keywords/v1', 'servable'); "
          "INSERT INTO namespace_grants (principal_id, namespace_id, can_read, can_write) "
          "VALUES (%s, %s, true, true)", (ns, ns, gen, gen, ns, Json(spec), spec_digest(spec), pid, ns))
    return ns, gen


def legacy_submit(config, pid, ns, gen, key, state="queued"):
    exp, job = new_id(), new_id()
    request = {"api_version": "v1", "namespace_id": ns, **DOC}
    query(config.database_url,
          "INSERT INTO experiences (id, namespace_id, raw_text, source_metadata, content_digest, state, created_by) "
          "VALUES (%s, %s, %s, %s, %s, 'retained', %s); "
          "INSERT INTO jobs (id, namespace_id, generation_id, experience_id, source_version, input_digest, "
          "requested_by, authority_epoch, state) VALUES (%s, %s, %s, %s, 1, %s, %s, 1, 'queued'); "
          "INSERT INTO ingest_receipts (principal_id, namespace_id, key_digest, request_digest, experience_id, "
          "initial_job_id) VALUES (%s, %s, %s, %s, %s, %s)",
          (exp, ns, DOC["text"], Json(stored_metadata(DOC)), sha256_hex(DOC["text"]), pid,
           job, ns, gen, exp, job_input_digest(DOC), pid,
           pid, ns, hmac_hex(config.hmac_key, key), hmac_hex(config.hmac_key, canonical_json(request)), exp, job))
    if state == "running":
        query(config.database_url, "UPDATE jobs SET state = 'running', attempt = 1, lease_owner = 'old', "
              "lease_until = now() + interval '5 minutes' WHERE id = %s", (job,))
    return exp, job


def test_fresh_database_has_single_generation_and_no_namespace_storage(database_url):
    assert query(database_url, "SELECT table_name FROM information_schema.columns WHERE table_schema = 'public' "
                 "AND (column_name = 'namespace_id' OR table_name IN ('namespaces', 'namespace_grants'))") == []
    [row] = query(database_url, "SELECT g.state, g.engine_spec->>'version' AS version, c.write_gate "
                  "FROM system_control c JOIN generations g ON g.id = c.active_generation_id")
    assert row == {"state": "servable", "version": "1", "write_gate": "open"}


def test_one_legacy_namespace_preserves_history_receipts_and_fences_running_worker(legacy):
    config, pid, token = legacy
    dsn = config.database_url
    ns, gen = legacy_generation(dsn, pid)
    exp, job = legacy_submit(config, pid, ns, gen, "held", state="running")
    prior_exp, prior_job = legacy_submit(config, pid, ns, gen, "done")
    gone_exp, gone_job = legacy_submit(config, pid, ns, gen, "gone")
    spec = reference_spec("1")
    snap = Snapshot(exp, 1, DOC["text"], DOC["source"], DOC["submission"])
    old = Claim(job, gen, 1, "old", spec, spec_digest(spec), job_input_digest(DOC), snap)
    draft = engine_for_spec(spec).process(replace(snap, experience_id=prior_exp))[0]
    artifact = new_id()
    query(dsn, "UPDATE jobs SET state = 'succeeded', output_count = 1 WHERE id = %s; "
          "INSERT INTO artifacts (id, namespace_id, generation_id, job_id, local_key, payload_schema, payload, "
          "display_text, search_text, state) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'valid'); "
          "INSERT INTO artifact_sources (artifact_id, experience_id, source_version, namespace_id) "
          "VALUES (%s, %s, 1, %s)",
          (prior_job, artifact, ns, gen, prior_job, draft.local_key, draft.payload_schema, Json(draft.payload),
           draft.display_text, draft.search_text, artifact, prior_exp, ns))
    query(dsn, "UPDATE experiences SET state = 'deleted', raw_text = NULL, source_metadata = NULL, "
          "content_digest = NULL, deleted_at = now() WHERE id = %s; "
          "UPDATE jobs SET state = 'cancelled' WHERE id = %s", (gone_exp, gone_job))
    retired_id, comparison_id = new_id(), new_id()
    query(dsn, "INSERT INTO generations (id, namespace_id, engine_spec, spec_digest, index_profile, state, "
          "retired_ingest_revision, comparison_receipt) "
          "VALUES (%s, %s, %s, %s, 'reference-keywords/v1', 'retired', 4, %s)",
          (retired_id, ns, Json(spec), spec_digest(spec), Json({"receipt_id": comparison_id, "namespace_id": ns})))
    outsider = new_id()
    query(dsn, "INSERT INTO principals (id, name, credential_digest, kind) "
          "VALUES (%s, 'no legacy grant', %s, 'admin')", (outsider, credential_digest("no-legacy-grant")))

    assert migrate(dsn) == [SINGLE_MEMORY_MIGRATION]
    assert migrate(dsn) == []
    svc = MemoryService(config)
    receipt, replayed = svc.submit(token, BODY, "held")
    assert replayed and (receipt["experience_id"], receipt["job_id"], receipt["generation_id"]) == (exp, job, gen)
    with pytest.raises(ServiceError, match="같은 키") as error:
        svc.submit(token, {**BODY, "source": {"kind": "other"}}, "held")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(ServiceError) as error:
        svc.submit(token, BODY, "gone")
    assert error.value.code == "EXPERIENCE_DELETED"
    with pytest.raises(ServiceError) as error:
        svc.search("no-legacy-grant", {"query": "PostgreSQL"})
    assert error.value.status == 404
    [original] = svc.search(token, {"query": "PostgreSQL"})["results"]
    assert original["artifact_id"] == artifact and original["sources"][0]["experience_id"] == prior_exp
    assert svc.get_experience(token, exp)["source"]["source_ref"] == "synthetic:legacy"
    worker = Worker(config, "old")
    assert worker.finalize(old, worker.compute(old)).kind == "stale"
    assert worker.process_one().kind == "succeeded"
    [retired] = query(dsn, "SELECT state, comparison_receipt FROM generations WHERE id = %s", (retired_id,))
    assert retired == {"state": "retired", "comparison_receipt": {"receipt_id": comparison_id}}
    [control] = query(dsn, "SELECT active_generation_id, ingest_revision FROM system_control")
    assert control == {"active_generation_id": gen, "ingest_revision": 4}


def test_multiple_legacy_namespaces_with_colliding_keys_are_rejected_atomically(legacy):
    config, pid, _ = legacy
    dsn = config.database_url
    for version in ("1", "2"):
        ns, gen = legacy_generation(dsn, pid, version)
        legacy_submit(config, pid, ns, gen, "colliding-key")
    with pytest.raises(psycopg2.errors.RaiseException, match="no automatic merge"):
        migrate(dsn)
    assert len(query(dsn, "SELECT active_generation_id FROM namespaces")) == 2
    assert len(query(dsn, "SELECT DISTINCT key_digest FROM ingest_receipts")) == 1
    assert len(query(dsn, "SELECT id FROM experiences")) == 2
    assert query(dsn, "SELECT version FROM schema_migrations WHERE version = %s", (SINGLE_MEMORY_MIGRATION,)) == []


@pytest.mark.parametrize("problem", ["paused", "building", "missing active"])
def test_incompatible_legacy_generation_state_is_rejected(legacy, problem):
    config, pid, _ = legacy
    dsn = config.database_url
    ns, gen = legacy_generation(dsn, pid)
    if problem == "paused":
        query(dsn, "UPDATE namespaces SET write_gate = 'paused', maintenance_generation_id = %s", (gen,))
    elif problem == "building":
        query(dsn, "UPDATE generations SET state = 'building'")
    else:
        query(dsn, "UPDATE namespaces SET active_generation_id = NULL")
    with pytest.raises(psycopg2.errors.RaiseException, match="upgrade refused"):
        migrate(dsn)
    assert query(dsn, "SELECT id FROM namespaces")[0]["id"] == ns


@pytest.mark.parametrize("mapped", [True, False])
def test_legacy_backup_replay_preserves_hashes_and_rejects_unmapped_grants(legacy, make_db, tmp_path, monkeypatch,
                                                                      mapped):
    config, pid, token = legacy
    dsn = config.database_url
    ns, gen = legacy_generation(dsn, pid)
    exp, _ = legacy_submit(config, pid, ns, gen, "old-backup")
    passphrase = tmp_path / "passphrase"
    passphrase.write_text("synthetic-only-passphrase\n")
    # The original archive used the same fields and encryption with format 1.
    with monkeypatch.context() as patch:
        patch.setattr(recovery, "FORMAT", 1)
        archive = recovery.backup(config, dsn, str(tmp_path / "backup"), str(passphrase))["archive"]
    ledger.append(config.policy_ledger, "grant_reduce",
                  {"principal_id": pid, "namespace_id": ns if mapped else new_id(),
                   "read": True, "write": False, "manage": False})
    ledger.append(config.policy_ledger, "delete_experience", {"experience_id": exp})
    ledger.append(config.policy_ledger, "permission_reduce",
                  {"principal_id": pid, "read": True, "write": True, "manage": False})
    latest_entries, seq, digest = ledger.verify(config.policy_ledger)
    target = make_db()
    restored = replace(config, database_url=target)
    recovery.restore(config, target, archive, str(passphrase))
    svc = MemoryService(restored)
    with pytest.raises(ServiceError) as error:
        svc.submit(token, BODY, "quarantine")
    assert error.value.code == "RECOVERY_QUARANTINE"
    if not mapped:
        with pytest.raises(recovery.RecoveryError, match="does not match"):
            recovery.reapply(restored, target)
        with pytest.raises(recovery.RecoveryError, match="verification failed"):
            recovery.release(restored, target, True)
        assert query(target, "SELECT service_state FROM system_control")[0]["service_state"] == "quarantined"
        assert query(target, "SELECT state FROM experiences WHERE id = %s", (exp,))[0]["state"] == "retained"
    else:
        stats = recovery.reapply(restored, target)
        assert stats["grant_reduce"] == stats["delete_experience"] == stats["permission_reduce"] == 1
        assert recovery.release(restored, target, True)["state"] == "serving"
        assert svc.search(token, {"query": "PostgreSQL"})["results"] == []
        with pytest.raises(ServiceError) as error:
            svc.submit(token, BODY, "never-write")
        assert error.value.status == 404
        with pytest.raises(ServiceError) as error:
            svc.get_experience(token, exp)
        assert error.value.code == "EXPERIENCE_DELETED"
    assert ledger.verify(config.policy_ledger) == (latest_entries, seq, digest)
