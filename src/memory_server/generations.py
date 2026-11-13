"""Generation lifecycle: start a rebuild, inspect, compare, activate (incl. rollback), abort, resume (§8).

Admin-only (CLI); every state change runs under the exclusive control lock so it is totally ordered
against submit, claim, finalize, delete and revoke. The candidate is never activated automatically,
and the write gate is never opened by a restart: gate and pointers live in PostgreSQL.
"""
import time
import uuid

from psycopg2.extras import Json, execute_values

from . import hooks, search as search_mod
from .db import run_tx
from .engine import engine_for_spec, engine_label, reference_spec, spec_digest
from .errors import ServiceError, invalid, not_found
from .operations import _audit, lock_policy
from .service import _grant, _lock_control
from .validation import iso, job_input_digest, parse_uuid, sha256_hex
from .worker import _input_doc

MAX_FIXTURE_QUERIES = 20
COMPARE_TOP_K = 5
MAX_GENERATIONS_PER_NAMESPACE = 8
MAX_REPORTED_IDS = 20
COMPARE_ATTEMPTS = 3


def _conflict(code, message):
    return ServiceError(409, code, message)


def _manager(cur, principal_id, namespace_id):
    cur.execute("SELECT p.id, p.auth_epoch, p.revoked_at FROM principals p WHERE p.id = %s", (principal_id,))
    who = cur.fetchone()
    if who is None or who["revoked_at"] is not None:
        raise not_found()
    grant = _grant(cur, principal_id, namespace_id)
    if grant is None or not grant["can_manage"]:
        raise not_found()
    return who


def _lock_namespace(cur, namespace_id):
    cur.execute("SELECT * FROM namespaces WHERE id = %s FOR UPDATE", (namespace_id,))
    ns = cur.fetchone()
    if ns is None:
        raise not_found()
    return ns


def _generation(cur, generation_id, lock=False):
    cur.execute("SELECT * FROM generations WHERE id = %s" + (" FOR UPDATE" if lock else ""), (generation_id,))
    gen = cur.fetchone()
    if gen is None:
        raise not_found()
    return gen


def _lock_generations(cur, ids):
    cur.execute("SELECT id FROM generations WHERE id = ANY(%s::uuid[]) ORDER BY id FOR UPDATE",
                (sorted({str(i) for i in ids}),))


def _require_engine(spec):
    if engine_for_spec(spec) is None:
        raise _conflict("ENGINE_VERSION_UNAVAILABLE",
                        "이 빌드에는 해당 엔진 코드가 없습니다. 보존된 빌드를 복원하거나 새 프로세스를 배포하세요.")


def _retained_inputs(cur, namespace_id):
    """Current valid source set as (id, version, input digest, intent), ordered by id."""
    cur.execute("SELECT id, source_version, raw_text, source_metadata FROM experiences "
                "WHERE namespace_id = %s AND state = 'retained' ORDER BY id", (namespace_id,))
    out = []
    for r in cur.fetchall():
        doc = _input_doc(r["raw_text"], r["source_metadata"])
        out.append({"id": str(r["id"]), "version": r["source_version"], "digest": job_input_digest(doc),
                    "explicit": doc["submission"].get("intent") == "explicit_memory"})
    return out


def _manifest_digest(inputs):
    return sha256_hex("\n".join("%s:%d:%s" % (i["id"], i["version"], i["digest"]) for i in inputs))


