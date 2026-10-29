"""Read durable evidence through the tool, including across restarts and pages."""
import json
import tempfile
import unittest
from pathlib import Path

from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError, handle
from factory_mcp.store import Store
from factory_mcp.tools import build
from tests.fakes import workitem


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "factory.sqlite3")
        self.config = Config(mcp_token="owner", repos=frozenset({"o/r"}))
        self.store = Store(self.path)
        self.store.put(workitem())
        self.store.put(dict(workitem(), id="other/r#2", repo="other/r"))
        self.tool = self.make_tool()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def make_tool(self):
        return build(self.store, Foreman(self.store, None, self.config, None), agent=True)[
            "factory_memory_events"]

    def read(self, **args):
        return self.tool.fn({"repo": "o/r", **args})

    def log(self, action="attempt_failed", **detail):
        self.store.log(action, detail, "o/r#1")

    def test_snapshot_pagination_survives_restart_and_excludes_new_events(self):
        for reason in ("first", "second", "third"):
            self.log(reason=reason)
        first = self.read(limit=1)
        self.assertTrue(first["hasMore"])
        self.store.close()
        self.store = Store(self.path)
        self.tool = self.make_tool()
        self.log(reason="later")
        rest = self.read(after=first["nextCursor"], through=first["through"])
        self.assertEqual([e["detail"]["reason"] for e in rest["events"]], ["second", "third"])
        self.assertFalse(rest["hasMore"])
        self.assertEqual(rest["nextCursor"], first["through"])
        later = self.read(after=rest["nextCursor"])
        self.assertEqual([e["detail"]["reason"] for e in later["events"]], ["later"])

    def test_other_repos_tool_arguments_and_global_rows_are_not_exposed(self):
        self.log(reason="visible")
        self.store.log("attempt_failed", {"reason": "other private data"}, "other/r#2")
        self.store.log("attempt_failed", {"reason": "global private data"})
        self.store.log("tool:factory_start_project", {"args": {"secret": "private"}}, "o/r#1")
        page = self.read(repo="O/R")
        self.assertEqual(page["repo"], "o/r")
        self.assertEqual([e["detail"]["reason"] for e in page["events"]], ["visible"])
        self.assertEqual(page["through"], page["events"][0]["seq"])
        for repo in ("other/r", "missing/r", None, ["o/r"]):
            with self.subTest(repo=repo), self.assertRaises(ToolError):
                self.read(repo=repo)

    def test_evidence_is_bounded_and_redacted_without_returning_arbitrary_payloads(self):
        self.log(reason="API_KEY=private-value password=hidden " + "x" * 500,
                 diagnosis="Bearer hidden", worker="pi", cause="failing test",
                 rawPrompt="private-prompt", diagnostics={"tail": "private-tail"},
                 question={"token": "private-token"})
        event = self.read()["events"][0]
        encoded = json.dumps(event)
        for secret in ("private-value", "hidden", "private-prompt", "private-tail", "private-token"):
            self.assertNotIn(secret, encoded)
        self.assertLessEqual(len(event["detail"]["reason"]), 300)
        self.assertEqual(event["detail"]["worker"], "pi")
        self.assertEqual(event["detail"]["cause"], "failing test")
        self.assertEqual(event["workItemId"], "o/r#1")
        self.assertIn("at", event)

    def test_gate_problems_are_bounded_sanitized_summaries(self):
        secret = "sk-" + "a" * 24
        self.log(reason="selected scalar", blocking=[
            {"problem": "First " + secret, "fingerprint": "hidden", "nested": {"private": "hidden"}},
            {"problem": "b" * 120}, {"problem": "third"}, {"problem": "fourth"},
            {"nested": {"problem": "malformed"}},
        ])
        event = self.read()["events"][0]
        self.assertEqual(event["detail"]["reason"], "selected scalar")
        problems = event["detail"]["gateProblems"]
        self.assertEqual(len(problems), 3)
        self.assertLessEqual(max(map(len, problems)), 100)
        self.assertLessEqual(sum(map(len, problems)), 300)
        self.assertEqual(problems[0], "<redacted>")
        self.assertEqual(problems[1], "b" * 100)
        self.assertNotIn("blocking", event["detail"])
        encoded = json.dumps(event)
        for hidden in (secret, "fingerprint", "nested", "private", "fourth", "malformed"):
            self.assertNotIn(hidden, encoded)

    def test_empty_and_exactly_full_pages_finish_without_moving_cursor_backwards(self):
        self.assertEqual(self.read(after=20)["nextCursor"], 20)
        self.assertEqual(self.read(after=20)["through"], 20)
        self.log(action="result", workOrder="o/r#1:r0:implement:a0", commitSha="abc")
        page = self.read(limit=1)
        self.assertFalse(page["hasMore"])
        self.assertEqual(page["events"][0]["detail"]["commitSha"], "abc")
        self.assertEqual(self.read(after=page["nextCursor"])["events"], [])

    def test_invalid_paging_is_refused_by_handler_not_just_advertised_schema(self):
        for args in ({"after": -1}, {"after": True}, {"after": "1"}, {"limit": 0},
                     {"limit": 101}, {"limit": True}, {"through": -1}, {"through": False},
                     {"after": 2, "through": 1}, {"unknown": "value"}):
            with self.subTest(args=args), self.assertRaises(ToolError):
                self.read(**args)

    def test_readonly_agent_tool_returns_evidence_over_mcp(self):
        self.log(reason="test failed")
        listing = self.tool.listing()
        self.assertTrue(listing["annotations"]["readOnlyHint"])
        before = self.store.history("o/r#1")
        result = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": self.tool.name, "arguments": {"repo": "o/r"}}}, {self.tool.name: self.tool})
        self.assertFalse(result["result"]["isError"])
        value = json.loads(result["result"]["content"][0]["text"])
        self.assertEqual(value["events"][0]["detail"]["reason"], "test failed")
        self.assertEqual(self.store.history("o/r#1"), before)
