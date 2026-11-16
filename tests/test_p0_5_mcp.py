"""P0-5: MCP over Streamable HTTP with the official MCP client, against the real ASGI app and real PostgreSQL.

Covers: initialize (both protocol generations), the exact tool surface, store -> job -> search -> source,
REST/MCP business equivalence, transport auth, per-call revocation, principal permissions, edge hardening,
error-leak behavior and the recovery quarantine gate.
"""
import asyncio
import json
import logging
import urllib.error
import urllib.request

import pytest

pytest.importorskip("mcp")
import httpx2  # noqa: E402
from mcp import Client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from httpserver import RunningServer  # noqa: E402
from memory_server.errors import ServiceError  # noqa: E402
from memory_server.operations import revoke_principal  # noqa: E402
from memory_server.service import MemoryService  # noqa: E402

TOOLS = {"memory_submit", "memory_job_status", "memory_search", "memory_get_source"}


@pytest.fixture
def server(world):
    with RunningServer(world.config) as running:
        yield running


def mcp_call(server, token, calls, mode="auto"):
    """Open one real MCP session and run [(tool, args), ...]; returns the list of CallToolResult."""
    async def go():
        headers = {"Authorization": "Bearer " + token} if token else {}
        http = httpx2.AsyncClient(headers=headers)
        async with Client(streamable_http_client(server.base + "/mcp", http_client=http), mode=mode) as client:
            return [await client.call_tool(name, args) for name, args in calls]
    return asyncio.run(go())


def one(server, token, name, args, mode="auto"):
    return mcp_call(server, token, [(name, args)], mode)[0]


def raw(server, method, path, token=None, body=None, headers=None, data=None):
    hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        hdrs["Authorization"] = "Bearer " + token
    hdrs.update(headers or {})
    if data is None and body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(server.base + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode("utf-8"), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), exc.headers


def submit_args(world, text, key, **extra):
    args = {"text": text, "idempotency_key": key}
    args.update(extra)
    return args


# --- protocol and surface -------------------------------------------------
@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_initialize_and_tool_surface(world, server, mode):
    async def go():
        http = httpx2.AsyncClient(headers={"Authorization": "Bearer " + world.a["token"]})
        async with Client(streamable_http_client(server.base + "/mcp", http_client=http), mode=mode) as client:
            return client.session.protocol_version, (await client.list_tools()).tools
    version, tools = asyncio.run(go())
    assert version in ("2026-07-28", "2025-11-25")
    assert {t.name for t in tools} == TOOLS  # no admin delete/revoke/retry/generation tools
    assert all("namespace_id" not in t.input_schema["properties"] for t in tools)
    submit = next(t for t in tools if t.name == "memory_submit")
    assert "ACCEPTED" in submit.description and "evidence_refs" in submit.description
    assert next(t for t in tools if t.name == "memory_search").annotations.read_only_hint is True


# --- store -> job -> search -> source over MCP ----------------------------
def test_explicit_memory_roundtrip_over_mcp(world, server):
    text = "우리 프로젝트의 배포 DB는 PostgreSQL이다 (deploy_db)"
    sub = one(server, world.a["token"], "memory_submit", submit_args(
        world, text, "m1", intent="explicit_memory", actor_reported="agent", title="배포 DB",
        retrieval_hints=["database", "postgres"], source_kind="agent_note", source_ref="chat:1"))
    assert not sub.is_error and sub.structured_content["status"] == "accepted"
    assert sub.structured_content["searchability"] == "not_guaranteed"  # accepted is not "memory created"
    job_id = sub.structured_content["job_id"]

    # Accepted, not yet processed: not searchable, status says why.
    status = one(server, world.a["token"], "memory_job_status", {"job_id": job_id})
    assert status.structured_content["state"] == "queued" and status.structured_content["currently_visible"] is False
    miss = one(server, world.a["token"], "memory_search", {"query": "PostgreSQL"})
    assert miss.structured_content["results"] == []

    world.drain()

    done = one(server, world.b["token"], "memory_job_status", {"job_id": job_id})  # another device
    assert done.structured_content["state"] == "succeeded" and done.structured_content["currently_visible"] is True
    hit = one(server, world.b["token"], "memory_search", {"query": "배포 DB PostgreSQL"})
    results = hit.structured_content["results"]
    assert results and results[0]["text"] == text  # explicit memory text preserved verbatim
    assert json.loads(hit.content[0].text) == hit.structured_content
    top = results[0]
    assert hit.structured_content["content_trust"] == "untrusted_data"
    assert hit.structured_content["engine"].startswith("reference/")  # processing version stays visible
    assert top["engine_data"]["representation"] == "explicit_memory"
    assert top["engine_data"]["verification"] != "verified" and top["engine_data"]["actor_basis"] == "client_reported"
    assert top["sources"][0]["locator"] == {"kind": "agent_note", "source_ref": "chat:1", "verified": False}
    assert {"title", "hints"} & set(top["engine_data"]["matched_fields"]) or top["engine_data"]["matched_keywords"]
    exp_id = top["sources"][0]["experience_id"]
    assert exp_id == sub.structured_content["experience_id"]
    src = one(server, world.b["token"], "memory_get_source", {"experience_id": exp_id})
    assert src.structured_content["content"]["text"] == text
    assert src.structured_content["client_reported"]["actor_basis"] == "client_reported"
    assert src.structured_content["content_trust"] == "untrusted_data"


