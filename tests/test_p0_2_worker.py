"""P0-2 Worker: claim, lease, fencing, retries, atomic publication (design T3, T4, T14, T17, T20 subset)."""
import threading
import time

import pytest

from memory_server import hooks
from memory_server.engine import ArtifactDraft, EngineError
from memory_server.worker import FAILED, RETRY, STALE, SUCCEEDED, Worker

MULTI = "첫 문단: 배포 DB는 PostgreSQL.\r\n\r\n둘째 문단 with English.\n\n\n셋째 문단 foo_bar\n둘째 줄"


def submit_plain(world, text="문단 하나", key=None):
    return world.submit(world.a["token"], key or "k-" + text[:8] + str(time.time()), world.body(text))[0]


def test_worker_publishes_explicit_memory_verbatim(world):
    text = "  배포 DB는 PostgreSQL을 사용한다.\r\n\r\n두 번째 문단도 그대로.  \n"
    r = world.explicit(world.a["token"], "e1", text, title="배포 DB 선택", hints=["데이터베이스", "database"])
    outcomes = world.drain()
    assert [o.kind for o in outcomes] == [SUCCEEDED]
    job = world.job(r["job_id"])
    assert job["state"] == "succeeded" and job["output_count"] == 1 and job["attempt"] == 1
    [art] = world.artifacts()
    assert art["local_key"] == "0" and art["display_text"] == text  # no rewrite, no split, no strip
    p = art["payload"]
    assert p["representation"] == "explicit_memory" and p["title"] == "배포 DB 선택"
    assert p["hints"] == ["데이터베이스", "database"]
    assert p["verification"] == "not_verified" and p["evidence_status"] == "no_source_linked"
    assert p["actor_reported"] == "agent" and p["actor_basis"] == "client_reported"
    src = world.query("SELECT * FROM artifact_sources WHERE artifact_id = %s", (art["id"],))
    assert len(src) == 1 and str(src[0]["experience_id"]) == r["experience_id"] and src[0]["spans"] is None


def test_reference_1_projects_whole_experience(world):
    r = submit_plain(world, MULTI, "p1")
    world.drain()
    [art] = world.artifacts()
    assert art["display_text"] == MULTI and art["payload"]["representation"] == "source_projection_whole"
    assert art["payload"]["verification"] == "not_assessed" and art["payload"]["evidence_status"] == "not_applicable"
    [src] = world.query("SELECT spans FROM artifact_sources WHERE artifact_id = %s", (art["id"],))
    assert src["spans"] == {"start_byte": 0, "end_byte": len(MULTI.encode("utf-8"))}
    assert str(world.job(r["job_id"])["generation_id"]) == world.personal["generation_id"]


def test_reference_2_projects_paragraphs_with_byte_spans(database_url, config):
    from memory_server import admin
    from conftest import World
    w = World(config)
    ns = admin.create_namespace(w.dsn, "ref2-ns", engine_version="2")
    admin.grant(w.dsn, w.a["principal_id"], ns["namespace_id"], write=True)
    r = w.svc.submit(w.a["token"], w.body(MULTI, ns["namespace_id"]), "p2")[0]
    w.drain()
    arts = w.artifacts(ns["namespace_id"])
    assert [a["local_key"] for a in arts] == ["0", "1", "2"]
    raw = MULTI.encode("utf-8")
    for a in arts:
        [src] = w.query("SELECT spans FROM artifact_sources WHERE artifact_id = %s", (a["id"],))
        assert raw[src["spans"]["start_byte"]:src["spans"]["end_byte"]].decode("utf-8") == a["display_text"]
        assert a["payload"]["representation"] == "source_projection_paragraph"
    assert arts[2]["display_text"] == "셋째 문단 foo_bar\n둘째 줄"
    assert str(w.job(r["job_id"])["generation_id"]) == ns["generation_id"]


def test_reference_2_keeps_explicit_memory_as_single_artifact(database_url, config):
    from memory_server import admin
    from conftest import World
    w = World(config)
    ns = admin.create_namespace(w.dsn, "ref2-explicit", engine_version="2")
    admin.grant(w.dsn, w.a["principal_id"], ns["namespace_id"], write=True)
    w.explicit(w.a["token"], "x1", MULTI, namespace_id=ns["namespace_id"])
    w.drain()
    [art] = w.artifacts(ns["namespace_id"])
    assert art["display_text"] == MULTI and art["payload"]["representation"] == "explicit_memory"


