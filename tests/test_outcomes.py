"""Merge facts, measured effects, versioned corrections and exposure are distinct."""
import unittest
from datetime import timedelta

from factory_mcp import documents, improvements, outcomes, tools
from factory_mcp.mcp import ToolError
from tests import test_improvements as publication
from tests import test_dreaming as base
from tests.fakes import workitem


class OutcomesTest(unittest.TestCase):
    def setUp(self):
        publication.ImprovementsTest.setUp(self)
        run = publication.ImprovementsTest.prepare(self)
        self.record = improvements.publish(self.foreman, run)["issues"][0]
        self.wid = f"o/r#{self.record['issueNumber']}"

    def tearDown(self):
        publication.ImprovementsTest.tearDown(self)

    def merged(self):
        self.store.put(workitem(id=self.wid, state="closed", headSha="a" * 40, prNumber=100))
        outcomes.refresh(self.foreman, base.AT)

    def measure(self, **over):
        return outcomes.validate(self.foreman, {"repo": "o/r", "fingerprint": self.record["fingerprint"],
            "revision": "b" * 40, "source": "owner:verified-runtime-report",
            "baselineOpportunities": 10, "baselineRecurrences": 4,
            "opportunities": 10, "recurrences": 0, **over}, base.AT)

    def report(self):
        return outcomes.report(self.store, self.config, {"repo": "o/r"})["improvements"][0]

    def test_merge_alone_never_means_deployed_or_improved(self):
        self.merged()
        report = self.report()
        self.assertEqual(report["implementation"], "merged")
        self.assertEqual(report["effect"], "unknown")
        self.assertNotIn("validation", report)
        self.assertEqual(report["exposedWorkItems"], 0)
        node = self.store.knowledge.search(self.config, {"repo": "o/r", "id": report["fixMemoryId"]})["memories"][0]
        self.assertIn("unverified", node["text"])
        self.assertEqual(node["links"][0]["id"], self.record["memoryId"])

    def test_reported_effect_keeps_source_and_can_be_contradicted_with_history(self):
        self.merged()
        first = self.measure()
        self.assertEqual(first["origin"], "owner_reported")
        self.assertEqual(self.report()["effect"], "observed_better")
        second = self.measure(version=1, recurrences=6)
        self.assertEqual(second["direction"], "not_observed_better")
        latest = self.store.knowledge.search(self.config, {"repo": "o/r", "id": first["memoryId"]})["memories"][0]
        self.assertEqual(latest["status"], "contradicted")
        old = self.store.knowledge.search(self.config, {"repo": "o/r", "id": first["memoryId"], "version": 1})["memories"][0]
        self.assertEqual(old["status"], "approved")

    def test_invalid_or_stale_measurements_are_refused(self):
        with self.assertRaises(ToolError): self.measure()
        self.merged()
        for args in ({"opportunities": 0}, {"recurrences": 11}, {"opportunities": True},
                     {"revision": "short"}, {"source": "password=hidden"}):
            with self.subTest(args=args), self.assertRaises(ToolError): self.measure(**args)
        self.measure()
        with self.assertRaises(ToolError): self.measure()

    def test_exposure_counts_only_failures_after_the_worker_received_memory(self):
        self.merged()
        measured = self.measure()
        wi = self.store.put(workitem(id="o/r#77", goal="Preserve the failing test name"))
        self.store.log("attempt_failed", {"reason": "earlier failure"}, wi["id"])
        order = documents.workorder(wi, "implement", 0, self.config)
        from factory_mcp.store import now
        order["createdAt"] = now()
        order["memory"] = [{"id": measured["memoryId"], "version": 1, "kind": "learning",
                            "text": "Source-linked observation", "source": "owner:report"}]
        self.store.save_workorder(order)
        self.store.log("dispatch", {"workOrder": order["id"]}, wi["id"])
        self.store.log("submitted", {"task": "accepted-task"}, wi["id"])
        self.assertEqual(self.report()["exposedWorkItems"], 1)
        self.assertEqual(self.report()["failuresAfterExposure"], 0)
        # A later pass failure is counted as temporal association, not same-cause recurrence.
        self.store.log("attempt_failed", {"reason": "a different test failed"}, wi["id"])
        self.assertEqual(self.report()["failuresAfterExposure"], 1)

    def test_driver_refreshes_merge_context_without_calling_a_model(self):
        self.store.put(workitem(id=self.wid, state="merged", headSha="a" * 40, prNumber=100))
        improvements.run(self.foreman, base.AT + timedelta(hours=1))
        self.assertIn("fixMemoryId", self.report())

    def test_validation_is_owner_only_and_status_respects_repo_scope(self):
        agent = tools.build(self.store, self.foreman, agent=True)
        self.assertIn("factory_improvement_status", agent)
        self.assertNotIn("factory_improvement_validate", agent)
        with self.assertRaises(ToolError): outcomes.report(self.store, self.config, {"repo": "other/private"})

    def test_relevant_code_changes_demote_measurements_without_erasing_their_history(self):
        self.merged()
        measured = self.measure()
        self.gh.branches[("o/r", "main")] = "c" * 40
        self.gh.files[("o/r", "c" * 40)] = [{"filename": "worker/runner.py"}]
        outcomes.refresh(self.foreman, base.AT + timedelta(days=1))
        self.assertEqual(self.report()["effect"], "needs_revalidation")
        current = self.store.knowledge.search(self.config, {"repo": "o/r", "id": measured["memoryId"]})["memories"][0]
        self.assertEqual(current["status"], "candidate")
        self.assertEqual(current["version"], 2)