def test_evidence_refs_still_unsupported_over_mcp(world, server):
    res = one(server, world.a["token"], "memory_submit",
              submit_args(world, "x", "ev1", evidence_refs=[{"experience_id": world.active_generation_id}]))
    assert res.is_error and res.structured_content["error"]["code"] == "UNSUPPORTED_FIELD"
    assert world.counts()["experiences"] == 0


# --- REST/MCP business equivalence ----------------------------------------
def test_rest_and_mcp_return_the_same_business_results(world, server):
    body = world.body("동등성 검증 문장 equivalence_marker", submission={"intent": "explicit_memory",
                                                                          "actor_reported": "user"})
    st, doc, _ = raw(server, "POST", "/v2/experiences", world.a["token"], body, {"Idempotency-Key": "eq1"})
    rest_receipt = json.loads(doc)
    assert st == 202
    replay = one(server, world.a["token"], "memory_submit", submit_args(
        world, "동등성 검증 문장 equivalence_marker", "eq1", intent="explicit_memory", actor_reported="user"))
    assert replay.structured_content["replayed"] is True
    assert replay.structured_content["experience_id"] == rest_receipt["experience_id"]
    world.drain()

    st, doc, _ = raw(server, "POST", "/v2/memories/search", world.b["token"],
                     {"query": "equivalence_marker"})
    rest_search = json.loads(doc)
    mcp_search = one(server, world.b["token"], "memory_search",
                     {"query": "equivalence_marker"}).structured_content
    rest_search.pop("request_id")
    assert rest_search == mcp_search and rest_search["results"]

    # Same errors, same codes and statuses.
    cases = [
        ("memory_search", {"query": "x"}, None),  # authenticated, no read permission
        ("memory_get_source", {"experience_id": "not-a-uuid"}, None),
    ]
    st, doc, _ = raw(server, "POST", "/v2/memories/search", world.outsider["token"], {"query": "x"})
    res = one(server, world.outsider["token"], *cases[0][:2])
    assert (st, json.loads(doc)["error"]["code"]) == (res.structured_content["error"]["status"],
                                                      res.structured_content["error"]["code"]) == (404, "NOT_FOUND")
    st, doc, _ = raw(server, "GET", "/v2/experiences/not-a-uuid", world.b["token"])
    res = one(server, world.b["token"], *cases[1][:2])
    assert st == 404 and res.is_error and res.structured_content["error"]["code"] == "NOT_FOUND"


def test_paused_gate_is_retryable_over_mcp(world, server):
    world.pause_ingest()
    res = one(server, world.a["token"], "memory_submit", submit_args(world, "x", "pg1"))
    err = res.structured_content["error"]
    assert res.is_error and err["code"] == "INGEST_PAUSED" and err["retryable"] is True and err["status"] == 503


# --- authentication, revocation, isolation --------------------------------
def test_missing_or_invalid_credential_is_rejected_at_the_transport(world, server):
    init = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    for token in (None, "mem_not-a-real-token"):
        st, body, hdrs = raw(server, "POST", "/mcp", token, init)
        assert st == 401 and hdrs["WWW-Authenticate"] == "Bearer"
        assert json.loads(body)["error"]["code"] == "UNAUTHENTICATED"
    with pytest.raises(Exception):
        one(server, None, "memory_search", {"query": "x"})


def test_proxy_identity_headers_are_not_authentication(world, server):
    init = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    st, _, _ = raw(server, "POST", "/mcp", None, init,
                   {"X-Forwarded-User": "device-a", "X-Authenticated-User": world.a["principal_id"],
                    "Remote-User": "admin"})
    assert st == 401


def test_revoking_a_device_blocks_its_next_call_but_not_the_other_device(world, server):
    world.explicit(world.a["token"], "rv1", "공유 메모리 shared_marker")
    world.drain()

    async def go():
        http = httpx2.AsyncClient(headers={"Authorization": "Bearer " + world.a["token"]})
        async with Client(streamable_http_client(server.base + "/mcp", http_client=http)) as client:
            args = {"query": "shared_marker"}
            first = await client.call_tool("memory_search", args)  # same open session...
            await asyncio.to_thread(revoke_principal, world.config, world.a["principal_id"])
            try:
                await client.call_tool("memory_search", args)  # ...is re-authorized per call
            except Exception as exc:
                return first, exc
            return first, None
    first, failure = asyncio.run(go())
    assert first.structured_content["results"] and failure is not None
    again = one(server, world.b["token"], "memory_search", {"query": "shared_marker"})
    assert again.structured_content["results"]  # B keeps its own access to the shared data
    st, _, _ = raw(server, "GET", "/v2/jobs/" + world.query("SELECT id FROM jobs LIMIT 1")[0]["id"], world.a["token"])
    assert st == 401


