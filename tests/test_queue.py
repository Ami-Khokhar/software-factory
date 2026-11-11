"""The orchestrator queue (ADR-0135): a labelled issue waits for a claim instead of reaching a
worker, an urgent entry is returned first and is found without waiting for a tick, a claim is
exclusive and expires, and an approval hands the work back to the ordinary dispatch path."""
import unittest
from unittest import mock

from factory_mcp import foreman as foreman_module
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

    def test_an_urgent_label_added_to_a_queued_workitem_re_orders_it_and_keeps_its_claim(self):
        """The common case the queue has to survive: an issue waits ten minutes, then someone marks it
        urgent. The store already knows it, so nothing else re-order it: the reconcile must."""
        self.claim()
        entry = self.store.get(ID)["queue"]
        self.assertFalse(entry["urgent"])
        self.github.issues[(REPO, 7)]["labels"] = [{"name": "factory:ready"}, {"name": "factory:urgent"}]
        wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual((wi["state"], wi["queue"]["urgent"]), ("queued", True))
        self.assertEqual(wi["queue"]["claimedBy"], entry["claimedBy"])  # a re-order is not a release

    def test_a_claim_waiting_out_its_seconds_sees_an_urgent_label_added_meanwhile(self):
        """A claim that is already blocking has to notice an urgent label, or the promise that urgent
        work wakes an agent within one poll is only true for the tick. The poll interval is shortened
        to zero here so the test does not wait it out."""
        self.foreman.approve_queued(ID, "day-desk")  # empty the store's queue
        self.github.add_issue(REPO, 9)
        original = self.github.labelled_issues
        reads = []

        def labelled(repo, label, *args, **kwargs):
            reads.append(label)
            if len(reads) == 2:  # the label is added while the claim is already waiting
                self.github.issues[(REPO, 9)]["labels"] = [{"name": "factory:urgent"}]
            return original(repo, label, *args, **kwargs)

        self.github.labelled_issues = labelled
        with mock.patch.object(foreman_module, "QUEUE_URGENT_POLL", 0):
            found = self.claim(seconds=20)
        self.assertGreaterEqual(len(reads), 2, "the waiting claim re-reads GitHub")
        self.assertIsNotNone(found, "an urgent label added during the wait is taken in")
        self.assertEqual(found["workItem"]["id"], "o/r#9")

    def test_a_lost_store_rebuilds_an_urgent_issue_too(self):
        """The queue inherits GitHub as the truth (ADR-0009), so a store rebuilt from scratch has to
        take in an issue that only ever carried the urgent label."""
        self.store.close()
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.foreman = Foreman(self.store, self.github, self.config, self.relay)
        self.github.add_issue(REPO, 9, labels=("factory:urgent",))
        self.assertIn("o/r#9", self.foreman.rebuild())
        self.assertEqual(self.store.get("o/r#9")["state"], "queued")

    def test_dependencies_change_on_a_queued_workitem(self):
        """Dependencies are set before any worker pass (ADR-0042), and a queued WorkItem has not had
        one: with the queue on, the state that used to be `ready` is `queued`."""
        self.github.add_issue(REPO, 8, labels=("factory:ready",))
        self.foreman.set_dependencies(ID, [8], by="owner")
        self.assertEqual(self.store.get(ID)["state"], "queued")

    def test_a_queued_workitem_whose_pr_exists_already_follows_the_pr(self):
        """A store rebuilt by GitHub, or a PR opened by hand, leaves an entry waiting on a decision that
        the PR has already answered: it follows the PR and drops the stale entry."""
        self.github.add_pr(REPO, 12, "factory/7", "a" * 40)
        wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "pr_open")
        self.assertIsNone(wi["queue"])

    def test_a_claim_carries_what_a_manager_decides_on(self):
        """The claim is the one read a manager gets: what the issue must satisfy, what it may touch,
        what earlier passes cost and why they failed, and who may run it. Facts only, so a decision
        about this WorkItem never needs a second call (ADR-0136)."""
        view = self.claim()
        self.assertEqual([c["claim"] for c in view["acceptance"]],
                         ["default_config_path() raises when the user registry is missing",
                          "README names the registry path"])
        self.assertEqual(view["paths"], [])
        self.assertEqual((view["attempts"]["used"], view["attempts"]["left"]), (0, 4))  # Config's default
        self.assertFalse(view["attempts"]["member"])
        self.assertTrue(view["attempts"]["gateFixFirst"])
        self.assertEqual(view["attempts"]["failures"], [])
        self.assertEqual(view["workers"]["ran"], [])
        self.assertEqual(view["workers"]["failed"], [])
        self.assertEqual(view["workers"]["allowed"], ["codex"])  # Config's only worker
        self.assertIsNone(view["gate"])

    def test_a_claim_reports_the_passes_that_ran_and_the_ones_that_failed(self):
        """A manager that cannot see who ran and why they failed decides blind: the packet carries both
        from the store, which already holds every pass and every failure."""
        self.store.save_workorder({"id": "wo1", "workItemId": ID, "worker": "pi-deepseek-factory"})
        self.store.save_workorder({"id": "wo2", "workItemId": ID, "worker": "pi-codex-factory"})
        self.store.log("attempt_failed", {"worker": "pi-codex-factory", "cause": "timeout",
                                          "reason": "the pass ran out of its 180 seconds"}, ID)
        view = self.claim()
        self.assertEqual(view["attempts"]["used"], 2)
        self.assertEqual(view["attempts"]["left"], 2)
        self.assertEqual(view["attempts"]["failures"],
                         [{"worker": "pi-codex-factory", "cause": "timeout",
                           "reason": "the pass ran out of its 180 seconds"}])
        self.assertEqual(view["workers"]["ran"], [{"worker": "pi-deepseek-factory", "passes": 1},
                                                  {"worker": "pi-codex-factory", "passes": 1}])
        self.assertEqual(view["workers"]["failed"], ["pi-codex-factory"])

    def test_a_person_retry_buys_a_fresh_budget_in_the_packet_as_it_does_in_the_dispatch(self):
        """One definition of the budget, read by the packet and spent by the dispatch (ADR-0112,
        ADR-0136). A retry a person granted writes off the passes before it, and the packet has to say
        so: an agent told `left: 0` would hold work the dispatch would happily run."""
        for number in range(4):
            self.store.save_workorder({"id": f"wo{number}", "workItemId": ID, "worker": "codex"})
        self.assertEqual(self.claim()["attempts"]["left"], 0)
        wi = self.store.get(ID)
        self.store.put(dict(wi, passesBefore=4,  # a person's retry
                            queue=dict(wi["queue"], claimedAt="2026-01-01T00:00:00Z")))
        self.assertEqual(self.claim()["attempts"]["left"], 4)

    def test_the_packet_says_when_the_issue_could_not_be_read(self):
        """Empty acceptance and empty paths are a fact, and so is a failed read: a manager told "no
        bullets" from a 502 would approve work it cannot see (ADR-0136)."""
        self.github.issues.pop((REPO, 7))
        view = self.claim()
        self.assertIn("issueError", view)
        self.assertEqual((view["acceptance"], view["paths"]), ([], []))

    def test_a_member_claim_carries_the_project_budget_it_also_spends(self):
        """A member's pass spends the Project's budget, and a paused Project dispatches nothing. Without
        those facts the manager approves work the next tick silently refuses (ADR-0136)."""
        self.store.create_project({"id": "demo", "repo": REPO, "title": "Demo", "state": "paused",
                                   "members": [ID], "budget": {"passes": 10, "deadline": None},
                                   "passesUsed": 7})
        self.store.put(dict(self.store.get(ID), projectId="demo"))
        view = self.claim()
        self.assertTrue(view["attempts"]["member"])
        self.assertEqual((view["attempts"]["projectState"], view["attempts"]["projectLeft"]),
                         ("paused", 3))

    def test_the_two_queue_tools_are_the_agent_writes(self):
        built = tools.build(self.store, self.foreman, agent=True)
        self.assertEqual(built["factory_queue_next"].fn({"by": "day-desk"})["next"]["workItem"]["id"], ID)
        self.assertEqual(built["factory_queue_approve"].fn(
            {"id": ID, "by": "day-desk"})["workItem"]["state"], "ready")


if __name__ == "__main__":
    unittest.main()
