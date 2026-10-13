"""The plan recovery allowance (ADR-0069): one owner approval, durably stored, counted, scoped.

Day Desk spends it on evidence-based retries and open-issue replacements without the owner
answering each attempt and without falsely setting ownerAsked. Safety stops and a confirmed
environment fault still need a person, an exhausted or out-of-scope use refuses, one allowance
is one approval per plan id, and two racing spends cannot both take the same use."""
import tempfile
import threading
import unittest
from pathlib import Path

from factory_mcp import tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.store import Conflict, Store
from tests.fakes import ISSUE_BODY, FakeGitHub, FakeRelay, workitem

REPO, ORIGINAL, REPLACEMENT = "o/r", "o/r#201", 300
PLAN = "plan/factory#239"


class AllowanceTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.foreman = Foreman(self.store, FakeGitHub(), Config(mcp_token="t", repos=frozenset({REPO})),
                               FakeRelay())
        self.agent, self.owner = tools.build(self.store, self.foreman, agent=True), tools.build(self.store, self.foreman)
        self.store.put(workitem(state="blocked", blockedOn="verify exited 1"))

    def approve(self, remaining=2, actions=("retry",), plan_id=PLAN):
        return self.owner["factory_approve_plan_allowance"].fn(
            {"planId": plan_id, "actions": list(actions), "remaining": remaining, "by": "Ami-Khokhar"})

    def allow(self):  # within one test, each subTest's use keeps its own plan id
        self.approve(remaining=1, plan_id=f"{PLAN}/{len(self.store.allowances()) + 1}")

    def use(self, **kw):
        args = {"id": "o/r#1", "planId": kw.get("planId", PLAN), "action": kw.get("action", "retry"),
                "note": kw.get("note", "the tail shows a stale .pyc, a rerun after clearing it is sound"),
                "by": "day-desk"}
        return kw.get("tools", self.agent)["factory_use_allowance"].fn(args)

    def test_one_owner_approval_is_bounded_and_durable(self):
        """The owner approves once; a second approval of the same plan refuses, the remaining count
        survives a restart of the store, and status shows what was authorized."""
        approved = self.approve(remaining=2)
        self.assertEqual(approved, {"planId": PLAN, "actions": ["retry"], "remaining": 2})
        with self.assertRaisesRegex(ToolError, "already approved"):
            self.approve(remaining=5)
        self.assertEqual(self.store.allowance(PLAN)["remaining"], 2)
        with tempfile.TemporaryDirectory() as dir:
            path = str(Path(dir) / "factory.sqlite3")
            durable = Store(path)
            durable.approve_allowance(PLAN, ["retry"], 2, "Ami-Khokhar")
            durable.close()
            reopened = Store(path)  # a fresh open of the same file after a restart
            self.assertEqual(reopened.allowance(PLAN)["remaining"], 2)
            reopened.close()
        summary = self.owner["factory_status"].fn({})["planAllowances"]
        self.assertEqual(summary, [{"planId": PLAN, "actions": ["retry"], "remaining": 2,
                                    "approvedBy": "Ami-Khokhar", "approvedAt": summary[0]["approvedAt"]}])

    def test_an_allowance_retry_moves_the_workitem_without_a_fresh_budget_or_ownerAsked(self):
        """Day Desk retries on the evidence; the per-item pass budget stays as it is, nothing marks
        ownerAsked, Day Desk's one own answer is not spent, and the audit names the decision."""
        before = self.store.get("o/r#1")
        self.approve(remaining=1)
        result = self.use()  # Day Desk, driving
        self.assertEqual(result["workItem"]["state"], "ready")
        after = self.store.get("o/r#1")
        self.assertEqual(after.get("passesBefore"), before.get("passesBefore"))
        spend = [h for h in self.store.history("o/r#1") if h["action"] == "allowance_spent"][-1]["detail"]
        self.assertEqual((spend["planId"], spend["action"], spend["remaining"]), (PLAN, "retry", 0))
        self.assertNotIn("ownerAsked", spend)
        self.assertTrue("stale .pyc" in spend["evidence"])  # the decision's evidence rides with it
        status = self.owner["factory_status"].fn({"id": "o/r#1"})["allowanceUse"]
        self.assertEqual((status["planId"], status["action"], status["remaining"]), (PLAN, "retry", 0))

    def test_the_evidence_rides_sanitized_and_names_no_secret(self):
        self.approve(remaining=1)
        self.use(note="rerun with TOKEN=ghp_secretzqx seen in the log cleared")
        spend = [h for h in self.store.history("o/r#1") if h["action"] == "allowance_spent"][-1]
        self.assertNotIn("ghp_secret", spend["detail"]["evidence"])
        self.assertIn("<redacted>", spend["detail"]["evidence"])

    def test_an_exhausted_or_out_of_scope_or_unapproved_use_refuses(self):
        self.approve(remaining=1)
        self.use()
        self.store.put(dict(self.store.get("o/r#1"), state="blocked", blockedOn="failing again"))
        with self.assertRaisesRegex(ToolError, "is exhausted; the owner decides"):  # no second use
            self.use()
        with self.assertRaisesRegex(ToolError, "permits \\['retry'\\], not 'regate'"):
            self.use(action="regate", note="out of scope", planId=PLAN)
        with self.assertRaisesRegex(ToolError, "no plan allowance for 'plan/none'"):
            self.use(planId="plan/none")
        self.assertEqual(self.store.get("o/r#1")["state"], "blocked")  # nothing moved

    def test_duplicate_concurrent_spend_takes_the_use_once(self):
        self.approve(remaining=1)
        results, errors = [], []
        def race():
            try:
                results.append(self.store.spend_allowance(PLAN, "retry", workitem_id="o/r#1"))
            except Conflict as exc:
                errors.append(str(exc))
        threads = [threading.Thread(target=race) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(errors), 1)
        self.assertEqual(Store(":memory:").allowance(PLAN), None)  # untouched stores stay untouched
        self.assertEqual(self.store.allowance(PLAN)["remaining"], 0)

    def test_a_safety_stop_needs_the_owner_and_spends_nothing(self):
        for blocked_on in ("pr-gatekeeper decided HUMAN; read the latest gate comment",
                           "the Result changes protected path .github/workflows/ci.yml",
                           "access denied: the token cannot read check runs"):
            with self.subTest(blocked_on=blocked_on):
                self.store.put(dict(self.store.get("o/r#1"), state="blocked", blockedOn=blocked_on))
                self.allow()
                plan_id = self.store.allowances()[-1]["planId"]
                with self.assertRaisesRegex(ToolError, "safety stop"):
                    self.use(planId=plan_id)
                self.assertEqual(self.store.allowance(plan_id)["remaining"], 1)
                self.assertEqual(self.store.get("o/r#1")["state"], "blocked")

    def test_a_confirmed_environment_fault_stays_paused_until_a_person_answers(self):
        """The fault is confirmed: the last attempt named the environment and no person answered
        since, so the allowance does not retry it. After the person's answer, the normal budget
        applies again."""
        self.approve(remaining=1)
        self.store.log("attempt_failed", {"reason": "checkout failed", "cause": "checkout or setup"},
                       "o/r#1")
        with self.assertRaisesRegex(ToolError, "environment failed \\(checkout or setup\\)"):
            self.use()
        self.assertEqual(self.store.get("o/r#1")["state"], "blocked")
        self.store.log("human_resolved", {"resolution": "retry"}, "o/r#1")
        self.store.put(dict(self.store.get("o/r#1"), state="blocked", blockedOn="another failure"))
        self.assertEqual(self.use()["workItem"]["state"], "ready")


