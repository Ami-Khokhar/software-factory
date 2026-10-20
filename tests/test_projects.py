"""The Project record (WO-01, ADR-0099): the store keeps a Project and reads it back, a stale
write loses, and `projects.problem` names the first limit the Lead's fields break."""
import tempfile
import unittest
from pathlib import Path

from factory_mcp import projects, tools
from factory_mcp.mcp import ToolError
from factory_mcp.store import Conflict, Store

AT = "2026-10-20T00:00:00Z"


def fields(**over):
    doc = {"id": "self-maintaining-apis", "repo": "acme/demo", "title": "Self-maintaining APIs",
           "goal": "Build self-maintaining APIs.", "blueprint": "1. The plan's modules.\n",
           "doneWhen": [{"claim": "a changed API spec opens a migration PR",
                         "command": "go test ./..."}],
           "budget": {"passes": 6, "deadline": "2026-11-01T00:00:00Z"},
           "approval": {"by": "owner", "words": "go.", "at": AT}}
    doc.update(over)
    return doc


class ProjectStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.dir.name) / "factory.sqlite3"))

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_a_created_project_reads_back_with_version_one(self):
        stored = self.store.create_project(projects.new(fields(), AT))
        self.assertEqual(stored["stateVersion"], 1)
        self.assertEqual(self.store.project("self-maintaining-apis"), stored)
        self.assertEqual(self.store.project("no-such-project"), None)
        self.assertEqual(self.store.projects(), [stored])
        self.assertEqual({key: stored[key] for key in
                          ("state", "passesUsed", "members", "pausedReason", "acceptanceItem")},
                         {"state": "active", "passesUsed": 0, "members": [], "pausedReason": None,
                          "acceptanceItem": None})

    def test_projects_read_in_creation_order(self):
        """`projects()` is every document in createdAt order, so the Lead sees the work as it began."""
        older = self.store.create_project(projects.new(fields(), "2026-10-20T00:00:00Z"))
        newer = self.store.create_project(projects.new(fields(id="other-project"),
                                                       "2026-10-21T00:00:00Z"))
        self.assertEqual([p["id"] for p in self.store.projects()],
                         [older["id"], newer["id"]])

    def test_a_second_project_with_the_same_id_conflicts(self):
        self.store.create_project(projects.new(fields(), AT))
        with self.assertRaisesRegex(Conflict, "self-maintaining-apis exists"):
            self.store.create_project(projects.new(fields(), AT))

    def test_a_stale_project_write_loses(self):
        first = self.store.create_project(projects.new(fields(), AT))
        self.store.put_project(dict(first, goal="Build self-maintaining APIs, better."))
        with self.assertRaises(Conflict):
            self.store.put_project(dict(first, goal="A write from an older read."))
        self.assertEqual(self.store.project("self-maintaining-apis")["goal"],
                         "Build self-maintaining APIs, better.")
        self.assertEqual(self.store.project("self-maintaining-apis")["stateVersion"], 2)

    def test_project_of_finds_the_project_that_lists_a_member(self):
        self.store.create_project(dict(projects.new(fields(), AT), members=["acme/demo#1"]))
        member = dict(projects.new(fields(id="split-project"), AT), members=["acme/demo#20"])
        self.store.create_project(member)
        self.assertEqual(self.store.project_of("acme/demo#20"), "split-project")
        self.assertEqual(self.store.project_of("acme/demo#1"), "self-maintaining-apis")
        self.assertEqual(self.store.project_of("acme/demo#404"), None)


