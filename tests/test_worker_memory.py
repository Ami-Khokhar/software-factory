"""Workers get bounded snapshots and use a pass-scoped HTTP reader."""
import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from factory_mcp.mcp import ToolError
from factory_mcp.server import make_server
from tests import test_worker_ask as ask
from tests.test_actions import schema_errors


class WorkerMemoryTest(unittest.TestCase):
    def setUp(self):
        ask.AskTest.setUp(self)

    def tearDown(self):
        self.store.close()

    def remember(self, key="style", **over):
        return self.store.knowledge.put(self.config, {"repo": ask.REPO, "key": key,
            "kind": "preference", "status": "approved", "text": "Use concise explanations",
            "source": "owner:message", **over}, "2026-10-28T00:00:00Z")

    def test_dispatch_saves_versions_and_only_approved_relevant_context(self):
        preferred = self.remember()
        self.remember("maybe", status="candidate", text="Unconfirmed opinion")
        self.remember("unrelated", kind="learning", text="Unrelated astronomy observation")
        order = ask.AskTest.start(self)
        self.assertEqual(schema_errors("workorder", order), [])
        self.assertEqual([d["id"] for d in order["memory"]], [preferred["id"]])
        self.remember(version=1, status="retired", text="Superseded advice")
        saved = self.store.workorders(ask.ID)[-1]
        self.assertEqual(saved["memory"][0]["version"], 1)
        self.assertEqual(saved["memory"][0]["text"], "Use concise explanations")

    def test_packet_has_count_and_character_budgets(self):
        for n in range(12):
            self.remember(str(n), text="x" * 1000)
        order = ask.AskTest.start(self)
        self.assertLessEqual(len(order["memory"]), 6)
        self.assertLessEqual(sum(len(json.dumps(d)) for d in order["memory"]), 4000)

    def test_worker_cannot_select_another_scope_or_use_an_ended_pass(self):
        self.remember()
        order = ask.AskTest.start(self)
        token = order["ask"]["token"]
        for args in ({"repo": "other/private"}, {"projectId": "other"}, {"version": 1}):
            with self.subTest(args=args), self.assertRaises(ToolError):
                self.foreman.worker_memory(token, args)
        result = self.foreman.worker_memory(token, {"query": "concise"})
        history = self.store.history(ask.ID)
        audit = next(h for h in history if h["action"] == "memory_read")
        self.assertEqual(audit["detail"]["memories"][0]["id"], result["memories"][0]["id"])
        self.assertNotIn("text", json.dumps(audit))
        self.store.put(dict(self.store.get(ask.ID), state="reviewing"))
        with self.assertRaises(ToolError):
            self.foreman.worker_memory(token, {})

    def test_installed_helper_reads_through_real_http_and_bad_tokens_are_refused(self):
        note = self.remember()
        order = ask.AskTest.start(self)
        server = make_server(self.config, self.store, self.github, self.relay)
        server.foreman = self.foreman
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}/worker/"
        try:
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
        finally:
            server.shutdown()
            server.server_close()
