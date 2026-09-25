"""Factory repos: the check before a repo is enabled, and repos.json (ADR-0014)."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from factory_mcp import tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.repos import GATE_CONFIG, GATE_WORKFLOW, TEMPLATE, check, load
from factory_mcp.store import Store
from tests.fakes import FakeGitHub, FakeRelay

ENTRY = {"name": "o/tool-added", "added_by": "amteshwar", "added_at": "2026-09-25T18:00:00Z"}
REPO = "o/r"


def gated(github, repo=REPO, config='{"verify": "make test"}'):
    """A repo with the gate installed and the factory standard in its AGENTS.md, written with
    Windows line endings, which the check ignores."""
    github.contents.update({(repo, GATE_CONFIG): config, (repo, GATE_WORKFLOW): "on: pull_request",
                            (repo, "AGENTS.md"): TEMPLATE.read_text().replace("\n", "\r\n")})
    return github


class RepoCheckTest(unittest.TestCase):
    def test_a_repo_with_the_gate_passes_every_check(self):
        checks = check(gated(FakeGitHub()), REPO)
        self.assertEqual([(c["id"], c["ok"], c["fix"]) for c in checks],
                         [("repo", True, None), ("gate_config", True, None), ("gate_workflow", True, None),
                          ("agents_md", True, None)])
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
                  "has no .github/workflows/pr-gatekeeper.yml"),
                 ("no AGENTS.md", "contents", {(REPO, "AGENTS.md"): None}, "agents_md", "o/r has no AGENTS.md"),
                 ("an AGENTS.md without the standard", "contents", {(REPO, "AGENTS.md"): "# Notes\n"},
                  "agents_md", "has no factory-standard block"),
                 ("an old copy of the standard", "contents",
                  {(REPO, "AGENTS.md"): TEMPLATE.read_text().replace("Fail loudly.", "Fail quietly.")},
                  "agents_md", "differs from software-factory's templates/AGENTS.md")]
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


class EnableRepoTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        subprocess.run(["git", "init", "-q", "--bare", str(root / "remote.git")], check=True)
        self.manifest, self.checkout = root / "config" / "repos.json", root / "work" / "o" / "r"
        self.github = gated(FakeGitHub())
        self.foreman = Foreman(Store(":memory:"), self.github, Config(
            mcp_token="t", repos=frozenset(), repos_file=str(self.manifest),
            checkout_root=str(root / "work"), git_url=str(root / "remote.git")), FakeRelay())

    def tearDown(self):
        self.dir.cleanup()

    def test_a_repo_that_fails_a_check_is_refused_and_nothing_changes(self):
        self.github.contents[(REPO, GATE_WORKFLOW)] = None
        with self.assertRaisesRegex(ToolError, "o/r is not ready: o/r has no .github/workflows/"):
            self.foreman.add_repo(REPO, "amteshwar")
        self.assertEqual((self.github.labels, self.manifest.exists(), self.checkout.exists()), ({}, False, False))
        self.assertNotIn(REPO, self.foreman.config.repos)

    def test_enabling_takes_effect_at_once_and_twice_is_once(self):
        self.assertEqual(self.foreman.add_repo(REPO, "amteshwar"),
                         {"repo": REPO, "added": True, "checkout": str(self.checkout)})
        self.assertTrue((self.checkout / ".git").is_dir())
        self.assertEqual(self.github.labels[REPO], {"factory:ready", "factory:proposed"})
        self.assertIn(REPO, self.foreman.config.repos)  # no restart
        self.assertEqual([(e["name"], e["added_by"]) for e in load(str(self.manifest))], [(REPO, "amteshwar")])

        (self.checkout / "kept").write_text("a second clone would fail on this directory")
        self.assertFalse(self.foreman.add_repo(REPO, "amteshwar")["added"])
        self.assertEqual(len(load(str(self.manifest))), 1)

    def test_it_needs_a_manifest_to_write(self):
        self.foreman.config = Config(mcp_token="t", repos=frozenset())
        with self.assertRaisesRegex(ToolError, "FACTORY_REPOS_FILE is not set"):
            self.foreman.add_repo(REPO, "amteshwar")
        self.assertEqual(self.github.labels, {})


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