def test_reference_2_output_limit_fails_without_partial_save(database_url, config):
    from memory_server import admin
    from conftest import World
    w = World(config)
    ns = admin.create_namespace(w.dsn, "ref2-limit", engine_version="2")
    admin.grant(w.dsn, w.a["principal_id"], ns["namespace_id"], write=True)
    text = "\n\n".join("p%d" % i for i in range(257))
    r = w.svc.submit(w.a["token"], w.body(text, ns["namespace_id"]), "lim")[0]
    [o] = w.drain()
    assert (o.kind, o.code) == (FAILED, "OUTPUT_LIMIT")
    assert w.artifacts(ns["namespace_id"]) == [] and w.job(r["job_id"])["state"] == "failed"


# --- T3: claim, lease, stale attempt ------------------------------------

def test_t3_claim_is_exclusive_and_increments_attempt(world):
    r = submit_plain(world, "x1", "c1")
    a, b = world.worker("A"), world.worker("B")
    claim = a.claim()
    assert claim is not None and claim.attempt == 1 and claim.job_id == r["job_id"]
    assert b.claim() is None  # running with a live lease: nobody else may take it
    job = world.job(r["job_id"])
    assert job["state"] == "running" and job["lease_owner"] == "A"


def test_t3_worker_dies_after_claim_lease_expiry_requeues_and_consumes_attempt(world):
    r = submit_plain(world, "x2", "c2")
    a, b = world.worker("A"), world.worker("B")
    assert a.claim().attempt == 1  # A "crashes": never finalizes
    world.expire_lease(r["job_id"])
    assert b.claim() is None  # reaped into backoff, not claimable at once
    job = world.job(r["job_id"])
    assert job["state"] == "queued" and job["error_code"] == "LEASE_EXPIRED" and job["lease_owner"] is None
    world.make_due(r["job_id"])
    second = b.claim()
    assert second.attempt == 2
    assert b.finalize(second, b.compute(second)).kind == SUCCEEDED
    assert len(world.artifacts()) == 1


def test_t3_lease_expiry_exhausts_attempts_then_failed(world):
    r = submit_plain(world, "x3", "c3")
    w = world.worker("A")
    for expected in (1, 2, 3):
        claim = w.claim()
        assert claim.attempt == expected
        world.expire_lease(r["job_id"])
        w.claim()  # reaper runs in the polling loop
        world.make_due(r["job_id"])
    job = world.job(r["job_id"])
    assert job["state"] == "failed" and job["error_code"] == "LEASE_EXPIRED"
    assert w.claim() is None and world.artifacts() == []


def test_t3_late_finalize_cannot_overwrite_new_attempt(world):
    r = submit_plain(world, "late", "c4")
    a, b = world.worker("A"), world.worker("B")
    old = a.claim()
    old_drafts = a.compute(old)
    world.expire_lease(r["job_id"])
    # stale while still 'running' with an expired lease and not yet reaped
    assert a.finalize(old, old_drafts).kind == STALE
    b.claim()  # reaps (backoff)
    world.make_due(r["job_id"])
    new = b.claim()
    assert new.attempt == 2
    assert a.finalize(old, old_drafts).kind == STALE  # new attempt running: A is fenced
    assert world.artifacts() == []
    assert b.finalize(new, b.compute(new)).kind == SUCCEEDED
    assert a.finalize(old, old_drafts).kind == STALE  # after B's success
    assert len(world.artifacts()) == 1
    assert world.job(r["job_id"])["attempt"] == 2 and world.job(r["job_id"])["output_count"] == 1


def test_t3_heartbeat_extends_only_current_attempt(world):
    r = submit_plain(world, "hb", "c5")
    a, b = world.worker("A"), world.worker("B")
    claim = a.claim()
    before = world.job(r["job_id"])["lease_until"]
    time.sleep(0.05)
    assert a.heartbeat(claim) is True
    assert world.job(r["job_id"])["lease_until"] > before
    world.expire_lease(r["job_id"])
    assert a.heartbeat(claim) is False  # expired leases cannot be revived
    b.claim()
    world.make_due(r["job_id"])
    assert b.claim().attempt == 2 and a.heartbeat(claim) is False


