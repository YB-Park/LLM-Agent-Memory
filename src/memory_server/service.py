"""Application service: auth, authorization, submit/receipt/job/source (§3, §5.1, §6)."""
import hashlib
import uuid

from psycopg2.extras import Json

from . import hooks, search as search_mod
from .db import run_tx
from .errors import ServiceError, invalid, not_found, unauthenticated
from .validation import (hmac_hex, iso, job_input_digest, parse_uuid, request_digest, sha256_hex,
                         stored_metadata, validate_idempotency_key, validate_submit)


def credential_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _lock_control(cur, exclusive, allow_quarantined=False):
    """Serialize against policy changes; refuse service while the database is quarantined (fail closed).

    Only administrator/recovery operations pass allow_quarantined=True.
    """
    mode = "UPDATE" if exclusive else "SHARE"
    cur.execute("SELECT * FROM system_control WHERE id = 1 FOR " + mode)
    row = cur.fetchone()
    if row["service_state"] != "serving" and not allow_quarantined:
        raise ServiceError(503, "RECOVERY_QUARANTINE", "서비스가 복구 검증 중입니다. 운영자가 해제할 때까지 사용할 수 없습니다.",
                           retryable=True, retry_after_ms=30000)
    return row


def _authenticate(cur, token):
    if not token:
        raise unauthenticated()
    cur.execute("SELECT id, auth_epoch, can_read, can_write FROM principals WHERE credential_digest = %s AND revoked_at IS NULL",
                (credential_digest(token),))
    row = cur.fetchone()
    if row is None:
        raise unauthenticated()
    return row


def legacy_namespace(cur):
    """Historical mapping for v1 receipt and policy replay compatibility."""
    cur.execute("SELECT safe_details->>'legacy_namespace_id' AS id FROM audit_events "
                "WHERE event_type = 'single_memory_migrated'")
    rows = cur.fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


