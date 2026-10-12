"""factory_replace_issue (ADR-0059): a WorkItem waiting on recovery is replaced with ordered
replacement issues. The whole graph is planned and validated before anything is written (guard 8)."""
import unittest

from pathlib import Path

from factory_mcp import tools, workitem
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.store import Store
from tests.fakes import ISSUE_BODY, FakeGit, FakeGitHub, FakeRelay
from tests.test_dependencies import THIRD
from tests.test_plan import FIRST, REPO, SECOND, plan

UPSTREAM = "o/r#199"
SKILL = Path(__file__).resolve().parent.parent / "tenant" / "hermes" / "skills" / "software-factory" / "SKILL.md"
ORIGINAL, A, B = SECOND, 300, 301  # 201 depends on 200; 300 and 301 are the replacements


class CountingGitHub(FakeGitHub):
    """FakeGitHub that remembers every call that changes GitHub, so a refusal can be shown to
    have made none."""

    def __init__(self):
        super().__init__()
        self.writes = []

    def edit_issue(self, repo, number, body):
        self.writes.append(("edit_issue", repo, number))
        return super().edit_issue(repo, number, body)

    def close_issue(self, repo, number):
        self.writes.append(("close_issue", repo, number))
        return super().close_issue(repo, number)


class ReplaceIssueTest(unittest.TestCase):
    def setUp(self):
        self.github = CountingGitHub()
        self.foreman = Foreman(Store(":memory:"), self.github, Config(mcp_token="t", repos=frozenset({REPO})),
                               FakeRelay())
        FakeGit(self.github).install(self)
        self.foreman.start_plan(REPO, plan("One", "Two", "Three"), "amteshwar")  # 201 waits for 200
        self.github.add_issue(REPO, int(UPSTREAM.split("#")[1]))
        self.github.add_issue(REPO, A)
        self.github.add_issue(REPO, B)
        self.github.add_issue(REPO, 150)
        self.foreman.set_dependencies(THIRD, [201, 150], "amteshwar")  # one edge on the original, one unrelated
        self.github.writes.clear()  # only the calls a replace makes are of interest

    def body(self, number):
        return self.github.issues[(REPO, number)]["body"]

    def wait(self, workitem_id=ORIGINAL):
        self.foreman.store.put(dict(self.foreman.store.get(workitem_id), state="human_waiting",
                                    blockedOn=f"this issue depends on #199, which ended cancelled"))

    def replace(self, workitem_id=ORIGINAL, numbers=(A, B), **kw):
        return self.foreman.replace_issue(workitem_id, list(numbers), "amteshwar", **kw)

    def test_replacements_inherit_the_original_upstream_and_chain_in_order(self):
        self.wait()
        self.replace()
        self.assertEqual(self.body(A), f"Depends on #200\n\n{ISSUE_BODY}")
        self.assertEqual(self.body(B), f"Depends on #{A}\n\n{ISSUE_BODY}")

    def test_a_dependent_swaps_only_the_original_edge_and_keeps_the_unrelated_one(self):
        self.wait()
        self.replace()
        self.assertEqual(self.body(202), f"Depends on #{B}\nDepends on #150\n\n{ISSUE_BODY}")

    def test_the_skill_replaces_then_approves_in_order(self):
        """A factory:proposed issue starts only when it is approved (round-2 finding): the guidance
        ends with the replacements approved after a successful replace call, and approves nothing
        before the call or when the call refuses."""
        text = SKILL.read_text()
        start = text.index("When one issue of the plan is beyond recovery")
        paragraph = text[start:text.index("\n\n", start)]
        replace = paragraph.index("factory_replace_issue")
        approve = paragraph.index("factory_approve_issue")
        refusal = paragraph.index("word for word")
        self.assertLess(replace, refusal)
        self.assertLess(refusal, approve)

    def test_the_original_is_cancelled_closed_and_journalled(self):
        self.wait()
        self.foreman.store.put(dict(self.foreman.store.get(ORIGINAL), waitingOn=199))
        wi = self.replace()
        self.assertEqual((wi["state"], wi["terminalReason"], wi.get("waitingOn")),
                         ("cancelled", "replaced by #300, #301", None))
        self.assertEqual(self.github.issues[(REPO, 201)]["state"], "closed")
        self.assertEqual(self.foreman.store.request(f"replace/{ORIGINAL}"),
                         {"requestId": f"replace/{ORIGINAL}", "operation": "replace",
                          "inputsFingerprint": self.foreman.store.request(f"replace/{ORIGINAL}")["inputsFingerprint"],
                          "phase": "done"})

    def test_a_refusal_makes_no_github_call_at_all(self):
        self.wait()
        # A dependent that already had a worker pass keeps its edge.
        self.foreman.store.save_workorder({"id": "wo-1", "workItemId": THIRD})
        with self.assertRaisesRegex(ToolError, "#202 already had a worker pass"):
            self.replace()
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.body(202), f"Depends on #201\nDepends on #150\n\n{ISSUE_BODY}")

    def test_approving_refused_replacements_is_refused_nothing_starts(self):
        """No activation after a failed replacement: the factory queued the graph but the dependent
        waits on an issue that would need a person to approve, and the refusal itself touched
        nothing on GitHub."""
        self.wait()
        self.foreman.store.save_workorder({"id": "wo-1", "workItemId": THIRD})
        with self.assertRaisesRegex(ToolError, "#202 already had a worker pass"):
            self.replace()
        replacement_ids = [workitem.workitem_id(REPO, number) for number in (A, B)]
        self.assertEqual([wi["state"] for wi in
                          (self.foreman.store.get(id) for id in replacement_ids) if wi], [])
        self.assertEqual(self.github.writes, [])

    def test_a_plan_that_would_make_a_cycle_is_refused(self):
        # #200 waits for #300 already, and #300 would inherit #200 as the original's upstream.
        self.foreman.set_dependencies(FIRST, [A], "amteshwar")
        self.github.writes.clear()
        self.wait()
        with self.assertRaisesRegex(ToolError, "would make a cycle: #200 depends on #300 depends on #200"):
            self.replace()
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.body(202), f"Depends on #201\nDepends on #150\n\n{ISSUE_BODY}")

    def test_invalid_originals_and_replacements_are_refused(self):
        with self.assertRaisesRegex(ToolError, "o/r#201 is ready; only a WorkItem waiting on recovery"):
            self.replace()
        self.wait()
        cases = [("itself", [201], "#201 cannot be replaced by itself"),
                 ("twice", [A, A], "names an issue twice"),
                 ("missing", [999], "does not exist"),
                 ("empty", [], "non-empty list")]
        for case, numbers, reason in cases:
            with self.subTest(case), self.assertRaisesRegex(ToolError, reason):
                self.replace(numbers=numbers)
        self.github.issues[(REPO, 12)] = {"number": 12, "state": "open", "body": "", "labels": [],
                                          "pull_request": {}}
        with self.assertRaisesRegex(ToolError, "#12 is a pull request"):
            self.replace(numbers=[12])
        self.assertEqual(self.github.writes, [])

    def test_an_original_with_a_live_worker_still_cannot_be_replaced(self):
        """A refusal that makes no GitHub call: a worker pass may still be running on the original,
        so replacing it could change the graph under it."""
        self.wait()
        self.foreman.store.save_workorder({"id": "wo-1", "workItemId": ORIGINAL})
        for live in ({"activeWorkOrderId": "wo-1"}, {"providerTaskId": "task-1"}):
            with self.subTest(live):
                self.foreman.store.put(dict(self.foreman.store.get(ORIGINAL), **live))
                with self.assertRaisesRegex(ToolError, "work is still live"):
                    self.replace()
                self.foreman.store.put(dict(self.foreman.store.get(ORIGINAL), activeWorkOrderId=None,
                                            providerTaskId=None))
        self.assertEqual(self.github.writes, [])

    def test_an_original_with_prior_attempts_and_no_live_worker_is_replaced(self):
        """The owner's case: the original had worker passes that all failed, nothing is live on it,
        and the replacements are untouched, so the whole replacement runs."""
        self.wait()
        self.foreman.store.put(dict(self.foreman.store.get(ORIGINAL),
                                    attempts=["pass one failed", "pass two failed"]))
        self.foreman.store.save_workorder({"id": "wo-1", "workItemId": ORIGINAL})
        self.foreman.store.save_workorder({"id": "wo-2", "workItemId": ORIGINAL})
        wi = self.replace()
        self.assertEqual((wi["state"], wi["terminalReason"]), ("cancelled", "replaced by #300, #301"))
        self.assertEqual(self.github.issues[(REPO, 201)]["state"], "closed")
        # the plan moved only the original's edges: the dependent keeps its unrelated one
        self.assertEqual(self.body(202), f"Depends on #{B}\nDepends on #150\n\n{ISSUE_BODY}")
        self.assertEqual([h["detail"].get("replacements") for h in self.foreman.store.history(ORIGINAL)
                          if h["action"] == "replaced"], [[A, B]])
        # the cancelled WorkItem keeps what was tried: the audit trail reads on from it
        self.assertEqual(wi["attempts"], ["pass one failed", "pass two failed"])
        self.assertEqual([h["detail"].get("replacements") for h in self.foreman.store.history(ORIGINAL)
                          if h["action"] == "replaced"], [[A, B]])
        self.assertEqual(self.foreman.store.request(f"replace/{ORIGINAL}")["phase"], "done")

    def test_a_replacement_may_not_wait_for_the_issue_it_replaces(self):
        self.wait()
        self.github.issues[(REPO, A)] = dict(self.github.issues[(REPO, A)], body=f"Depends on #201\n\n{ISSUE_BODY}")
        with self.assertRaisesRegex(ToolError, "name a fresh issue that waits for nothing"):
            self.replace(numbers=[A])
        self.assertEqual(self.github.writes, [])

    def seeded(self, number):
        """A WorkItem for an already opened and dispatched replacement issue."""
        doc = dict(self.foreman.store.get(ORIGINAL), id=workitem.workitem_id(REPO, number),
                   issueNumber=number, stateVersion=0)
        self.foreman.store.put(doc)

    def test_a_replacement_that_already_had_a_worker_pass_is_refused(self):
        self.wait()
        self.seeded(A)
        self.foreman.store.save_workorder({"id": "wo-1", "workItemId": workitem.workitem_id(REPO, A)})
        with self.assertRaisesRegex(ToolError, "#300 already had a worker pass, so it cannot be a replacement"):
            self.replace()
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.body(A), ISSUE_BODY)

    def test_the_tool_is_a_write_tool_for_the_agent_and_the_owner(self):
        for agent, original, numbers in ((True, FIRST, [A]), (False, SECOND, [B])):
            built = tools.build(self.foreman.store, self.foreman, agent=agent)
            tool = built["factory_replace_issue"]
            self.assertEqual(tool.effect, "write")
            self.wait(original)
            result = tool.fn({"id": original, "replacements": numbers, "by": "x"})
            self.assertEqual(result["workItem"]["state"], "cancelled")
            self.assertEqual([h["detail"].get("agent") for h in self.foreman.store.history(original)
                              if h["action"] == "replaced"], [True] if agent else [None])


if __name__ == "__main__":
    unittest.main()
