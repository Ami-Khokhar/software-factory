"""The replacement operation's pause barrier over adoption (#235): a partly run or interrupted
replace journalling records its durable barrier before any write, so after a restart or a partial
failure the rebuild discovers the ready-labelled graph members without dispatching them while an
unrelated ready issue proceeds; and when the barrier lands after the dispatch decision, the
submission recheck is what keeps the worker off."""
import os
import tempfile
import unittest

from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.store import REQUEST_PHASES, Store
from tests.fakes import FakeGit, FakeGitHub, FakeRelay
from tests.test_dependencies import THIRD
from tests.test_plan import FIRST, REPO, SECOND, plan

ORIGINAL = SECOND  # 201: ready and waiting on nothing, so its successor is dispatchable
A, B = 300, 301  # the replacements: 300 inherits nothing from 201, so it could dispatch alone
UNRELATED = 310
# FakeGitHub numbers new issues from 200: plan("One", "Two", "Three") gives 200, 201, 202.


class SplittingGitHub(FakeGitHub):
    """FakeGitHub that fails one close_issue call, so the replace stops with its writes partly done:
    the edits went through, the original is still open."""

    def __init__(self, fail_on):
        super().__init__()
        self.fail_on = fail_on

    def close_issue(self, repo, number):
        if (repo, number) == self.fail_on:
            raise RuntimeError("github dropped the connection")
        return super().close_issue(repo, number)


