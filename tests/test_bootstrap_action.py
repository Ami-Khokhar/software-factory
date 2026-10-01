"""The owner-approved repo bootstrap prepares a setup PR, never merges controls."""
import json
import subprocess
import unittest
from unittest import mock

from factory_mcp import patches, repos
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.github import GitHubError
from factory_mcp.mcp import ToolError
from factory_mcp.repos import GATE_CONFIG, GATE_WORKFLOW, TEMPLATE
from factory_mcp.store import Store
from tests.fakes import FakeBootstrapGit, FakeGitHub, FakeRelay

KEY, READ = "opencode-value-7f3a", "read-token-value-91c2"  # test stand-ins, never real secrets


class FakeSecretCommands:
    """subprocess.run for `security` and `gh secret`: a login Keychain and the repo's secret names."""

    def __init__(self):
        self.keychain = {}  # service name -> value
        self.secrets = set()  # secret names on the repo
        self.calls = []  # (argv, stdin, env) of every command run
        self.failing = {}  # argv[:3] -> (exit code, stderr) that command returns instead

    def __call__(self, argv, input=None, env=None, capture_output=False, text=False, timeout=None):
        self.calls.append((list(argv), input, env))

        def done(code, out="", err=""):
            return subprocess.CompletedProcess(argv, code, out, err)

        if tuple(argv[:3]) in self.failing:
            code, err = self.failing[tuple(argv[:3])]
            return done(code, "", err)
        if argv[:2] == ["security", "find-generic-password"]:
            service = argv[argv.index("-s") + 1]
            if service in self.keychain:
                return done(0, self.keychain[service] + "\n")
            return done(44, "", "security: The specified item could not be found in the keychain.\n")
        if argv[:3] == ["gh", "secret", "list"]:
            return done(0, json.dumps([{"name": name} for name in sorted(self.secrets)]))
        if argv[:3] == ["gh", "secret", "set"]:
            self.secrets.add(argv[3])
            return done(0)
        raise AssertionError(f"unexpected command {argv}")


