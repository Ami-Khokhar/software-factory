"""The repo manifest: load, save, upsert, validation, and how it layers over the env config."""
import json
import tempfile
import unittest
from pathlib import Path

from factory_mcp import repos
from factory_mcp.config import Config

DOC = {"version": 1,
       "defaults": {"workers": ["pi-glm-factory:local"], "trusted_authors": ["Ami-Khokhar"],
                    "protected_paths": [".github/", "AGENTS.md"]},
       "repos": [{"name": "o/r", "risk": "local-only", "workers": ["pi:local", "codex:hosted"],
                  "trusted_authors": ["alice"], "gate": {"verify": "python3 -m unittest"}},
                 {"name": "o/off", "enabled": False}]}


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.dir.name) / "cfg" / "repos.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_file_is_empty(self):
        self.assertEqual(repos.load(self.path), repos.Manifest())

    def test_round_trip(self):
        manifest = repos.parse(DOC)
        self.assertEqual(manifest.enabled(), {"o/r"})
        self.assertEqual(manifest.local_only(), {"o/r"})
        self.assertEqual(manifest.repos["o/r"].workers, (("pi", "local"), ("codex", "hosted")))
        repos.save(self.path, manifest)
        self.assertEqual(repos.load(self.path), manifest)
        self.assertEqual([p.name for p in Path(self.path).parent.iterdir()], ["repos.json"])

    def test_documented_example_parses(self):
        example = Path(__file__).resolve().parent.parent / "docs" / "repos.example.json"
        self.assertEqual(repos.load(str(example)).enabled(), {"Ami-Khokhar/agent-relay"})

    def test_upsert_replaces_one_entry(self):
        repos.save(self.path, repos.parse(DOC))
        manifest = repos.upsert(self.path, repos.Repo(name="o/new", verify="make test"))
        self.assertEqual(set(manifest.repos), {"o/r", "o/off", "o/new"})
        manifest = repos.upsert(self.path, repos.Repo(name="o/new", risk="local-only"))
        self.assertEqual(repos.load(self.path).repos["o/new"].risk, "local-only")
        self.assertEqual(len(json.loads(Path(self.path).read_text())["repos"]), 3)

    def test_rejects_bad_documents(self):
        for doc in ({"version": 2}, {"version": 1, "repos": [{"name": "nope"}]},
                    {"version": 1, "repos": [{"name": "o/r", "risk": "yolo"}]},
                    {"version": 1, "repos": [{"name": "o/r", "workers": ["pi"]}]},
                    {"version": 1, "repos": [{"name": "o/r"}, {"name": "o/r"}]}):
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                repos.parse(doc)
        Path(self.path).parent.mkdir(parents=True)
        Path(self.path).write_text("{not json")
        with self.assertRaisesRegex(ValueError, "repos.json"):
            repos.load(self.path)


class ConfigLayeringTest(unittest.TestCase):
    def test_env_only_when_no_manifest(self):
        config = Config.from_env({"FACTORY_MCP_TOKEN": "t", "FACTORY_REPOS": "a/b",
                                  "FACTORY_WORKERS": "codex:hosted", "FACTORY_REPOS_FILE": "/nonexistent"})
        self.assertEqual(config.repos, {"a/b"})
        self.assertEqual(config.workers_for("a/b"), (("codex", "hosted"),))
        self.assertEqual(config.trusted_for("a/b"), {"a"})  # nobody configured: the owner
        self.assertTrue(config.trusts("a/b", "A"))

    def test_manifest_over_defaults_over_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "repos.json")
            Path(path).write_text(json.dumps(DOC))
            config = Config.from_env({"FACTORY_MCP_TOKEN": "t", "FACTORY_REPOS": "a/b",
                                      "FACTORY_WORKERS": "codex:hosted", "FACTORY_REPOS_FILE": path,
                                      "FACTORY_TRUSTED_AUTHORS": "bob", "FACTORY_CHECKOUT_ROOT": tmp})
        self.assertEqual(config.repos, {"a/b", "o/r"})  # env and manifest both count
        self.assertEqual(config.local_only_repos, {"o/r"})
        self.assertEqual(config.workers_for("o/r"), (("pi", "local"), ("codex", "hosted")))
        self.assertEqual(config.workers_for("a/b"), (("pi-glm-factory", "local"),))
        self.assertEqual(config.trusted_for("o/r"), {"alice"})
        self.assertEqual(config.trusted_for("a/b"), {"Ami-Khokhar"})
        self.assertEqual(config.checkout("o/r"), str(Path(tmp) / "o" / "r"))

    def test_reload_does_not_accumulate(self):
        config = Config(mcp_token="t", repos=frozenset({"a/b"})).with_manifest(repos.parse(DOC))
        self.assertEqual(config.repos, {"a/b", "o/r"})
        config = config.with_manifest(repos.Manifest())
        self.assertEqual(config.repos, {"a/b"})
        self.assertEqual(config.workers, (("codex", "hosted"),))


if __name__ == "__main__":
    unittest.main()
