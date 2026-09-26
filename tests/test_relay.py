"""factory_mcp.relay against a local HTTP server standing in for agent-relay's task API."""
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from factory_mcp.relay import Relay, RelayError


class Api(BaseHTTPRequestHandler):
    bodies, tasks, auth = [], {}, []  # auth: each request's Authorization header, or None

    def log_message(self, *args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        Api.auth.append(self.headers.get("Authorization"))
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        Api.bodies.append((self.path, body))
        if body["agentId"] == "broken":
            return self.reply(500, {"error": "agent crashed"})
        Api.tasks["t1"] = {"id": "t1", "agentId": body["agentId"], "status": "queued"}
        return self.reply(202, Api.tasks["t1"])

    def do_GET(self):
        Api.auth.append(self.headers.get("Authorization"))
        task = Api.tasks.get(self.path.rsplit("/", 1)[-1])
        return self.reply(200, task) if task else self.reply(404, {"error": "unknown task"})


class RelayClientTest(unittest.TestCase):
    def setUp(self):
        Api.bodies, Api.tasks, Api.auth = [], {}, []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.token_file = Path(tmp.name, "token")  # absent until a test writes it
        env = mock.patch.dict(os.environ, {"AGENT_RELAY_TOKEN_FILE": str(self.token_file)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AGENT_RELAY_TOKEN", None)  # the patch restores it
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Api)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.relay = Relay(f"http://127.0.0.1:{self.server.server_port}")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_submit_sends_the_task_with_its_checkout(self):
        task = self.relay.submit("pi", "the WorkOrder", "o/r#7:r0:implement:a0", 60000, cwd="/work/o/r")
        self.assertEqual(task["id"], "t1")
        self.assertEqual(Api.bodies[-1], ("/v1/tasks", {
            "agentId": "pi", "input": "the WorkOrder", "requestId": "o/r#7:r0:implement:a0",
            "timeoutMs": 60000, "cwd": "/work/o/r"}))
        self.relay.submit("pi", "the WorkOrder", "o/r#7:r0:implement:a1", 60000)
        self.assertNotIn("cwd", Api.bodies[-1][1])  # no checkout root: the agent's own cwd

    def test_a_task_the_relay_forgot_is_none(self):
        """The Foreman counts a None as an attempt lost to a relay restart."""
        self.relay.submit("pi", "x", "k", 1000)
        self.assertEqual(self.relay.get("t1")["status"], "queued")
        self.assertIsNone(self.relay.get("t404"))

    def test_failures_are_relay_errors(self):
        with self.assertRaisesRegex(RelayError, "relay 500: .*agent crashed"):
            self.relay.submit("broken", "x", "k", 1000)
        with socket.socket() as probe:  # a port with nothing listening on it
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        with self.assertRaisesRegex(RelayError, "relay unreachable"):
            Relay(f"http://127.0.0.1:{closed}").get("t1")

    def submit_and_get(self):
        """The Authorization header of one submit and one get."""
        del Api.auth[:]
        self.relay.submit("pi", "x", "k", 1000)
        self.relay.get("t1")
        return Api.auth

    def test_the_token_in_AGENT_RELAY_TOKEN_is_sent_on_submit_and_get(self):
        os.environ["AGENT_RELAY_TOKEN"] = "env-token"
        self.token_file.write_text("file-token\n")  # the variable wins, as it does in the relay
        self.assertEqual(self.submit_and_get(), ["Bearer env-token"] * 2)

    def test_a_token_file_written_after_the_client_was_made_is_sent(self):
        """launchd starts the relay and factory-mcp together; the relay writes the file on start."""
        self.assertEqual(self.submit_and_get(), [None, None])
        self.token_file.write_text("file-token\n")
        self.assertEqual(self.submit_and_get(), ["Bearer file-token"] * 2)

    def test_no_token_and_no_token_file_sends_no_header(self):
        """An older relay takes no token and keeps working."""
        self.assertEqual(self.submit_and_get(), [None, None])


if __name__ == "__main__":
    unittest.main()
