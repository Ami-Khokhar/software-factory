"""factory_replace_issue's own replacements (ADR-0064): a title-and-body entry opens the replacement
as a `factory:proposed` issue under a durable request-and-position marker, and a repeat of the same
journalled call verifies that marker and wording and adopts the exact issue a lost create response
already opened, so a replay never opens a duplicate. A proposal is never factory:ready, so nothing
can dispatch it, and the pause barrier holds the created numbers until the cleanup completes."""
import os
import tempfile
import unittest

from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.store import REQUEST_PHASES, Store
from tests.fakes import ISSUE_BODY, FakeGit, FakeGitHub, FakeRelay
from tests.test_dependencies import THIRD
from tests.test_plan import FIRST, REPO, SECOND, plan

ORIGINAL = SECOND  # 201, waiting on 200 (FIRST) after start_plan One, Two, Three
REQUEST = f"replace/{ORIGINAL}"
LAST = 202  # the direct dependent that must re-point to the last replacement
MY_NUMBER = int(SECOND.split("#")[1])  # 201, the original's issue number
UNRELATED = MY_NUMBER - 1  # 200, an open issue 201 waits on that 202 must keep  # an unrelated open issue 202 also waits on, kept by the rewiring


def spec(title):
    return {"title": title, "body": ISSUE_BODY}


class FlakyGitHub(FakeGitHub):
    """FakeGitHub that can lose one create_issue response after GitHub made the issue, or one
    close_issue call, so an operation stops with its writes partly done."""

    def __init__(self):
        super().__init__()
        self.fail_create = 0
        self.closes = []  # every (repo, number) close_issue reached GitHub with
        self.fail_close = None
        self.fail_edit = None  # (repo, number) of the dependent rewrite to fail on, once
        self.edit_mode = "stored"  # "stored": GitHub keeps the body but the response is lost;
                                   # "drifted": GitHub stores a drifted body and answers calmly;
                                   # "dropped": the connection dies before the body is stored

    def edit_issue(self, repo, number, body):
        if (repo, number) == self.fail_edit:
            self.fail_edit = None
            if self.edit_mode == "dropped":
                raise RuntimeError("github dropped the connection before the rewrite was stored")
            if self.edit_mode == "stored":
                super().edit_issue(repo, number, body)
                raise RuntimeError("github stored the rewrite and dropped the connection")
            return super().edit_issue(repo, number, body + "\nA line GitHub wrote on its own")
        return super().edit_issue(repo, number, body)

    def create_issue(self, repo, title, body, labels):
        lost = self.fail_create > 0
        if lost:
            self.fail_create -= 1
        issue = super().create_issue(repo, title, body, labels)
        if lost:
            raise RuntimeError("github made the issue and dropped the connection")
        return issue

    def close_issue(self, repo, number):
        self.closes.append((repo, number))
        if (repo, number) == self.fail_close:
            raise RuntimeError("github dropped the connection")
        return super().close_issue(repo, number)


