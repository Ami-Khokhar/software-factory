"""Intake, verdict parsing, the fact-driven reconcile table, and rebuild after store loss."""
import unittest

from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.store import Store
from factory_mcp.verdict import fingerprint, parse_marker, verdicts
from factory_mcp.workitem import intake, parse_acceptance, reconcile
from tests.fakes import ISSUE_BODY, FakeGitHub, FakeRelay, marker_comment

REPO = "o/r"
CONFIG = Config(mcp_token="t", repos=frozenset({REPO}))


class AcceptanceTest(unittest.TestCase):
    def test_parses_claims_and_checks(self):
        criteria, missing = parse_acceptance(
            "intro\n- not acceptance — done when: x\n## Acceptance\n"
            "- A works — done when: `make test` passes\n* [x] B (done when: file b exists)\n"
            "- C has no check\n## Notes\n- ignored")
        self.assertEqual(criteria, [{"claim": "A works", "doneWhen": "`make test` passes"},
                                    {"claim": "B", "doneWhen": "file b exists"}])
        self.assertEqual(missing, ["C has no check"])

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

    def test_verdicts_keep_order_and_skip_malformed(self):
        comments = [{"body": marker_comment("CHANGES", 1, "s1")},
                    {"body": "<!-- pr-gatekeeper {not json} -->"},
                    {"body": marker_comment("MERGE", 2, "s2")}]
        self.assertEqual([v["outcome"] for v in verdicts(comments, REPO, 5)], ["CHANGES", "MERGE"])


def facts(pr=None, verdict=None, gate=None, issue_state="open", labels=("factory:ready",)):
    return {"issue": {"state": issue_state, "labels": list(labels)}, "pr": pr,
            "verdicts": [verdict] if verdict else [], "gate": gate}


PR = {"number": 5, "state": "open", "merged": False, "headSha": "s2"}
DONE = {"status": "completed", "conclusion": "failure"}


def v(outcome, head="s2", round_=1):
    return {"outcome": outcome, "headSha": head, "round": round_, "findings": []}


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
        self.github.add_verdict(REPO, 11, "CHANGES", 1, "s1")
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
