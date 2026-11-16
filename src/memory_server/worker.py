"""Worker: claim -> compute (no DB transaction held) -> finalize (§5, §6.3, §6.4).

Control-lock discipline: claim and reaper take the control row FOR SHARE; finalize takes it FOR UPDATE
and re-validates the *current* attempt, lease, source, authority and generation before publishing.
Duplicate computation is possible; duplicate publication is not.
"""
import json
import logging
import signal
import threading
import time
import uuid
from collections import namedtuple
from dataclasses import dataclass
from typing import Any, Dict, Optional

import psycopg2
from psycopg2.extras import Json

from . import hooks
from .db import connect, run_tx
from .engine import MAX_ARTIFACTS, MAX_OUTPUT_BYTES, EngineError, Snapshot, engine_for_spec
from .errors import ServiceError
from .service import _lock_control
from .validation import canonical_json, job_input_digest

log = logging.getLogger("memory_server.worker")

Outcome = namedtuple("Outcome", "kind code job_id")
SUCCEEDED, CANCELLED, FAILED, RETRY, STALE = "succeeded", "cancelled", "failed", "retry_scheduled", "stale"


@dataclass
class Claim:
    job_id: str
    generation_id: str
    attempt: int
    lease_owner: str
    spec: Dict[str, Any]
    spec_digest: str
    input_digest: str
    snapshot: Snapshot


def _input_doc(raw_text, metadata):
    sub = dict(metadata.get("submission", {}))
    sub.pop("submission_profile", None)
    return {"text": raw_text, "source": metadata.get("source", {}), "submission": sub}


