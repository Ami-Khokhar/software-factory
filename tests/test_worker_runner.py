"""worker/runner.py: the trusted half of a worker. Real git, a scripted harness, no Docker."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from factory_mcp import documents
from factory_mcp.config import Config
from tests.fakes import workitem

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
import runner  # noqa: E402

HARNESS = r'''
import os, stat, subprocess, sys
mode = sys.argv[1]
def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True)
if mode in ("fix", "nocommit", "plant"):
    open("app.txt", "w").write("fixed\n")
if mode == "cheat":
    open(".github/pr-gatekeeper.json", "w").write('{"verify": "true"}')
    open("app.txt", "w").write("still broken\n")
if mode in ("fix", "cheat"):
    git("add", "-A")
    git("-c", "user.name=h", "-c", "user.email=h@h", "commit", "-qm", "harness work")
if mode == "plant":  # a filter driver and hooks, which run wherever git touches this tree
    trap = '[ -n "$FACTORY_TEST_SANDBOX" ] || touch "$FACTORY_TEST_MARKER"'
    open(".gitattributes", "w").write("* filter=plant\n")
    git("config", "filter.plant.clean", trap + "; cat")
    git("config", "filter.plant.smudge", trap + "; cat")
    for hook in ("pre-commit", "post-commit", "post-checkout", "pre-push"):
        path = os.path.join(".git", "hooks", hook)
        open(path, "w").write("#!/bin/sh\n" + trap + "\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
print("DONE")
'''
# What the image's system git config and the container boundary give every sandboxed command.
SANDBOX_ENV = {"FACTORY_TEST_SANDBOX": "1", "GIT_AUTHOR_NAME": "factory-worker",
               "GIT_AUTHOR_EMAIL": "w@w", "GIT_COMMITTER_NAME": "factory-worker", "GIT_COMMITTER_EMAIL": "w@w"}


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        seed, self.checkout = root / "seed", root / "checkout"
        sh(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        sh(root, "init", "-q", "-b", "main", str(seed))
        (seed / ".github").mkdir()
        (seed / ".github" / "pr-gatekeeper.json").write_text('{"verify": "python3 check.py"}')
        (seed / "check.py").write_text(
            "import sys\nsys.exit(0 if open('app.txt').read().strip() == 'fixed' else 1)\n")
        (seed / "app.txt").write_text("broken\n")
        sh(seed, "add", "-A")
        sh(seed, "-c", "user.name=s", "-c", "user.email=s@s", "commit", "-qm", "seed")
        sh(seed, "push", "-q", str(self.origin), "main")
        self.base = sh(seed, "rev-parse", "HEAD")
        sh(root, "clone", "-q", str(self.origin), str(self.checkout))
        sh(self.checkout, "config", "remote.origin.url", "https://github.com/o/r.git")
        sh(self.checkout, "config", f"url.{self.origin}.insteadOf", "https://github.com/o/r.git")
        (root / "harness.py").write_text(HARNESS)
        self.harness = root / "harness.py"
        self.marker = root / "ran-on-the-host"
        auth = root / "auth.json"
        auth.write_text("{}")
        self.mounts = []

        def local_run(sandbox, argv, seconds, mount, with_agent_dir=False, env=None):
            """Sandbox.run without Docker: the same argv in the mounted directory, marked sandboxed."""
            self.mounts.append(mount)
            done = subprocess.run(argv, cwd=mount, capture_output=True, text=True, timeout=60,
                                  env=dict(os.environ, **SANDBOX_ENV, **(env or {})))
            return done.returncode, done.stdout, done.stderr

        patches = [mock.patch.object(runner, "PI_AUTH", str(auth)),
                   mock.patch.object(runner.Sandbox, "run", local_run),
                   mock.patch.object(runner, "ensure_docker", lambda: None),
                   mock.patch.dict(os.environ, {"FACTORY_TEST_MARKER": str(self.marker)})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_order(self, mode, kind="implement", repo="o/r", head=None):
        wi = workitem(id="o/r#7", repo=repo, issueNumber=7, goal="Fix the app", headSha=head)
        order = documents.workorder(wi, kind, 0, Config(mcp_token="t", repos=frozenset()), self.base)
        text = documents.worker_input(order, "factory/7")
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.checkout)
        try:
            with redirect_stdout(out):
                runner.main(["--", sys.executable, str(self.harness), mode, text])
        finally:
            os.chdir(cwd)
        return json.loads(out.getvalue().strip().splitlines()[-1]), order

    def apply_on(self, commit, diff):
        fresh = Path(self.tmp.name) / f"fresh-{commit[:7]}"
        sh(self.tmp.name, "clone", "-q", str(self.origin), str(fresh))
        sh(fresh, "checkout", "-q", commit)
        subprocess.run(["git", "apply", "--index", "-"], cwd=fresh, input=diff, text=True, check=True)
        return fresh

    def test_completed_patch_applies_on_the_base(self):
        result, order = self.run_order("fix")
        self.assertEqual((result["status"], result["workOrderId"]), ("completed", order["id"]), result.get("error"))
        self.assertEqual((result["patch"]["format"], result["patch"]["baseSha"]), ("unified", self.base))
        self.assertEqual(result["evidence"][0]["command"], "python3 check.py")
        self.assertEqual(result["evidence"][0]["exit"], 0)
        self.assertEqual(documents.check_result(result, order), [])
        self.assertEqual((self.apply_on(self.base, result["patch"]["diff"]) / "app.txt").read_text(), "fixed\n")

    def test_uncommitted_work_is_collected(self):
        result, _ = self.run_order("nocommit")
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertIn("+fixed", result["patch"]["diff"])

    def test_no_change_fails(self):
        result, order = self.run_order("nothing")
        self.assertEqual((result["status"], result["error"]), ("failed", "the harness made no change"))
        self.assertEqual(documents.check_result(result, order), [])

    def test_a_checkout_of_another_repo_is_refused(self):
        result, _ = self.run_order("fix", repo="o/other")
        self.assertEqual(result["status"], "failed")
        self.assertIn("is o/r, not o/other", result["error"])

    def test_verify_comes_from_the_base_commit(self):
        result, _ = self.run_order("cheat")  # rewrote verify to `true` and left the app broken
        self.assertEqual(result["status"], "failed")
        self.assertEqual((result["evidence"][0]["command"], result["evidence"][0]["exit"]), ("python3 check.py", 1))

    def test_fix_builds_on_the_pr_head(self):
        branch = Path(self.tmp.name) / "branch"
        sh(self.tmp.name, "clone", "-q", str(self.origin), str(branch))
        (branch / "notes.txt").write_text("round 1\n")
        sh(branch, "checkout", "-q", "-b", "factory/7")
        sh(branch, "add", "-A")
        sh(branch, "-c", "user.name=w", "-c", "user.email=w@w", "commit", "-qm", "round 1")
        sh(branch, "push", "-q", "origin", "factory/7")
        head = sh(branch, "rev-parse", "HEAD")
        result, _ = self.run_order("fix", kind="fix", head=head)
        self.assertEqual((result["status"], result["patch"]["baseSha"]), ("completed", head), result.get("error"))
        self.assertNotIn("notes.txt", result["patch"]["diff"])
        fresh = self.apply_on(head, result["patch"]["diff"])
        self.assertEqual(((fresh / "notes.txt").exists(), (fresh / "app.txt").read_text()), (True, "fixed\n"))

    def test_the_harness_gets_a_copy_and_never_the_checkout(self):
        before = (sh(self.checkout, "rev-parse", "HEAD"), (self.checkout / ".git" / "config").read_text())
        result, _ = self.run_order("plant")
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertTrue(self.mounts)
        for mount in self.mounts:
            self.assertFalse(Path(mount).resolve().is_relative_to(self.checkout.resolve()), mount)
            self.assertFalse(Path(mount).exists(), "the copy is removed after the pass")
        self.assertEqual((sh(self.checkout, "rev-parse", "HEAD"), (self.checkout / ".git" / "config").read_text()),
                         before)
        self.assertEqual(sh(self.checkout, "status", "--porcelain"), "")

    def test_hooks_and_filters_the_harness_plants_never_run_on_the_host(self):
        result, _ = self.run_order("plant")
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertIn("+fixed", result["patch"]["diff"])
        self.assertFalse(self.marker.exists(), "host git ran in a tree the harness wrote")


class EnsureDockerTest(unittest.TestCase):
    """After a reboot Docker is down; the pass starts OrbStack instead of failing for a person."""

    def stubs(self, orb_starts):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bin_, up = Path(tmp.name), Path(tmp.name) / "up"
        (bin_ / "docker").write_text(f'#!/bin/sh\n[ -e "{up}" ]\n')
        (bin_ / "orb").write_text(f'#!/bin/sh\n{"touch " + repr(str(up)) if orb_starts else "true"}\n')
        (bin_ / "open").write_text("#!/bin/sh\ntrue\n")
        for name in ("docker", "orb", "open"):
            os.chmod(bin_ / name, 0o755)
        return mock.patch.dict(os.environ, {"PATH": f"{bin_}:{os.environ['PATH']}"}), up

    def test_a_stopped_docker_is_started(self):
        env, up = self.stubs(orb_starts=True)
        with env:
            runner.ensure_docker(wait=5, poll=0.05)
        self.assertTrue(up.exists())

    def test_docker_that_does_not_start_fails_the_pass(self):
        env, _ = self.stubs(orb_starts=False)
        with env, self.assertRaisesRegex(runner.WorkerError, "docker is not available"):
            runner.ensure_docker(wait=0.3, poll=0.05)


if __name__ == "__main__":
    unittest.main()
