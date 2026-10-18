"""Autonomous recovery V1 (ADR-0081): a timeout moves the next attempt to another model family
before the two-strike block, a block is deferred while the policy still has an untried worker, an
accepted relay task the relay lost is re-queued without a strike, and an escalation is an incident
that re-arms only when its material facts change."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from factory_mcp import patches, tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.store import Store
from tests.fakes import FakeGit, FakeGitHub, FakeRelay, result_line, workitem
from tests.test_plan import FIRST, REPO, SECOND, plan

ID = "o/r#7"
POOL = (("pi-glm-factory", "local"), ("opencode-glm-factory", "local"), ("pi-deepseek-factory", "local"))
ROOT = Path(__file__).resolve().parent.parent
TIMED_OUT = "task timed_out: Agent exceeded 1800000 ms"
PARTIAL_WORK = ("A worker ran out of time. Its unfinished work is the commit you start from. "
                "Check each acceptance bullet against the code, not against any report. "
                "Keep what is right, fix or remove the rest, and finish.")


def timeout_result(order_id, base_sha, diff="diff --git a/src/app.py b/src/app.py\n"):
    """A Result from a pass that ran out of clock (ADR-0086): failed with the timeout error,
    carrying the partial patch the runner salvaged."""
    return "working...\n" + json.dumps({
        "schemaVersion": "factory.result/v1", "workOrderId": order_id, "status": "failed",
        "error": "the harness timed out",
        "patch": {"format": "unified", "diff": diff, "baseSha": base_sha},
        "evidence": [], "startedAt": "2026-09-24T00:00:00Z", "finishedAt": "2026-09-24T00:10:00Z"})


def schema_errors(name, doc):
    schema = json.loads((ROOT / "schemas" / f"{name}.schema.json").read_text())
    return [e.message for e in Draft202012Validator(schema).iter_errors(doc)]


class ForemanCase(unittest.TestCase):
    """A foreman over fakes with the helpers every case here needs."""

    workers = (("codex", "hosted"),)
    max_passes = 4

    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.git = FakeGit(self.github).install(self)
        self.github.add_issue(REPO, 7)
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO}), workers=self.workers,
                                      max_passes=self.max_passes), self.relay)
        self.foreman.intake(REPO, 7)

    def tick(self):
        wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual(schema_errors("workitem", wi), [])
        return wi

    def dispatch(self):
        wi = self.tick()
        self.assertEqual(wi["state"], "implementing")
        return wi, self.relay.tasks[wi["providerTaskId"]]

    def relay_timeout(self):
        """One worker pass the relay killed at its wall, and the tick that reads it."""
        wi, task = self.dispatch()
        self.relay.finish(task["id"], status="timed_out", error="Agent exceeded 1800000 ms")
        return self.tick()

    def recoveries(self, action=None):
        return [h["detail"] for h in self.foreman.store.history(ID) if h["action"] == "recovery"
                and (action is None or h["detail"]["action"] == action)]

    def results(self):
        return [h["detail"] for h in self.foreman.store.history(ID) if h["action"] == "recovery_result"]


class ProtectedGit(FakeGit):
    """FakeGit that keeps patches.apply's protected-path wall: a patch whose diff names a
    protected path is refused with PatchError, as the real push refuses it."""
    def apply(self, url, token, branch, base_sha, patch, message, force, protected=(), limits=None):
        if any(name in patch.get("diff", "") for name in protected):
            raise patches.PatchError(f"patch changes protected path {protected[0]}")
        return super().apply(url, token, branch, base_sha, patch, message, force, protected, limits)


class SalvageTest(ForemanCase):
    """ADR-0086: a timed-out pass's partial work is kept on a scratch branch and the next pass
    finishes it. These are runner timeouts — a failed Result line — not relay kills."""
    workers = POOL

    def test_a_migrated_legacy_wip_is_retired_when_the_workitem_is_reconciled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "factory.sqlite3")
            legacy = Store(path)
            legacy.put(workitem(id=ID, repo=REPO, issueNumber=7, state="blocked", branch="factory/7",
                                blockedOn="verify failed", attempts=["verify failed"],
                                wip={"branch": "factory-wip/7", "sha": "partial", "workOrder": "old-order",
                                     "moves": 1}))
            legacy.close()
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA user_version = 8")
            store = Store(path)
            github, relay = FakeGitHub(), FakeRelay()
            github.add_issue(REPO, 7)
            github.branches[(REPO, "factory-wip/7")] = "partial"
            foreman = Foreman(store, github, Config(mcp_token="t", repos=frozenset({REPO})), relay)

            wi = foreman.tick(ID)["workItem"]

            self.assertIsNone(wi["wip"])
            self.assertIn((REPO, "factory-wip/7"), github.deleted)
            store.close()

    def keep_partial(self, diff="diff --git a/src/app.py b/src/app.py\n"):
        """One pass that ran out of clock with partial work, and the tick that keeps it."""
        wi, task = self.dispatch()
        order = self.foreman.store.workorders(ID)[-1]
        base_sha = order.get("continuationSha") or (order.get("headSha") if order["kind"] == "fix"
                                                      else order["baseSha"])
        self.relay.finish(task["id"], output=timeout_result(wi["activeWorkOrderId"], base_sha, diff))
        return self.tick()

    def test_a_timed_out_pass_keeps_its_partial_work_for_the_next_pass(self):
        wi = self.keep_partial()
        self.assertEqual((self.git.pushes[-1]["branch"], self.git.pushes[-1]["base"]),
                         ("factory-wip/7", "base0"))
        self.assertEqual((wi["state"], wi["wip"]["branch"], wi["wip"]["sha"], wi["wip"]["moves"]),
                         ("ready", "factory-wip/7", "c1", 1))
        wi, _ = self.dispatch()  # the rescue pass bases itself on the kept work
        order = self.foreman.store.workorders(ID)[-1]
        self.assertEqual(order["baseSha"], "base0")
        self.assertEqual(order["continuationSha"], "c1")
        self.assertEqual(order["findingsToAddress"][0]["problem"], PARTIAL_WORK)

    def test_a_third_timeout_does_not_move_the_wip_again(self):
        self.keep_partial()  # salvage 1: wip moves to c1 (moves 1)
        self.keep_partial()  # its rescue times out too: salvage 2 (moves 2)
        wi = self.keep_partial()  # that rescue times out again: no third salvage push
        self.assertEqual((wi["state"], wi["wip"]["moves"], wi["wip"]["sha"]), ("blocked", 2, "c1"))
        self.assertEqual(len([p for p in self.git.pushes if p["branch"] == "factory-wip/7"]), 2)

    def test_an_implement_checkpoint_survives_a_stable_base_and_is_kept_for_the_running_rescue(self):
        self.keep_partial()  # the implement pass timed out; its checkpoint continues from base0
        wi, _ = self.dispatch()  # the rescue pass bases itself on the kept work
        self.assertEqual((wi["state"], wi["wip"]["sha"]), ("implementing", "c1"))
        self.assertNotIn((REPO, "factory-wip/7"), self.github.deleted)
        self.github.branches[(REPO, "main")] = "base1"  # the base moves while the rescue runs
        wi = self.tick()
        self.assertEqual((wi["state"], wi["wip"]["sha"]), ("implementing", "c1"))  # kept for the pass
        self.assertNotIn((REPO, "factory-wip/7"), self.github.deleted)
        self.relay.finish(wi["providerTaskId"], output=result_line(wi["activeWorkOrderId"]))
        wi = self.tick()
        self.assertIsNone(wi["wip"])  # the pass concluded; the checkpoint is retired

    def test_an_implement_checkpoint_is_retired_when_the_default_branch_moves_before_the_next_pass(self):
        self.keep_partial()
        self.github.branches[(REPO, "main")] = "base1"  # before the rescue pass starts
        wi = self.tick()
        self.assertIsNone(wi.get("wip"))
        self.assertIn((REPO, "factory-wip/7"), self.github.deleted)
        order = self.foreman.store.workorders(ID)[-1]
        self.assertIsNone(order.get("continuationSha"))  # a fresh pass, not a stale base

    def test_a_completed_result_retires_the_scratch_branch(self):
        self.keep_partial()
        wi, task = self.dispatch()
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
        wi = self.tick()
        self.assertIsNone(wi["wip"])
        self.assertEqual([h["detail"].get("branch") for h in self.foreman.store.history(ID)
                          if h["action"] == "wip_retired"], ["factory-wip/7"])

    def test_a_partial_touching_a_protected_path_is_not_kept(self):
        ProtectedGit(self.github).install(self)
        wi = self.keep_partial(
            diff="diff --git a/.github/pr-gatekeeper.json b/.github/pr-gatekeeper.json\n")
        self.assertIsNone(wi.get("wip"))
        self.assertEqual(self.git.pushes, [])  # nothing was pushed at all
        self.assertEqual(wi["state"], "ready")  # the failed attempt stands, salvage or not


class WorkerFailoverTest(ForemanCase):
    workers = POOL

    def test_a_first_timeout_moves_the_next_attempt_to_another_model_family(self):
        """A: pi-glm-factory and opencode-glm-factory are one family; the swap goes past the pair."""
        wi, _ = self.dispatch()
        self.assertEqual(wi["worker"], "pi-glm-factory")
        wi = self.relay_timeout()
        self.assertEqual(wi["state"], "ready")  # one strike: no block, no budget reset
        self.assertEqual(wi["attempts"], [f"pi-glm-factory: {TIMED_OUT}"])
        wi, _ = self.dispatch()
        self.assertEqual(wi["worker"], "pi-deepseek-factory")  # not the other glm worker
        self.assertEqual((wi["recovery"]["action"], wi["recovery"]["fromWorker"], wi["recovery"]["toWorker"]),
                         ("worker_swap", "pi-glm-factory", "pi-deepseek-factory"))
        (decision,) = self.recoveries("worker_swap")
        self.assertEqual(set(decision), {"recoveryId", "action", "category", "fromWorker", "toWorker",
                                         "budget", "succeeded", "escalationAvoided", "swapped"})
        self.assertEqual((decision["swapped"], decision["category"]),
                         (True, "timeout (unknown cause)"))
        self.assertEqual(decision["budget"], {"passesUsed": 1, "maxPasses": 4, "strikes": 1})
        self.assertIsNone(decision["succeeded"])
        self.assertIsNone(decision["escalationAvoided"])

    def test_the_outcome_of_a_swap_is_recorded_when_its_attempt_concludes(self):
        self.relay_timeout()  # pi-glm-factory
        wi, task = self.dispatch()  # pi-deepseek-factory, the swap
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
        wi = self.tick()
        self.assertEqual(wi["state"], "pr_open")
        self.assertIsNone(wi["recovery"])  # the open record is gone once its outcome is known
        (result,) = self.results()
        self.assertEqual(set(result), {"recoveryId", "action", "category", "fromWorker", "toWorker",
                                       "succeeded", "escalationAvoided"})
        self.assertEqual((result["action"], result["succeeded"], result["escalationAvoided"]),
                         ("worker_swap", True, True))
        self.assertEqual(result["recoveryId"], self.recoveries("worker_swap")[0]["recoveryId"])

    def test_two_timeouts_do_not_block_while_an_untried_worker_remains(self):
        """B: the block waits for the failover the policy already covers, once per budget."""
        self.relay_timeout()  # pi-glm-factory
        wi = self.relay_timeout()  # pi-deepseek-factory: two strikes, two families
        self.assertEqual(wi["state"], "ready")  # not blocked: opencode-glm-factory was never tried
        self.assertEqual(wi["attempts"], [f"pi-glm-factory: {TIMED_OUT}", f"pi-deepseek-factory: {TIMED_OUT}"])
        self.assertEqual((wi["recovery"]["action"], wi["recovery"]["toWorker"]),
                         ("block_deferred", "opencode-glm-factory"))
        (decision,) = self.recoveries("block_deferred")
        self.assertEqual(decision["category"], "timeout (unknown cause)")
        self.assertEqual(decision["budget"], {"passesUsed": 2, "maxPasses": 4, "strikes": 2})
        self.assertIsNone(decision["succeeded"])
        wi, task = self.dispatch()  # the deferred attempt runs on the third worker
        self.assertEqual(wi["worker"], "opencode-glm-factory")
        self.assertEqual(wi["recovery"]["action"], "block_deferred")  # its record is the one kept
        self.assertEqual(len(self.recoveries()), 2)  # no duplicate decision for the same attempt
        self.relay.finish(task["id"], status="timed_out", error="Agent exceeded 1800000 ms")
        wi = self.tick()
        self.assertEqual(wi["state"], "blocked")  # once per budget: the deferred attempt failed
        self.assertEqual(len(wi["attempts"]), 3)
        self.assertEqual(wi["blockedOn"], "3 worker attempts at the implement WorkOrder failed. "
                                          "Fix the cause named in attempts, then call factory_resolve_human.")
        self.assertEqual([(r["action"], r["succeeded"]) for r in self.results()],
                         [("worker_swap", False), ("block_deferred", False)])

    def test_the_deferral_never_lands_on_a_worker_from_the_gate_reviewers_family(self):
        """ADR-0085: the deferral still prefers outside the timed-out family (ADR-0081), but the
        gate reviewer's family is a hard wall: with no other family left, the block stands."""
        self.github.contents[(REPO, ".github/pr-gatekeeper.json")] = '{"reviewer_model": "deepseek-v4.1-flash"}'
        self.relay_timeout()  # pi-glm-factory; the swap falls back to the same family, never deepseek
        wi = self.relay_timeout()  # opencode-glm-factory: two strikes, and no untried worker outside deepseek
        self.assertEqual(wi["state"], "blocked")
        self.assertEqual(self.recoveries("block_deferred"), [])

    def test_two_timeouts_block_when_no_untried_worker_remains(self):
        """The block stands when routing's only choice is a worker that already failed here."""
        self.foreman.config = Config(mcp_token="t", repos=frozenset({REPO}), workers=POOL[:2])
        self.relay_timeout()  # pi-glm-factory
        wi = self.relay_timeout()  # opencode-glm-factory: fresh, but the same family
        self.assertEqual(wi["state"], "blocked")
        self.assertEqual(len(wi["attempts"]), 2)
        self.assertEqual((wi["incident"]["action"], wi["incident"]["causes"]),
                         ("attempt_failed", ["unknown"]))
        self.assertEqual(self.recoveries("block_deferred"), [])
        # the swap was asked for and was not available: one honest audit row
        self.assertEqual([r.get("swapped") for r in self.recoveries("worker_swap")], [False])

    def test_a_deferred_attempt_the_owner_routes_away_concludes_its_record(self):
        """The deferral's worker is a decision, not a binding: a Worker: line set before the deferred
        attempt runs moves it, and the deferral's record concludes as never run."""
        self.relay_timeout()  # pi-glm-factory
        wi = self.relay_timeout()  # pi-deepseek-factory
        self.assertEqual(wi["recovery"]["toWorker"], "opencode-glm-factory")
        self.foreman.set_worker(ID, ["pi-deepseek-factory"], "day-desk")
        wi = self.tick()
        self.assertEqual((wi["state"], wi["worker"]), ("implementing", "pi-deepseek-factory"))
        self.assertIsNone(wi["recovery"])  # nothing open survives into the attempt
        self.assertEqual([(r["action"], r["succeeded"]) for r in self.results()],
                         [("worker_swap", False), ("block_deferred", False)])
        self.assertEqual([r.get("swapped") for r in self.recoveries("worker_swap")], [True, False])

    def test_a_wait_that_ends_in_an_escalation_concludes_an_open_recovery_record(self):
        """A deferral a dependency escalation interrupts never runs: its record concludes and
        nothing pending is left on a person's queue."""
        self.relay_timeout()
        wi = self.relay_timeout()
        self.assertEqual(wi["recovery"]["action"], "block_deferred")
        self.github.add_issue(REPO, 200)  # the dependency, ended without a merge
        self.foreman.intake(REPO, 200)
        self.foreman.store.put(dict(self.foreman.store.get(f"{REPO}#200"), state="cancelled",
                                    terminalReason="replaced by o/r#300"))
        self.github.issues[(REPO, 7)]["body"] = "Depends on #200\n"
        wi = self.tick()
        self.assertEqual(wi["state"], "human_waiting")
        self.assertIn("this issue depends on #200", wi["blockedOn"])
        self.assertIsNone(wi["recovery"])
        self.assertEqual([(r["action"], r["succeeded"]) for r in self.results()],
                         [("worker_swap", False), ("block_deferred", False)])

    def test_a_dispatch_that_finds_no_worker_concludes_an_open_recovery_record(self):
        """A queue that ends in "no local worker" is not the attempt the deferral chose: the record
        concludes and clears, so no open recovery is left behind on a person's queue."""
        self.foreman.config = Config(mcp_token="t", repos=frozenset({REPO}),
                                     workers=(("codex", "hosted"),), local_only_repos=frozenset({REPO}))
        self.foreman.store.put(dict(self.foreman.store.get(ID), recovery={
            "id": "r-pending", "action": "block_deferred", "category": "timeout (unknown cause)",
            "fromWorker": "pi-glm-factory", "toWorker": "pi-deepseek-factory", "at": "2026-10-16T00:00:00Z"}))
        wi = self.tick()
        self.assertEqual((wi["state"], wi["recovery"]), ("human_waiting", None))
        self.assertIn("no local worker", wi["blockedOn"])
        self.assertEqual(self.results(), [{"recoveryId": "r-pending", "action": "block_deferred",
                                           "category": "timeout (unknown cause)",
                                           "fromWorker": "pi-glm-factory",
                                           "toWorker": "pi-deepseek-factory",
                                           "succeeded": False, "escalationAvoided": False}])

    def test_the_pass_budget_still_stops_the_recovery_attempt(self):
        """G: recovery never buys passes; the budget writes exhausted and the record concludes."""
        self.foreman.config = Config(mcp_token="t", repos=frozenset({REPO}), workers=POOL, max_passes=2)
        self.relay_timeout()  # pass 1
        wi = self.relay_timeout()  # pass 2: the deferral is written
        self.assertEqual((wi["state"], wi["recovery"]["action"]), ("ready", "block_deferred"))
        wi = self.tick()
        self.assertEqual(wi["state"], "exhausted")
        self.assertIn("2 worker passes used", wi["terminalReason"])
        self.assertIsNone(wi["recovery"])
        self.assertEqual((self.results()[-1]["action"], self.results()[-1]["succeeded"]),
                         ("block_deferred", False))
        self.assertEqual(len(self.foreman.store.workorders(ID)), 2)


