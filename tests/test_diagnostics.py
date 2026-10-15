"""#190: the attempt audit diagnoses a failure from the Result's optional diagnostics — one cause
read from one factory_status call, sanitized and bounded — and the next WorkOrder carries it
without secrets. A timeout with no supporting diagnostics stays an unknown cause; the blocked
guidance no longer equates it with an issue too big for one pass."""
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from factory_mcp import diagnostics, tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.store import Store
from tests.fakes import FakeGit, FakeGitHub, FakeRelay

REPO, ID = "o/r", "o/r#7"
ROOT = Path(__file__).resolve().parent.parent
ENV_SENTINEL = "FACTORY_TEST_SENTINEL=very-secret-env-value"
TOKEN_SENTINEL = "ghp_SENTINELTOKEN123456"


def schema_errors(name, doc):
    schema = json.loads((ROOT / "schemas" / f"{name}.schema.json").read_text())
    return [e.message for e in Draft202012Validator(schema).iter_errors(doc)]


class DiagnosticsTest(unittest.TestCase):
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

    def fail_with(self, diagnostics=None, evidence=(), error="the harness timed out",
                  status="failed", patch=None):
        """A worker pass that ends in a Result with the given body, then the tick that reads it."""
        wi, task = self.dispatch()
        body = {"schemaVersion": "factory.result/v1", "workOrderId": wi["activeWorkOrderId"],
                "status": status, "patch": patch, "evidence": list(evidence),
                "error": error if status == "failed" else None,
                "startedAt": "2026-09-24T00:00:00Z", "finishedAt": "2026-09-24T00:01:00Z"}
        if diagnostics is not None:
            body["diagnostics"] = diagnostics
        self.relay.finish(task["id"], output="working...\n" + json.dumps(body))
        wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual(schema_errors("workitem", wi), [])
        return wi

    def attempt_log(self):
        return next(h for h in self.foreman.store.history(ID) if h["action"] == "attempt_failed")

    def status(self):
        return tools.build(self.foreman.store, self.foreman)["factory_status"].fn({"id": ID})

    def test_a_setup_phase_that_failed_is_named_in_one_status_without_the_secrets(self):
        """The checkout's own diagnostics say it failed; the findable secrets in its tail do not."""
        diagnostics = {"elapsedMs": 1500, "phases": [
            {"phase": "checkout", "elapsedMs": 1200, "exitCode": 128,
             "tail": f"fatal: fetch failed {ENV_SENTINEL} PLAINTEXT_KEY=hunter2 {TOKEN_SENTINEL}"}]}
        wi = self.fail_with(diagnostics, error="git fetch failed")
        log = self.attempt_log()
        self.assertEqual(log["detail"]["cause"], "checkout or setup")
        self.assertIn("exit 128", log["detail"]["diagnosis"])
        self.assertIn(ENV_SENTINEL.split("=")[0] + "=<redacted>", log["detail"]["diagnosis"])
        for secret in (ENV_SENTINEL, "PLAINTEXT_KEY=hunter2", TOKEN_SENTINEL):
            self.assertNotIn(secret, json.dumps(log["detail"]))
        # "environment": this fault now pauses too (ADR-0051), so the detail carries the repair text
        self.assertEqual(set(log["detail"]),
                         {"to", "from", "reason", "worker", "cause", "diagnosis", "escalation"})
        self.assertIn("diagnosis: checkout or setup: exit 128:", wi["attempts"][0])

        # Evidence in one status: the same cause, and nothing else, on the attempt_failed entry.
        response = self.status()
        found = [h for h in response["history"] if h["action"] == "attempt_failed"]
        self.assertEqual([h["detail"]["cause"] for h in found], ["checkout or setup"])
        self.assertIn("exit 128", response["workItem"]["attempts"][0])
        self.assertNotIn(TOKEN_SENTINEL, json.dumps(response))

    def test_a_harness_phase_that_ran_the_pass_out_is_a_harness_timeout(self):
        """The diagnostics support what the reason says: a phase at most of the WorkOrder's wall."""
        diagnostics = {"elapsedMs": 990, "phases": [
            {"phase": "harness execution", "elapsedMs": 970, "exitCode": 143,
             "tail": "killed after the wall clock"}]}
        wi = self.fail_with(diagnostics)
        self.assertEqual(self.attempt_log()["detail"]["cause"], "harness timeout")

    def test_a_harness_phase_killed_for_time_is_a_harness_timeout_whatever_the_clock_says(self):
        """Exit 124 is the runner's own kill for time, so even a phase the wall clock could never
        cover — the runner leaves room for patch collection and the verify — names the timeout
        (ADR-0052)."""
        diagnostics = {"elapsedMs": 900, "phases": [
            {"phase": "harness execution", "elapsedMs": 850, "exitCode": 124,
             "tail": "step one kthxbye"}]}
        wi = self.fail_with(diagnostics)
        self.assertEqual(self.attempt_log()["detail"]["cause"], "harness timeout")
        self.assertIn("exit 124", wi["attempts"][0])

    def test_a_harness_phase_short_of_the_budget_is_not_a_timeout(self):
        """An early exit is some other failure; only the clock or the runner's own kill for time
        makes a phase a timeout."""
        diagnostics = {"elapsedMs": 500, "phases": [
            {"phase": "harness execution", "elapsedMs": 480, "exitCode": 2, "tail": "agent crashed"}]}
        self.fail_with(diagnostics)
        self.assertNotIn("cause", self.attempt_log()["detail"])

    def test_a_completed_result_without_a_patch_is_an_empty_patch(self):
        wi = self.fail_with(status="completed", patch=None)
        self.assertEqual(self.attempt_log()["detail"]["cause"], "empty patch")
        self.assertIn("diagnosis: empty patch", wi["attempts"][0])

    def test_a_verification_phase_that_failed_is_a_failing_test(self):
        diagnostics = {"elapsedMs": 9000, "phases": [
            {"phase": "harness execution", "elapsedMs": 1000, "exitCode": 0},
            {"phase": "verification", "elapsedMs": 8000, "exitCode": 1, "tail": "FAILED (failures=2)"}]}
        wi = self.fail_with(diagnostics, error="verify exited 1")
        log = self.attempt_log()
        self.assertEqual(log["detail"]["cause"], "failing test")
        self.assertIn("FAILED (failures=2)", log["detail"]["diagnosis"])
        self.assertIn("diagnosis: failing test: exit 1:", wi["attempts"][0])

    def test_a_test_evidence_entry_that_did_not_pass_is_a_failing_test(self):
        evidence = ({"kind": "test", "command": "make test", "exit": 1, "tail": "FAILED"},)
        wi = self.fail_with(error="verify exited 1: FAILED (failures=2)", evidence=evidence)
        self.assertEqual(self.attempt_log()["detail"]["cause"], "failing test")
        self.assertIn("diagnosis: failing test: exit 1: FAILED", wi["attempts"][0])

    def test_malformed_diagnostics_fail_the_attempt_instead_of_crashing_the_tick(self):
        """RED before the guard: shapes the Result check rejects must not raise out of diagnose,
        or the WorkItem stays implementing and every later tick crashes."""
        for diagnostics in ({"elapsedMs": 1, "phases": [{"elapsedMs": 1, "exitCode": 1}]},
                            {"phases": ["checkout"]},
                            {"phases": {"phase": "checkout", "exitCode": 1}},
                            {"phase": "harness execution", "elapsedMs": "970", "exitCode": 1}):
            with self.subTest(diagnostics=diagnostics):
                self.setUp()
                wi = self.fail_with(diagnostics, error="git fetch failed")
                log = self.attempt_log()
                self.assertTrue(log["detail"]["reason"].startswith(
                    "codex: diagnostics need an integer elapsedMs"), log["detail"]["reason"])
                self.assertNotIn("cause", log["detail"])
                self.assertEqual(wi["state"], "ready")

    def test_an_evidence_entry_without_an_exit_is_not_a_failing_test(self):
        """RED before the guard: an entry with no exit is not evidence of a failing test."""
        evidence = ({"kind": "test", "command": "make test"},)
        wi = self.fail_with(error="the harness timed out", evidence=evidence)
        detail = self.attempt_log()["detail"]
        self.assertNotIn("cause", detail)
        self.assertNotIn("failing test", wi["attempts"][0])

    def test_a_timeout_with_no_supporting_diagnostics_stays_unknown(self):
        wi = self.fail_with(diagnostics=None)
        detail = self.attempt_log()["detail"]
        self.assertNotIn("cause", detail)
        self.assertEqual(wi["attempts"][0], "codex: worker reported failed: the harness timed out")
        wi, task = self.dispatch()
        self.relay.finish(task["id"], status="timed_out", error="timed out")
        wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "blocked")
        self.assertNotIn("too big", wi["blockedOn"])
        self.assertNotIn("split", wi["blockedOn"])
        self.assertIn("Fix the cause named in attempts", wi["blockedOn"])

    def test_the_next_workorder_carries_the_bounded_diagnosis_without_the_secrets(self):
        # A failing test, not a checkout fault, so the pass retries and the next order carries the tail
        diagnostics = {"elapsedMs": 9000, "phases": [
            {"phase": "harness execution", "elapsedMs": 1000, "exitCode": 0},
            {"phase": "verification", "elapsedMs": 8000, "exitCode": 1,
             "tail": f"FAILED {ENV_SENTINEL} {TOKEN_SENTINEL} late"}]}
        self.fail_with(diagnostics, error="verify exited 1")
        wi, task = self.dispatch()
        order = json.loads(task["input"].split("WorkOrder:\n", 1)[1])
        problem = order["findingsToAddress"][0]["problem"]
        self.assertEqual(order["findingsToAddress"][0]["fingerprint"], "previous-attempt")
        self.assertIn("diagnosis: failing test: exit 1: FAILED", problem)
        self.assertIn("late", problem)  # the relevant tail of the cause
        self.assertNotIn("very-secret-env-value", problem)
        self.assertNotIn(TOKEN_SENTINEL, task["input"])
        self.assertLess(len(problem), 400)  # bounded, even with a full tail underneath it
        self.assertEqual(schema_errors("workorder", order), [])

    def test_an_intervening_timeout_still_carries_the_last_failing_test_finding(self):
        """#271: the attempt after a verify failure is useful; a later empty timeout must not bury
        it. The carried failing test and the latest timeout ride together, chronologically."""
        failing = "codex: worker reported failed: verify exited 1; diagnosis: failing test: exit 1: FAIL: test_x (m.C.test_x) AssertionError: 20 != 4"
        timeout = "codex: worker reported failed: the harness timed out"
        findings = diagnostics.attempt_findings([failing, timeout])
        self.assertEqual([f["problem"] for f in findings], [failing, timeout])
        self.assertEqual({f["fingerprint"] for f in findings}, {"previous-attempt"})

    def test_an_actionable_latest_attempt_is_carried_alone(self):
        failing = "codex: worker reported failed: verify exited 2; diagnosis: failing test: exit 2: FAIL"
        older = "codex: worker reported failed: the harness timed out; diagnosis: harness timeout: exit 124: step one"
        findings = diagnostics.attempt_findings([older, failing])
        self.assertEqual([f["problem"] for f in findings], [failing])
        self.assertEqual(diagnostics.attempt_findings([]), [])

    def test_the_carried_finding_stays_bound_and_without_secrets(self):
        failing = "codex: verify exited 1" + "; diagnosis: failing test: exit 1: " + "x" * 400
        timeout = "codex: worker reported failed: the harness timed out"
        findings = diagnostics.attempt_findings([failing, timeout])
        self.assertLessEqual(len(findings[0]["problem"]), len(failing))
        self.assertNotIn(TOKEN_SENTINEL, json.dumps(findings))

    def test_the_next_workorder_carries_the_failing_test_beside_the_timeout(self):
        """The dispatch path, not only the helper: a stored verify failure survives a later empty
        timeout in the next WorkOrder's findingsToAddress."""
        failing = "codex: worker reported failed: verify exited 1; diagnosis: failing test: exit 1: " \
                  "FAIL: test_x (m.C.test_x) AssertionError: expected 4 lines got 20"
        timeout = "codex: worker reported failed: the harness timed out"
        wi = self.foreman.store.get(ID)
        wi["attempts"] = [failing, timeout]
        self.foreman.store.put(wi)
        wi, task = self.dispatch()
        order = json.loads(task["input"].split("WorkOrder:\n", 1)[1])
        problems = [f["problem"] for f in order["findingsToAddress"]]
        self.assertEqual(problems, [failing, timeout])
        self.assertEqual(schema_errors("workorder", order), [])

    def test_the_workorders_own_payload_is_never_carried_forward(self):
        """A harness that echoes the task input must not put the WorkOrder's payload in the diagnosis."""
        diagnostics = {"elapsedMs": 100, "tail": 'task input echoed: {"schemaVersion": "factory.'
                       'workorder/v1", "kind": "implement"} then the agent crashed'}
        self.fail_with(diagnostics, error="the harness timed out")
        wi, task = self.dispatch()
        problem = json.loads(task["input"].split("WorkOrder:\n", 1)[1])["findingsToAddress"][0]["problem"]
        self.assertIn("unknown cause", problem)
        self.assertIn("then the agent crashed", problem)
        self.assertNotIn("factory.workorder/v1", problem)
        self.assertNotIn('"kind"', problem)

    def test_two_timeouts_no_longer_say_the_issue_is_too_big(self):
        """RED before #190: the old guidance read every timeout as scope only just because."""
        self.fail_with(error="the harness timed out")
        wi, task = self.dispatch()
        self.foreman.relay.finish(task["id"], status="timed_out", error="timed out")
        wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "blocked")
        self.assertNotIn("too big", wi["blockedOn"])
        self.assertIn("Fix the cause named in attempts", wi["blockedOn"])


if __name__ == "__main__":
    unittest.main()
