"""Factory repos: the check before a repo is enabled, and repos.json (ADR-0014)."""
import dataclasses
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from factory_mcp import tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.repos import (GATE_CONFIG, GATE_WORKFLOW, TEMPLATE, bootstrap_plan, check, load,
                               plan_on, standard_blocks)
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


class BootstrapPlanTest(unittest.TestCase):
    def test_archive_or_missing_push_access_refuses_before_any_plan(self):
        for changes, reason in (({"archived": True}, "archived"),
                                ({"permissions": {"push": False}}, "cannot push")):
            with self.subTest(changes=changes):
                github = FakeGitHub()
                github.repos[REPO] = changes
                with self.assertRaisesRegex(ValueError, reason):
                    bootstrap_plan(github, REPO, "make test")
                self.assertEqual(github.contents, {})

    def test_missing_controls_have_one_deterministic_three_file_plan(self):
        github = FakeGitHub()

        plan = bootstrap_plan(github, REPO, "python3 -m unittest")

        self.assertEqual((plan["repo"], plan["default_branch"], plan["base_sha"]), (REPO, "main", "base0"))
        self.assertEqual(set(plan["files"]), {GATE_CONFIG, GATE_WORKFLOW, "AGENTS.md"})
        config = json.loads(plan["files"][GATE_CONFIG])
        self.assertEqual(config, {"verify": "python3 -m unittest"})
        self.assertEqual(standard_blocks(plan["files"]["AGENTS.md"]), standard_blocks(TEMPLATE.read_text()))
        self.assertEqual(github.contents, {})
        again = bootstrap_plan(github, REPO, "python3 -m unittest")
        self.assertEqual((again["files"], again["fingerprint"]), (plan["files"], plan["fingerprint"]))
        self.assertNotIn("initial_files", plan)

    def test_an_empty_repo_plans_a_readme_first_and_the_controls_for_the_setup_pr(self):
        github = FakeGitHub()
        github.branches[(REPO, "main")] = None

        plan = bootstrap_plan(github, REPO, "python3 -m unittest")

        self.assertIsNone(plan["base_sha"])
        self.assertEqual(plan["initial_files"], {"README.md": "# r\n"})
        self.assertEqual(set(plan["files"]), {GATE_CONFIG, GATE_WORKFLOW, "AGENTS.md"})
        github.branches[(REPO, "main")] = "readme-sha"  # what the next plan sees after the README
        self.assertEqual(plan_on(plan, "readme-sha"), bootstrap_plan(github, REPO, "python3 -m unittest"))

    def test_missing_controls_are_added_without_rewriting_valid_existing_ones(self):
        github = FakeGitHub()
        github.contents[(REPO, GATE_CONFIG)] = '{"verify": "make test"}'
        github.contents[(REPO, "AGENTS.md")] = "# Project notes\n"

        plan = bootstrap_plan(github, REPO)

        self.assertEqual(set(plan["files"]), {GATE_WORKFLOW, "AGENTS.md"})
        self.assertEqual(plan["files"]["AGENTS.md"], "# Project notes\n\n" + standard_blocks(TEMPLATE.read_text())[0] + "\n")

    def test_a_missing_config_requires_the_owner_selected_verify_command(self):
        with self.assertRaisesRegex(ValueError, "verify command"):
            bootstrap_plan(FakeGitHub(), REPO)

    def test_conflicting_control_state_is_refused(self):
        cases = [
            (GATE_CONFIG, "not JSON", "not valid JSON"),
            (GATE_CONFIG, '{"max_rounds": 3}', "has no verify command"),
            ("AGENTS.md", "<!-- factory-standard:begin -->old<!-- factory-standard:end -->", "differs"),
            ("AGENTS.md", "<!-- factory-standard:begin -->", "malformed"),
        ]
        for path, content, reason in cases:
            with self.subTest(path=path, content=content):
                github = FakeGitHub()
                github.contents[(REPO, path)] = content
                with self.assertRaisesRegex(ValueError, reason):
                    bootstrap_plan(github, REPO, "make test")
                self.assertEqual(github.contents[(REPO, path)], content)

    def test_a_repo_that_already_has_valid_controls_needs_no_files(self):
        plan = bootstrap_plan(gated(FakeGitHub()), REPO)

        self.assertEqual(plan["files"], {})


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

    def test_a_missing_control_names_the_bootstrap_tool_as_its_fix(self):
        cases = [("gate_config", (REPO, GATE_CONFIG), None), ("gate_workflow", (REPO, GATE_WORKFLOW), None),
                 ("agents_md", (REPO, "AGENTS.md"), None), ("agents_md", (REPO, "AGENTS.md"), "# Notes\n")]
        for check_id, key, text in cases:
            with self.subTest(check_id=check_id, text=text):
                github = gated(FakeGitHub())
                github.contents[key] = text
                failed = [c for c in check(github, REPO) if not c["ok"]]
                self.assertEqual([c["id"] for c in failed], [check_id])
                self.assertIn("factory_bootstrap_repo", failed[0]["fix"])
                bootstrap_plan(github, REPO, "make test")  # the advertised fix does not refuse

    def test_an_agents_md_the_bootstrap_refuses_gets_a_by_hand_fix(self):
        for text, refusal in (("<!-- factory-standard:begin -->\n", "malformed"),
                              (TEMPLATE.read_text().replace("Fail loudly.", "Fail quietly."), "differs")):
            with self.subTest(refusal=refusal):
                github = gated(FakeGitHub())
                github.contents[(REPO, "AGENTS.md")] = text
                [failed] = [c for c in check(github, REPO) if not c["ok"]]
                self.assertIn("yourself", failed["fix"])
                self.assertFalse(failed["fix"].startswith("Call factory_bootstrap_repo"))
                with self.assertRaisesRegex(ValueError, refusal):
                    bootstrap_plan(github, REPO, "make test")

    def test_the_tool_says_whether_the_repo_is_ready_and_enabled(self):
        foreman = Foreman(Store(":memory:"), gated(FakeGitHub()), Config(mcp_token="t", repos=frozenset({REPO})),
                          FakeRelay())
        found = tools.build(foreman.store, foreman)
        self.assertEqual({key: value for key, value in found["factory_repo_check"].fn({"repo": REPO}).items()
                          if key != "checks"}, {"repo": REPO, "enabled": True, "ready": True})
        other = found["factory_repo_check"].fn({"repo": "o/other"})
        self.assertEqual((other["enabled"], other["ready"]), (False, False))
        for name in ("o/r/../x", "o/..", "../r", "o/."):  # a checkout path must stay under its root
            with self.subTest(name), self.assertRaisesRegex(ToolError, "owner/repo"):
                found["factory_repo_check"].fn({"repo": name})
        self.assertTrue(found["factory_repo_check"].fn({"repo": "O/R"})["enabled"])
        self.assertEqual(found["factory_status"].fn({})["repos"], [REPO])