class BarrierTest(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".sqlite")
        self.github = SplittingGitHub(None)  # nothing fails until the test asks
        self.foreman = self.start(Store(self.path))
        FakeGit(self.github).install(self)
        self.foreman.start_plan(REPO, plan("One", "Two", "Three"), "amteshwar")
        self.foreman.set_dependencies(SECOND, [], "amteshwar")  # 201 waits on nothing: its successors, too
        self.github.add_issue(REPO, A)
        self.github.add_issue(REPO, B)
        self.github.add_issue(REPO, UNRELATED)
        self.foreman.set_dependencies(THIRD, [201], "amteshwar")
        self.foreman.store.put(dict(self.foreman.store.get(ORIGINAL), state="human_waiting",
                                    blockedOn="this issue depends on #199, which ended cancelled"))

    def start(self, store):
        return Foreman(store, self.github, Config(mcp_token="t", repos=frozenset({REPO})), FakeRelay())

    def restart(self):
        self.foreman.store.close()
        self.foreman = self.start(Store(self.path))

    def interrupted_replace(self):
        """A replace whose last GitHub write (the original's close) fails: the whole graph is
        edited, the journal is at pushing, and the call raises."""
        self.github.fail_on = (REPO, 201)
        with self.assertRaisesRegex(RuntimeError, "dropped the connection"):
            self.foreman.replace_issue(ORIGINAL, [A, B], "amteshwar",
                           "owner approves the replacement", "req")
        self.github.fail_on = None

    def tearDown(self):
        self.foreman.store.close()
        os.unlink(self.path)

    def held_graph(self):
        return sorted((role, wid) for role, ids in {
            "original": [ORIGINAL], "replacement": ["o/r#300", "o/r#301"],
            "dependent": ["o/r#202"]}.items() for wid in ids)

    def test_an_interrupted_replace_records_the_barrier_before_any_write(self):
        """The barrier names the whole graph from before the edits and outlives the failed call."""
        self.interrupted_replace()
        self.assertEqual(sorted((p["role"], p["workItemId"])
                                for p in self.foreman.store.active_pauses()),
                         self.held_graph())
        self.assertEqual(self.foreman.store.request(f"replace/{ORIGINAL}")["phase"], REQUEST_PHASES[1])
        self.assertEqual(self.foreman.relay.tasks, {})
        self.assertEqual(self.foreman.store.get(ORIGINAL)["state"], "human_waiting")

    def test_an_interrupted_attempt_cannot_release_the_barrier(self):
        """The journal decides the release: a partial operation keeps the pause until it completes."""
        self.interrupted_replace()
        with self.assertRaisesRegex(Exception, "has not completed"):
            self.foreman.store.release_pause(f"replace/{ORIGINAL}")
        self.assertTrue(self.foreman.store.active_pauses())

    def test_a_release_killed_after_the_journal_will_not_stick(self):
        """A run whose release was killed leaves the barrier over a done journal and a cancelled
        original. The next replace call cannot run a cancelled replacement again, but it does see
        the done phase, releases the barrier, and holds nothing back."""
        request = f"replace/{ORIGINAL}"
        self.foreman.replace_issue(ORIGINAL, [A, B], "amteshwar",
                           "owner approves the replacement", "req")  # the run completed and released
        self.foreman.store.pause(request, "replace", {"original": [ORIGINAL],
                                                      "replacement": [f"o/r#{A}"],
                                                      "dependent": ["o/r#202"]})  # release never journalled
        self.foreman.store.close()
        self.restart()
        self.assertTrue(self.foreman.store.active_pauses(), "the restart lost the active barrier")
        with self.assertRaisesRegex(ToolError, "is cancelled"):
            self.foreman.replace_issue(ORIGINAL, [A, B], "amteshwar",
                           "owner approves the replacement", "req")
        self.assertEqual(self.foreman.store.active_pauses(), [], "the barrier outlived its journal")
        self.assertEqual(self.foreman.store.all_workorders(), [])

    def test_after_a_restart_rebuild_holds_the_members_and_the_unrelated_proceeds(self):
        """A ready replacement with nothing to wait for would dispatch on its own facts; only the
        persisted barrier stops it, and only it: discovery of the same restart ticks the unrelated
        ready issue on."""
        self.interrupted_replace()
        self.restart()
        held = ["o/r#202", "o/r#300", "o/r#301"]
        self.foreman.tick()  # discovery, rebuild, then a tick of every WorkItem the store has
        self.assertTrue(self.foreman.store.active_pauses(), "a restart did not keep the barrier")
        for wid in held:
            with self.subTest(wid=wid):
                self.assertNotIn(self.foreman.store.get(wid)["state"], ("dispatching", "implementing"))
                self.assertFalse(any(h["action"] in ("dispatch", "submit", "submitted")
                                     for h in self.foreman.store.history(wid)))
        with self.subTest("the member its own facts would dispatch"):  # ready, and waits for nothing
            self.assertTrue(any(h["action"] == "dispatch_paused"
                                for h in self.foreman.store.history("o/r#300")),
                            "o/r#300 dispatched while its operation was incomplete")
        self.assertEqual(sorted(t["agentId"] for t in self.foreman.relay.tasks.values()),
                         ["codex", "codex"], "unrelated ready work (200, 310) did not proceed")

    def test_a_resume_completes_releases_and_the_members_run_after_the_release(self):
        self.interrupted_replace()
        self.restart()
        self.foreman.replace_issue(ORIGINAL, [A, B], "amteshwar",
                           "owner approves the replacement", "req")  # resume the journalled work
        self.assertEqual((self.foreman.store.request(f"replace/{ORIGINAL}")["phase"],
                          self.foreman.store.active_pauses()), (REQUEST_PHASES[2], []))
        self.assertEqual(self.foreman.store.get(ORIGINAL)["state"], "cancelled")
        self.foreman.tick()
        self.assertEqual(self.foreman.store.get("o/r#300")["state"], "implementing")
        for wid in ("o/r#202", "o/r#301", f"o/r#{UNRELATED}"):
            wi = self.foreman.store.get(wid)
            self.assertNotIn(wi["state"], ("dispatching", "implementing") if wid != f"o/r#{UNRELATED}"
                             else ())

    def test_the_barrier_recorded_at_submission_holds_and_a_release_resumes(self):
        """The barrier lands after the dispatch decision, before the relay call: the submission
        recheck is what keeps the worker off, and the released WorkOrder submits with no new pass."""
        request = f"replace/{ORIGINAL}"
        flight = {"started": False}

        class RacingForeman(Foreman):
            """A replace operation that begins exactly between the dispatch decision and submit."""

            def submit(self_, wi, order):
                if not flight["started"] and wi["id"] == f"o/r#{A}":
                    flight["started"] = True
                    self_.store.open_request(request, "replace",
                                             {"workItemId": ORIGINAL, "replacements": [A, B]})
                    self_.store.pause(request, "replace",
                                      {"original": [ORIGINAL], "replacement": [f"o/r#{A}", f"o/r#{B}"],
                                       "dependent": ["o/r#202"]})
                    self_.store.advance_request(request, REQUEST_PHASES[1])
                return Foreman.submit(self_, wi, order)

        self.foreman.store.put(dict(self.foreman.store.get(ORIGINAL), id=f"o/r#{A}",
                                    issueNumber=A, state="ready", blockedOn=None, waitingOn=None,
                                    terminalReason=None, stateVersion=0))  # 300 is already taken in
        foreman = RacingForeman(self.foreman.store, self.github, self.foreman.config, self.foreman.relay)
        before, wi = foreman.tick_one(f"o/r#{A}")
        self.assertEqual((before, wi["state"]), ("ready", "dispatching"))
        self.assertEqual(foreman.relay.tasks, {}, "a worker ran while the barrier was being recorded")
        self.assertEqual([h["action"] for h in foreman.store.history(f"o/r#{A}")][-1], "submit_paused")
        foreman.store.advance_request(request, REQUEST_PHASES[-1])  # the operation completed
        foreman.store.release_pause(request)
        orders = len(foreman.store.all_workorders())
        foreman.tick_one(f"o/r#{A}")
        self.assertEqual(foreman.store.get(f"o/r#{A}")["state"], "implementing")
        self.assertEqual(len(foreman.store.all_workorders()), orders,
                         "the release submitted the saved WorkOrder with no new pass")

    def test_a_kill_after_the_originals_close_does_not_freeze_the_graph(self):
        """A kill (or a store error) between the original's close and the journal's advance leaves
        the writes partly done; the tick reconciles the closed original to cancelled, and the retry
        with the same inputs ends the run's journal and releases the barrier, so the held members
        run again instead of dispatching never (ADR-0063)."""
        class DyingForeman(Foreman):
            """A replace that dies after the close, before the WorkItem write and the advance."""

            def _write(self_, old, new, action, **detail):
                if action == "replaced" and not getattr(self_, "died", False):
                    self_.died = True
                    raise RuntimeError("the process died after the close")
                return Foreman._write(self_, old, new, action, **detail)

        request = f"replace/{ORIGINAL}"
        dying = DyingForeman(self.foreman.store, self.github, self.foreman.config, self.foreman.relay)
        with self.assertRaisesRegex(RuntimeError, "died after the close"):
            dying.replace_issue(ORIGINAL, [A, B], "amteshwar",
                           "owner approves the replacement", "req")
        self.assertEqual((self.foreman.store.request(request)["phase"],
                          bool(self.foreman.store.active_pauses())), (REQUEST_PHASES[1], True))
        self.foreman.tick_one(ORIGINAL)  # a tick takes the close in: the original reconciles
        self.assertEqual(self.foreman.store.get(ORIGINAL)["state"], "cancelled")
        self.foreman.replace_issue(ORIGINAL, [A, B], "amteshwar",
                           "owner approves the replacement", "req")  # the retry with the same inputs
        self.assertEqual((self.foreman.store.request(request)["phase"],
                          self.foreman.store.active_pauses()), (REQUEST_PHASES[-1], []))
        self.assertTrue(any(h["action"] == "replace_abandoned"
                            for h in self.foreman.store.history(ORIGINAL)))
        # the release leaves the replacement standing on its own facts: discovery after the
        # release finds nothing holding it, and its dependent stays behind it
        self.foreman.tick()
        self.assertFalse(any(h["action"] == "dispatch_paused"
                             for h in self.foreman.store.history(f"o/r#{A}")))
        self.assertNotIn(self.foreman.store.get("o/r#202")["state"], ("dispatching", "implementing"),
                         "a dependent ran past the replacement that now stands before it")

    def test_a_person_who_closes_the_original_during_the_operation_does_not_freeze_the_graph(self):
        """The person's resolve on the original is the owner's recovery; the retry with the same
        inputs records the takeover, advances the journal to done and releases the barrier."""
        self.interrupted_replace()
        self.foreman.resolve_human(ORIGINAL, "close", "taking the issue myself", "owner")
        self.foreman.replace_issue(ORIGINAL, [A, B], "amteshwar",
                           "owner approves the replacement", "req")
        self.assertEqual((self.foreman.store.request(f"replace/{ORIGINAL}")["phase"],
                          self.foreman.store.active_pauses()), (REQUEST_PHASES[-1], []))
        # the release leaves the replacement dispatchable again: on its own facts now, not on the
        # barrier's, while its dependent still stands behind it
        self.foreman.tick()  # discovery and a tick decide every WorkItem on its own facts
        self.assertFalse(any(h["action"] == "dispatch_paused"
                             for h in self.foreman.store.history(f"o/r#{A}")))

    def test_a_repeat_of_a_taken_over_run_records_no_barrier_over_the_graph(self):
        """A call whose journal is already done (the person's takeover ended the run) never holds
        the graph again: the repeat returns the WorkItem it found without recording a barrier, so
        no pause is left that only a store edit could clear (ADR-0063)."""
        self.interrupted_replace()
        self.github.close_issue(REPO, A)  # the person closes the first replacement
        self.foreman.tick()  # the close is a fact the store takes in
        self.foreman.replace_issue(ORIGINAL, [A, B], "amteshwar",
                           "owner approves the replacement", "req")  # the retry: takeover, done
        self.github.reopen_issue(REPO, A)  # the replacement is ready again on GitHub
        self.foreman.replace_issue(ORIGINAL, [A, B], "amteshwar",
                           "owner approves the replacement", "req")  # the same call again
        self.assertEqual(self.foreman.store.active_pauses(), [],
                         "a done journal recorded a barrier it never released")
        self.foreman.tick()
        self.assertFalse(any(h["action"] == "dispatch_paused"
                             for h in self.foreman.store.history(f"o/r#{A}")))

    def test_a_person_who_closes_a_replacement_during_the_operation_does_not_freeze_the_graph(self):
        """A replacement the person closed cannot run again; the retry ends the operation and frees
        the rest of the barrier's graph, which its own now-ended dependency holds back."""
        self.interrupted_replace()
        self.github.close_issue(REPO, A)  # the person closes the first replacement
        self.foreman.tick()  # the close is a fact the store takes in
        self.foreman.replace_issue(ORIGINAL, [A, B], "amteshwar",
                           "owner approves the replacement", "req")
        self.assertEqual((self.foreman.store.request(f"replace/{ORIGINAL}")["phase"],
                          self.foreman.store.active_pauses()), (REQUEST_PHASES[-1], []))
        self.foreman.tick()
        for wid in (f"o/r#{B}", "o/r#202"):  # 300 was never a WorkItem the store took in
            self.assertFalse(self.foreman.store.workorders(wid),
                             f"a worker ran against {wid}, whose graph the person moved")
            self.assertNotIn(self.foreman.store.get(wid)["state"], ("dispatching", "implementing"))
