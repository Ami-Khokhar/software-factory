"""repos.json (ADR-0014): the repos a tool enabled join FACTORY_REPOS in the allowlist."""
import json
import tempfile
import unittest
from pathlib import Path

from factory_mcp.config import Config

ENTRY = {"name": "o/tool-added", "added_by": "amteshwar", "added_at": "2026-09-25T18:00:00Z"}


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