class IncidentTest(ForemanCase):
    """The escalation is an incident: one answer each, a new one only on a material change."""

    max_passes = 8

    def two_plain_failures(self):
        for _ in range(2):
            _, task = self.dispatch()
            self.relay.finish(task["id"], status="failed", error="crashed")
            wi = self.tick()
        return wi

    def harness_timeout(self):
        """A runner Result that ran out of the clock, with the diagnostics that name the cause."""
        wi, task = self.dispatch()
        body = {"schemaVersion": "factory.result/v1", "workOrderId": wi["activeWorkOrderId"],
                "status": "failed", "patch": None, "evidence": [], "error": "the harness timed out",
                "diagnostics": {"elapsedMs": 990, "phases": [
                    {"phase": "harness execution", "elapsedMs": 970, "exitCode": 124, "tail": "step one"}]},
                "startedAt": "2026-09-24T00:00:00Z", "finishedAt": "2026-09-24T00:10:00Z"}
        self.relay.finish(task["id"], output="working...\n" + json.dumps(body))
        return self.tick()

    def may_resolve(self):
        rows = tools.build(self.foreman.store, self.foreman)["factory_status"].fn({})["workItems"]
        return next(r for r in rows if r["id"] == ID)["agentMayResolve"]

    def resolve(self, *args, **kw):
        return self.foreman.resolve_human(ID, *args, **kw)

    def test_a_material_change_re_arms_the_incident_once_and_an_unchanged_one_never_does(self):
        wi = self.two_plain_failures()
        self.assertEqual((wi["state"], wi["incident"]["id"], wi["incident"]["causes"],
                          wi["incident"]["agentAnswered"]), ("blocked", 1, ["unknown"], False))
        self.assertTrue(self.may_resolve())
        self.resolve("retry", "run the tests before you commit", "day-desk", agent=True)
        self.assertEqual(self.foreman.store.get(ID)["incident"]["agentAnswered"], True)

        for _ in range(2):
            self.harness_timeout()
        wi = self.tick()
        self.assertEqual(wi["state"], "blocked")
        self.assertEqual((wi["incident"]["id"], wi["incident"]["causes"]), (2, ["harness timeout"]))
        self.assertFalse(wi["incident"]["agentAnswered"])  # new facts: one fresh evaluation
        self.assertTrue(self.may_resolve())
        self.resolve("retry", "the second try timed out; split the work", "day-desk", agent=True)

        for _ in range(2):
            self.harness_timeout()
        wi = self.tick()
        self.assertEqual(wi["state"], "blocked")
        self.assertEqual(wi["incident"]["id"], 2)  # the same unresolved incident: never re-armed
        self.assertFalse(self.may_resolve())
        with self.assertRaisesRegex(ToolError, "already answered o/r#7's current escalation"):
            self.resolve("retry", "again", "day-desk", agent=True)
        self.assertEqual(self.foreman.store.get(ID)["state"], "blocked")
        self.assertEqual(self.resolve("retry", "the owner says try once more", "Ami-Khokhar")["state"], "ready")