def coverage(cur, generation_id, inputs):
    """Bidirectional check: the retained source set must equal this generation's successful job inputs.

    Jobs of sources that are no longer retained are excluded; every retained source needs a succeeded Job with
    matching version/digest and the Reference output shape (explicit: exactly 1, projection: >= 1) whose
    artifacts cite that source.
    """
    cur.execute(
        "SELECT j.experience_id, j.source_version, j.input_digest, j.state, j.output_count, "
        "(SELECT count(*) FROM artifacts a WHERE a.job_id = j.id AND a.state = 'valid') AS valid_artifacts, "
        "(SELECT count(*) FROM artifacts a WHERE a.job_id = j.id AND a.state = 'valid' AND NOT EXISTS ("
        "   SELECT 1 FROM artifact_sources s WHERE s.artifact_id = a.id AND s.experience_id = j.experience_id "
        "   AND s.source_version = j.source_version)) AS unsourced "
        "FROM jobs j WHERE j.generation_id = %s", (generation_id,))
    jobs = {(str(r["experience_id"]), r["source_version"]): r for r in cur.fetchall()}
    bad = {"missing": [], "pending": [], "failed": [], "stale_input": [], "bad_output": []}
    succeeded = 0
    for i in inputs:
        job = jobs.get((i["id"], i["version"]))
        if job is None:
            bad["missing"].append(i["id"])
        elif job["state"] in ("queued", "running"):
            bad["pending"].append(i["id"])
        elif job["state"] != "succeeded":
            bad["failed"].append(i["id"])
        elif job["input_digest"] != i["digest"]:
            bad["stale_input"].append(i["id"])
        else:
            n = job["valid_artifacts"]
            shape_ok = n == 1 if i["explicit"] else n >= 1
            if not shape_ok or n != job["output_count"] or job["unsourced"]:
                bad["bad_output"].append(i["id"])
            else:
                succeeded += 1
    counts = {k: len(v) for k, v in bad.items()}
    return {"ok": not any(counts.values()) and succeeded == len(inputs), "expected_sources": len(inputs),
            "covered_sources": succeeded, "problems": counts,
            "examples": {k: v[:MAX_REPORTED_IDS] for k, v in bad.items() if v}}


def _observe(cur, namespace_id, base, cand, inputs):
    """Everything an activation decision depends on. Caller holds the control lock."""
    cur.execute("SELECT policy_revision FROM system_control WHERE id = 1")
    policy = cur.fetchone()["policy_revision"]
    cur.execute("SELECT ingest_revision, active_generation_id, maintenance_generation_id, write_gate "
                "FROM namespaces WHERE id = %s", (namespace_id,))
    ns = cur.fetchone()
    cur.execute("SELECT id, result_revision, spec_digest, state FROM generations WHERE id = ANY(%s::uuid[])",
                ([str(base["id"]), str(cand["id"])],))
    gens = {str(r["id"]): r for r in cur.fetchall()}
    b, c = gens[str(base["id"])], gens[str(cand["id"])]
    return {"policy_revision": policy, "ingest_revision": ns["ingest_revision"],
            "active": str(ns["active_generation_id"]), "maintenance": str(ns["maintenance_generation_id"]),
            "write_gate": ns["write_gate"], "base_result_revision": b["result_revision"],
            "candidate_result_revision": c["result_revision"], "base_spec_digest": b["spec_digest"],
            "candidate_spec_digest": c["spec_digest"], "base_state": b["state"], "candidate_state": c["state"],
            "manifest_digest": _manifest_digest(inputs),
            "coverage": {"base": coverage(cur, base["id"], inputs), "candidate": coverage(cur, cand["id"], inputs)}}


# --- status ------------------------------------------------------------------------
def generation_status(config, generation_id):
    gid = parse_uuid(generation_id, "generation_id")

    def fn(cur):
        _lock_control(cur, exclusive=False)
        gen = _generation(cur, gid)
        return _status_body(cur, gen)
    return run_tx(config, fn)


def _status_body(cur, gen):
    cur.execute("SELECT * FROM namespaces WHERE id = %s", (gen["namespace_id"],))
    ns = cur.fetchone()
    cur.execute("SELECT state, count(*) AS n FROM jobs WHERE generation_id = %s GROUP BY state", (gen["id"],))
    jobs = {r["state"]: r["n"] for r in cur.fetchall()}
    inputs = _retained_inputs(cur, gen["namespace_id"])
    receipt = gen["comparison_receipt"]
    return {
        "generation_id": str(gen["id"]), "namespace_id": str(gen["namespace_id"]), "state": gen["state"],
        "engine": engine_label(gen["engine_spec"]), "index_profile": gen["index_profile"],
        "spec_digest": gen["spec_digest"],
        "base_generation_id": str(gen["base_generation_id"]) if gen["base_generation_id"] else None,
        "is_active": ns["active_generation_id"] == gen["id"],
        "is_maintenance_candidate": ns["maintenance_generation_id"] == gen["id"],
        "active_generation_id": str(ns["active_generation_id"]), "write_gate": ns["write_gate"],
        "snapshot_ingest_revision": gen["snapshot_ingest_revision"], "ingest_revision": ns["ingest_revision"],
        "retired_ingest_revision": gen["retired_ingest_revision"], "manifest_digest": gen["manifest_digest"],
        "jobs": jobs, "coverage": coverage(cur, gen["id"], inputs),
        "comparison_receipt_id": receipt["receipt_id"] if receipt else None,
    }


