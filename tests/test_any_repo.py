"""Any repo (ADR-0027): an issue a trusted author marks factory:ready enables its repo when the repo
passes the check, and the author hears why when it does not."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.repos import load
from factory_mcp.store import Store
from tests.fakes import ISSUE_BODY, FakeGitHub, FakeRelay
from tests.test_repos import gated

OWNER = "amteshwar"
NEW = "o/new"  # passes the check, not listed
BARE = "o/bare"  # no gate files, not listed


class AnyRepoTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        subprocess.run(["git", "init", "-q", "--bare", str(root / "remote.git")], check=True)
        self.manifest = root / "config" / "repos.json"
        self.github = gated(FakeGitHub(), repo=NEW)
        # FakeGitHub opens the factory's own issues as the repo owner, "o", as the token's user would.
        self.foreman = Foreman(Store(":memory:"), self.github, Config(
            mcp_token="t", repos=frozenset(), repos_file=str(self.manifest), trusted_authors=frozenset({OWNER, "o"}),
            checkout_root=str(root / "work"), git_url=str(root / "remote.git")), FakeRelay())

    def comments_on(self, repo, number):
        return [body for r, n, body in self.github.posted if (r, n) == (repo, number)]

    def test_an_issue_in_an_unlisted_repo_that_passes_the_check_is_taken_in(self):
        self.github.add_issue(NEW, 3, author=OWNER)
        self.assertEqual(self.foreman.rebuild(), [f"{NEW}#3"])
        self.assertEqual(self.foreman.store.get(f"{NEW}#3")["state"], "ready")
        self.assertIn(NEW, self.foreman.config.repos)
        self.assertEqual([(e["name"], e["added_by"]) for e in load(str(self.manifest))],
                         [(NEW, f"{OWNER}, who marked {NEW}#3 factory:ready")])
        self.assertEqual(self.github.labels[NEW], {"factory:ready", "factory:proposed"})

    def test_a_repo_that_fails_the_check_gets_one_comment_and_no_workitem(self):
        self.github.add_issue(BARE, 3, author=OWNER)
        self.assertEqual((self.foreman.rebuild(), self.foreman.rebuild()), ([], []))
        comments = self.comments_on(BARE, 3)
        self.assertEqual(len(comments), 1, comments)
        self.assertIn(f"{BARE} is not ready: {BARE} has no .github/pr-gatekeeper.json", comments[0])
        self.assertIn("factory_bootstrap_repo", comments[0])  # the fix, not just the failure
        self.assertEqual((self.foreman.store.get(f"{BARE}#3"), self.manifest.exists()), (None, False))

    def test_an_issue_an_untrusted_author_opened_is_never_taken_in(self):
        self.github.add_issue(NEW, 3, author="stranger")
        self.assertEqual(self.foreman.rebuild(), [])
        self.assertEqual((self.github.posted, self.manifest.exists()), ([], False))
        self.assertNotIn(NEW, self.foreman.config.repos)

    def test_approving_an_issue_in_an_unlisted_repo_that_passes_the_check_enables_it(self):
        self.github.add_issue(NEW, 4, labels=("factory:proposed",), author=OWNER)
        self.assertEqual(self.foreman.approve_issue(NEW, 4, OWNER)["state"], "ready")
        self.assertEqual([(e["name"], e["added_by"]) for e in load(str(self.manifest))], [(NEW, OWNER)])

    def test_proposing_an_issue_in_an_unlisted_repo_that_passes_the_check_enables_it(self):
        self.foreman.propose_issue(NEW, "Greet by name", ISSUE_BODY)
        self.assertEqual([(e["name"], e["added_by"]) for e in load(str(self.manifest))],
                         [(NEW, "factory_propose_issue")])

    def test_a_plan_in_an_unlisted_repo_that_passes_the_check_enables_it(self):
        self.foreman.start_plan(NEW, [{"title": "One", "body": ISSUE_BODY}], OWNER)
        self.assertEqual([(e["name"], e["added_by"]) for e in load(str(self.manifest))], [(NEW, OWNER)])

    def test_the_tools_refuse_an_unlisted_repo_that_fails_the_check_and_write_nothing(self):
        self.github.add_issue(BARE, 4, labels=("factory:proposed",), author=OWNER)
        calls = {"approve": lambda: self.foreman.approve_issue(BARE, 4, OWNER),
                 "propose": lambda: self.foreman.propose_issue(BARE, "Greet by name", ISSUE_BODY),
                 "plan": lambda: self.foreman.start_plan(BARE, [{"title": "One", "body": ISSUE_BODY}], OWNER)}
        for name, call in calls.items():
            with self.subTest(name), self.assertRaisesRegex(ToolError, f"{BARE} is not ready: {BARE} has no "):
                call()
        self.assertEqual((self.github.labelled, len(self.github.issues), self.manifest.exists()), ([], 1, False))


if __name__ == "__main__":
    unittest.main()
