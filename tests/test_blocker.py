"""A WorkItem that no longer waits on a person drops its blockedOn, and factory_status shows it."""
import unittest
from unittest import mock

from factory_mcp import metrics, tools
from factory_mcp.config import Config
from factory_mcp.github import GitHubError
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
    """#480: an escalation says what the person has not seen yet, not only what is wrong."""

    BLOCKERS = [{"fingerprint": "a1", "firstSeenRound": 1, "resolved": False},
                {"fingerprint": "b2", "firstSeenRound": 1, "resolved": False},
                {"fingerprint": "c3", "firstSeenRound": 3, "resolved": False}]
    ANSWERED_AT_2 = [{"action": "reconcile", "at": "t1", "detail": {"headSha": "c1", "round": 2}},
                     {"action": "human_resolved", "at": "t2",
                      "detail": {"resolution": "retry", "by": "owner"}}]

    def workitem(self, **fields):
        wi = {"id": ID, "state": "human_waiting", "blockedOn": HUMAN_BLOCKED_ON, "owner": "o",
              "attempts": [], "gateRound": 3, "blockers": list(self.BLOCKERS)}
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

    def escalate_again(self, sha, round_):
        """A gate verdict on `sha`, so the WorkItem comes back to a person."""
        self.github.add_run(REPO, sha, conclusion="failure")
        self.github.add_verdict(REPO, 100, "HUMAN", round_, sha, findings=[
            {"severity": "blocking", "path": "src/app.py", "problem": "the parse drops a null"}])
        return self.tick()

    def test_new_counts_the_findings_first_reported_after_the_round_they_answered(self):
        """A first escalation is often raised at the round in which the same problem repeated
        (ADR-0005), so "new" is measured from the round the reader answered, never from the gate's
        current one."""
        esc = metrics.escalation(self.workitem(), self.ANSWERED_AT_2)
        self.assertEqual(esc["findings"], {"new": 1, "reported": 2, "resolved": 0})

    def test_every_open_finding_is_new_when_nobody_has_answered_yet(self):
        history = [{"action": "reconcile", "at": "t1", "detail": {"headSha": "c1", "round": 3}}]
        self.assertEqual(metrics.escalation(self.workitem(), history)["findings"],
                         {"new": 3, "reported": 0, "resolved": 0})

    def test_a_finding_they_already_answered_at_the_same_round_is_not_new_again(self):
        """A regate keeps its round (ADR-0113), so the findings it repeats are not new."""
        blockers = [{"fingerprint": "a1", "firstSeenRound": 2, "resolved": False},
                    {"fingerprint": "b2", "firstSeenRound": 2, "resolved": False}]
        esc = metrics.escalation(self.workitem(blockers=blockers, gateRound=2), self.ANSWERED_AT_2)
        self.assertEqual(esc["findings"], {"new": 0, "reported": 2, "resolved": 0})

    def test_a_resolved_finding_is_counted_apart_from_new_and_reported(self):
        blockers = self.BLOCKERS + [{"fingerprint": "d4", "firstSeenRound": 1, "resolved": True}]
        esc = metrics.escalation(self.workitem(blockers=blockers), self.ANSWERED_AT_2)
        self.assertEqual(esc["findings"], {"new": 1, "reported": 2, "resolved": 1})

    def test_a_reader_who_answered_before_any_verdict_sees_every_finding_as_new(self):
        """An answer can come before any accepted verdict, so the answered round defaults to 0
        rather than to nothing: comparing a finding's round against None would raise."""
        history = [{"action": "reconcile", "at": "t1", "detail": {"headSha": "c1"}},
                   {"action": "human_resolved", "at": "t2", "detail": {"by": "owner"}}]
        blockers = [{"fingerprint": "a1", "firstSeenRound": 2, "resolved": False},
                    {"fingerprint": "b2", "firstSeenRound": 2, "resolved": False}]
        esc = metrics.escalation(self.workitem(blockers=blockers, gateRound=2), history)
        self.assertEqual(esc["findings"], {"new": 2, "reported": 0, "resolved": 0})

    def test_a_workitem_the_gate_never_reported_on_says_nothing_about_findings(self):
        self.assertIsNone(metrics.escalation(self.workitem(blockers=[]), [])["findings"])

    def test_it_owes_a_comparison_from_the_head_the_last_answer_was_given_on(self):
        """The base is the head they answered, not the default branch and not the reviewed head:
        what they have not read is what changed after that answer."""
        self.assertEqual(metrics.changed_since(self.workitem(headSha="c2"), self.ANSWERED_AT_2),
                         {"from": "c1", "to": "c2", "by": "owner"})

    def test_the_answer_travels_so_an_agents_answer_is_not_shown_as_the_owners(self):
        history = [{"action": "reconcile", "detail": {"headSha": "c1", "round": 1}},
                   {"action": "human_resolved", "detail": {"agent": True, "by": "day-desk"}}]
        self.assertEqual(metrics.changed_since(self.workitem(headSha="c2"), history)["by"],
                         "day-desk")

    def test_it_owes_no_comparison_when_nobody_has_answered_yet(self):
        history = [{"action": "reconcile", "at": "t1", "detail": {"headSha": "c1"}}]
        self.assertIsNone(metrics.changed_since(self.workitem(headSha="c1"), history))

    def test_it_owes_no_comparison_when_the_head_is_the_one_they_answered(self):
        history = [{"action": "reconcile", "detail": {"headSha": "c1"}},
                   {"action": "human_resolved", "detail": {}},
                   {"action": "reconcile", "detail": {"headSha": "c1"}}]
        self.assertIsNone(metrics.changed_since(self.workitem(headSha="c1"), history))

    def test_the_paths_the_caller_read_ride_on_the_escalation_unchanged(self):
        changed = {"from": "c1", "to": "c2", "by": "owner", "paths": ["src/app.py"]}
        self.assertEqual(metrics.escalation(self.workitem(), [], changed=changed)["changedSince"],
                         changed)

    def test_a_first_escalation_compares_nothing(self):
        self.land("c1")
        self.assertEqual(self.escalate_again("c1", 1)["state"], "human_waiting")
        before = list(self.github.compares)
        self.assertIsNone(self.status()["escalation"]["changedSince"])
        self.assertEqual(self.github.compares, before)

    def test_it_lists_the_paths_that_changed_since_the_last_answer(self):
        self.land("c1")
        self.assertEqual(self.escalate_again("c1", 1)["state"], "human_waiting")

        self.foreman.resolve_human(ID, "retry", "try the parse again", by="owner")
        self.land("c2")  # a fix pass onto the same pull request, so the head moves
        self.assertEqual(self.escalate_again("c2", 2)["state"], "human_waiting")

        changed = self.status()["escalation"]["changedSince"]
        self.assertEqual(changed, {"from": "c1", "to": "c2", "by": "owner", "paths": ["src/app.py"]})
        self.assertIn((REPO, "c1", "c2"), self.github.compares)

    def test_a_comparison_github_refuses_is_reported_and_not_raised(self):
        """A base a force-push erased must not fail the status call that would have shown it."""
        self.land("c1")
        self.assertEqual(self.escalate_again("c1", 1)["state"], "human_waiting")
        self.foreman.resolve_human(ID, "retry", "try the parse again", by="owner")
        self.land("c2")
        self.assertEqual(self.escalate_again("c2", 2)["state"], "human_waiting")

        refused = mock.patch.object(self.github, "changed_files",
                                    side_effect=GitHubError(404, "Not Found"))
        with refused:
            escalation = self.status()["escalation"]
        self.assertEqual(escalation["changedSince"],
                         {"from": "c1", "to": "c2", "by": "owner", "paths": None,
                          "error": "GitHub could not compare c1..c2: GitHub 404: Not Found"})


if __name__ == "__main__":
    unittest.main()
