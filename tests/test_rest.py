"""REST adapter over a real HTTP socket (§9.1, §9.2)."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from memory_server.rest import make_server


@pytest.fixture
def base_url(world):
    server = make_server(world.config, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % server.server_address[1]
    server.shutdown()
    server.server_close()


def call(base, method, path, token=None, key=None, body=None, raw=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8")), exc.headers


def test_submit_replay_and_reads(world, base_url):
    body = world.body("REST로 접수한 본문")
    st, doc, _ = call(base_url, "POST", "/v1/experiences", world.a["token"], "r1", body)
    assert st == 202 and doc["replayed"] is False and doc["request_id"].startswith("req-")
    st2, doc2, _ = call(base_url, "POST", "/v1/experiences", world.a["token"], "r1", body)
    assert st2 == 200 and doc2["replayed"] is True and doc2["experience_id"] == doc["experience_id"]
    st, job, _ = call(base_url, "GET", "/v1/jobs/" + doc["job_id"], world.b["token"])
    assert st == 200 and job["state"] == "queued"
    st, exp, _ = call(base_url, "GET", "/v1/experiences/" + doc["experience_id"], world.b["token"])
    assert st == 200 and exp["content"]["text"] == "REST로 접수한 본문"


def test_error_mapping(world, base_url):
    body = world.body()
    st, doc, hdrs = call(base_url, "POST", "/v1/experiences", None, "e1", body)
    assert st == 401 and hdrs["WWW-Authenticate"] == "Bearer" and doc["error"]["code"] == "UNAUTHENTICATED"
    assert call(base_url, "POST", "/v1/experiences", world.a["token"], "e2", raw=b"{not json")[0] == 400
    assert call(base_url, "POST", "/v1/experiences", world.a["token"], None, body)[1]["error"]["code"] \
        == "INVALID_IDEMPOTENCY_KEY"
    st, doc, _ = call(base_url, "POST", "/v1/experiences", world.a["token"], "e3", world.body("x" * 70000))
    assert st == 413
    st, doc, _ = call(base_url, "POST", "/v1/experiences", world.a["token"], "e4", raw=b" " * (97 * 1024))
    assert st == 413 and doc["error"]["code"] == "PAYLOAD_TOO_LARGE"
    call(base_url, "POST", "/v1/experiences", world.a["token"], "e5", body)
    st, doc, _ = call(base_url, "POST", "/v1/experiences", world.a["token"], "e5", world.body("다른 본문"))
    assert st == 409 and doc["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert call(base_url, "GET", "/v1/nope", world.a["token"])[0] == 404


def test_paused_gate_returns_503_with_retry_after(world, base_url):
    world.query("UPDATE namespaces SET write_gate = 'paused' WHERE id = %s", (world.ns,))
    st, doc, hdrs = call(base_url, "POST", "/v1/experiences", world.a["token"], "p1", world.body())
    assert st == 503 and doc["error"]["retryable"] is True and hdrs["Retry-After"] == "5"


def test_deleted_experience_410_over_rest(world, base_url):
    _, doc, _ = call(base_url, "POST", "/v1/experiences", world.a["token"], "d1", world.body())
    world.query("UPDATE experiences SET state='deleted', raw_text=NULL, source_metadata=NULL, "
                "content_digest=NULL, deleted_at=now() WHERE id=%s", (doc["experience_id"],))
    assert call(base_url, "GET", "/v1/experiences/" + doc["experience_id"], world.a["token"])[0] == 410
    assert call(base_url, "POST", "/v1/experiences", world.a["token"], "d1", world.body())[0] == 410


def test_search_over_rest(world, base_url):
    world.explicit(world.a["token"], "rs1", "REST 검색 대상 memo_alpha")
    world.drain()
    st, doc, _ = call(base_url, "POST", "/v1/memories/search", world.reader["token"],
                      body={"namespace_id": world.ns, "query": "memo_alpha"})
    assert st == 200 and len(doc["results"]) == 1 and doc["content_trust"] == "untrusted_data"
    assert doc["request_id"].startswith("req-")
    st, doc, _ = call(base_url, "POST", "/v1/memories/search", world.reader["token"],
                      body={"namespace_id": world.work["namespace_id"], "query": "memo_alpha"})
    assert st == 404
    st, doc, _ = call(base_url, "POST", "/v1/memories/search", world.a["token"],
                      body={"namespace_id": world.ns, "query": "x", "keywords": []})
    assert st == 400
    assert call(base_url, "POST", "/v1/memories/search", None, body={})[0] == 401
