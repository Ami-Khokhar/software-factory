"""The weekly report's numbers (metrics.weekly) over fixed audit histories and a fixed clock, the
reason the Foreman records when a WorkItem goes to a person, and the factory_report tool."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from factory_mcp import lessons, metrics, tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.store import Store
from tests.fakes import FakeGit, FakeGitHub, FakeRelay, workitem

NOW = datetime(2026, 10, 5, 3, 30, tzinfo=timezone.utc)  # Monday 09:00 in Asia/Kolkata
UNTIL, SINCE = "2026-10-05T03:30:00Z", "2026-09-28T03:30:00Z"
THIS_WEEK, WEEK_BEFORE, EARLIER = "2026-10-01T12:00:00Z", "2026-09-25T12:00:00Z", "2026-09-10T12:00:00Z"
HUMAN = "pr-gatekeeper decided HUMAN; read the latest gate comment"
SILENT = "no pr-gatekeeper verdict on {} after 120 min; check the gate workflow run"
ATTEMPTS = ("2 worker attempts at the implement WorkOrder failed. Fix the cause named in attempts, "
            "then call factory_resolve_human.")


def entry(at, action, **detail):
    return {"at": at, "action": action, "detail": detail}


def merged(at):
    """A worker's commit that the gate merged at `at`: an autonomous end."""
    return [entry(at, "result", commitSha="c1"),
            entry(at, "reconcile", headSha="c1", **{"from": "merge_pending", "to": "merged"})]


def escalated(at, to="human_waiting", reason=HUMAN, action="reconcile"):
    """An audit entry as Foreman._write logs a move into a state that needs a person."""
    return entry(at, action, escalation=reason, **{"from": "reviewing", "to": to})


def weekly(histories, candidates=(), effect=None, now=NOW):
    """metrics.weekly over one WorkItem per history, each in the state its history ends in."""
    workitems = [{"id": workitem_id, "state": next((h["detail"]["to"] for h in reversed(history)
                                                    if "to" in h["detail"]), "issued")}
                 for workitem_id, history in histories.items()]
    return metrics.weekly(workitems, lambda workitem_id: histories[workitem_id], list(candidates),
                          effect or {}, now)


class WeekTest(unittest.TestCase):
    def test_autonomy_counts_the_workitems_that_ended_in_each_week(self):
        report = weekly({"o/r#1": merged(THIS_WEEK),
                         "o/r#2": [escalated("2026-09-30T00:00:00Z")] + merged(THIS_WEEK),
                         "o/r#3": merged(WEEK_BEFORE),
                         "o/r#4": merged(EARLIER),
                         "o/r#5": [entry(THIS_WEEK, "pushed", **{"from": "implementing", "to": "reviewing"})]})
        self.assertEqual((report["since"], report["until"]), (SINCE, UNTIL))
        self.assertEqual(report["autonomy"], {"ended": 2, "autonomous": 1, "unknown": 0, "rate": 0.5})
        self.assertEqual(report["autonomyWeekBefore"],
                         {"ended": 1, "autonomous": 1, "unknown": 0, "rate": 1.0})

    def test_a_week_runs_from_seven_days_before_now_up_to_and_including_now(self):
        after_now = "2026-10-05T03:30:01Z"
        report = weekly({"o/r#1": merged(UNTIL), "o/r#2": merged(SINCE), "o/r#3": merged(after_now),
                         "o/r#4": [escalated(UNTIL)], "o/r#5": [escalated(SINCE)],
                         "o/r#6": [escalated(after_now)]})
        self.assertEqual((report["autonomy"]["ended"], report["autonomyWeekBefore"]["ended"]), (1, 1))
        self.assertEqual(report["escalated"], 1)

    def test_a_week_later_the_same_history_is_the_week_before(self):
        histories = {"o/r#1": merged(THIS_WEEK), "o/r#2": [escalated(THIS_WEEK)]}
        later = weekly(histories, now=NOW + timedelta(days=7))
        self.assertEqual((later["autonomy"]["ended"], later["autonomyWeekBefore"]["ended"],
                          later["escalated"]), (0, 1, 0))

    def test_equal_reasons_are_one_group_and_the_three_most_frequent_are_kept(self):
        resolved = entry(THIS_WEEK, "human_resolved", **{"from": "human_waiting", "to": "changes_requested"})
        report = weekly({
            "o/r#1": [escalated(THIS_WEEK), resolved, escalated(THIS_WEEK)],
            "o/r#2": [escalated(THIS_WEEK)],
            "o/r#3": [escalated(THIS_WEEK, reason=SILENT.format("a" * 40), action="gate_silent")],
            "o/r#4": [escalated(THIS_WEEK, reason=SILENT.format("b" * 40), action="gate_silent")],
            "o/r#5": [escalated(THIS_WEEK, to="blocked", reason=ATTEMPTS, action="attempt_failed")],
            "o/r#6": [escalated(THIS_WEEK, to="exhausted", action="exhausted",
                                reason="4 worker passes used; FACTORY_MAX_PASSES is 4")],
            "o/r#7": [escalated(WEEK_BEFORE)]})
        self.assertEqual(report["escalated"], 7)
        self.assertEqual(report["topReasons"], [{"reason": HUMAN, "count": 3},
                                                {"reason": SILENT.format("<sha>"), "count": 2},
                                                {"reason": ATTEMPTS, "count": 1}])

    def test_a_workitem_that_stays_waiting_is_counted_once(self):
        still = entry(THIS_WEEK, "reconcile", headSha="c2", **{"from": "human_waiting", "to": "human_waiting"})
        self.assertEqual(weekly({"o/r#1": [escalated(THIS_WEEK), still]})["escalated"], 1)

    def test_an_escalation_logged_without_a_reason_names_its_state_and_action(self):
        old = entry(THIS_WEEK, "gate_silent", **{"from": "reviewing", "to": "human_waiting"})
        self.assertEqual(weekly({"o/r#1": [old]})["topReasons"],
                         [{"reason": "human_waiting, reason not recorded (gate_silent)", "count": 1}])

    def test_new_candidates_are_those_that_became_one_this_week(self):
        candidates = [{"repo": "o/r", "fingerprint": "new", "candidateSince": THIS_WEEK},
                      {"repo": "o/r", "fingerprint": "old", "candidateSince": WEEK_BEFORE}]
        self.assertEqual([c["fingerprint"] for c in weekly({}, candidates)["newCandidates"]], ["new"])

    def test_only_lessons_flagged_not_working_are_listed(self):
        effect = {"o/r:a": {"repo": "o/r", "fingerprint": "a", "notWorking": True},
                  "o/r:b": {"repo": "o/r", "fingerprint": "b", "notWorking": False}}
        self.assertEqual([e["fingerprint"] for e in weekly({}, effect=effect)["notWorking"]], ["a"])


