"""The pause barrier blocks dispatch (#233, #234): a recovery operation's active pause barrier
holds the WorkItems it names (original, replacement, dependent) with no worker submission, work
the barrier does not name carries on, and the guard is rechecked immediately before a worker runs
so a barrier recorded after the dispatch decision still holds."""
import unittest

from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.store import PAUSE_ROLES, Store
from tests.fakes import ISSUE_BODY, FakeGit, FakeGitHub, FakeRelay
from tests.test_plan import FIRST, REPO, SECOND, plan

THIRD = "o/r#202"  # the dependent: waits for FIRST
REQUEST = "req-234"
# FakeGitHub numbers new issues from 200: FIRST waits on nothing, SECOND on FIRST, THIRD on SECOND.


def fenced(title):
    """An issue whose `Paths:` fence is its own (ADR-0088): two plan issues listing the same
    files would run in turn, and these tests are about dispatching side by side."""
    return {"title": title, "body": ISSUE_BODY.replace(
        "## Acceptance", f"Paths: src/{title.lower()}*\n\n## Acceptance")}


class BarrierTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        FakeGit(self.github).install(self)
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO})), self.relay)
        self.foreman.start_plan(REPO, [fenced("One"), fenced("Two"), fenced("Three")], "amteshwar")

    def pause(self, role, workitem_id):
        """A barrier names every role of the recovery graph; only the named member is in the plan.
        Placeholder ids stand in for the other roles so the held work alone can be read off."""
        members = dict.fromkeys(PAUSE_ROLES, "o/r#unrelated")
        members[role] = workitem_id
        request = f"{REQUEST}-{len(self.foreman.store.requests()) + 1}"
        self.foreman.store.open_request(request, "replace", {"graph": request})
        self.foreman.store.pause(request, "replace", members)
        return request

    def states(self, *ids):
        return [self.foreman.store.get(i)["state"] for i in ids]

    def test_a_paused_workitem_does_not_dispatch(self):
        self.pause("original", FIRST)
        self.foreman.tick_one(FIRST)
        self.assertEqual(self.states(FIRST), ["ready"])
        self.assertEqual(self.relay.tasks, {})
        self.assertEqual(self.foreman.store.history(FIRST)[-1]["action"], "dispatch_paused")

    def test_original_replacement_and_dependent_all_hold(self):
        self.pause("original", FIRST)
        self.pause("replacement", SECOND)
        self.pause("dependent", THIRD)
        self.foreman.tick_one(FIRST)
        self.foreman.tick_one(THIRD)
        self.assertEqual(self.states(FIRST, SECOND, THIRD), ["ready", "ready", "ready"])
        self.assertEqual(self.relay.tasks, {})

    def test_a_workitem_the_barrier_does_not_name_proceeds(self):
        """A paused dependent leaves the rest of the plan running: the barrier holds asked-for work only."""
        self.foreman.set_dependencies(SECOND, [], "amteshwar")  # drop SECOND's waiting on FIRST
        self.pause("dependent", THIRD)
        self.foreman.tick_one(FIRST)
        self.foreman.tick_one(SECOND)
        self.foreman.tick_one(THIRD)
        self.assertEqual(self.states(FIRST, SECOND, THIRD), ["implementing", "implementing", "ready"])
        self.assertEqual(len(self.relay.tasks), 2)


class PausingForeman(Foreman):
    """The barrier lands after the dispatch decision, before submit: the race the guard covers."""

    def __init__(self, *args):
        super().__init__(*args)
        self.paused = False

    def submit(self, wi, order, guarded=False):
        if not self.paused:
            self.paused = True
            request = f"{REQUEST}-{len(self.store.requests()) + 1}"
            self.store.open_request(request, "replace", {"graph": request})
            self.store.pause(request, "replace", {"original": FIRST, "replacement": SECOND,
                                                   "dependent": THIRD})
        return Foreman.submit(self, wi, order)


class RaceTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        FakeGit(self.github).install(self)
        self.foreman = PausingForeman(Store(":memory:"), self.github,
                                      Config(mcp_token="t", repos=frozenset({REPO})), self.relay)
        self.foreman.start_plan(REPO, plan("One", "Two", "Three"), "amteshwar")

    def release(self):
        request_id = self.foreman.store.requests()[-1]["requestId"]
        self.foreman.store.advance_request(request_id, "pushing")
        self.foreman.store.advance_request(request_id, "done")
        self.foreman.store.release_pause(request_id)

    def test_a_barrier_recorded_before_submission_holds_and_a_release_resumes(self):
        self.foreman.tick_one(FIRST)
        self.assertEqual(self.foreman.store.get(FIRST)["state"], "dispatching")
        self.assertEqual(self.relay.tasks, {})
        self.assertEqual(self.foreman.store.history(FIRST)[-1]["action"], "submit_paused")
        # releasing journalled-due barrier: the saved WorkOrder submits on the next tick, no new order
        self.release()
        self.foreman.tick_one(FIRST)
        wi = self.foreman.store.get(FIRST)
        self.assertEqual((wi["state"], wi["providerTaskId"]), ("implementing", "task-1"))