class ProblemTest(unittest.TestCase):
    """`problem` names the first limit a field set breaks, in the schema's order."""

    CASES = [
        ("a two-character id", fields(id="ab"), "id must be 3 to 41"),
        ("a forty-three-character id", fields(id="0" * 42), "id must be 3 to 41"),
        ("an id with an upper-case letter", fields(id="Project-x"), "id must be 3 to 41"),
        ("a repo without an owner", fields(repo="demo"), "repo must be in the form owner/repo"),
        ("a repo without a separator", fields(repo="acme"), "repo must be in the form owner/repo"),
        ("an empty title", fields(title=""), "title must be 1 to 100"),
        ("a long title", fields(title="x" * 101), "title is 101 characters; the limit is 100"),
        ("a long goal", fields(goal="x" * 2001), "goal is 2001 characters; the limit is 2000"),
        ("a long blueprint", fields(blueprint="x" * 20001),
         "blueprint is 20001 characters; the limit is 20000"),
        ("six doneWhen checks", fields(doneWhen=[{"claim": "c", "command": "x"}] * 6),
         "doneWhen must be 1 to 5"),
        ("an empty doneWhen", fields(doneWhen=[]), "doneWhen must be 1 to 5"),
        ("a doneWhen check with an unknown key", fields(doneWhen=[
            {"claim": "c", "command": "x", "by": "n"}]), "doneWhen[1] carries keys"),
        ("a long doneWhen claim", fields(doneWhen=[
            {"claim": "x" * 301, "command": "x"}]), "doneWhen[1] claim is 301 characters"),
        ("a long doneWhen command", fields(doneWhen=[
            {"claim": "c", "command": "x" * 501}]), "doneWhen[1] command is 501 characters"),
        ("a second doneWhen check with a long claim", fields(doneWhen=[
            {"claim": "c", "command": "x"}, {"claim": "x" * 301, "command": "y"}]),
         "doneWhen[2] claim is 301 characters"),
        ("zero budget passes", fields(budget={"passes": 0, "deadline": AT}), "budget passes"),
        ("sixty-one budget passes", fields(budget={"passes": 61, "deadline": AT}), "budget passes"),
        ("a missing budget deadline", fields(budget={"passes": 1}), "budget deadline must be a date-time"),
        ("a budget with an unknown key", fields(budget={"passes": 1, "deadline": AT, "used": 0}),
         "budget carries keys"),
        ("an approval with an unknown key",
         fields(approval={"by": "owner", "words": "go.", "at": AT, "asked": True}),
         "approval carries keys"),
        ("a long approval by", fields(approval={"by": "x" * 101, "words": "go.", "at": AT}),
         "approval by is 101 characters"),
        ("a long approval words", fields(approval={"by": "owner", "words": "x" * 501, "at": AT}),
         "approval words is 501 characters"),
        ("a missing approval at", fields(approval={"by": "owner", "words": "go."}),
         "approval at must be a date-time"),
        ("the first limit wins: a bad id names itself, not a later field",
         fields(id="ab", repo="nope", title=""), "id must be 3 to 41"),
    ]

    def test_problem_names_the_first_limit_a_project_breaks(self):
        for case, fields_to_check, phrase in self.CASES:
            with self.subTest(case):
                self.assertIn(phrase, projects.problem(fields_to_check))
        self.assertEqual(projects.problem(fields()), None)

    def test_a_new_project_refuses_fields_that_break_a_limit(self):
        with self.assertRaisesRegex(ValueError, "blueprint is 20001 characters"):
            projects.new(fields(blueprint="x" * 20001), AT)
        with self.assertRaisesRegex(ValueError, "budget passes"):
            projects.new(fields(budget={"passes": 61, "deadline": AT}), AT)


class ProjectStatusToolTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.dir.name) / "factory.sqlite3"))
        self.tool = tools.build(self.store, None)["factory_project_status"]
        self.agent_tool = tools.build(self.store, None, agent=True)["factory_project_status"]

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_project_status_reads_one_and_all(self):
        """Day Desk's token lists the read tool, and one id reads one Project; no id lists them all
        in createdAt order, like the store."""
        self.assertIn("factory_project_status", tools.AGENT_TOOLS)
        self.assertEqual(self.agent_tool.effect, "read")
        older = self.store.create_project(projects.new(fields(), "2026-10-20T00:00:00Z"))
        newer = self.store.create_project(projects.new(fields(id="other-project"),
                                                       "2026-10-21T00:00:00Z"))
        self.assertEqual(self.tool.fn({"id": "self-maintaining-apis"}), {"project": older})
        self.assertEqual(self.tool.fn({}), {"projects": [older, newer]})
        with self.assertRaises(ToolError):
            self.tool.fn({"id": "no-such-project"})


if __name__ == "__main__":
    unittest.main()
