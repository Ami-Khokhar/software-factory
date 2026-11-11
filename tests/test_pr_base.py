"""Per-repo prBase (ADR-0141): the branch a repo's factory pull requests target and factory work
starts from. Without the setting, GitHub's default branch, as before."""
import unittest

from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.store import Store
from tests.fakes import FakeGit, FakeGitHub, FakeRelay, result_line

REPO, ID = "o/r", "o/r#7"


class PrBaseTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.git = FakeGit(self.github).install(self)
        self.github.add_issue(REPO, 7)
        self.github.branches[(REPO, "factory-main")] = "fm1"
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO}),
                                      pr_bases={REPO.lower(): "factory-main"}),
                               self.relay)
        self.foreman.intake(REPO, 7)

    def tick(self):
        return self.foreman.tick(ID)["workItem"]

    def dispatched(self):
        wi = self.tick()
        self.assertEqual(wi["state"], "implementing", wi)
        return wi, self.relay.tasks[wi["providerTaskId"]]

    def test_a_pr_base_repo_opens_its_pr_against_the_pr_base(self):
        """A repo with prBase set gets its pull request opened with that base, never the default."""
        wi, task = self.dispatched()
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
        self.assertEqual(self.tick()["state"], "pr_open")
        self.assertEqual(self.github.created_pulls, [(REPO, "factory/7", "factory-main")])

    def test_work_starts_from_the_pr_base_head(self):
        """The pass builds on prBase's head: the default branch's head (base0) is not the base."""
        self.github.branches[(REPO, "factory-main")] = "fm1"
        wi, task = self.dispatched()
        order = self.foreman.store.workorders(ID)[0]
        self.assertEqual(order["baseSha"], "fm1")
        self.assertEqual(order["baseBranch"], "factory-main")
        self.assertIn("target the factory-main branch", task["input"])
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
        self.assertEqual(self.tick()["state"], "pr_open")
        self.assertEqual(self.git.pushes, [{"branch": "factory/7", "base": "fm1", "force": True}])

    def test_size_and_changed_file_checks_compare_against_the_pr_base(self):
        """The Foreman reads the whole pull request's changed files against prBase, not the default."""
        wi, task = self.dispatched()
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
        self.assertEqual(self.tick()["state"], "pr_open")
        self.assertEqual(self.github.compares, [(REPO, "factory-main", "c1")])

    def test_a_missing_pr_base_blocks_the_work_item_and_opens_no_pr(self):
        """factory-main does not exist on the remote: the WorkItem waits for a person and no PR is
        opened against any other branch."""
        del self.github.branches[(REPO, "factory-main")]
        wi = self.tick()
        self.assertEqual(wi["state"], "human_waiting", wi)
        self.assertIn("no factory-main branch", wi["blockedOn"])
        self.assertIn("prBase", wi["blockedOn"])
        self.assertEqual(self.github.created_pulls, [])
        self.assertEqual(self.relay.tasks, {})


class PrBaseConfigTest(unittest.TestCase):
    ENV = {"FACTORY_MCP_TOKEN": "t"}

    def test_from_env_parses_owner_repo_branch_pairs(self):
        config = Config.from_env({**self.ENV, "FACTORY_PR_BASES": "a/B=F1, o/r=factory-main"})
        self.assertEqual(config.pr_base("A/b"), "F1")  # GitHub repo names ignore case
        self.assertEqual(config.pr_base("O/R"), "factory-main")
        self.assertIsNone(config.pr_base("o/other"))  # no setting: the default branch, as before

    def test_a_malformed_pr_base_list_is_refused(self):
        for value in ("a/b", "a/b=", "=main", "no-slash=main", "a/b=main,a/b=other"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "FACTORY_PR_BASES"):
                    Config.from_env({**self.ENV, "FACTORY_PR_BASES": value})
