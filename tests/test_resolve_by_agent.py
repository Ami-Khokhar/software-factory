"""Day Desk drives (ADR-0030): with the agent token it answers an escalation once, and after that
the owner decides. factory_status says which waiting WorkItems it may still answer."""
import unittest

from factory_mcp import tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.store import Store
from tests.fakes import FakeGitHub, FakeRelay, workitem


class ResolveByAgentTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.foreman = Foreman(self.store, FakeGitHub(), Config(mcp_token="t", repos=frozenset({"o/r"})), FakeRelay())
        self.store.put(workitem(state="blocked", blockedOn="verify exited 1", attempts=["a", "b"]))
        self.agent, self.owner = tools.build(self.store, self.foreman, agent=True), tools.build(self.store, self.foreman)

    def resolve(self, as_tools, resolution="retry", note="run the tests before you commit", by="day-desk"):
        return as_tools["factory_resolve_human"].fn({"id": "o/r#1", "resolution": resolution, "note": note, "by": by})

    def block_again(self):
        wi = self.store.get("o/r#1")
        self.store.put(dict(wi, state="blocked", blockedOn="verify exited 1 again"))

    def may_resolve(self):
        return next(w for w in self.owner["factory_status"].fn({})["workItems"] if w["id"] == "o/r#1")["agentMayResolve"]

    def test_the_agent_answers_an_escalation_once_and_then_the_owner_decides(self):
        self.assertTrue(self.may_resolve())
        self.assertEqual(self.resolve(self.agent)["workItem"]["state"], "ready")
        self.block_again()
        self.assertFalse(self.may_resolve())
        with self.assertRaisesRegex(ToolError, "Day Desk already resolved o/r#1 once, so the owner decides now"):
            self.resolve(self.agent, resolution="close")
        self.assertEqual(self.store.get("o/r#1")["state"], "blocked")
        self.assertEqual(self.resolve(self.owner, by="Ami-Khokhar")["workItem"]["state"], "ready")

    def test_the_owners_answers_do_not_use_up_the_agents_one(self):
        self.resolve(self.owner, by="Ami-Khokhar")
        self.block_again()
        self.assertTrue(self.may_resolve())
        self.assertEqual(self.resolve(self.agent)["workItem"]["state"], "ready")

    def test_the_tool_passes_owner_asked_only_through_the_agent_token(self):
        self.store.put(dict(self.store.get("o/r#1"), state="exhausted"))
        with self.assertRaisesRegex(ToolError, "used its budget of passes"):
            self.resolve(self.agent)  # the plain agent retry stays refused
        asked = {"id": "o/r#1", "resolution": "retry", "note": "owner: try again", "by": "day-desk",
                 "ownerAsked": True}
        self.assertEqual(self.agent["factory_resolve_human"].fn(asked)["workItem"]["state"], "ready")

    def test_an_owner_asked_retry_does_not_use_up_day_desks_one_answer(self):
        self.agent["factory_resolve_human"].fn({"id": "o/r#1", "resolution": "retry", "by": "day-desk",
                                                "note": "owner: try again", "ownerAsked": True})
        self.block_again()
        self.assertTrue(self.may_resolve())
        self.assertEqual(self.resolve(self.agent)["workItem"]["state"], "ready")

    def test_an_owner_asked_close_via_the_tool_frees_a_workitem_day_desk_answered(self):
        self.resolve(self.agent)  # its one answer
        self.block_again()
        asked = {"id": "o/r#1", "resolution": "close", "by": "day-desk", "note": "owner: close it",
                 "ownerAsked": True}
        self.assertEqual(self.agent["factory_resolve_human"].fn(asked)["workItem"]["state"], "cancelled")
        detail = [h for h in self.store.history("o/r#1") if h["action"] == "human_resolved"][-1]["detail"]
        self.assertTrue(detail["agent"] and detail["ownerAsked"])


if __name__ == "__main__":
    unittest.main()
