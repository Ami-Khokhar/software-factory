"""Host recovery through public MCP tools. Only subprocess and the clock are faked."""
import json
import subprocess
import threading
import unittest
from unittest import mock

from factory_mcp import mcp, tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.store import Store
from fakes import FakeGitHub, workitem


class DockerControlTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.foreman = Foreman(self.store, FakeGitHub(), Config(mcp_token="t", repos=frozenset({"o/r"})))
        self.tools = tools.build(self.store, self.foreman, agent=True)
        self.calls = []
        self.running = False
        self.containers = ""
        self.fail = None
        self.starts = True
        def run(argv, **kwargs):
            self.calls.append(tuple(argv))
            code, out = 0, ""
            if argv == ("docker", "info"):
                code = 0 if self.running else 1
            elif argv == ("docker", "ps", "-q"):
                out = self.containers
            elif argv == ("orb", "start") and self.starts:
                self.running = True
            elif argv == ("orb", "stop"):
                self.running = False
            if tuple(argv) == self.fail:
                code = 1
            return subprocess.CompletedProcess(argv, code, out, "")
        patch = mock.patch("factory_mcp.docker.subprocess.run", run)
        patch.start()
        self.addCleanup(patch.stop)

    def call(self, name, args=None):
        reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": args or {}}}, self.tools)["result"]
        return reply["isError"], json.loads(reply["content"][0]["text"])

    def control(self, action):
        return self.call("factory_docker_control", {"action": action})

    def test_start_recovers_a_stopped_engine_and_checks_readiness(self):
        self.assertEqual(self.control("start"), (False, {"running": True, "changed": True}))
        self.assertEqual(self.calls, [("docker", "info"), ("orb", "start"), ("docker", "info")])
        self.assertEqual(self.control("start"), (False, {"running": True, "changed": False}))

    def test_status_reads_without_starting_the_engine(self):
        self.assertEqual(self.call("factory_docker_status"), (False, {"running": False}))
        self.assertEqual(self.calls, [("docker", "info")])

    def test_stop_refuses_queued_or_running_factory_work(self):
        for state in ("ready", "changes_requested", "dispatching", "implementing"):
            self.store.put(workitem(id=f"o/r#{state}", state=state))
            self.assertTrue(self.control("stop")[0])
        self.assertEqual(self.calls, [])

    def test_stop_refuses_any_running_container(self):
        self.running, self.containers = True, "daydesk-or-another-app\n"
        error, result = self.control("stop")
        self.assertTrue(error)
        self.assertIn("containers are running", result["error"])
        self.assertNotIn(("orb", "stop"), self.calls)

    def test_stop_requires_a_successful_container_inventory(self):
        self.running, self.fail = True, ("docker", "ps", "-q")
        self.assertTrue(self.control("stop")[0])
        self.assertNotIn(("orb", "stop"), self.calls)

    def test_idle_stop_verifies_the_engine_is_down(self):
        self.running = True
        self.assertEqual(self.control("stop"), (False, {"running": False, "changed": True}))
        self.assertEqual(self.calls[-2:], [("orb", "stop"), ("docker", "info")])

    def test_a_start_that_never_becomes_ready_fails(self):
        self.starts = False
        with mock.patch("factory_mcp.docker.time.monotonic", side_effect=[0, 1, 121]), \
                mock.patch("factory_mcp.docker.time.sleep"):
            error, result = self.control("start")
        self.assertTrue(error)
        self.assertIn("did not finish start", result["error"])

    def test_stop_waits_for_the_dispatch_lock_before_reading_state(self):
        self.foreman.runtime_lock.acquire()
        done = threading.Event()
        def stop():
            self.control("stop")
            done.set()
        thread = threading.Thread(target=stop)
        thread.start()
        try:
            self.assertFalse(done.wait(0.05))
            self.assertEqual(self.calls, [])
        finally:
            self.foreman.runtime_lock.release()
            thread.join(2)
        self.assertTrue(done.is_set())
