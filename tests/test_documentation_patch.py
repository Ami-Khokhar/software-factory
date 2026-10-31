"""Exercise pre-push documentation and custom-path enforcement with real git."""
import subprocess
import unittest

from factory_mcp import patches
from factory_mcp.config import Config
from tests import test_bootstrap_git as fixtures
from tests.test_bootstrap_git import ENV, git


class DocumentationPatchTest(unittest.TestCase):
    setUp = fixtures.BootstrapGitTest.setUp
    tearDown = fixtures.BootstrapGitTest.tearDown
    write = fixtures.BootstrapGitTest.write

    def patch(self, name):
        self.write(name, "Document the service.\n")
        git("add", name, cwd=self.seed)
        return {"format": "diff", "diff": git("diff", "--cached", "--binary", cwd=self.seed) + "\n"}

    def test_authorised_agents_patch_reaches_remote(self):
        sha = patches.apply(self.bare, None, "docs", self.base, self.patch("AGENTS.md"),
                            "Document service", False,
                            protected=Config(mcp_token="t", repos=frozenset()).protected_paths)
        self.assertEqual(git("show", f"{sha}:AGENTS.md", cwd=self.bare), "Document the service.")

    def test_custom_protection_refuses_patch_before_push(self):
        """Guard: explicitly protected instructions remain protected."""
        with self.assertRaisesRegex(patches.PatchError, "changes protected path AGENTS.md"):
            patches.apply(self.bare, None, "docs", self.base, self.patch("AGENTS.md"),
                          "Document service", False, protected=(".github/", "AGENTS.md"))
        result = subprocess.run(["git", "show-ref", "--verify", "refs/heads/docs"],
                                cwd=self.bare, env=ENV, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
