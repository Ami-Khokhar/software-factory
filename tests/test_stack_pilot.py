"""scripts/stack_pilot: the stacked-PR pilot policy and its isolated two-layer fixture."""
import importlib.machinery
import importlib.util
import os
import subprocess
import tempfile
import unittest


LOADER = importlib.machinery.SourceFileLoader(
    "stack_pilot", os.path.join(os.path.dirname(__file__), "..", "scripts", "stack_pilot"))
pilot = importlib.util.module_from_spec(importlib.util.spec_from_loader("stack_pilot", LOADER))
LOADER.exec_module(pilot)


def parent(**changes):
    head = {"head": "p" * 40, "merged": None, "status": "open", "lines": 60, "files": 3,
            "verdict": "MERGE"}
    return {**head, **changes}


def child(**changes):
    head = {"head": "c" * 40, "base": "p" * 40, "status": "open", "lines": 90, "files": 4,
            "verdict": "MERGE"}
    return {**head, **changes}


class PolicyTest(unittest.TestCase):
    def test_a_fresh_child_under_an_open_parent_waits_for_the_parent(self):
        self.assertEqual(pilot.check_child(child(), parent())["action"], "hold")

    def test_a_child_over_the_file_count_is_split_before_anything_else(self):
        verdict = pilot.check_child(child(files=21), parent(status="failed"))
        self.assertEqual(verdict["action"], "split")
        self.assertIn("21 files", verdict["reason"])

    def test_a_child_over_the_600_line_target_warns_and_keeps_going(self):
        # The line target is advisory everywhere the factory measures a whole pull request
        # (ADR-0070), so the pilot must not split or pause on line count alone.
        verdict = pilot.check_child(child(lines=601), parent())
        self.assertEqual(verdict["action"], "hold")
        self.assertIn("advisory", verdict["warning"])
        verdict = pilot.check_child(child(lines=601),
                                    parent(status="merged", merged="p" * 40))
        self.assertEqual(verdict["action"], "merge")
        self.assertIn("advisory", verdict["warning"])
        verdict = pilot.check_child(child(lines=601), parent(status="failed"))
        self.assertEqual(verdict["action"], "pause")
        self.assertIn("advisory", verdict["warning"])

    def test_a_failed_parent_pauses_the_child(self):
        self.assertEqual(pilot.check_child(child(), parent(status="failed"))["action"], "pause")

    def test_a_cancelled_parent_pauses_the_child(self):
        self.assertEqual(pilot.check_child(child(), parent(status="cancelled"))["action"], "pause")

    def test_a_paused_parent_beats_stale_evidence(self):
        verdict = pilot.check_child(child(), parent(status="failed", head="z" * 40))
        self.assertEqual(verdict["action"], "pause")

    def test_a_parent_move_invalidates_the_child_evidence(self):
        verdict = pilot.check_child(child(), parent(head="z" * 40))
        self.assertEqual(verdict["action"], "stale")
        self.assertIn("rebase", verdict["reason"])

    def test_the_parent_merge_requires_the_child_rerun_on_the_squash_sha(self):
        verdict = pilot.check_child(child(), parent(status="merged", merged="m" * 40))
        self.assertEqual(verdict["action"], "stale")
        self.assertIn("m" * 40, verdict["reason"])

    def test_a_child_without_merge_evidence_holds(self):
        verdict = pilot.check_child(child(base="p" * 40, verdict="CHANGES"),
                         parent(status="merged", merged="p" * 40))
        self.assertEqual(verdict["action"], "hold")

    def test_a_fresh_child_of_a_merged_parent_merges_bottom_up(self):
        verdict = pilot.check_child(child(), parent(status="merged", merged="p" * 40))
        self.assertEqual(verdict["action"], "merge")

    def test_a_merge_verdict_over_the_600_line_target_carries_the_advisory(self):
        verdict = pilot.check_child(child(lines=601),
                                    parent(status="merged", merged="p" * 40))
        self.assertEqual(verdict["action"], "merge")
        self.assertIn("advisory", verdict["warning"])

    def test_the_integrated_stack_fits_one_pull_request_budget(self):
        verdict = pilot.check_stack([parent(status="merged", merged="p" * 40), child()])
        self.assertTrue(verdict["fits"])
        self.assertEqual((verdict["lines"], verdict["files"]), (90, 4))

    def test_an_open_stack_over_the_file_count_is_split(self):
        verdict = pilot.check_stack([parent(), child(files=18)], limits=(600, 20))
        self.assertFalse(verdict["fits"])
        self.assertIn("file budget", verdict["reason"])

    def test_an_open_stack_over_the_600_line_target_warns_but_fits(self):
        verdict = pilot.check_stack([parent(), child(lines=560)])
        self.assertTrue(verdict["fits"])

    def test_merged_layers_leave_the_budget_to_the_open_layers(self):
        verdict = pilot.check_stack([parent(status="merged", lines=60, merged="p" * 40),
                                     child(lines=560, files=8)])
        self.assertTrue(verdict["fits"])
        self.assertEqual((verdict["lines"], verdict["files"]), (560, 8))
        self.assertIn("budget", verdict["reason"])


class FixtureTest(unittest.TestCase):
    def test_the_two_layer_rehearsal_runs_in_isolated_bare_repositories(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = subprocess.run(["python3", "scripts/stack_pilot", tmp],
                                    cwd=os.path.dirname(os.path.dirname(__file__)),
                                    check=True, capture_output=True, text=True,
                                    env=dict(os.environ, GIT_AUTHOR_NAME="t",
                                             GIT_AUTHOR_EMAIL="t@t",
                                             GIT_COMMITTER_NAME="t",
                                             GIT_COMMITTER_EMAIL="t@t")).stdout.splitlines()
        self.assertIn("-> hold:", output[0])
        self.assertIn("-> stale:", "\n".join(output))
        self.assertIn("-> pause:", "\n".join(output))
        self.assertIn("-> merge:", "\n".join(output))
        self.assertIn("-> fits:", "\n".join(output))
