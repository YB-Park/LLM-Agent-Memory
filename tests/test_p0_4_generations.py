"""P0-4: Generation rebuild, comparison, activation, rollback (design T9-T13, T19 rollback part; §8)."""
import json
import os
import subprocess
import sys
import threading
import uuid

import pytest

from memory_server import admin, generations as gens, hooks, operations
from memory_server.errors import ServiceError

MULTI = "알파 문단 apple\n\n베타 문단 banana\n\n감마 문단 cherry"
QUERIES = ["banana", "explicitfact", "singlefact", "nomatchterm"]


def manager(world):
    admin.set_permissions(world.dsn, world.b["principal_id"], manage=True)
    return world.b["principal_id"]


def seed(world):
    e1 = world.explicit(world.a["token"], "s1", "배포 DB는 PostgreSQL이다 explicitfact", title="DB 선택")
    e2 = world.svc.submit(world.a["token"], world.body(MULTI), "s2")[0]
    e3 = world.svc.submit(world.a["token"], world.body("단일 문단 일반 원본 singlefact"), "s3")[0]
    world.drain()
    return e1, e2, e3


def control_row(world):
    return world.query("SELECT * FROM system_control")[0]


def active_id(world):
    return str(control_row(world)["active_generation_id"])


def start(world, version="2", op=None, expected=None, pid=None, **kw):
    return gens.start_generation(world.config, pid or world.b["principal_id"], op or str(uuid.uuid4()),
                                 expected or active_id(world), version, **kw)


def compare(world, gid, queries=QUERIES, pid=None):
    return gens.compare_generation(world.config, pid or world.b["principal_id"], gid, queries)


def activate(world, gid, receipt, expected=None, pid=None):
    return gens.activate_generation(world.config, pid or world.b["principal_id"], gid, expected or active_id(world),
                                    receipt)


def resume(world, pid=None):
    return gens.resume_ingest(world.config, pid or world.b["principal_id"], active_id(world))


def rebuild_to(world, version, queries=QUERIES):
    """start → drain → compare → activate; returns (generation id, previous generation id)."""
    prev = active_id(world)
    g = start(world, version)["generation_id"]
    world.drain()
    rep = compare(world, g, queries)
    assert rep["status"] == "ready", rep["coverage"]
    activate(world, g, rep["receipt_id"])
    return g, prev


def artifact_texts(world, gid):
    return sorted(r["display_text"] for r in world.query(
        "SELECT display_text FROM artifacts WHERE generation_id = %s AND state = 'valid'", (gid,)))


def raises(code, status=409):
    class Ctx(object):
        def __enter__(self):
            self.cm = pytest.raises(ServiceError)
            self.info = self.cm.__enter__()
            return self.info

        def __exit__(self, *exc):
            ok = self.cm.__exit__(*exc)
            assert self.info.value.code == code and self.info.value.status == status, \
                (self.info.value.status, self.info.value.code)
            return ok
    return Ctx()


# ---- start / manifest / intake pause (T9) -------------------------------------------

def test_start_pins_exact_source_set_pauses_intake_and_keeps_g1_serving(world):
    pid = manager(world)
    e1, e2, e3 = seed(world)
    gone = world.explicit(world.a["token"], "s4", "삭제될 기억 gonefact")
    world.drain()
    operations.delete_experience(world.config, gone["experience_id"])
    g1 = active_id(world)
    st = start(world, "2")
    g2 = st["generation_id"]
    assert st["state"] == "building" and st["engine"] == "reference/2" and st["write_gate"] == "paused"
    assert st["base_generation_id"] == g1 and st["is_maintenance_candidate"] is True and st["is_active"] is False
    manifest = {str(r["experience_id"]) for r in world.query("SELECT experience_id FROM jobs WHERE generation_id=%s",
                                                              (g2,))}
    assert manifest == {e1["experience_id"], e2["experience_id"], e3["experience_id"]}  # deleted source excluded
    assert all(str(j["requested_by"]) == pid for j in world.query("SELECT requested_by FROM jobs WHERE generation_id=%s", (g2,)))
    # G1 keeps answering while G2 is only a candidate
    res = world.search(world.a["token"], "banana")
    assert res["generation_id"] == g1 and res["engine"] == "reference/1" and len(res["results"]) == 1
    # operation ID makes start idempotent; a different spec under the same ID conflicts
    again = start(world, "2", op=g2)
    assert again["already_exists"] is True and again["generation_id"] == g2
    with raises("OPERATION_CONFLICT"):
        start(world, "1", op=g2)
    with raises("REBUILD_IN_PROGRESS"):
        start(world, "2")


