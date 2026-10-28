"""Streamable HTTP for MCP: JSON-RPC over POST /mcp, application/json responses, no SSE.

Clients (Hermes, the factory CLI) get a JSON body for a request, 202 for a notification, and 405
for the optional GET stream. factory-mcp listens on loopback, and every call carries the token.
"""
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import diagnostics, mcp, tools
from .config import Config
from .foreman import Foreman
from .github import GitHub
from .relay import Relay
from .store import Store

MAX_BODY = 1_048_576


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "factory-mcp"

    def log_message(self, *args):
        pass

    def _send(self, status, body=None, headers=()):
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        if body is not None:
            self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(405, {"error": "method_not_allowed"}, [("allow", "POST")])

    do_DELETE = do_GET

    def _worker_ask(self):
        """A running worker pass asks Day Desk a question (ADR-0098). The pass's own token is the
        whole credential: it names one live WorkOrder and can do nothing else. The route is on the
        same loopback listener, which a worker container reaches as host.docker.internal."""
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            return self._send(400, {"error": "bad_content_length"})
        if length > 8192:
            self.close_connection = True
            return self._send(413, {"error": "body_too_large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad_json"})
        if not isinstance(body, dict):
            return self._send(400, {"error": "bad_json"})
        try:
            reply = self.server.foreman.worker_ask(body.get("token"), body.get("question"),
                                                   body.get("wait", 240))
        except mcp.ToolError as exc:
            return self._send(409, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - the worker gets an answer, not a reset socket
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
        self._send(200, reply)

    def _worker_memory(self):
        try:
            length = int(self.headers.get("content-length") or 0)
            if not 0 <= length <= 8192:
                self.close_connection = True
                return self._send(413, {"error": "body_too_large"})
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except (ValueError, TypeError):
            return self._send(400, {"error": "bad_request"})
        token = body.pop("token", "")
        try:
            result = self.server.foreman.worker_memory(token, body)
        except mcp.ToolError as exc:
            return self._send(403, {"error": str(exc)})
        self._send(200, result)

    def do_POST(self):
        if self.path.split("?")[0] == "/worker/memory":
            return self._worker_memory()
        if self.path.split("?")[0] == "/worker/ask":
            return self._worker_ask()
        if self.path.split("?")[0] != "/mcp":
            return self._send(404, {"error": "not_found"})
        # MCP clients send no Origin; a browser always does. Refusing it closes DNS rebinding,
        # which the specification requires servers to guard against.
        if "Origin" in self.headers:
            return self._send(403, {"error": "origin_not_allowed"})
        sent = self.headers.get("Authorization", "").encode()
        config = self.server.config
        if hmac.compare_digest(sent, f"Bearer {config.mcp_token}".encode()):
            caller = "owner"
        elif config.agent_token and hmac.compare_digest(sent, f"Bearer {config.agent_token}".encode()):
            caller = "agent"  # Day Desk driving (ADR-0030): the token says so, not the model
        else:
            return self._send(401, {"error": "unauthorized"}, [("www-authenticate", "Bearer")])
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            return self._send(400, {"error": "bad_content_length"})
        if length > MAX_BODY:
            self.close_connection = True
            return self._send(413, {"error": "body_too_large"})
        try:
            message = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            return self._send(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": "Parse error"}})
        response = mcp.handle(message, self.server.tools[caller], self.server.audit[caller])
        if response is None:
            return self._send(202)
        self._send(200, response)


def make_server(config, store, github=None, relay=None):
    server = ThreadingHTTPServer(("127.0.0.1", config.port), Handler)  # plain HTTP: loopback only
    server.daemon_threads = True
    server.config = config
    foreman = Foreman(store, github or GitHub(config.github_token, config.github_api), config,
                      relay or Relay(config.relay_url))
    server.foreman = foreman
    server.tools = {"owner": tools.build(store, foreman), "agent": tools.build(store, foreman, agent=True)}

    def audit(caller):
        def write(name, args, ok):
            saved = dict(args)
            if name == "factory_memory_put":
                saved = {key: args[key] for key in ("repo", "projectId", "key", "kind", "status", "version")
                         if key in args}
            if name == "factory_improvement_validate":
                saved.pop("source", None)
            if name == "factory_queue_retry" and isinstance(saved.get("evidence"), str):
                saved["evidence"] = diagnostics.sanitize(saved["evidence"])
            store.log(f"tool:{name}", {"args": saved, "ok": ok, "caller": caller},
                      workitem_id=args.get("id"))
        return write
    server.audit = {caller: audit(caller) for caller in server.tools}
    return server


def poll(foreman, store, seconds):
    """Tick everything every few seconds."""
    while True:
        time.sleep(seconds)
        try:
            foreman.tick()
        except Exception as exc:  # noqa: BLE001 - the poller must outlive one bad tick
            store.log("poll_error", {"error": str(exc)})


def main():
    config = Config.from_env()
    store = Store(config.db_path)
    server = make_server(config, store)
    if config.poll_seconds > 0:
        threading.Thread(target=poll, args=(server.foreman, store, config.poll_seconds),
                         daemon=True).start()
    print(f"factory-mcp on http://127.0.0.1:{server.server_port}/mcp", flush=True)
    server.serve_forever()
