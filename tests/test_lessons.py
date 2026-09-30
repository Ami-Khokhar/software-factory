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
        store = store_with((REPO, verdict_history("sometimes")), (REPO, verdict_history("sometimes")),
                           (REPO, verdict_history("often")), (REPO, verdict_history("often")),
                           (REPO, verdict_history("often")))
        self.assertEqual([c["fingerprint"] for c in lessons.candidates(store, REPO)],
                         ["often", "sometimes"])

    def test_a_candidate_shows_the_gates_latest_wording_of_the_problem(self):
        store = store_with((REPO, verdict_history(problem="No test for the error path")),
                           (REPO, verdict_history(problem="Still no test for the 401 path")))
        self.assertEqual(lessons.candidates(store, REPO)[0]["problem"], "Still no test for the 401 path")

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

    def test_a_lessons_file_that_is_not_json_is_named_in_the_error(self):
        Path(self.path).write_text("{truncated")
        with self.assertRaisesRegex(ToolError, "lessons.json is not JSON"):
            self.tools["factory_lessons"].fn({"repo": REPO})

    def test_factory_lessons_says_when_no_lessons_file_is_configured(self):
        foreman = Foreman(self.store, self.github,
                          Config(mcp_token="t", repos=frozenset({REPO})), FakeRelay())
        with self.assertRaisesRegex(ToolError, "FACTORY_LESSONS_FILE is not set"):
            tools.build(self.store, foreman)["factory_lessons"].fn({"repo": REPO})

    def test_retiring_needs_the_name_of_who_retired_it(self):
        self.approve()
        with self.assertRaisesRegex(ToolError, "needs the name of the person"):
            self.tools["factory_retire_lesson"].fn({"repo": REPO, "fingerprint": FP, "by": " "})
        self.assertEqual(len(lessons.load(self.path)), 1)

    def test_status_shows_each_lessons_effect_from_the_stored_workorders(self):
        self.approve()
        for round_, kind, findings in ((0, "implement", []),
                                       (1, "fix", [{"fingerprint": FP, "problem": "No test"}])):
            self.store.save_workorder({"id": f"{REPO}#7:r{round_}:{kind}:a0", "workItemId": f"{REPO}#7",
                                       "round": round_, "kind": kind, "repo": REPO,
                                       "lessons": [{"fingerprint": FP, "text": "t"}],
                                       "findingsToAddress": findings})
        summary = self.tools["factory_status"].fn({})
        effect = summary["lessonEffect"][f"{REPO}:{FP}"]
        self.assertEqual((effect["sent"], effect["recurred"], effect["notWorking"]), (1, 1, False))
        self.assertNotIn("lessonsError", summary)

    def test_status_names_a_broken_lessons_file(self):
        Path(self.path).write_text('{"version": 2}')
        summary = self.tools["factory_status"].fn({})
        self.assertIn('needs "version": 1', summary["lessonsError"])
        self.assertNotIn("lessonEffect", summary)

    def test_retire_removes_the_lesson(self):
        self.approve()
        result = self.tools["factory_retire_lesson"].fn({"repo": REPO, "fingerprint": FP, "by": "amteshwar"})
        self.assertEqual(result, {"repo": REPO, "fingerprint": FP, "retired": True})
        self.assertEqual(lessons.load(self.path), [])
        with self.assertRaisesRegex(ToolError, "is not a lesson"):
            self.tools["factory_retire_lesson"].fn({"repo": REPO, "fingerprint": FP, "by": "amteshwar"})

    def test_one_fingerprint_is_a_lesson_in_each_repo_on_its_own(self):
        """A fingerprint names a path and a problem, not a repo: two repos can share one."""
        both = lambda r: [{"fingerprint": FP, "workItems": 2}]
        lessons.approve(self.path, REPO, FP, "here", "amteshwar", both)
        lessons.approve(self.path, "other/r", FP, "there", "amteshwar", both)
        lessons.retire(self.path, "other/r", FP)
        self.assertEqual([(l["repo"], l["text"]) for l in lessons.load(self.path)], [(REPO, "here")])

    def test_a_hand_edited_entry_without_its_approver_is_refused(self):
        Path(self.path).write_text(json.dumps(
            {"version": 1, "lessons": [{"repo": REPO, "fingerprint": FP, "text": "t"}]}))
        with self.assertRaisesRegex(ValueError, "a lesson needs approved_by; .*needs approved_at"):
            lessons.load(self.path)