def test_start_guards(world):
    pid = manager(world)
    seed(world)
    with raises("STALE_ACTIVE"):
        start(world, "2", expected=str(uuid.uuid4()))
    with raises("ENGINE_VERSION_UNAVAILABLE"):
        start(world, "9")
    with pytest.raises(ServiceError) as e:  # write-only device is not a manager
        start(world, "2", pid=world.a["principal_id"])
    assert e.value.status == 404
    assert control_row(world)["write_gate"] == "open" and control_row(world)["maintenance_generation_id"] is None
    old = gens.MAX_GENERATIONS
    gens.MAX_GENERATIONS = 1
    try:
        with raises("GENERATION_LIMIT"):
            start(world, "2", pid=pid)
    finally:
        gens.MAX_GENERATIONS = old


def test_intake_is_rejected_while_paused_and_original_receipt_still_replays(world):
    manager(world)
    e1, _, _ = seed(world)
    start(world, "2")
    before = world.counts()
    with pytest.raises(ServiceError) as e:
        world.explicit(world.a["token"], "new-during", "리빌드 중 입력")
    assert (e.value.status, e.value.code, e.value.retryable) == (503, "INGEST_PAUSED", True)
    assert world.counts() == before  # no source, receipt or job was created
    replay, replayed = world.svc.submit(world.a["token"], world.body(
        "배포 DB는 PostgreSQL이다 explicitfact", submission={"intent": "explicit_memory", "actor_reported": "agent",
                                                        "title": "DB 선택"}), "s1")
    assert replayed is True and replay["experience_id"] == e1["experience_id"]


def test_submit_committed_before_start_is_in_the_manifest(world):
    manager(world)
    seed(world)
    late = world.explicit(world.a["token"], "pre", "시작 직전 접수 prefact")  # job still queued in G1
    g2 = start(world, "2")["generation_id"]
    world.drain()
    ids = {str(r["experience_id"]) for r in world.query("SELECT experience_id FROM jobs WHERE generation_id = %s",
                                                         (g2,))}
    assert late["experience_id"] in ids
    assert gens.generation_status(world.config, g2)["coverage"]["ok"] is True


# ---- build, isolation, coverage (T10) -------------------------------------------------

def test_same_sources_two_reference_versions_are_isolated_per_generation(world):
    manager(world)
    e1, e2, e3 = seed(world)
    g1 = active_id(world)
    g2 = start(world, "2")["generation_id"]
    world.drain()
    assert artifact_texts(world, g1) == sorted(["배포 DB는 PostgreSQL이다 explicitfact", MULTI,
                                                "단일 문단 일반 원본 singlefact"])
    assert artifact_texts(world, g2) == sorted(["배포 DB는 PostgreSQL이다 explicitfact", "알파 문단 apple",
                                                "베타 문단 banana", "감마 문단 cherry",
                                                "단일 문단 일반 원본 singlefact"])
    # general search is G1 only: the whole-text artifact, never a G2 paragraph
    [hit] = world.search(world.a["token"], "banana")["results"]
    assert hit["text"] == MULTI and hit["engine_data"]["representation"] == "source_projection_whole"
    # every result of either generation points back at the same original sources
    for g in (g1, g2):
        srcs = {str(r["experience_id"]) for r in world.query(
            "SELECT s.experience_id FROM artifact_sources s JOIN artifacts a ON a.id = s.artifact_id "
            "WHERE a.generation_id = %s", (g,))}
        assert srcs == {e1["experience_id"], e2["experience_id"], e3["experience_id"]}


