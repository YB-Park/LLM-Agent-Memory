"""Minimal REST adapter (stdlib only): validates transport, extracts credentials, maps errors (§3, §9)."""
import argparse
import json
import logging
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .errors import ServiceError
from .service import MemoryService

log = logging.getLogger("memory_server.rest")
_JOB = re.compile(r"^/v1/jobs/([^/]+)$")
_EXP = re.compile(r"^/v1/experiences/([^/]+)$")


def make_server(config, host="127.0.0.1", port=8080):
    service = MemoryService(config)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "memory-server"

        def log_message(self, fmt, *args):  # request lines may contain IDs only; use structured log instead
            pass

        def _token(self):
            header = self.headers.get("Authorization", "")
            parts = header.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                return parts[1].strip()
            return None

        def _send(self, status, body, request_id, headers=None):
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-Request-Id", request_id)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self):
            length = self.headers.get("Content-Length")
            if length is None or not length.isdigit():
                raise ServiceError(411, "LENGTH_REQUIRED", "Content-Length is required")
            if int(length) > config.max_request_bytes:
                self.close_connection = True
                raise ServiceError(413, "PAYLOAD_TOO_LARGE", "request exceeds %d bytes" % config.max_request_bytes)
            raw = self.rfile.read(int(length))
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise ServiceError(400, "INVALID_JSON", "request body is not valid JSON")

        def _dispatch(self, method):
            request_id = "req-" + uuid.uuid4().hex[:12]
            started = time.monotonic()
            status, body, extra = 500, None, {}
            try:
                path = self.path.split("?", 1)[0]
                token = self._token()
                if method == "POST" and path == "/v1/experiences":
                    payload = self._read_json()
                    receipt, replayed = service.submit(token, payload, self.headers.get("Idempotency-Key"))
                    body = dict(receipt, request_id=request_id)
                    status = 200 if replayed else 202
                elif method == "POST" and path == "/v1/memories/search":
                    body = dict(service.search(token, self._read_json()), request_id=request_id)
                    status = 200
                elif method == "GET" and _JOB.match(path):
                    body = dict(service.get_job(token, _JOB.match(path).group(1)), request_id=request_id)
                    status = 200
                elif method == "GET" and _EXP.match(path):
                    body = dict(service.get_experience(token, _EXP.match(path).group(1)), request_id=request_id)
                    status = 200
                else:
                    raise ServiceError(404, "NOT_FOUND", "대상을 찾을 수 없습니다.")
            except ServiceError as exc:
                status, body = exc.status, dict(exc.to_dict(), request_id=request_id)
                if status == 401:
                    extra["WWW-Authenticate"] = "Bearer"
                if exc.retry_after_ms:
                    extra["Retry-After"] = str(max(1, exc.retry_after_ms // 1000))
            except Exception:
                log.exception("unhandled error request_id=%s", request_id)
                status = 500
                body = {"error": {"code": "INTERNAL", "message": "내부 오류", "retryable": False},
                        "request_id": request_id}
            log.info(json.dumps({"request_id": request_id, "method": method, "status": status,
                                 "ms": int((time.monotonic() - started) * 1000)}))
            self._send(status, body, request_id, extra)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    server = make_server(Config.from_env(), args.host, args.port)
    log.info("listening on %s:%d (plain HTTP; terminate TLS in front)", args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
