"""Remote verify (ADR-0045): config, the WorkOrder flag, the GitHub client's two new calls against a
local HTTP server, and the Foreman's decisions at each step of a run on a GitHub macOS runner."""
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from factory_mcp import documents
from factory_mcp.config import Config
from factory_mcp.foreman import REMOTE_VERIFY_WORKFLOW, Foreman
from factory_mcp.github import GitHub
from factory_mcp.store import Store
from tests.fakes import FakeGit, FakeGitHub, FakeRelay, result_line, workitem

REPO, ID = "o/r", "o/r#1"
ENV = {"FACTORY_MCP_TOKEN": "t", "FACTORY_REPOS": REPO}
NO_TESTS = ('"evidence": [{"kind": "test", "command": "python3 -m unittest", "exit": 0, "tail": "OK"}]', '"evidence": []')


class ConfigTest(unittest.TestCase):
    def test_the_list_and_the_wait_are_read_like_the_other_settings(self):
        config = Config.from_env(dict(ENV, FACTORY_REMOTE_VERIFY_REPOS="Ami-Khokhar/ideadump, o/r",
                                      FACTORY_REMOTE_VERIFY_SECONDS="60"))
        self.assertTrue(config.remote_verify("ami-khokhar/IDEADUMP"))
        self.assertFalse(config.remote_verify("o/other"))
        self.assertEqual(config.remote_verify_seconds, 60)
        self.assertFalse(Config.from_env(ENV).remote_verify(REPO))

    def test_a_wait_below_one_second_is_refused(self):
        with self.assertRaisesRegex(ValueError, "FACTORY_REMOTE_VERIFY_SECONDS must be 1 or more"):
            Config.from_env(dict(ENV, FACTORY_REMOTE_VERIFY_SECONDS="0"))


class WorkOrderTest(unittest.TestCase):
    def order(self, **config):
        return documents.workorder(workitem(), "implement", 0,
                                   Config(mcp_token="t", repos=frozenset({REPO}), **config), "base0")

    def test_only_a_remote_verify_repo_gets_the_flag(self):
        self.assertNotIn("remoteVerify", self.order())
        self.assertTrue(self.order(remote_verify_repos=frozenset({REPO}))["remoteVerify"])

    def test_a_remote_verify_result_needs_no_test_evidence_from_the_worker(self):
        result = json.loads(result_line("o/r#1:r0:implement:a0").splitlines()[-1])
        result["evidence"] = []
        flagged = dict(self.order(remote_verify_repos=frozenset({REPO})), id=result["workOrderId"])
        self.assertEqual(documents.check_result(result, flagged), [])
        self.assertEqual(documents.check_result(result, dict(self.order(), id=result["workOrderId"])),
                         ["required evidence tests-pass: no passing test entry"])


class LogApi(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        LogApi.seen.append((self.path, self.headers.get("Authorization")))
        port = self.server.server_port
        if self.path.endswith("/runs/9/jobs"):
            self.send(200, json.dumps({"jobs": [{"id": 1, "conclusion": "success"},
                                                {"id": 2, "conclusion": "failure"}]}))
        elif self.path.endswith("/jobs/2/logs"):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{port}/blob/log.txt")
            self.end_headers()
        elif self.path == "/blob/log.txt":
            self.send(200, "x" * 50 + "error: Cannot find 'Foo' in scope")
        else:
            self.send(404, "{}")

    def do_DELETE(self):
        LogApi.seen.append((self.path, "DELETE"))
        self.send(204, "")

    def send(self, status, text):
        self.send_response(status)
        self.end_headers()
        self.wfile.write(text.encode())


class GitHubClientTest(unittest.TestCase):
    def setUp(self):
        LogApi.seen = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), LogApi)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.github = GitHub("secret", f"http://127.0.0.1:{server.server_port}")

    def test_the_log_tail_is_the_failed_jobs_and_the_signed_url_gets_no_token(self):
        self.assertEqual(self.github.run_log_tail("o/r", 9, 33), "error: Cannot find 'Foo' in scope"[-33:])
        by_path = dict(LogApi.seen)
        self.assertEqual(by_path["/repos/o/r/actions/jobs/2/logs"], "Bearer secret")
        self.assertIsNone(by_path["/blob/log.txt"])

    def test_a_log_that_cannot_be_read_is_said_not_raised(self):
        self.assertIn("could not be read", self.github.run_log_tail("o/r", 404))

    def test_a_branch_is_deleted_by_its_ref(self):
        self.github.delete_branch("o/r", "factory-verify/1-0")
        self.assertIn(("/repos/o/r/git/refs/heads/factory-verify/1-0", "DELETE"), LogApi.seen)


class ForemanTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.github.add_issue(REPO, 1)
        self.github.contents[(REPO, REMOTE_VERIFY_WORKFLOW)] = "name: factory-verify\n"
        self.git = FakeGit(self.github).install(self)
        config = Config(mcp_token="t", repos=frozenset({REPO}), remote_verify_repos=frozenset({REPO}),
                        remote_verify_seconds=600)
        self.foreman = Foreman(Store(":memory:"), self.github, config, self.relay)
        self.foreman.intake(REPO, 1)

    def finish(self):
        """The worker's pass ends; the tick that reads it. Returns the WorkItem."""
        wi = self.foreman.tick(ID)["workItem"]
        self.assertIn('"remoteVerify": true', self.relay.tasks[wi["providerTaskId"]]["input"])
        self.relay.finish(wi["providerTaskId"], output=result_line(wi["activeWorkOrderId"]).replace(*NO_TESTS))
        return self.foreman.tick(ID)["workItem"]

    def run_ends(self, conclusion, sha="c1"):
        self.github.workflow_run_list[(REPO, sha)] = [{"name": "factory-verify", "id": 9, "status": "completed",
                                                       "conclusion": conclusion, "html_url": "https://gh/run/9"}]

    def test_the_patch_goes_to_a_scratch_branch_and_the_item_waits_for_the_run(self):
        wi = self.finish()
        self.assertEqual((wi["state"], wi["providerTaskId"] is not None), ("implementing", True))
        self.assertEqual(wi["remoteVerify"]["branch"], "factory-verify/1-1")
        self.assertEqual([p["branch"] for p in self.git.pushes], ["factory-verify/1-1"])
        self.assertTrue(self.git.pushes[0]["force"])
        self.assertEqual(self.foreman.tick(ID)["workItem"], wi)  # no run yet: nothing changes, no new push
        self.assertEqual(len(self.git.pushes), 1)

    def test_a_pass_deletes_the_scratch_branch_then_pushes_the_branch_and_opens_the_pr(self):
        self.finish()
        self.run_ends("success")
        wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual((wi["state"], wi["remoteVerify"]), ("pr_open", None))
        self.assertEqual(self.github.deleted, [(REPO, "factory-verify/1-1")])
        self.assertEqual([p["branch"] for p in self.git.pushes], ["factory-verify/1-1", "factory/1"])
        self.assertIn("factory-verify workflow (GitHub macOS runner)\n(exit 0)", self.github.pulls[0]["body"])

    def test_a_failure_is_an_attempt_with_the_log_tail_and_the_next_order_carries_it(self):
        self.finish()
        self.run_ends("failure")
        self.github.logs[9] = "...\nerror: Cannot find 'Foo' in scope\n** TEST FAILED **"
        wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual((wi["state"], wi["remoteVerify"], len(self.git.pushes)), ("ready", None, 1))
        self.assertIn("remote verify failure: ... error: Cannot find 'Foo' in scope ** TEST FAILED **", wi["attempts"][0])
        self.assertEqual(self.github.deleted, [(REPO, "factory-verify/1-1")])
        wi = self.foreman.tick(ID)["workItem"]
        order = json.loads(self.relay.tasks[wi["providerTaskId"]]["input"].split("WorkOrder:\n", 1)[1])
        self.assertIn("Cannot find 'Foo'", order["findingsToAddress"][0]["problem"])

    def test_failed_runs_count_against_the_pass_budget(self):
        self.finish()
        self.run_ends("failure")
        self.foreman.tick(ID)
        wi = self.foreman.tick(ID)["workItem"]  # the second pass
        self.relay.finish(wi["providerTaskId"], output=result_line(wi["activeWorkOrderId"]).replace(*NO_TESTS))
        self.foreman.tick(ID)  # its scratch branch
        self.run_ends("failure", "c1")
        self.assertEqual(self.foreman.tick(ID)["workItem"]["state"], "blocked")

    def test_a_run_that_never_finishes_fails_the_attempt_after_the_wait(self):
        self.finish()
        with mock.patch.object(time, "time", return_value=time.time() + 601):
            wi = self.foreman.tick(ID)["workItem"]
        self.assertIn("no finished run of factory-verify", wi["attempts"][0])
        self.assertEqual(self.github.deleted, [(REPO, "factory-verify/1-1")])

    def test_a_repo_without_the_workflow_waits_for_the_owner_and_pushes_nothing(self):
        del self.github.contents[(REPO, REMOTE_VERIFY_WORKFLOW)]
        wi = self.finish()
        self.assertEqual((wi["state"], wi["attempts"], self.git.pushes), ("human_waiting", [], []))
        self.assertIn("docs/RUNBOOK.md", wi["blockedOn"])

    def test_another_repo_is_verified_by_its_worker_as_before(self):
        other = Foreman(Store(":memory:"), self.github, Config(mcp_token="t", repos=frozenset({REPO})), self.relay)
        other.intake(REPO, 1)
        wi = other.tick(ID)["workItem"]
        self.relay.finish(wi["providerTaskId"], output=result_line(wi["activeWorkOrderId"]))
        self.assertEqual(other.tick(ID)["workItem"]["state"], "pr_open")
        self.assertEqual([p["branch"] for p in self.git.pushes], ["factory/1"])