class AllowanceReplacementTest(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO})), FakeRelay())
        self.agent, self.owner = tools.build(self.foreman.store, self.foreman, agent=True), \
            tools.build(self.foreman.store, self.foreman)
        self.foreman.start_plan(REPO, [{"title": "One", "body": ISSUE_BODY},
                                       {"title": "Two", "body": ISSUE_BODY}], "Ami-Khokhar")
        self.github.add_issue(REPO, REPLACEMENT)
        self.foreman.store.put(dict(self.foreman.store.get(ORIGINAL), state="human_waiting",
                                    blockedOn="two attempts failed"))

    def test_day_desk_may_replace_open_issues_through_the_allowance(self):
        self.owner["factory_approve_plan_allowance"].fn(
            {"planId": PLAN, "actions": ["retry", "replace"], "remaining": 2, "by": "Ami-Khokhar"})
        result = self.agent["factory_replace_issue"].fn(
            {"id": ORIGINAL, "replacements": [REPLACEMENT], "by": "day-desk",
             "note": "the owner's plan covers this; evidence: both attempts failed on the timeout",
             "requestId": f"req/{ORIGINAL}", "allowancePlanId": PLAN})
        self.assertEqual(result["workItem"]["state"], "cancelled")
        self.assertEqual(self.foreman.store.allowance(PLAN)["remaining"], 1)
        replaced = next(h for h in self.foreman.store.history(ORIGINAL) if h["action"] == "replaced")
        self.assertEqual(replaced["detail"]["allowance"], PLAN)
        self.assertNotIn("ownerAsked", replaced["detail"])

    def test_an_allowance_replacement_of_planned_scope_needs_the_owner(self):
        """Title-and-body items open new work the owner never saw: that changes the material scope."""
        self.owner["factory_approve_plan_allowance"].fn(
            {"planId": PLAN, "actions": ["retry", "replace"], "remaining": 2, "by": "Ami-Khokhar"})
        with self.assertRaisesRegex(ToolError, "material scope"):
            self.agent["factory_replace_issue"].fn(
                {"id": ORIGINAL, "replacements": [{"title": "Fresh", "body": ISSUE_BODY}],
                 "by": "day-desk", "note": "scoped like the original", "requestId": "req/one",
                 "allowancePlanId": PLAN})
        self.assertEqual(self.foreman.store.allowance(PLAN)["remaining"], 2)
        self.assertEqual(self.foreman.store.get(ORIGINAL)["state"], "human_waiting")

    def test_a_journalled_replay_spends_no_second_use(self):
        """The first run spends one use and journals it; a replay, even one that refuses, resumes
        the journal and never pays a second use for the same request."""
        self.owner["factory_approve_plan_allowance"].fn(
            {"planId": PLAN, "actions": ["replace"], "remaining": 1, "by": "Ami-Khokhar"})
        args = {"id": ORIGINAL, "replacements": [REPLACEMENT], "by": "day-desk",
                "note": "evidence", "requestId": f"req/{ORIGINAL}", "allowancePlanId": PLAN}
        self.agent["factory_replace_issue"].fn(args)
        with self.assertRaisesRegex(ToolError, "only a WorkItem waiting on recovery"):  # already done
            self.agent["factory_replace_issue"].fn(args)
        self.assertEqual(self.foreman.store.allowance(PLAN)["remaining"], 0)  # still one use, not two


if __name__ == "__main__":
    unittest.main()
