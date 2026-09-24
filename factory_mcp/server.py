"""Streamable HTTP for MCP: JSON-RPC over POST /mcp, application/json responses, no SSE.

OpenBot's client (@modelcontextprotocol/sdk StreamableHTTPClientTransport) accepts a JSON body for
a request, 202 for a notification, and 405 for the optional GET stream.
"""
import hmac
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import mcp, tools
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

    def do_POST(self):
        if self.path.split("?")[0] != "/mcp":
            return self._send(404, {"error": "not_found"})
        # OpenBot calls from its server and sends no Origin; a browser always does. Refusing it
        # closes DNS rebinding, which the specification requires servers to guard against.
        if "Origin" in self.headers:
            return self._send(403, {"error": "origin_not_allowed"})
        expected = f"Bearer {self.server.config.mcp_token}".encode()
        if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected):
            return self._send(401, {"error": "unauthorized"}, [("www-authenticate", "Bearer")])
        length = int(self.headers.get("content-length") or 0)
        if length > MAX_BODY:
            self.close_connection = True
            return self._send(413, {"error": "body_too_large"})
        try:
            message = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            return self._send(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": "Parse error"}})
        response = mcp.handle(message, self.server.tools, self.server.audit)
        if response is None:
            return self._send(202)
        self._send(200, response)


def make_server(config, store, github=None, relay=None):
    server = ThreadingHTTPServer((config.host, config.port), Handler)
    server.daemon_threads = True
    server.config = config
    foreman = Foreman(store, github or GitHub(config.github_token), config,
                      relay or Relay(config.relay_url))
    server.foreman = foreman
    server.tools = tools.build(store, foreman)
    server.audit = lambda name, args, ok: store.log(
        f"tool:{name}", {"args": args, "ok": ok}, workitem_id=args.get("id"))
    if config.tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(config.tls_cert, config.tls_key or None)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def poll(foreman, store, seconds):
    """Tick everything every few seconds. OpenBot's 15-minute routine stays the backstop (F5)."""
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
    scheme = "https" if config.tls_cert else "http"
    print(f"factory-mcp on {scheme}://{config.host}:{server.server_port}/mcp", flush=True)
    server.serve_forever()
