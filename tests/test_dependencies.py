"""factory_set_dependencies (ADR-0042): the owner or Day Desk changes what a WorkItem that no worker has
touched waits for, so independent issues of a plan run side by side."""
import unittest

from factory_mcp import metrics, tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.store import Store
from tests.fakes import ISSUE_BODY, FakeGit, FakeGitHub, FakeRelay, result_line
from tests.test_plan import FIRST, REPO, SECOND

THIRD = "o/r#202"
# Distinct fences per issue (ADR-0088): two plan issues listing the same files run in turn, and
# the tests here are about the dependency edges, so only removing an edge may run two at once.
BODY_ONE = ISSUE_BODY.replace("## Acceptance", "Paths: src/one*\n\n## Acceptance")
BODY_TWO = ISSUE_BODY.replace("## Acceptance", "Paths: src/two*\n\n## Acceptance")
BODY_THREE = ISSUE_BODY.replace("## Acceptance", "Paths: src/three*\n\n## Acceptance")


class SetDependenciesTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        FakeGit(self.github).install(self)
        self.foreman = Foreman(Store(":memory:"), self.github, Config(mcp_token="t", repos=frozenset({REPO})),
                               self.relay)
        self.foreman.start_plan(REPO, [{"title": "One", "body": BODY_ONE},
                                       {"title": "Two", "body": BODY_TWO},
                                       {"title": "Three", "body": BODY_THREE}], "amteshwar")

    def body(self, number):
        return self.github.issues[(REPO, number)]["body"]

    def set(self, workitem_id, numbers, by="amteshwar", **kw):
        return self.foreman.set_dependencies(workitem_id, numbers, by, **kw)

    def test_removing_the_dependency_lets_two_issues_of_a_plan_run_side_by_side(self):
        self.set(SECOND, [])
        self.assertEqual(self.body(201), BODY_TWO)
        self.foreman.tick()
        self.assertEqual([self.foreman.store.get(i)["state"] for i in (FIRST, SECOND, THIRD)],
                         ["implementing", "implementing", "ready"])
        self.assertEqual(self.foreman.store.get(THIRD)["waitingOn"], 201)

    def test_dependencies_are_replaced_and_a_wait_that_named_the_old_one_is_cleared(self):
        self.foreman.config = Config(mcp_token="t", repos=frozenset({REPO}), passes_per_repo=1)
        self.foreman.tick()  # FIRST takes the only slot; SECOND waits for 200
        self.assertEqual(self.foreman.store.get(SECOND)["waitingOn"], 200)
        self.assertIsNone(self.set(SECOND, [])["waitingOn"])
        self.set(THIRD, [200])
        self.assertEqual(self.body(202), f"Depends on #200\n\n{BODY_THREE}")
        self.set(THIRD, [200, 201])
        self.assertEqual(self.body(202), f"Depends on #200\nDepends on #201\n\n{BODY_THREE}")

    def test_the_change_is_audited_with_who_asked_and_whether_the_agent_did(self):
        self.set(THIRD, [200], by="day-desk", agent=True)
        self.assertEqual([h["detail"] for h in self.foreman.store.history(THIRD)
                          if h["action"] == "dependencies_changed"],
                         [{"by": "day-desk", "was": [201], "now": [200], "agent": True}])

    def test_the_audit_entry_leaves_the_end_state_reading_of_the_history_working(self):
        """`from` and `to` hold states in every other entry, and metrics looks them up in a set, so a
        list there would break factory_report for the whole store."""
        self.set(THIRD, [200])
        self.assertIsNone(metrics.ended_at(self.foreman.store.history(THIRD)))

    def test_a_workitem_no_worker_has_touched_may_change_and_any_other_may_not(self):
        self.foreman.tick()  # FIRST gets a pass
        with self.assertRaisesRegex(ToolError, "o/r#200 is implementing; dependencies change only"):
            self.set(FIRST, [])
        self.foreman.store.put(dict(self.foreman.store.get(FIRST), state="ready"))  # a retry: a pass is on record
        with self.assertRaisesRegex(ToolError, "o/r#200 already had a worker pass, so its dependencies stay"):
            self.set(FIRST, [])
        self.foreman.store.put(dict(self.foreman.store.get(FIRST), state="human_waiting"))
        with self.assertRaisesRegex(ToolError, "o/r#200 already had a worker pass, so its dependencies stay"):
            self.set(FIRST, [])
        for state in ("pr_open", "closed", "cancelled"):
            self.foreman.store.put(dict(self.foreman.store.get(THIRD), state=state))
            with self.assertRaisesRegex(ToolError, f"is {state}; dependencies change only"):
                self.set(THIRD, [])
        self.assertEqual(self.body(202), f"Depends on #201\n\n{BODY_THREE}")

    def test_a_human_waiting_workitem_with_no_pass_may_change_its_order(self):
        # Agentspace#5: its dependency was cancelled and replaced, so the order was fixable before a pass.
        self.foreman.store.put(dict(self.foreman.store.get(THIRD), state="human_waiting",
                                    blockedOn="this issue depends on #201, which ended cancelled"))
        self.set(THIRD, [200], by="day-desk", agent=True)
        self.assertEqual(self.body(202), f"Depends on #200\n\n{BODY_THREE}")
        wi = self.foreman.resolve_human(THIRD, "retry", "wait for the replacement", "day-desk", agent=True)
        self.assertEqual((wi["state"], wi["blockedOn"]), ("ready", None))

    def test_a_dependency_must_be_an_open_issue_of_the_same_repo_that_has_not_ended(self):
        self.github.add_issue(REPO, 300, state="closed")
        self.github.add_issue(REPO, 301)
        self.foreman.store.put(dict(self.foreman.store.get(FIRST), state="exhausted", terminalReason="4 passes"))
        cases = [("o/other#3 as text", ["o/other#3"], "list of at most 10 issue numbers in the WorkItem's own repo"),
                 ("itself", [202], "#202 is this issue"),
                 ("missing", [999], "#999 does not exist"),
                 ("closed unmerged", [300], "#300 was closed without a merged PR"),
                 ("exhausted", [200], "#200 ended exhausted: 4 passes"),
                 ("twice", [201, 201], "names an issue twice"),
                 ("a bool", [True], "list of at most 10")]
        for case, numbers, reason in cases:
            with self.subTest(case), self.assertRaisesRegex(ToolError, reason):
                self.set(THIRD, numbers)
        with self.assertRaisesRegex(ToolError, "#12 is a pull request"):
            self.github.issues[(REPO, 12)] = {"number": 12, "state": "open", "body": "", "labels": [],
                                              "pull_request": {}}
            self.set(THIRD, [12])
        self.assertEqual(self.body(202), f"Depends on #201\n\n{BODY_THREE}")

    def test_a_change_that_would_make_a_cycle_is_refused(self):
        with self.assertRaisesRegex(ToolError, "#201 depends on #200, so #200 cannot depend on #201: "
                                               "that would make a cycle"):
            self.set(FIRST, [201])
        with self.assertRaisesRegex(ToolError, "#201 depends on #200, so #200 cannot depend on #202"):
            self.set(FIRST, [202])  # 202 waits for 201, which waits for 200: a path, not a direct line
        self.assertEqual(self.body(200), BODY_ONE)

    def test_an_unknown_workitem_and_a_missing_name_are_refused(self):
        with self.assertRaisesRegex(ToolError, "o/r#9 is not a WorkItem the factory knows"):
            self.set("o/r#9", [])
        with self.assertRaisesRegex(ToolError, "needs the name of the person who asked"):
            self.set(THIRD, [], by=" ")

    def test_the_tool_is_a_write_tool_for_the_agent_and_the_owner(self):
        for agent in (True, False):
            built = tools.build(self.foreman.store, self.foreman, agent=agent)
            tool = built["factory_set_dependencies"]
            self.assertEqual(tool.effect, "write")
            result = tool.fn({"id": THIRD, "dependsOn": [200], "by": "x"})
            self.assertEqual(result["workItem"]["id"], THIRD)
        self.assertEqual([h["detail"].get("agent") for h in self.foreman.store.history(THIRD)
                          if h["action"] == "dependencies_changed"], [True, None])


