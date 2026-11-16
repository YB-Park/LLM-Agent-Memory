"""P0-3: deletion, authority revocation, manual retry and correction (design T5, T6, T7, T19; §6.4, §6.5)."""
import json
import threading
import time
import uuid

import pytest
from psycopg2.extras import Json

from memory_server import admin, hooks, operations
from memory_server.engine import reference_spec, spec_digest
from memory_server.errors import ServiceError
from memory_server.worker import CANCELLED, STALE, SUCCEEDED

TEXT = "배포 DB는 PostgreSQL을 사용한다. zebra_token"


def delete(world, experience_id):
    return operations.delete_experience(world.config, experience_id)


def policy_revision(world):
    return world.query("SELECT policy_revision FROM system_control")[0]["policy_revision"]


def add_generation(world, experience_id, source_job_id, state="building", version="1"):
    """Second generation (as P0-4 rebuild will create one) with a real queued Job for the same source."""
    spec = reference_spec(version)
    gen = str(uuid.uuid4())
    world.query("INSERT INTO generations (id, engine_spec, spec_digest, index_profile, state, "
                "snapshot_ingest_revision) VALUES (%s, %s, %s, 'reference-keywords/v1', %s, 0)",
                (gen, Json(spec), spec_digest(spec), state))
    old = world.job(source_job_id)
    job = str(uuid.uuid4())
    world.query("INSERT INTO jobs (id, generation_id, experience_id, source_version, input_digest, "
                "requested_by, authority_epoch, state) VALUES (%s, %s, %s, 1, %s, %s, %s, 'queued')",
                (job, gen, experience_id, old["input_digest"], old["requested_by"],
                 old["authority_epoch"]))
    return gen, job