# --- start -----------------------------------------------------------------------
def start_generation(config, principal_id, namespace_id, operation_id, expected_active, engine_version,
                     index_profile=None):
    """Create a candidate generation over the exact current source set and pause intake (§8.2 step 1)."""
    pid = parse_uuid(principal_id, "principal_id")
    nid = parse_uuid(namespace_id, "namespace_id")
    op = parse_uuid(operation_id, "operation_id")
    expected = parse_uuid(expected_active, "expected_active")
    if not isinstance(engine_version, str) or not engine_version:
        raise invalid("engine_version must be a non-empty string")
    spec = reference_spec(engine_version)
    _require_engine(spec)
    return run_tx(config, lambda cur: _start_tx(cur, pid, nid, op, expected, spec, index_profile))


def _start_tx(cur, pid, nid, op, expected, spec, index_profile):
    lock_policy(cur)
    ns = _lock_namespace(cur, nid)
    who = _manager(cur, pid, nid)
    cur.execute("SELECT * FROM generations WHERE id = %s", (op,))
    existing = cur.fetchone()
    if existing is not None:
        same = (str(existing["namespace_id"]) == nid and existing["spec_digest"] == spec_digest(spec)
                and str(existing["base_generation_id"]) == expected
                and (index_profile is None or existing["index_profile"] == index_profile))
        if not same:
            raise _conflict("OPERATION_CONFLICT", "같은 operation ID로 다른 요청이 이미 처리되었습니다.")
        body = _status_body(cur, existing)
        body["already_exists"] = True
        return body
    if str(ns["active_generation_id"]) != expected:
        raise _conflict("STALE_ACTIVE", "활성 세대가 기대값과 다릅니다. 상태를 다시 확인하세요.")
    if ns["write_gate"] != "open" or ns["maintenance_generation_id"] is not None:
        raise _conflict("REBUILD_IN_PROGRESS", "이미 진행 중인 유지보수가 있습니다. 완료·중단 후 다시 시도하세요.")
    cur.execute("SELECT count(*) AS n FROM generations WHERE namespace_id = %s", (nid,))
    if cur.fetchone()["n"] >= MAX_GENERATIONS_PER_NAMESPACE:
        raise _conflict("GENERATION_LIMIT", "세대 수 한도에 도달했습니다. Phase 0에는 자동 정리가 없습니다.")
    base = _generation(cur, expected)
    if base["state"] != "servable":
        raise _conflict("NO_ACTIVE_GENERATION", "활성 세대가 검색 가능한 상태가 아닙니다.")
    profile = index_profile or base["index_profile"]
    inputs = _retained_inputs(cur, nid)
    cur.execute("INSERT INTO generations (id, namespace_id, base_generation_id, engine_spec, spec_digest, "
                "index_profile, state, snapshot_ingest_revision, manifest_digest) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'building', %s, %s)",
                (op, nid, expected, Json(spec), spec_digest(spec), profile, ns["ingest_revision"],
                 _manifest_digest(inputs)))
    cur.execute("UPDATE namespaces SET write_gate = 'paused', maintenance_generation_id = %s WHERE id = %s",
                (op, nid))
    hooks.fire("generation_start_before_jobs")
    if inputs:
        # Job authority is the manage principal that started the rebuild, not each source's creator, so a
        # revoked creator does not make its sources impossible to rebuild.
        execute_values(cur, "INSERT INTO jobs (id, namespace_id, generation_id, experience_id, source_version, "
                       "input_digest, requested_by, authority_epoch, state) VALUES %s",
                       [(str(uuid.uuid4()), nid, op, i["id"], i["version"], i["digest"], pid, who["auth_epoch"],
                         "queued") for i in inputs],
                       template="(%s, %s, %s, %s, %s, %s, %s, %s, %s)")
    hooks.fire("generation_start_before_commit")
    _audit(cur, "generation_started", pid, {"generation_id": str(op), "namespace_id": nid,
                                            "base_generation_id": expected},
           {"engine": engine_label(spec), "sources": len(inputs), "manifest_digest": _manifest_digest(inputs)})
    body = _status_body(cur, _generation(cur, op))
    body["already_exists"] = False
    return body


