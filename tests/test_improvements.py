"""Saved plans publish once, adopt lost responses, and retain bounded repair authority."""
import dataclasses
import unittest
from datetime import timedelta
from unittest import mock

from factory_mcp import dream_model, improvements
from factory_mcp.foreman import Foreman
from factory_mcp.github import GitHubError
from factory_mcp.mcp import ToolError
from tests import test_dream_model as model_tests
from tests import test_dreaming as base
from tests.fakes import FakeGitHub, FakeRelay


class ImprovementsTest(unittest.TestCase):
    def setUp(self):
        model_tests.DreamModelTest.setUp(self)
        self.gh, self.relay = FakeGitHub(), FakeRelay()
        self.foreman = Foreman(self.store, self.gh, self.config, self.relay)

    def tearDown(self):
        model_tests.DreamModelTest.tearDown(self)

    def prepare(self, count=1, paths=None):
        plan = model_tests.DreamModelTest.plan(self)
        if paths is not None: plan["improvements"][0]["paths"] = paths
        if count == 2:
            second = {**plan["improvements"][0], "cause": "Different root cause"}
            plan["improvements"].append(second)
        with mock.patch.object(dream_model, "completion", return_value=plan):
            dream_model.reflect(self.store, self.config, base.AT)
        return self.store.dreaming.latest()

    def test_proposed_by_default_and_repeated_publication_does_not_duplicate(self):
        run = self.prepare()
        first = improvements.publish(self.foreman, run)
        second = improvements.publish(self.foreman, run)
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual(first["issues"][0]["issueNumber"], second["issues"][0]["issueNumber"])
        issue = next(iter(self.gh.issues.values()))
        self.assertIn("factory:proposed", {label["name"] for label in issue["labels"]})
        self.assertIn("## Acceptance", issue["body"])
        self.assertIn("https://github.com/o/r/issues/1", issue["body"])
        self.assertEqual(self.relay.tasks, {})

    def test_lost_create_response_is_adopted_from_remote_marker(self):
        run = self.prepare()
        create = self.gh.create_issue
        def lost(*args):
            create(*args)
            raise GitHubError(502, "response lost")
        with mock.patch.object(self.gh, "create_issue", side_effect=lost), self.assertRaises(GitHubError):
            improvements.publish(self.foreman, run)
        result = improvements.publish(self.foreman, run)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(self.gh.issues), 1)

    def test_ambiguous_create_never_blindly_retries(self):
        run = self.prepare()
        with mock.patch.object(self.gh, "create_issue", side_effect=GitHubError(502, "unknown")), self.assertRaises(GitHubError):
            improvements.publish(self.foreman, run)
        with mock.patch.object(self.gh, "create_issue") as create:
            result = improvements.publish(self.foreman, run)
            create.assert_not_called()
        self.assertEqual(result["status"], "publication_pending")

    def test_allowlisted_auto_admission_uses_normal_trust_checks_and_only_one_slot(self):
        self.config = dataclasses.replace(self.config, dream_auto_repos=frozenset({"o/r"}))
        self.foreman.config = self.config
        result = improvements.publish(self.foreman, self.prepare(count=2))
        self.assertEqual([row["status"] for row in result["issues"]].count("admitted"), 1)
        admitted = next(row for row in result["issues"] if row["status"] == "admitted")
        wi = self.store.get(f"o/r#{admitted['issueNumber']}")
        self.assertEqual(wi["state"], "ready")
        self.assertEqual(self.relay.tasks, {})  # ordinary ticks dispatch later

    def test_protected_changes_are_never_auto_admitted(self):
        self.config = dataclasses.replace(self.config, dream_auto_repos=frozenset({"o/r"}))
        self.foreman.config = self.config
        result = improvements.publish(self.foreman, self.prepare(paths=[".github/workflows/gate.yml"]))
        self.assertEqual(result["issues"][0]["status"], "proposed")
        self.assertIn("protected", result["issues"][0]["reason"])

    def test_untrusted_marker_is_not_adopted(self):
        run = self.prepare()
        candidate = run["result"]["improvements"][0]
        self.gh.add_issue("o/r", 4, body=improvements.body(candidate, run), author="stranger")
        with self.assertRaises(ToolError): improvements.publish(self.foreman, run)
        self.assertEqual(len(self.gh.issues), 1)

    def test_closed_existing_issue_is_adopted_without_reopening(self):
        run = self.prepare()
        self.gh.add_issue("o/r", 4, body=improvements.body(run["result"]["improvements"][0], run), state="closed")
        result = improvements.publish(self.foreman, run)
        self.assertEqual(result["issues"][0]["issueNumber"], 4)
        self.assertEqual(self.gh.issues[("o/r", 4)]["state"], "closed")

    def test_publication_resumes_without_a_second_model_call(self):
        self.prepare()
        with mock.patch.object(dream_model, "completion") as model:
            result = improvements.run(self.foreman, base.AT)
            model.assert_not_called()
        self.assertEqual(result["status"], "complete")

    def test_exact_related_gate_followup_is_reused_without_modifying_it(self):
        run = self.prepare()
        candidate = run["result"]["improvements"][0]
        original = candidate["cause"] + " in worker/runner.py"
        self.gh.add_issue("o/r", 50, body=original)
        self.gh.issues[("o/r", 50)]["title"] = "Follow-up to #12: previous review"
        result = improvements.publish(self.foreman, run)
        self.assertEqual(result["issues"][0]["issueNumber"], 50)
        self.assertEqual(self.gh.issues[("o/r", 50)]["body"], original)
        self.assertEqual(len(self.gh.issues), 1)

    def test_failed_improvements_do_not_start_automatic_descendant_repairs(self):
        self.config = dataclasses.replace(self.config, dream_auto_repos=frozenset({"o/r"}))
        self.foreman.config = self.config
        first = improvements.publish(self.foreman, self.prepare())["issues"][0]
        wid = f"o/r#{first['issueNumber']}"
        self.store.log("attempt_failed", {"reason": "repair failed"}, wid)
        events, _ = self.store.dreaming.collect(self.config)
        event = next(e for e in events if e["workItemId"] == wid)
        plan = model_tests.DreamModelTest.plan(self)
        plan["improvements"][0].update(cause="Repair itself failed", evidence=[dream_model.source_id(event)])
        plan["learnings"] = []
        with mock.patch.object(dream_model, "completion", return_value=plan):
            dream_model.reflect(self.store, self.config, base.AT + timedelta(days=7))
        result = improvements.publish(self.foreman, self.store.dreaming.latest())
        self.assertEqual(result["issues"][0]["status"], "proposed")
        self.assertIn("descendant", result["issues"][0]["reason"])
