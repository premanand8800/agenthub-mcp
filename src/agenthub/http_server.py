"""Optional REST API for scripts and tools that do not speak MCP.

Defenses: loopback-only by default, Host header allow-list (DNS rebinding), Origin allow-list
(browser CSRF), bearer token checked before the body is read, JSON-only POSTs, body size cap,
failed-auth rate limiting.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from agenthub import __version__
from agenthub.errors import HubError, InvalidArgument, NotFound
from agenthub.hub import Hub
from agenthub.security import load_or_create_token, token_matches

MAX_BODY_BYTES = 1024 * 1024
AUTH_FAILS_PER_MINUTE = 10
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

Route = Tuple[str, "re.Pattern[str]", Callable[..., Any]]


def _int(q: Dict[str, List[str]], key: str) -> Optional[int]:
    if key not in q:
        return None
    try:
        return int(q[key][0])
    except ValueError:
        raise InvalidArgument(f"query parameter '{key}' must be an integer") from None


def _routes() -> List[Route]:
    def r(method: str, pattern: str, fn: Callable[..., Any]) -> Route:
        regex = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$"
        return method, re.compile(regex), fn

    run_keys = (
        "workdir",
        "permission_mode",
        "model",
        "session_id",
        "add_dirs",
        "images",
        "output_schema",
        "fallback_agents",
    )

    def pick(body: Dict[str, Any], allowed, required=("prompt",)) -> Dict[str, Any]:
        unknown = sorted(set(body) - {*required, *allowed})
        if unknown:
            raise InvalidArgument(f"Unknown field(s): {', '.join(unknown)}")
        return {k: body[k] for k in allowed if body.get(k) is not None}

    return [
        r("GET", "/v1/agents", lambda h, q, b: h.list_agents()),
        r("GET", "/v1/agents/{agent}/models", lambda h, q, b, agent: h.list_models(agent)),
        r("GET", "/v1/agents/{agent}/sessions", lambda h, q, b, agent: h.list_sessions(agent, _int(q, "limit"))),
        r(
            "GET",
            "/v1/agents/{agent}/sessions/search",
            lambda h, q, b, agent: h.search_sessions(agent, (q.get("q") or [""])[0], _int(q, "limit")),
        ),
        r(
            "GET",
            "/v1/agents/{agent}/sessions/{session_id}",
            lambda h, q, b, agent, session_id: h.get_transcript(agent, session_id, _int(q, "max_steps")),
        ),
        r(
            "POST",
            "/v1/agents/{agent}/sessions/{session_id}/messages",
            lambda h, q, b, agent, session_id: h.send_message(agent, session_id, b.get("message"), source="http"),
        ),
        r(
            "POST",
            "/v1/agents/{agent}/ask",
            lambda h, q, b, agent: h.ask(agent, b.get("prompt"), source="http", **pick(b, run_keys)),
        ),
        r(
            "POST",
            "/v1/agents/{agent}/tasks",
            lambda h, q, b, agent: h.start_task(
                agent, b.get("prompt"), source="http", **pick(b, (*run_keys, "isolation"))
            ),
        ),
        r(
            "POST",
            "/v1/review",
            lambda h, q, b: h.review(
                b.get("agent"),
                source="http",
                **pick(
                    b,
                    ("workdir", "base", "task_id", "instructions", "model", "output_schema", "fallback_agents"),
                    ("agent",),
                ),
            ),
        ),
        r(
            "POST",
            "/v1/compare",
            lambda h, q, b: h.compare(
                b.get("agents"),
                b.get("prompt"),
                source="http",
                **pick(b, ("workdir", "output_schema"), ("agents", "prompt")),
            ),
        ),
        r("GET", "/v1/tasks", lambda h, q, b: h.list_tasks(_int(q, "limit"), (q.get("status") or [None])[0])),
        r("GET", "/v1/tasks/{task_id}", lambda h, q, b, task_id: h.get_task(task_id)),
        r("GET", "/v1/tasks/{task_id}/wait", lambda h, q, b, task_id: h.wait_task(task_id, _int(q, "timeout"))),
        r("GET", "/v1/tasks/{task_id}/logs", lambda h, q, b, task_id: h.task_logs(task_id, _int(q, "tail"))),
        r("GET", "/v1/tasks/{task_id}/diff", lambda h, q, b, task_id: h.get_task_diff(task_id)),
        r("POST", "/v1/tasks/{task_id}/cancel", lambda h, q, b, task_id: h.cancel_task(task_id, source="http")),
        r(
            "POST",
            "/v1/tasks/{task_id}/apply",
            lambda h, q, b, task_id: h.apply_task(task_id, bool(b.get("keep_worktree")), source="http"),
        ),
        r("POST", "/v1/tasks/{task_id}/discard", lambda h, q, b, task_id: h.discard_task(task_id, source="http")),
    ]


class _AuthLimiter:
    def __init__(self) -> None:
        self._fails: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> bool:
        with self._lock:
            q = self._fails[ip]
            while q and q[0] < time.time() - 60:
                q.popleft()
            return len(q) >= AUTH_FAILS_PER_MINUTE

    def fail(self, ip: str) -> None:
        with self._lock:
            self._fails[ip].append(time.time())


class AgentHubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: Tuple[str, int], hub: Hub, token: str, allow_remote: bool = False):
        super().__init__(addr, Handler)
        self.hub = hub
        self.token = token
        self.allow_remote = allow_remote
        self.routes = _routes()
        self.limiter = _AuthLimiter()
        port = self.server_address[1]
        self.allowed_hosts = {f"{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]")}


class Handler(BaseHTTPRequestHandler):
    server: AgentHubServer
    server_version = f"AgentHub/{__version__}"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("agenthub-http: " + (fmt % args) + "\n")

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        # Path only: query strings can carry secrets a client should not have sent.
        self.log_message("%s %s %s", self.command, urlparse(self.path).path, code)

    def _send(self, code: int, data: Any) -> None:
        body = json.dumps(data, indent=2, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header("Connection", "close")
        origin = self.headers.get("Origin")
        if origin and origin in self.server.hub.config.http_allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, code: int, err_code: str, message: str) -> None:
        # We may not have read the request body; don't reuse this connection.
        self.close_connection = True
        self._send(code, {"ok": False, "error": {"code": err_code, "message": message}})

    def _precheck(self) -> bool:
        """Host and Origin checks. Run before auth so rebinding/CSRF attempts never reach the token check."""
        if not self.server.allow_remote and self.headers.get("Host", "") not in self.server.allowed_hosts:
            self._fail(421, "bad_host", "Host header not allowed")
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in self.server.hub.config.http_allowed_origins:
            self._fail(403, "bad_origin", f"Origin '{origin}' is not in http_allowed_origins")
            return False
        return True

    def _authorized(self) -> bool:
        ip = self.client_address[0]
        if self.server.limiter.blocked(ip):
            self._fail(429, "rate_limited", "Too many failed authentication attempts; wait a minute")
            return False
        if token_matches(self.server.token, self.headers.get("Authorization")):
            return True
        self.server.limiter.fail(ip)
        self.server.hub.audit.record("http", "auth", "rejected", None, client=ip, path=urlparse(self.path).path)
        self._fail(401, "unauthorized", "Send 'Authorization: Bearer <token>'. Get it with: agenthub token")
        return False

    def do_OPTIONS(self) -> None:
        if not self._precheck():
            return
        origin = self.headers.get("Origin")
        self.send_response(204)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        if not self._precheck():
            return
        url = urlparse(self.path)
        if method == "GET" and url.path == "/health":
            self._send(200, {"ok": True, "service": "agenthub", "version": __version__})
            return
        if not self._authorized():
            return
        body: Dict[str, Any] = {}
        if method == "POST":
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._fail(415, "unsupported_media_type", "POST bodies must be application/json")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self._fail(413, "payload_too_large", f"Body must be between 0 and {MAX_BODY_BYTES} bytes")
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._fail(400, "invalid_json", "Body is not valid JSON")
                return
            if not isinstance(body, dict):
                self._fail(400, "invalid_json", "Body must be a JSON object")
                return
        query = parse_qs(url.query)
        for m, regex, fn in self.server.routes:
            match = regex.match(url.path)
            if match and m == method:
                params = {k: unquote(v) for k, v in match.groupdict().items()}
                try:
                    data = fn(self.server.hub, query, body, **params)
                except HubError as e:
                    self._send(e.http_status, {"ok": False, "error": e.to_dict()})
                except Exception as e:
                    self.log_message("internal error: %r", e)
                    self._fail(500, "internal_error", f"{type(e).__name__}: {e}")
                else:
                    self._send(200, data)
                return
        e = NotFound(f"No route for {method} {url.path}")
        self._send(404, {"ok": False, "error": e.to_dict()})


def run(host: str = "127.0.0.1", port: int = 8765, allow_remote: bool = False, hub: Optional[Hub] = None) -> None:
    if host not in LOOPBACK_HOSTS and not allow_remote:
        raise SystemExit(
            f"Refusing to listen on {host}: the API can run code on this machine. "
            "Pass --allow-remote only behind a TLS reverse proxy you control."
        )
    hub = hub or Hub()
    hub.prune()
    token = load_or_create_token(hub.config.token_path)
    server = AgentHubServer((host, port), hub, token, allow_remote)
    print(
        f"AgentHub {__version__} HTTP API on http://{host}:{server.server_address[1]}  (token: agenthub token)",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.shutdown()
        server.server_close()
