"""Routing by locality with failover, and patch Results that factory-mcp pushes with real git."""
import os
import subprocess
import tempfile
import unittest

from factory_mcp import patches
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.routing import route
from factory_mcp.store import Store
from tests.fakes import ISSUE_BODY, FakeGitHub, FakeRelay, result_line

REPO, ID = "o/r", "o/r#7"
WORKERS = (("codex", "hosted"), ("pi-glm", "local"))
THREE = WORKERS + (("opencode-glm", "local"),)
ENV = dict(os.environ, GIT_AUTHOR_NAME="w", GIT_AUTHOR_EMAIL="w@x", GIT_COMMITTER_NAME="w",
           GIT_COMMITTER_EMAIL="w@x")


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True,
                          text=True).stdout.strip()


class GitBackedGitHub(FakeGitHub):
    """FakeGitHub whose branches and diffs come from a real bare repository."""

    def __init__(self, bare):
        super().__init__()
        self.bare = bare

    def branch_head(self, repo, branch):
        try:
            return git("rev-parse", f"refs/heads/{branch}", cwd=self.bare)
        except subprocess.CalledProcessError:
            return None

    def changed_files(self, repo, base, head):
        rows = git("diff", "--numstat", f"{base}...{head}", cwd=self.bare).splitlines()
        return [{"filename": f, "status": "modified", "additions": int(a), "deletions": int(d)}
                for a, d, f in (row.split("\t") for row in rows)]

    def create_pull(self, repo, head, base, title, body):
        self.branches[(repo, head)] = self.branch_head(repo, head)
        return super().create_pull(repo, head, base, title, body)


