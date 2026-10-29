"""Context history, provenance and project/repo boundaries through public tools."""
import tempfile
import unittest
from pathlib import Path

from factory_mcp import projects, tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.store import Store
from tests.test_projects import fields, AT


class KnowledgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "factory.sqlite3")
        self.store = Store(self.path)
        self.config = Config(mcp_token="owner", repos=frozenset({"acme/demo", "acme/other"}))
        for id_ in ("project-one", "project-two"):
            self.store.create_project(projects.new(fields(id=id_), AT))
        self.bind()

    def bind(self):
        foreman = Foreman(self.store, None, self.config, None)
        self.owner = tools.build(self.store, foreman)
        self.agent = tools.build(self.store, foreman, agent=True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def put(self, **over):
        return self.owner["factory_memory_put"].fn({"repo": "acme/demo", "key": "explain",
            "kind": "preference", "text": "Use short visual explanations", "source": "owner:request-1", **over})

    def search(self, **over):
        return self.agent["factory_memory_search"].fn({"repo": "acme/demo", **over})["memories"]

    def test_versions_and_provenance_survive_restart_and_stale_writes_fail(self):
        first = self.put()
        second = self.put(version=1, status="approved", text="Use short explanations and diagrams",
                          source="owner:request-2")
        self.assertEqual(first["id"], second["id"])
        self.store.close()
        self.store = Store(self.path)
        self.bind()
        self.assertEqual(self.search(id=first["id"], version=1), [first])
        self.assertEqual(self.search(id=first["id"]), [second])
        with self.assertRaises(ToolError):
            self.put(version=1, text="stale instruction")
        self.assertEqual(self.search(approvedOnly=True), [second])

    def test_candidates_are_not_approved_and_agent_cannot_write(self):
        self.put()
        self.assertEqual(self.search(approvedOnly=True), [])
        self.assertNotIn("factory_memory_put", self.agent)
        self.assertFalse(self.owner["factory_memory_put"].listing()["annotations"]["readOnlyHint"])
        self.assertTrue(self.agent["factory_memory_search"].listing()["annotations"]["readOnlyHint"])

    def test_token_cost_preferences_are_not_mistaken_for_credentials(self):
        for index, content in enumerate(("Prefer efficient token use, lower token costs and security token configuration",
                "Store the token in the keychain, never in the repo", "Keep secrets out of logs",
                "Rotate the password before the release", "FACTORY_WALL_MS=1200000 fixed the timeout")):
            node = self.put(key=f"safe-{index}", text=content, status="approved")
            self.assertEqual(self.search(id=node["id"], approvedOnly=True), [node])

    def test_a_valid_kind_change_is_refused_without_changing_the_record(self):
        first = self.put()
        with self.assertRaisesRegex(ToolError, "kind cannot change"):
            self.put(version=1, kind="learning")
        self.assertEqual(self.search(id=first["id"]), [first])

    def test_preferences_precede_other_matching_knowledge(self):
        decision = self.put(key="diagram-choice", kind="decision", text="Use diagrams")
        preference = self.put(key="visual-style", text="Use diagrams")
        self.assertEqual(self.search(query="diagrams"), [preference, decision])

    def test_project_read_cannot_see_sibling_memory_or_links(self):
        shared = self.put()
        one = self.put(projectId="project-one", links=[{"id": shared["id"], "relation": "uses"}])
        two = self.put(projectId="project-two")
        self.assertEqual(self.search(), [shared])
        self.assertEqual({d["id"] for d in self.search(projectId="project-one")}, {shared["id"], one["id"]})
        self.assertEqual(self.search(projectId="project-one", id=two["id"]), [])
        for scope in ("project-one", ""):
            with self.subTest(scope=scope), self.assertRaises(ToolError):
                self.put(projectId=scope, key="leak", links=[{"id": two["id"], "relation": "uses"}])
        other = self.put(repo="acme/other")
        with self.assertRaises(ToolError):
            self.put(key="cross-repo", links=[{"id": other["id"], "relation": "uses"}])
        with self.assertRaises(ToolError):
            self.search(repo="acme/other", projectId="project-one")

    def test_invalid_updates_rollback_and_secrets_are_refused(self):
        first = self.put()
        for changes in ({"text": "API_KEY=private-value"}, {"text": "sk-" + "a1B2" * 8},
                        {"text": "ghp_" + "A" * 32}, {"text": "github_pat_" + "B" * 32},
                        {"text": "AKIA" + "C" * 16}, {"text": "PRIVATE_KEY=private-value"},
                        {"source": "password=hidden"},
                        {"kind": "unknown"}, {"status": "unknown"}, {"version": True},
                        {"links": [{"id": "missing", "relation": "uses"}]}, {"text": "x" * 1001}):
            with self.subTest(changes=changes), self.assertRaises(ToolError):
                self.put(**{"version": 1, **changes})
        self.assertEqual(self.search(id=first["id"]), [first])

    def test_unicode_search_and_literal_query_and_retirement(self):
        first = self.put(text="STRASSE fixes Straße", status="approved")
        self.assertEqual(self.search(query="straße FIXES"), [first])
        self.assertEqual(self.search(query="%"), [])
        self.put(version=1, status="retired")
        self.assertEqual(self.search(approvedOnly=True), [])
        self.assertEqual(self.search(id=first["id"], version=1), [first])
        for args in ({"repo": "not/enabled"}, {"limit": True}, {"limit": 21},
                     {"approvedOnly": "true"}, {"version": 1}, {"unknown": True}):
            with self.subTest(args=args), self.assertRaises(ToolError):
                self.search(**args)
