"""P0-5 end-to-end against the REAL Docker Compose deployment (TLS proxy, API/MCP, Worker, PostgreSQL), with real
container kills and restarts and the official MCP client over https. Opt-in: needs Docker, `scripts/gen-secrets.sh`,
`scripts/dev-certs.sh` and `.env` (MEMORY_ALLOWED_HOSTS=localhost); synthetic data only; it STOPS and RESTARTS the
stack of the current directory, so never point it at a stack holding real data.

    E2E_COMPOSE=1 python3 -m pytest -q tests/test_p0_5_compose_e2e.py
"""
import asyncio
import json
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("E2E_COMPOSE") != "1", reason="set E2E_COMPOSE=1 (needs Docker stack)")
pytest.importorskip("mcp")
import httpx2  # noqa: E402
from mcp import Client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
BASE = "https://localhost:8443"
CA = os.path.join(ROOT, "certs", "ca.crt")


def dc(*args, check=True, timeout=240):
    proc = subprocess.run(["docker", "compose"] + list(args), cwd=ROOT, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise AssertionError("docker compose %s failed: %s" % (" ".join(args), proc.stderr[-800:]))
    return proc


def admin(*args):
    proc = dc("--profile", "tools", "run", "--rm", "-T", "admin", *args)
    return json.loads(proc.stdout[proc.stdout.index("{"):])


def psql(sql):
    return dc("exec", "-T", "db", "psql", "-U", "postgres", "-d", "memory", "-At", "-c", sql).stdout.strip()


def wait_ready(timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        proc = dc("exec", "-T", "api", "python3", "-c",
                  "import urllib.request as u;u.urlopen(u.Request('http://127.0.0.1:8080/readyz',"
                  "headers={'Host':'localhost'}),timeout=3)", check=False)
        if proc.returncode == 0:
            return
        time.sleep(1)
    raise AssertionError("stack did not become ready")


def mcp(token, calls, ca=CA):
    async def go():
        http = httpx2.AsyncClient(headers={"Authorization": "Bearer " + token},
                                  verify=ssl.create_default_context(cafile=ca))
        async with Client(streamable_http_client(BASE + "/mcp", http_client=http)) as client:
            return [await client.call_tool(n, a) for n, a in calls]
    return asyncio.run(go())


def tool(token, name, args):
    return mcp(token, [(name, args)])[0]


def wait_job(token, job_id, want="succeeded", timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = tool(token, "memory_job_status", {"job_id": job_id}).structured_content
        if job["state"] == want:
            return job
        time.sleep(0.5)
    raise AssertionError("job did not reach %s: %s" % (want, job))


@pytest.fixture(scope="module")
def stack():
    dc("up", "-d", "--wait", timeout=300)
    wait_ready()
    tag = uuid.uuid4().hex[:6]
    ns = admin("create-namespace", "--name", "e2e-" + tag)["namespace_id"]
    dev = {}
    for name in ("a", "b", "mgr"):
        dev[name] = admin("create-principal", "--name", "%s-%s" % (name, tag))
        flags = ["--read", "--write"] + (["--manage"] if name == "mgr" else [])
        admin("grant", "--principal", dev[name]["principal_id"], "--namespace", ns, *flags)
    return {"ns": ns, "dev": dev, "tag": tag}


def submit(stack, who, key, text, **extra):
    args = {"namespace_id": stack["ns"], "text": text, "idempotency_key": key, "intent": "explicit_memory",
            "actor_reported": "user"}
    args.update(extra)
    return tool(stack["dev"][who]["token"], "memory_submit", args)


def search(stack, who, query):
    res = tool(stack["dev"][who]["token"], "memory_search", {"namespace_id": stack["ns"], "query": query})
    return res


# --- transport security ------------------------------------------------------------------
def test_only_tls_is_served_and_internal_ports_are_not_published(stack):
    ctx = ssl.create_default_context(cafile=CA)
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(urllib.request.Request(BASE + "/v1/jobs/" + str(uuid.uuid4())), context=ctx)
    assert err.value.code == 401                                            # TLS works, auth is enforced
    with pytest.raises(urllib.error.URLError):                              # an unknown CA must be rejected by clients
        urllib.request.urlopen(BASE + "/v1/jobs/x", context=ssl.create_default_context())
    plain = subprocess.run(["curl", "-s", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}",
                            "http://localhost:8443/v1/jobs/x"], stdout=subprocess.PIPE, universal_newlines=True)
    assert plain.stdout in ("400", "000")                                   # plaintext HTTP is never served
    ports = subprocess.run(["docker", "ps", "--filter", "label=com.docker.compose.project=memory", "--format",
                            "{{.Names}} {{.Ports}}"], stdout=subprocess.PIPE, universal_newlines=True).stdout
    published = [l for l in ports.splitlines() if "->" in l]
    assert len(published) == 1 and "memory-proxy-1" in published[0] and "127.0.0.1:8443->8443" in published[0]
    for hidden in ("/healthz", "/readyz"):                                  # operational endpoints stay internal
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(BASE + hidden, context=ctx)
        assert err.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as err:                      # a wrong Host is refused by the app
        urllib.request.urlopen(urllib.request.Request(BASE + "/v1/jobs/x", headers={"Host": "evil.example"}),
                               context=ctx)
    assert err.value.code in (404, 421)
    assert psql("SELECT rolsuper FROM pg_roles WHERE rolname = 'memory_app'") == "f"
    assert dc("exec", "-T", "api", "id", "-u").stdout.strip() == "10001"   # not root


# --- store / recall / revoke across real containers -----------------------------------------
def test_store_recall_source_and_revocation_over_https_mcp(stack):
    sub = submit(stack, "a", "e2e-1", "우리 프로젝트의 배포 DB는 PostgreSQL이다 e2e_marker", title="배포 DB",
                 retrieval_hints=["database"])
    assert not sub.is_error and sub.structured_content["status"] == "accepted"
    wait_job(stack["dev"]["a"]["token"], sub.structured_content["job_id"])    # the worker CONTAINER did the work
    hit = search(stack, "b", "e2e_marker").structured_content["results"]      # other device, other session
    assert len(hit) == 1 and hit[0]["engine_data"]["verification"] == "not_verified"
    src = tool(stack["dev"]["b"]["token"], "memory_get_source",
               {"experience_id": hit[0]["sources"][0]["experience_id"]}).structured_content
    assert "PostgreSQL" in src["content"]["text"] and src["content_trust"] == "untrusted_data"
    admin("revoke-principal", "--principal", stack["dev"]["a"]["principal_id"])
    with pytest.raises(Exception):
        search(stack, "a", "e2e_marker")                                      # revoked device: refused at transport
    assert len(search(stack, "b", "e2e_marker").structured_content["results"]) == 1   # B keeps access


# --- process / container lifecycle ----------------------------------------------------------
def test_worker_killed_with_a_claimed_job_is_recovered(stack):
    tok = stack["dev"]["b"]["token"]
    dc("stop", "worker")                                                      # keep the real worker out of the way
    sub = submit(stack, "b", "e2e-kill", "kill9 claimed marker killterm")
    jid = sub.structured_content["job_id"]
    claimer = ("import time\nfrom memory_server.config import Config\nfrom memory_server.worker import Worker\n"
               "c = Worker(Config.from_env(), 'doomed').claim()\nprint('claimed', c.attempt, flush=True)\n"
               "time.sleep(600)\n")
    dc("run", "-d", "--no-deps", "--name", "e2e-claimer", "--entrypoint", "python3", "worker", "-c", claimer)
    try:
        for _ in range(40):
            if "claimed" in subprocess.run(["docker", "logs", "e2e-claimer"], stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, universal_newlines=True).stdout:
                break
            time.sleep(0.5)
        assert wait_job(tok, jid, "running", timeout=5)["state"] == "running"
        subprocess.run(["docker", "kill", "-s", "KILL", "e2e-claimer"], check=True, stdout=subprocess.PIPE)
    finally:
        subprocess.run(["docker", "rm", "-f", "e2e-claimer"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    dc("start", "worker")                                                     # lease (30 s) expires, worker re-claims
    job = wait_job(tok, jid, timeout=90)
    assert job["state"] == "succeeded"
    assert psql("SELECT attempt FROM jobs WHERE id = '%s'" % jid) == "2"
    assert psql("SELECT count(*) FROM artifacts WHERE job_id = '%s' AND state = 'valid'" % jid) == "1"
    assert len(search(stack, "b", "killterm").structured_content["results"]) == 1


def test_work_queued_while_worker_down_completes_after_restart(stack):
    dc("kill", "-s", "KILL", "worker")                                        # SIGKILL of the real worker container
    sub = submit(stack, "b", "e2e-queued", "queued while down marker queuedterm")
    time.sleep(2)
    assert tool(stack["dev"]["b"]["token"], "memory_job_status",
                {"job_id": sub.structured_content["job_id"]}).structured_content["state"] == "queued"
    dc("up", "-d", "worker")
    wait_job(stack["dev"]["b"]["token"], sub.structured_content["job_id"])


def test_api_and_database_restarts_keep_data_and_degrade_safely(stack):
    dc("restart", "api")
    wait_ready()
    assert len(search(stack, "b", "queuedterm").structured_content["results"]) == 1
    dc("stop", "db")
    req = urllib.request.Request(BASE + "/v1/memories/search", method="POST",
                                 data=json.dumps({"namespace_id": stack["ns"], "query": "queuedterm"}).encode(),
                                 headers={"Authorization": "Bearer " + stack["dev"]["b"]["token"],
                                          "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as err:                         # DB is down: a safe, retryable 503
        urllib.request.urlopen(req, context=ssl.create_default_context(cafile=CA), timeout=20)
    body = err.value.read().decode()
    assert err.value.code == 503 and json.loads(body)["error"]["code"] == "DB_UNAVAILABLE"
    assert "Traceback" not in body and "psycopg2" not in body and "172.29" not in body
    with pytest.raises(Exception):                                             # MCP clients see the transport 503
        search(stack, "b", "queuedterm")
    dc("start", "db")
    dc("up", "-d", "--wait", "db", "api", "worker", timeout=200)
    wait_ready()
    assert len(search(stack, "b", "queuedterm").structured_content["results"]) == 1   # persisted across the outage
    # PostgreSQL was SIGTERM-ed by `stop`; the named volume kept all rows.


def test_full_stack_restart_while_input_gate_is_paused(stack):
    mgr = stack["dev"]["mgr"]["principal_id"]
    active = psql("SELECT active_generation_id FROM namespaces WHERE id = '%s'" % stack["ns"])
    gen = admin("start-generation", "--principal", mgr, "--namespace", stack["ns"], "--expected-active", active,
                "--engine-version", "2")["generation_id"]
    assert psql("SELECT write_gate FROM namespaces WHERE id = '%s'" % stack["ns"]) == "paused"
    dc("down")                                                               # whole stack; volumes are kept
    dc("up", "-d", "--wait", timeout=300)
    wait_ready()
    assert psql("SELECT write_gate FROM namespaces WHERE id = '%s'" % stack["ns"]) == "paused"
    res = submit(stack, "b", "e2e-paused", "must wait pausedterm")
    assert res.is_error and res.structured_content["error"]["retryable"] is True   # gate survived the restart
    admin("abort-generation", "--principal", mgr, "--generation", gen, "--expected-active", active)
    ok = submit(stack, "b", "e2e-paused", "must wait pausedterm")                 # same key, now accepted
    assert not ok.is_error
    wait_job(stack["dev"]["b"]["token"], ok.structured_content["job_id"])


# --- logs never contain secrets ------------------------------------------------------------------
def test_no_secret_material_in_container_logs(stack):
    logs = dc("logs", "--no-color").stdout + dc("--profile", "tools", "logs", "--no-color", check=False).stdout
    secrets = [stack["dev"][n]["token"] for n in stack["dev"]]
    for name in ("hmac_key", "db_app_password", "db_owner_password", "db_superuser_password", "db_backup_password",
                 "backup_passphrase"):
        secrets.append(open(os.path.join(ROOT, "secrets", name)).read().strip())
    leaked = [s[:6] for s in secrets if s and s in logs]
    assert leaked == [] and "Authorization" not in logs


# --- backup, quarantined restore, ledger replay (containers) ------------------------------------------------
def test_backup_and_isolated_restore_do_not_resurrect_deleted_or_revoked(stack, tmp_path):
    backups = os.path.join(ROOT, "backups")
    os.makedirs(backups, exist_ok=True)
    os.chown(backups, 10001, 10001)
    run = uuid.uuid4().hex[:8]                                                # the stack outlives a single test run
    keepterm, goneterm = "keepterm" + run, "goneterm" + run
    keep = submit(stack, "b", "bk-keep-" + run, "backupkeep marker " + keepterm)
    gone = submit(stack, "b", "bk-gone-" + run, "backupgone marker " + goneterm)
    for r in (keep, gone):
        wait_job(stack["dev"]["b"]["token"], r.structured_content["job_id"])
    rec = ["--profile", "tools", "run", "--rm", "-T", "recovery"]
    out = dc(*rec, "backup", "--passphrase-file", "/run/secrets/backup_passphrase", "--out-dir", "/backups").stdout
    archive = os.path.basename(json.loads(out[out.index("{"):])["archive"])
    assert subprocess.run(["grep", "-c", goneterm, os.path.join(backups, archive)], stdout=subprocess.PIPE
                          ).stdout.strip() == b"0"                            # encrypted at rest

    # Policy changes AFTER the backup.
    exp = psql("SELECT id FROM experiences WHERE raw_text LIKE '%" + goneterm + "%'")
    admin("delete-experience", "--experience", exp)
    victim = stack["dev"]["mgr"]["principal_id"]
    admin("revoke-principal", "--principal", victim)

    # Restore into a NEW database as the schema owner; the live database is never touched.
    psql("DROP DATABASE IF EXISTS memory_restore")
    psql("CREATE DATABASE memory_restore OWNER memory_owner TEMPLATE template0 ENCODING 'UTF8'")
    owner_pw = open(os.path.join(ROOT, "secrets", "db_owner_password")).read().strip()
    url_file = os.path.join(ROOT, "secrets", "restore_database_url")
    open(url_file, "w").write("postgresql://memory_owner:%s@db:5432/memory_restore" % owner_pw)
    os.chmod(url_file, 0o644)
    mount = ["-v", url_file + ":/run/secrets/restore_database_url:ro"]
    tgt = ["--target-database-url-file", "/run/secrets/restore_database_url"]
    try:
        restored = dc(*rec[:3], *mount, *rec[3:], "restore", "--archive", "/backups/" + archive,
                      "--passphrase-file", "/run/secrets/backup_passphrase", *tgt).stdout
        assert json.loads(restored[restored.index("{"):])["state"] == "quarantined"
        # Raw restore re-exposes the deleted text and the revoked principal: this is what recovery must undo.
        assert psql_db("memory_restore", "SELECT count(*) FROM experiences WHERE raw_text LIKE '%" + goneterm + "%'") == "1"
        assert psql_db("memory_restore", "SELECT service_state FROM system_control") == "quarantined"
        verify = dc(*rec[:3], *mount, *rec[3:], "verify", *tgt, check=False)
        assert verify.returncode == 3                                         # verification fails before replay
        dc(*rec[:3], *mount, *rec[3:], "reapply", *tgt)
        dc(*rec[:3], *mount, *rec[3:], "verify", *tgt)
        refused = dc(*rec[:3], *mount, *rec[3:], "release", *tgt, check=False)
        assert refused.returncode == 2                                        # no attestation, no release
        assert psql_db("memory_restore", "SELECT count(*) FROM experiences WHERE raw_text LIKE '%" + goneterm + "%'") == "0"
        assert psql_db("memory_restore", "SELECT count(*) FROM principals WHERE id = '%s' AND revoked_at IS NOT NULL"
                       % victim) == "1"
        assert psql_db("memory_restore", "SELECT service_state FROM system_control") == "quarantined"
    finally:
        os.remove(url_file)
        psql("DROP DATABASE IF EXISTS memory_restore")
    # The live database kept serving its own current state throughout.
    assert len(search(stack, "b", keepterm).structured_content["results"]) == 1
    assert search(stack, "b", goneterm).structured_content["results"] == []


def psql_db(db, sql):
    return dc("exec", "-T", "db", "psql", "-U", "postgres", "-d", db, "-At", "-c", sql).stdout.strip()