class WorkOrderLessonsTest(unittest.TestCase):
    """Issue 3: the WorkOrder carries the repo's lessons to the worker."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = str(Path(self.dir.name) / "lessons.json")
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.github.add_issue(REPO, 7)
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO}), lessons_file=self.path),
                               self.relay)
        self.foreman.intake(REPO, 7)

    def dispatch(self):
        wi = self.foreman.tick(f"{REPO}#7")["workItem"]
        self.assertEqual(wi["state"], "implementing")
        order = self.foreman.store.workorders(f"{REPO}#7")[-1]
        return wi, order, self.relay.tasks[wi["providerTaskId"]]

    def test_a_workorder_for_a_repo_with_lessons_carries_them(self):
        lessons.approve(self.path, REPO, FP, "Every new endpoint carries a test for its 401 path.",
                        "amteshwar", lambda r: [{"fingerprint": FP, "workItems": 2}])
        wi, order, task = self.dispatch()
        self.assertEqual(order["lessons"],
                         [{"fingerprint": FP, "text": "Every new endpoint carries a test for its 401 path."}])
        self.assertIn('"lessons"', task["input"])

    def test_a_workorder_for_a_repo_with_none_carries_an_empty_list(self):
        wi, order, _ = self.dispatch()
        self.assertEqual(order["lessons"], [])

    def test_the_worker_rules_still_come_before_the_workorder(self):
        lessons.approve(self.path, REPO, FP, "avoid the 401 gap", "amteshwar",
                        lambda r: [{"fingerprint": FP, "workItems": 2}])
        _, order, task = self.dispatch()
        rules, payload = task["input"].split("WorkOrder:", 1)
        self.assertIn("lessons are past gate findings in this repo", rules)
        self.assertLess(rules.index("Rules (these override"), rules.index("lessons are past gate"))
        self.assertIn(order["id"], payload)

    def test_at_most_ten_lessons_ride_along(self):
        for n in range(12):
            lessons.approve(self.path, REPO, f"fp-{n}", f"lesson {n}", "amteshwar",
                            lambda r, n=n: [{"fingerprint": f"fp-{n}", "workItems": 2}])
        _, order, _ = self.dispatch()
        self.assertEqual(len(order["lessons"]), 10)

    def test_a_hand_edited_long_lesson_is_cut_to_the_schemas_300_characters(self):
        Path(self.path).write_text(json.dumps({"version": 1, "lessons": [
            {"repo": REPO, "fingerprint": FP, "text": "x" * 400, "approved_by": "amteshwar",
             "approved_at": "2026-09-27T00:00:00Z"}]}))
        _, order, _ = self.dispatch()
        self.assertEqual(order["lessons"], [{"fingerprint": FP, "text": "x" * 300}])

    def test_only_this_repos_lessons_ride_along(self):
        lessons.approve(self.path, "other/r", "fp-other", "other repo's lesson", "amteshwar",
                        lambda r: [{"fingerprint": "fp-other", "workItems": 2}])
        _, order, _ = self.dispatch()
        self.assertEqual(order["lessons"], [])

    def test_a_broken_lessons_file_does_not_stop_the_line(self):
        Path(self.path).write_text("{not json")
        wi, order, _ = self.dispatch()
        self.assertEqual(order["lessons"], [])
        self.assertIn("lessons_invalid", [h["action"] for h in self.foreman.store.history(f"{REPO}#7")])


if __name__ == "__main__":
    unittest.main()