class CaseBlindGitHub(FakeGitHub):
    """Like GitHub: any case finds the repo, and the answer spells its name one way."""
    def repository(self, repo):
        return super().repository(repo.lower())

    def file(self, repo, path):
        return super().file(repo.lower(), path)

    def create_label(self, repo, name, color, description):
        super().create_label(repo.lower(), name, color, description)


class EnableRepoTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        subprocess.run(["git", "init", "-q", "--bare", str(root / "remote.git")], check=True)
        self.manifest, self.checkout = root / "config" / "repos.json", root / "work" / "o" / "r"
        self.github = gated(CaseBlindGitHub())
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

    def test_one_repo_is_listed_once_whatever_the_case_it_is_named_in(self):
        self.assertEqual(self.foreman.add_repo("O/R", "amteshwar")["repo"], REPO)  # GitHub's spelling
        self.assertFalse(self.foreman.add_repo("o/R", "amteshwar")["added"])
        self.assertEqual(([e["name"] for e in load(str(self.manifest))], self.foreman.config.repos),
                         ([REPO], frozenset({REPO})))

    def test_a_repo_a_person_put_in_the_manifest_is_not_added_again(self):
        """A person may edit repos.json while factory-mcp runs; a second entry would stop the next start."""
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(json.dumps({"version": 1, "repos": [{"name": "O/r"}]}))
        self.assertEqual(self.foreman.add_repo(REPO, "amteshwar")["added"], False)
        self.assertEqual([e["name"] for e in load(str(self.manifest))], ["O/r"])
        self.assertEqual(self.foreman.config.repos, frozenset({"O/r"}))  # and it works at once

    def test_a_repo_in_factory_repos_keeps_that_spelling_and_stays_out_of_the_manifest(self):
        self.foreman.config = dataclasses.replace(self.foreman.config, repos=frozenset({"O/r"}))
        self.assertEqual(self.foreman.add_repo(REPO, "amteshwar"),
                         {"repo": "O/r", "added": False, "checkout": str(self.checkout.parent.parent / "O" / "r")})
        self.assertEqual((self.manifest.exists(), self.foreman.config.repos), (False, frozenset({"O/r"})))

    def test_it_needs_a_manifest_to_write_and_a_place_for_the_checkout(self):
        enabled = self.foreman.config
        for case, config in (("FACTORY_REPOS_FILE", dict(repos_file="")),
                             ("FACTORY_CHECKOUT_ROOT", dict(checkout_root=""))):
            self.foreman.config = dataclasses.replace(enabled, **config)
            with self.subTest(case), self.assertRaisesRegex(ToolError, f"{case} is not set"):
                self.foreman.add_repo(REPO, "amteshwar")
        self.assertEqual((self.github.labels, self.manifest.exists()), ({}, False))


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
        with self.assertRaisesRegex(ValueError, "O/Tool-Added and o/tool-added are one repo"):
            self.config(json.dumps({"version": 1, "repos": [ENTRY]}), FACTORY_REPOS="O/Tool-Added")


if __name__ == "__main__":
    unittest.main()