class PathsOverlapTest(unittest.TestCase):
    """ADR-0088: two ready nodes that list the same files run one after the other. Disjoint nodes
    run side by side, and a node waiting for a person holds nothing."""
    first_body = ISSUE_BODY.replace("## Acceptance", "Paths: app*, lib*\n\n## Acceptance")
    disjoint = ISSUE_BODY.replace("## Acceptance", "Paths: web*\n\n## Acceptance")
    same = ISSUE_BODY.replace("## Acceptance", "Paths: app*\n\n## Acceptance")

    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.git = FakeGit(self.github).install(self)
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO})), self.relay)

    def plan(self, one, two):
        """Two independent issues of one repo: no Depends line, so only the overlap can hold two."""
        self.foreman.start_plan(REPO, [{"title": "One", "body": one},
                                       {"title": "Two", "body": two, "after": []}], "amteshwar")

    def states(self):
        return [self.foreman.store.get(f"{REPO}#{n}")["state"] for n in (200, 201)]

    def test_two_ready_nodes_with_disjoint_paths_dispatch_in_one_tick(self):
        self.plan(self.first_body, self.disjoint)
        self.foreman.tick()
        self.assertEqual(self.states(), ["implementing", "implementing"])

    def test_two_ready_nodes_with_overlapping_paths_run_in_turn(self):
        self.plan(self.first_body, self.same)
        self.foreman.tick()
        self.assertEqual(self.states(), ["implementing", "ready"])
        self.assertEqual([h["detail"] for h in self.foreman.store.history(SECOND)
                          if h["action"] == "waiting"], [{"on": FIRST, "paths": ["app*"]}])
        first = self.foreman.store.get(FIRST)
        self.git.files = (("app/one.py", 5, 0),)  # inside FIRST's fence: the push must land
        self.relay.finish(first["providerTaskId"], output=result_line(first["activeWorkOrderId"]))
        self.foreman.tick()  # the push lands and the PR opens
        self.github.add_run(REPO, "c1")
        self.github.add_verdict(REPO, self.github.pulls[-1]["number"], "MERGE", 1, "c1")
        self.foreman.tick()
        self.github.pulls[-1].update(state="closed", merged_at="2026-09-24T03:00:00Z")
        self.foreman.tick()  # the merge ends the first node's hold
        self.assertEqual(self.states(), ["closed", "implementing"])

    def test_a_node_waiting_for_a_person_holds_no_files(self):
        self.plan(self.first_body, self.same)
        self.foreman.store.put(dict(self.foreman.store.get(FIRST), state="human_waiting",
                                    blockedOn="two attempts failed", owner="amteshwar"))
        self.foreman.tick()
        self.assertEqual(self.states(), ["human_waiting", "implementing"])


if __name__ == "__main__":
    unittest.main()