def test_incomplete_candidate_gets_no_receipt_and_cannot_be_activated(world):
    manager(world)
    seed(world)
    g2 = start(world, "2")["generation_id"]  # not drained yet: every job is pending
    rep = compare(world, g2)
    assert rep["status"] == "incomplete" and rep["receipt_id"] is None
    assert rep["coverage"]["candidate"]["problems"]["pending"] == 3
    with raises("RECEIPT_INVALID"):
        activate(world, g2, str(uuid.uuid4()))
    assert active_id(world) != g2


def test_coverage_is_an_id_set_check_not_a_count_check(world):
    manager(world)
    e1, e2, e3 = seed(world)
    g2 = start(world, "2")["generation_id"]
    world.drain()
    assert compare(world, g2)["status"] == "ready"
    # Same number of retained sources and jobs, but a different set: a source slipped in outside intake.
    operations.delete_experience(world.config, e3["experience_id"])
    intruder = str(uuid.uuid4())
    world.query("INSERT INTO experiences (id, raw_text, source_metadata, content_digest, state, "
                "created_by) VALUES (%s, 'sql 직접 삽입 intruder', %s, 'x', 'retained', %s)",
                (intruder, json.dumps({"source": {}, "submission": {}}), world.a["principal_id"]))
    cov = gens.generation_status(world.config, g2)["coverage"]
    assert cov["ok"] is False and cov["problems"]["missing"] == 1 and cov["examples"]["missing"] == [intruder]
    assert cov["expected_sources"] == 3
    rep = compare(world, g2)
    assert rep["status"] == "incomplete"


@pytest.mark.parametrize("tamper,problem", [
    ("UPDATE jobs SET state = 'failed', error_code = 'X' WHERE generation_id = %s AND experience_id = %s", "failed"),
    ("UPDATE jobs SET state = 'cancelled', error_code = 'X' WHERE generation_id = %s AND experience_id = %s",
     "failed"),
    ("DELETE FROM artifact_sources WHERE artifact_id IN (SELECT id FROM artifacts WHERE generation_id = %s "
     "AND job_id IN (SELECT id FROM jobs WHERE experience_id = %s))", "bad_output"),
    ("UPDATE artifacts SET state = 'invalidated', payload = NULL, display_text = NULL, search_text = NULL "
     "WHERE generation_id = %s AND job_id IN (SELECT id FROM jobs WHERE experience_id = %s)", "bad_output"),
])
def test_zero_output_failed_cancelled_and_unsourced_jobs_block_the_switch(world, tamper, problem):
    manager(world)
    e1, e2, e3 = seed(world)
    g2 = start(world, "2")["generation_id"]
    world.drain()
    first = compare(world, g2)
    assert first["status"] == "ready"
    if tamper.startswith("DELETE FROM artifact_sources"):
        world.query("ALTER TABLE artifact_sources DISABLE TRIGGER ALL")
    world.query(tamper, (g2, e1["experience_id"]))
    cov = gens.generation_status(world.config, g2)["coverage"]
    assert cov["ok"] is False and cov["problems"][problem] == 1
    with raises("COVERAGE_INCOMPLETE"):  # revisions are unchanged, so only the coverage re-check can stop this
        activate(world, g2, first["receipt_id"])
    assert compare(world, g2)["status"] == "incomplete"
    assert active_id(world) != g2


# ---- compare / activate (T11) -------------------------------------------------------------

