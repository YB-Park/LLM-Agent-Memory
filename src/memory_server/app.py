"""ASGI application: REST (/v2/*) + MCP Streamable HTTP (/mcp) + health, behind one security edge.

Both protocol adapters call the same MemoryService; nothing here decides authorization or business outcomes.
The edge only does transport work: Host/Origin checks, body limits, abuse throttling, request ids, safe logging.
Credentials are never trusted from proxy-supplied identity headers: the only identity input is the bearer token,
which the service re-verifies against the database on every request (so revocation is immediate even for a
client that keeps its connection or MCP session).
"""
import argparse
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import defaultdict, deque

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import ledger, mcp_tools
from .config import Config
from .db import connect
from .errors import ServiceError
from .migrate import MIGRATIONS_DIR
from .service import MemoryService



log = logging.getLogger("memory_server.http")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
PROTECTED_PREFIXES = ("/v2/", "/mcp")


class SlidingWindow(object):
    """Small in-process limiter: at most `limit` events per key within `window` seconds."""

    def __init__(self, limit, window, max_keys=10000):
        self.limit, self.window, self.max_keys = limit, window, max_keys
        self._events = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key, now):
        q = self._events[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        if not q:
            self._events.pop(key, None)
        return q

    def blocked(self, key):
        with self._lock:
            return len(self._prune(key, time.monotonic())) >= self.limit

    def hit(self, key):
        """Record an event; returns True while still within the limit."""
        with self._lock:
            now = time.monotonic()
            if len(self._events) >= self.max_keys and key not in self._events:
                self._events.clear()  # bound memory under key-spraying; resets only throttling state
            q = self._prune(key, now) if key in self._events else self._events[key]
            q.append(now)
            return len(q) <= self.limit


def _host_allowed(header, allowed):
    host = (header or "").lower()
    bare = host.rsplit(":", 1)[0] if not host.endswith("]") and ":" in host else host
    for entry in allowed:
        entry = entry.lower()
        if host == entry or (":" not in entry.strip("[]") and bare == entry):
            return True
    return False


def _json(status, body, headers=None):
    return JSONResponse(body, status_code=status, headers=headers)


def error_response(exc, request_id):
    headers = {"X-Request-Id": request_id}
    if exc.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if exc.retry_after_ms:
        headers["Retry-After"] = str(max(1, exc.retry_after_ms // 1000))
    return _json(exc.status, dict(exc.to_dict(), request_id=request_id), headers)


class Edge(object):
    """Pure-ASGI middleware (no BaseHTTPMiddleware: it would buffer and re-wrap bodies)."""

    def __init__(self, app, config, service):
        self.app, self.config, self.service = app, config, service
        self.auth_failures = SlidingWindow(config.auth_fail_limit, config.auth_fail_window_seconds)
        self.budget = SlidingWindow(config.request_limit_per_minute, 60)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = "req-" + uuid.uuid4().hex[:12]
        started = time.monotonic()
        path = scope["path"]
        method = scope["method"]
        client = (scope.get("client") or ("-", 0))[0]
        state = {"status": 0}

        async def send_logged(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            early, receive = await self._admit(scope, receive, request_id, client)
            if early is not None:
                await early(scope, receive, send_logged)
            else:
                scope.setdefault("state", {})["request_id"] = request_id
                await self.app(scope, receive, send_logged)
        except Exception:
            log.exception("unhandled error request_id=%s", request_id)
            body = {"error": {"code": "INTERNAL", "message": "내부 오류", "retryable": False},
                    "request_id": request_id}
            if state["status"] == 0:
                await _json(500, body)(scope, receive, send_logged)
        if state["status"] == 401:
            self.auth_failures.hit(client)
        log.info(json.dumps({"request_id": request_id, "method": method, "path": path, "status": state["status"],
                             "ms": int((time.monotonic() - started) * 1000), "client": client}))

    async def _admit(self, scope, receive, request_id, client):
        """Returns (early_response_or_None, receive). An early response ends the request at the edge."""
        cfg = self.config
        path = scope["path"]
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}

        def deny(status, code, message, retry_after=None, www=False):
            h = {"X-Request-Id": request_id}
            if retry_after:
                h["Retry-After"] = str(retry_after)
            if www:
                h["WWW-Authenticate"] = "Bearer"
            return _json(status, {"error": {"code": code, "message": message, "retryable": bool(retry_after)},
                                  "request_id": request_id}, h)

        health = path in ("/healthz", "/readyz")
        host = headers.get("host")
        if not (_host_allowed(host, cfg.allowed_hosts) or (health and _host_allowed(host, LOOPBACK_HOSTS))):
            return deny(421, "HOST_NOT_ALLOWED", "허용되지 않은 Host입니다."), receive
        origin = headers.get("origin")
        if origin is not None and origin not in cfg.allowed_origins:
            return deny(403, "ORIGIN_NOT_ALLOWED", "허용되지 않은 Origin입니다."), receive
        if health:
            return None, receive
        if self.auth_failures.blocked(client):
            return deny(429, "TOO_MANY_FAILED_AUTH", "인증 실패가 너무 많습니다. 잠시 후 다시 시도하세요.", 30), receive

        token = None
        parts = headers.get("authorization", "").split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
        budget_key = hashlib.sha256((token or client).encode("utf-8")).hexdigest()[:24]
        if not self.budget.hit(budget_key):
            return deny(429, "RATE_LIMITED", "요청이 너무 많습니다.", 10), receive

        if scope["method"] in ("POST", "PUT", "PATCH"):
            declared = headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > cfg.max_request_bytes:
                return deny(413, "PAYLOAD_TOO_LARGE", "request exceeds %d bytes" % cfg.max_request_bytes), receive
            body, ok = b"", True
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return deny(400, "CLIENT_DISCONNECTED", "연결이 끊어졌습니다."), receive
                body += message.get("body", b"")
                if len(body) > cfg.max_request_bytes:
                    return deny(413, "PAYLOAD_TOO_LARGE", "request exceeds %d bytes" % cfg.max_request_bytes), receive
                if not message.get("more_body"):
                    break
            sent = {"done": False}

            async def replay():
                if not sent["done"]:
                    sent["done"] = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.disconnect"}

            receive = replay

        if path == "/mcp" or path.startswith("/mcp/"):
            # Transport-level 401: answered before any protocol handling. The tools re-verify per call.
            try:
                await anyio.to_thread.run_sync(self.service.authenticate, token)
            except ServiceError as exc:
                return error_response(exc, request_id), receive
        return None, receive


def _ready(config):
    conn = None
    try:
        conn = connect(config.database_url)
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '2000ms'")
            cur.execute("SELECT service_state FROM system_control WHERE id = 1")
            state = cur.fetchone()["service_state"]
            cur.execute("SELECT version FROM schema_migrations")
            applied = {r["version"] for r in cur.fetchall()}
        wanted = {f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql")}
        if wanted - applied:
            return 503, {"status": "not_ready", "reason": "migrations_pending"}
        if state != "serving":
            return 503, {"status": "not_ready", "reason": state}
        return 200, {"status": "ready"}
    except Exception:
        return 503, {"status": "not_ready", "reason": "database_unavailable"}
    finally:
        if conn is not None:
            conn.close()


def create_app(config):
    if not config.allowed_hosts:
        raise SystemExit("MEMORY_ALLOWED_HOSTS is required (comma-separated public host names)")
    ledger.ensure(config.policy_ledger)
    service = MemoryService(config)
    mcp = mcp_tools.build_server(service)

    async def payload(request):
        try:
            return json.loads((await request.body()).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ServiceError(400, "INVALID_JSON", "request body is not valid JSON")

    def token_of(request):
        parts = request.headers.get("authorization", "").split(None, 1)
        return parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else None

    async def run(request, fn, *args, status=200):
        rid = request.state.request_id
        try:
            out = await anyio.to_thread.run_sync(lambda: fn(*args))
        except ServiceError as exc:
            return error_response(exc, rid)
        if isinstance(out, tuple):  # submit: (receipt, replayed)
            body, replayed = out
            return _json(200 if replayed else 202, dict(body, request_id=rid))
        return _json(status, dict(out, request_id=rid))

    @mcp.custom_route("/v2/experiences", methods=["POST"])
    async def submit(request: Request):
        try:
            body = await payload(request)
        except ServiceError as exc:
            return error_response(exc, request.state.request_id)
        return await run(request, service.submit, token_of(request), body, request.headers.get("idempotency-key"))

    @mcp.custom_route("/v2/memories/search", methods=["POST"])
    async def search(request: Request):
        try:
            body = await payload(request)
        except ServiceError as exc:
            return error_response(exc, request.state.request_id)
        return await run(request, service.search, token_of(request), body)

    @mcp.custom_route("/v2/jobs/{job_id}", methods=["GET"])
    async def job(request: Request):
        return await run(request, service.get_job, token_of(request), request.path_params["job_id"])

    @mcp.custom_route("/v2/experiences/{experience_id}", methods=["GET"])
    async def experience(request: Request):
        return await run(request, service.get_experience, token_of(request), request.path_params["experience_id"])

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request):
        return _json(200, {"status": "alive"})

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readyz(request: Request):
        status, body = await anyio.to_thread.run_sync(_ready, config)
        return _json(status, body)

    @mcp.custom_route("/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def unknown(request: Request):
        return _json(404, {"error": {"code": "NOT_FOUND", "message": "대상을 찾을 수 없습니다.", "retryable": False},
                           "request_id": request.state.request_id})

    app = mcp.streamable_http_app(stateless_http=True, json_response=True,
                                  max_request_body_size=config.max_request_bytes)
    app.add_middleware(Edge, config=config, service=service)
    return app


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description="Memory API + MCP server (plain HTTP; terminate TLS in front)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--forwarded-allow-ips", default="127.0.0.1",
                        help="peers whose X-Forwarded-For is trusted (the TLS proxy)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = Config.from_env()
    uvicorn.run(create_app(config), host=args.host, port=args.port, access_log=False, proxy_headers=True,
                forwarded_allow_ips=args.forwarded_allow_ips, timeout_keep_alive=15,
                timeout_graceful_shutdown=20, server_header=False)


if __name__ == "__main__":
    main()
