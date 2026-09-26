"""scripts/factory: `update` deploys a branch only when its tests pass, and follows it to main once
merged; `add-repo` asks factory-mcp, as Day Desk does; `watch` reports each escalation once; `stop`
sends the relay its token."""
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

        def call(tool, arguments=None):
            calls.append(tool)
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
        self.assertEqual(self.add_repo(ready=True), (["factory_repo_check", "factory_add_repo"], None))

    def test_a_failing_check_stops_it_before_the_repo_is_enabled(self):
        calls, stopped = self.add_repo(ready=False)
        self.assertEqual(calls, ["factory_repo_check"])
        self.assertIn("o/r is not ready", stopped)


class WatchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(cli, "STATE", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def watch(self, *workitems):
        """What one cron run prints, given the WorkItems factory_status returns."""
        status = {"workItems": [{"id": i, "state": s, "blockedOn": b} for i, s, b in workitems]}
        printed = io.StringIO()
        with mock.patch.object(cli, "call", lambda tool, arguments=None: status), \
                contextlib.redirect_stdout(printed):
            cli.cmd_watch([])
        return printed.getvalue()

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
