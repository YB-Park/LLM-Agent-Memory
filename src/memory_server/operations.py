"""Administrator operations on policy state: delete, revoke, manual retry (§6.4, §6.5, §9.3).

These are reachable only through the admin CLI (and tests); they are deliberately not registered on the
REST adapter or any MCP surface. Every operation runs in one transaction under the exclusive control lock,
so it is totally ordered against submit, claim and finalize.
"""
from psycopg2.extras import Json

from . import hooks
from .db import run_tx
from .errors import ServiceError, not_found
from .service import _grant, _lock_control
from .validation import parse_uuid

NON_TERMINAL = ("queued", "running")


def _audit(cur, event, actor_id, object_ids, details=None):
    cur.execute("INSERT INTO audit_events (event_type, actor_id, object_ids, safe_details) VALUES (%s, %s, %s, %s)",
                (event, actor_id, Json(object_ids), Json(details or {})))


def _fence_jobs(cur, where, params, code):
    """Cancel matching queued/running jobs and bump their attempt so a late finalize cannot match."""
    cur.execute("UPDATE jobs SET state = 'cancelled', error_code = %s, attempt = attempt + 1, lease_owner = NULL, "
                "lease_until = NULL, completed_at = now() WHERE state IN ('queued', 'running') AND (" + where + ") "
                "RETURNING id, generation_id", [code] + list(params))
    return cur.fetchall()


def fence_principal_jobs(cur, principal_id, code="AUTHORITY_REVOKED"):
    """Used by revoke and by grant reduction: the principal's open jobs lose their authority (§6.5)."""
    return _fence_jobs(cur, "requested_by = %s", (principal_id,), code)


def lock_policy(cur):
    _lock_control(cur, exclusive=True)


def bump_policy(cur):
    cur.execute("UPDATE system_control SET policy_revision = policy_revision + 1 WHERE id = 1")


# --- delete -------------------------------------------------------------
def delete_experience(config, experience_id, actor_id=None):
    exp_id = parse_uuid(experience_id, "experience_id")
    return run_tx(config, lambda cur: _delete_tx(cur, exp_id, actor_id))


def _delete_tx(cur, exp_id, actor_id):
    lock_policy(cur)
    cur.execute("SELECT id, namespace_id, state FROM experiences WHERE id = %s FOR UPDATE", (exp_id,))
    exp = cur.fetchone()
    if exp is None:
        raise not_found()
    ns_id = exp["namespace_id"]
    if exp["state"] == "deleted":
        return _delete_body(exp_id, 0, 0, already=True)
    hooks.fire("delete_after_lock")
    cur.execute("UPDATE experiences SET state = 'deleted', raw_text = NULL, source_metadata = NULL, "
                "content_digest = NULL, deleted_at = now() WHERE id = %s", (exp_id,))
    # Every artifact that cites this source, in any generation, is invalidated; payload and text are erased.
    cur.execute("UPDATE artifacts SET state = 'invalidated', payload = NULL, display_text = NULL, "
                "search_text = NULL WHERE state = 'valid' AND (id IN "
                "(SELECT artifact_id FROM artifact_sources WHERE experience_id = %s) "
                "OR job_id IN (SELECT id FROM jobs WHERE experience_id = %s)) RETURNING generation_id",
                (exp_id, exp_id))
    invalidated = cur.fetchall()
    fenced = _fence_jobs(cur, "experience_id = %s", (exp_id,), "SOURCE_DELETED")
    touched = {str(r["generation_id"]) for r in invalidated} | {str(r["generation_id"]) for r in fenced}
    if touched:
        cur.execute("UPDATE generations SET result_revision = result_revision + 1 WHERE id = ANY(%s::uuid[])",
                    (sorted(touched),))
    bump_policy(cur)
    _audit(cur, "experience_deleted", actor_id, {"experience_id": str(exp_id), "namespace_id": str(ns_id)},
           {"artifacts_invalidated": len(invalidated), "jobs_cancelled": len(fenced)})
    return _delete_body(exp_id, len(invalidated), len(fenced), already=False)


def _delete_body(exp_id, invalidated, cancelled, already):
    return {"experience_id": str(exp_id), "state": "deleted", "visibility": "hidden",
            "operational_payload": "redacted", "backup_erasure": "not_guaranteed",
            "artifacts_invalidated": invalidated, "jobs_cancelled": cancelled, "already_deleted": already}


