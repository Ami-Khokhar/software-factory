"""Routing by locality with failover, and patch Results that factory-mcp pushes with real git."""
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from factory_mcp import patches, worker_ops
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.routing import family, route
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

    def test_a_worker_family_is_the_model_token_in_its_id(self):
        self.assertEqual([family(worker) for worker in
                          ("pi-glm-factory", "opencode-glm-factory", "pi-deepseek-factory", "codex")],
                         ["glm", "glm", "deepseek", "codex"])

    def test_a_model_id_is_read_for_its_family_too(self):
        self.assertEqual([family(name) for name in
                          ("deepseek-v4.1-flash", "glm-5.3-flash", "mimo-v2.6-flash-free", "codex")],
                         ["deepseek", "glm", "mimo", "codex"])

    def test_a_timeout_family_is_avoided_while_another_family_may_run(self):
        """ADR-0081: pi-glm and opencode-glm are one model family, so avoiding the family passes
        both of them, and the preference never widens the set of workers that may run."""
        self.assertEqual(route(THREE, False, {"pi-glm"}, avoid_families={"glm"}), "codex")
        self.assertEqual(route(THREE, False, set(), avoid_families={"glm"}), "codex")
        # both families failed: the ordinary order stands, never a wider set
        self.assertEqual(route(THREE, False, {"codex", "pi-glm", "opencode-glm"}, avoid_families={"glm"}),
                         "codex")
        # nobody outside the avoided family may run: the same family again, not an invented worker
        self.assertEqual(route(THREE, True, set(), avoid_families={"glm"}), "pi-glm")
        self.assertEqual(route(THREE, True, {"pi-glm"}, avoid_families={"glm"}), "opencode-glm")


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

    def test_an_issue_that_names_an_agent_not_in_factory_workers_goes_to_an_allowed_worker(self):
        """ADR-0110: a stale pin falls back to FACTORY_WORKERS and leaves an audit row, not a question."""
        foreman = self.foreman(body=f"Worker: codex-unsandboxed\n\n{ISSUE_BODY}", workers=WORKERS)
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "implementing")
        self.assertEqual(self.relay.tasks[wi["providerTaskId"]]["agentId"], "codex")
        rows = [h["detail"] for h in foreman.store.history(ID) if h["action"] == "unknown_worker_rerouted"]
        self.assertEqual(rows, [{"unknown": ["codex-unsandboxed"], "worker": "codex"}])

    def test_a_stale_pin_beside_an_allowed_named_worker_keeps_to_the_named_worker(self):
        foreman = self.foreman(body=f"Worker: codex-unsandboxed\nWorker: pi-glm\n\n{ISSUE_BODY}",
                               workers=WORKERS)
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual(self.relay.tasks[wi["providerTaskId"]]["agentId"], "pi-glm")

    def test_a_stale_pin_with_no_eligible_allowed_worker_waits_for_a_person(self):
        foreman = self.foreman(body=f"Worker: pi-gone-factory\n\n{ISSUE_BODY}",
                               workers=(("pi-deepseek-factory", "local"),))
        self.github.contents[(REPO, ".github/pr-gatekeeper.json")] = '{"reviewer_model": "deepseek-v4.1-flash"}'
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual((wi["state"], wi["blockedOn"]),
                         ("human_waiting", "every allowed worker is in the reviewer family deepseek (the "
                          "issue's Worker: line also names pi-gone-factory, which is not in "
                          "FACTORY_WORKERS)"))
        self.assertEqual(self.relay.tasks, {})

    def test_a_stale_pin_with_no_worker_eligible_names_the_dropped_pin(self):
        foreman = self.foreman(body=f"Worker: pi-gone-factory\n\n{ISSUE_BODY}",
                               workers=(("pi-deepseek-factory", "local"),))
        self.github.contents[(REPO, ".github/pr-gatekeeper.json")] = '{"reviewer_model": "deepseek-v4.1-flash"}'
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "human_waiting")
        self.assertIn("pi-gone-factory", wi["blockedOn"])
        detail = next(h["detail"] for h in foreman.store.history(ID) if h["action"] == "no_worker")
        self.assertEqual(detail["workers"], ["pi-gone-factory"])

    def test_a_stale_pin_on_a_local_only_repo_names_it_in_the_wait(self):
        foreman = self.foreman(body=f"Worker: pi-gone-factory\n\n{ISSUE_BODY}",
                               local_only_repos=frozenset({REPO}))
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "human_waiting")
        self.assertIn("pi-gone-factory", wi["blockedOn"])
        detail = next(h["detail"] for h in foreman.store.history(ID) if h["action"] == "no_worker")
        self.assertEqual(detail["workers"], ["pi-gone-factory"])

    def test_two_stale_pins_with_no_worker_eligible_name_both_in_the_plural(self):
        foreman = self.foreman(body=f"Worker: pi-gone-factory\nWorker: pi-absent-factory\n\n{ISSUE_BODY}",
                               workers=(("pi-deepseek-factory", "local"),))
        self.github.contents[(REPO, ".github/pr-gatekeeper.json")] = '{"reviewer_model": "deepseek-v4.1-flash"}'
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "human_waiting")
        self.assertEqual(wi["blockedOn"],
                         "every allowed worker is in the reviewer family deepseek (the "
                         "issue's Worker: lines also name pi-gone-factory, pi-absent-factory, "
                         "which are not in FACTORY_WORKERS)")
        detail = next(h["detail"] for h in foreman.store.history(ID) if h["action"] == "no_worker")
        self.assertEqual(detail["workers"], ["pi-gone-factory", "pi-absent-factory"])

    def test_a_pass_never_goes_to_the_gate_reviewers_family(self):
        """ADR-0085: the gate reviews deepseek-v4.1-flash, so the first pass skips pi-deepseek-factory."""
        foreman = self.foreman(workers=(("pi-deepseek-factory", "local"), ("pi-glm-factory", "local")))
        self.github.contents[(REPO, ".github/pr-gatekeeper.json")] = '{"reviewer_model": "deepseek-v4.1-flash"}'
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual(self.relay.tasks[wi["providerTaskId"]]["agentId"], "pi-glm-factory")

    def test_a_worker_line_that_names_only_the_reviewers_family_waits_for_a_person(self):
        foreman = self.foreman(body=f"Worker: pi-deepseek-factory\n\n{ISSUE_BODY}",
                               workers=(("pi-deepseek-factory", "local"), ("pi-glm-factory", "local")))
        self.github.contents[(REPO, ".github/pr-gatekeeper.json")] = '{"reviewer_model": "deepseek-v4.1-flash"}'
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual((wi["state"], wi["blockedOn"]),
                         ("human_waiting", "every allowed worker is in the reviewer family deepseek"))
        self.assertEqual(self.relay.tasks, {})

    def test_a_repo_without_a_gate_config_routes_as_before(self):
        foreman = self.foreman(workers=(("pi-deepseek-factory", "local"), ("pi-glm-factory", "local")))
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual(self.relay.tasks[wi["providerTaskId"]]["agentId"], "pi-deepseek-factory")

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
        # ADR-0079: a 5th source spec switches the metric to source lines, whole PR included; over
        # the ceiling is the size-only marker the Foreman routes to decomposition.
        with self.assertRaisesRegex(patches.PatchError, "source-size-ceiling: .*4 source lines"):
            patches.apply(self.bare, "", "factory/7", head, patch, "Fix", force=False,
                          limits=("main", 600, 20, True,
                                  {"target": 4, "ceiling": 3, "advisory": True}))
        pushed = patches.apply(self.bare, "", "factory/7", head, patch, "Fix", force=False,
                               limits=("main", 600, 20, True,
                                       {"target": 4, "ceiling": 4, "advisory": True}))
        self.assertEqual(git("rev-parse", "refs/heads/factory/7", cwd=self.bare), pushed)

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
        """ADR-0079: a patch 1 source line over the 700 target is pushed; the whole pull request is
        noted, and tests and docs never count against the source target."""
        git("checkout", "-q", "-B", "factory/7", "origin/main", cwd=self.work)
        self.write("big.py", "x = 1\n" * 701)
        git("add", "-A", cwd=self.work)
        diff = git("diff", "--cached", "origin/main", cwd=self.work) + "\n"
        foreman = self.foreman()
        wi = foreman.tick(ID)["workItem"]
        self.relay.finish(wi["providerTaskId"], output=result_line(wi["activeWorkOrderId"], diff))
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "pr_open", wi.get("attempts"))
        self.assertTrue(self.github.branch_head(REPO, "factory/7"))
        noted = [row["detail"] for row in foreman.store.history(ID)
                 if row["action"] == "pr_size_advisory"]
        self.assertEqual(noted, [{"source": 701, "tests": 0, "docs": 0, "total": 701,
                                  "target": 700, "files": 1}])

    def test_a_patch_that_is_not_text_is_a_patch_error(self):
        """poll turns a PatchError into a failed attempt; anything else leaves the WorkItem stuck."""
        with self.assertRaisesRegex(patches.PatchError, "not UTF-8 text"):
            patches.apply(self.bare, "", "factory/7", "main", {"format": "unified", "diff": "diff \ud800\n"},
                          "Nothing", force=True)

    def test_local_only_fallback_names_an_enabled_worker_it_cannot_honour(self):
        """An enabled activation row that routing drops keeps its reason in the escalation:
        a person reads the real cause, never "no local worker" when FACTORY_WORKERS is fine."""
        credential = os.path.join(self.tmp.name, "auth.json")
        open(credential, "w").write("auth")
        registry = os.path.join(self.tmp.name, "agents.json")
        with open(registry, "w") as handle:
            json.dump({"agents": [{"id": "pi-glm", "command": "python3", "args": [
                str(worker_ops.RUNNER), "--image", "factory-worker:latest",
                "--credential", f"{credential}:/tmp/auth",
                "--", "opencode", "run", "--model", "pi/glm"], "timeoutMs": 1000}]}, handle)
        foreman = self.foreman(workers=WORKERS, local_only_repos=frozenset({REPO}), agents_file=registry)
        foreman.store.set_worker_activation("pi-glm", 1, "any stored binding",
                                            {"worker": "pi-glm", "enabled": True,
                                             "binding": "any stored binding", "by": "Day Desk",
                                             "note": "enabled for the test", "agent": True})
        with mock.patch.object(worker_ops, "_image_id", return_value=None):
            wi = foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "human_waiting")
        self.assertEqual([r["action"] for r in foreman.store.history(ID) if r["action"] == "no_worker"],
                         ["no_worker"])
        self.assertIn("pi-glm is enabled but cannot be used: worker image is unavailable; "
                      "repair it and rerun factory_check_worker", wi["blockedOn"])

    def test_local_only_repo(self):
        # FACTORY_LOCAL_ONLY_REPOS may spell the repo in another case: GitHub repo names ignore case
        wi = self.foreman(workers=WORKERS, local_only_repos=frozenset({REPO.upper()})).tick(ID)["workItem"]
        self.assertEqual(self.relay.tasks[wi["providerTaskId"]]["agentId"], "pi-glm")
        wi = self.foreman(local_only_repos=frozenset({REPO})).tick(ID)["workItem"]
        self.assertEqual(wi["state"], "human_waiting")
        self.assertIn("no local worker", wi["blockedOn"])

    def test_set_worker_edits_the_issue_and_the_next_tick_routes_to_it(self):
        """factory_set_worker (ADR-0029) is Day Desk's sanctioned chooser: it writes the Worker:
        lines the next tick reads, so a choice reaches the dispatch without a hand-edited issue."""
        foreman = self.foreman(workers=THREE)
        foreman.set_worker(ID, ["opencode-glm", "pi-glm"], "day-desk")
        self.assertIn("Worker: opencode-glm\nWorker: pi-glm",
                      self.github.issue(REPO, 7)["body"])
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual(self.relay.tasks[wi["providerTaskId"]]["agentId"], "opencode-glm")

    def test_set_worker_refuses_an_agent_not_in_factory_workers_before_editing(self):
        """An unknown id is refused before the edit: the relay holds unsandboxed agents an issue must
        never reach."""
        foreman = self.foreman(workers=THREE)
        before = self.github.issue(REPO, 7)["body"]
        with self.assertRaisesRegex(ToolError, "not in FACTORY_WORKERS"):
            foreman.set_worker(ID, ["codex-unsandboxed"], "day-desk")
        self.assertEqual(self.github.issue(REPO, 7)["body"], before)  # unchanged

    def test_set_worker_falls_back_to_factory_workers_order_for_empty(self):
        """[] clears the Worker: lines, so routing uses FACTORY_WORKERS order again."""
        foreman = self.foreman(body=f"Worker: opencode-glm\n\n{ISSUE_BODY}", workers=THREE)
        foreman.set_worker(ID, [], "day-desk")
        self.assertNotIn("Worker:", self.github.issue(REPO, 7)["body"])
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual(self.relay.tasks[wi["providerTaskId"]]["agentId"], "codex")

    def test_set_worker_refuses_while_a_worker_is_running(self):
        """The choice may not change under a live pass: the dispatch already sent the WorkOrder,
        and the issue body it read is the order in force."""
        foreman = self.foreman(workers=THREE)
        wi = foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "implementing")
        with self.assertRaisesRegex(ToolError, "worker running now"):
            foreman.set_worker(ID, ["pi-glm"], "day-desk")
        self.assertEqual(self.github.issue(REPO, 7)["body"], ISSUE_BODY)  # unchanged

    def test_set_worker_refuses_a_terminal_workitem(self):
        """A closed, cancelled or exhausted WorkItem's worker no longer matters."""
        foreman = self.foreman(workers=THREE)
        foreman.store.put(dict(foreman.store.get(ID), state="closed"))
        with self.assertRaisesRegex(ToolError, "its worker no longer matters"):
            foreman.set_worker(ID, ["pi-glm"], "day-desk")


if __name__ == "__main__":
    unittest.main()
