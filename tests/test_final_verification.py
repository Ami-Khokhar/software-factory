"""The final integrated verification (#205): replacement recovery, the patch limits and the
completion reporting driven together over the public Foreman API, plus the gate-secret timeout
case. A WorkItem that was interrupted mid-replace, replayed, and whose replacement worker busts
the PR limits still ends in one honest escalation the weekly report counts. Host gates without a
relay checkout or a Docker daemon are recorded in the checkpoint doc, never faked here."""
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import factory_mcp.gate_secrets as gate_secrets
from factory_mcp import metrics
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp import workitem
from factory_mcp.store import Store
from tests.fakes import FakeGit, FakeGitHub, FakeRelay, result_line
from tests.test_plan import FIRST, REPO, SECOND, plan

KEYCHAIN_TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"  # shaped so the redaction regex catches it
ORIGINAL = SECOND  # 201: ready, waiting on nothing — the same fixture as the recovery suites
A, B = 300, 301  # FakeGitHub numbers new issues from 200: start_plan gives 200, 201, 202


class CountingGitHub(FakeGitHub):
    """FakeGitHub that counts every write, so the tests can show a refusal or a replay makes
    none, and whose `close_issue` can drop midway so a replace is interrupted."""

    def __init__(self):
        super().__init__()
        self.writes = []
        self.drop_close = None

    def close_issue(self, repo, number):
        self.writes.append(("close_issue", repo, number))
        if (repo, number) == self.drop_close:
            raise RuntimeError("github dropped the connection")
        return super().close_issue(repo, number)

    def edit_issue(self, repo, number, body):
        self.writes.append(("edit_issue", repo, number))
        return super().edit_issue(repo, number, body)


class RecoveryLimitTest(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".sqlite")
        self.github = CountingGitHub()
        self.foreman = Foreman(Store(self.path), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO})), FakeRelay())
        FakeGit(self.github).install(self)
        self.foreman.start_plan(REPO, plan("One", "Two", "Three"), "amteshwar")  # 202 waits on 201
        self.foreman.set_dependencies(SECOND, [], "amteshwar")  # 201 waits on nothing
        self.github.add_issue(REPO, A)
        self.github.add_issue(REPO, B)
        self.foreman.store.put(dict(self.foreman.store.get(ORIGINAL), state="human_waiting",
                                    blockedOn="this issue depends on #199, which ended cancelled"))
        self.github.writes.clear()

    def tearDown(self):
        self.foreman.store.close()
        os.unlink(self.path)

    def replace(self):
        return self.foreman.replace_issue(ORIGINAL, [A, B], "amteshwar", "owner approves",
                                          "final-verification-req")

    def interrupt(self):
        """A replace whose last write (the original's close) fails after the graph edits landed:
        the journal is at its close, and the caller owns nothing it did not journal."""
        self.github.drop_close = (REPO, 201)
        with self.assertRaisesRegex(RuntimeError, "dropped the connection"):
            self.replace()
        self.github.drop_close = None

    def restart(self):
        store = self.foreman.store
        self.foreman.store = Store(self.path)
        self.foreman = Foreman(self.foreman.store, self.github,
                               Config(mcp_token="t", repos=frozenset({REPO})), FakeRelay())
        store.close()

    def test_an_interrupted_replace_replays_the_one_missing_write_and_a_repeat_makes_none(self):
        """Restart the store, replay the same requestId: the graph ends exactly where the plan said
        (201 retired, 300 then 301 chained, 202 moved onto the last replacement, nothing
        duplicated), and a second replay after the run is done performs no GitHub writes at all."""
        self.interrupt()
        writes_before_restart = len(self.github.writes)
        self.restart()
        self.replace()  # the replay: only the close was left

        self.assertEqual(self.github.issues[(REPO, 201)]["state"], "closed")
        depends = {number: workitem.depends_on(self.github.issues[(REPO, number)]["body"])
                   for number in (A, B, 202)}
        self.assertEqual(depends[A], [])
        self.assertEqual(depends[B], [A])
        self.assertEqual(depends[202], [B])
        self.assertEqual(self.github.writes[writes_before_restart:],
                         [("close_issue", REPO, 201)])

        # a repeat once the run is done and released refuses before any write: GitHub is the truth
        with self.assertRaisesRegex(ToolError, "is cancelled; only a WorkItem waiting on recovery"):
            self.replace()
        self.assertEqual(self.github.writes[writes_before_restart:],
                         [("close_issue", REPO, 201)])