def test_compare_reports_per_query_source_differences_without_quality_verdict_or_content(world):
    manager(world)
    seed(world)
    g1 = active_id(world)
    g2 = start(world, "2")["generation_id"]
    world.drain()
    rep = compare(world, g2)
    assert rep["status"] == "ready" and rep["external_calls"] == 0 and rep["quality_verdict"] is None
    assert rep["base_engine"] == "reference/1" and rep["candidate_engine"] == "reference/2"
    assert rep["coverage"]["base"]["ok"] and rep["coverage"]["candidate"]["ok"]
    by = {QUERIES[q["query_index"]]: q for q in rep["queries"]}
    assert by["banana"]["base"]["artifact_count"] == 1 and by["banana"]["candidate"]["artifact_count"] == 1
    assert by["banana"]["only_in_base"] == [] and by["banana"]["only_in_candidate"] == []  # same source ID
    assert by["nomatchterm"]["base"]["experience_ids"] == [] and by["nomatchterm"]["in_both"] == []
    assert by["banana"]["base"]["latency_ms"] >= 0
    # stored evidence has IDs, revisions, hashes and counts only
    stored = world.query("SELECT comparison_receipt FROM generations WHERE id = %s", (g2,))[0]["comparison_receipt"]
    dump = json.dumps(stored) + json.dumps(world.query("SELECT safe_details FROM audit_events "
                                                       "WHERE event_type = 'generation_compared'", ()),
                                           default=str)
    assert "banana" not in dump and "apple" not in dump and stored["base_generation_id"] == g1
    assert stored["query_count"] == len(QUERIES) and stored["external_calls"] == 0
    with pytest.raises(ServiceError) as e:
        compare(world, g2, ["q%d" % i for i in range(21)])
    assert e.value.status == 400


def test_activation_switches_pointer_and_search_keeps_gate_paused_until_resume(world):
    manager(world)
    seed(world)
    g1 = active_id(world)
    g2 = start(world, "2")["generation_id"]
    world.drain()
    rep = compare(world, g2)
    out = activate(world, g2, rep["receipt_id"])
    assert out["active_generation_id"] == g2 and out["previous_active_generation_id"] == g1
    assert out["write_gate"] == "paused" and out["already_applied"] is False
    states = {str(r["id"]): (r["state"], r["retired_ingest_revision"]) for r in world.query(
        "SELECT id, state, retired_ingest_revision FROM generations")}
    assert states[g1][0] == "retired" and states[g1][1] == control_row(world)["ingest_revision"]
    assert states[g2][0] == "servable"
    [hit] = world.search(world.a["token"], "banana")["results"]  # now served by G2's paragraph projection
    assert hit["text"] == "베타 문단 banana" and hit["sources"][0]["span"]["unit"] == "utf8_byte"
    with pytest.raises(ServiceError) as e:
        world.explicit(world.a["token"], "after-activate", "스모크 중 입력")
    assert e.value.code == "INGEST_PAUSED"
    # a repeated activation with the same receipt reports the existing result
    again = activate(world, g2, rep["receipt_id"], expected=g1)
    assert again["already_applied"] is True and active_id(world) == g2
    assert resume(world)["write_gate"] == "open" and resume(world)["already_applied"] is True
    new = world.explicit(world.a["token"], "after-resume", "전환 이후 접수 postswitchfact")
    assert new["generation_id"] == g2
    world.drain()
    assert len(world.search(world.a["token"], "postswitchfact")["results"]) == 1


def test_receipt_goes_stale_after_delete_revoke_or_result_change(world):
    manager(world)
    e1, e2, e3 = seed(world)
    g2 = start(world, "2")["generation_id"]
    world.drain()
    stale = compare(world, g2)["receipt_id"]
    operations.delete_experience(world.config, e3["experience_id"])  # allowed while paused
    with raises("COMPARISON_STALE"):
        activate(world, g2, stale)
    rep = compare(world, g2)
    assert rep["status"] == "ready" and rep["coverage"]["candidate"]["expected_sources"] == 2
    # a deleted source never comes back through the switch
    activate(world, g2, rep["receipt_id"])
    assert world.search(world.a["token"], "singlefact")["results"] == []
    assert not any("singlefact" in t for t in artifact_texts(world, g2))


