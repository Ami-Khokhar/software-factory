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

    def test_a_control_or_credential_path_is_protected_at_any_depth(self):
        """Guard: nested credential names are refused on the push path, plain source is not."""
        protected = Config(mcp_token="t", repos=frozenset()).protected_paths
        for name in ("worker/.env", "backend/.env.local", "worker/secrets/key",
                     ".github/workflows/ci.yml"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(patches.PatchError, "changes protected path"):
                    patches.apply(self.bare, None, "docs", self.base, self.patch(name),
                                  "Add it", False, protected=protected)
                git("reset", "-q", cwd=self.seed)  # each refusal patches its name alone
        sha = patches.apply(self.bare, None, "docs", self.base, self.patch("worker/main.py"),
                            "Add source", False, protected=protected)
        self.assertEqual(git("show", f"{sha}:worker/main.py", cwd=self.bare).strip(),
                         "Document the service.")

    def test_custom_protection_refuses_patch_before_push(self):
        """Guard: explicitly protected instructions remain protected."""
        with self.assertRaisesRegex(patches.PatchError, "changes protected path AGENTS.md"):
            patches.apply(self.bare, None, "docs", self.base, self.patch("AGENTS.md"),
                          "Document service", False, protected=(".github/", "AGENTS.md"))
        result = subprocess.run(["git", "show-ref", "--verify", "refs/heads/docs"],
                                cwd=self.bare, env=ENV, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
