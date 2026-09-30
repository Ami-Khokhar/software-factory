"""The protected-path bootstrap writer uses a new branch and a local Git repository."""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

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

    def test_an_update_adds_one_commit_with_the_changed_files_on_top_of_the_setup_branch(self):
        first = patches.bootstrap(self.bare, None, "factory/setup", self.base,
                                  {"AGENTS.md": "first\n", "gate.json": "{}\n"}, "Set up factory")

        sha = patches.update_bootstrap(self.bare, None, "factory/setup", first,
                                       {"AGENTS.md": "second\n", "gate.json": "{}\n"}, "Update")

        self.assertEqual(sha, self.head("factory/setup"))
        self.assertEqual(git("rev-parse", "factory/setup^", cwd=self.bare), first)
        self.assertEqual(git("diff", "--name-only", first, sha, cwd=self.bare), "AGENTS.md")
        self.assertEqual(git("show", f"{sha}:AGENTS.md", cwd=self.bare), "second")

    def test_an_update_to_a_branch_that_holds_the_files_pushes_nothing(self):
        first = patches.bootstrap(self.bare, None, "factory/setup", self.base,
                                  {"AGENTS.md": "first\n"}, "Set up factory")

        self.assertIsNone(patches.update_bootstrap(self.bare, None, "factory/setup", first,
                                                   {"AGENTS.md": "first\n"}, "Update"))
        self.assertEqual(self.head("factory/setup"), first)

    def test_an_update_to_a_branch_that_moved_is_refused_and_not_forced(self):
        first = patches.bootstrap(self.bare, None, "factory/setup", self.base,
                                  {"AGENTS.md": "first\n"}, "Set up factory")
        git("fetch", "-q", "origin", cwd=self.seed)
        git("checkout", "-q", "-b", "other", "origin/factory/setup", cwd=self.seed)
        self.write("notes.md", "owner's change\n")
        git("add", "notes.md", cwd=self.seed)
        git("commit", "-q", "-m", "owner", cwd=self.seed)
        git("push", "-q", "origin", "HEAD:factory/setup", cwd=self.seed)
        moved = self.head("factory/setup")

        with self.assertRaisesRegex(patches.PatchError, "cannot fast-forward"):
            patches.update_bootstrap(self.bare, None, "factory/setup", first, {"AGENTS.md": "second\n"}, "Update")

        self.assertEqual(self.head("factory/setup"), moved)

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


class InitialCommitTest(unittest.TestCase):
    """An empty repo gets one README commit on its default branch, and nothing else."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bare = os.path.join(self.tmp.name, "origin.git")
        git("init", "-q", "--bare", "-b", "main", self.bare, cwd=self.tmp.name)

    def refs(self):
        return git("for-each-ref", "--format=%(refname)", cwd=self.bare).splitlines()

    def push_other_commit(self, branch):
        seed = os.path.join(self.tmp.name, "seed")
        git("init", "-q", seed, cwd=self.tmp.name)
        git("commit", "-q", "--allow-empty", "-m", "someone else", cwd=seed)
        git("push", "-q", self.bare, f"HEAD:refs/heads/{branch}", cwd=seed)
        return git("rev-parse", "HEAD", cwd=seed)

    def test_an_empty_repo_gets_one_readme_commit_that_a_setup_branch_can_start_from(self):
        sha = patches.initial_commit(self.bare, None, "main", {"README.md": "# r\n"}, "Start the repository")

        self.assertEqual(self.refs(), ["refs/heads/main"])
        self.assertEqual(git("rev-parse", "refs/heads/main", cwd=self.bare), sha)
        self.assertEqual(git("rev-list", "--count", sha, cwd=self.bare), "1")
        self.assertEqual(git("ls-tree", "-r", "--name-only", sha, cwd=self.bare), "README.md")
        self.assertEqual(git("show", f"{sha}:README.md", cwd=self.bare), "# r")
        setup = patches.bootstrap(self.bare, None, "factory/setup", sha, {"AGENTS.md": "standard\n"},
                                  "Set up factory")
        self.assertEqual(git("rev-parse", "factory/setup^", cwd=self.bare), sha)
        self.assertEqual(git("rev-parse", "refs/heads/main", cwd=self.bare), sha)
        self.assertEqual(git("diff", "--name-only", sha, setup, cwd=self.bare), "AGENTS.md")

    def test_a_repo_with_any_ref_gets_no_initial_commit(self):
        other = self.push_other_commit("other")

        with self.assertRaisesRegex(patches.PatchError, "not empty"):
            patches.initial_commit(self.bare, None, "main", {"README.md": "# r\n"}, "Start the repository")

        self.assertEqual(self.refs(), ["refs/heads/other"])
        self.assertEqual(git("rev-parse", "refs/heads/other", cwd=self.bare), other)

    def test_a_commit_that_lands_after_the_check_stops_the_push(self):
        real_run, landed = subprocess.run, []

        def run(args, *rest, **kwargs):  # someone pushes to main just after ls-remote saw no refs
            done = real_run(args, *rest, **kwargs)
            if args[:2] == ["git", "ls-remote"] and not landed:
                landed.append(True)
                with mock.patch("subprocess.run", real_run):
                    landed.append(self.push_other_commit("main"))
            return done

        with mock.patch("factory_mcp.patches.subprocess.run", side_effect=run):
            with self.assertRaisesRegex(patches.PatchError, "may have got a commit"):
                patches.initial_commit(self.bare, None, "main", {"README.md": "# r\n"}, "Start the repository")

        self.assertEqual(self.refs(), ["refs/heads/main"])
        self.assertEqual(git("rev-parse", "refs/heads/main", cwd=self.bare), landed[1])


if __name__ == "__main__":
    unittest.main()
