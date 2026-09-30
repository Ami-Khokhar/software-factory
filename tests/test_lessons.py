"""Lesson candidates from the audit log, and the owner-approved lessons file (ADR-0020)."""
import json
import tempfile
import unittest
from pathlib import Path

from factory_mcp import lessons, tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.store import Store
from tests.fakes import FakeGitHub, FakeRelay, workitem

REPO = "o/r"
FP = "f1ngerpr1nt"


def verdict_history(fingerprint=FP, problem="No test for the error path"):
    """One accepted verdict's audit entry, as Foreman.tick_one writes it."""
    return [("reconcile", {"headSha": "c1", "from": "reviewing", "to": "changes_requested",
                           "blocking": [{"fingerprint": fingerprint, "problem": problem}]})]


def store_with(*repos_findings):
    """A store holding one WorkItem per argument: (repo, [history entries])."""
    store = Store(":memory:")
    for n, (repo, history) in enumerate(repos_findings, start=1):
        wi = workitem(id=f"{repo}#{n}", repo=repo, issueNumber=n)
        store.put(wi)
        for action, detail in history:
            store.log(action, detail, wi["id"])
    return store


class CandidatesTest(unittest.TestCase):
    def test_a_finding_on_two_workitems_is_a_candidate(self):
        store = store_with((REPO, verdict_history()), (REPO, verdict_history()))
        found = lessons.candidates(store, REPO)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["fingerprint"], FP)
        self.assertEqual(found[0]["workItems"], 2)

    def test_a_finding_on_one_workitem_is_not(self):
        store = store_with((REPO, verdict_history()), (REPO, verdict_history("other")))
        self.assertEqual(lessons.candidates(store, REPO), [])

    def test_a_minor_finding_is_not(self):
        history = [("reconcile", {"headSha": "c1", "from": "reviewing", "to": "changes_requested"})]
        store = store_with((REPO, history), (REPO, history))
        self.assertEqual(lessons.candidates(store, REPO), [])

    def test_two_rounds_on_one_workitem_are_one_sight_not_two(self):
        history = verdict_history() + verdict_history(problem="still there, round 2")
        store = store_with((REPO, history))
        self.assertEqual(lessons.candidates(store, REPO), [])

    def test_findings_stay_within_one_repo(self):
        store = store_with((REPO, verdict_history()), (REPO, verdict_history()),
                           ("other/r", verdict_history("other")), ("other/r", verdict_history("other")))
        self.assertEqual([c["fingerprint"] for c in lessons.candidates(store, REPO)], [FP])
        self.assertEqual([c["fingerprint"] for c in lessons.candidates(store, "other/r")], ["other"])

    def test_repos_match_case_insensitively(self):
        store = store_with((REPO, verdict_history()), ("O/R", verdict_history()))
        self.assertEqual(len(lessons.candidates(store, REPO.upper())), 1)

    def test_most_seen_first(self):
        store = store_with((REPO, verdict_history("often")), (REPO, verdict_history("often")),
                           (REPO, verdict_history("once")))
        self.assertEqual([c["fingerprint"] for c in lessons.candidates(store, REPO)], ["often"])

    def test_an_empty_store_has_no_candidates(self):
        self.assertEqual(lessons.candidates(Store(":memory:"), REPO), [])


class ApproveRetireTest(unittest.TestCase):
    """The owner's acts, through the tools Day Desk calls."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = str(Path(self.dir.name) / "lessons.json")
        self.github = FakeGitHub()
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO}), lessons_file=self.path),
                               FakeRelay())
        self.store = self.foreman.store
        for n in (7, 8):  # the gate blocked on FP on two WorkItems: FP is a candidate
            wi = workitem(id=f"{REPO}#{n}", repo=REPO, issueNumber=n)
            self.store.put(wi)
            self.store.log("reconcile", {"headSha": f"c{n}", "from": "reviewing",
                                         "to": "changes_requested",
                                         "blocking": [{"fingerprint": FP, "problem": "No test"}]},
                           wi["id"])
        self.tools = tools.build(self.store, self.foreman)

    def approve(self, fingerprint=FP, text="Every new endpoint carries a test for its 401 path.",
                **args):
        return self.tools["factory_approve_lesson"].fn(
            dict({"repo": REPO, "fingerprint": fingerprint, "text": text, "by": "amteshwar"}, **args))

    def test_an_approved_lesson_is_in_the_file_with_approver_and_time(self):
        entry = self.approve()
        stored = json.loads(Path(self.path).read_text())["lessons"][0]
        self.assertEqual(stored, entry)
        self.assertEqual(stored["approved_by"], "amteshwar")
        self.assertTrue(stored["approved_at"])
        self.assertEqual(stored["repo"], REPO)

    def test_the_stored_text_is_capped_at_300_characters_and_one_line(self):
        entry = self.approve(text="  word  \n " + "x" * 400)
        self.assertEqual(entry["text"], "word " + "x" * 295)

    def test_a_fingerprint_that_is_not_a_candidate_is_refused(self):
        with self.assertRaisesRegex(ToolError, "not a candidate in o/r"):
            self.approve(fingerprint="never-seen")
        self.assertEqual(Path(self.path).exists(), False)  # nothing written

    def test_a_lesson_is_approved_once(self):
        self.approve()
        with self.assertRaisesRegex(ToolError, "already a lesson"):
            self.approve()
        self.assertEqual(len(lessons.load(self.path)), 1)

    def test_the_owner_edits_the_wording_the_candidate_does_not_fix_it(self):
        entry = self.approve(text="Say it your own way.")
        self.assertEqual(entry["text"], "Say it your own way.")
        self.assertEqual(entry["fingerprint"], FP)  # the candidate's fingerprint, the owner's words

    def test_factory_lessons_shows_candidates_and_lessons(self):
        shown = self.tools["factory_lessons"].fn({"repo": REPO})
        self.assertEqual([c["fingerprint"] for c in shown["candidates"]], [FP])
        self.assertEqual(shown["lessons"], [])
        self.approve()
        shown = self.tools["factory_lessons"].fn({"repo": REPO})
        self.assertEqual([l["fingerprint"] for l in shown["lessons"]], [FP])
        self.assertEqual([l["text"] for l in shown["lessons"]],
                         ["Every new endpoint carries a test for its 401 path."])

    def test_a_repo_off_the_allowlist_has_no_lessons(self):
        with self.assertRaisesRegex(ToolError, "not in FACTORY_REPOS"):
            self.tools["factory_lessons"].fn({"repo": "other/r"})
        with self.assertRaisesRegex(ToolError, "not in FACTORY_REPOS"):
            self.approve(repo="other/r", fingerprint="other")

    def test_no_lessons_file_is_configured(self):
        foreman = Foreman(self.store, self.github,
                          Config(mcp_token="t", repos=frozenset({REPO})), FakeRelay())
        tool = tools.build(self.store, foreman)["factory_approve_lesson"]
        with self.assertRaisesRegex(ToolError, "FACTORY_LESSONS_FILE is not set"):
            tool.fn({"repo": REPO, "fingerprint": FP, "text": "t", "by": "o"})

    def test_retire_removes_the_lesson(self):
        self.approve()
        result = self.tools["factory_retire_lesson"].fn({"fingerprint": FP, "by": "amteshwar"})
        self.assertEqual(result, {"fingerprint": FP, "retired": True})
        self.assertEqual(lessons.load(self.path), [])
        with self.assertRaisesRegex(ToolError, "is not a lesson"):
            self.tools["factory_retire_lesson"].fn({"fingerprint": FP, "by": "amteshwar"})


if __name__ == "__main__":
    unittest.main()
