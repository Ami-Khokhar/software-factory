"""A WorkItem that no longer waits on a person drops its blockedOn, and factory_status shows it."""
import unittest

from factory_mcp import metrics, tools
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


class EscalationSaysWhatIsNewTest(unittest.TestCase):
    """#480: an escalation says what is new since the person's last answer, not only what is wrong."""

    def workitem(self, **fields):
        wi = {"id": ID, "state": "human_waiting", "blockedOn": HUMAN_BLOCKED_ON, "owner": "o",
              "attempts": [], "gateRound": 2,
              "blockers": [{"fingerprint": "a1", "firstSeenRound": 1, "resolved": False},
                           {"fingerprint": "b2", "firstSeenRound": 1, "resolved": False},
                           {"fingerprint": "c3", "firstSeenRound": 2, "resolved": False},
                           {"fingerprint": "d4", "firstSeenRound": 1, "resolved": True}]}
        wi.update(fields)
        return wi

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

    def status(self):
        """factory_status for this one WorkItem, which is where an escalation is built."""
        return tools.build(self.foreman.store, self.foreman)["factory_status"].fn({"id": ID})

    def test_it_counts_findings_new_in_this_round_apart_from_ones_reported_before(self):
        esc = metrics.escalation(self.workitem(), [])
        self.assertEqual(esc["findings"], {"new": 1, "reported": 2, "resolved": 1})

    def test_a_workitem_the_gate_never_reported_on_says_nothing_about_findings(self):
        self.assertIsNone(metrics.escalation(self.workitem(blockers=[]), [])["findings"])

    def test_it_owes_a_comparison_from_the_head_the_person_last_answered(self):
        """The base is the head the person answered, not the default branch and not the reviewed
        head: what they have not read is what changed after their answer."""
        history = [{"action": "reconcile", "at": "t1", "detail": {"headSha": "c1"}},
                   {"action": "human_resolved", "at": "t2", "detail": {"resolution": "retry"}}]
        self.assertEqual(metrics.changed_since(self.workitem(headSha="c2"), history), ("c1", "c2"))

    def test_it_owes_no_comparison_when_nobody_has_answered_yet(self):
        history = [{"action": "reconcile", "at": "t1", "detail": {"headSha": "c1"}}]
        self.assertIsNone(metrics.changed_since(self.workitem(headSha="c1"), history))

    def test_it_owes_no_comparison_when_the_head_is_the_one_they_answered(self):
        history = [{"action": "reconcile", "detail": {"headSha": "c1"}},
                   {"action": "human_resolved", "detail": {}},
                   {"action": "reconcile", "detail": {"headSha": "c1"}}]
        self.assertIsNone(metrics.changed_since(self.workitem(headSha="c1"), history))

    def test_the_paths_the_caller_read_ride_on_the_escalation_unchanged(self):
        changed = {"from": "c1", "to": "c2", "paths": ["src/app.py"]}
        self.assertEqual(metrics.escalation(self.workitem(), [], changed=changed)["changedSince"],
                         changed)

    def test_a_first_escalation_compares_nothing(self):
        self.land("c1")
        self.github.add_run(REPO, "c1", conclusion="failure")
        self.github.add_verdict(REPO, 100, "HUMAN", 1, "c1")
        self.assertEqual(self.tick()["state"], "human_waiting")
        before = list(self.github.compares)
        self.assertIsNone(self.status()["escalation"]["changedSince"])
        self.assertEqual(self.github.compares, before)

    def test_it_lists_the_paths_that_changed_since_the_last_answer(self):
        self.land("c1")
        self.github.add_run(REPO, "c1", conclusion="failure")
        self.github.add_verdict(REPO, 100, "HUMAN", 1, "c1")
        self.assertEqual(self.tick()["state"], "human_waiting")

        self.foreman.resolve_human(ID, "retry", "try the parse again", by="owner")
        self.land("c2")  # a fix pass onto the same pull request, so the head moves
        self.github.add_run(REPO, "c2", conclusion="failure")
        self.github.add_verdict(REPO, 101, "HUMAN", 2, "c2")
        self.assertEqual(self.tick()["state"], "human_waiting")

        changed = self.status()["escalation"]["changedSince"]
        self.assertEqual(changed, {"from": "c1", "to": "c2", "paths": ["src/app.py"]})
        self.assertIn((REPO, "c1", "c2"), self.github.compares)


if __name__ == "__main__":
    unittest.main()