def test_receipt_goes_stale_after_revocation_of_unrelated_device(world):
    manager(world)
    seed(world)
    g2 = start(world, "2")["generation_id"]
    world.drain()
    stale = compare(world, g2)["receipt_id"]
    operations.revoke_principal(world.config, world.reader["principal_id"])
    with raises("COMPARISON_STALE"):
        activate(world, g2, stale)
    assert activate(world, g2, compare(world, g2)["receipt_id"])["already_applied"] is False


def test_compare_discards_results_when_state_changes_between_read_and_store(world):
    manager(world)
    e1, e2, e3 = seed(world)
    g2 = start(world, "2")["generation_id"]
    world.drain()
    fired = []

    def mutate_once():
        if not fired:
            fired.append(1)
            operations.delete_experience(world.config, e1["experience_id"])
    hooks.register("compare_between_phases", mutate_once)
    rep = compare(world, g2)
    hooks.clear()
    assert fired and rep["status"] == "ready"
    # the stored receipt reflects the post-delete state, so it activates
    assert rep["coverage"]["candidate"]["expected_sources"] == 2
    activate(world, g2, rep["receipt_id"])


def test_delete_racing_with_activation_waits_and_then_invalidates_new_generation(world):
    manager(world)
    e1, e2, e3 = seed(world)
    g2 = start(world, "2")["generation_id"]
    world.drain()
    rep = compare(world, g2)
    inside, release = threading.Event(), threading.Event()
    hooks.register("activate_before_commit", lambda: (inside.set(), release.wait(10)))
    box = {}
    t = threading.Thread(target=lambda: box.update(a=activate(world, g2, rep["receipt_id"])))
    t.start()
    assert inside.wait(10)
    hooks.clear()
    d = threading.Thread(target=lambda: box.update(d=operations.delete_experience(world.config,
                                                                                   e2["experience_id"])))
    d.start()
    from test_p0_3_policy import wait_until_blocked_on_control
    wait_until_blocked_on_control(world)
    assert "d" not in box
    release.set()
    t.join(10)
    d.join(10)
    assert box["a"]["active_generation_id"] == g2 and box["d"]["artifacts_invalidated"] >= 4
    assert world.search(world.a["token"], "banana")["results"] == []
    assert world.search(world.a["token"], "apple")["results"] == []


def test_wrong_expected_active_and_foreign_receipt_cannot_switch(world):
    manager(world)
    seed(world)
    g1 = active_id(world)
    g2 = start(world, "2")["generation_id"]
    world.drain()
    rep = compare(world, g2)
    with raises("STALE_ACTIVE"):
        activate(world, g2, rep["receipt_id"], expected=str(uuid.uuid4()))
    with raises("RECEIPT_INVALID"):
        activate(world, g2, str(uuid.uuid4()))
    with pytest.raises(ServiceError) as e:  # a write-only device cannot activate
        activate(world, g2, rep["receipt_id"], pid=world.a["principal_id"])
    assert e.value.status == 404 and active_id(world) == g1


# ---- abort / resume / restart recovery (T12) ---------------------------------------------

def test_abort_fences_candidate_reopens_intake_on_g1(world):
    manager(world)
    seed(world)
    g1 = active_id(world)
    g2 = start(world, "2")["generation_id"]
    pid = world.b["principal_id"]
    out = gens.abort_generation(world.config, pid, g2, g1)
    assert out["state"] == "aborted" and out["write_gate"] == "open" and out["jobs_cancelled"] == 3
    assert world.drain() == []  # fenced jobs never run
    assert gens.abort_generation(world.config, pid, g2, g1)["already_applied"] is True
    new = world.explicit(world.a["token"], "after-abort", "중단 후 접수 abortfact")
    assert new["generation_id"] == g1
    world.drain()
    assert len(world.search(world.a["token"], "abortfact")["results"]) == 1
    assert world.search(world.a["token"], "banana")["results"][0]["text"] == MULTI
    with raises("NOT_IN_MAINTENANCE"):
        compare(world, g2)
    with raises("ALREADY_ACTIVE"):
        gens.abort_generation(world.config, pid, g1, g1)
    # a new rebuild can start after an abort
    assert start(world, "2")["state"] == "building"