class HumanGateTest(ForemanCase):
    def test_a_human_verdict_stays_for_the_owner_across_ticks_and_recoveries(self):
        """C: no tick, worker swap or incident rule moves a gate wait; the owner's answer does."""
        wi, task = self.dispatch()
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
        wi = self.tick()
        self.assertEqual((wi["state"], wi["prNumber"]), ("pr_open", 100))
        self.github.add_verdict(REPO, 100, "HUMAN", 1, "c1")
        wi = self.tick()
        self.assertEqual(wi["state"], "human_waiting")
        self.assertEqual(wi["blockedOn"], "pr-gatekeeper decided HUMAN; read the latest gate comment")
        self.assertEqual((wi["incident"]["action"], wi["incident"]["gateRound"]), ("reconcile", 1))
        version, incident, tasks = wi["stateVersion"], wi["incident"], len(self.relay.tasks)
        for _ in range(3):
            wi = self.tick()
            self.assertEqual((wi["state"], wi["stateVersion"], wi["incident"]), ("human_waiting", version, incident))
        self.assertEqual(len(self.relay.tasks), tasks)  # no worker ran: nobody ticked the gate wait back
        self.assertEqual(self.resolve_with_owner("regate"), "reviewing")  # the owner's way back

    def resolve_with_owner(self, resolution):
        return self.foreman.resolve_human(ID, resolution, "the gate can look again", "amteshwar")["state"]