# --- compare -----------------------------------------------------------------------
def _validate_fixture(queries):
    if (not isinstance(queries, list) or not 1 <= len(queries) <= MAX_FIXTURE_QUERIES
            or not all(isinstance(q, str) for q in queries)):
        raise invalid("queries must be a list of 1-%d strings" % MAX_FIXTURE_QUERIES)
    return queries


def _fixture_hash(queries):
    return sha256_hex("\n".join(sha256_hex(q) for q in queries))


def _run_generation_queries(cur, namespace_id, gen, queries):
    out = []
    for q in queries:
        started = time.perf_counter()
        res = search_mod.run(cur, namespace_id, gen, {"namespace_id": str(namespace_id), "query": q,
                                                       "limit": COMPARE_TOP_K})
        latency = round((time.perf_counter() - started) * 1000, 2)
        ids = []
        for hit in res["results"]:
            for src in hit["sources"]:
                if src["experience_id"] not in ids:
                    ids.append(src["experience_id"])
        out.append({"experience_ids": ids, "artifact_count": len(res["results"]), "latency_ms": latency,
                    "effective_keywords": res["effective_keywords"]})
    return out


def _context(cur, pid, gid, exclusive):
    cand = _generation(cur, gid)
    nid = str(cand["namespace_id"])
    if exclusive:
        ns = _lock_namespace(cur, nid)
    else:
        cur.execute("SELECT * FROM namespaces WHERE id = %s", (nid,))
        ns = cur.fetchone()
    _manager(cur, pid, nid)
    return cand, nid, ns


def _require_candidate(ns, cand):
    if ns["active_generation_id"] == cand["id"]:
        raise _conflict("ALREADY_ACTIVE", "이미 활성 세대입니다.")
    if ns["write_gate"] != "paused" or ns["maintenance_generation_id"] != cand["id"]:
        raise _conflict("NOT_IN_MAINTENANCE", "이 세대는 현재 유지보수 후보가 아닙니다.")
    if cand["state"] not in ("building", "retired"):
        raise _conflict("NOT_A_CANDIDATE", "후보로 쓸 수 없는 세대 상태입니다.")


def compare_generation(config, principal_id, generation_id, queries):
    pid = parse_uuid(principal_id, "principal_id")
    gid = parse_uuid(generation_id, "generation_id")
    queries = _validate_fixture(queries)
    for _ in range(COMPARE_ATTEMPTS):
        try:
            observed, report = run_tx(config, lambda cur: _compare_read(cur, pid, gid, queries))
            hooks.fire("compare_between_phases")
            return run_tx(config, lambda cur: _compare_store(cur, pid, gid, queries, observed, report))
        except _Stale:
            continue
    raise ServiceError(409, "COMPARISON_STALE", "비교 중 상태가 계속 바뀌었습니다. 다시 실행하세요.", retryable=True)


class _Stale(Exception):
    pass


def _compare_read(cur, pid, gid, queries):
    _lock_control(cur, exclusive=False)
    cand, nid, ns = _context(cur, pid, gid, exclusive=False)
    _require_candidate(ns, cand)
    base = _generation(cur, ns["active_generation_id"])
    _require_engine(base["engine_spec"])
    _require_engine(cand["engine_spec"])
    inputs = _retained_inputs(cur, nid)
    observed = _observe(cur, nid, base, cand, inputs)
    base_hits = _run_generation_queries(cur, nid, base, queries)
    cand_hits = _run_generation_queries(cur, nid, cand, queries)
    rows = []
    for idx, (b, c) in enumerate(zip(base_hits, cand_hits)):
        rows.append({"query_index": idx, "base": b, "candidate": c,
                     "only_in_base": [i for i in b["experience_ids"] if i not in c["experience_ids"]],
                     "only_in_candidate": [i for i in c["experience_ids"] if i not in b["experience_ids"]],
                     "in_both": [i for i in b["experience_ids"] if i in c["experience_ids"]]})
    report = {"namespace_id": nid, "base_generation_id": str(base["id"]), "candidate_generation_id": str(cand["id"]),
              "base_engine": engine_label(base["engine_spec"]), "candidate_engine": engine_label(cand["engine_spec"]),
              "coverage": observed["coverage"], "fixture": {"hash": _fixture_hash(queries), "queries": len(queries)},
              "queries": rows, "top_k": COMPARE_TOP_K, "external_calls": 0, "quality_verdict": None,
              "revisions": {k: observed[k] for k in ("policy_revision", "ingest_revision", "base_result_revision",
                                                     "candidate_result_revision")}}
    return observed, report