# --- revoke ---------------------------------------------------------------
def revoke_principal(config, principal_id, actor_id=None):
    pid = parse_uuid(principal_id, "principal_id")
    return run_tx(config, lambda cur: _revoke_tx(cur, pid, actor_id))


def _revoke_tx(cur, pid, actor_id):
    lock_policy(cur)
    cur.execute("SELECT id, revoked_at FROM principals WHERE id = %s FOR UPDATE", (pid,))
    who = cur.fetchone()
    if who is None:
        raise not_found()
    if who["revoked_at"] is not None:
        return {"principal_id": str(pid), "revoked": True, "already_revoked": True, "jobs_cancelled": 0,
                "shared_data": "retained"}
    cur.execute("UPDATE principals SET revoked_at = now(), auth_epoch = auth_epoch + 1 WHERE id = %s", (pid,))
    fenced = fence_principal_jobs(cur, pid)
    bump_policy(cur)
    _audit(cur, "principal_revoked", actor_id, {"principal_id": str(pid)}, {"jobs_cancelled": len(fenced)})
    return {"principal_id": str(pid), "revoked": True, "already_revoked": False, "jobs_cancelled": len(fenced),
            "shared_data": "retained"}


# --- manual retry -----------------------------------------------------------
def retry_job(config, principal_id, job_id, expected_attempt):
    """A manage-capable principal re-queues a failed/cancelled job under its own authority (§6.5)."""
    pid = parse_uuid(principal_id, "principal_id")
    jid = parse_uuid(job_id, "job_id")
    if isinstance(expected_attempt, bool) or not isinstance(expected_attempt, int) or expected_attempt < 0:
        raise ServiceError(400, "VALIDATION_ERROR", "expected_attempt must be a non-negative integer")
    return run_tx(config, lambda cur: _retry_tx(cur, pid, jid, expected_attempt))


def _retry_tx(cur, pid, jid, expected_attempt):
    lock_policy(cur)
    cur.execute("SELECT id, auth_epoch, revoked_at FROM principals WHERE id = %s", (pid,))
    who = cur.fetchone()
    cur.execute("SELECT * FROM jobs WHERE id = %s FOR UPDATE", (jid,))
    job = cur.fetchone()
    if who is None or who["revoked_at"] is not None or job is None:
        raise not_found()
    grant = _grant(cur, pid, job["namespace_id"])
    if grant is None or not grant["can_manage"]:
        raise not_found()
    if job["attempt"] != expected_attempt:
        if (job["attempt"] > expected_attempt and job["requested_by"] == pid
                and job["state"] in ("queued", "running", "succeeded")):
            return _retry_body(job, already=True)  # an earlier identical command already took effect
        raise ServiceError(409, "STALE_ATTEMPT", "Job의 attempt가 요청 시점과 다릅니다. 상태를 다시 확인하세요.")
    if job["state"] not in ("failed", "cancelled"):
        raise ServiceError(409, "JOB_NOT_RETRYABLE", "failed 또는 cancelled 상태의 Job만 재시도할 수 있습니다.")
    cur.execute("SELECT state FROM experiences WHERE id = %s", (job["experience_id"],))
    if cur.fetchone()["state"] != "retained":
        raise ServiceError(409, "SOURCE_DELETED", "삭제된 원본의 Job은 재시도할 수 없습니다.")
    cur.execute("SELECT state FROM generations WHERE id = %s", (job["generation_id"],))
    if cur.fetchone()["state"] not in ("building", "servable"):
        raise ServiceError(409, "GENERATION_UNAVAILABLE", "종료된 세대의 Job은 재시도할 수 없습니다.")
    cur.execute("UPDATE jobs SET state = 'queued', requested_by = %s, authority_epoch = %s, retry_count = 0, "
                "error_code = NULL, not_before = now(), lease_owner = NULL, lease_until = NULL, "
                "completed_at = NULL WHERE id = %s RETURNING *", (pid, who["auth_epoch"], jid))
    new = cur.fetchone()
    _audit(cur, "job_retried", pid, {"job_id": str(jid), "namespace_id": str(job["namespace_id"])},
           {"previous_principal_id": str(job["requested_by"]), "new_principal_id": str(pid),
            "previous_state": job["state"], "previous_error_code": job["error_code"]})
    return _retry_body(new, already=False)


def _retry_body(job, already):
    return {"job_id": str(job["id"]), "state": job["state"], "attempt": job["attempt"],
            "requested_by": str(job["requested_by"]), "already_applied": already}
