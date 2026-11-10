"""The orchestrator queue (ADR-0135): a labelled issue waits for a claim instead of reaching a
worker, an urgent entry is returned first and is found without waiting for a tick, a claim is
exclusive and expires, and an approval hands the work back to the ordinary dispatch path."""
import unittest

from factory_mcp import tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.store import Store
from tests.fakes import FakeGitHub, FakeRelay, ISSUE_BODY

REPO, ID = "o/r", "o/r#7"


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.config = Config(mcp_token="t", repos=frozenset({REPO}), queue=True)
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.foreman = Foreman(self.store, self.github, self.config, self.relay)
        self.github.add_issue(REPO, 7, labels=("factory:ready",))
        self.foreman.intake(REPO, 7)

    def claim(self, seconds=0):
        return self.foreman.next_queued("day-desk", seconds)

    def test_a_ready_issue_enters_the_queue_and_reaches_no_worker(self):
        self.assertEqual(self.store.get(ID)["state"], "queued")
        wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "queued")
        self.assertEqual((self.store.workorders(ID), self.relay.tasks), ([], {}))

    def test_the_claim_returns_the_entry_once_and_marks_who_took_it(self):
        found = self.claim()
        self.assertEqual(found["workItem"]["id"], ID)
        self.assertEqual(found["queued"], 1)
        self.assertEqual(found["workItem"]["queue"]["claimedBy"], "day-desk")
        self.assertIsNone(self.claim())  # held by that claim, and nothing else is queued

    def test_an_urgent_entry_jumps_the_queue(self):
        self.github.add_issue(REPO, 8, labels=("factory:ready", "factory:urgent"))
        self.foreman.intake(REPO, 8)
        self.assertEqual(self.claim()["workItem"]["id"], "o/r#8")
        self.assertEqual(self.claim()["workItem"]["id"], ID)

    def test_an_urgent_issue_is_found_without_a_tick(self):
        """The label is read from GitHub inside a waiting claim, so an urgent issue the store has
        never seen does not wait for the poll loop."""
        self.foreman.approve_queued(ID, "day-desk")  # the queue is empty, so the store has nothing
        self.github.add_issue(REPO, 9, labels=("factory:urgent",))
        found = self.claim(seconds=5)
        self.assertIsNotNone(found, "an urgent issue is taken in by the claim itself")
        self.assertEqual(found["workItem"]["id"], "o/r#9")
        self.assertEqual(found["workItem"]["state"], "queued")

    def test_a_claim_past_its_lease_goes_back_to_the_queue(self):
        self.assertEqual(self.claim()["workItem"]["id"], ID)
        wi = self.store.get(ID)
        self.store.put(dict(wi, queue=dict(wi["queue"], claimedAt="2026-01-01T00:00:00Z")))
        again = self.claim()
        self.assertIsNotNone(again, "a claim past its lease is free again")
        self.assertEqual(again["workItem"]["id"], ID)

    def test_an_approval_hands_it_to_the_dispatch_path(self):
        self.claim()
        self.foreman.approve_queued(ID, "day-desk", "worth a pass as it stands")
        self.assertEqual((self.store.get(ID)["state"], self.store.get(ID)["queue"]), ("ready", None))
        self.assertEqual(self.foreman.tick(ID)["workItem"]["state"], "implementing")
        self.assertEqual((len(self.store.workorders(ID)), len(self.relay.tasks)), (1, 1))

    def test_only_a_queued_workitem_is_approved(self):
        self.foreman.approve_queued(ID, "day-desk")
        with self.assertRaisesRegex(ToolError, "only a queued"):
            self.foreman.approve_queued(ID, "day-desk")

    def test_a_proposed_issue_is_not_queued_and_joins_when_it_gains_ready(self):
        self.github.add_issue(REPO, 10, labels=("factory:proposed",), body=ISSUE_BODY)
        wi = self.foreman.intake(REPO, 10)
        self.assertEqual((wi["state"], wi["queue"]), ("planned", None))
        self.github.issues[(REPO, 10)]["labels"] = [{"name": "factory:ready"}]
        moved = self.foreman.tick("o/r#10")["workItem"]
        self.assertEqual(moved["state"], "queued")
        self.assertTrue((moved.get("queue") or {}).get("at"))  # its place in the order is stamped

    def test_the_two_queue_tools_are_the_agent_writes(self):
        built = tools.build(self.store, self.foreman, agent=True)
        self.assertEqual(built["factory_queue_next"].fn({"by": "day-desk"})["next"]["workItem"]["id"], ID)
        self.assertEqual(built["factory_queue_approve"].fn(
            {"id": ID, "by": "day-desk"})["workItem"]["state"], "ready")


if __name__ == "__main__":
    unittest.main()