def _compare_store(cur, pid, gid, queries, observed, report):
    lock_policy(cur)
    cand, nid, ns = _context(cur, pid, gid, exclusive=True)
    _require_candidate(ns, cand)
    base = _generation(cur, ns["active_generation_id"])
    _lock_generations(cur, [base["id"], cand["id"]])
    if _observe(cur, nid, base, cand, _retained_inputs(cur, nid)) != observed:
        raise _Stale()
    covered = observed["coverage"]["base"]["ok"] and observed["coverage"]["candidate"]["ok"]
    if not covered:
        report["status"] = "incomplete"
        report["receipt_id"] = None
        return report
    receipt = {"receipt_id": str(uuid.uuid4()), "namespace_id": nid, "base_generation_id": str(base["id"]),
               "candidate_generation_id": str(cand["id"]), "policy_revision": observed["policy_revision"],
               "ingest_revision": observed["ingest_revision"],
               "base_result_revision": observed["base_result_revision"],
               "candidate_result_revision": observed["candidate_result_revision"],
               "base_spec_digest": observed["base_spec_digest"],
               "candidate_spec_digest": observed["candidate_spec_digest"],
               "manifest_digest": observed["manifest_digest"], "fixture_hash": report["fixture"]["hash"],
               "query_count": len(queries), "sources": observed["coverage"]["candidate"]["expected_sources"],
               "external_calls": 0}
    cur.execute("UPDATE generations SET comparison_receipt = %s WHERE id = %s", (Json(receipt), cand["id"]))
    _audit(cur, "generation_compared", pid, {"generation_id": str(cand["id"]), "namespace_id": nid,
                                             "receipt_id": receipt["receipt_id"]},
           {"fixture_hash": receipt["fixture_hash"], "query_count": len(queries), "sources": receipt["sources"]})
    report["status"] = "ready"
    report["receipt_id"] = receipt["receipt_id"]
    return report


# --- activate (also conditional rollback) ---------------------------------------
def activate_generation(config, principal_id, generation_id, expected_active, receipt_id):
    pid = parse_uuid(principal_id, "principal_id")
    gid = parse_uuid(generation_id, "generation_id")
    expected = parse_uuid(expected_active, "expected_active")
    receipt = parse_uuid(receipt_id, "receipt_id")
    return run_tx(config, lambda cur: _activate_tx(cur, pid, gid, expected, receipt))