def test_read_only_principal_shares_memory_but_cannot_submit(world, server):
    denied = one(server, world.reader["token"], "memory_submit", submit_args(world, "x", "iso1"))
    assert denied.is_error and denied.structured_content["error"]["code"] == "NOT_FOUND"
    world.explicit(world.a["token"], "iso3", "공유 메모리 shared_marker")
    world.drain()
    for principal in (world.b, world.reader):
        found = one(server, principal["token"], "memory_search", {"query": "shared_marker"})
        assert len(found.structured_content["results"]) == 1
    assert world.counts()["experiences"] == 1


def test_legacy_selector_rejected_even_with_valid_v2_mcp_arguments(world, server):
    args = submit_args(world, "legacy", "legacy-key", namespace_id=world.active_generation_id)
    for tool, arguments in (
        ("memory_submit", args),
        ("memory_search", {"query": "legacy", "namespace_id": world.active_generation_id}),
    ):
        denied = one(server, world.a["token"], tool, arguments)
        assert denied.is_error and denied.structured_content["error"]["code"] == "UNKNOWN_FIELD"
    assert world.counts()["experiences"] == 0


# --- edge hardening --------------------------------------------------------
def test_host_origin_size_and_malformed_requests(world, server):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    assert raw(server, "POST", "/mcp", world.a["token"], msg, {"Host": "evil.example"})[0] == 421
    assert raw(server, "GET", "/v2/jobs/x", world.a["token"], headers={"Host": "evil.example"})[0] == 421
    assert raw(server, "POST", "/mcp", world.a["token"], msg, {"Origin": "http://evil.example"})[0] == 403
    st, body, _ = raw(server, "POST", "/mcp", world.a["token"], data=b" " * (97 * 1024))
    assert st == 413 and json.loads(body)["error"]["code"] == "PAYLOAD_TOO_LARGE"
    st, body, _ = raw(server, "POST", "/mcp", world.a["token"], data=b"{broken")
    assert st == 400 and "Traceback" not in body
    assert raw(server, "POST", "/v2/experiences", world.a["token"], data=b"{broken",
               headers={"Idempotency-Key": "x"})[0] == 400


def test_failed_auth_throttling_blocks_the_client_not_the_service(world):
    with RunningServer(world.config, auth_fail_limit=3) as server:
        for _ in range(3):
            assert raw(server, "GET", "/v2/jobs/" + world.active_generation_id, "mem_wrong")[0] == 401
        st, body, hdrs = raw(server, "GET", "/v2/jobs/" + world.active_generation_id, "mem_wrong")
        assert st == 429 and hdrs["Retry-After"] == "30"
        assert raw(server, "GET", "/healthz")[0] == 200  # health stays reachable


def test_health_and_readiness(world, server):
    assert raw(server, "GET", "/healthz")[0] == 200
    st, body, _ = raw(server, "GET", "/readyz")
    assert st == 200 and json.loads(body) == {"status": "ready"}


# --- information hygiene ---------------------------------------------------
def test_internal_errors_and_credentials_do_not_leak(world, server, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    secret = world.a["token"]

    def boom(*a, **k):
        raise RuntimeError("db password=hunter2 host=internal-db")
    monkeypatch.setattr(MemoryService, "search", boom)
    res = one(server, secret, "memory_search", {"query": "x"})
    text = json.dumps(res.structured_content) + res.content[0].text
    assert res.is_error and res.structured_content["error"]["code"] == "INTERNAL"
    assert "hunter2" not in text and "Traceback" not in text and "internal-db" not in text
    st, body, _ = raw(server, "POST", "/v2/memories/search", secret, {"query": "x"})
    assert st == 500 and "hunter2" not in body and "Traceback" not in body
    raw(server, "GET", "/v2/jobs/" + world.active_generation_id, "mem_wrongwrongwrong")
    logged = "\n".join(r.getMessage() + (str(r.exc_info[1]) if r.exc_info else "") for r in caplog.records)
    assert secret not in logged and "mem_wrongwrongwrong" not in logged
    assert "Authorization" not in logged


# --- recovery quarantine (fail closed) -------------------------------------
def test_quarantine_blocks_every_serving_path(world, server):
    world.query("UPDATE system_control SET service_state = 'quarantined'")
    try:
        st, body, hdrs = raw(server, "POST", "/mcp", world.a["token"],
                             {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        assert st == 503 and json.loads(body)["error"]["code"] == "RECOVERY_QUARANTINE"
        assert hdrs["Retry-After"] == "30"
        st, body, _ = raw(server, "POST", "/v2/experiences", world.a["token"], world.body(),
                          {"Idempotency-Key": "q1"})
        assert st == 503 and json.loads(body)["error"]["code"] == "RECOVERY_QUARANTINE"
        st, body, _ = raw(server, "GET", "/readyz")
        assert st == 503 and json.loads(body)["reason"] == "quarantined"
        assert world.counts()["experiences"] == 0
        with pytest.raises(ServiceError) as exc:  # workers do not claim while quarantined
            world.worker().process_one()
        assert exc.value.code == "RECOVERY_QUARANTINE"
    finally:
        world.query("UPDATE system_control SET service_state = 'serving'")