class MemoryService:
    def __init__(self, config):
        self.config = config

    # --- submit -------------------------------------------------------
    def submit(self, token, body, idempotency_key):
        """Returns (receipt dict, replayed). Source, receipt and Job commit together (I1)."""
        result = run_tx(self.config, lambda cur: self._submit_tx(cur, token, body, idempotency_key))
        hooks.fire("submit_after_commit")
        return result

    def _submit_tx(self, cur, token, body, idempotency_key):
        cfg = self.config
        control = _lock_control(cur, exclusive=True)
        principal = _authenticate(cur, token)
        if not principal["can_write"]:
            raise not_found()
        key = validate_idempotency_key(idempotency_key)
        inp = validate_submit(body, cfg)
        key_digest = hmac_hex(cfg.hmac_key, key)

        cur.execute("SELECT r.request_digest, r.request_version, r.experience_id, r.initial_job_id, e.state, j.generation_id "
                    "FROM ingest_receipts r "
                    "JOIN experiences e ON e.id = r.experience_id "
                    "JOIN jobs j ON j.id = r.initial_job_id "
                    "WHERE r.principal_id = %s AND r.key_digest = %s",
                    (principal["id"], key_digest))
        receipt = cur.fetchone()
        if receipt is not None:
            legacy_id = None
            if receipt["request_version"] == 1:
                legacy_id = legacy_namespace(cur)
                if legacy_id is None:
                    raise ServiceError(503, "RECEIPT_HISTORY_UNAVAILABLE", "이전 접수 기록을 확인할 수 없습니다.")
            if receipt["request_digest"] != request_digest(cfg, inp, legacy_id):
                raise ServiceError(409, "IDEMPOTENCY_CONFLICT", "같은 키로 다른 요청이 이미 접수되었습니다.")
            if receipt["state"] == "deleted":
                raise ServiceError(410, "EXPERIENCE_DELETED", "접수된 Experience가 삭제되었습니다.")
            return self._receipt_body(receipt["experience_id"], receipt["initial_job_id"],
                                      receipt["generation_id"], True), True

        if control["write_gate"] != "open":
            raise ServiceError(503, "INGEST_PAUSED", "서버가 재처리 중입니다. 같은 키로 나중에 다시 제출하세요.",
                               retryable=True, retry_after_ms=5000)
        if control["active_generation_id"] is None:
            raise ServiceError(503, "NO_ACTIVE_GENERATION", "활성 세대가 없습니다.", retryable=True,
                               retry_after_ms=5000)
        cur.execute("SELECT count(*) AS n FROM experiences WHERE state = 'retained'")
        if cur.fetchone()["n"] >= cfg.max_retained:
            raise ServiceError(409, "MEMORY_CAPACITY", "보존 가능한 원본 수 한도에 도달했습니다.")
        cur.execute("SELECT count(*) AS n FROM jobs WHERE state IN ('queued', 'running')")
        if cur.fetchone()["n"] >= cfg.max_open_jobs:
            raise ServiceError(503, "QUEUE_FULL", "처리 대기열이 가득 찼습니다. 같은 키로 나중에 다시 제출하세요.",
                               retryable=True, retry_after_ms=5000)

        experience_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        generation_id = str(control["active_generation_id"])
        cur.execute("INSERT INTO experiences (id, raw_text, source_metadata, content_digest, "
                    "state, created_by) VALUES (%s, %s, %s, %s, 'retained', %s)",
                    (experience_id, inp["text"], Json(stored_metadata(inp)),
                     sha256_hex(inp["text"]), principal["id"]))
        hooks.fire("submit_after_experience_insert")
        cur.execute("INSERT INTO jobs (id, generation_id, experience_id, source_version, "
                    "input_digest, requested_by, authority_epoch, state) "
                    "VALUES (%s, %s, %s, 1, %s, %s, %s, 'queued')",
                    (job_id, generation_id, experience_id, job_input_digest(inp),
                     principal["id"], principal["auth_epoch"]))
        hooks.fire("submit_after_job_insert")
        cur.execute("INSERT INTO ingest_receipts (principal_id, key_digest, request_digest, "
                    "experience_id, initial_job_id) VALUES (%s, %s, %s, %s, %s)",
                    (principal["id"], key_digest, request_digest(cfg, inp), experience_id, job_id))
        cur.execute("UPDATE system_control SET ingest_revision = ingest_revision + 1 WHERE id = 1")
        return self._receipt_body(experience_id, job_id, generation_id, False), False

    @staticmethod
    def _receipt_body(experience_id, job_id, generation_id, replayed):
        return {"api_version": "v2", "status": "accepted", "experience_id": str(experience_id),
                "source_version": 1, "job_id": str(job_id), "generation_id": str(generation_id),
                "replayed": replayed, "searchability": "not_guaranteed"}

    # --- authentication (transport edge) ------------------------------
    def authenticate(self, token):
        """Valid, unrevoked credential? Used by the HTTP edge to answer 401 before any protocol handling."""
        return run_tx(self.config, lambda cur: self._authenticate_tx(cur, token))

    @staticmethod
    def _authenticate_tx(cur, token):
        _lock_control(cur, exclusive=False)
        return str(_authenticate(cur, token)["id"])

    # --- search -------------------------------------------------------
    def search(self, token, body):
        return run_tx(self.config, lambda cur: self._search_tx(cur, token, body))

    def _search_tx(self, cur, token, body):
        control = _lock_control(cur, exclusive=False)
        principal = _authenticate(cur, token)
        if not principal["can_read"]:
            raise not_found()
        if not isinstance(body, dict):
            raise invalid("request body must be a JSON object")
        cur.execute("SELECT id, engine_spec, index_profile FROM generations "
                    "WHERE id = %s AND state = 'servable'", (control["active_generation_id"],))
        generation = cur.fetchone()
        if generation is None:
            raise ServiceError(503, "NO_ACTIVE_GENERATION", "검색 가능한 활성 세대가 없습니다.", retryable=True,
                               retry_after_ms=5000)
        return search_mod.run(cur, generation, body)

    # --- reads --------------------------------------------------------
    def get_job(self, token, job_id):
        job_id = parse_uuid(job_id, "job_id") if _is_uuid(job_id) else None
        return run_tx(self.config, lambda cur: self._get_job_tx(cur, token, job_id))

    def _get_job_tx(self, cur, token, job_id):
        _lock_control(cur, exclusive=False)
        principal = _authenticate(cur, token)
        if job_id is None or not principal["can_read"]:
            raise not_found()
        cur.execute("SELECT j.id, j.generation_id, j.state, j.attempt, j.output_count, "
                    "j.error_code, j.not_before, e.state AS exp_state, c.active_generation_id "
                    "FROM jobs j JOIN experiences e ON e.id = j.experience_id "
                    "CROSS JOIN system_control c WHERE c.id = 1 AND j.id = %s", (job_id,))
        job = cur.fetchone()
        if job is None:
            raise not_found()
        cur.execute("SELECT now() AS now")
        now = cur.fetchone()["now"]
        visible, reason = _visibility(job)
        body = {"job_id": str(job["id"]), "generation_id": str(job["generation_id"]), "state": job["state"],
                "attempt": job["attempt"], "output_count": job["output_count"],
                "currently_visible": visible, "visibility_reason": reason,
                "error": {"code": job["error_code"]} if job["error_code"] else None,
                "retryable": False, "observed_at": iso(now)}
        if job["state"] == "queued":
            body["next_attempt_at"] = iso(job["not_before"])
        return body

    def get_experience(self, token, experience_id):
        exp_id = parse_uuid(experience_id, "experience_id") if _is_uuid(experience_id) else None
        return run_tx(self.config, lambda cur: self._get_experience_tx(cur, token, exp_id))

    def _get_experience_tx(self, cur, token, exp_id):
        _lock_control(cur, exclusive=False)
        principal = _authenticate(cur, token)
        if exp_id is None or not principal["can_read"]:
            raise not_found()
        cur.execute("SELECT id, source_version, raw_text, source_metadata, state, created_by, "
                    "received_at FROM experiences WHERE id = %s", (exp_id,))
        exp = cur.fetchone()
        if exp is None:
            raise not_found()
        if exp["state"] == "deleted":
            raise ServiceError(410, "EXPERIENCE_DELETED", "삭제된 Experience입니다.")
        meta = exp["source_metadata"]
        source = meta.get("source", {})
        sub = meta.get("submission", {})
        return {
            "api_version": "v2", "experience_id": str(exp["id"]), "source_version": exp["source_version"],
            "state": "retained",
            "content": {"media_type": "text/plain", "text": exp["raw_text"]},
            "source": {k: v for k, v in source.items() if k != "occurred_at"},
            "submission": sub,
            "created_by": str(exp["created_by"]), "received_at": iso(exp["received_at"]),
            "client_reported": {"occurred_at": source.get("occurred_at"),
                                "actor_reported": sub.get("actor_reported", "unknown"),
                                "actor_basis": "client_reported"},
            "content_trust": "untrusted_data",
        }


def _is_uuid(value):
    try:
        uuid.UUID(str(value))
        return isinstance(value, str)
    except ValueError:
        return False


def _visibility(job):
    if job["exp_state"] == "deleted":
        return False, "deleted_source"
    if job["state"] != "succeeded":
        return False, "not_succeeded"
    if job["generation_id"] != job["active_generation_id"]:
        return False, "inactive_generation"
    if not job["output_count"]:
        return False, "no_output"
    return True, "active_valid"