def test_resume_refuses_while_candidate_pending(world):
    manager(world)
    seed(world)
    start(world, "2")
    with raises("CANDIDATE_PENDING"):
        resume(world)
    assert control_row(world)["write_gate"] == "paused"


def test_start_crash_before_commit_leaves_no_trace_and_intake_open(world):
    manager(world)
    seed(world)
    before = world.query("SELECT count(*) AS n FROM generations")[0]["n"]
    hooks.register("generation_start_before_commit", lambda: (_ for _ in ()).throw(RuntimeError("killed")))
    with pytest.raises(RuntimeError):
        start(world, "2")
    hooks.clear()
    assert world.query("SELECT count(*) AS n FROM generations")[0]["n"] == before
    assert control_row(world)["write_gate"] == "open" and control_row(world)["maintenance_generation_id"] is None
    assert world.explicit(world.a["token"], "still-open", "정상 접수 openfact")["status"] == "accepted"


def test_state_survives_restart_and_gate_is_not_reopened_automatically(world):
    manager(world)
    seed(world)
    g1 = active_id(world)
    g2 = start(world, "2")["generation_id"]
    env = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(__file__), "..", "src"),
               MEMORY_DATABASE_URL=world.dsn, MEMORY_HMAC_KEY="x" * 40)
    out = subprocess.run([sys.executable, "-m", "memory_server.admin", "generation-status", "--generation", g2],
                         env=env, capture_output=True, text=True, check=True).stdout
    st = json.loads(out)  # a different OS process sees the persisted state
    assert st["state"] == "building" and st["write_gate"] == "paused" and st["active_generation_id"] == g1
    from memory_server.service import MemoryService
    from memory_server.worker import Worker
    fresh = MemoryService(world.config)
    assert len(fresh.search(world.a["token"], {"query": "banana"})["results"]) == 1
    Worker(world.config, "after-restart").run_until_idle()
    with pytest.raises(ServiceError) as e:
        fresh.submit(world.a["token"], world.body("재시작 후 입력"), "post-restart")
    assert e.value.code == "INGEST_PAUSED"


def test_activation_crash_before_commit_keeps_g1_and_retry_completes(world):
    manager(world)
    seed(world)
    g1 = active_id(world)
    g2 = start(world, "2")["generation_id"]
    world.drain()
    rep = compare(world, g2)
    hooks.register("activate_before_commit", lambda: (_ for _ in ()).throw(RuntimeError("killed")))
    with pytest.raises(RuntimeError):
        activate(world, g2, rep["receipt_id"])
    hooks.clear()
    assert active_id(world) == g1 and control_row(world)["write_gate"] == "paused"
    states = {str(r["id"]): r["state"] for r in world.query("SELECT id, state FROM generations")}
    assert states[g1] == "servable" and states[g2] == "building"
    assert world.search(world.a["token"], "banana")["results"][0]["text"] == MULTI  # G1 still answers
    assert activate(world, g2, rep["receipt_id"], expected=g1)["already_applied"] is False
    assert activate(world, g2, rep["receipt_id"], expected=g1)["already_applied"] is True  # lost-response retry


def test_worker_death_during_candidate_build_still_converges(world):
    manager(world)
    seed(world)
    g2 = start(world, "2")["generation_id"]
    w = world.worker("dies")
    hooks.register("worker_after_claim", lambda: (_ for _ in ()).throw(RuntimeError("killed")))
    with pytest.raises(RuntimeError):
        w.process_one()
    hooks.clear()
    for j in world.query("SELECT id FROM jobs WHERE state = 'running'"):
        world.expire_lease(j["id"])
        world.make_due(j["id"])
    for _ in range(3):
        world.query("UPDATE jobs SET not_before = now() WHERE state = 'queued'")
        world.drain()
    assert compare(world, g2)["status"] == "ready"


