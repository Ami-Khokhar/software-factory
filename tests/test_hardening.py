"""Failure modes found in the dress rehearsal and review (scripts/rehearsal.py)."""
import http.client
import threading
import unittest

from factory_mcp import mcp
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.server import make_server
from factory_mcp.store import Store
from tests.fakes import FakeGitHub, FakeRelay, result_line

REPO, ID = "o/r", "o/r#7"


class ForemanHardeningTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.github.add_issue(REPO, 7)
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO})), self.relay)
        self.foreman.intake(REPO, 7)

    def tick(self):
        return self.foreman.tick(ID)["workItem"]

    def test_silent_gate_goes_to_a_person(self):
        wi = self.tick()
        self.github.push(REPO, "factory/7", "c1")
        self.relay.finish(wi["providerTaskId"], output=result_line(wi["activeWorkOrderId"], "c1"))
        self.assertEqual(self.tick()["state"], "pr_open")
        self.assertEqual(self.tick()["state"], "pr_open")  # within the wait: nothing happens
        self.foreman.config = Config(mcp_token="t", repos=frozenset({REPO}), gate_wait_seconds=-1)
        wi = self.tick()
        self.assertEqual(wi["state"], "human_waiting")
        self.assertIn("no pr-gatekeeper verdict on c1", wi["blockedOn"])

    def test_truncated_relay_output_is_named(self):
        wi = self.tick()
        self.relay.tasks[wi["providerTaskId"]]["outputTruncated"] = True
        self.relay.finish(wi["providerTaskId"], output="thousands of log lines")
        self.assertIn("AGENT_RELAY_MAX_OUTPUT_BYTES", self.tick()["attempts"][0])

    def test_one_broken_workitem_does_not_stop_the_rest(self):
        self.github.add_issue(REPO, 8)
        self.foreman.intake(REPO, 8)
        real = self.github.issue

        def issue(repo, number):
            if number == 7:
                raise KeyError("unexpected payload")
            return real(repo, number)

        self.github.issue = issue
        result = self.foreman.tick()
        self.assertEqual([t["id"] for t in result["ticked"]], ["o/r#8"])
        self.assertEqual(self.foreman.store.history(ID)[-1]["action"], "tick_error")


class ProtocolHardeningTest(unittest.TestCase):
    def test_params_must_be_an_object(self):
        reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1]}, {})
        self.assertEqual(reply["error"]["code"], -32602)

    def test_bad_content_length_gets_an_answer(self):
        server = make_server(Config(mcp_token="t", repos=frozenset(), port=0), Store(":memory:"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.putrequest("POST", "/mcp")
            connection.putheader("Authorization", "Bearer t")
            connection.putheader("content-length", "abc")
            connection.endheaders()
            self.assertEqual(connection.getresponse().status, 400)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
