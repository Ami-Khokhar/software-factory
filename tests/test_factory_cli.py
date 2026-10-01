"""scripts/factory: `update` deploys a branch only when its tests pass, and follows it to main once
merged; `add-repo` asks factory-mcp, as Day Desk does; `watch` reports each escalation once;
`report` fits the week in one message; `stop` sends the relay its token."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


class NewIssueTest(unittest.TestCase):
    def new(self, *args):
        with mock.patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, \
                self.assertRaises(SystemExit):
            cli.cmd_new(["o/r", "Greet by name", "greet says hello | test_greet passes", *args])
        return run.call_args.kwargs["input"]

    def test_each_w_names_a_worker_in_a_worker_line_in_order(self):
        self.assertEqual(self.new("-w", "opencode-glm-factory", "-w", "pi-glm-factory"),
                         "Greet by name\n\nWorker: opencode-glm-factory\nWorker: pi-glm-factory\n\n"
                         "## Acceptance\n- greet says hello — done when: test_greet passes\n")

    def test_without_w_the_issue_has_no_worker_line(self):
        self.assertEqual(self.new(), "Greet by name\n\n## Acceptance\n- greet says hello — done when: test_greet passes\n")


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

    def test_a_dockerfile_update_rebuilds_the_worker_image(self):
        self.commit("worker/Dockerfile", "FROM scratch\n")
        git(self.dev, "push", "-q", "origin", "HEAD:main")
        with mock.patch.object(cli, "run") as run:
            cli.cmd_update([])
        run.assert_called_once_with(
            "docker", "build", "-q", "-t", "factory-worker:latest", str(self.deploy / "worker"))

    def test_a_failing_commit_is_not_deployed(self):
        old = self.head()
        self.commit("tests/test_ok.py", PASS.replace("True)", "False)"))
        git(self.dev, "push", "-q", "origin", "HEAD:main")
        with self.assertRaisesRegex(SystemExit, r"tests fail[\s\S]*False is not true"):  # and why
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


class WatchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(cli, "STATE", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def watch(self, *workitems, driving=False, command=None):
        """What one cron run prints, given the WorkItems factory_status returns. Each is (id, state,
        blockedOn) or a full row. `driving`: the secrets hold FACTORY_AGENT_TOKEN."""
        status = {"workItems": [w if isinstance(w, dict) else {"id": w[0], "state": w[1], "blockedOn": w[2]}
                                for w in workitems]}
        secrets = {"FACTORY_AGENT_TOKEN": "a"} if driving else {}
        printed = io.StringIO()
        with mock.patch.object(cli, "call", lambda tool, arguments=None: status), \
                mock.patch.object(cli, "read_env", lambda path: secrets), contextlib.redirect_stdout(printed):
            (command or cli.cmd_watch)([])
        return printed.getvalue()

    def test_while_day_desk_drives_the_owner_hears_only_what_it_may_not_answer_or_left_too_long(self):
        now = time.time()
        stamp = lambda ago: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - ago))
        fresh = {"id": "o/r#7", "state": "blocked", "blockedOn": "verify exited 1", "agentMayResolve": True,
                 "updatedAt": stamp(60)}
        answered = {"id": "o/r#8", "state": "human_waiting", "blockedOn": "HUMAN", "agentMayResolve": False,
                    "updatedAt": stamp(60)}
        printed = self.watch(fresh, answered, driving=True)
        self.assertNotIn("o/r#7", printed)  # Day Desk answers it first
        self.assertIn("o/r#8 is human_waiting: HUMAN", printed)  # Day Desk answered it once already
        self.assertIn("o/r#7 is blocked", self.watch(dict(fresh, updatedAt=stamp(1801)), answered, driving=True))
        self.assertIn("o/r#7 is blocked", self.watch(dict(fresh, blockedOn="new reason")))  # not driving

    def test_the_drive_queue_lists_only_what_day_desk_may_still_answer(self):
        rows = [{"id": "o/r#7", "state": "blocked", "blockedOn": "verify exited 1", "agentMayResolve": True},
                {"id": "o/r#8", "state": "human_waiting", "blockedOn": "HUMAN", "agentMayResolve": False},
                {"id": "o/r#9", "state": "implementing", "blockedOn": None}]
        self.assertEqual(self.watch(*rows, command=cli.cmd_drive_queue), "o/r#7 is blocked: verify exited 1\n")

    def test_each_escalation_is_reported_once(self):
        blocked = ("o/r#7", "blocked", "verify exited 1")
        first = self.watch(blocked, ("o/r#8", "implementing", None))
        self.assertIn("o/r#7 is blocked: verify exited 1", first)
        self.assertNotIn("o/r#8", first)
        self.assertEqual(self.watch(blocked), "")  # nothing new, so Hermes sends nothing
        second = self.watch(blocked, ("o/r#9", "human_waiting", "pr-gatekeeper decided HUMAN"))
        self.assertEqual(second.splitlines()[0], "o/r#9 is human_waiting: pr-gatekeeper decided HUMAN")
        self.assertNotIn("o/r#7", second)
        self.watch()  # the owner answered both
        self.assertIn("o/r#7 is blocked", self.watch(blocked))  # blocked again is a new escalation

    def test_it_fails_loudly_when_factory_mcp_is_down(self):
        with socket.socket() as probe:  # a port with nothing listening on it
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        with mock.patch.object(cli, "PORT", closed), \
                mock.patch.object(cli, "read_env", lambda path: {"FACTORY_MCP_TOKEN": "t"}), \
                self.assertRaisesRegex(SystemExit, "factory-mcp is not running"):
            cli.cmd_watch([])


class ReportTest(unittest.TestCase):
    NONE_ENDED = {"ended": 0, "autonomous": 0, "unknown": 0, "rate": None}
    EMPTY = {"since": "2026-09-28T03:30:00Z", "until": "2026-10-05T03:30:00Z",
             "autonomy": NONE_ENDED, "autonomyWeekBefore": NONE_ENDED, "escalated": 0,
             "topReasons": [], "newCandidates": [], "notWorking": []}

    def report(self, **numbers):
        """The lines `factory report` prints, given what factory_report returns. It reads only."""
        calls, printed = [], io.StringIO()

        def call(tool, arguments=None):
            calls.append(tool)
            return dict(self.EMPTY, **numbers)

        with mock.patch.object(cli, "call", call), contextlib.redirect_stdout(printed):
            cli.cmd_report([])
        self.assertEqual(calls, ["factory_report"])
        return printed.getvalue().splitlines()

    def test_a_week_where_nothing_ended_or_escalated_is_one_line(self):
        self.assertEqual(self.report(),
                         ["Factory week 2026-09-28 to 2026-10-05: nothing ended and nothing escalated."])

    def test_a_week_gives_both_autonomy_rates_and_the_top_reasons(self):
        lines = self.report(autonomy={"ended": 4, "autonomous": 3, "unknown": 0, "rate": 0.75},
                            escalated=3, topReasons=[{"reason": "pr-gatekeeper decided HUMAN", "count": 2},
                                                     {"reason": "verify\n exited 1", "count": 1}])
        self.assertEqual(lines, [
            "Factory week 2026-09-28 to 2026-10-05",
            "Autonomy: 3 of 4 ended WorkItems needed no person (75%). The week before: nothing ended.",
            "Went to a person: 3. Top reasons:",
            "  2x pr-gatekeeper decided HUMAN",
            "  1x verify exited 1"])

    def test_a_full_week_fits_one_telegram_message(self):
        long = "a finding that runs on\n" * 30
        lines = self.report(
            autonomy={"ended": 9, "autonomous": 2, "unknown": 1, "rate": 0.222},
            autonomyWeekBefore={"ended": 3, "autonomous": 3, "unknown": 2, "rate": 1.0},
            escalated=40, topReasons=[{"reason": long, "count": 10}] * 3,
            newCandidates=[{"repo": "o/r", "fingerprint": f"f{n}", "problem": long, "workItems": 2}
                           for n in range(12)],
            notWorking=[{"repo": "o/r", "text": long, "sent": 4, "recurred": 3}] * 12)
        self.assertLessEqual(len(lines), 20)
        self.assertLessEqual(max(len(line) for line in lines), cli.REPORT_WIDTH)
        self.assertIn("New lesson candidates: 12", lines)
        self.assertIn("Lessons not working: 12", lines)
        self.assertIn("2 of 9 ended WorkItems needed no person (22%), and 1 rebuilt from GitHub", lines[1])


class StatusTest(unittest.TestCase):
    def status(self, **summary):
        """The lines `factory status` prints, given what factory_status returns."""
        summary = dict({"workItems": [], "autonomy": {"autonomous": 0, "ended": 0}}, **summary)
        printed = io.StringIO()
        with mock.patch.object(cli, "call", lambda tool, arguments=None: summary), \
                contextlib.redirect_stdout(printed):
            cli.cmd_status([])
        return printed.getvalue().splitlines()

    def test_each_lesson_gets_a_line_and_one_that_is_not_working_is_flagged(self):
        long = "Every new endpoint carries a test for its 401 path, and its 403 path too."
        lines = self.status(lessonEffect={
            "o/r:fp1": {"repo": "o/r", "text": long, "sent": 4, "recurred": 3, "notWorking": True},
            "o/r:fp2": {"repo": "o/r", "text": "Name tests for behavior", "sent": 2, "recurred": 0,
                        "notWorking": False}})
        self.assertEqual(lines[-2:], [f"lesson [o/r] {long[:60]}: sent on 4, recurred on 3  NOT WORKING",
                                      "lesson [o/r] Name tests for behavior: sent on 2, recurred on 0"])

    def test_a_workitem_that_waits_on_another_issue_names_it(self):
        lines = self.status(workItems=[{"id": "o/r#201", "state": "ready", "waitingOn": 200},
                                       {"id": "o/r#200", "state": "implementing", "waitingOn": None}])
        self.assertTrue(lines[0].endswith("   waiting on #200"))
        self.assertNotIn("waiting", lines[1])

    def test_a_lessons_file_the_factory_cannot_read_is_named(self):
        lines = self.status(lessonsError="lessons.json is not JSON: Expecting value")
        self.assertEqual(lines[-1], "lessons: lessons.json is not JSON: Expecting value")


class Relay(BaseHTTPRequestHandler):
    """agent-relay's task API: one running task for each of two agents."""
    seen = []  # (method, path, Authorization header or None)

    def log_message(self, *args):
        pass

    def reply(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        Relay.seen.append(("GET", self.path, self.headers.get("Authorization")))
        self.reply({"tasks": [{"id": "t1", "agentId": "worker"}, {"id": "t2", "agentId": "other"}]})

    def do_DELETE(self):
        Relay.seen.append(("DELETE", self.path, self.headers.get("Authorization")))
        self.reply({"id": self.path.rsplit("/", 1)[-1], "status": "cancelled"})


class StopTest(unittest.TestCase):
    def setUp(self):
        Relay.seen = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), Relay)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config, self.token_file = Path(tmp.name), Path(tmp.name, "relay-token")
        idle = subprocess.CompletedProcess([], 1, "", "")  # no service, listener or container
        for p in (mock.patch.object(cli, "RELAY", f"http://127.0.0.1:{server.server_port}"),
                  mock.patch.object(cli, "SETTINGS", {"FACTORY_WORKERS": "worker:local"}),
                  mock.patch.object(cli, "CONFIG", self.config),
                  mock.patch.object(cli, "run", lambda *cmd, check=False: idle),
                  mock.patch.dict(os.environ, {"AGENT_RELAY_TOKEN_FILE": str(self.token_file)})):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("AGENT_RELAY_TOKEN", None)  # the patch restores it

    def stop(self):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            cli.cmd_stop([])
        return printed.getvalue()

    def test_stop_sends_the_relay_token_from_its_file(self):
        self.token_file.write_text("file-token\n")
        self.assertIn("cancelled worker task t1", self.stop())
        self.assertEqual(Relay.seen, [("GET", "/v1/tasks?status=running", "Bearer file-token"),
                                      ("DELETE", "/v1/tasks/t1", "Bearer file-token")])

    def test_stop_sends_the_token_factory_serve_gives_factory_mcp(self):
        (self.config / "secrets").write_text("AGENT_RELAY_TOKEN=secret-token\n")
        self.token_file.write_text("file-token\n")
        printed = self.stop()
        self.assertEqual({auth for _, _, auth in Relay.seen}, {"Bearer secret-token"})
        self.assertNotIn("secret-token", printed)


if __name__ == "__main__":
    unittest.main()
