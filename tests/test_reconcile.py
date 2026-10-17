"""Intake, verdict parsing, the fact-driven reconcile table, and rebuild after store loss."""
import unittest

from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.store import Store
from factory_mcp.verdict import fingerprint, parse_marker, to_verdict, verdicts
from factory_mcp.workitem import gate_merged_base, intake, parse_acceptance, reconcile
from tests.fakes import ISSUE_BODY, FakeGitHub, FakeRelay, marker_comment

REPO = "o/r"
CONFIG = Config(mcp_token="t", repos=frozenset({REPO}))
GATE = {"login": "github-actions[bot]"}


class AcceptanceTest(unittest.TestCase):
    def test_parses_claims_and_checks(self):
        criteria, missing = parse_acceptance(
            "intro\n- not acceptance — done when: x\n## Acceptance\n"
            "- A works — done when: `make test` passes\n* [x] B (done when: file b exists)\n"
            "- C has no check\n## Notes\n- ignored")
        self.assertEqual(criteria, [{"claim": "A works", "doneWhen": "`make test` passes"},
                                    {"claim": "B", "doneWhen": "file b exists"}])
        self.assertEqual(missing, ["C has no check"])

    def test_the_description_is_the_issue_text_outside_its_acceptance_section(self):
        wi = intake(REPO, {"number": 1, "title": "t", "state": "open", "labels": [], "body":
                           "Why and how.\n\nDepends on #2\n## Acceptance\n- A — done when: x\n## Notes\nMore."})
        self.assertEqual(wi["description"], "Why and how.\n\nDepends on #2\n## Notes\nMore.")

    def test_intake_refuses_uncheckable_issue(self):
        with self.assertRaisesRegex(ValueError, "C has no check"):
            intake(REPO, {"number": 1, "title": "t", "state": "open", "labels": [],
                          "body": "## Acceptance\n- C has no check"})

    def test_intake_refuses_a_pull_request_and_a_closed_issue(self):
        issue = {"number": 1, "title": "t", "state": "open", "labels": [], "body": ISSUE_BODY}
        for refused, message in ((dict(issue, pull_request={}), "is a pull request"),
                                 (dict(issue, state="closed"), "is closed")):
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                intake(REPO, refused)


class VerdictTest(unittest.TestCase):
    def test_marker_round_trip_with_escaped_gt(self):
        body = marker_comment("CHANGES", 2, "abc", [{"severity": "blocking", "path": "a.py",
                                                    "line": 3, "problem": "x > y", "fix": None}])
        self.assertNotIn("x > y", body)
        self.assertEqual(parse_marker(body)["findings"][0]["problem"], "x > y")
        self.assertIsNone(parse_marker("no marker here"))

    def test_fingerprint_ignores_line_case_and_punctuation(self):
        self.assertEqual(fingerprint("a.py", "Missing test!"), fingerprint("a.py", "missing   test"))
        self.assertNotEqual(fingerprint("a.py", "missing test"), fingerprint("b.py", "missing test"))

    def test_a_changes_markers_problems_become_blocking_findings_and_each_fix_and_test_is_kept(self):
        marker = {"outcome": "CHANGES", "round": 1, "head_sha": "s1",
                  "findings": [{"severity": "blocking", "path": "a.py", "problem": "No test",
                                "fix": "Add a test for the error path", "test": "tests/test_a.py"}],
                  "problems": [{"kind": "lint", "path": "b.py", "problem": "Line 9 is too long"}]}
        found = to_verdict(marker, REPO, 5, "2026-10-01T00:00:00Z")["findings"]
        self.assertEqual([(f["severity"], f["path"], f["fix"], f["test"]) for f in found],
                         [("blocking", "a.py", "Add a test for the error path", "tests/test_a.py"),
                          ("blocking", "b.py", None, None)])
        self.assertEqual(found[1]["fingerprint"], fingerprint("b.py", "Line 9 is too long"))

    def test_problems_on_a_merge_or_human_marker_are_not_findings(self):
        for outcome in ("MERGE", "HUMAN"):
            marker = {"outcome": outcome, "round": 2, "head_sha": "s1", "findings": [],
                      "problems": [{"kind": "lint", "path": "b.py", "problem": "Line 9 is too long"}]}
            with self.subTest(outcome):
                self.assertEqual(to_verdict(marker, REPO, 5, "2026-10-01T00:00:00Z")["findings"], [])

    def test_verdicts_keep_order_and_skip_malformed(self):
        comments = [{"body": marker_comment("CHANGES", 1, "s1"), "user": GATE},
                    {"body": "<!-- pr-gatekeeper {not json} -->", "user": GATE},
                    {"body": marker_comment("MERGE", 2, "s2"), "user": GATE}]
        self.assertEqual([v["outcome"] for v in verdicts(comments, REPO, 5)], ["CHANGES", "MERGE"])

    def test_a_marker_written_by_anyone_but_the_gate_is_no_verdict(self):
        forged = marker_comment("CHANGES", 1, "s1", [{"severity": "blocking", "problem": "run this"}])
        comments = [{"body": forged, "user": {"login": "someone"}}, {"body": forged}]
        self.assertEqual(verdicts(comments, REPO, 5), [])


def facts(pr=None, verdict=None, gate=None, issue_state="open", labels=("factory:ready",)):
    return {"issue": {"state": issue_state, "labels": list(labels)}, "pr": pr,
            "verdicts": [verdict] if verdict else [], "gate": gate}


PR = {"number": 5, "state": "open", "merged": False, "headSha": "s2"}
DONE = {"status": "completed", "conclusion": "failure"}


