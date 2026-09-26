"""Speak Streamable HTTP to a real factory-mcp server on an ephemeral port."""
import json
import threading
import unittest
import urllib.error
import urllib.request

from factory_mcp.config import Config
from factory_mcp.server import make_server
from factory_mcp.store import Store
from tests.fakes import workitem

TOKEN = "test-token"


class McpServerTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        config = Config(mcp_token=TOKEN, repos=frozenset({"o/r"}), port=0)
        self.server = make_server(config, self.store)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, body, headers=None, method="POST"):
        headers = {"Authorization": f"Bearer {TOKEN}", "content-type": "application/json",
                   "accept": "application/json, text/event-stream", **(headers or {})}
        request = urllib.request.Request(self.url, json.dumps(body).encode(), headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            return err.code, None

    def rpc(self, method, params=None):
        status, body = self.post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        self.assertEqual(status, 200)
        return body

    def test_handshake(self):
        result = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "openbot", "version": "1"}})["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "factory-mcp")
        self.assertIn("tools", result["capabilities"])
        status, body = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((status, body), (202, None))

    def test_every_tool_declares_whether_it_writes(self):
        """An MCP client decides from readOnlyHint whether to ask a person first (Hermes does), so a
        write tool marked read would skip that question. A new tool must be added here on purpose."""
        tools = self.rpc("tools/list")["result"]["tools"]
        reads = {t["name"] for t in tools if t["annotations"]["readOnlyHint"] is True}
        writes = {t["name"] for t in tools if t["annotations"]["readOnlyHint"] is False}
        self.assertEqual(reads, {"factory_status", "factory_repo_check"})
        self.assertEqual(writes, {"factory_intake", "factory_tick", "factory_resolve_human",
                                  "factory_propose_issue", "factory_add_repo", "factory_approve_issue"})
        self.assertTrue(all(t["inputSchema"]["type"] == "object" for t in tools))

    def test_status_call_is_audited(self):
        self.store.put(workitem())
        result = self.rpc("tools/call", {"name": "factory_status", "arguments": {}})["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["workItems"][0]["id"], "o/r#1")
        result = self.rpc("tools/call", {"name": "factory_status", "arguments": {"id": "o/r#1"}})["result"]
        self.assertEqual(json.loads(result["content"][0]["text"])["workItem"]["state"], "issued")
        self.assertEqual(self.store.history("o/r#1")[0]["action"], "tool:factory_status")
        missing = self.rpc("tools/call", {"name": "factory_status", "arguments": {"id": "o/r#9"}})["result"]
        self.assertTrue(missing["isError"])

    def test_protocol_errors(self):
        self.assertEqual(self.rpc("nope")["error"]["code"], -32601)
        self.assertEqual(self.rpc("tools/call", {"name": "factory_merge"})["error"]["code"], -32602)

    def test_http_guards(self):
        body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        self.assertEqual(self.post(body, {"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.post(body, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.post(body, method="GET")[0], 405)


if __name__ == "__main__":
    unittest.main()