class CandidateSinceTest(unittest.TestCase):
    def test_a_candidate_dates_from_the_gates_first_report_on_its_second_workitem(self):
        store = Store(":memory:")
        for n in (1, 2):
            store.put(workitem(id=f"o/r#{n}", issueNumber=n))
        found = {"blocking": [{"fingerprint": "fp", "problem": "No test for the 401 path"}],
                 "from": "reviewing", "to": "changes_requested"}
        times = iter(["2026-09-20T00:00:00Z", "2026-09-30T00:00:00Z", "2026-10-02T00:00:00Z"])
        with mock.patch("factory_mcp.store.now", lambda: next(times)):
            for workitem_id in ("o/r#1", "o/r#2", "o/r#1"):
                store.log("reconcile", found, workitem_id)
        self.assertEqual(lessons.candidates(store, "o/r")[0]["candidateSince"], "2026-09-30T00:00:00Z")


class ReportToolTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay, self.store = FakeGitHub(), FakeRelay(), Store(":memory:")
        FakeGit(self.github).install(self)
        self.github.add_issue("o/r", 7)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.lessons_file = Path(tmp.name, "lessons.json")
        self.foreman = Foreman(self.store, self.github, Config(
            mcp_token="t", repos=frozenset({"o/r"}), lessons_file=str(self.lessons_file)), self.relay)
        self.report = tools.build(self.store, self.foreman)["factory_report"].fn

    def test_the_reason_a_workitem_went_to_a_person_is_logged_and_reported(self):
        self.foreman.intake("o/r", 7)
        for _ in range(2):
            wi = self.foreman.tick("o/r#7")["workItem"]
            self.relay.finish(wi["providerTaskId"], output="no result here")
            wi = self.foreman.tick("o/r#7")["workItem"]
        self.assertEqual(wi["state"], "blocked")
        logged = [h["detail"].get("escalation") for h in self.store.history("o/r#7")
                  if h["detail"].get("to") == "blocked"]
        self.assertEqual(logged, [wi["blockedOn"]])
        report = self.report({})
        self.assertEqual((report["escalated"], report["topReasons"]),
                         (1, [{"reason": wi["blockedOn"], "count": 1}]))

    def test_a_new_candidate_names_its_repo(self):
        for n in (1, 2):
            self.store.log("reconcile", {"blocking": [{"fingerprint": "fp", "problem": "p"}],
                                         "from": "reviewing", "to": "changes_requested"}, f"o/r#{n}")
            self.store.put(workitem(id=f"o/r#{n}", issueNumber=n))
        self.assertEqual([(c["repo"], c["fingerprint"]) for c in self.report({})["newCandidates"]],
                         [("o/r", "fp")])

    def test_a_broken_lessons_file_fails_the_report_and_says_why(self):
        self.lessons_file.write_text("not json")
        with self.assertRaisesRegex(ToolError, "lessons.json is not JSON"):
            self.report({})

    def test_an_unreadable_lessons_file_fails_the_report_and_names_it(self):
        self.lessons_file.write_text('{"version": 1, "lessons": []}')
        read_text = Path.read_text

        def unreadable(path, *args, **kwargs):  # the file system: the file is there, reading fails
            if path == self.lessons_file:
                raise PermissionError(13, "Permission denied", str(path))
            return read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", unreadable):
            with self.assertRaisesRegex(ToolError, "Permission denied"):
                self.report({})


if __name__ == "__main__":
    unittest.main()