class DependencyRecoveryTest(unittest.TestCase):
    """D and E: the dependency incident re-arms only when the dependency's facts change."""

    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        FakeGit(self.github).install(self)
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO})), self.relay)
        self.foreman.start_plan(REPO, plan("One", "Two", "Three"), "amteshwar")
        self.foreman.store.put(dict(self.foreman.store.get(FIRST), state="exhausted",
                                    terminalReason="4 passes"))  # #200 ended without a merge

    def one(self, workitem_id):
        wi = self.foreman.tick(workitem_id)["workItem"]
        self.assertEqual(schema_errors("workitem", wi), [])
        return wi

    def recoveries(self, workitem_id, action=None):
        return [h["detail"] for h in self.foreman.store.history(workitem_id) if h["action"] == "recovery"
                and (action is None or h["detail"]["action"] == action)]

    def may_resolve(self, workitem_id):
        rows = tools.build(self.foreman.store, self.foreman)["factory_status"].fn({})["workItems"]
        return next(r for r in rows if r["id"] == workitem_id)["agentMayResolve"]

    def test_an_unchanged_dependency_never_re_arms_and_a_changed_one_re_arms_once(self):
        wi = self.one(SECOND)
        self.assertEqual(wi["state"], "human_waiting")
        self.assertEqual(wi["blockedOn"], "this issue depends on #200, which ended exhausted: 4 passes. "
                                          "Edit or remove its 'Depends on #200' line, then retry; or close it")
        self.assertEqual((wi["incident"]["action"], wi["incident"]["dependency"], wi["incident"]["end"],
                          wi["incident"]["id"]), ("dependency_ended", 200, "ended exhausted: 4 passes", 1))
        self.assertTrue(self.may_resolve(SECOND))
        self.foreman.resolve_human(SECOND, "retry", "wait for it", "day-desk", agent=True)
        wi = self.one(SECOND)  # the wait resumes and meets the same ended dependency again
        self.assertEqual((wi["state"], wi["incident"]["id"], wi["incident"]["agentAnswered"]),
                         ("human_waiting", 1, True))
        version = wi["stateVersion"]
        for _ in range(2):
            wi = self.one(SECOND)
            self.assertEqual((wi["stateVersion"], wi["incident"]["id"]), (version, 1))  # a tick re-arms nothing
        self.assertFalse(self.may_resolve(SECOND))  # E: the same incident, answered, stays answered

        self.foreman.store.put(dict(self.foreman.store.get(FIRST), state="cancelled",
                                    terminalReason="replaced by o/r#300"))
        wi = self.one(SECOND)  # D: the end changed materially: one fresh evaluation
        self.assertEqual(wi["incident"]["id"], 2)
        self.assertEqual(wi["incident"]["end"], "ended cancelled: replaced by o/r#300")
        self.assertFalse(wi["incident"]["agentAnswered"])
        self.assertTrue(self.may_resolve(SECOND))
        (rearmed,) = self.recoveries(SECOND, "incident_rearmed")
        self.assertEqual((rearmed["dependency"], rearmed["end"], rearmed["succeeded"]),
                         (200, "ended cancelled: replaced by o/r#300", True))
        self.one(SECOND)
        self.assertEqual(self.recoveries(SECOND, "incident_rearmed"), [rearmed])  # once, not per tick

    def test_a_dependency_that_comes_back_resumes_the_workitem_without_a_person(self):
        wi = self.one(SECOND)
        self.assertEqual(wi["state"], "human_waiting")
        self.foreman.store.put(dict(self.foreman.store.get(FIRST), state="ready"))  # the owner retried #200
        wi = self.one(SECOND)
        self.assertEqual((wi["state"], wi["waitingOn"], wi["blockedOn"]), ("ready", 200, None))
        self.assertEqual((wi["incident"]["id"], wi["incident"]["end"], wi["incident"]["agentAnswered"]),
                         (2, "open", True))
        (resumed,) = self.recoveries(SECOND, "dependency_resumed")
        self.assertEqual((resumed["dependency"], resumed["end"], resumed["escalationAvoided"]),
                         (200, "open", True))

        self.github.issues[(REPO, 200)]["state"] = "closed"  # #200 merges
        self.github.add_pr(REPO, 99, "factory/200", "c9", state="closed", merged=True)
        wi = self.one(SECOND)
        self.assertEqual((wi["state"], wi["waitingOn"]), ("implementing", None))


