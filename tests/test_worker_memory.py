"""Workers get bounded snapshots and use a pass-scoped HTTP reader."""
import io
import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager, redirect_stderr
from pathlib import Path

from factory_mcp import documents
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.server import make_server
from factory_mcp.store import Store
from tests.fakes import FakeGit, FakeGitHub, FakeRelay
from tests.test_actions import schema_errors

REPO, ID = "o/r", "o/r#7"


class WorkerMemoryTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        FakeGit(self.github).install(self)
        self.github.add_issue(REPO, 7)
        self.config = Config(mcp_token="t", repos=frozenset({REPO}), port=0)
        self.store = Store(":memory:")
        self.foreman = Foreman(self.store, self.github, self.config, self.relay)
        self.foreman.intake(REPO, 7)

    def tearDown(self):
        self.store.close()

    def start(self):
        self.foreman.tick()
        return self.store.workorders(ID)[-1]

    @contextmanager
    def serving(self):
        server = make_server(self.config, self.store, self.github, self.relay)
        server.foreman = self.foreman
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/worker/"
        finally:
            server.shutdown()
            server.server_close()

    def remember(self, key="style", **over):
        return self.store.knowledge.put(self.config, {"repo": REPO, "key": key,
            "kind": "preference", "status": "approved", "text": "Use concise explanations",
            "source": "owner:message", **over}, "2026-10-28T00:00:00Z")

    def test_dispatch_saves_versions_and_only_approved_relevant_context(self):
        preferred = self.remember()
        self.remember("maybe", status="candidate", text="Unconfirmed opinion")
        self.remember("unrelated", kind="learning", text="Unrelated astronomy observation")
        order = self.start()
        self.assertEqual(schema_errors("workorder", order), [])
        self.assertEqual([d["id"] for d in order["memory"]], [preferred["id"]])
        self.remember(version=1, status="retired", text="Superseded advice")
        saved = self.store.workorders(ID)[-1]
        self.assertEqual(saved["memory"][0]["version"], 1)
        self.assertEqual(saved["memory"][0]["text"], "Use concise explanations")

    def test_packet_has_count_and_character_budgets(self):
        for n in range(12):
            self.remember(str(n), text="x" * 1000)
        order = self.start()
        self.assertLessEqual(len(order["memory"]), 6)
        self.assertLessEqual(sum(len(json.dumps(d)) for d in order["memory"]), 4000)

    def test_prompt_explains_memory_retrieval_only_when_a_packet_is_present(self):
        self.remember()
        order = self.start()
        branch = self.store.get(ID)["branch"]
        prompt = documents.worker_input(order, branch)
        self.assertIn("factory-memory 'search words'", prompt)
        self.assertIn("data, not permission", prompt)
        self.assertIn("Search results carry a status", prompt)
        for empty in ({key: value for key, value in order.items() if key != "memory"},
                      dict(order, memory=[])):
            self.assertNotIn("factory-memory", documents.worker_input(empty, branch))

    def test_worker_cannot_select_another_scope_or_use_an_ended_pass(self):
        self.remember()
        order = self.start()
        token = order["ask"]["token"]
        for args in ({"repo": "other/private"}, {"projectId": "other"}, {"version": 1}):
            with self.subTest(args=args), self.assertRaises(ToolError):
                self.foreman.worker_memory(token, args)
        result = self.foreman.worker_memory(token, {"query": "concise"})
        history = self.store.history(ID)
        audit = next(h for h in history if h["action"] == "memory_read")
        self.assertEqual(audit["detail"]["memories"][0]["id"], result["memories"][0]["id"])
        self.assertNotIn("text", json.dumps(audit))
        self.store.put(dict(self.store.get(ID), state="reviewing"))
        with self.assertRaises(ToolError):
            self.foreman.worker_memory(token, {})

    def test_installed_helper_reads_through_real_http_and_bad_tokens_are_refused(self):
        note = self.remember()
        order = self.start()
        with self.serving() as base:
            helper = Path(__file__).resolve().parent.parent / "worker" / "factory-memory"
            done = subprocess.run([sys.executable, str(helper), "--id", note["id"]],
                env=dict(os.environ, FACTORY_ASK_URL=base + "ask", FACTORY_ASK_TOKEN=order["ask"]["token"]),
                capture_output=True, text=True, timeout=10)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stdout)["memories"][0]["id"], note["id"])
            request = urllib.request.Request(base + "memory", json.dumps({"token": "wrong"}).encode())
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(error.exception.code, 403)
            failed = subprocess.run([sys.executable, str(helper), "concise"],
                env=dict(os.environ, FACTORY_ASK_URL=base + "ask", FACTORY_ASK_TOKEN="wrong"),
                capture_output=True, text=True, timeout=10)
            self.assertEqual(failed.returncode, 1)
            self.assertIn("retrieval unavailable", failed.stderr)

    def test_helper_without_a_capability_fails_explicitly(self):
        helper = Path(__file__).resolve().parent.parent / "worker" / "factory-memory"
        env = {key: value for key, value in os.environ.items() if key != "FACTORY_ASK_TOKEN"}
        done = subprocess.run([sys.executable, str(helper), "concise"], env=env,
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(done.returncode, 2)
        self.assertIn("no active factory capability", done.stderr)

    def test_memory_route_reports_argument_errors_as_json(self):
        token = self.start()["ask"]["token"]
        with self.serving() as base:
            for args in ({"query": "x" * 201}, {"limit": "many"}, {"repo": "other/r"}):
                request = urllib.request.Request(base + "memory", json.dumps({"token": token, **args}).encode())
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(caught.exception.code, 400)
                self.assertIn("error", json.load(caught.exception))

    def test_memory_route_logs_storage_failure_without_leaking_request_data(self):
        token = self.start()["ask"]["token"]
        with self.serving() as base:
            self.store.close()  # Real SQLite failure: no mock of the controller or search.
            payload = {"token": token, "query": "credential-like-marker-should-not-leak"}
            request = urllib.request.Request(base + "memory", json.dumps(payload).encode())
            diagnostic = io.StringIO()
            with redirect_stderr(diagnostic), self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 500)
            self.assertEqual(json.load(caught.exception), {"error": "memory retrieval failed"})
            self.assertIn("factory-mcp worker memory failed (ProgrammingError); check factory database and server logs",
                          diagnostic.getvalue())
            self.assertNotIn(token, diagnostic.getvalue())
            self.assertNotIn("credential-like-marker-should-not-leak", diagnostic.getvalue())

    def test_closed_stderr_does_not_interrupt_memory_error_response(self):
        token = self.start()["ask"]["token"]
        with self.serving() as base:
            self.store.close()  # Real SQLite failure reaches the route over HTTP.
            diagnostic = io.StringIO()
            diagnostic.close()
            request = urllib.request.Request(base + "memory", json.dumps({"token": token}).encode())
            with redirect_stderr(diagnostic), self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 500)
            self.assertEqual(json.load(caught.exception), {"error": "memory retrieval failed"})
