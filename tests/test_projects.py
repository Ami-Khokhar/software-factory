"""The Project record (WO-01, ADR-0099): the store keeps a Project and reads it back, a stale
write loses, and `projects.problem` names the first limit the Lead's fields break. WO-02: the
owner's one yes starts a Project (ADR-0100) from the Lead's plan, through the Foreman and its
`factory_start_project` tool."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from factory_mcp import projects, tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.store import Conflict, Store
from tests.fakes import ISSUE_BODY, FakeGitHub, FakeRelay
from tests.test_actions import schema_errors

AT = "2026-10-20T00:00:00Z"
REPO = "o/r"  # FakeGitHub numbers new issues from 200
FENCED = ISSUE_BODY.replace("## Acceptance",
                            "Paths: src/*\n\n## Acceptance")


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

    def test_projects_created_at_the_same_at_read_back_in_id_order(self):
        """now() has 1s resolution, so two Projects can share a createdAt; the id breaks the tie
        and the read is deterministic."""
        self.store.create_project(projects.new(fields(id="project-b"), AT))
        self.store.create_project(projects.new(fields(id="project-a"), AT))
        self.assertEqual([p["id"] for p in self.store.projects()],
                         ["project-a", "project-b"])

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
        ("an id with a trailing newline", fields(id="acme-api\n"), "id must be 3 to 41"),
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


class StartProjectTest(unittest.TestCase):
    """The owner's one yes starts a Project (ADR-0100): the record is stored, the plan's issues
    open in order, and each opened WorkItem is a member with its `projectId` set."""

    def setUp(self):
        self.github = FakeGitHub()
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO})), FakeRelay())

    def lead_fields(self, **over):
        fields = {"id": "self-maintaining-apis", "repo": REPO, "title": "Self-maintaining APIs",
                  "goal": "Build self-maintaining APIs.", "blueprint": "1. The plan's modules.\n",
                  "doneWhen": [{"claim": "a changed API spec opens a migration PR",
                                "command": "go test ./..."}],
                  "budget": {"passes": 30, "deadline": "2026-11-01T00:00:00Z"}}
        fields.update(over)
        return fields

    def plan(self, *titles):
        return [{"title": title, "body": FENCED} for title in titles]

    def test_start_project_opens_the_plan_and_makes_each_issue_a_member(self):
        made = self.foreman.start_project(self.lead_fields(), self.plan("One", "Two", "Three"),
                                          "amteshwar", "go.")
        numbers = [item["issue"] for item in made["issues"]]
        self.assertEqual([self.github.issues[(REPO, number)]["title"] for number in numbers],
                         ["One", "Two", "Three"])
        self.assertEqual([(item["issue"], item["url"]) for item in made["issues"]],
                         [(number, f"https://github.com/{REPO}/issues/{number}")
                          for number in numbers])
        members = [f"{REPO}#{number}" for number in numbers]
        project = made["project"]
        self.assertEqual(schema_errors("project", project), [])
        self.assertEqual(project["members"], members)
        self.assertEqual(project["approval"], {"by": "amteshwar", "words": "go.", "at": project["approval"]["at"]})
        for member in members:
            wi = self.foreman.store.get(member)
            self.assertEqual((wi["state"], wi["projectId"]), ("ready", "self-maintaining-apis"))
            self.assertEqual(schema_errors("workitem", wi), [])
            self.assertTrue(self.foreman.store.acquire(member, "another-holder"))  # the join freed the lease
        self.assertEqual([row["detail"] for row in self.foreman.store.global_history()
                          if row["action"] == "project_started"],
                         [{"project": "self-maintaining-apis", "by": "amteshwar",
                           "members": members, "passes": 30}])
        self.assertEqual(self.foreman.store.project("self-maintaining-apis"), project)

    def test_start_project_without_owner_words_opens_nothing(self):
        for words in (None, "", "  "):
            self.setUp()
            with self.assertRaisesRegex(ToolError, "starting a project needs the owner's words "
                                                   "from the chat, quoted, as ownerWords"):
                self.foreman.start_project(self.lead_fields(), self.plan("One"), "amteshwar", words)
            self.assertEqual((self.github.issues, self.foreman.store.projects()), ({}, []))

    def test_start_project_with_a_bad_budget_opens_nothing(self):
        fields = self.lead_fields(budget={"passes": 61, "deadline": "2026-11-01T00:00:00Z"})
        with self.assertRaisesRegex(ToolError, r"budget passes must be 1 to 60\. No issue was opened"):
            self.foreman.start_project(fields, self.plan("One"), "amteshwar", "go.")
        self.assertEqual((self.github.issues, self.foreman.store.projects()), ({}, []))

    def test_a_project_id_that_exists_is_refused_before_any_issue_opens(self):
        self.foreman.start_project(self.lead_fields(), self.plan("One"), "amteshwar", "go.")
        self.assertEqual(list(self.github.issues), [(REPO, 200)])
        with self.assertRaisesRegex(ToolError, "project self-maintaining-apis exists; choose "
                                               "another id, or read it with factory_project_status"):
            self.foreman.start_project(self.lead_fields(), self.plan("Two"), "amteshwar", "go again.")
        self.assertEqual(list(self.github.issues), [(REPO, 200)])

    def test_a_plan_that_fails_stores_no_project(self):
        issues = self.plan("One") + [{"title": "Two", "body": "## Acceptance\n- it works"}]
        with self.assertRaisesRegex(ToolError, "issue 2 of the plan needs an '## Acceptance' list"):
            self.foreman.start_project(self.lead_fields(), issues, "amteshwar", "go.")
        self.assertEqual((self.github.issues, self.foreman.store.projects()), ({}, []))

    def test_an_issue_the_project_lists_joins_it_at_intake(self):
        self.foreman.start_project(self.lead_fields(), self.plan("One"), "amteshwar", "go.")
        project = self.foreman.store.project("self-maintaining-apis")
        self.foreman.store.put_project(dict(project, members=project["members"] + [f"{REPO}#300"]))
        self.github.add_issue(REPO, 300)
        self.assertEqual(self.foreman.intake(REPO, 300)["projectId"], "self-maintaining-apis")
        self.assertEqual([h["detail"] for h in self.foreman.store.history(f"{REPO}#300")
                          if h["action"] == "project_joined"],
                         [{"project": "self-maintaining-apis", "from": "ready", "to": "ready"}])

    def test_add_project_members_lists_ids_and_joins_the_taken_in_ones(self):
        self.foreman.start_project(self.lead_fields(), self.plan("One"), "amteshwar", "go.")
        self.github.add_issue(REPO, 300)
        self.foreman.intake(REPO, 300)
        project = self.foreman.add_project_members(
            "self-maintaining-apis", [f"{REPO}#300", f"{REPO}#301", f"{REPO}#200"])
        self.assertEqual(project["members"], [f"{REPO}#200", f"{REPO}#300", f"{REPO}#301"])
        self.assertEqual(self.foreman.store.get(f"{REPO}#300")["projectId"],
                         "self-maintaining-apis")
        self.assertEqual(self.foreman.store.get(f"{REPO}#301"), None)  # no WorkItem yet: listed only
        again = self.foreman.add_project_members("self-maintaining-apis", [f"{REPO}#300"])
        self.assertEqual(again["members"], [f"{REPO}#200", f"{REPO}#300", f"{REPO}#301"])
        with self.assertRaisesRegex(ToolError, "No project other-project"):
            self.foreman.add_project_members("other-project", [f"{REPO}#300"])

    def test_start_project_builds_members_and_the_record_from_the_resolved_repo(self):
        """The caller may spell the repo in another case than the allowlist does: the stored repo
        and every member id use the spelling start_plan resolved, so the joins name WorkItems the
        factory actually runs, not dead ids."""
        foreman = Foreman(Store(":memory:"), FakeGitHub(),
                          Config(mcp_token="t", repos=frozenset({"Acme/Widgets"})), FakeRelay())
        fields = self.lead_fields(repo="acme/widgets", id="resolved-spelling")
        made = foreman.start_project(fields, self.plan("One"), "amteshwar", "go.")
        self.assertEqual(fields["repo"], "acme/widgets")  # the caller's dict keeps its own spelling
        self.assertEqual((made["project"]["repo"], made["project"]["members"]),
                         ("Acme/Widgets", ["Acme/Widgets#200"]))
        self.assertEqual(foreman.store.get("Acme/Widgets#200")["projectId"], "resolved-spelling")
        self.assertEqual(foreman.store.get("acme/widgets#200"), None)
        self.assertEqual([row["detail"]["members"] for row in foreman.store.global_history()
                          if row["action"] == "project_started"], [["Acme/Widgets#200"]])

    def test_the_intake_join_waits_under_the_workitems_lease_and_refuses_when_it_stays_busy(self):
        """The intake-side join is the lease path, not a bare write: a tick holding the WorkItem's
        lease makes intake wait through the 5 one-second retries and then refuse loud, with the
        projectId still unset and no uncaught Conflict escaping the intake."""
        self.foreman.start_project(self.lead_fields(), self.plan("One"), "amteshwar", "go.")
        project = self.foreman.store.project("self-maintaining-apis")
        self.foreman.store.put_project(dict(project, members=project["members"] + [f"{REPO}#300"]))
        self.github.add_issue(REPO, 300)
        self.foreman.store.acquire(f"{REPO}#300", "a-tick")
        sleeps = []
        with mock.patch("factory_mcp.foreman.time.sleep", side_effect=lambda s: sleeps.append(s)):
            with self.assertRaisesRegex(ToolError, "o/r#300 is being ticked and its lease stayed "
                                                   "busy through 5 retries"):
                self.foreman.intake(REPO, 300)
        self.assertEqual(sleeps, [1] * 5)
        self.assertEqual(self.foreman.store.get(f"{REPO}#300")["projectId"], None)

    def test_the_intake_join_joins_during_a_retry_once_the_lease_frees(self):
        """A tick releases while the join waits: the retry takes the lease and completes the join
        inside the same intake call, which returns the joined WorkItem."""
        self.foreman.start_project(self.lead_fields(), self.plan("One"), "amteshwar", "go.")
        project = self.foreman.store.project("self-maintaining-apis")
        self.foreman.store.put_project(dict(project, members=project["members"] + [f"{REPO}#300"]))
        self.github.add_issue(REPO, 300)
        self.foreman.store.acquire(f"{REPO}#300", "a-tick")
        with mock.patch("factory_mcp.foreman.time.sleep",
                        side_effect=lambda _s: self.foreman.store.release(f"{REPO}#300", "a-tick")):
            wi = self.foreman.intake(REPO, 300)
        self.assertEqual(wi["projectId"], "self-maintaining-apis")
        self.assertEqual([h["detail"] for h in self.foreman.store.history(f"{REPO}#300")
                          if h["action"] == "project_joined"],
                         [{"project": "self-maintaining-apis", "from": "ready", "to": "ready"}])

    def test_start_project_resumes_a_join_that_died_without_opening_the_plan_again(self):
        """A join that exhausted its retries raised the ToolError whose advice ('call the tool
        again') is now true: the same call, same id, same `by` and same quoted ownerWords, finds
        its own Project, opens no second plan, finishes the joins, and names the start once."""
        self.foreman.store.acquire(f"{REPO}#200", "a-tick")
        with mock.patch("factory_mcp.foreman.time.sleep"):
            with self.assertRaisesRegex(ToolError, "o/r#200 is being ticked"):
                self.foreman.start_project(self.lead_fields(), self.plan("One", "Two"),
                                           "amteshwar", "go.")
        self.assertEqual(list(self.github.issues), [(REPO, 200), (REPO, 201)])  # the plan stands
        project = self.foreman.store.project("self-maintaining-apis")
        self.assertEqual(project["members"], [f"{REPO}#200", f"{REPO}#201"])
        self.assertEqual((self.foreman.store.get(f"{REPO}#200")["projectId"],
                          self.foreman.store.get(f"{REPO}#201")["projectId"]), (None, None))
        started = [row for row in self.foreman.store.global_history()
                   if row["action"] == "project_started"]
        self.assertEqual(started, [])  # the first call raised before naming the start
        self.foreman.store.release(f"{REPO}#200", "a-tick")
        made = self.foreman.start_project(self.lead_fields(), self.plan("One", "Two"),
                                          "amteshwar", "go.")
        self.assertEqual(made["issues"], [])
        self.assertEqual([(self.foreman.store.get(f"{REPO}#200")["projectId"],
                           self.foreman.store.get(f"{REPO}#201")["projectId"])],
                         [("self-maintaining-apis", "self-maintaining-apis")])
        self.assertEqual([row["detail"] for row in self.foreman.store.global_history()
                          if row["action"] == "project_started"],
                         [{"project": "self-maintaining-apis", "by": "amteshwar",
                           "members": [f"{REPO}#200", f"{REPO}#201"], "passes": 30}])

    def test_status_names_each_workitems_project(self):
        self.foreman.start_project(self.lead_fields(), self.plan("One", "Two"), "amteshwar", "go.")
        self.github.add_issue(REPO, 300)
        self.foreman.intake(REPO, 300)  # the Project lists no WorkItem for #300: it belongs to no Project
        rows = {row["id"]: row for row in tools.build(
            self.foreman.store, self.foreman)["factory_status"].fn({})["workItems"]}
        self.assertEqual((rows[f"{REPO}#200"]["projectId"], rows[f"{REPO}#201"]["projectId"]),
                         ("self-maintaining-apis", "self-maintaining-apis"))
        self.assertEqual(rows[f"{REPO}#300"]["projectId"], None)


class StartProjectToolTest(unittest.TestCase):
    """`factory_start_project` is a write tool Day Desk's token lists, and it names exactly the
    keys it takes; anything else or a missing one is refused with the key list."""

    def setUp(self):
        self.github = FakeGitHub()
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO})), FakeRelay())
        self.tool = tools.build(self.foreman.store, self.foreman)["factory_start_project"]

    def lead_args(self, **over):
        args = {"id": "self-maintaining-apis", "repo": REPO, "title": "Self-maintaining APIs",
                "goal": "Build self-maintaining APIs.", "blueprint": "1. The plan's modules.\n",
                "doneWhen": [{"claim": "a changed API spec opens a migration PR",
                              "command": "go test ./..."}],
                "budget": {"passes": 30, "deadline": "2026-11-01T00:00:00Z"},
                "issues": [{"title": "One", "body": FENCED}],
                "ownerWords": "go.", "by": "amteshwar"}
        args.update(over)
        return args

    def test_the_agent_token_lists_the_write_tool(self):
        self.assertIn("factory_start_project", tools.AGENT_TOOLS)
        agent = tools.build(self.foreman.store, self.foreman, agent=True)
        self.assertIn("factory_start_project", agent)
        self.assertFalse(agent["factory_start_project"].listing()["annotations"]["readOnlyHint"])

    def test_the_tool_schema_accepts_the_leads_arguments(self):
        from jsonschema import Draft202012Validator
        self.assertEqual([e.message for e in
                          Draft202012Validator(self.tool.input_schema).iter_errors(self.lead_args())], [])

    def test_the_tool_refuses_unknown_or_missing_keys_with_the_key_list(self):
        for args in (self.lead_args(extra="no"), {k: v for k, v in self.lead_args().items() if k != "ownerWords"}):
            with self.assertRaisesRegex(ToolError, "factory_start_project requires exactly the keys "):
                self.tool.fn(args)
        self.assertEqual((self.github.issues, self.foreman.store.projects()), ({}, []))

    def test_the_tool_starts_the_project_and_passes_owner_words_through(self):
        made = self.tool.fn(self.lead_args(issues=[{"title": "One", "body": FENCED},
                                                   {"title": "Two", "body": FENCED, "after": []}]))
        self.assertEqual(made["project"]["members"], [f"{REPO}#200", f"{REPO}#201"])
        self.assertEqual(made["project"]["approval"]["words"], "go.")
        self.assertEqual(self.foreman.store.get(f"{REPO}#201")["projectId"], "self-maintaining-apis")


if __name__ == "__main__":
    unittest.main()