# --- T4: atomic publication, crash windows -----------------------------

def test_t4_crash_before_commit_publishes_nothing_and_retry_succeeds(world):
    r = world.explicit(world.a["token"], "t4", "원자적 공개 시험")
    w = world.worker("A")

    def boom():
        raise RuntimeError("process killed before commit")
    hooks.register("finalize_after_insert", boom)
    with pytest.raises(RuntimeError):
        w.process_one()
    hooks.clear()
    assert world.artifacts() == [] and world.query("SELECT count(*) AS n FROM artifact_sources WHERE namespace_id = %s", (world.ns,))[0]["n"] == 0
    job = world.job(r["job_id"])
    assert job["state"] == "running" and job["output_count"] is None  # not succeeded, not visible
    assert world.search(world.a["token"], "원자적 공개")["results"] == []
    world.expire_lease(r["job_id"])
    w.claim()
    world.make_due(r["job_id"])
    assert [o.kind for o in w.run_until_idle()] == [SUCCEEDED]
    assert len(world.artifacts()) == 1
    assert len(world.search(world.a["token"], "원자적 공개")["results"]) == 1


def test_t4_crash_after_commit_is_not_reexecuted(world):
    r = world.explicit(world.a["token"], "t4b", "커밋 후 응답 유실")
    w = world.worker("A")
    hooks.register("worker_after_commit", lambda: (_ for _ in ()).throw(RuntimeError("killed after commit")))
    with pytest.raises(RuntimeError):
        w.process_one()
    hooks.clear()
    assert world.job(r["job_id"])["state"] == "succeeded"
    assert w.process_one() is None and len(world.artifacts()) == 1


