"""A WorkItem that no longer waits on a person drops its blockedOn, and factory_status shows it."""
import unittest

from factory_mcp import tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.store import Store
from factory_mcp.workitem import reconcile
from tests.fakes import FakeGit, FakeGitHub, FakeRelay, result_line

REPO, ID = "o/r", "o/r#7"
HUMAN_BLOCKED_ON = "pr-gatekeeper decided HUMAN; read the latest gate comment"


class BlockerDroppedTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.git = FakeGit(self.github).install(self)
        self.github.add_issue(REPO, 7)
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO})), self.relay)
        self.foreman.intake(REPO, 7)
        self.assertEqual(self.tick()["state"], "implementing")

    def tick(self):
        return self.foreman.tick(ID)["workItem"]

    def land(self, sha):
        """One worker pass whose patch lands as `sha`."""
        wi = self.tick()
        self.git.sha = sha
        self.relay.finish(wi["providerTaskId"], output=result_line(wi["activeWorkOrderId"]))
        return self.tick()

    def status_row(self):
        rows = tools.build(self.foreman.store, self.foreman)["factory_status"].fn({})["workItems"]
        return [r for r in rows if r["id"] == ID][0]

    def test_a_workitem_that_merges_after_a_human_verdict_drops_its_blocker(self):
        self.land("c1")
        self.github.add_run(REPO, "c1", conclusion="failure")
        self.github.add_verdict(REPO, 100, "HUMAN", 1, "c1")
        wi = self.tick()
        self.assertEqual(wi["state"], "human_waiting")
        self.assertEqual(wi["blockedOn"], HUMAN_BLOCKED_ON)
        self.assertEqual(self.status_row()["blockedOn"], HUMAN_BLOCKED_ON)

        self.github.pulls[0].update(state="closed", merged_at="2026-09-24T03:00:00Z")
        wi = self.tick()
        self.assertEqual(wi["state"], "closed")
        self.assertIsNone(wi["blockedOn"])
        self.assertIsNone(self.status_row()["blockedOn"])

    def test_the_issue_comment_names_day_desk_as_the_first_decider(self):
        """A routine escalation must not summon a person before Day Desk has tried: it answers
        each incident once, and the comment said a person had to decide first."""
        self.land("c1")
        self.github.add_run(REPO, "c1", conclusion="failure")
        self.github.add_verdict(REPO, 100, "HUMAN", 1, "c1")
        self.assertEqual(self.tick()["state"], "human_waiting")
        body = self.github.posted[-1][2]
        self.assertIn("software-factory is waiting on a decision", body)
        self.assertIn("Day Desk decides this one first", body)
        self.assertNotIn("Nothing more will happen until a person decides", body)

    def test_an_issue_closed_without_a_merge_drops_its_blocker_too(self):
        self.land("c1")
        self.github.add_run(REPO, "c1", conclusion="failure")
        self.github.add_verdict(REPO, 100, "HUMAN", 1, "c1")
        wi = self.tick()
        self.assertEqual(wi["state"], "human_waiting")
        self.assertEqual(wi["blockedOn"], HUMAN_BLOCKED_ON)
        self.assertEqual(self.status_row()["blockedOn"], HUMAN_BLOCKED_ON)

        self.github.issues[(REPO, 7)]["state"] = "closed"  # a person closed it, no merged PR
        wi = self.tick()
        self.assertEqual(wi["state"], "cancelled")
        self.assertIsNone(wi["blockedOn"])
        self.assertIsNone(self.status_row()["blockedOn"])


if __name__ == "__main__":
    unittest.main()
