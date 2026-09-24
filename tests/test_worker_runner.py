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
import subprocess, sys
mode = sys.argv[1]
def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True)
if mode in ("fix", "nocommit"):
    open("app.txt", "w").write("fixed\n")
if mode == "cheat":
    open(".github/pr-gatekeeper.json", "w").write('{"verify": "true"}')
    open("app.txt", "w").write("still broken\n")
if mode in ("fix", "cheat"):
    git("add", "-A")
    git("-c", "user.name=h", "-c", "user.email=h@h", "commit", "-qm", "harness work")
print("DONE")
'''


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def local_run(self, argv, seconds, with_agent_dir):
    """Sandbox.run without Docker: the same argv in the checkout."""
    done = subprocess.run(argv, cwd=self.cwd, capture_output=True, text=True, timeout=60)
    return done.returncode, done.stdout + done.stderr


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        seed, self.work = root / "seed", root / "work"
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
        sh(root, "clone", "-q", str(self.origin), str(self.work))
        sh(self.work, "config", "remote.origin.url", "https://github.com/o/r.git")
        sh(self.work, "config", f"url.{self.origin}.insteadOf", "https://github.com/o/r.git")
        (root / "harness.py").write_text(HARNESS)
        self.harness = root / "harness.py"
        auth = root / "auth.json"
        auth.write_text("{}")
        patches = [mock.patch.object(runner, "PI_AUTH", str(auth)),
                   mock.patch.object(runner.Sandbox, "run", local_run)]
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
        os.chdir(self.work)
        try:
            with redirect_stdout(out):
                runner.main(["--", sys.executable, str(self.harness), mode, text])
        finally:
            os.chdir(cwd)
        return json.loads(out.getvalue().strip().splitlines()[-1]), order

    def test_completed_patch_applies_on_the_base(self):
        result, order = self.run_order("fix")
        self.assertEqual((result["status"], result["workOrderId"]), ("completed", order["id"]), result.get("error"))
        self.assertEqual((result["patch"]["format"], result["patch"]["baseSha"]), ("unified", self.base))
        self.assertEqual(result["evidence"][0]["command"], "python3 check.py")
        self.assertEqual(result["evidence"][0]["exit"], 0)
        self.assertEqual(documents.check_result(result, order), [])
        fresh = Path(self.tmp.name) / "fresh"
        sh(self.tmp.name, "clone", "-q", str(self.origin), str(fresh))
        subprocess.run(["git", "apply", "--index", "-"], cwd=fresh, input=result["patch"]["diff"],
                       text=True, check=True)
        self.assertEqual((fresh / "app.txt").read_text(), "fixed\n")

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
        self.assertTrue((self.work / "notes.txt").exists())


if __name__ == "__main__":
    unittest.main()