class LegacyDependencyTextTest(ForemanCase):
    def test_only_the_factorys_own_escalation_text_names_a_dependency_to_re_check(self):
        """A WorkItem that escalated before the incident field is re-checked only when its blockedOn
        is the text the factory writes; a stray 'depends on #n' phrase re-arms nothing (ADR-0081)."""
        self.github.add_issue(REPO, 12)  # open: a stray match would wrongly resume the WorkItem
        self.foreman.store.put(dict(self.foreman.store.get(ID), state="human_waiting",
                                    blockedOn="the fix depends on #12, which a person must land first"))
        version = self.foreman.store.get(ID)["stateVersion"]
        wi = self.tick()
        self.assertEqual((wi["state"], wi["stateVersion"], wi.get("incident")),
                         ("human_waiting", version, None))
        self.assertEqual(self.recoveries(), [])
        # the factory's own text still reaches the dependency, and an unchanged end does nothing
        self.foreman.intake(REPO, 12)
        self.foreman.store.put(dict(self.foreman.store.get(f"{REPO}#12"), state="cancelled",
                                    terminalReason="replaced by o/r#9"))
        self.foreman.store.put(dict(self.foreman.store.get(ID), blockedOn=(
            "this issue depends on #12, which ended cancelled: replaced by o/r#9. Edit or remove its "
            "'Depends on #12' line, then retry; or close it")))
        version = self.foreman.store.get(ID)["stateVersion"]
        wi = self.tick()
        self.assertEqual((wi["state"], wi["stateVersion"], wi.get("incident")),
                         ("human_waiting", version, None))  # unchanged: no record, no write
        self.assertEqual(self.recoveries(), [])


