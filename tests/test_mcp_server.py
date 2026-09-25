"""Speak Streamable HTTP to a real factory-mcp server on an ephemeral port."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from factory_mcp.config import Config
from factory_mcp.server import make_server
from factory_mcp.store import Store
from tests.fakes import FakeGitHub, FakeRelay, workitem

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

    def test_tools_listed_with_effects(self):
        tools = {t["name"]: t for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertEqual(tools["factory_status"]["annotations"], {"readOnlyHint": True})
        self.assertEqual(tools["factory_status"]["inputSchema"]["type"], "object")
        reads = {name for name, t in tools.items() if t["annotations"]["readOnlyHint"]}
        self.assertEqual(reads, {"factory_status", "factory_repos", "factory_repo_check",
                                 "factory_repo_status"})
        self.assertEqual(set(tools) - reads, {"factory_intake", "factory_tick", "factory_resolve_human",
                                              "factory_propose_issue", "factory_add_repo",
                                              "factory_approve_issue"})
        self.assertFalse([name for name in tools if "merge" in name])

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


class DayDeskFlowTest(McpServerTest):
    """plan.md §13: check a repo, enable it, approve an issue, and the factory starts it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.github = Store(":memory:"), FakeGitHub()
        self.github.add_repo("o/new")
        config = Config(mcp_token=TOKEN, repos=frozenset(), port=0, checkout_root=self.tmp.name,
                        repos_file=str(Path(self.tmp.name) / "repos.json"), workers=(("pi", "local"),))
        clone = lambda url, token, dest: Path(dest, ".git").mkdir(parents=True)  # noqa: E731
        self.server = make_server(config, self.store, self.github, FakeRelay(),
                                  ops={"probe": lambda image, tool: True, "clone": clone})
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"

    def tearDown(self):
        super().tearDown()
        self.tmp.cleanup()

    def call(self, name, **arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})["result"]
        return result["isError"], json.loads(result["content"][0]["text"])

    def test_check_enable_approve_start(self):
        failed, check = self.call("factory_repo_check", repo="o/new")
        self.assertEqual((failed, check["ok"]), (False, True))
        failed, _ = self.call("factory_add_repo", repo="o/new", confirm=False, by="amteshwar")
        self.assertTrue(failed)
        failed, added = self.call("factory_add_repo", repo="o/new", confirm=True, by="amteshwar")
        self.assertFalse(failed, added)
        self.assertEqual(self.call("factory_repos")[1], {"repos": ["o/new"]})

        self.github.add_issue("o/new", 3, labels=("factory:proposed",), author="o")
        failed, started = self.call("factory_approve_issue", repo="o/new", issue=3, confirm=True,
                                    by="amteshwar")
        self.assertFalse(failed, started)
        self.assertEqual(started["workItem"]["state"], "ready")
        self.assertEqual(self.server.foreman.tick("o/new#3")["to"], "implementing")
        self.assertEqual(self.call("factory_status")[1]["needsYou"], [])
        audited = [h["action"] for h in self.store.history(None)]
        self.assertIn("repo:added", audited)
        self.assertIn("tool:factory_add_repo", audited)


if __name__ == "__main__":
    unittest.main()
