"""REST adapter over a real HTTP socket (§9.1, §9.2)."""
import json
import urllib.error
import urllib.request

import pytest

pytest.importorskip("starlette")  # the HTTP adapter needs the pinned ASGI stack (not in the legacy image)
from httpserver import RunningServer  # noqa: E402


@pytest.fixture
def base_url(world):
    with RunningServer(world.config) as running:
        yield running.base


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
    st, doc, _ = call(base_url, "POST", "/v2/experiences", world.a["token"], "r1", body)
    assert st == 202 and doc["replayed"] is False and doc["request_id"].startswith("req-")
    st2, doc2, _ = call(base_url, "POST", "/v2/experiences", world.a["token"], "r1", body)
    assert st2 == 200 and doc2["replayed"] is True and doc2["experience_id"] == doc["experience_id"]
    st, job, _ = call(base_url, "GET", "/v2/jobs/" + doc["job_id"], world.b["token"])
    assert st == 200 and job["state"] == "queued"
    st, exp, _ = call(base_url, "GET", "/v2/experiences/" + doc["experience_id"], world.b["token"])
    assert st == 200 and exp["content"]["text"] == "REST로 접수한 본문"


def test_error_mapping(world, base_url):
    body = world.body()
    st, doc, hdrs = call(base_url, "POST", "/v2/experiences", None, "e1", body)
    assert st == 401 and hdrs["WWW-Authenticate"] == "Bearer" and doc["error"]["code"] == "UNAUTHENTICATED"
    assert call(base_url, "POST", "/v2/experiences", world.a["token"], "e2", raw=b"{not json")[0] == 400
    assert call(base_url, "POST", "/v2/experiences", world.a["token"], None, body)[1]["error"]["code"] \
        == "INVALID_IDEMPOTENCY_KEY"
    st, doc, _ = call(base_url, "POST", "/v2/experiences", world.a["token"], "e3", world.body("x" * 70000))
    assert st == 413
    st, doc, _ = call(base_url, "POST", "/v2/experiences", world.a["token"], "e4", raw=b" " * (97 * 1024))
    assert st == 413 and doc["error"]["code"] == "PAYLOAD_TOO_LARGE"
    call(base_url, "POST", "/v2/experiences", world.a["token"], "e5", body)
    st, doc, _ = call(base_url, "POST", "/v2/experiences", world.a["token"], "e5", world.body("다른 본문"))
    assert st == 409 and doc["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert call(base_url, "GET", "/v2/nope", world.a["token"])[0] == 404


def test_paused_gate_returns_503_with_retry_after(world, base_url):
    world.pause_ingest()
    st, doc, hdrs = call(base_url, "POST", "/v2/experiences", world.a["token"], "p1", world.body())
    assert st == 503 and doc["error"]["retryable"] is True and hdrs["Retry-After"] == "5"


def test_deleted_experience_410_over_rest(world, base_url):
    _, doc, _ = call(base_url, "POST", "/v2/experiences", world.a["token"], "d1", world.body())
    world.query("UPDATE experiences SET state='deleted', raw_text=NULL, source_metadata=NULL, "
                "content_digest=NULL, deleted_at=now() WHERE id=%s", (doc["experience_id"],))
    assert call(base_url, "GET", "/v2/experiences/" + doc["experience_id"], world.a["token"])[0] == 410
    assert call(base_url, "POST", "/v2/experiences", world.a["token"], "d1", world.body())[0] == 410


def test_search_over_rest(world, base_url):
    world.explicit(world.a["token"], "rs1", "REST 검색 대상 memo_alpha")
    world.drain()
    st, doc, _ = call(base_url, "POST", "/v2/memories/search", world.reader["token"],
                      body={"query": "memo_alpha"})
    assert st == 200 and len(doc["results"]) == 1 and doc["content_trust"] == "untrusted_data"
    assert doc["request_id"].startswith("req-")
    st, doc, _ = call(base_url, "POST", "/v2/memories/search", world.outsider["token"],
                      body={"query": "memo_alpha"})
    assert st == 404
    st, doc, _ = call(base_url, "POST", "/v2/memories/search", world.a["token"],
                      body={"query": "x", "keywords": []})
    assert st == 400
    assert call(base_url, "POST", "/v2/memories/search", None, body={})[0] == 401


def test_v1_paths_and_legacy_fields_are_rejected(world, base_url):
    token = world.a["token"]
    body = world.body(namespace_id=world.active_generation_id)
    assert call(base_url, "POST", "/v1/experiences", token, "old-path", body)[0] == 404
    st, doc, _ = call(base_url, "POST", "/v2/experiences", token, "old-field", body)
    assert (st, doc["error"]["code"]) == (400, "UNKNOWN_FIELD")
    st, doc, _ = call(base_url, "POST", "/v2/memories/search", token,
                      body={"query": "legacy", "namespace_id": world.active_generation_id})
    assert (st, doc["error"]["code"]) == (400, "UNKNOWN_FIELD")
    assert world.counts()["experiences"] == 0