class ReplacementToReportTest(unittest.TestCase):
    """The recovery hand-off into the guards and the report: a replacement approved and dispatched
    whose worker returns a patch over the PR limits fails twice, goes to a person naming the size,
    and the completion report counts that escalation once. Merge authority and the 600-line/20-file
    limits are never loosened on the way."""
    def setUp(self):
        self.github = CountingGitHub()
        self.relay = FakeRelay()
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO})), self.relay)
        self.git = FakeGit(self.github).install(self)
        self.foreman.start_plan(REPO, plan("One"), "amteshwar")
        self.foreman.store.put(dict(self.foreman.store.get(FIRST), state="human_waiting",
                                    blockedOn="this issue depends on #199, which ended cancelled"))
        self.github.add_issue(REPO, A)
        self.foreman.replace_issue(FIRST, [A], "amteshwar", "owner approves", "size-req")
        self.foreman.approve_issue(REPO, A, "amteshwar")
        self.github.writes.clear()

    def workitem_id(self):
        return f"{REPO}#{A}"

    def dispatch(self):
        wi = self.foreman.tick(self.workitem_id())["workItem"]
        self.assertEqual(wi["state"], "implementing")
        return wi

    def fail_with(self, files):
        wi = self.dispatch()
        self.git.files = files
        self.relay.finish(wi["providerTaskId"], output=result_line(wi["activeWorkOrderId"]))
        return self.foreman.tick(self.workitem_id())["workItem"]

    def test_a_replacement_worker_over_the_limit_ends_reported_to_a_person_untouched_by_github(self):
        wi = self.fail_with((("src/big.py", 591, 10),))  # 601 changed lines: over the 600 limit
        self.assertEqual(wi["state"], "ready")
        self.assertIn("601 lines in 1 files exceeds the WorkOrder constraints", wi["attempts"][0])
        self.assertEqual(self.github.pulls, [])  # nothing was pushed that busts the limits

        wi = self.fail_with((("src/big2.py", 595, 12),))
        self.assertEqual(wi["state"], "blocked")
        reasons = metrics.escalation_reasons([self.foreman.store.get(self.workitem_id())],
                                             self.foreman.store.history, "0000", "9999")
        self.assertEqual(len(reasons), 1)
        self.assertIn("2 worker attempts", reasons[0]["reason"])
        self.assertIn("factory_resolve_human", reasons[0]["reason"])

    def test_an_environment_stop_in_a_replacement_pauses_it_and_the_report_counts_it_once(self):
        """A confirmed environment fault pauses the replacement on the first pass (ADR-0051): no
        second worker meets the same broken environment, and the report names the environment, not
        the code, as the reason a person is needed."""
        diagnostics = {"elapsedMs": 1500, "phases": [
            {"phase": "checkout", "elapsedMs": 1200, "exitCode": 128,
             "tail": "fatal: fetch failed"}]}
        wi = self.dispatch()
        self.relay.finish(wi["providerTaskId"], output=json.dumps({
            "schemaVersion": "factory.result/v1", "workOrderId": wi["activeWorkOrderId"],
            "status": "failed", "patch": None, "evidence": [],
            "error": "git fetch failed", "diagnostics": diagnostics,
            "startedAt": "2026-10-12T00:00:00Z", "finishedAt": "2026-10-12T00:01:00Z"}))
        wi = self.foreman.tick(self.workitem_id())["workItem"]
        self.assertEqual(wi["state"], "human_waiting")
        self.assertIn("environment failed", wi["blockedOn"])
        self.assertIn("Repair the environment", wi["blockedOn"])
        reasons = metrics.escalation_reasons([self.foreman.store.get(self.workitem_id())],
                                             self.foreman.store.history, "0000", "9999")
        self.assertEqual(len(reasons), 1)
        self.assertIn("environment failed", reasons[0]["reason"])
        self.assertEqual(len(self.relay.tasks), 1)  # the first pass alone: no worker was re-dispatched


class CycleGuardTest(unittest.TestCase):
    def setUp(self):
        self.github = CountingGitHub()
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO})), FakeRelay())
        FakeGit(self.github).install(self)
        self.foreman.start_plan(REPO, plan("One", "Two", "Three"), "amteshwar")

    def test_a_dependency_that_would_cycle_the_plan_is_refused_without_writes(self):
        writes = len(self.github.writes)
        with self.assertRaisesRegex(ToolError, "cycle"):
            self.foreman.set_dependencies(FIRST, [202], "amteshwar")  # 202 already waits on 200
        self.assertEqual(len(self.github.writes), writes)


class SecretTimeoutTest(unittest.TestCase):
    """`gh secret set` that hangs (a gate secret's subprocess timeout): the secret fails, and
    neither the reason nor the Keychain's value ever names the secret (ADR-0022/0023)."""

    def setUp(self):
        def run(argv, input=None, env=None, capture_output=True, text=True, timeout=None):
            if "list" in argv:
                return subprocess.CompletedProcess(argv, 0,
                                                   stdout='[{"name": "GATEKEEPER_READ_TOKEN"}]',
                                                   stderr="")  # GATEKEEPER is on GitHub; OPENCODE is missing
            if argv[0] == "security":
                return subprocess.CompletedProcess(argv, 0, stdout=KEYCHAIN_TOKEN + "\n", stderr="")
            if "set" in argv:
                raise subprocess.TimeoutExpired(argv, 60)
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

        with mock.patch("factory_mcp.gate_secrets.subprocess.run", side_effect=run):
            self.statuses = gate_secrets.ensure("o/r", KEYCHAIN_TOKEN)

    def test_a_timed_out_secret_set_is_reported_without_the_secret(self):
        set_status = next(s for s in self.statuses if s["name"] == "OPENCODE_GO_API_KEY")
        self.assertEqual(set_status["status"], "failed: gh secret set timed out")
        read_status = next(s for s in self.statuses if s["name"] == "GATEKEEPER_READ_TOKEN")
        self.assertEqual(read_status["status"], "already set")
        self.assertNotIn(KEYCHAIN_TOKEN, json.dumps(self.statuses))


if __name__ == "__main__":
    unittest.main()
