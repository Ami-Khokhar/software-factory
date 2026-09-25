"""Repo operations: factory_repo_check, factory_repo_status, factory_add_repo, factory_approve_issue."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from factory_mcp import repos, tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.github import GitHubError
from factory_mcp.mcp import ToolError
from factory_mcp.ops import Ops, verify_tool
from factory_mcp.store import Store
from tests.fakes import FakeGitHub, FakeRelay, ISSUE_BODY

REPO = "o/new"


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.github, self.store = FakeGitHub(), Store(":memory:")
        self.github.add_repo(REPO)
        self.probed, self.cloned = [], []
        self.tool_found = True
        config = Config(mcp_token="t", repos=frozenset({"o/r"}), checkout_root=self.tmp.name,
                        repos_file=str(Path(self.tmp.name) / "repos.json"))
        self.foreman = Foreman(self.store, self.github, config, FakeRelay())
        self.ops = Ops(self.store, self.github, config, self.foreman, probe=self.probe, clone=self.clone)
        self.tools = tools.build(self.store, self.foreman, self.ops)

    def tearDown(self):
        self.tmp.cleanup()

    def probe(self, image, tool):
        self.probed.append((image, tool))
        return self.tool_found

    def clone(self, url, token, dest):
        self.cloned.append((url, dest))
        os.makedirs(os.path.join(dest, ".git"))

    def checks(self, repo=REPO):
        result = self.ops.repo_check(repo)
        return result, {c["id"]: c for c in result["checks"]}


class RepoCheckTest(Harness):
    def test_prepared_repo_passes_and_nothing_is_written(self):
        result, checks = self.checks()
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["defaultBranch"], result["private"], result["risk"]), ("main", False, "normal"))
        self.assertEqual(set(checks), {"repo_exists", "not_archived", "gate_config", "gate_workflow",
                                       "verify_tool", "labels", "token_checks", "branch_protection",
                                       "checkout_root", "trusted_authors"})
        self.assertFalse(checks["labels"]["ok"])  # optional: add_repo creates them
        self.assertEqual(self.probed, [("factory-worker:latest", "python3")])
        self.assertEqual((self.github.writes, self.store.history(None), self.cloned), ([], [], []))

    def test_each_required_failure_names_a_fix(self):
        cases = {
            "repo_exists": lambda: self.github.repos.__setitem__(REPO, False),
            "not_archived": lambda: self.github.repos[REPO].update(archived=True),
            "gate_config": lambda: self.github.contents_by_path.pop((REPO, ".github/pr-gatekeeper.json")),
            "verify_tool": lambda: setattr(self, "tool_found", False),
        }
        for check_id, breaks in cases.items():
            with self.subTest(check=check_id):
                self.tearDown()
                self.setUp()
                breaks()
                result, checks = self.checks()
                self.assertFalse(result["ok"])
                self.assertFalse(checks[check_id]["ok"])
                self.assertTrue(checks[check_id]["fix"])

    def test_gate_config_must_parse_and_name_verify(self):
        for text in ("{not json", json.dumps({"verify": ""}), "[]"):
            self.github.contents_by_path[(REPO, ".github/pr-gatekeeper.json")] = text
            self.assertFalse(self.checks()[1]["gate_config"]["ok"], text)

    def test_private_repo_needs_checks_read(self):
        self.github.add_repo(REPO, private=True)
        self.assertTrue(self.checks()[1]["token_checks"]["required"])
        self.github.check_runs = lambda repo, ref, name="pr-gatekeeper": (_ for _ in ()).throw(
            GitHubError(403, "Resource not accessible by personal access token"))
        result, checks = self.checks()
        self.assertFalse(result["ok"])
        self.assertIn("Checks: Read", checks["token_checks"]["fix"])

    def test_optional_checks_do_not_fail_the_verdict(self):
        self.github.add_repo(REPO, workflow=False, protected=False)
        result, checks = self.checks()
        self.assertTrue(result["ok"])
        self.assertFalse(checks["gate_workflow"]["ok"] or checks["branch_protection"]["ok"])

    def test_docker_unavailable_and_local_settings(self):
        self.tool_found = None
        self.assertIn("docker unavailable", self.checks()[1]["verify_tool"]["detail"])
        self.ops.config = Config(mcp_token="t", repos=frozenset(), worker_image="")
        result, checks = self.checks()
        self.assertTrue(checks["verify_tool"]["ok"])
        self.assertFalse(checks["checkout_root"]["ok"])
        self.assertFalse(result["ok"])

    def test_malformed_reference(self):
        with self.assertRaisesRegex(ToolError, "not a github repo reference"):
            self.ops.repo_check("not-a-repo")

    def test_verify_tool(self):
        self.assertEqual(verify_tool("python3 -m pip install -r r.txt && npm test"), "npm")
        self.assertEqual(verify_tool(""), "")


class AddRepoTest(Harness):
    def add(self, **args):
        return self.ops.add_repo(REPO, by="amteshwar", **args)

    def test_enables_a_prepared_repo_idempotently(self):
        added = self.add()
        self.assertEqual(added["labelsCreated"], ["factory:proposed", "factory:ready"])
        self.assertTrue(added["cloned"])
        self.assertIn(REPO, self.ops.config.repos)
        self.assertIn(REPO, self.foreman.config.repos)  # the Foreman sees the reload
        entry = repos.load(self.ops.config.repos_file).repos[REPO]
        self.assertEqual((entry.verify, entry.added_by), ("python3 -m unittest", "amteshwar"))
        self.assertEqual(self.store.history(None)[-1]["action"], "repo:added")

        again = self.add(risk="local-only", workers=["pi:local"])
        self.assertEqual((again["cloned"], again["labelsCreated"]), (False, []))
        self.assertEqual(len(self.cloned), 1)
        entry = repos.load(self.ops.config.repos_file).repos[REPO]
        self.assertEqual((entry.risk, entry.workers, entry.added_by), ("local-only", (("pi", "local"),), "amteshwar"))
        self.assertIn(REPO, self.ops.config.local_only_repos)
        self.assertEqual(again["status"]["workers"], ["pi:local"])

    def test_refuses_on_a_failing_check_and_changes_nothing(self):
        self.github.contents_by_path.pop((REPO, ".github/pr-gatekeeper.json"))
        with self.assertRaisesRegex(ToolError, "not enabled; nothing changed.*gate_config"):
            self.add()
        self.assertEqual((self.github.writes, self.cloned), ([], []))
        self.assertFalse(Path(self.ops.config.repos_file).exists())
        self.assertNotIn(REPO, self.ops.config.repos)

    def test_refuses_bad_arguments(self):
        with self.assertRaisesRegex(ToolError, "risk"):
            self.add(risk="yolo")
        with self.assertRaisesRegex(ToolError, "agentId:local"):
            self.add(workers=["pi"])
        with self.assertRaisesRegex(ToolError, "none of its workers is local"):
            self.add(risk="local-only", workers=["codex:hosted"])

    def test_tool_requires_confirm(self):
        tool = self.tools["factory_add_repo"]
        with self.assertRaisesRegex(ToolError, "confirm"):
            tool.fn({"repo": REPO, "confirm": False, "by": "amteshwar"})
        self.assertEqual(tool.fn({"repo": REPO, "confirm": True, "by": "amteshwar"})["repo"], REPO)


class ApproveIssueTest(Harness):
    def setUp(self):
        super().setUp()
        self.github.add_issue("o/r", 7, labels=("factory:proposed",), author="o")

    def test_labels_and_takes_the_issue_in_once(self):
        done = self.ops.approve_issue("o/r", 7, "amteshwar")
        self.assertEqual((done["alreadyReady"], done["workItem"]["state"]), (False, "ready"))
        self.assertEqual(self.github.writes, [("add_labels", "o/r", 7, ("factory:ready",))])
        again = self.ops.approve_issue("o/r", 7, "amteshwar")
        self.assertTrue(again["alreadyReady"])
        self.assertEqual(len(self.github.writes), 1)
        self.assertEqual([h["action"] for h in self.store.history("o/r#7")], ["issue:approved", "intake"])

    def test_refusals(self):
        self.github.add_issue("o/r", 8, author="mallory")
        self.github.add_issue("o/r", 9, body="## Acceptance\n- it works", author="o")
        self.github.add_issue("o/r", 10, state="closed", author="o")
        for repo, number, message in (("o/r", 8, "not a trusted author"), ("o/r", 9, "Acceptance"),
                                      ("o/r", 10, "not an open issue"), ("o/r", 99, "does not exist"),
                                      ("x/y", 7, "not an enabled factory repo")):
            with self.subTest(number=number), self.assertRaisesRegex(ToolError, message):
                self.ops.approve_issue(repo, number, "amteshwar")
        self.assertEqual(self.github.writes, [])

    def test_trusted_authors_from_config(self):
        self.github.add_issue("o/r", 8, body=ISSUE_BODY, author="Alice")
        self.ops.config = Config(mcp_token="t", repos=frozenset({"o/r"}),
                                 trusted_authors=frozenset({"alice"}))
        self.assertEqual(self.ops.approve_issue("o/r", 8, "amteshwar")["issue"], 8)
        with self.assertRaisesRegex(ToolError, "not a trusted author"):
            self.ops.approve_issue("o/r", 7, "amteshwar")  # the owner is no longer implied


class RepoStatusTest(Harness):
    def test_shapes(self):
        self.github.add_issue("o/r", 7)
        self.foreman.intake("o/r", 7)
        self.store.log("tick_error", {"error": "GitHub 502: bad gateway"}, "o/r#7")
        row = self.ops.repo_status()["repos"][0]
        self.assertEqual({k: row[k] for k in ("name", "enabled", "source", "risk", "trustedAuthors")},
                         {"name": "o/r", "enabled": True, "source": "env", "risk": "normal",
                          "trustedAuthors": ["o"]})
        self.assertEqual(row["workItems"], {"total": 1, "byState": {"ready": 1}})
        self.assertEqual(row["lastError"], "GitHub 502: bad gateway")
        self.assertNotIn("check", row)

        live = self.tools["factory_repo_status"].fn({"repo": REPO, "live": True})["repos"][0]
        self.assertEqual((live["enabled"], live["source"], live["check"]["ok"]), (False, "none", True))
        self.assertEqual(self.tools["factory_repos"].fn({}), {"repos": ["o/r"]})


if __name__ == "__main__":
    unittest.main()