def _activate_tx(cur, pid, gid, expected, receipt_id):
    lock_policy(cur)
    cand, nid, ns = _context(cur, pid, gid, exclusive=True)
    stored = cand["comparison_receipt"] or {}
    if (ns["active_generation_id"] == cand["id"] and stored.get("receipt_id") == receipt_id
            and stored.get("base_generation_id") == expected):
        return _activation_body(cand["id"], expected, already=True)
    if str(ns["active_generation_id"]) != expected:
        raise _conflict("STALE_ACTIVE", "활성 세대가 기대값과 다릅니다. 다시 비교하세요.")
    _require_candidate(ns, cand)
    base = _generation(cur, expected)
    if cand["state"] == "retired" and cand["retired_ingest_revision"] != ns["ingest_revision"]:
        raise _conflict("ROLLBACK_REBUILD_REQUIRED",
                        "이 세대가 퇴역한 뒤 새 원본이 접수되어 포인터 롤백은 원본을 누락시킵니다. "
                        "이전 엔진 버전으로 새 세대를 재구축하세요.")
    _lock_generations(cur, [base["id"], cand["id"]])
    _require_engine(base["engine_spec"])
    _require_engine(cand["engine_spec"])
    if stored.get("receipt_id") != receipt_id:
        raise _conflict("RECEIPT_INVALID", "비교 receipt가 없거나 최신이 아닙니다. 다시 비교하세요.")
    observed = _observe(cur, nid, base, cand, _retained_inputs(cur, nid))
    expect = {"policy_revision": stored["policy_revision"], "ingest_revision": stored["ingest_revision"],
              "base_result_revision": stored["base_result_revision"],
              "candidate_result_revision": stored["candidate_result_revision"],
              "base_spec_digest": stored["base_spec_digest"],
              "candidate_spec_digest": stored["candidate_spec_digest"],
              "manifest_digest": stored["manifest_digest"]}
    if stored["base_generation_id"] != expected or any(observed[k] != v for k, v in expect.items()):
        raise _conflict("COMPARISON_STALE", "비교 이후 정책·원본·결과가 바뀌었습니다. 다시 비교하세요.")
    if not (observed["coverage"]["base"]["ok"] and observed["coverage"]["candidate"]["ok"]):
        raise _conflict("COVERAGE_INCOMPLETE", "현재 유효 원본 coverage가 충족되지 않았습니다.")
    hooks.fire("activate_before_commit")
    cur.execute("UPDATE generations SET state = 'retired', retired_ingest_revision = %s WHERE id = %s",
                (ns["ingest_revision"], base["id"]))
    cur.execute("UPDATE generations SET state = 'servable', retired_ingest_revision = NULL WHERE id = %s",
                (cand["id"],))
    cur.execute("UPDATE namespaces SET active_generation_id = %s WHERE id = %s", (cand["id"], nid))
    _audit(cur, "generation_activated", pid, {"generation_id": str(cand["id"]), "namespace_id": nid,
                                              "previous_generation_id": str(base["id"]),
                                              "receipt_id": receipt_id},
           {"rollback": cand["state"] == "retired", "ingest_revision": ns["ingest_revision"]})
    return _activation_body(cand["id"], base["id"], already=False)


def _activation_body(active, previous, already):
    return {"active_generation_id": str(active), "previous_active_generation_id": str(previous),
            "write_gate": "paused", "already_applied": already}


# --- prepare rollback ---------------------------------------------------------------
def prepare_rollback(config, principal_id, namespace_id, target_generation_id, expected_active):
    """Make a retired generation the maintenance target, if no source was accepted since it retired (§8.3)."""
    pid = parse_uuid(principal_id, "principal_id")
    nid = parse_uuid(namespace_id, "namespace_id")
    target = parse_uuid(target_generation_id, "generation_id")
    expected = parse_uuid(expected_active, "expected_active")
    return run_tx(config, lambda cur: _prepare_rollback_tx(cur, pid, nid, target, expected))


def _prepare_rollback_tx(cur, pid, nid, target, expected):
    lock_policy(cur)
    ns = _lock_namespace(cur, nid)
    _manager(cur, pid, nid)
    gen = _generation(cur, target)
    if str(gen["namespace_id"]) != nid or gen["state"] != "retired":
        raise _conflict("NOT_ROLLBACK_TARGET", "퇴역한 세대만 롤백 대상이 될 수 있습니다.")
    if str(ns["active_generation_id"]) != expected:
        raise _conflict("STALE_ACTIVE", "활성 세대가 기대값과 다릅니다.")
    if ns["maintenance_generation_id"] == gen["id"] and ns["write_gate"] == "paused":
        return _status_body(cur, gen)
    if ns["write_gate"] == "paused" and ns["maintenance_generation_id"] != ns["active_generation_id"]:
        raise _conflict("REBUILD_IN_PROGRESS", "이미 진행 중인 유지보수가 있습니다.")
    if gen["retired_ingest_revision"] != ns["ingest_revision"]:
        raise _conflict("ROLLBACK_REBUILD_REQUIRED",
                        "이 세대가 퇴역한 뒤 새 원본이 접수되어 포인터 롤백은 원본을 누락시킵니다. "
                        "이전 엔진 버전(%s)으로 새 세대를 재구축하세요." % engine_label(gen["engine_spec"]))
    _require_engine(gen["engine_spec"])
    cur.execute("UPDATE namespaces SET write_gate = 'paused', maintenance_generation_id = %s WHERE id = %s",
                (gen["id"], nid))
    _audit(cur, "rollback_prepared", pid, {"generation_id": str(gen["id"]), "namespace_id": nid})
    return _status_body(cur, gen)