class ReplaceCreationTest(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".sqlite")
        self.github = FlakyGitHub()
        self.foreman = Foreman(Store(self.path), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO})), FakeRelay())
        FakeGit(self.github).install(self)
        self.foreman.start_plan(REPO, plan("One", "Two", "Three"), "amteshwar")  # 201 waits on 200
        self.foreman.set_dependencies(THIRD, [201], "amteshwar")  # 202 waits on the original only
        self.foreman.store.put(dict(self.foreman.store.get(ORIGINAL), state="human_waiting",
                                    blockedOn="this issue depends on #199, which ended cancelled"))

    def tearDown(self):
        self.foreman.store.close()
        os.unlink(self.path)

    def restart(self):
        """The server was killed and came back: the store is durable, GitHub is not."""
        self.foreman.store.close()
        self.foreman = Foreman(Store(self.path), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO})), FakeRelay())

    def replace(self, items):
        return self.foreman.replace_issue(ORIGINAL, items, "amteshwar")

    def proposals(self):
        return sorted(issue["number"] for issue in self.github.labelled_issues(REPO, "factory:proposed"))

    def interrupted(self, items):
        """A replace whose last GitHub write (the original's close) fails: everything before it
        happened, the journal is at pushing, and the call raises."""
        self.github.fail_close = (REPO, 201)
        with self.assertRaisesRegex(RuntimeError, "dropped the connection"):
            self.replace(items)
        self.github.fail_close = None
        self.restart()

    def test_a_planned_replacement_is_opened_proposed_and_chained(self):
        """The factory opens the planned replacements itself, labelled factory:proposed only: the
        first waits where the original waited, the next on the one before it, and the dependent
        watches the created number; the store holds no WorkItem of them on its own."""
        self.replace([spec("Recover this"), spec("Then that")])
        first, second = self.proposals()
        marker = "<!-- factory replace replace/o/r#201 {} -->"
        self.assertEqual([self.github.issues[(REPO, first)]["labels"],
                          self.github.issues[(REPO, second)]["labels"]],
                         [[{"name": "factory:proposed"}], [{"name": "factory:proposed"}]])
        self.assertEqual(self.github.issues[(REPO, first)]["body"],
                         f"Depends on #200\n\n{ISSUE_BODY}\n{marker.format(1)}")
        self.assertEqual(self.github.issues[(REPO, second)]["body"],
                         f"Depends on #{first}\n\n{ISSUE_BODY}\n{marker.format(2)}")
        self.assertEqual(self.github.issues[(REPO, LAST)]["body"],
                         f"Depends on #{second}\n\n{ISSUE_BODY}")
        self.assertEqual([wi["state"] for wi in
                          (self.foreman.store.get(f"o/r#{n}") for n in (first, second)) if wi], [])
        self.assertEqual(self.foreman.store.active_pauses(), [])  # the cleanup completed

    def test_created_proposals_stay_proposed_and_do_not_dispatch(self):
        """No readiness arrives with the recovery: after a complete operation and a tick, a proposal
        is at most an intaken `planned` WorkItem, and no worker starts until a person approves."""
        self.replace([spec("Recover this")])
        self.foreman.tick()  # ready work of the plan goes; a proposal must not
        (number,) = self.proposals()
        wid = f"o/r#{number}"
        if self.foreman.store.get(wid):  # discovery intook it
            self.assertEqual(self.foreman.store.get(wid)["state"], "planned")
            self.assertFalse([h for h in self.foreman.store.history(wid)
                              if h["action"] in ("dispatch", "submit", "submitted")])
        self.assertEqual([False for task in self.foreman.relay.tasks.values()
                          if task["id"] and False], [])

    def test_a_lost_create_response_adopts_the_exact_issue(self):
        """GitHub made the issue and then the create response was lost, before anything was saved
        under the request: the retry finds the marker, verifies the wording, and finishes on the
        same issue number, opening nothing a second time."""
        self.github.fail_create = 1
        with self.assertRaisesRegex(RuntimeError, "dropped the connection"):
            self.replace([spec("Recover this")])
        (made,) = self.proposals()
        self.assertEqual(self.foreman.store.request(REQUEST)["phase"], REQUEST_PHASES[0])
        self.assertEqual(self.foreman.store.active_pauses(), [])
        self.replace([spec("Recover this")])
        self.assertEqual(self.proposals(), [made])
        self.assertEqual(self.foreman.store.request(REQUEST)["phase"], REQUEST_PHASES[2])
        self.assertEqual((self.foreman.store.get(ORIGINAL)["state"],
                          self.foreman.store.active_pauses()), ("cancelled", []))

    def test_after_a_restart_the_barrier_holds_and_the_resume_adopts(self):
        """A kill after the creates and the edits leaves the barrier over the created numbers too:
        a restart ticks the store, the proposals stay proposed and still, and the same call resumes
        on them with no duplicate, then releases."""
        self.interrupted([spec("Recover this"), spec("Then that")])
        held = self.foreman.store.active_pauses()
        created = sorted(int(row["workItemId"].split("#")[1]) for row in held
                         if row["role"] == "replacement")
        self.assertEqual(len(created), 2, f"the barrier did not hold the created numbers: {held}")
        self.assertEqual(self.foreman.store.request(REQUEST)["phase"], REQUEST_PHASES[1])
        self.foreman.tick()  # ready work of the plan goes; the proposals must not
        self.assertTrue(self.foreman.store.active_pauses(), "a restart did not keep the barrier")
        for wid in [row["workItemId"] for row in self.foreman.store.active_pauses()]:
            stored = self.foreman.store.get(wid)
            if stored:
                self.assertNotIn(stored["state"], ("dispatching", "implementing"))
        self.assertEqual(self.proposals(), created)  # ticks opened nothing new
        self.replace([spec("Recover this"), spec("Then that")])  # the resume adopts and completes
        self.assertEqual((self.foreman.store.request(REQUEST)["phase"],
                          self.foreman.store.active_pauses()), (REQUEST_PHASES[2], []))
        self.assertEqual(self.proposals(), created)  # the resume adopted, it did not open again
        self.assertEqual([self.github.issues[(REPO, n)]["labels"] for n in created],
                         [[{"name": "factory:proposed"}], [{"name": "factory:proposed"}]])
        self.foreman.tick()  # proposals still need a person: no worker starts on their own
        self.assertEqual([self.github.issues[(REPO, n)]["labels"] for n in created],
                         [[{"name": "factory:proposed"}], [{"name": "factory:proposed"}]])
        for n in created:  # intaken or unknown to the store, a proposal dispatches nothing
            stored = self.foreman.store.get(f"o/r#{n}")
            if stored:
                self.assertEqual(stored["state"], "planned")
                self.assertFalse([h for h in self.foreman.store.history(f"o/r#{n}")
                                  if h["action"] in ("dispatch", "submit", "submitted")])
            self.assertEqual([t for t in self.foreman.relay.tasks.values()
                              if t.get("issue", {}).get("number") == n], [])  # an intake happened; dispatching did not

    def test_the_rewiring_reads_back_exactly_and_keeps_the_unrelated_edges(self):
        """Each rewired body is verified against what GitHub holds after the edit: the moved edge
        is exact, and the dependent's other Depends-on lines and text stand untouched."""
        self.foreman.set_dependencies(THIRD, [MY_NUMBER, UNRELATED], "amteshwar")  # 202 waits on 201, 200
        self.replace([spec("Recover this")])
        (replacement,) = self.proposals()
        self.assertEqual(self.github.issues[(REPO, LAST)]["body"],
                         f"Depends on #{replacement}\nDepends on #{UNRELATED}\n\n{ISSUE_BODY}")
        self.assertEqual(self.github.issues[(REPO, UNRELATED)]["body"], ISSUE_BODY)  # untouched
        self.assertEqual(self.github.issues[(REPO, MY_NUMBER)]["state"], "closed")
        # the original closes once: a duplicate write would leave a second failure point
        # after GitHub already closed it and before the journal reached done (ADR-0065)
        self.assertEqual(self.github.closes.count((REPO, MY_NUMBER)), 1)

    def test_a_drifted_edit_refuses_and_the_journalled_retry_replays_it(self):
        """GitHub stores something other than the written edge and answers calmly: the exact
        readback refuses, so the edge is never silently wrong. The barrier and the pushing journal
        hold, and the next run of the same call replays the write with the edges exact."""
        self.foreman.set_dependencies(THIRD, [MY_NUMBER, UNRELATED], "amteshwar")
        self.github.edit_mode = "drifted"
        self.github.fail_edit = (REPO, LAST)
        with self.assertRaisesRegex(ToolError, "does not read back"):
            self.replace([spec("Recover this")])
        self.assertEqual(self.foreman.store.get(ORIGINAL)["state"], "human_waiting")
        self.assertTrue(self.foreman.store.active_pauses(), "the drift cleared the barrier")
        self.assertEqual(self.foreman.store.request(REQUEST)["phase"], REQUEST_PHASES[1])
        self.replace([spec("Recover this")])  # the same journalled call replays the rewiring
        (replacement,) = self.proposals()
        self.assertEqual(self.github.issues[(REPO, LAST)]["body"],
                         f"Depends on #{replacement}\nDepends on #{UNRELATED}\n\n{ISSUE_BODY}\n"
                         "A line GitHub wrote on its own")
        self.assertEqual((self.foreman.store.get(ORIGINAL)["state"],
                          self.foreman.store.active_pauses()), ("cancelled", []))

    def test_a_lost_edit_response_replays_without_duplicate_or_lost_edges(self):
        """GitHub stored the rewired body and then lost the response: the run stops with the
        barrier held, and the retry reads the exact body back, rewrites nothing a second time,
        and lands on the same edges with no doubling and nothing dropped."""
        self.foreman.set_dependencies(THIRD, [MY_NUMBER, UNRELATED], "amteshwar")
        self.github.fail_edit = (REPO, LAST)  # "stored": applied, then the response was lost
        with self.assertRaisesRegex(RuntimeError, "dropped the connection"):
            self.replace([spec("Recover this")])
        (replacement,) = self.proposals()
        self.assertEqual(self.github.issues[(REPO, LAST)]["body"],
                         f"Depends on #{replacement}\nDepends on #{UNRELATED}\n\n{ISSUE_BODY}")
        self.assertEqual(self.foreman.store.request(REQUEST)["phase"], REQUEST_PHASES[1])
        before = self.github.issues[(REPO, LAST)]["body"]
        self.replace([spec("Recover this")])
        self.assertEqual(self.github.issues[(REPO, LAST)]["body"], before)
        self.assertEqual(self.foreman.store.request(REQUEST)["phase"], REQUEST_PHASES[2])

    def test_a_kill_between_rewirings_replays_the_remaining_edges(self):
        """The connection dies part-way through the dependent rewrites, before the body was
        stored: the journal is at pushing with the barrier held, and the resume finishes the
        remaining edges with no duplicate and nothing lost."""
        self.foreman.set_dependencies(THIRD, [MY_NUMBER, UNRELATED], "amteshwar")
        self.github.edit_mode = "dropped"
        self.github.fail_edit = (REPO, LAST)
        with self.assertRaisesRegex(RuntimeError, "dropped the connection"):
            self.replace([spec("Recover this")])
        created = self.proposals()
        self.assertEqual(len(created), 1)
        self.assertEqual(self.github.issues[(REPO, LAST)]["body"],
                         f"Depends on #{MY_NUMBER}\nDepends on #{UNRELATED}\n\n{ISSUE_BODY}")
        self.restart()
        self.assertTrue(self.foreman.store.active_pauses(), "the kill did not keep the barrier")
        self.replace([spec("Recover this")])  # the resume adopts the proposal and finishes
        (replacement,) = created
        self.assertEqual(self.proposals(), created)  # the resume adopted, it opened nothing again
        self.assertEqual(self.github.issues[(REPO, LAST)]["body"],
                         f"Depends on #{replacement}\nDepends on #{UNRELATED}\n\n{ISSUE_BODY}")
        self.assertEqual((self.foreman.store.request(REQUEST)["phase"],
                          self.foreman.store.active_pauses()), (REQUEST_PHASES[2], []))

    def test_mismatched_words_refuse(self):
        """An issue this request's marker names whose wording was changed after the fact refuses the
        call, and the call changes nothing on GitHub."""
        self.github.add_issue(REPO, 420, labels=("factory:proposed",),
                              body="Changed after the request\n\n<!-- factory replace replace/o/r#201 1 -->")
        before = {(key, value["body"]) for key, value in self.github.issues.items()}
        with self.assertRaisesRegex(ToolError, "420 carries this request's marker"):
            self.replace([spec("Recover this")])
        self.assertEqual({(key, value["body"]) for key, value in self.github.issues.items()}, before)
        self.assertEqual(self.foreman.store.get(ORIGINAL)["state"], "human_waiting")

    def test_two_issues_with_the_same_marker_refuse(self):
        """Two factory:proposed issues carry this request's marker: which one is the replacement
        cannot be coded here, so the call refuses and opens nothing."""
        for _ in range(2):
            self.github.create_issue(REPO, spec("Recover this")["title"],
                                     ISSUE_BODY + "\n\n<!-- factory replace replace/o/r#201 1 -->",
                                     ["factory:proposed"])
        first, second = self.proposals()
        with self.assertRaisesRegex(ToolError, f"appears on #{first}, #{second}"):
            self.replace([spec("Recover this")])

    def test_a_planned_replacement_is_checked_before_anything_is_created(self):
        """A planned item with an unusable body, its own Depends-on lines, or a protected path in it
        is refused before the first create call, like start_plan's items are."""
        self.github.fail_create = 1  # the check must win even though a create would fail too
        for bad, message in [
                ({"title": "", "body": ISSUE_BODY}, "the planned replacement at position 1"),
                ({"title": "Clean thing", "body": ISSUE_BODY + "\n\nDepends on #200"}, "Depends on"),
                ({"title": ".github/ thing", "body": ISSUE_BODY}, "which only a person changes")]:
            with self.subTest(bad=bad["title"]):
                with self.assertRaisesRegex(ToolError, message.replace("#", "")):
                    self.replace([bad])


if __name__ == "__main__":
    unittest.main()
