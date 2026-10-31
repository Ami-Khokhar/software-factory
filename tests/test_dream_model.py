"""Reflection validates real evidence, bounds provider calls and never approves inferred memory."""
import dataclasses
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from factory_mcp import dream_model
from factory_mcp.mcp import ToolError
from tests import test_dreaming as base


class DreamModelTest(unittest.TestCase):
    def setUp(self):
        base.DreamingTest.setUp(self)
        self.config = dataclasses.replace(self.config, dream_url="https://model.example/chat", dream_model="small")
        self.store.log("attempt_failed", {"cause": "failing test", "reason": "verify failed"}, "o/r#1")

    def tearDown(self):
        base.DreamingTest.tearDown(self)

    def plan(self):
        return {"improvements": [{"repo": "o/r", "cause": "Runner loses verification details",
            "summary": "Preserve the failing test name", "paths": ["worker/runner.py"],
            "checks": [{"claim": "Failure names the test", "command": "python3 -m unittest tests.test_worker_runner"}],
            "evidence": ["o/r@1"]}], "learnings": [{"repo": "o/r", "kind": "preference",
            "text": "Prefer compact failure summaries", "evidence": ["o/r@1"]}]}

    def test_one_reflection_persists_plan_and_tentative_learning_then_cools_down(self):
        with mock.patch.object(dream_model, "completion", return_value=self.plan()) as model:
            result = dream_model.reflect(self.store, self.config, base.AT)
            again = dream_model.reflect(self.store, self.config, base.AT)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result["status"], "planned")
        self.assertEqual(again["reason"], "cooldown")
        memories = self.store.knowledge.search(self.config, {"repo": "o/r"})["memories"]
        self.assertEqual(memories[0]["status"], "candidate")
        self.assertIn("o/r@1", memories[0]["source"])
        self.assertEqual(self.store.knowledge.search(self.config, {"repo": "o/r", "approvedOnly": True})["memories"], [])
        self.assertIn("fingerprint", self.store.dreaming.latest()["result"]["improvements"][0])

    def test_invalid_grounding_or_provider_failure_retains_cooldown_and_no_memory(self):
        invalid = self.plan()
        invalid["improvements"][0]["evidence"] = ["other/private@1"]
        with mock.patch.object(dream_model, "completion", return_value=invalid):
            with self.assertRaises(ToolError): dream_model.reflect(self.store, self.config, base.AT)
        self.assertEqual(self.store.dreaming.latest()["status"], "failed")
        self.assertEqual(self.store.knowledge.search(self.config, {"repo": "o/r"})["memories"], [])
        with mock.patch.object(dream_model, "completion") as model:
            dream_model.reflect(self.store, self.config, base.AT)
            model.assert_not_called()

    def test_missing_configuration_skips_without_claim(self):
        config = dataclasses.replace(self.config, dream_url="")
        self.assertEqual(dream_model.reflect(self.store, config, base.AT)["status"], "not_configured")
        self.assertIsNone(self.store.dreaming.latest())

    def test_evidence_graph_links_are_real_and_sensitive_diagnostics_are_redacted(self):
        self.store.log("attempt_failed", {"reason": "sk-" + "a1B2" * 8}, "o/r#1")
        self.store.log("attempt_failed", {"reason": "Bearer private-value"}, "o/r#1")
        with mock.patch.object(dream_model, "completion", return_value=self.plan()):
            dream_model.reflect(self.store, self.config, base.AT)
        docs = self.store.knowledge.search(self.config, {"repo": "o/r", "limit": 20})["memories"]
        self.assertNotIn("private-value", json.dumps(docs))
        self.assertNotIn("a1B2", json.dumps(docs))
        pref = next(doc for doc in docs if doc["kind"] == "preference")
        linked = self.store.knowledge.search(self.config, {"repo": "o/r", "id": pref["links"][0]["id"]})
        self.assertEqual(linked["memories"][0]["kind"], "evidence")

    def test_candidate_bounds_paths_and_credentials_are_refused(self):
        evidence, _ = self.store.dreaming.collect(self.config)
        for change in ({"paths": ["../secret"]}, {"summary": "sk-" + "a1B2" * 8},
                       {"checks": []}, {"paths": [".git/config"]}, {"extra": "field"}):
            plan = self.plan()
            plan["improvements"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ToolError):
                dream_model.validate(plan, evidence)

    def test_a_documentation_proposal_validates_with_its_own_check(self):
        evidence, _ = self.store.dreaming.collect(self.config)
        plan = self.plan()
        plan["improvements"][0].update(
            cause="Workers re-learn the same instruction", summary="Say it once in docs/trouble.md",
            paths=["docs/trouble.md"],
            checks=[{"claim": "docs/trouble.md names the rule",
                     "command": "grep -c 'report the failure once' docs/trouble.md"}])
        saved = dream_model.validate(plan, evidence)
        self.assertEqual(saved["improvements"][0]["paths"], ["docs/trouble.md"])
        # The prompt is what allows reflection to propose a documentation change at all.
        self.assertIn(".md", dream_model.PROMPT)
        self.assertIn("documentation", dream_model.PROMPT)

    def test_real_http_client_sets_output_budget_and_reads_structured_response(self):
        received = []
        plan = self.plan()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
                data = json.dumps({"choices": [{"message": {"content": json.dumps(plan)}}]}).encode()
                self.send_response(200)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            config = dataclasses.replace(self.config, dream_url=f"http://127.0.0.1:{server.server_port}/chat")
            self.assertEqual(dream_model.completion(config, [{"sourceId": "o/r@1"}]), plan)
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]["max_tokens"], 2048)
            with self.assertRaises(ToolError):
                dream_model.completion(dataclasses.replace(config, dream_url="http://external.example/chat"), [])
        finally:
            server.shutdown()
            server.server_close()
