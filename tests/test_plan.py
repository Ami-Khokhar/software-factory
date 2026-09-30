"""factory_start_plan (ADR-0024): one approval opens a plan's issues in order, and a ready WorkItem
waits for the issue its `Depends on #n` line names."""
import dataclasses
import unittest

from factory_mcp import tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.github import GitHubError
from factory_mcp.mcp import ToolError
from factory_mcp.store import Store
from tests.fakes import ISSUE_BODY, FakeGit, FakeGitHub, FakeRelay, result_line
from tests.test_actions import schema_errors

REPO, FIRST, SECOND = "o/r", "o/r#200", "o/r#201"  # FakeGitHub numbers new issues from 200
NOT_TO_CHANGE = "- AGENTS.md keeps its rules — done when: `git diff main` does not list AGENTS.md\n"


def plan(*titles):
    return [{"title": title, "body": ISSUE_BODY} for title in titles]


class StartPlanTest(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()
        self.foreman = Foreman(Store(":memory:"), self.github, Config(mcp_token="t", repos=frozenset({REPO})),
                               FakeRelay())

    def test_one_approval_opens_every_issue_in_order_ready_and_depending_on_the_one_before(self):
        made = self.foreman.start_plan(REPO, plan("One", "Two", "Three"), "amteshwar")
        numbers = [item["issue"] for item in made["issues"]]
        issues = [self.github.issues[(REPO, number)] for number in numbers]
        self.assertEqual([issue["title"] for issue in issues], ["One", "Two", "Three"])
        self.assertEqual([issue["body"] for issue in issues],
                         [ISSUE_BODY, f"Depends on #{numbers[0]}\n\n{ISSUE_BODY}",
                          f"Depends on #{numbers[1]}\n\n{ISSUE_BODY}"])
        self.assertEqual([[label["name"] for label in issue["labels"]] for issue in issues],
                         [["factory:ready"]] * 3)
        for number in numbers:
            workitem_id = f"{REPO}#{number}"
            self.assertEqual(self.foreman.store.get(workitem_id)["state"], "ready")
            self.assertEqual([h["detail"] for h in self.foreman.store.history(workitem_id)
                              if h["action"] == "approved"], [{"by": "amteshwar"}])

    def test_one_issue_that_fails_a_check_opens_no_issue(self):
        cases = [
            ("no check", REPO, plan("One") + [{"title": "Two", "body": "## Acceptance\n- it works"}],
             "issue 2 of the plan needs an '## Acceptance' list .* Without a check: it works"),
            ("a workflow change", REPO, plan("One", "Two") + [{"title": "Edit .github/workflows/ci.yml",
                                                             "body": ISSUE_BODY}],
             "issue 3 of the plan names .github/, which only a person changes"),
            ("AGENTS.md named", REPO, [{"title": "One", "body": ISSUE_BODY + NOT_TO_CHANGE}],
             "issue 1 of the plan names AGENTS.md"),
            ("no title", REPO, [{"title": " ", "body": ISSUE_BODY}], "issue 1 of the plan needs a title and a body"),
            ("11 issues", REPO, plan(*(f"T{n}" for n in range(11))), "a plan has 1 to 10 issues"),
            ("no issues", REPO, [], "a plan has 1 to 10 issues"),
            ("a repo off the allowlist", "o/other", plan("One"), "o/other is not in FACTORY_REPOS"),
        ]
        for case, repo, issues, reason in cases:
            with self.subTest(case), self.assertRaisesRegex(ToolError, reason):
                self.foreman.start_plan(repo, issues, "amteshwar")
        with self.assertRaisesRegex(ToolError, "needs the name of the person who approved it"):
            self.foreman.start_plan(REPO, plan("One"), " ")
        self.assertEqual((self.github.issues, self.foreman.store.all()), ({}, []))

    def test_a_github_failure_mid_plan_names_the_issues_already_opened(self):
        create = self.github.create_issue

        def refuse_two(repo, title, body, labels):
            if title == "Two":
                raise GitHubError(502, "Bad Gateway")
            return create(repo, title, body, labels)
        self.github.create_issue = refuse_two
        with self.assertRaisesRegex(ToolError, "GitHub 502: Bad Gateway. The plan stopped. Issues opened, "
                                               "which the factory works on: #200$"):
            self.foreman.start_plan(REPO, plan("One", "Two", "Three"), "amteshwar")
        self.assertEqual(list(self.github.issues), [(REPO, 200)])

    def test_an_issue_opened_but_refused_at_intake_is_named_as_one_the_factory_will_not_run(self):
        self.foreman.config = dataclasses.replace(self.foreman.config, trusted_authors=frozenset({"someone"}))
        with self.assertRaisesRegex(ToolError, r"who is not a trusted author .*\. The plan stopped\. Issues "
                                               r"opened, which the factory works on: none\. #200 was opened "
                                               r"too, but the factory will not run it: fix the cause above, "
                                               r"or close #200$"):
            self.foreman.start_plan(REPO, plan("One", "Two"), "amteshwar")
        self.assertEqual(list(self.github.issues), [(REPO, 200)])

    def test_only_a_whole_protected_path_word_refuses_a_plan(self):
        cases = [("raw.githubusercontent.com", True), ("AGENTS.mdx", True),
                 ("edit .github/workflows/ci.yml", False), ("keep `AGENTS.md`.", False),
                 ("docs/AGENTS.md", False)]
        for mention, accepted in cases:
            with self.subTest(mention):
                self.setUp()
                body = ISSUE_BODY + f"- it mentions {mention} — done when: the README says so\n"
                if accepted:
                    self.assertEqual(len(self.foreman.start_plan(REPO, [{"title": "T", "body": body}],
                                                                 "amteshwar")["issues"]), 1)
                else:
                    with self.assertRaisesRegex(ToolError, "which only a person changes"):
                        self.foreman.start_plan(REPO, [{"title": "T", "body": body}], "amteshwar")


class DependsOnTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        FakeGit(self.github).install(self)
        self.foreman = Foreman(Store(":memory:"), self.github, Config(mcp_token="t", repos=frozenset({REPO})),
                               self.relay)
        self.foreman.start_plan(REPO, plan("One", "Two"), "amteshwar")

    def get(self, workitem_id):
        wi = self.foreman.store.get(workitem_id)
        self.assertEqual(schema_errors("workitem", wi), [])
        return wi

    def test_a_dependent_waits_while_its_dependency_is_open_and_starts_once_it_merged(self):
        self.foreman.tick()
        first = self.get(FIRST)
        self.relay.finish(first["providerTaskId"], output=result_line(first["activeWorkOrderId"]))
        self.foreman.tick()  # no worker runs in the repo now, so only the dependency holds SECOND
        self.assertEqual(self.get(FIRST)["state"], "pr_open")
        self.assertEqual((self.get(SECOND)["state"], self.get(SECOND)["waitingOn"]), ("ready", 200))
        self.assertEqual((self.foreman.store.workorders(SECOND), len(self.relay.tasks)), ([], 1))
        summary = tools.build(self.foreman.store, self.foreman)["factory_status"].fn({})
        self.assertIn({"id": SECOND, "state": "ready", "waitingOn": 200},
                      [{key: wi[key] for key in ("id", "state", "waitingOn")} for wi in summary["workItems"]])

        self.github.add_run(REPO, "c1")
        self.github.add_verdict(REPO, 100, "MERGE", 1, "c1")
        self.foreman.tick()
        self.assertEqual((self.get(FIRST)["state"], self.get(SECOND)["state"]), ("merge_pending", "ready"))
        self.github.pulls[0].update(state="closed", merged_at="2026-09-24T03:00:00Z")
        self.foreman.tick()  # FIRST is merged and its issue closed; then SECOND starts
        self.assertEqual(self.get(FIRST)["state"], "closed")
        self.assertEqual((self.get(SECOND)["state"], self.get(SECOND)["waitingOn"]), ("implementing", None))

    def test_a_dependency_that_ends_without_a_merge_sends_the_dependent_to_a_person(self):
        def ended(state, reason):
            def end():
                self.foreman.store.put(dict(self.foreman.store.get(FIRST), state=state, terminalReason=reason))
            return end

        def close_issue():
            self.github.issues[(REPO, 200)]["state"] = "closed"

        def depend_on_a_missing_issue():
            self.github.issues[(REPO, 201)]["body"] = f"Depends on #999\n\n{ISSUE_BODY}"
        cases = [("closed without a merged PR", close_issue, "#200, which was closed without a merged PR"),
                 ("exhausted", ended("exhausted", "4 worker passes used"),
                  "#200, which ended exhausted: 4 worker passes used"),
                 ("cancelled", ended("cancelled", "closed by amteshwar: not needed"),
                  "#200, which ended cancelled: closed by amteshwar: not needed"),
                 ("missing", depend_on_a_missing_issue, "#999, which does not exist")]
        for case, end, reason in cases:
            with self.subTest(case):
                self.setUp()
                end()
                self.foreman.tick()
                second = self.get(SECOND)
                self.assertEqual((second["state"], second["waitingOn"]), ("human_waiting", None))
                self.assertIn(f"this issue depends on {reason}. Edit or remove", second["blockedOn"])
                self.assertEqual(self.foreman.store.workorders(SECOND), [])
                self.assertEqual([number for _, number, _ in self.github.posted], [201])  # the owner hears

    def test_a_waiting_dependent_whose_own_issue_closes_waits_no_more(self):
        self.foreman.tick()
        self.assertEqual(self.get(SECOND)["waitingOn"], 200)
        self.github.issues[(REPO, 201)]["state"] = "closed"
        self.foreman.tick()
        self.assertEqual((self.get(SECOND)["state"], self.get(SECOND)["waitingOn"]), ("cancelled", None))

    def test_after_the_owner_removes_the_line_a_retry_starts_the_work(self):
        self.foreman.store.put(dict(self.foreman.store.get(FIRST), state="exhausted", terminalReason="budget"))
        self.foreman.tick()
        self.assertEqual(self.get(SECOND)["state"], "human_waiting")
        self.github.issues[(REPO, 201)]["body"] = ISSUE_BODY  # GitHub holds the order (ADR-0009)
        self.foreman.resolve_human(SECOND, "retry", "run it without #200", "amteshwar")
        self.foreman.tick()
        self.assertEqual(self.get(SECOND)["state"], "implementing")


if __name__ == "__main__":
    unittest.main()