class RelayLossTest(ForemanCase):
    def test_a_relay_loss_never_charges_a_strike_and_is_bounded(self):
        """F: a verified relay loss re-queues the same WorkOrder, twice at most, without external writes."""
        wi, task = self.dispatch()
        order_id = wi["activeWorkOrderId"]
        for round_ in (1, 2):
            self.relay.restart()
            wi = self.tick()
            self.assertEqual((wi["state"], wi["attempts"], wi["infraRetries"]), ("dispatching", [], round_))
            self.assertEqual(wi["recovery"]["action"], "relay_lost")
            wi = self.tick()
            self.assertEqual(wi["state"], "implementing")
        # H: one WorkOrder, one task per idempotency key, nothing pushed or opened by the re-queue
        self.assertEqual([o["id"] for o in self.foreman.store.workorders(ID)], [order_id])
        self.assertEqual(list(self.relay.by_request), [order_id])
        self.assertEqual(self.git.pushes, [])
        self.assertEqual(self.github.pulls, [])
        decisions = self.recoveries("relay_lost")
        self.assertEqual(len(decisions), 2)
        decision = decisions[0]
        self.assertEqual(set(decision), {"recoveryId", "action", "category", "fromWorker", "toWorker",
                                         "budget", "succeeded", "escalationAvoided", "task", "retries"})
        self.assertEqual((decision["fromWorker"], decision["toWorker"], decision["category"]),
                         ("codex", "codex", "relay lost the task"))
        self.assertIsNone(decision["succeeded"])
        self.assertEqual(decision["retries"], 1)

        self.relay.restart()
        wi = self.tick()  # the third loss is past the bound: the ordinary failed attempt stands
        self.assertEqual((wi["state"], len(wi["attempts"])), ("ready", 1))
        self.assertIn("re-queued 2 times already", wi["attempts"][0])
        self.assertEqual(len(self.recoveries("relay_lost")), 2)
        self.assertEqual(self.results()[-1]["succeeded"], False)
        # a person's retry starts a fresh pass budget, and the infrastructure allowance with it
        wi, task = self.dispatch()
        self.relay.finish(task["id"], status="failed", error="the harness failed")
        self.assertEqual(self.tick()["state"], "blocked")
        wi = self.foreman.resolve_human(ID, "retry", "spend another budget", "Ami-Khokhar")
        self.assertEqual((wi["state"], wi["infraRetries"]), ("ready", 0))

    def test_a_pending_remote_verify_falls_back_to_the_ordinary_attempt(self):
        """A scratch branch cannot be reconciled after a loss, so the recovery stands down."""
        wi, _ = self.dispatch()
        self.foreman.store.put(dict(self.foreman.store.get(ID),
                                    remoteVerify={"branch": "factory-verify/7-1", "sha": "c9",
                                                  "startedAt": "2026-09-24T00:00:00Z"}))
        self.relay.restart()
        wi = self.tick()
        self.assertEqual((wi["state"], len(wi["attempts"])), ("ready", 1))
        self.assertIn("a remote verify was pending", wi["attempts"][0])
        self.assertIsNone(wi.get("infraRetries"))
        self.assertEqual(self.recoveries("relay_lost"), [])


if __name__ == "__main__":
    unittest.main()
