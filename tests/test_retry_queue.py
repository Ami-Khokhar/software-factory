"""Durable retry intent creation and readback; this phase never dispatches or spends."""
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from factory_mcp import tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.store import Store
from tests.fakes import FakeGitHub, FakeRelay, workitem

REPO, ID, PLAN = "o/r", "o/r#1", "plan/current"
ROOT = Path(__file__).resolve().parent.parent


class RetryQueueTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "factory.sqlite3")
        self.store = Store(self.path)
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.github.add_issue(REPO, 1)
        self.foreman = Foreman(self.store, self.github,
                               Config(mcp_token="t", repos=frozenset({REPO})), self.relay)
        self.agent = tools.build(self.store, self.foreman, agent=True)
        self.store.put(workitem(id=ID, repo=REPO, issueNumber=1, branch="factory/1", state="blocked",
                                blockedOn="verify exited 1", attempts=["verify exited 1", "verify failed again"]))
        self.store.approve_allowance(PLAN, ["retry"], 1, "owner", scope={"repo": REPO, "items": [ID]})

    def tearDown(self):
        self.temp.cleanup()

    def queue(self, **overrides):
        args = {"id": ID, "planId": PLAN, "requestId": "retry/one", "by": "day-desk",
                "evidence": "rerun after clearing the stale cache"}
        args.update(overrides)
        return self.agent["factory_queue_retry"].fn(args)

    def test_request_survives_restart_and_replays_without_spend_or_dispatch(self):
        first = self.queue()
        replay = self.queue()
        self.assertEqual((first["status"], replay["requestId"]), ("queued", "retry/one"))
        self.assertEqual((self.store.allowance(PLAN)["remaining"], self.store.workorders(ID)), (1, []))
        row = self.agent["factory_status"].fn({})["workItems"][0]
        self.assertEqual(row["pendingRetry"]["requestId"], "retry/one")
        schema = json.loads((ROOT / "schemas" / "workitem.schema.json").read_text())
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(self.store.get(ID))), [])
        with self.assertRaisesRegex(ToolError, "different inputs"):
            self.queue(evidence="different input")

        self.store.close()
        self.store = Store(self.path)
        self.foreman = Foreman(self.store, self.github,
                               Config(mcp_token="t", repos=frozenset({REPO})), self.relay)
        self.agent = tools.build(self.store, self.foreman, agent=True)
        replay = self.queue()
        self.foreman.tick(ID)
        self.assertEqual((replay["status"], self.store.get(ID)["pendingRetry"]["status"],
                          self.store.allowance(PLAN)["remaining"], self.store.workorders(ID)),
                         ("queued", "queued", 1, []))

    def test_owner_takeover_cancels_the_queue_without_spending(self):
        self.queue()
        wi = self.foreman.resolve_human(ID, "close", "close this item", "owner")
        self.assertEqual((wi["state"], wi.get("pendingRetry")), ("cancelled", None))
        self.assertEqual((self.store.request("retry/one")["phase"], self.store.allowance(PLAN)["remaining"]),
                         ("cancelled", 1))

    def test_busy_and_unsafe_requests_never_consume_authority(self):
        self.store.acquire(ID, "another-tick")
        result = self.queue()
        self.store.release(ID, "another-tick")
        self.assertEqual((result["status"], result["reason"], self.store.request("retry/one")),
                         ("retryable", "workitem_busy", None))
        self.store.put(dict(self.store.get(ID), blockedOn="pr-gatekeeper decided HUMAN"))
        with self.assertRaisesRegex(ToolError, "safety stop"):
            self.queue()
        self.store.put(dict(self.store.get(ID), blockedOn="verify exited 1"))
        self.store.put(workitem(id="o/r#2", repo=REPO, issueNumber=2, branch="factory/2", state="blocked",
                                blockedOn="verify failed", attempts=["verify failed", "verify failed again"]))
        with self.assertRaisesRegex(ToolError, "does not cover o/r#2"):
            self.agent["factory_queue_retry"].fn(
                {"id": "o/r#2", "planId": PLAN, "requestId": "retry/two", "by": "day-desk",
                 "evidence": "ordinary failure"})
        self.store.save_workorder({"id": "prior/1", "workItemId": ID})
        limited = Foreman(self.store, self.github, Config(mcp_token="t", repos=frozenset({REPO}),
                                                         max_passes=1), self.relay)
        with self.assertRaisesRegex(ToolError, "used its 1 passes"):
            limited.queue_retry(ID, PLAN, "retry/spent", "day-desk", "ordinary failure")
        self.assertEqual(self.store.allowance(PLAN)["remaining"], 1)


if __name__ == "__main__":
    unittest.main()