class RouteTest(unittest.TestCase):
    def test_route(self):
        self.assertEqual(route(WORKERS, False, set()), "codex")
        self.assertEqual(route(WORKERS, True, set()), "pi-glm")
        self.assertEqual(route(WORKERS, False, {"codex"}), "pi-glm")
        self.assertEqual(route(WORKERS, False, {"codex", "pi-glm"}), "codex")
        self.assertIsNone(route((("codex", "hosted"),), True, set()))

    def test_an_issue_that_names_workers_routes_only_to_them_in_its_order(self):
        self.assertEqual(route(THREE, False, set(), ("opencode-glm", "pi-glm")), "opencode-glm")
        self.assertEqual(route(THREE, False, {"opencode-glm"}, ("opencode-glm", "pi-glm")), "pi-glm")
        self.assertEqual(route(THREE, False, {"opencode-glm", "pi-glm"}, ("opencode-glm", "pi-glm")),
                         "opencode-glm")  # both failed: the first named again, never codex
        self.assertIsNone(route(THREE, True, set(), ("codex",)))  # a local-only repo, a hosted choice


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bare = os.path.join(self.tmp.name, "origin.git")
        git("init", "-q", "--bare", "-b", "main", self.bare, cwd=self.tmp.name)
        self.work = os.path.join(self.tmp.name, "worker")
        git("clone", "-q", self.bare, self.work, cwd=self.tmp.name)
        self.write("app.py", "print('old')\n")
        git("add", ".", cwd=self.work)
        git("commit", "-q", "-m", "init", cwd=self.work)
        git("push", "-q", "origin", "HEAD:main", cwd=self.work)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        with open(os.path.join(self.work, name), "w") as handle:
            handle.write(text)

    def foreman(self, body=ISSUE_BODY, **config):
        self.github, self.relay = GitBackedGitHub(self.bare), FakeRelay()
        self.github.add_issue(REPO, 7, body=body)
        config = Config(mcp_token="t", repos=frozenset({REPO}), git_url=self.bare, **config)
        foreman = Foreman(Store(":memory:"), self.github, config, self.relay)
        foreman.intake(REPO, 7)
        return foreman

    def worker_change(self):
        """What any worker would produce for the WorkOrder: one commit on factory/7."""
        git("checkout", "-q", "-B", "factory/7", "origin/main", cwd=self.work)
        self.write("app.py", "print('new')\n")
        git("commit", "-q", "-am", "Fail loud", cwd=self.work)

    def complete(self, foreman, output):
        wi = foreman.tick(ID)["workItem"]
        self.relay.finish(wi["providerTaskId"], output=output(wi["activeWorkOrderId"]))
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "pr_open", wi.get("attempts"))
        return wi

    def tree(self, sha):
        return git("rev-parse", f"{sha}^{{tree}}", cwd=self.bare)

    def test_unified_and_mbox_patches_push_the_workers_change(self):
        self.worker_change()
        workers_tree = git("rev-parse", "HEAD^{tree}", cwd=self.work)
        for fmt, diff in (("unified", git("diff", "origin/main", "HEAD", cwd=self.work) + "\n"),
                          ("mbox", git("format-patch", "--stdout", "origin/main", cwd=self.work) + "\n")):
            with self.subTest(fmt):
                wi = self.complete(self.foreman(), lambda order_id: result_line(order_id, diff, fmt))
                self.assertEqual((wi["activeWorkOrderId"], self.tree(wi["headSha"])), (None, workers_tree))
                self.assertIn("- `app.py`: modified (+1 -1)", self.github.pulls[0]["body"])
                git("push", "-q", "origin", "--delete", "factory/7", cwd=self.work)

    def test_failed_patch_is_an_attempt_and_the_next_goes_to_another_worker(self):
        foreman = self.foreman(workers=WORKERS)
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual(self.relay.tasks[wi["providerTaskId"]]["agentId"], "codex")
        self.relay.finish(wi["providerTaskId"],
                          output=result_line(wi["activeWorkOrderId"], "diff --git a/nope.py b/nope.py\nbroken\n"))
        wi = foreman.tick(ID)["workItem"]
        self.assertIn("patch not pushed: git apply failed", wi["attempts"][0])
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual(self.relay.tasks[wi["providerTaskId"]]["agentId"], "pi-glm")

    def test_an_issue_with_a_worker_line_goes_to_that_worker_and_fails_over_only_to_named_ones(self):
        foreman = self.foreman(body=f"Worker: opencode-glm\nWorker: pi-glm\n\n{ISSUE_BODY}", workers=THREE)
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual(self.relay.tasks[wi["providerTaskId"]]["agentId"], "opencode-glm")
        self.relay.finish(wi["providerTaskId"], output="no Result")
        foreman.tick(ID)  # the failed attempt
        wi = foreman.tick(ID)["workItem"]  # the next pass: the second named worker, not codex
        self.assertEqual(self.relay.tasks[wi["providerTaskId"]]["agentId"], "pi-glm")

    def test_an_issue_that_names_an_agent_not_in_factory_workers_goes_to_a_person(self):
        foreman = self.foreman(body=f"Worker: codex-unsandboxed\n\n{ISSUE_BODY}", workers=WORKERS)
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "human_waiting")
        self.assertIn("names codex-unsandboxed, which is not in FACTORY_WORKERS (codex, pi-glm)", wi["blockedOn"])
        self.assertEqual(self.relay.tasks, {})

    def test_a_patch_to_a_protected_path_is_never_pushed(self):
        # A pushed workflow file runs (`on: push`) with the repo's secrets before any later check.
        git("checkout", "-q", "-B", "factory/7", "origin/main", cwd=self.work)
        os.makedirs(os.path.join(self.work, ".github", "workflows"))
        self.write(".github/workflows/x.yml", "on: push\n")
        self.write("app.py", "print('new')\n")
        git("add", "-A", cwd=self.work)
        diff = git("diff", "--cached", "origin/main", cwd=self.work) + "\n"
        foreman = self.foreman()
        wi = foreman.tick(ID)["workItem"]
        self.relay.finish(wi["providerTaskId"], output=result_line(wi["activeWorkOrderId"], diff))
        wi = foreman.tick(ID)["workItem"]
        self.assertIn("changes protected path .github/workflows/x.yml", wi["attempts"][0])
        self.assertIsNone(self.github.branch_head(REPO, "factory/7"))

    def test_a_protected_path_is_refused_whatever_name_the_base_has(self):
        """patches.apply resolves the base once: named HEAD, it would be the new commit by the check."""
        git("checkout", "-q", "-B", "factory/7", "origin/main", cwd=self.work)
        os.makedirs(os.path.join(self.work, ".github", "workflows"))
        self.write(".github/workflows/x.yml", "on: push\n")
        git("add", "-A", cwd=self.work)
        patch = {"format": "unified", "diff": git("diff", "--cached", "origin/main", cwd=self.work) + "\n"}
        for base in ("HEAD", "@"):
            with self.subTest(base), self.assertRaisesRegex(patches.PatchError, "protected path .github/"):
                patches.apply(self.bare, "", "factory/7", base, patch, "Add a workflow", force=True,
                              protected=[".github/"])
        self.assertEqual(git("branch", "--list", "factory/7", cwd=self.bare), "")  # never pushed

    def test_a_patch_that_takes_the_branch_over_its_limits_is_never_pushed(self):
        """ADR-0040 with ADR-0070. Counted from where the branch left main, as GitHub counts the PR:
        a small fix on a big PR counts the whole PR. The file count binds, the line count is advisory
        unless a WorkOrder says hard."""
        git("checkout", "-q", "-B", "factory/7", "origin/main", cwd=self.work)
        self.write("app.py", "print('new')\n")
        git("commit", "-q", "-am", "The PR so far", cwd=self.work)
        git("push", "-q", "origin", "factory/7", cwd=self.work)
        head = git("rev-parse", "HEAD", cwd=self.work)
        self.write("more.py", "a = 1\nb = 2\n")
        git("add", "-A", cwd=self.work)
        patch = {"format": "unified", "diff": git("diff", "--cached", "HEAD", cwd=self.work) + "\n"}
        for limits in (("main", 3, 20), ("main", 3, 20, False), ("main", 600, 1), ("main", 600, 1, False)):
            with self.subTest(limits), self.assertRaisesRegex(patches.PatchError, "4 lines in 2 files exceeds"):
                patches.apply(self.bare, "", "factory/7", head, patch, "Fix", force=False, limits=limits)
        self.assertEqual(git("rev-parse", "refs/heads/factory/7", cwd=self.bare), head)  # never pushed
        # Advisory lines exceed: warned, and pushed anyway; at the target it just pushes.
        pushed = patches.apply(self.bare, "", "factory/7", head, patch, "Fix", force=False,
                               limits=("main", 3, 2, True))
        self.assertEqual(git("rev-parse", "refs/heads/factory/7", cwd=self.bare), pushed)
        rolled = patches.apply(self.bare, "", "factory/7", head, patch, "Reset to head", force=True,
                               limits=("main", 4, 2, True))
        self.assertEqual(git("rev-parse", "refs/heads/factory/7", cwd=self.bare), rolled)
        # A WorkOrder that says the line bound is hard still refuses (migration path, ADR-0070).
        git("push", "-q", "-f", "origin", f"{head}:refs/heads/factory/7", cwd=self.work)
        with self.assertRaisesRegex(patches.PatchError, "exceeds the WorkOrder constraint of 3 lines"):
            patches.apply(self.bare, "", "factory/7", head, patch, "Fix", force=False,
                          limits=("main", 3, 2, False))

    def test_a_change_over_the_workorders_limits_fails_the_attempt_before_the_push(self):
        git("checkout", "-q", "-B", "factory/7", "origin/main", cwd=self.work)
        for n in range(21):
            self.write(f"big{n}.py", "x = 1\n")  # 21 files: over the file count, which binds
        git("add", "-A", cwd=self.work)
        diff = git("diff", "--cached", "origin/main", cwd=self.work) + "\n"
        foreman = self.foreman()
        wi = foreman.tick(ID)["workItem"]
        self.relay.finish(wi["providerTaskId"], output=result_line(wi["activeWorkOrderId"], diff))
        wi = foreman.tick(ID)["workItem"]
        self.assertIn("patch not pushed: 21 lines in 21 files exceeds the WorkOrder constraint of 20 files",
                      wi["attempts"][0])
        self.assertIsNone(self.github.branch_head(REPO, "factory/7"))

    def test_an_advisory_lines_exceedance_is_pushed_and_the_pr_opens(self):
        """ADR-0070: a patch 1 line over the 600 target is pushed; the whole pull request is noted."""
        git("checkout", "-q", "-B", "factory/7", "origin/main", cwd=self.work)
        self.write("big.py", "x = 1\n" * 601)
        git("add", "-A", cwd=self.work)
        diff = git("diff", "--cached", "origin/main", cwd=self.work) + "\n"
        foreman = self.foreman()
        wi = foreman.tick(ID)["workItem"]
        self.relay.finish(wi["providerTaskId"], output=result_line(wi["activeWorkOrderId"], diff))
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "pr_open", wi.get("attempts"))
        self.assertTrue(self.github.branch_head(REPO, "factory/7"))
        noted = [row["detail"] for row in foreman.store.history(ID) if row["action"] == "pr_size_advisory"]
        self.assertEqual(noted, [{"lines": 601, "target": 600, "files": 1}])

    def test_a_patch_that_is_not_text_is_a_patch_error(self):
        """poll turns a PatchError into a failed attempt; anything else leaves the WorkItem stuck."""
        with self.assertRaisesRegex(patches.PatchError, "not UTF-8 text"):
            patches.apply(self.bare, "", "factory/7", "main", {"format": "unified", "diff": "diff \ud800\n"},
                          "Nothing", force=True)

    def test_local_only_repo(self):
        # FACTORY_LOCAL_ONLY_REPOS may spell the repo in another case: GitHub repo names ignore case
        wi = self.foreman(workers=WORKERS, local_only_repos=frozenset({REPO.upper()})).tick(ID)["workItem"]
        self.assertEqual(self.relay.tasks[wi["providerTaskId"]]["agentId"], "pi-glm")
        wi = self.foreman(local_only_repos=frozenset({REPO})).tick(ID)["workItem"]
        self.assertEqual(wi["state"], "human_waiting")
        self.assertIn("no local worker", wi["blockedOn"])


if __name__ == "__main__":
    unittest.main()