# ---- authority of rebuild jobs ---------------------------------------------------------

def test_revoked_rebuild_manager_blocks_switch_until_another_manager_retries(world):
    pid = manager(world)
    seed(world)
    admin.set_permissions(world.dsn, world.a["principal_id"], manage=True)
    g2 = start(world, "2")["generation_id"]
    operations.revoke_principal(world.config, pid)  # the manager who owns the G2 jobs
    assert world.drain() == []
    rep = compare(world, g2, pid=world.a["principal_id"])
    assert rep["status"] == "incomplete" and rep["coverage"]["candidate"]["problems"]["failed"] == 3
    for j in world.query("SELECT id, attempt FROM jobs WHERE generation_id = %s", (g2,)):
        operations.retry_job(world.config, world.a["principal_id"], str(j["id"]), j["attempt"])
    world.drain()
    rep = compare(world, g2, pid=world.a["principal_id"])
    assert rep["status"] == "ready"
    assert activate(world, g2, rep["receipt_id"], pid=world.a["principal_id"])["active_generation_id"] == g2


# ---- rollback (T13) ------------------------------------------------------------------------

def test_rollback_without_new_input_reapplies_latest_deletions_and_revocations(world):
    manager(world)
    e1, e2, e3 = seed(world)
    g2, g1 = rebuild_to(world, "2")
    # while G2 is active (gate paused), a source is deleted: G1 holds stale results of it
    operations.delete_experience(world.config, e3["experience_id"])
    assert any("singlefact" in t for t in [r["display_text"] or "" for r in world.query(
        "SELECT display_text FROM artifacts WHERE generation_id = %s", (g1,))]) is False
    prep = gens.prepare_rollback(world.config, world.b["principal_id"], g1, g2)
    assert prep["state"] == "retired" and prep["is_maintenance_candidate"] is True
    rep = compare(world, g1)
    assert rep["status"] == "ready" and rep["base_engine"] == "reference/2" and rep["candidate_engine"] == "reference/1"
    out = activate(world, g1, rep["receipt_id"], expected=g2)
    assert out["active_generation_id"] == g1 and out["previous_active_generation_id"] == g2
    assert world.search(world.a["token"], "singlefact")["results"] == []  # deleted source stays hidden
    [hit] = world.search(world.a["token"], "banana")["results"]
    assert hit["text"] == MULTI  # reference/1 whole-text projection again
    # G2 is retired and could be rolled forward under the same rule
    assert world.query("SELECT state FROM generations WHERE id = %s", (g2,))[0]["state"] == "retired"
    resume(world)
    new = world.explicit(world.a["token"], "after-rb", "롤백 후 접수 rbfact")
    world.drain()
    assert new["generation_id"] == g1 and len(world.search(world.a["token"], "rbfact")["results"]) == 1


def test_rollback_requires_rebuild_when_input_arrived_after_switch(world):
    manager(world)
    seed(world)
    g2, g1 = rebuild_to(world, "2")
    resume(world)
    e5 = world.explicit(world.a["token"], "e5", "전환 이후 신규 입력 newfact")
    world.drain()
    with raises("ROLLBACK_REBUILD_REQUIRED"):
        gens.prepare_rollback(world.config, world.b["principal_id"], g1, g2)
    # even an input that was accepted and then deleted forces a rebuild (conservative)
    operations.delete_experience(world.config, e5["experience_id"])
    with raises("ROLLBACK_REBUILD_REQUIRED"):
        gens.prepare_rollback(world.config, world.b["principal_id"], g1, g2)
    assert active_id(world) == g2 and control_row(world)["write_gate"] == "open"
    # a G1 pointer rollback would have silently dropped E5; the rebuild path keeps it
    e6 = world.explicit(world.a["token"], "e6", "롤백 대상 이후 입력 keepfact")
    world.drain()
    g3 = start(world, "1")["generation_id"]
    world.drain()
    rep = compare(world, g3)
    assert rep["status"] == "ready" and rep["candidate_engine"] == "reference/1"
    activate(world, g3, rep["receipt_id"])
    [hit] = world.search(world.a["token"], "keepfact")["results"]
    assert hit["sources"][0]["experience_id"] == e6["experience_id"] and hit["engine_data"]["representation"] \
        == "explicit_memory"
    assert world.search(world.a["token"], "newfact")["results"] == []  # the deleted E5 stays deleted
    assert world.search(world.a["token"], "banana")["results"][0]["text"] == MULTI


