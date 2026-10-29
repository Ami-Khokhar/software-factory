"""A blocker becomes a bounded repair, passes the gate, and yields retrievable measured memory."""
import dataclasses
import unittest
from unittest import mock

from factory_mcp import dream_model, improvements, outcomes, tools
from tests import test_worker_ask as worker
from tests.test_dreaming import AT
from tests.fakes import result_line


class DreamLoopTest(unittest.TestCase):
    def setUp(self):
        worker.AskTest.setUp(self)
        self.foreman.config = dataclasses.replace(self.config, dream_url="https://model.example/chat",
            dream_model="small", dream_auto_repos=frozenset({worker.REPO}))
        self.config = self.foreman.config

    def tearDown(self):
        self.store.close()

    def test_blocker_to_gate_merge_to_measured_context(self):
        self.store.log("attempt_failed", {"reason": "missing error test", "cause": "failing test"}, worker.ID)
        events, _ = self.store.dreaming.collect(self.config)
        ref = dream_model.source_id(events[-1])
        plan = {"improvements": [{"repo": worker.REPO, "cause": "Missing error handling",
            "summary": "Preserve the failing test name", "paths": ["src/app.py"],
            "checks": [{"claim": "Error regression is covered", "command": "python3 -m unittest"}],
            "evidence": [ref]}], "learnings": []}
        with mock.patch.object(dream_model, "completion", return_value=plan):
            result = improvements.run(self.foreman, AT)
        repair = result["issues"][0]
        self.assertEqual(repair["status"], "admitted")
        wid = f"{worker.REPO}#{repair['issueNumber']}"
        wi = self.foreman.tick(wid)["workItem"]
        self.assertEqual(wi["state"], "implementing")
        self.git.sha = "a" * 40
        self.relay.finish(wi["providerTaskId"], output=result_line(wi["activeWorkOrderId"]))
        wi = self.foreman.tick(wid)["workItem"]
        self.assertEqual(wi["state"], "pr_open")
        self.github.add_run(worker.REPO, wi["headSha"])
        self.github.add_verdict(worker.REPO, wi["prNumber"], "MERGE", 1, wi["headSha"])
        self.assertEqual(self.foreman.tick(wid)["workItem"]["state"], "merge_pending")
        self.github.pulls[0].update(state="closed", merged_at="2026-10-28T01:00:00Z")
        self.assertEqual(self.foreman.tick(wid)["workItem"]["state"], "closed")
        self.github.branches[(worker.REPO, "main")] = "b" * 40
        outcomes.refresh(self.foreman, AT)
        owner = tools.build(self.store, self.foreman)
        measured = owner["factory_improvement_validate"].fn({"repo": worker.REPO,
            "fingerprint": repair["fingerprint"], "revision": "b" * 40, "source": "owner:runtime-test-report",
            "baselineOpportunities": 10, "baselineRecurrences": 4, "opportunities": 10, "recurrences": 0})
        packet = self.store.knowledge.packet(self.config, {"repo": worker.REPO, "goal": "Preserve failing test names"})
        self.assertIn(measured["memoryId"], {entry["id"] for entry in packet})
        with mock.patch.object(dream_model, "completion") as model:
            improvements.run(self.foreman, AT)
            model.assert_not_called()
