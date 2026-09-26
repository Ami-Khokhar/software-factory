"""scripts/factory: `update` deploys a branch only when its tests pass, and follows it to main once
merged; `add-repo` asks factory-mcp, as Day Desk does."""
import importlib.machinery
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

LOADER = importlib.machinery.SourceFileLoader(
    "factory_cli", str(Path(__file__).resolve().parent.parent / "scripts" / "factory"))
cli = importlib.util.module_from_spec(importlib.util.spec_from_loader("factory_cli", LOADER))
LOADER.exec_module(cli)
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@t")
PASS = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test(self):\n        self.assertTrue(True)\n"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True,
                          text=True).stdout.strip()


class UpdateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.origin, self.dev, self.deploy = root / "origin.git", root / "dev", root / "deploy"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(root, "clone", "-q", str(self.origin), str(self.dev))
        self.commit("tests/test_ok.py", PASS)
        git(self.dev, "push", "-q", "origin", "HEAD:main")
        git(root, "clone", "-q", str(self.origin), str(self.deploy))
        self.restarts = []
        for p in (mock.patch.object(cli, "HERE", self.deploy),
                  mock.patch.object(cli, "restart_factory_mcp", lambda: self.restarts.append(1)),
                  mock.patch.object(cli, "branch_merged", lambda branch: False)):
            p.start()
            self.addCleanup(p.stop)

    def commit(self, name, text, cwd=None, message=None):
        cwd = cwd or self.dev
        (cwd / name).parent.mkdir(parents=True, exist_ok=True)
        (cwd / name).write_text(text)
        git(cwd, "add", "-A")
        git(cwd, "commit", "-q", "-m", message or f"change {name}")
        return git(cwd, "rev-parse", "HEAD")

    def head(self):
        return git(self.deploy, "rev-parse", "HEAD"), git(self.deploy, "branch", "--show-current")

    def test_a_passing_commit_is_deployed_and_factory_mcp_restarted(self):
        new = self.commit("notes.txt", "x\n")
        git(self.dev, "push", "-q", "origin", "HEAD:main")
        cli.cmd_update([])
        self.assertEqual((self.head(), len(self.restarts)), ((new, "main"), 1))
        cli.cmd_update([])  # nothing new: no restart
        self.assertEqual(len(self.restarts), 1)

    def test_a_failing_commit_is_not_deployed(self):
        old = self.head()
        self.commit("tests/test_ok.py", PASS.replace("True)", "False)"))
        git(self.dev, "push", "-q", "origin", "HEAD:main")
        with self.assertRaisesRegex(SystemExit, "tests fail"):
            cli.cmd_update([])
        self.assertEqual((self.head(), self.restarts), (old, []))

    def test_a_merged_and_deleted_branch_is_followed_to_main(self):
        git(self.dev, "checkout", "-q", "-b", "feature")
        self.commit("feature.txt", "f\n")
        git(self.dev, "push", "-q", "origin", "feature")
        git(self.deploy, "fetch", "-q", "origin")
        git(self.deploy, "checkout", "-q", "-b", "feature", "origin/feature")
        git(self.dev, "checkout", "-q", "main")
        merged = self.commit("feature.txt", "f\n", message="Feature (#1)")  # a squash merge
        git(self.dev, "push", "-q", "origin", "HEAD:main", ":feature")
        cli.cmd_update([])
        self.assertEqual((self.head(), len(self.restarts)), ((merged, "main"), 1))

    def test_a_checkout_with_local_changes_is_left_alone(self):
        (self.deploy / "tests" / "test_ok.py").write_text(PASS + "# local edit\n")
        self.commit("notes.txt", "x\n")
        git(self.dev, "push", "-q", "origin", "HEAD:main")
        with self.assertRaisesRegex(SystemExit, "local changes"):
            cli.cmd_update([])
        self.assertIn("# local edit", (self.deploy / "tests" / "test_ok.py").read_text())



class AddRepoTest(unittest.TestCase):
    def add_repo(self, ready):
        """The tools add-repo called, and how it exited. It must run no command of its own."""
        calls = []

        def call(tool, arguments=None, timeout=60):
            calls.append((tool, timeout))
            return {"repo": "o/r", "ready": ready, "added": True,
                    "checks": [{"id": "gate_workflow", "ok": ready, "detail": "d", "fix": "f"}]}

        with mock.patch.object(cli, "call", call), mock.patch("builtins.print"), \
                mock.patch.object(cli, "run", side_effect=AssertionError("add-repo ran a command")):
            try:
                cli.cmd_add_repo(["o/r"])
            except SystemExit as stopped:
                return calls, str(stopped)
        return calls, None

    def test_a_ready_repo_is_enabled_through_the_tools(self):
        # enabling clones the worker checkout, which may take up to 900s
        self.assertEqual(self.add_repo(ready=True),
                         ([("factory_repo_check", 60), ("factory_add_repo", 960)], None))

    def test_a_failing_check_stops_it_before_the_repo_is_enabled(self):
        calls, stopped = self.add_repo(ready=False)
        self.assertEqual(calls, [("factory_repo_check", 60)])
        self.assertIn("o/r is not ready", stopped)

if __name__ == "__main__":
    unittest.main()