def test_t4_parallel_workers_publish_each_job_once(world):
    ids = [world.explicit(world.a["token"], "par%d" % i, "병렬 처리 항목 %d" % i)["job_id"] for i in range(24)]
    outcomes, errors = [], []

    def run(name):
        try:
            outcomes.extend(Worker(world.config, name).run_until_idle())
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=("w%d" % i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(outcomes) == 24 and {o.kind for o in outcomes} == {SUCCEEDED}
    assert len(world.artifacts()) == 24
    assert all(world.job(j)["attempt"] == 1 and world.job(j)["output_count"] == 1 for j in ids)


def test_compute_holds_no_transaction_or_lock(world):
    r = world.explicit(world.a["token"], "nolock", "계산 중 잠금 없음")
    results = {}

    def during_compute():
        # runs while the engine "computes": a writer needs the exclusive control lock and must not block
        started = time.monotonic()
        results["submit"] = world.explicit(world.b["token"], "other", "다른 입력")
        results["elapsed"] = time.monotonic() - started
        results["idle_in_tx"] = world.query(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database() "
            "AND state LIKE 'idle in transaction%%'")[0]["n"]

    hooks.register("worker_after_claim", during_compute)
    assert world.worker("A").process_one().kind == SUCCEEDED
    assert results["elapsed"] < 0.9 and results["idle_in_tx"] == 0
    assert world.job(r["job_id"])["state"] == "succeeded"


# --- failure classification and output validation -----------------------

def _claim_job(world, text="검증 대상"):
    r = world.explicit(world.a["token"], "bad-%d" % time.time_ns(), text)
    w = world.worker("A")
    claim = w.claim()
    return r, w, claim


def good_draft(claim, **over):
    d = dict(local_key="0", payload_schema="reference-artifact/v1", payload={"x": 1}, display_text="t",
             search_text="t", source_refs=[{"experience_id": claim.snapshot.experience_id,
                                            "source_version": claim.snapshot.source_version}])
    d.update(over)
    return ArtifactDraft(**d)


@pytest.mark.parametrize("mutate,code", [
    (lambda c: [], "OUTPUT_INVALID"),
    (lambda c: [good_draft(c), good_draft(c)], "OUTPUT_INVALID"),
    (lambda c: [good_draft(c, display_text="")], "OUTPUT_INVALID"),
    (lambda c: [good_draft(c, payload={"x": "a\x00b"})], "OUTPUT_INVALID"),
    (lambda c: [good_draft(c, source_refs=[])], "OUTPUT_INVALID"),
    (lambda c: [good_draft(c, source_refs=[{"experience_id": "00000000-0000-0000-0000-000000000001",
                                            "source_version": 1}])], "OUTPUT_PROVENANCE"),
    (lambda c: [good_draft(c, source_refs=[{"experience_id": c.snapshot.experience_id, "source_version": 1,
                                            "start_byte": 5, "end_byte": 999999}])], "OUTPUT_PROVENANCE"),
    (lambda c: [good_draft(c, local_key=str(i)) for i in range(257)], "OUTPUT_LIMIT"),
])
def test_invalid_engine_output_is_rejected_atomically(world, mutate, code):
    r, w, claim = _claim_job(world)
    outcome = w.finalize(claim, mutate(claim))
    assert (outcome.kind, outcome.code) == (FAILED, code)
    assert world.artifacts() == [] and world.job(r["job_id"])["state"] == "failed"


def test_retryable_engine_error_backs_off_then_fails_at_limit(world):
    r = world.explicit(world.a["token"], "retry", "일시 오류")
    w = world.worker("A")
    for attempt in (1, 2):
        claim = w.claim()
        assert claim.attempt == attempt
        out = w.finalize(claim, error=EngineError("ENGINE_TIMEOUT", retryable=True))
        assert (out.kind, out.code) == (RETRY, "ENGINE_TIMEOUT")
        job = world.job(r["job_id"])
        assert job["state"] == "queued" and job["lease_owner"] is None
        assert w.claim() is None  # backoff is honoured
        world.make_due(r["job_id"])
    claim = w.claim()
    out = w.finalize(claim, error=EngineError("ENGINE_TIMEOUT", retryable=True))
    assert (out.kind, out.code) == (FAILED, "ENGINE_TIMEOUT")
    assert world.job(r["job_id"])["attempt"] == 3 and world.artifacts() == []


def test_permanent_engine_error_is_not_retried(world):
    r = world.explicit(world.a["token"], "perm", "형식 오류")
    w = world.worker("A")
    out = w.finalize(w.claim(), error=EngineError("OUTPUT_INVALID"))
    assert out.kind == FAILED and world.job(r["job_id"])["state"] == "failed"
    world.make_due(r["job_id"])
    assert w.claim() is None


def test_unknown_engine_version_fails_without_output(world):
    r = world.explicit(world.a["token"], "nover", "엔진 없음")
    world.query("UPDATE generations SET engine_spec = jsonb_set(engine_spec, '{version}', '\"99\"') WHERE id = %s",
                (world.personal["generation_id"],))
    [o] = world.drain()
    assert (o.kind, o.code) == (FAILED, "ENGINE_VERSION_UNAVAILABLE")
    assert world.artifacts() == [] and world.job(r["job_id"])["state"] == "failed"


def test_engine_crash_becomes_safe_failure(world, monkeypatch):
    r = world.explicit(world.a["token"], "crash", "엔진 예외")
    from memory_server import engine

    def boom(self, snap):
        raise ValueError("secret raw text: " + snap.text)
    monkeypatch.setattr(engine.ReferenceEngine, "process", boom)
    [o] = world.drain()
    assert (o.kind, o.code) == (FAILED, "ENGINE_ERROR")
    job = world.job(r["job_id"])
    assert job["error_code"] == "ENGINE_ERROR" and "secret" not in str(job)


def test_job_status_reports_success_visibility_and_failure(world):
    ok = world.explicit(world.a["token"], "s1", "상태 확인")
    world.drain()
    st = world.svc.get_job(world.b["token"], ok["job_id"])
    assert st["state"] == "succeeded" and st["currently_visible"] is True and st["output_count"] == 1
    assert st["visibility_reason"] == "active_valid"


def test_claim_cancels_when_source_digest_changed(world):
    r = world.explicit(world.a["token"], "tamper", "원본 무결성")
    world.query("UPDATE jobs SET input_digest = 'deadbeef' WHERE id = %s", (r["job_id"],))
    assert world.drain() == []  # cancelled at claim, never run
    job = world.job(r["job_id"])
    assert (job["state"], job["error_code"]) == ("cancelled", "INPUT_CHANGED")
    assert world.artifacts() == []