def _has_nul(obj):
    if isinstance(obj, str):
        return "\x00" in obj
    if isinstance(obj, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return any(_has_nul(v) for v in obj)
    return False


class Worker(object):
    def __init__(self, config, worker_id=None):
        self.config = config
        self.worker_id = worker_id or "worker-%s" % uuid.uuid4().hex[:8]

    # --- authority / validity re-check (used by claim and finalize) ------
    @staticmethod
    def _blocker(cur, job):
        """Reason the job may no longer run, or None. Caller holds the control lock."""
        cur.execute("SELECT state, raw_text, source_metadata FROM experiences WHERE id = %s", (job["experience_id"],))
        exp = cur.fetchone()
        if exp is None or exp["state"] != "retained":
            return "SOURCE_DELETED"
        doc = _input_doc(exp["raw_text"], exp["source_metadata"])
        if job_input_digest(doc) != job["input_digest"]:
            return "INPUT_CHANGED"
        cur.execute("SELECT auth_epoch, revoked_at, can_write FROM principals WHERE id = %s",
                    (job["requested_by"],))
        who = cur.fetchone()
        if (who is None or who["revoked_at"] is not None or who["auth_epoch"] != job["authority_epoch"]
                or not who["can_write"]):
            return "AUTHORITY_REVOKED"
        cur.execute("SELECT state FROM generations WHERE id = %s", (job["generation_id"],))
        gen = cur.fetchone()
        if gen is None or gen["state"] not in ("building", "servable"):
            return "GENERATION_UNAVAILABLE"
        return None

    # --- claim ----------------------------------------------------------
    def claim(self) -> Optional[Claim]:
        return run_tx(self.config, self._claim_tx)

    def _claim_tx(self, cur):
        cfg = self.config
        _lock_control(cur, exclusive=False)
        self._reap(cur)
        for _ in range(50):
            cur.execute("SELECT * FROM jobs WHERE state = 'queued' AND not_before <= now() "
                        "ORDER BY not_before, created_at, id FOR UPDATE SKIP LOCKED LIMIT 1")
            job = cur.fetchone()
            if job is None:
                return None
            reason = self._blocker(cur, job)
            if reason is not None:
                self._terminate(cur, job["id"], CANCELLED, reason)
                continue
            cur.execute("SELECT g.engine_spec, g.spec_digest, e.raw_text, e.source_metadata FROM generations g, "
                        "experiences e WHERE g.id = %s AND e.id = %s", (job["generation_id"], job["experience_id"]))
            row = cur.fetchone()
            cur.execute("UPDATE jobs SET state = 'running', attempt = attempt + 1, retry_count = retry_count + 1, "
                        "lease_owner = %s, lease_until = now() + make_interval(secs => %s), error_code = NULL "
                        "WHERE id = %s RETURNING attempt", (self.worker_id, cfg.lease_seconds, job["id"]))
            attempt = cur.fetchone()["attempt"]
            meta = row["source_metadata"]
            doc = _input_doc(row["raw_text"], meta)
            snap = Snapshot(str(job["experience_id"]), job["source_version"], doc["text"], doc["source"],
                            doc["submission"])
            return Claim(str(job["id"]), str(job["generation_id"]), attempt,
                         self.worker_id, row["engine_spec"], row["spec_digest"], job["input_digest"], snap)
        return None

    def _reap(self, cur):
        """Expired leases consume an attempt: back to queued with backoff, or failed at the limit (§6.3)."""
        cur.execute("UPDATE jobs SET state = 'failed', error_code = 'LEASE_EXPIRED', lease_owner = NULL, "
                    "lease_until = NULL, completed_at = now() "
                    "WHERE state = 'running' AND lease_until < now() AND retry_count >= %s",
                    (self.config.max_job_attempts,))
        cur.execute("UPDATE jobs SET state = 'queued', error_code = 'LEASE_EXPIRED', lease_owner = NULL, "
                    "lease_until = NULL, not_before = now() + make_interval(secs => "
                    "CASE WHEN retry_count <= 1 THEN 1 ELSE 5 END + random() * 0.5) "
                    "WHERE state = 'running' AND lease_until < now()")

    def _terminate(self, cur, job_id, state, code):
        cur.execute("UPDATE jobs SET state = %s, error_code = %s, lease_owner = NULL, lease_until = NULL, "
                    "completed_at = now() WHERE id = %s", (state, code, job_id))

    # --- heartbeat --------------------------------------------------------
    def heartbeat(self, claim) -> bool:
        """Conditional lease extension on the current attempt only; no control lock (§5.1)."""
        def fn(cur):
            cur.execute("UPDATE jobs SET lease_until = now() + make_interval(secs => %s) "
                        "WHERE id = %s AND state = 'running' AND attempt = %s AND lease_owner = %s "
                        "AND lease_until > now()", (self.config.lease_seconds, claim.job_id, claim.attempt,
                                                    claim.lease_owner))
            return cur.rowcount == 1
        return run_tx(self.config, fn)

    # --- compute ----------------------------------------------------------
    def compute(self, claim):
        """Run the engine on the immutable snapshot. Returns drafts or raises EngineError."""
        engine = engine_for_spec(claim.spec)
        if engine is None:
            raise EngineError("ENGINE_VERSION_UNAVAILABLE", "no engine code for this spec")
        return engine.process(claim.snapshot)

    # --- finalize ---------------------------------------------------------
    def finalize(self, claim, drafts=None, error=None) -> Outcome:
        """Publish drafts or record a safe failure for exactly this attempt."""
        hooks.fire("finalize_before_tx")
        return run_tx(self.config, lambda cur: self._finalize_tx(cur, claim, drafts, error))

    def _finalize_tx(self, cur, claim, drafts, error):
        cfg = self.config
        _lock_control(cur, exclusive=True)
        cur.execute("SELECT j.*, now() > j.lease_until AS lease_expired FROM jobs j WHERE j.id = %s FOR UPDATE",
                    (claim.job_id,))
        job = cur.fetchone()
        if (job is None or job["state"] != "running" or job["attempt"] != claim.attempt
                or job["lease_owner"] != claim.lease_owner or job["lease_expired"]):
            return Outcome(STALE, "STALE_ATTEMPT", claim.job_id)
        reason = self._blocker(cur, job)
        if reason is None:
            cur.execute("SELECT spec_digest FROM generations WHERE id = %s", (job["generation_id"],))
            if cur.fetchone()["spec_digest"] != claim.spec_digest or job["input_digest"] != claim.input_digest:
                reason = "INPUT_CHANGED"
        if reason is not None:
            self._terminate(cur, job["id"], CANCELLED, reason)
            return Outcome(CANCELLED, reason, claim.job_id)
        hooks.fire("finalize_after_revalidate")
        if error is None:
            problem = self._validate_drafts(claim, drafts)
            if problem is not None:
                error = EngineError(problem)
        if error is not None:
            return self._record_failure(cur, job, error)
        self._publish(cur, job, claim, drafts)
        hooks.fire("finalize_after_insert")
        return Outcome(SUCCEEDED, None, claim.job_id)

    def _record_failure(self, cur, job, error):
        code = error.code
        if error.retryable and job["retry_count"] < self.config.max_job_attempts:
            delay = 1 if job["retry_count"] <= 1 else 5
            cur.execute("UPDATE jobs SET state = 'queued', error_code = %s, lease_owner = NULL, lease_until = NULL, "
                        "not_before = now() + make_interval(secs => %s + random() * 0.5) WHERE id = %s",
                        (code, delay, job["id"]))
            return Outcome(RETRY, code, str(job["id"]))
        self._terminate(cur, job["id"], FAILED, code)
        return Outcome(FAILED, code, str(job["id"]))

    @staticmethod
    def _validate_drafts(claim, drafts):
        """Application-service validation of engine output; returns a safe error code or None."""
        if not isinstance(drafts, list) or not 1 <= len(drafts) <= MAX_ARTIFACTS:
            return "OUTPUT_LIMIT" if isinstance(drafts, list) and len(drafts) > MAX_ARTIFACTS else "OUTPUT_INVALID"
        keys, total = set(), 0
        text_bytes = len(claim.snapshot.text.encode("utf-8"))
        for d in drafts:
            if (not isinstance(d.local_key, str) or not d.local_key or d.local_key in keys
                    or not isinstance(d.display_text, str) or not d.display_text
                    or not isinstance(d.search_text, str) or not isinstance(d.payload, dict)
                    or not isinstance(d.payload_schema, str) or not d.payload_schema
                    or not d.source_refs):
                return "OUTPUT_INVALID"
            keys.add(d.local_key)
            try:
                blob = canonical_json(d.payload)
            except (TypeError, ValueError):
                return "OUTPUT_INVALID"
            if _has_nul(d.payload) or "\x00" in d.display_text or "\x00" in d.search_text:
                return "OUTPUT_INVALID"
            total += len(blob) + len(d.display_text) + len(d.search_text)
            for ref in d.source_refs:
                if (ref.get("experience_id") != claim.snapshot.experience_id
                        or ref.get("source_version") != claim.snapshot.source_version):
                    return "OUTPUT_PROVENANCE"  # an engine may only cite this job's snapshot
                if "start_byte" in ref or "end_byte" in ref:
                    s, e = ref.get("start_byte"), ref.get("end_byte")
                    if not (isinstance(s, int) and isinstance(e, int) and 0 <= s < e <= text_bytes):
                        return "OUTPUT_PROVENANCE"
        if total > MAX_OUTPUT_BYTES:
            return "OUTPUT_LIMIT"
        return None

    def _publish(self, cur, job, claim, drafts):
        for d in drafts:
            artifact_id = str(uuid.uuid4())
            cur.execute("INSERT INTO artifacts (id, generation_id, job_id, local_key, "
                        "payload_schema, payload, display_text, search_text, state) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'valid')",
                        (artifact_id, job["generation_id"], job["id"], d.local_key,
                         d.payload_schema, Json(d.payload), d.display_text, d.search_text))
            for ref in d.source_refs:
                spans = None
                if "start_byte" in ref:
                    spans = Json({"start_byte": ref["start_byte"], "end_byte": ref["end_byte"]})
                cur.execute("INSERT INTO artifact_sources (artifact_id, experience_id, source_version, "
                            "spans) VALUES (%s, %s, %s, %s)",
                            (artifact_id, ref["experience_id"], ref["source_version"], spans))
        cur.execute("UPDATE jobs SET state = 'succeeded', output_count = %s, error_code = NULL, lease_owner = NULL, "
                    "lease_until = NULL, completed_at = now() WHERE id = %s", (len(drafts), job["id"]))
        cur.execute("UPDATE generations SET result_revision = result_revision + 1 WHERE id = %s",
                    (job["generation_id"],))

    # --- driver -----------------------------------------------------------
    def process_one(self) -> Optional[Outcome]:
        claim = self.claim()
        if claim is None:
            return None
        hooks.fire("worker_after_claim")
        stop = threading.Event()
        beat = threading.Thread(target=self._beat, args=(claim, stop), daemon=True)
        beat.start()
        drafts, error = None, None
        try:
            drafts = self.compute(claim)
        except EngineError as exc:
            error = exc
        except Exception:
            log.exception("engine crashed job=%s", claim.job_id)
            error = EngineError("ENGINE_ERROR")
        finally:
            stop.set()
            beat.join()
        hooks.fire("worker_after_compute")
        outcome = self.finalize(claim, drafts, error)
        hooks.fire("worker_after_commit")
        return outcome

    def _beat(self, claim, stop):
        while not stop.wait(self.config.heartbeat_seconds):
            try:
                if not self.heartbeat(claim):
                    return
            except Exception:
                log.warning("heartbeat failed job=%s", claim.job_id)

    def run_until_idle(self, limit=10000):
        outcomes = []
        for _ in range(limit):
            outcome = self.process_one()
            if outcome is None:
                break
            outcomes.append(outcome)
        return outcomes

    def run_forever(self, poll_interval=1.0, stop=None):
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                if self.process_one() is None:
                    stop.wait(poll_interval)
            except ServiceError as exc:
                log.warning("worker iteration failed code=%s", exc.code)
                stop.wait(poll_interval)
            except psycopg2.Error:
                log.exception("worker database error")
                stop.wait(poll_interval)


def main():
    import argparse
    from .config import Config
    parser = argparse.ArgumentParser(description="Reference memory worker")
    parser.add_argument("--once", action="store_true", help="process queued jobs, then exit")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    worker = Worker(Config.from_env())
    if args.once:
        print(json.dumps([o._asdict() for o in worker.run_until_idle()]))
    else:
        stop = threading.Event()
        # SIGTERM/SIGINT finish the job in hand, then exit; an unfinished job is recovered by lease expiry.
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        worker.run_forever(args.poll_interval, stop)
        log.info("worker stopped")


if __name__ == "__main__":
    main()