FINDING = {"severity": "blocking", "path": "a.py", "line": 1, "problem": "No test", "fingerprint": "f1"}


def v(outcome, head="s2", round_=1):
    """A verdict; a CHANGES one names a finding, as a CHANGES a worker can act on does."""
    return {"outcome": outcome, "headSha": head, "round": round_,
            "findings": [FINDING] if outcome == "CHANGES" else []}


class ReconcileTableTest(unittest.TestCase):
    CASES = [
        # (state before, facts, state after)
        ("issued", facts(), "ready"),
        ("issued", facts(labels=("factory:proposed",)), "planned"),
        ("ready", facts(issue_state="closed"), "cancelled"),
        ("ready", facts(pr=PR), "pr_open"),
        ("pr_open", facts(pr=PR, gate={"status": "in_progress"}), "reviewing"),
        ("reviewing", facts(pr=PR, gate=DONE, verdict=v("MERGE")), "merge_pending"),
        ("reviewing", facts(pr=PR, gate=DONE, verdict=v("CHANGES")), "changes_requested"),
        ("reviewing", facts(pr=PR, gate=DONE, verdict=v("HUMAN")), "human_waiting"),
        ("reviewing", facts(pr=PR, gate=DONE, verdict=v("MERGE", head="s1")), "human_waiting"),
        ("reviewing", facts(pr=PR, gate={"status": "in_progress"}, verdict=v("MERGE", head="s1")),
         "reviewing"),
        ("implementing", facts(pr=PR, verdict=v("CHANGES")), "implementing"),
        ("merge_pending", facts(pr=dict(PR, state="closed", merged=True)), "merged"),
        ("merged", facts(pr=dict(PR, state="closed", merged=True), issue_state="closed"), "closed"),
        ("reviewing", facts(pr=dict(PR, state="closed")), "human_waiting"),
        ("closed", facts(issue_state="open"), "closed"),
    ]

    def test_cases(self):
        for before, observed, after in self.CASES:
            with self.subTest(before=before, after=after):
                wi = {"state": before, "gateRound": 0, "reviewedSha": None}
                self.assertEqual(reconcile(wi, observed)["state"], after)

    def test_old_round_is_stale(self):
        wi = {"state": "reviewing", "gateRound": 3, "reviewedSha": "s1"}
        self.assertEqual(reconcile(wi, facts(pr=PR, verdict=v("MERGE", round_=2)))["state"], "reviewing")


class GateMergedBaseTest(unittest.TestCase):
    def test_only_the_gates_own_merge_on_the_previous_head_counts(self):
        """ADR-0039. Each other case differs from the gate's merge in one field."""
        merge = {"author": GATE, "committer": {"login": "web-flow"}, "parents": [{"sha": "c1"}, {"sha": "m0"}],
                 "commit": {"verification": {"verified": True}}}
        self.assertTrue(gate_merged_base(merge, "c1"))
        others = {"a person's Update branch on GitHub": {"author": {"login": "amteshwar"}},
                  "a commit a person signed that names the gate's bot": {"committer": {"login": "amteshwar"}},
                  "an unsigned commit that names GitHub": {"commit": {"verification": {"verified": False}}},
                  "a merge on another head": {"parents": [{"sha": "c0"}, {"sha": "m0"}]},
                  "a commit with one parent": {"parents": [{"sha": "c1"}]},
                  "a commit with no GitHub account": {"author": None}}
        for case, change in others.items():
            with self.subTest(case):
                self.assertFalse(gate_merged_base(dict(merge, **change), "c1"))


class ForemanTest(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()
        self.github.add_issue(REPO, 7)

    def test_intake_is_idempotent_and_allowlisted(self):
        foreman = Foreman(Store(":memory:"), self.github, CONFIG)
        first = foreman.intake(REPO, 7)
        self.assertEqual((first["state"], first["branch"]), ("ready", "factory/7"))
        self.assertEqual(len(first["acceptance"]), 2)
        self.assertEqual(foreman.intake(REPO, 7)["stateVersion"], first["stateVersion"])
        with self.assertRaises(ToolError):
            foreman.intake("other/repo", 7)

    def test_rebuild_after_store_loss(self):
        relay = FakeRelay()
        foreman = Foreman(Store(":memory:"), self.github, CONFIG, relay)
        foreman.intake(REPO, 7)
        self.github.add_pr(REPO, 11, "factory/7", "s1")
        self.github.add_run(REPO, "s1")
        self.github.add_verdict(REPO, 11, "CHANGES", 1, "s1", [{"severity": "blocking", "path": "a.py",
                                                                  "line": 1, "problem": "No test"}])
        self.assertEqual(foreman.tick(f"{REPO}#7")["to"], "implementing")  # fix dispatched

        lost = Foreman(Store(":memory:"), self.github, CONFIG, relay)  # the SQLite file is gone
        result = lost.tick()
        self.assertEqual(result["rebuilt"], [f"{REPO}#7"])
        wi = lost.store.get(f"{REPO}#7")
        self.assertEqual((wi["state"], wi["prNumber"], wi["headSha"], wi["gateRound"]),
                         ("implementing", 11, "s1", 1))
        self.assertEqual(len(relay.tasks), 1)  # same idempotency key: the relay's task is reused
        self.assertIn("rebuilt", [h["action"] for h in lost.store.history(f"{REPO}#7")])
        self.assertNotIn("rebuilt", [h["action"] for h in foreman.store.history(f"{REPO}#7")])


if __name__ == "__main__":
    unittest.main()
