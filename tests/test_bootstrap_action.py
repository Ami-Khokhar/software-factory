"""The owner-approved repo bootstrap prepares a setup PR, never merges controls."""
import unittest
from unittest import mock

from factory_mcp import patches
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.repos import GATE_CONFIG, GATE_WORKFLOW, TEMPLATE
from factory_mcp.store import Store
from tests.fakes import FakeGitHub, FakeRelay


class BootstrapActionTest(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset(), git_url="https://github.com/{repo}.git"),
                               FakeRelay())
        self.pushed = []

        def push(url, token, branch, base_sha, files, message):
            self.pushed.append((url, branch, base_sha, files, message))
            self.github.push("o/r", branch, "setup-sha", files=[(name, 1, 0) for name in files])
            return "setup-sha"

        patch = mock.patch("factory_mcp.foreman.patches.bootstrap", side_effect=push)
        patch.start()
        self.addCleanup(patch.stop)
        self.initial = []

        def start(url, token, branch, files, message):
            self.initial.append((url, branch, files))
            self.github.branches[("o/r", branch)] = "readme-sha"
            return "readme-sha"

        patch = mock.patch("factory_mcp.foreman.patches.initial_commit", side_effect=start)
        patch.start()
        self.addCleanup(patch.stop)
        self.log = mock.Mock(wraps=self.foreman.store.log)  # records every audit entry it writes
        self.foreman.store.log = self.log

    def empty_repo(self):
        self.github.branches[("o/r", "main")] = None  # GitHub: an empty repo's default branch has no head

    def test_one_approval_prepares_one_manual_setup_pr_and_retry_adopts_it(self):
        first = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")
        second = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        self.assertEqual(first, second)
        self.assertEqual(first["repo"], "o/r")
        self.assertEqual(first["prNumber"], 100)
        self.assertTrue(first["needsHumanMerge"])
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(set(self.pushed[0][3]), {GATE_CONFIG, GATE_WORKFLOW, "AGENTS.md"})
        self.assertEqual(self.github.branch_head("o/r", "main"), "base0")
        body = self.github.pulls[0]["body"]
        for heading in ("## Task", "## Intent", "## Why it was needed", "## Why this approach",
                        "## What changed", "## Proof of work"):
            self.assertIn(heading, body)
        self.assertIn("owner must review and merge", body.lower())
        for path in (GATE_CONFIG, GATE_WORKFLOW, "AGENTS.md"):
            self.assertIn(f"- `{path}`", body)
        self.assertEqual(self.initial, [])
        self.assertIsNone(first.get("initialCommit"))

    def test_an_empty_repo_gets_a_readme_commit_then_the_controls_on_a_setup_pr(self):
        self.empty_repo()

        first = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")
        second = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        self.assertEqual(self.initial, [("https://github.com/o/r.git", "main", {"README.md": "# r\n"})])
        self.assertEqual(first["initialCommit"], "readme-sha")
        self.assertTrue(first["needsHumanMerge"])
        self.assertEqual(len(self.pushed), 1)
        _, branch, base_sha, files, _ = self.pushed[0]
        self.assertEqual((base_sha, set(files)), ("readme-sha", {GATE_CONFIG, GATE_WORKFLOW, "AGENTS.md"}))
        self.assertEqual(self.github.branch_head("o/r", "main"), "readme-sha")
        self.assertEqual(self.github.pulls[0]["head"]["ref"], branch)
        self.assertEqual(second["prNumber"], first["prNumber"])  # a retry adopts the PR
        audit = {call.args[0]: call.args[1] for call in self.log.call_args_list}
        self.assertEqual(audit["bootstrap_pr"]["initialCommit"], "readme-sha")
        self.assertEqual(audit["bootstrap_pr"]["baseSha"], "readme-sha")
        self.assertEqual(audit["bootstrap_initial_commit"]["sha"], "readme-sha")

    def test_a_refused_initial_commit_pushes_no_setup_branch(self):
        self.empty_repo()
        with mock.patch("factory_mcp.foreman.patches.initial_commit",
                        side_effect=patches.PatchError("the repo may have got a commit since the check")):
            with self.assertRaisesRegex(ToolError, "may have got a commit"):
                self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")
        self.assertEqual((self.pushed, self.github.pulls), ([], []))

    def test_existing_controls_need_no_setup_pr(self):
        self.github.contents.update({("o/r", GATE_CONFIG): '{"verify":"make test"}',
                                     ("o/r", GATE_WORKFLOW): "workflow", ("o/r", "AGENTS.md"): TEMPLATE.read_text()})
        result = self.foreman.bootstrap_repo("o/r", None, "owner")
        self.assertEqual(result["repo"], "o/r")
        self.assertFalse(result["needsHumanMerge"])
        self.assertEqual((self.pushed, self.github.pulls), ([], []))

    def test_default_branch_changed_before_push_refuses_without_writing(self):
        original = self.github.branch_head
        calls = [0]

        def moving_head(repo, branch):
            calls[0] += 1
            return "new-base" if calls[0] > 1 and branch == "main" else original(repo, branch)

        with mock.patch.object(self.github, "branch_head", side_effect=moving_head):
            with self.assertRaisesRegex(ToolError, "changed"):
                self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")
        self.assertEqual((self.pushed, self.github.pulls), ([], []))


if __name__ == "__main__":
    unittest.main()
