"""The protected-path bootstrap writer uses a new branch and a local Git repository."""
import os
import subprocess
import tempfile
import unittest

from factory_mcp import patches


ENV = dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.com",
           GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.com")


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True,
                          text=True).stdout.strip()


class BootstrapGitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bare = os.path.join(self.tmp.name, "origin.git")
        self.seed = os.path.join(self.tmp.name, "seed")
        git("init", "-q", "--bare", "-b", "main", self.bare, cwd=self.tmp.name)
        git("clone", "-q", self.bare, self.seed, cwd=self.tmp.name)
        self.write("README.md", "base\n")
        git("add", "README.md", cwd=self.seed)
        git("commit", "-q", "-m", "initial", cwd=self.seed)
        git("push", "-q", "origin", "HEAD:main", cwd=self.seed)
        self.base = git("rev-parse", "HEAD", cwd=self.seed)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        path = os.path.join(self.seed, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(text)

    def head(self, branch):
        return git("rev-parse", f"refs/heads/{branch}", cwd=self.bare)

    def test_bootstrap_commits_only_the_supplied_paths_on_a_new_branch(self):
        files = {
            ".github/pr-gatekeeper.json": '{"verify": "python3 -m unittest"}\n',
            "AGENTS.md": "<!-- factory-standard:begin -->\n<!-- factory-standard:end -->\n",
        }

        sha = patches.bootstrap(self.bare, None, "factory/setup", self.base, files, "Set up factory")

        self.assertEqual(sha, self.head("factory/setup"))
        self.assertEqual(git("rev-parse", "factory/setup^", cwd=self.bare), self.base)
        self.assertEqual(set(git("diff", "--name-only", self.base, sha, cwd=self.bare).splitlines()),
                         set(files))
        self.assertEqual(git("show", f"{sha}:README.md", cwd=self.bare), "base")

    def test_existing_setup_branch_is_never_overwritten(self):
        first = patches.bootstrap(self.bare, None, "factory/setup", self.base,
                                  {"AGENTS.md": "first\n"}, "Set up factory")

        with self.assertRaisesRegex(patches.PatchError, "already exists"):
            patches.bootstrap(self.bare, None, "factory/setup", self.base,
                              {"AGENTS.md": "second\n"}, "Set up factory")

        self.assertEqual(self.head("factory/setup"), first)

    def test_a_missing_base_does_not_create_the_setup_branch(self):
        with self.assertRaises(patches.PatchError):
            patches.bootstrap(self.bare, None, "factory/setup", "0" * 40,
                              {"AGENTS.md": "standard\n"}, "Set up factory")
        with self.assertRaises(subprocess.CalledProcessError):
            self.head("factory/setup")

    def test_unsafe_paths_are_rejected_before_a_branch_is_pushed(self):
        for name in ("../AGENTS.md", "/tmp/AGENTS.md", "a/../../AGENTS.md", ".git/config"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(patches.PatchError, "unsafe bootstrap path"):
                    patches.bootstrap(self.bare, None, "factory/setup", self.base,
                                      {name: "standard\n"}, "Set up factory")
                with self.assertRaises(subprocess.CalledProcessError):
                    self.head("factory/setup")

    def test_a_symlink_in_the_base_cannot_redirect_a_bootstrap_write(self):
        outside = os.path.join(self.tmp.name, "outside")
        os.mkdir(outside)
        os.symlink(outside, os.path.join(self.seed, "linked"))
        git("add", "linked", cwd=self.seed)
        git("commit", "-q", "-m", "symlink", cwd=self.seed)
        git("push", "-q", "origin", "HEAD:main", cwd=self.seed)
        base = git("rev-parse", "HEAD", cwd=self.seed)

        with self.assertRaisesRegex(patches.PatchError, "symlink"):
            patches.bootstrap(self.bare, None, "factory/setup", base,
                              {"linked/AGENTS.md": "standard\n"}, "Set up factory")

        self.assertFalse(os.path.exists(os.path.join(outside, "AGENTS.md")))


if __name__ == "__main__":
    unittest.main()
