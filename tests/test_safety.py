"""Unattended-run safety: who may start work, one worker per checkout, and escalations reach a person."""
import json
import unittest

from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.github import GitHubError
from factory_mcp.mcp import ToolError
from factory_mcp.store import Store
from tests.fakes import FakeGitHub, FakeRelay

REPO, OTHER = "o/r", "o/s"


def foreman(github, relay, **config):
    return Foreman(Store(":memory:"), github, Config(mcp_token="t", repos=frozenset({REPO, OTHER}), **config),
                   relay)


class TrustedAuthorsTest(unittest.TestCase):
    def test_only_trusted_authors_reach_a_worker(self):
        github, relay = FakeGitHub(), FakeRelay()
        github.add_issue(REPO, 7, author="stranger")
        github.add_issue(REPO, 8)  # opened by the repo owner, trusted by default
        f = foreman(github, relay)
        with self.assertRaisesRegex(ToolError, "opened by stranger, who is not a trusted author"):
            f.intake(REPO, 7)
        f.tick()
        f.tick()
        self.assertIsNone(f.store.get("o/r#7"))
        self.assertEqual([h["action"] for h in f.store.history("o/r#7")], ["rebuild_skipped"])
        self.assertEqual(f.store.get("o/r#8")["state"], "implementing")

    def test_trusted_authors_setting_replaces_the_owner_default(self):
        config = Config.from_env({"FACTORY_MCP_TOKEN": "t", "FACTORY_REPOS": REPO,
                                  "FACTORY_TRUSTED_AUTHORS": "alice, bob"})
        self.assertEqual(config.trusted_authors, frozenset({"alice", "bob"}))
        self.assertTrue(config.trusts(REPO, "bob"))
        self.assertFalse(config.trusts(REPO, "o"))
        self.assertTrue(Config(mcp_token="t", repos=frozenset({REPO})).trusts(REPO, "o"))


class OneWorkerPerCheckoutTest(unittest.TestCase):
    def test_second_issue_in_a_repo_waits_for_the_first(self):
        github, relay = FakeGitHub(), FakeRelay()
        for number in (7, 8):
            github.add_issue(REPO, number)
        github.add_issue(OTHER, 9)
        f = foreman(github, relay)
        f.tick()
        states = {wi["id"]: wi["state"] for wi in f.store.all()}
        self.assertEqual(states, {"o/r#7": "implementing", "o/r#8": "ready", "o/s#9": "implementing"})

        relay.finish(f.store.get("o/r#7")["providerTaskId"], status="failed", error="boom")
        f.tick()  # 7 fails its attempt and goes back to ready; 8 may start only once 7 is not working
        busy = [wi["id"] for wi in f.store.all() if wi["repo"] == REPO and wi["state"] == "implementing"]
        self.assertEqual(len(busy), 1)

    def test_task_runs_in_the_repo_checkout(self):
        github, relay = FakeGitHub(), FakeRelay()
        github.add_issue(REPO, 7)
        f = foreman(github, relay, checkout_root="/work")
        f.intake(REPO, 7)
        wi = f.tick("o/r#7")["workItem"]
        self.assertEqual(relay.tasks[wi["providerTaskId"]]["cwd"], "/work/o/r")
        self.assertIsNone(Config(mcp_token="t", repos=frozenset()).checkout(REPO))


class EscalationTest(unittest.TestCase):
    def blocked(self, github):
        relay = FakeRelay()
        github.add_issue(REPO, 7)
        f = foreman(github, relay, owner="amteshwar")
        f.intake(REPO, 7)
        for _ in range(2):
            wi = f.tick("o/r#7")["workItem"]
            relay.finish(wi["providerTaskId"], status="failed", error="tests failed")
            wi = f.tick("o/r#7")["workItem"]
        self.assertEqual(wi["state"], "blocked")
        return f, wi

    def test_entering_blocked_comments_once_on_the_issue(self):
        github = FakeGitHub()
        f, wi = self.blocked(github)
        f.tick("o/r#7")
        self.assertEqual(len(github.posted), 1)
        repo, number, body = github.posted[0]
        self.assertEqual((repo, number), (REPO, 7))
        self.assertIn("`o/r#7` is `blocked`", body)
        self.assertIn("factory_resolve_human", body)
        self.assertTrue(body.endswith("@amteshwar"))

    def test_a_failed_comment_does_not_stop_the_transition(self):
        class NoComments(FakeGitHub):
            def comment(self, repo, number, body):
                raise GitHubError(403, "Resource not accessible")
        f, wi = self.blocked(NoComments())
        self.assertIn("notify_failed", [h["action"] for h in f.store.history("o/r#7")])

    def test_a_retry_says_why_the_last_attempt_failed(self):
        github, relay = FakeGitHub(), FakeRelay()
        github.add_issue(REPO, 7)
        f = foreman(github, relay)
        f.intake(REPO, 7)
        wi = f.tick("o/r#7")["workItem"]
        relay.finish(wi["providerTaskId"], status="failed", error="verify exited 1")
        self.assertEqual(f.tick("o/r#7")["workItem"]["state"], "ready")
        self.assertEqual(f.tick("o/r#7")["workItem"]["state"], "implementing")
        order = f.store.workorders("o/r#7")[-1]
        self.assertEqual(order["id"], "o/r#7:r0:implement:a1")
        self.assertEqual([x["fingerprint"] for x in order["findingsToAddress"]], ["previous-attempt"])
        self.assertIn("verify exited 1", order["findingsToAddress"][0]["problem"])
        self.assertIn("an earlier attempt failed", relay.tasks[f.store.get("o/r#7")["providerTaskId"]]["input"])
        self.assertIn("Do not push", json.dumps(relay.tasks[f.store.get("o/r#7")["providerTaskId"]]["input"]))

    def test_a_retry_of_a_blocked_workitem_carries_the_owners_note(self):
        github, relay = FakeGitHub(), FakeRelay()
        github.add_issue(REPO, 7)
        f = foreman(github, relay)
        f.intake(REPO, 7)
        for _ in range(2):  # two failed attempts before any PR block the WorkItem
            wi = f.tick("o/r#7")["workItem"]
            relay.finish(wi["providerTaskId"], status="failed", error="verify exited 1")
            wi = f.tick("o/r#7")["workItem"]
        self.assertEqual(wi["state"], "blocked")
        f.resolve_human("o/r#7", "retry", "Put the field in health(), not in the router", "o")
        wi = f.tick("o/r#7")["workItem"]
        self.assertEqual(wi["state"], "implementing")
        order = f.store.workorders("o/r#7")[-1]
        self.assertEqual([(x["fingerprint"], x["problem"]) for x in order["findingsToAddress"]],
                         [("human", "Put the field in health(), not in the router")])
        self.assertIn('fingerprint "human" is from the owner', relay.tasks[wi["providerTaskId"]]["input"])
        wi = f.tick("o/r#7")["workItem"]  # the note is used once: a later pass does not repeat it
        relay.finish(wi["providerTaskId"], status="failed", error="verify exited 2")
        f.tick("o/r#7")
        f.tick("o/r#7")
        self.assertEqual([x["fingerprint"] for x in f.store.workorders("o/r#7")[-1]["findingsToAddress"]],
                         ["previous-attempt"])


if __name__ == "__main__":
    unittest.main()
