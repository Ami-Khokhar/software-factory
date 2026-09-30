"""Whether the owner-approved lessons work: `metrics.lesson_effect` on stored WorkOrders, and an
autonomy output that does not change."""
import unittest

from factory_mcp.metrics import autonomy, lesson_effect
from tests.fakes import workitem

REPO = "o/r"
FP = "fp1"
KEY = f"{REPO}:{FP}"


def order(workitem_id, round_, kind="implement", lessons=(), findings=()):
    return {"id": f"{workitem_id}:r{round_}:{kind}:a0", "workItemId": workitem_id,
            "round": round_, "kind": kind, "repo": workitem_id.split("#")[0],
            "lessons": [{"fingerprint": fp, "text": f"avoid {fp}"} for fp in lessons],
            "findingsToAddress": [{"fingerprint": fp, "problem": "p"} for fp in findings]}


def lesson(fp=FP, repo=REPO):
    return {"repo": repo, "fingerprint": fp, "text": f"avoid {fp}",
            "approved_by": "amteshwar", "approved_at": "2026-09-27T00:00:00Z"}


class LessonEffectTest(unittest.TestCase):
    def test_a_lesson_that_held_is_reported_as_working(self):
        orders = [order("o/r#1", 0, lessons=[FP]), order("o/r#2", 0, lessons=[FP])]
        effect = lesson_effect([lesson()], lambda: orders)[KEY]
        self.assertEqual((effect["sent"], effect["recurred"], effect["notWorking"]), (2, 0, False))
        self.assertEqual(effect["workItems"], ["o/r#1", "o/r#2"])

    def test_a_recurrence_is_counted(self):
        """A later fix WorkOrder still carried the fingerprint: the gate reported it again after
        the lesson had ridden along."""
        orders = [order("o/r#1", 0, lessons=[FP]),
                  order("o/r#1", 1, kind="fix", findings=[FP]),
                  order("o/r#2", 0, lessons=[FP])]
        effect = lesson_effect([lesson()], lambda: orders)[KEY]
        self.assertEqual((effect["sent"], effect["recurred"]), (2, 1))
        self.assertFalse(effect["notWorking"])

    def test_a_recurrence_is_counted_when_the_fix_carries_the_lesson_too(self):
        """As the Foreman writes them: every WorkOrder after approval carries the lesson."""
        orders = [order("o/r#1", 0, lessons=[FP]),
                  order("o/r#1", 1, kind="fix", lessons=[FP], findings=[FP])]
        self.assertEqual(lesson_effect([lesson()], lambda: orders)[KEY]["recurred"], 1)

    def test_a_finding_before_the_lesson_is_not_a_recurrence(self):
        orders = [order("o/r#1", 0, kind="fix", findings=[FP]),
                  order("o/r#1", 1, kind="fix", lessons=[FP])]
        self.assertEqual(lesson_effect([lesson()], lambda: orders)[KEY]["recurred"], 0)

    def test_three_or_more_recurrences_flag_it_not_working(self):
        orders = []
        for n in range(4):
            orders.append(order(f"o/r#{n}", 0, lessons=[FP]))
            orders.append(order(f"o/r#{n}", 1, kind="fix", findings=[FP]))
        self.assertTrue(lesson_effect([lesson()], lambda: orders)[KEY]["notWorking"])

    def test_two_recurrences_do_not_flag_it(self):
        orders = []
        for n in range(2):
            orders.append(order(f"o/r#{n}", 0, lessons=[FP]))
            orders.append(order(f"o/r#{n}", 1, kind="fix", findings=[FP]))
        self.assertFalse(lesson_effect([lesson()], lambda: orders)[KEY]["notWorking"])

    def test_one_fingerprint_reported_once_per_lesson(self):
        self.assertEqual(list(lesson_effect([lesson(), lesson()], lambda: [])), [KEY])

    def test_another_repos_lessons_are_measured_too(self):
        orders = [order("other/r#1", 0, lessons=[FP])]
        effect = lesson_effect([lesson(repo="other/r")], lambda: orders)["other/r:" + FP]
        self.assertEqual(effect["repo"], "other/r")

    def test_one_fingerprint_in_two_repos_is_measured_per_repo(self):
        orders = [order("o/r#1", 0, lessons=[FP]), order("o/r#1", 1, kind="fix", findings=[FP]),
                  order("other/r#1", 0, lessons=[FP])]
        effect = lesson_effect([lesson(), lesson(repo="other/r")], lambda: orders)
        self.assertEqual((effect[KEY]["sent"], effect[KEY]["recurred"]), (1, 1))
        self.assertEqual((effect["other/r:" + FP]["sent"], effect["other/r:" + FP]["recurred"]), (1, 0))

    def test_no_lessons_gives_no_effect(self):
        self.assertEqual(lesson_effect([], lambda: [order("o/r#1", 0)]), {})


class AutonomyUnchangedTest(unittest.TestCase):
    def test_output_shape_is_unchanged(self):
        store = _store_with_history()
        workitems = [{"id": "o/r#1", "state": "merged", "terminalReason": None}]
        self.assertEqual(autonomy(workitems, store.history),
                         {"ended": 1, "autonomous": 1, "unknown": 0, "rate": 1.0})

    def test_the_fixture_history_gives_the_fixture_answer(self):
        import json
        from pathlib import Path
        from factory_mcp.store import Store
        fixture = json.loads((Path(__file__).parent / "fixtures" / "autonomy-history.json").read_text())
        store = Store(":memory:")
        for workitem_id, item in fixture["workItems"].items():
            for action, detail in item["history"]:
                store.log(action, detail, workitem_id)
        workitems = [{"id": i, "state": item["state"]} for i, item in fixture["workItems"].items()]
        self.assertEqual(autonomy(workitems, store.history), fixture["expected"])


def _store_with_history():
    from factory_mcp.store import Store
    store = Store(":memory:")
    wi = workitem(id="o/r#1")
    store.put(wi)
    store.log("result", {"commitSha": "c1"}, wi["id"])
    store.log("reconcile", {"headSha": "c1", "from": "pr_open", "to": "merged"}, wi["id"])
    return store


if __name__ == "__main__":
    unittest.main()