def wait_until_blocked_on_control(world, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = world.query("SELECT 1 FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                           "AND query LIKE '%%system_control%%' AND datname = current_database()")
        if rows:
            return
        time.sleep(0.02)
    raise AssertionError("no session is waiting for the control lock")


# ---- deletion (T5, T6) ---------------------------------------------------

def test_delete_before_worker_runs_cancels_job_and_publishes_nothing(world):
    r = world.explicit(world.a["token"], "d1", TEXT)
    out = delete(world, r["experience_id"])
    assert out["visibility"] == "hidden" and out["operational_payload"] == "redacted"
    assert out["backup_erasure"] == "not_guaranteed" and out["jobs_cancelled"] == 1
    assert world.drain() == []
    job = world.job(r["job_id"])
    assert (job["state"], job["error_code"]) == ("cancelled", "SOURCE_DELETED")
    assert world.artifacts() == [] and world.search(world.a["token"], "zebra_token")["results"] == []


def test_delete_after_publish_erases_and_hides_everywhere(world):
    r = world.explicit(world.a["token"], "d2", TEXT, title="비밀 제목", hints=["hintword"])
    world.drain()
    assert len(world.search(world.a["token"], "zebra_token")["results"]) == 1
    before = policy_revision(world)
    out = delete(world, r["experience_id"])
    assert out["artifacts_invalidated"] == 1 and policy_revision(world) == before + 1
    assert world.search(world.a["token"], "zebra_token")["results"] == []
    assert world.search(world.reader["token"], "hintword")["results"] == []
    # tombstone and invalidated artifact keep no content
    exp = world.query("SELECT * FROM experiences WHERE id = %s", (r["experience_id"],))[0]
    assert exp["state"] == "deleted" and exp["raw_text"] is None and exp["source_metadata"] is None
    assert exp["content_digest"] is None and exp["deleted_at"] is not None
    [art] = world.artifacts(state="invalidated")
    assert art["payload"] is None and art["display_text"] is None and art["search_text"] is None
    with pytest.raises(ServiceError) as e:
        world.svc.get_experience(world.b["token"], r["experience_id"])
    assert e.value.status == 410
    job = world.svc.get_job(world.b["token"], r["job_id"])
    assert job["state"] == "succeeded" and job["currently_visible"] is False
    assert job["visibility_reason"] == "deleted_source"
    # the old idempotency key cannot resurrect it
    with pytest.raises(ServiceError) as e:
        world.svc.submit(world.a["token"], world.body(TEXT, submission={
            "intent": "explicit_memory", "actor_reported": "agent", "title": "비밀 제목",
            "retrieval_hints": ["hintword"]}), "d2")
    assert e.value.status == 410
    # audit has ids and counts only
    audit = world.query("SELECT * FROM audit_events WHERE event_type = 'experience_deleted' "
                        "AND object_ids->>'experience_id' = %s", (r["experience_id"],))
    assert len(audit) == 1 and "zebra_token" not in json.dumps(audit[0], default=str)


def test_delete_is_idempotent_and_unknown_id_is_not_found(world):
    r = world.explicit(world.a["token"], "d3", TEXT)
    world.drain()
    first = delete(world, r["experience_id"])
    before = policy_revision(world)
    again = delete(world, r["experience_id"])
    assert first["already_deleted"] is False and again["already_deleted"] is True
    assert again["artifacts_invalidated"] == 0 and policy_revision(world) == before
    with pytest.raises(ServiceError) as e:
        delete(world, str(uuid.uuid4()))
    assert e.value.status == 404


@pytest.mark.parametrize("point", ["worker_after_compute", "finalize_before_tx"])
def test_delete_while_worker_is_processing_blocks_late_publication(world, point):
    r = world.explicit(world.a["token"], "d4", TEXT)
    w = world.worker("W1")
    hooks.register(point, lambda: delete(world, r["experience_id"]))
    outcome = w.process_one()
    hooks.clear()
    assert outcome.kind == STALE
    assert world.artifacts() == [] and world.artifacts(state="invalidated") == []
    job = world.job(r["job_id"])
    assert (job["state"], job["error_code"]) == ("cancelled", "SOURCE_DELETED")
    assert world.search(world.a["token"], "zebra_token")["results"] == []


def test_delete_just_before_commit_waits_for_finalize_then_invalidates(world):
    """Finalize holds the control lock from revalidation to commit; delete queues behind it (T5)."""
    r = world.explicit(world.a["token"], "d5", TEXT)
    w = world.worker("W1")
    inserted, release = threading.Event(), threading.Event()

    def pause():
        inserted.set()
        assert release.wait(10)
    hooks.register("finalize_after_insert", pause)
    result = {}
    t = threading.Thread(target=lambda: result.update(o=w.process_one()))
    t.start()
    assert inserted.wait(10)
    hooks.clear()
    d = threading.Thread(target=lambda: result.update(d=delete(world, r["experience_id"])))
    d.start()
    wait_until_blocked_on_control(world)
    assert "d" not in result  # the delete is still waiting
    release.set()
    t.join(10)
    d.join(10)
    assert result["o"].kind == SUCCEEDED and result["d"]["artifacts_invalidated"] == 1
    assert world.search(world.a["token"], "zebra_token")["results"] == []
    assert world.artifacts() == [] and len(world.artifacts(state="invalidated")) == 1


def test_delete_invalidates_results_in_every_generation_and_stays_after_reuse(world):
    r = world.explicit(world.a["token"], "d6", TEXT)
    world.drain()
    gen2, job2 = add_generation(world, r["experience_id"], r["job_id"], state="building", version="2")
    world.drain()
    assert world.job(job2)["state"] == "succeeded"
    assert len(world.artifacts()) == 2
    revs = {str(g["id"]): g["result_revision"] for g in world.query(
        "SELECT id, result_revision FROM generations")}
    delete(world, r["experience_id"])
    assert world.artifacts() == [] and len(world.artifacts(state="invalidated")) == 2
    for g in world.query("SELECT id, result_revision FROM generations"):
        assert g["result_revision"] == revs[str(g["id"])] + 1
    # even if the old generation is made servable/active, nothing is visible and nothing can be retried
    world.query("UPDATE generations SET state = 'retired' WHERE state = 'servable'; "
                "UPDATE generations SET state = 'servable' WHERE id = %s; "
                "UPDATE system_control SET active_generation_id = %s", (gen2, gen2))
    assert world.search(world.a["token"], "zebra_token")["results"] == []


def test_delete_does_not_cascade_to_independent_explicit_memory(world):
    general = world.submit(world.a["token"], "g1", world.body("일반 원본 본문 alpha_source"))[0]
    explicit = world.svc.submit(world.a["token"], world.body(
        "독립 기억 beta_memory", submission={"intent": "explicit_memory", "actor_reported": "user"},
        source={"kind": "note", "source_ref": "exp:" + general["experience_id"]}), "g2")[0]
    world.drain()
    delete(world, general["experience_id"])
    assert world.search(world.a["token"], "alpha_source")["results"] == []
    [hit] = world.search(world.a["token"], "beta_memory")["results"]
    assert hit["sources"][0]["experience_id"] == explicit["experience_id"]
    assert hit["sources"][0]["locator"]["verified"] is False


def test_manual_correction_flow_old_memory_never_reappears(world):
    wrong = world.explicit(world.a["token"], "c1", "프로젝트 DB는 MySQL이다 wrongfact")
    world.drain()
    assert len(world.search(world.a["token"], "wrongfact")["results"]) == 1
    delete(world, wrong["experience_id"])
    right = world.explicit(world.a["token"], "c2", "프로젝트 DB는 PostgreSQL이다 rightfact")
    assert right["experience_id"] != wrong["experience_id"]
    world.drain()
    assert world.search(world.a["token"], "wrongfact")["results"] == []
    [hit] = world.search(world.a["token"], "rightfact")["results"]
    assert hit["sources"][0]["experience_id"] == right["experience_id"]
    assert world.job(right["job_id"])["state"] == "succeeded"
    # no supersession link between the two
    assert "supersedes" not in json.dumps(hit)
    # a later worker run or reaper pass cannot bring it back
    world.drain()
    assert world.search(world.a["token"], "wrongfact")["results"] == []


# ---- revocation (T7) ---------------------------------------------------------

def test_revoke_cancels_open_jobs_blocks_device_and_keeps_shared_data_for_b(world):
    done = world.explicit(world.a["token"], "v1", "공유 기억 sharedfact")
    world.drain()
    pending = world.explicit(world.a["token"], "v2", "대기 중 기억 pendingfact")
    out = operations.revoke_principal(world.config, world.a["principal_id"])
    assert out["revoked"] is True and out["jobs_cancelled"] == 1 and out["shared_data"] == "retained"
    job = world.job(pending["job_id"])
    assert (job["state"], job["error_code"]) == ("cancelled", "AUTHORITY_REVOKED")
    # A is locked out everywhere
    for call in (lambda: world.search(world.a["token"], "sharedfact"),
                 lambda: world.svc.get_experience(world.a["token"], done["experience_id"]),
                 lambda: world.explicit(world.a["token"], "v3", "x y")):
        with pytest.raises(ServiceError) as e:
            call()
        assert e.value.status == 401
    # B keeps reading A's already-published shared result and the source
    [hit] = world.search(world.b["token"], "sharedfact")["results"]
    assert hit["sources"][0]["experience_id"] == done["experience_id"]
    assert world.svc.get_experience(world.b["token"], done["experience_id"])["state"] == "retained"
    # the cancelled job published nothing
    assert world.drain() == [] and world.search(world.b["token"], "pendingfact")["results"] == []
    again = operations.revoke_principal(world.config, world.a["principal_id"])
    assert again["already_revoked"] is True and again["jobs_cancelled"] == 0


def test_revoke_during_compute_fences_late_finalize(world):
    r = world.explicit(world.a["token"], "v4", "계산 중 철회 racefact")
    w = world.worker("W1")
    hooks.register("worker_after_compute", lambda: operations.revoke_principal(world.config,
                                                                              world.a["principal_id"]))
    outcome = w.process_one()
    hooks.clear()
    assert outcome.kind == STALE and world.artifacts() == []
    assert world.job(r["job_id"])["state"] == "cancelled"
    assert world.search(world.b["token"], "racefact")["results"] == []


def test_revoke_waits_for_inflight_finalize_which_stays_published(world):
    r = world.explicit(world.a["token"], "v5", "커밋 직전 철회 lastfact")
    w = world.worker("W1")
    inserted, release = threading.Event(), threading.Event()
    hooks.register("finalize_after_insert", lambda: (inserted.set(), release.wait(10)))
    result = {}
    t = threading.Thread(target=lambda: result.update(o=w.process_one()))
    t.start()
    assert inserted.wait(10)
    hooks.clear()
    rv = threading.Thread(target=lambda: result.update(r=operations.revoke_principal(
        world.config, world.a["principal_id"])))
    rv.start()
    wait_until_blocked_on_control(world)
    release.set()
    t.join(10)
    rv.join(10)
    assert result["o"].kind == SUCCEEDED and result["r"]["jobs_cancelled"] == 0
    # revocation is not deletion: the shared result stays for B
    assert len(world.search(world.b["token"], "lastfact")["results"]) == 1


def test_permission_reduction_fences_open_jobs(world):
    r = world.explicit(world.a["token"], "v6", "권한 축소 reducedfact")
    out = admin.set_permissions(world.dsn, world.a["principal_id"], read=True)  # write removed
    assert out["jobs_cancelled"] == 1
    assert world.job(r["job_id"])["error_code"] == "AUTHORITY_REVOKED"
    assert world.drain() == []
    with pytest.raises(ServiceError) as e:  # token is still valid but its epoch moved and write is gone
        world.explicit(world.a["token"], "v7", "다시 시도 again")
    assert e.value.status == 404


# ---- manual retry ----------------------------------------------------------------

def cancelled_job_of_a(world, key, text):
    r = world.explicit(world.a["token"], key, text)
    operations.revoke_principal(world.config, world.a["principal_id"])
    return r


def test_manage_principal_retries_cancelled_job_under_its_own_authority(world):
    r = cancelled_job_of_a(world, "m1", "재시도 대상 retryfact")
    admin.set_permissions(world.dsn, world.b["principal_id"], manage=True)
    attempt = world.job(r["job_id"])["attempt"]
    out = operations.retry_job(world.config, world.b["principal_id"], r["job_id"], attempt)
    assert out["state"] == "queued" and out["requested_by"] == world.b["principal_id"]
    audit = world.query("SELECT safe_details FROM audit_events WHERE event_type = 'job_retried' "
                        "AND object_ids->>'job_id' = %s", (r["job_id"],))[0]["safe_details"]
    assert audit["previous_principal_id"] == world.a["principal_id"]
    assert audit["new_principal_id"] == world.b["principal_id"]
    # nothing was published before the retry ran
    assert world.search(world.b["token"], "retryfact")["results"] == []
    [o] = world.drain()
    assert o.kind == SUCCEEDED
    assert len(world.search(world.b["token"], "retryfact")["results"]) == 1
    # a repeated identical command is acknowledged, not re-applied
    again = operations.retry_job(world.config, world.b["principal_id"], r["job_id"], attempt)
    assert again["already_applied"] is True and world.job(r["job_id"])["state"] == "succeeded"


def test_retry_requires_manage_and_matching_attempt(world):
    r = cancelled_job_of_a(world, "m2", "관리 권한 필요")
    attempt = world.job(r["job_id"])["attempt"]
    with pytest.raises(ServiceError) as e:  # B can write but not manage
        operations.retry_job(world.config, world.b["principal_id"], r["job_id"], attempt)
    assert e.value.status == 404
    admin.set_permissions(world.dsn, world.b["principal_id"], manage=True)
    with pytest.raises(ServiceError) as e:
        operations.retry_job(world.config, world.b["principal_id"], r["job_id"], attempt + 5)
    assert (e.value.status, e.value.code) == (409, "STALE_ATTEMPT")
    # a revoked principal cannot retry either
    operations.revoke_principal(world.config, world.b["principal_id"])
    with pytest.raises(ServiceError) as e:
        operations.retry_job(world.config, world.b["principal_id"], r["job_id"], attempt)
    assert e.value.status == 404


def test_retry_refused_for_deleted_source_succeeded_job_and_retired_generation(world):
    admin.set_permissions(world.dsn, world.b["principal_id"], manage=True)
    b = world.b["principal_id"]
    done = world.explicit(world.a["token"], "m3", "성공한 작업 okfact")
    world.drain()
    with pytest.raises(ServiceError) as e:
        operations.retry_job(world.config, b, done["job_id"], world.job(done["job_id"])["attempt"])
    assert e.value.code == "JOB_NOT_RETRYABLE"

    gone = world.explicit(world.a["token"], "m4", "삭제될 작업 gonefact")
    delete(world, gone["experience_id"])  # job cancelled with SOURCE_DELETED
    with pytest.raises(ServiceError) as e:
        operations.retry_job(world.config, b, gone["job_id"], world.job(gone["job_id"])["attempt"])
    assert e.value.code == "SOURCE_DELETED"
    assert world.drain() == []

    live = world.explicit(world.a["token"], "m5", "세대 종료 retiredfact")
    gen2, job2 = add_generation(world, live["experience_id"], live["job_id"], state="building")
    world.query("UPDATE jobs SET state = 'failed', error_code = 'X' WHERE id = %s", (job2,))
    world.query("UPDATE generations SET state = 'retired' WHERE id = %s", (gen2,))
    with pytest.raises(ServiceError) as e:
        operations.retry_job(world.config, b, job2, world.job(job2)["attempt"])
    assert e.value.code == "GENERATION_UNAVAILABLE"


def test_failed_job_after_attempt_exhaustion_can_be_retried_with_fresh_budget(world):
    r = world.explicit(world.a["token"], "m6", "한도 소진 exhaustfact")
    world.query("UPDATE jobs SET state = 'failed', error_code = 'LEASE_EXPIRED', retry_count = 3, attempt = 3, "
                "completed_at = now() WHERE id = %s", (r["job_id"],))
    admin.set_permissions(world.dsn, world.a["principal_id"], manage=True)
    operations.retry_job(world.config, world.a["principal_id"], r["job_id"], 3)
    job = world.job(r["job_id"])
    assert job["state"] == "queued" and job["retry_count"] == 0 and job["error_code"] is None
    world.drain()
    assert len(world.search(world.a["token"], "exhaustfact")["results"]) == 1


# ---- surface -----------------------------------------------------------------------------

def test_admin_cli_commands_and_no_public_admin_surface(world, monkeypatch, capsys):
    pytest.importorskip("starlette")  # the HTTP adapter needs the pinned ASGI stack (not in the legacy image)
    from httpserver import RunningServer
    import urllib.error
    import urllib.request

    r = world.explicit(world.a["token"], "x1", "CLI 삭제 clifact")
    monkeypatch.setenv("MEMORY_DATABASE_URL", world.dsn)
    monkeypatch.setenv("MEMORY_HMAC_KEY", "x" * 40)
    admin.main(["delete-experience", "--experience", r["experience_id"]])
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "deleted" and out["backup_erasure"] == "not_guaranteed"
    with pytest.raises(SystemExit) as e:
        admin.main(["delete-experience", "--experience", str(uuid.uuid4())])
    assert e.value.code == 1

    with RunningServer(world.config) as running:
        for method, path in (("DELETE", "/v2/experiences/" + r["experience_id"]),
                             ("POST", "/v2/admin/principals/%s/revoke" % world.a["principal_id"]),
                             ("POST", "/v2/admin/jobs/%s/retry" % r["job_id"])):
            req = urllib.request.Request(running.base + path, method=method, data=b"{}",
                                         headers={"Authorization": "Bearer " + world.a["token"]})
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(req)
            assert err.value.code in (404, 405, 501)  # no handler either way
