"""P0-5: real OS-process lifecycle against real PostgreSQL. A child process is SIGKILLed at a named point inside
the worker or an admin operation (no cleanup handlers run, connections just drop); the parent then checks what
state the system recovers to. Complements the in-process fault injection of P0-2..P0-4."""
import json
import os
import signal
import subprocess
import sys
import time
import uuid

import pytest

from memory_server import admin, generations as gens
from memory_server.worker import Worker

from conftest import HMAC_KEY
from test_p0_4_generations import MULTI, active_id, manager, seed

CHILD = r"""
import json, os, signal, sys
from memory_server import hooks, generations as gens
from memory_server.config import Config
from memory_server.worker import Worker
cfg = Config.from_env()
spec = json.loads(sys.argv[1])
hooks.register(spec["kill_at"], lambda: os.kill(os.getpid(), signal.SIGKILL))
if spec["action"] == "worker":
    Worker(cfg, "doomed-worker").process_one()
elif spec["action"] == "start_generation":
    gens.start_generation(cfg, spec["principal"], spec["namespace"], spec["operation"], spec["expected"], "2")
elif spec["action"] == "activate":
    gens.activate_generation(cfg, spec["principal"], spec["generation"], spec["expected"], spec["receipt"])
print("child survived")  # reaching here means the kill point was never hit
"""


def child_env(world):
    env = dict(os.environ, MEMORY_DATABASE_URL=world.dsn, MEMORY_HMAC_KEY=HMAC_KEY)
    env["PYTHONPATH"] = os.path.join(os.path.dirname(__file__), "..", "src")
    return env


def kill_child(world, **spec):
    proc = subprocess.run([sys.executable, "-c", CHILD, json.dumps(spec)], env=child_env(world),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
    assert proc.returncode == -signal.SIGKILL, (proc.returncode, proc.stdout, proc.stderr[-500:])


@pytest.mark.parametrize("point", ["worker_after_claim", "finalize_after_insert", "finalize_after_revalidate"])
def test_worker_sigkill_recovers_without_duplicate_artifacts(world, point):
    receipt = world.explicit(world.a["token"], "k1", "processkill marker alphaterm")
    kill_child(world, action="worker", kill_at=point)
    job = world.job(receipt["job_id"])
    if point == "worker_after_claim":
        assert job["state"] == "running" and job["attempt"] == 1  # lease held by a dead process
    else:
        assert world.artifacts() == []  # an uncommitted publication left nothing visible
    world.expire_lease(receipt["job_id"])
    world.make_due(receipt["job_id"])
    deadline = time.time() + 15
    while world.job(receipt["job_id"])["state"] != "succeeded" and time.time() < deadline:
        world.worker("survivor").process_one()
        world.make_due(receipt["job_id"])
    job = world.job(receipt["job_id"])
    assert job["state"] == "succeeded" and job["attempt"] == 2
    assert len(world.artifacts()) == 1
    assert len(world.search(world.b["token"], "alphaterm")["results"]) == 1


def test_worker_process_stops_cleanly_on_sigterm(world):
    receipt = world.explicit(world.a["token"], "k1", "sigterm marker betaterm")
    proc = subprocess.Popen([sys.executable, "-m", "memory_server.worker", "--poll-interval", "0.2"],
                            env=child_env(world), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True)
    try:
        deadline = time.time() + 20
        while world.job(receipt["job_id"])["state"] != "succeeded" and time.time() < deadline:
            time.sleep(0.2)
        assert world.job(receipt["job_id"])["state"] == "succeeded"   # a separate OS process did the work
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert "worker stopped" in proc.stderr.read()


def test_admin_killed_while_preparing_rebuild_leaves_no_half_generation(world):
    pid = manager(world)
    seed(world)
    before = active_id(world)
    op = str(uuid.uuid4())
    kill_child(world, action="start_generation", kill_at="generation_start_before_commit", principal=pid,
               namespace=world.ns, operation=op, expected=before)
    ns = world.query("SELECT write_gate, active_generation_id FROM namespaces WHERE id = %s", (world.ns,))[0]
    assert ns["write_gate"] == "open" and str(ns["active_generation_id"]) == before  # rolled back as a unit
    assert world.query("SELECT count(*) AS n FROM generations WHERE namespace_id = %s", (world.ns,))[0]["n"] == 1
    again = gens.start_generation(world.config, pid, world.ns, op, before, "2")   # the same operation id retries
    assert again["generation_id"]


def test_activation_killed_before_commit_then_safely_retried(world):
    pid = manager(world)
    seed(world)
    before = active_id(world)
    g2 = gens.start_generation(world.config, pid, world.ns, str(uuid.uuid4()), before, "2")["generation_id"]
    world.drain()
    report = gens.compare_generation(world.config, pid, g2, ["banana", "explicitfact"])
    assert report["status"] == "ready"
    kill_child(world, action="activate", kill_at="activate_before_commit", principal=pid, generation=g2,
               expected=before, receipt=report["receipt_id"])
    assert active_id(world) == before                                   # the client never got an answer; nothing moved
    assert world.query("SELECT write_gate FROM namespaces WHERE id = %s", (world.ns,))[0]["write_gate"] == "paused"
    out = gens.activate_generation(world.config, pid, g2, before, report["receipt_id"])  # retry succeeds once
    assert out["active_generation_id"] == g2
    assert len(world.search(world.a["token"], "explicitfact")["results"]) >= 1
