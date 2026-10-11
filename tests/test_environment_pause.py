"""#191: a confirmed environment fault pauses the WorkItem for repair instead of dispatching another
worker against the same broken environment. A failing test and a harness timeout that the diagnostics
support are not environment faults, and keep the normal retry; a person's retry after the pause works."""
import json
import unittest
from pathlib import Path

from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.store import Store
from tests.fakes import FakeGit, FakeGitHub, FakeRelay

REPO, ID = "o/r", "o/r#7"


class EnvironmentPauseTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.git = FakeGit(self.github).install(self)
        self.github.add_issue(REPO, 7)
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO}), wall_ms=1000), self.relay)
        self.foreman.intake(REPO, 7)

    def dispatch(self):
        wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "implementing")
        return wi, self.relay.tasks[wi["providerTaskId"]]

    def fail_with(self, diagnostics=None, evidence=(), error="the harness timed out"):
        """A worker pass that ends in a failed Result, then the tick that reads it."""
        wi, task = self.dispatch()
        body = {"schemaVersion": "factory.result/v1", "workOrderId": wi["activeWorkOrderId"],
                "status": "failed", "patch": None, "evidence": list(evidence),
                "error": error, "startedAt": "2026-09-24T00:00:00Z", "finishedAt": "2026-09-24T00:01:00Z"}
        if diagnostics is not None:
            body["diagnostics"] = diagnostics
        self.relay.finish(task["id"], output="working...\n" + json.dumps(body))
        return self.foreman.tick(ID)["workItem"]

    def test_a_failure_the_diagnostics_name_an_environment_for_pauses_the_pass(self):
        diagnostics = {"elapsedMs": 1500, "phases": [
            {"phase": "checkout", "elapsedMs": 1200, "exitCode": 128, "tail": "fatal: fetch failed"}]}
        wi = self.fail_with(diagnostics, error="git fetch failed")
        self.assertEqual(wi["state"], "human_waiting")
        self.assertIn("environment failed", wi["blockedOn"])
        self.assertIn("factory_resolve_human", wi["blockedOn"])
        log = next(h for h in self.foreman.store.history(ID) if h["action"] == "attempt_failed")
        self.assertEqual(log["detail"]["cause"], "checkout or setup")

    def test_after_an_environment_failure_tick_dispatches_no_worker(self):
        diagnostics = {"elapsedMs": 1500, "phases": [
            {"phase": "checkout", "elapsedMs": 1200, "exitCode": 128, "tail": "fatal: fetch failed"}]}
        wi = self.fail_with(diagnostics, error="git fetch failed")
        for _ in range(3):
            wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual(len(self.foreman.relay.tasks), 1)  # the failed pass alone
        self.assertEqual(wi["state"], "human_waiting")

    def test_a_failing_test_keeps_the_normal_retry(self):
        """A verification phase that failed is code, not environment: the next pass runs."""
        diagnostics = {"elapsedMs": 9000, "phases": [
            {"phase": "harness execution", "elapsedMs": 1000, "exitCode": 0},
            {"phase": "verification", "elapsedMs": 8000, "exitCode": 1, "tail": "FAILED (failures=2)"}]}
        wi = self.fail_with(diagnostics, error="verify exited 1")
        self.assertEqual(wi["state"], "ready")
        wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "implementing")
        self.assertEqual(len(self.foreman.relay.tasks), 2)

    def test_a_harness_timeout_keeps_the_normal_retry(self):
        """A phase that ran the pass out is not evidence against the environment: the next pass runs."""
        diagnostics = {"elapsedMs": 990, "phases": [
            {"phase": "harness execution", "elapsedMs": 970, "exitCode": 143,
             "tail": "killed after the wall clock"}]}
        wi = self.fail_with(diagnostics)
        self.assertEqual(wi["state"], "ready")
        wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "implementing")

    def test_a_persons_retry_after_the_pause_runs_a_worker(self):
        diagnostics = {"elapsedMs": 1500, "phases": [
            {"phase": "checkout", "elapsedMs": 1200, "exitCode": 128, "tail": "fatal: fetch failed"}]}
        self.fail_with(diagnostics, error="git fetch failed")
        self.foreman.resolve_human(ID, "retry", "pip cache cleared", by="owner")
        wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "implementing")
        order = json.loads(self.relay.tasks[wi["providerTaskId"]]["input"].split("WorkOrder:\n", 1)[1])
        self.assertEqual(order["findingsToAddress"],
                         [{"fingerprint": "human", "problem": "pip cache cleared"}])


if __name__ == "__main__":
    unittest.main()