def test_rollback_activation_rechecks_ingest_revision_itself(world):
    manager(world)
    seed(world)
    g2, g1 = rebuild_to(world, "2")
    gens.prepare_rollback(world.config, world.b["principal_id"], g1, g2)
    rep = compare(world, g1)
    world.query("UPDATE system_control SET ingest_revision = ingest_revision + 1")
    with raises("ROLLBACK_REBUILD_REQUIRED"):
        activate(world, g1, rep["receipt_id"], expected=g2)
    assert active_id(world) == g2


def test_rollback_target_without_engine_code_is_refused_and_prepared_rollback_can_be_aborted(world):
    manager(world)
    seed(world)
    g2, g1 = rebuild_to(world, "2")
    world.query("UPDATE generations SET engine_spec = jsonb_set(engine_spec, '{version}', '\"7\"') WHERE id = %s",
                (g1,))
    with raises("ENGINE_VERSION_UNAVAILABLE"):
        gens.prepare_rollback(world.config, world.b["principal_id"], g1, g2)
    world.query("UPDATE generations SET engine_spec = jsonb_set(engine_spec, '{version}', '\"1\"') WHERE id = %s",
                (g1,))
    gens.prepare_rollback(world.config, world.b["principal_id"], g1, g2)
    with raises("CANDIDATE_PENDING"):
        resume(world)
    out = gens.abort_generation(world.config, world.b["principal_id"], g1, g2)
    assert out["state"] == "retired" and out["write_gate"] == "open"
    assert active_id(world) == g2


# ---- empty memory and the CLI --------------------------------------------------------------

def test_empty_memory_can_be_rebuilt(world):
    manager(world)
    g2 = start(world, "2")["generation_id"]
    rep = compare(world, g2, ["anything"])
    assert rep["status"] == "ready" and rep["coverage"]["candidate"]["expected_sources"] == 0
    assert activate(world, g2, rep["receipt_id"])["active_generation_id"] == g2


def test_cli_rebuild_flow(world, monkeypatch, capsys, tmp_path):
    pid = manager(world)
    seed(world)
    monkeypatch.setenv("MEMORY_DATABASE_URL", world.dsn)
    monkeypatch.setenv("MEMORY_HMAC_KEY", "x" * 40)

    def cli(*args):
        admin.main(list(args))
        return json.loads(capsys.readouterr().out)
    g1 = active_id(world)
    st = cli("start-generation", "--principal", pid, "--expected-active", g1,
             "--engine-version", "2")
    g2 = st["generation_id"]
    world.drain()
    qf = tmp_path / "queries.json"
    qf.write_text(json.dumps(["banana", "singlefact"]), encoding="utf-8")
    rep = cli("compare-generation", "--principal", pid, "--generation", g2, "--queries-file", str(qf))
    assert rep["status"] == "ready"
    out = cli("activate-generation", "--principal", pid, "--generation", g2, "--expected-active", g1,
              "--receipt", rep["receipt_id"])
    assert out["active_generation_id"] == g2
    assert cli("resume-ingest", "--principal", pid, "--expected-active", g2)[
        "write_gate"] == "open"
    assert cli("generation-status", "--generation", g2)["is_active"] is True