# --- abort / resume -----------------------------------------------------------------
def abort_generation(config, principal_id, generation_id, expected_active):
    pid = parse_uuid(principal_id, "principal_id")
    gid = parse_uuid(generation_id, "generation_id")
    expected = parse_uuid(expected_active, "expected_active")
    return run_tx(config, lambda cur: _abort_tx(cur, pid, gid, expected))


def _abort_tx(cur, pid, gid, expected):
    lock_policy(cur)
    cand, nid, ns = _context(cur, pid, gid, exclusive=True)
    if str(ns["active_generation_id"]) != expected:
        raise _conflict("STALE_ACTIVE", "활성 세대가 기대값과 다릅니다.")
    if cand["id"] == ns["active_generation_id"]:
        raise _conflict("ALREADY_ACTIVE", "활성 세대는 중단할 수 없습니다.")
    if cand["state"] == "aborted" and ns["write_gate"] == "open":
        return {"generation_id": str(gid), "state": "aborted", "active_generation_id": expected,
                "write_gate": "open", "jobs_cancelled": 0, "already_applied": True}
    if ns["maintenance_generation_id"] != cand["id"] or cand["state"] not in ("building", "retired"):
        raise _conflict("NOT_IN_MAINTENANCE", "이 세대는 현재 유지보수 후보가 아닙니다.")
    cancelled = 0
    if cand["state"] == "building":
        cur.execute("UPDATE jobs SET state = 'cancelled', error_code = 'GENERATION_ABORTED', attempt = attempt + 1, "
                    "lease_owner = NULL, lease_until = NULL, completed_at = now() "
                    "WHERE generation_id = %s AND state IN ('queued', 'running')", (cand["id"],))
        cancelled = cur.rowcount
        cur.execute("UPDATE generations SET state = 'aborted' WHERE id = %s", (cand["id"],))
    cur.execute("UPDATE namespaces SET write_gate = 'open', maintenance_generation_id = NULL WHERE id = %s", (nid,))
    _audit(cur, "generation_aborted", pid, {"generation_id": str(gid), "namespace_id": nid},
           {"jobs_cancelled": cancelled, "was": cand["state"]})
    return {"generation_id": str(gid), "state": "aborted" if cand["state"] == "building" else cand["state"],
            "active_generation_id": expected, "write_gate": "open", "jobs_cancelled": cancelled,
            "already_applied": False}


def resume_namespace(config, principal_id, namespace_id, expected_active, generation_id):
    """Reopen intake on the active generation after an activation or rollback (§8.2 step 7)."""
    pid = parse_uuid(principal_id, "principal_id")
    nid = parse_uuid(namespace_id, "namespace_id")
    expected = parse_uuid(expected_active, "expected_active")
    gid = parse_uuid(generation_id, "generation_id")
    return run_tx(config, lambda cur: _resume_tx(cur, pid, nid, expected, gid))


def _resume_tx(cur, pid, nid, expected, gid):
    lock_policy(cur)
    ns = _lock_namespace(cur, nid)
    _manager(cur, pid, nid)
    if str(ns["active_generation_id"]) != expected or expected != gid:
        raise _conflict("STALE_ACTIVE", "활성 세대가 기대값과 다릅니다.")
    if ns["write_gate"] == "open" and ns["maintenance_generation_id"] is None:
        return {"active_generation_id": expected, "write_gate": "open", "already_applied": True}
    if ns["maintenance_generation_id"] != ns["active_generation_id"]:
        raise _conflict("CANDIDATE_PENDING", "처리 중인 후보가 있습니다. 먼저 activate 또는 abort 하세요.")
    cur.execute("UPDATE namespaces SET write_gate = 'open', maintenance_generation_id = NULL WHERE id = %s", (nid,))
    _audit(cur, "namespace_resumed", pid, {"namespace_id": nid, "generation_id": expected})
    return {"active_generation_id": expected, "write_gate": "open", "already_applied": False}