class BootstrapActionTest(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset(), git_url="https://github.com/{repo}.git",
                                      github_token="factory-token"),
                               FakeRelay())
        self.commands = FakeSecretCommands()  # no test runs the real security or gh
        patch = mock.patch("factory_mcp.gate_secrets.subprocess.run", side_effect=self.commands)
        patch.start()
        self.addCleanup(patch.stop)
        git = FakeBootstrapGit(self.github).install(self)
        self.initial, self.pushed, self.updates = git.initial, git.pushed, git.updates
        self.log = mock.Mock(wraps=self.foreman.store.log)  # records every audit entry it writes
        self.foreman.store.log = self.log

    def empty_repo(self):
        self.github.branches[("o/r", "main")] = None  # GitHub: an empty repo's default branch has no head

    def test_one_approval_prepares_one_manual_setup_pr_and_retry_adopts_it(self):
        first = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")
        second = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        self.assertEqual(first, second)
        self.assertEqual(first["repo"], "o/r")
        self.assertEqual(first["prNumber"], 100)
        self.assertTrue(first["needsHumanMerge"])
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(set(self.pushed[0][3]), {GATE_CONFIG, GATE_WORKFLOW, "AGENTS.md"})
        self.assertEqual(self.github.branch_head("o/r", "main"), "base0")
        body = self.github.pulls[0]["body"]
        for heading in ("## Task", "## Intent", "## Why it was needed", "## Why this approach",
                        "## What changed", "## Proof of work"):
            self.assertIn(heading, body)
        self.assertIn("owner must review and merge", body.lower())
        for path in (GATE_CONFIG, GATE_WORKFLOW, "AGENTS.md"):
            self.assertIn(f"- `{path}`", body)
        self.assertEqual(self.initial, [])
        self.assertIsNone(first.get("initialCommit"))

    def test_an_empty_repo_gets_a_readme_commit_then_the_controls_on_a_setup_pr(self):
        self.empty_repo()

        first = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")
        second = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        self.assertEqual(self.initial, [("https://github.com/o/r.git", "main", {"README.md": "# r\n"})])
        self.assertEqual(first["initialCommit"], "readme-sha")
        self.assertTrue(first["needsHumanMerge"])
        self.assertEqual(len(self.pushed), 1)
        _, branch, base_sha, files, _ = self.pushed[0]
        self.assertEqual((base_sha, set(files)), ("readme-sha", {GATE_CONFIG, GATE_WORKFLOW, "AGENTS.md"}))
        self.assertEqual(self.github.branch_head("o/r", "main"), "readme-sha")
        self.assertEqual(self.github.pulls[0]["head"]["ref"], branch)
        self.assertEqual(second["prNumber"], first["prNumber"])  # a retry adopts the PR
        audit = {call.args[0]: call.args[1] for call in self.log.call_args_list}
        self.assertEqual(audit["bootstrap_pr"]["initialCommit"], "readme-sha")
        self.assertEqual(audit["bootstrap_pr"]["baseSha"], "readme-sha")
        self.assertEqual(audit["bootstrap_initial_commit"]["sha"], "readme-sha")

    def test_a_retry_after_the_plan_changes_updates_the_open_setup_pr_instead_of_opening_another(self):
        first = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        again = self.foreman.bootstrap_repo("o/r", "make test", "owner")  # the planned files differ now

        self.assertEqual((again["prNumber"], len(self.github.pulls), len(self.pushed)), (first["prNumber"], 1, 1))
        branch, head, files = self.updates[-1]
        self.assertEqual((branch, head), (self.github.pulls[0]["head"]["ref"], "setup-sha"))
        self.assertEqual(json.loads(files[GATE_CONFIG]), {"verify": "make test"})
        self.assertEqual(again["updatedSha"], "update-sha")
        self.assertEqual(self.github.pulls[0]["head"]["sha"], "update-sha")
        audit = {call.args[0]: call.args[1] for call in self.log.call_args_list}
        self.assertEqual((audit["bootstrap_pr_updated"]["prNumber"], audit["bootstrap_pr_updated"]["headSha"]),
                         (first["prNumber"], "update-sha"))

    def test_an_update_rewrites_the_setup_pr_body_with_its_files_and_fresh_secret_statuses(self):
        self.github.contents[("o/r", GATE_WORKFLOW)] = "on: pull_request"
        self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")
        self.assertNotIn(f"- `{GATE_WORKFLOW}`", self.github.pulls[0]["body"])
        del self.github.contents[("o/r", GATE_WORKFLOW)]  # a person removed it on the default branch
        self.github.branches[("o/r", "main")] = "base1"
        self.commands.keychain = {"software-factory/OPENCODE_GO_API_KEY": KEY,
                                  "software-factory/GATEKEEPER_READ_TOKEN": READ}

        again = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        body = self.github.pulls[0]["body"]
        self.assertEqual(again["updatedSha"], "update-sha")
        for line in (f"- `{GATE_WORKFLOW}`", "- `GATEKEEPER_READ_TOKEN`: set", "base1 HEAD"):
            self.assertIn(line, body)
        self.assertNotIn("missing: ", body)
        self.assertNotIn(KEY, body)

    def test_a_default_branch_that_moved_during_a_retry_leaves_the_setup_pr_alone(self):
        self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")
        original, reads = self.github.branch_head, []

        def moving_head(repo, branch):  # the plan reads base0, then a commit lands on main
            reads.append(branch)
            return "new-base" if branch == "main" and reads.count("main") > 1 else original(repo, branch)

        with mock.patch.object(self.github, "branch_head", side_effect=moving_head):
            with self.assertRaisesRegex(ToolError, "default branch changed during bootstrap"):
                self.foreman.bootstrap_repo("o/r", "make test", "owner")
        self.assertEqual((self.updates, self.github.pulls[0]["head"]["sha"]), ([], "setup-sha"))

    def test_a_setup_branch_without_an_open_pr_is_refused_before_any_write(self):
        plan = repos.bootstrap_plan(self.github, "o/r", "python3 -m unittest")
        self.github.push("o/r", "factory-bootstrap/" + plan["fingerprint"][:16], "orphan-sha")

        with self.assertRaisesRegex(ToolError, "exists without an open PR"):
            self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        self.assertEqual((self.pushed, self.github.pulls, self.commands.calls), ([], [], []))

    def test_a_setup_pr_opened_meanwhile_is_adopted_when_creating_one_fails(self):
        def racing_create(repo, head, base, title, body):  # another call opened it first
            self.github.add_pr(repo, 7, head, self.github.branch_head(repo, head))
            raise GitHubError(422, f"A pull request already exists for o:{head}.")

        with mock.patch.object(self.github, "create_pull", side_effect=racing_create):
            result = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        self.assertEqual((result["prNumber"], len(self.github.pulls)), (7, 1))
        audit = {call.args[0]: call.args[1] for call in self.log.call_args_list}
        self.assertEqual(audit["bootstrap_pr"]["prNumber"], 7)

    def test_a_setup_branch_that_cannot_fast_forward_is_refused_naming_its_pr(self):
        self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")
        error = patches.PatchError("factory-bootstrap/x cannot fast-forward from setup-sha")
        with mock.patch("factory_mcp.foreman.patches.update_bootstrap", side_effect=error):
            with self.assertRaisesRegex(ToolError, "setup PR #100 was not updated.*cannot fast-forward"):
                self.foreman.bootstrap_repo("o/r", "make test", "owner")
        self.assertEqual((len(self.pushed), len(self.github.pulls)), (1, 1))

    def test_several_open_setup_prs_are_refused_naming_each(self):
        self.github.add_pr("o/r", 2, "factory-bootstrap/bbbb", "b")
        self.github.add_pr("o/r", 1, "factory-bootstrap/aaaa", "a")
        with self.assertRaisesRegex(ToolError, r"several open setup PRs \(#1, #2\)"):
            self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")
        self.assertEqual((self.pushed, self.updates), ([], []))

    def test_a_setup_pr_from_a_fork_or_a_closed_one_is_not_adopted(self):
        self.github.add_pr("o/r", 1, "factory-bootstrap/aaaa", "a")
        self.github.pulls[-1]["head"]["repo"] = {"full_name": "someone/r"}
        self.github.add_pr("o/r", 2, "factory-bootstrap/bbbb", "b", state="closed")

        result = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        self.assertEqual((result["prNumber"], len(self.pushed), self.updates), (102, 1, []))

    def test_an_empty_repo_refuses_a_setup_branch_named_after_its_readme_commit(self):
        self.empty_repo()
        plan = repos.plan_on(repos.bootstrap_plan(self.github, "o/r", "python3 -m unittest"), "readme-sha")
        self.github.branches[("o/r", "factory-bootstrap/" + plan["fingerprint"][:16])] = "someone-else"

        with self.assertRaisesRegex(ToolError, "exists without an open PR"):
            self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        self.assertEqual((self.pushed, self.github.pulls), ([], []))

    def test_a_refused_initial_commit_pushes_no_setup_branch(self):
        self.empty_repo()
        with mock.patch("factory_mcp.foreman.patches.initial_commit",
                        side_effect=patches.PatchError("the repo may have got a commit since the check")):
            with self.assertRaisesRegex(ToolError, "may have got a commit"):
                self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")
        self.assertEqual((self.pushed, self.github.pulls), ([], []))

    def test_existing_controls_need_no_setup_pr(self):
        self.github.contents.update({("o/r", GATE_CONFIG): '{"verify":"make test"}',
                                     ("o/r", GATE_WORKFLOW): "workflow", ("o/r", "AGENTS.md"): TEMPLATE.read_text()})
        result = self.foreman.bootstrap_repo("o/r", None, "owner")
        self.assertEqual(result["repo"], "o/r")
        self.assertFalse(result["needsHumanMerge"])
        self.assertEqual((self.pushed, self.github.pulls), ([], []))

    def test_default_branch_changed_before_push_refuses_without_writing(self):
        original = self.github.branch_head
        calls = [0]

        def moving_head(repo, branch):
            calls[0] += 1
            return "new-base" if calls[0] > 1 and branch == "main" else original(repo, branch)

        with mock.patch.object(self.github, "branch_head", side_effect=moving_head):
            with self.assertRaisesRegex(ToolError, "changed"):
                self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")
        self.assertEqual((self.pushed, self.github.pulls, self.commands.calls), ([], [], []))

    def secret_status(self, result):
        return {item["name"]: item["status"] for item in result["secrets"]}

    def written_text(self, result):
        """Everything the bootstrap handed out or wrote: its result, the audit log, the PR body."""
        audit = [json.dumps(call.args) for call in self.log.call_args_list]
        return "\n".join([json.dumps(result), *audit, *(pr["body"] for pr in self.github.pulls)])

    def test_keychain_secrets_go_to_gh_on_stdin_and_only_names_are_reported(self):
        self.commands.keychain = {"software-factory/OPENCODE_GO_API_KEY": KEY,
                                  "software-factory/GATEKEEPER_READ_TOKEN": READ}

        result = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        self.assertEqual(self.secret_status(result),
                         {"OPENCODE_GO_API_KEY": "set", "GATEKEEPER_READ_TOKEN": "set"})
        sets = [(argv, stdin, env) for argv, stdin, env in self.commands.calls if argv[:3] == ["gh", "secret", "set"]]
        self.assertEqual([(argv, stdin) for argv, stdin, _ in sets],
                         [(["gh", "secret", "set", "OPENCODE_GO_API_KEY", "--repo", "o/r"], KEY),
                          (["gh", "secret", "set", "GATEKEEPER_READ_TOKEN", "--repo", "o/r"], READ)])
        self.assertTrue(all(env["GH_TOKEN"] == "factory-token" for _, _, env in sets))
        for argv, _, env in self.commands.calls:  # a value is never in argv, nor in an env value
            self.assertFalse({KEY, READ} & set(argv) or {KEY, READ} & set((env or {}).values()))
        written = self.written_text(result)
        self.assertNotIn(KEY, written)
        self.assertNotIn(READ, written)
        self.assertIn("- `OPENCODE_GO_API_KEY`: set", self.github.pulls[0]["body"])
        audit = {call.args[0]: call.args[1] for call in self.log.call_args_list}
        self.assertEqual(audit["bootstrap_secrets"]["secrets"], result["secrets"])

    def test_a_secret_the_repo_has_is_left_alone(self):
        self.commands.secrets = {"OPENCODE_GO_API_KEY", "GATEKEEPER_READ_TOKEN"}
        self.commands.keychain = {"software-factory/OPENCODE_GO_API_KEY": KEY}

        result = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        self.assertEqual(self.secret_status(result),
                         {"OPENCODE_GO_API_KEY": "already set", "GATEKEEPER_READ_TOKEN": "already set"})
        self.assertEqual([argv[:3] for argv, _, _ in self.commands.calls], [["gh", "secret", "list"]])

    def test_a_secret_missing_from_the_keychain_is_reported_and_the_setup_pr_still_opens(self):
        self.commands.keychain = {"software-factory/OPENCODE_GO_API_KEY": KEY}

        result = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        self.assertEqual(self.secret_status(result), {
            "OPENCODE_GO_API_KEY": "set",
            "GATEKEEPER_READ_TOKEN": "missing: add it with security add-generic-password "
                                     "-s software-factory/GATEKEEPER_READ_TOKEN -a factory -w"})
        self.assertEqual(result["prNumber"], 100)
        self.assertEqual(self.commands.secrets, {"OPENCODE_GO_API_KEY"})

    def test_a_retry_after_the_owner_adds_the_secret_sets_it(self):
        self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")
        self.commands.keychain = {"software-factory/OPENCODE_GO_API_KEY": KEY,
                                  "software-factory/GATEKEEPER_READ_TOKEN": READ}

        again = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        self.assertEqual(again["prNumber"], 100)
        self.assertEqual(self.secret_status(again),
                         {"OPENCODE_GO_API_KEY": "set", "GATEKEEPER_READ_TOKEN": "set"})

    def test_a_retry_that_only_sets_a_secret_rewrites_the_setup_pr_body(self):
        self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")
        self.assertIn("- `GATEKEEPER_READ_TOKEN`: missing: ", self.github.pulls[0]["body"])
        self.commands.keychain = {"software-factory/OPENCODE_GO_API_KEY": KEY,
                                  "software-factory/GATEKEEPER_READ_TOKEN": READ}

        again = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        body = self.github.pulls[0]["body"]
        self.assertNotIn("updatedSha", again)  # the files were already on the PR: no commit
        self.assertIn("- `GATEKEEPER_READ_TOKEN`: set", body)
        self.assertNotIn("missing: ", body)
        self.assertNotIn(READ, body)

    def test_a_failing_gh_that_echoes_the_value_reports_only_its_name_and_exit_code(self):
        self.commands.keychain = {"software-factory/OPENCODE_GO_API_KEY": KEY,
                                  "software-factory/GATEKEEPER_READ_TOKEN": READ}
        self.commands.failing[("gh", "secret", "set")] = (1, f"failed to set secret: body {KEY} {READ}\n")

        result = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        self.assertEqual(self.secret_status(result), {"OPENCODE_GO_API_KEY": "failed: gh secret set exited 1",
                                                      "GATEKEEPER_READ_TOKEN": "failed: gh secret set exited 1"})
        written = self.written_text(result)
        self.assertNotIn(KEY, written)
        self.assertNotIn(READ, written)

    def test_a_set_that_gh_secret_list_does_not_show_is_reported_as_failed(self):
        self.commands.keychain = {"software-factory/OPENCODE_GO_API_KEY": KEY}
        self.commands.failing[("gh", "secret", "set")] = (0, "")  # exits 0 but stores nothing

        result = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        self.assertEqual(self.secret_status(result)["OPENCODE_GO_API_KEY"],
                         "failed: gh secret list does not show it after gh secret set")

    def test_a_command_that_cannot_run_is_a_failed_status_and_the_setup_pr_still_opens(self):
        cases = {"not installed": (FileNotFoundError(2, "No such file"), "failed: gh is not installed"),
                 "timed out": (subprocess.TimeoutExpired(["gh"], 60), "failed: gh secret list timed out"),
                 "not runnable": (PermissionError(13, "denied"), "failed: gh secret list failed: PermissionError")}
        for case, (error, status) in cases.items():
            with self.subTest(case), mock.patch("factory_mcp.gate_secrets.subprocess.run", side_effect=error):
                result = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")  # then retries
                self.assertEqual(set(self.secret_status(result).values()), {status})
                self.assertEqual(result["prNumber"], 100)

    def test_a_secret_list_that_is_not_json_names_is_a_failed_status(self):
        with mock.patch("factory_mcp.gate_secrets.subprocess.run",
                        return_value=subprocess.CompletedProcess(["gh"], 0, "not json", "")):
            result = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")
        self.assertEqual(set(self.secret_status(result).values()),
                         {"failed: gh secret list printed no JSON list of names"})

    def test_an_empty_keychain_value_is_missing_and_not_set(self):
        self.commands.keychain = {"software-factory/OPENCODE_GO_API_KEY": ""}

        result = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        self.assertTrue(self.secret_status(result)["OPENCODE_GO_API_KEY"].startswith("missing: "))
        self.assertEqual(self.commands.secrets, set())

    def test_a_refused_secret_list_reports_gh_error_line_without_any_token(self):
        pat, oauth, fine = "ghp_" + "a1" * 18, "gho_" + "b2" * 18, "github_pat_11AB_" + "c3" * 20
        refused = (f"HTTP 403: Resource not accessible by personal access token factory-token {pat} "
                   f"{oauth} {fine}\nsecond line {KEY}\n")
        cases = {
            "refused": (refused, "failed: gh secret list exited 1: HTTP 403: Resource not accessible by "
                                 "personal access token [token] [token] [token] [token]; the factory's GitHub "
                                 "token needs Actions secrets read/write (the \"Secrets\" permission on a "
                                 "fine-grained token, the `repo` scope on a classic one)"),
            "other": ("\n  could not connect to api.github.com\n", "failed: gh secret list exited 1: "
                                                                   "could not connect to api.github.com"),
            "long": ("x" * 500, "failed: gh secret list exited 1: " + "x" * 200),
            "silent": ("", "failed: gh secret list exited 1"),
        }
        for case, (stderr, status) in cases.items():
            with self.subTest(case):
                self.commands.failing[("gh", "secret", "list")] = (1, stderr)
                result = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")
                self.assertEqual(set(self.secret_status(result).values()), {status})
                written = self.written_text(result)
                for secret in ("factory-token", pat, oauth, fine, KEY):
                    self.assertNotIn(secret, written)

    def test_a_locked_keychain_reports_the_exit_code_not_missing(self):
        self.commands.failing[("security", "find-generic-password", "-s")] = (51, f"{KEY}\n")

        result = self.foreman.bootstrap_repo("o/r", "python3 -m unittest", "owner")

        self.assertEqual(set(self.secret_status(result).values()),
                         {"failed: security find-generic-password exited 51"})
        self.assertNotIn(KEY, self.written_text(result))


if __name__ == "__main__":
    unittest.main()
