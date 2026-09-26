"""Factory repos: the check before a repo is enabled, and repos.json (ADR-0014)."""
import json
import tempfile
import unittest
from pathlib import Path

from factory_mcp import tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.repos import GATE_CONFIG, GATE_WORKFLOW, check
from factory_mcp.store import Store
from tests.fakes import FakeGitHub, FakeRelay

ENTRY = {"name": "o/tool-added", "added_by": "amteshwar", "added_at": "2026-09-25T18:00:00Z"}
REPO = "o/r"


def gated(github, repo=REPO, config='{"verify": "make test"}'):
    github.contents.update({(repo, GATE_CONFIG): config, (repo, GATE_WORKFLOW): "on: pull_request"})
    return github


class RepoCheckTest(unittest.TestCase):
    def test_a_repo_with_the_gate_passes_every_check(self):
        checks = check(gated(FakeGitHub()), REPO)
        self.assertEqual([(c["id"], c["ok"], c["fix"]) for c in checks],
                         [("repo", True, None), ("gate_config", True, None), ("gate_workflow", True, None)])
        self.assertEqual(checks[1]["detail"], "verify: make test")

    def test_each_gap_fails_its_check_and_says_how_to_fix_it(self):
        cases = [("no such repo", "repos", {REPO: None}, "repo", "no repo o/r"),
                 ("archived", "repos", {REPO: {"archived": True}}, "repo", "o/r is archived"),
                 ("a token that cannot push", "repos", {REPO: {"permissions": {"push": False}}}, "repo",
                  "cannot push"),
                 ("no gate config", "contents", {(REPO, GATE_CONFIG): None}, "gate_config",
                  "has no .github/pr-gatekeeper.json"),
                 ("no verify command", "contents", {(REPO, GATE_CONFIG): '{"max_rounds": 3}'}, "gate_config",
                  "has no verify command"),
                 ("a gate config that is not JSON", "contents", {(REPO, GATE_CONFIG): "verify: make"},
                  "gate_config", "has no verify command"),
                 ("no gate workflow", "contents", {(REPO, GATE_WORKFLOW): None}, "gate_workflow",
                  "has no .github/workflows/pr-gatekeeper.yml")]
        for case, attribute, changes, check_id, detail in cases:
            with self.subTest(case):
                github = gated(FakeGitHub())
                getattr(github, attribute).update(changes)
                failed = [c for c in check(github, REPO) if not c["ok"]]
                self.assertEqual([c["id"] for c in failed], [check_id])
                self.assertRegex(failed[0]["detail"], detail)
                self.assertTrue(failed[0]["fix"])

    def test_the_tool_says_whether_the_repo_is_ready_and_enabled(self):
        foreman = Foreman(Store(":memory:"), gated(FakeGitHub()), Config(mcp_token="t", repos=frozenset({REPO})),
                          FakeRelay())
        found = tools.build(foreman.store, foreman)
        self.assertEqual({key: value for key, value in found["factory_repo_check"].fn({"repo": REPO}).items()
                          if key != "checks"}, {"repo": REPO, "enabled": True, "ready": True})
        other = found["factory_repo_check"].fn({"repo": "o/other"})
        self.assertEqual((other["enabled"], other["ready"]), (False, False))
        with self.assertRaisesRegex(ToolError, "owner/repo"):
            found["factory_repo_check"].fn({"repo": "o/r/../x"})
        self.assertEqual(found["factory_status"].fn({})["repos"], [REPO])


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name, "repos.json")

    def tearDown(self):
        self.dir.cleanup()

    def config(self, text, **env):
        self.path.write_text(text)
        return Config.from_env(dict(env, FACTORY_MCP_TOKEN="t", FACTORY_REPOS_FILE=str(self.path)))

    def test_the_allowlist_is_factory_repos_and_the_manifest(self):
        config = self.config(json.dumps({"version": 1, "repos": [ENTRY]}), FACTORY_REPOS="o/env-listed")
        self.assertEqual(config.repos, frozenset({"o/env-listed", "o/tool-added"}))
        self.path.unlink()
        without = Config.from_env({"FACTORY_MCP_TOKEN": "t", "FACTORY_REPOS": "o/env-listed",
                                   "FACTORY_REPOS_FILE": str(self.path)})
        self.assertEqual(without.repos, frozenset({"o/env-listed"}))

    def test_a_malformed_manifest_stops_the_start_with_the_reason(self):
        cases = [("no version", {"repos": [ENTRY]}, 'needs "version": 1'),
                 ("a bad name", {"version": 1, "repos": [dict(ENTRY, name="o/r/x")]}, "form owner/repo"),
                 ("an unknown field", {"version": 1, "repos": [dict(ENTRY, workers=["pi"])]},
                  "o/tool-added: unknown field workers"),
                 ("a login that is not a string", {"version": 1, "repos": [dict(ENTRY, added_by=7)]},
                  "added_by must be a string"),
                 ("a repo named twice", {"version": 1, "repos": [ENTRY, dict(ENTRY, name="O/Tool-Added")]},
                  "O/Tool-Added is listed twice")]
        for case, doc, reason in cases:
            with self.subTest(case), self.assertRaisesRegex(ValueError, reason):
                self.config(json.dumps(doc))
        with self.assertRaisesRegex(ValueError, "repos.json is not JSON"):
            self.config("{not json")


if __name__ == "__main__":
    unittest.main()
