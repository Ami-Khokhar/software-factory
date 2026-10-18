"""The owner-authorized bootstrap-only setup merge (ADR-0074): narrow, audited, fail-closed."""
import re
import unittest
from unittest import mock

from factory_mcp import repos, tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.github import GitHubError
from factory_mcp.mcp import ToolError
from factory_mcp.repos import GATE_CONFIG
from factory_mcp.store import Store
from tests.fakes import FakeBootstrapGit, FakeGitHub, FakeRelay
from tests.test_bootstrap_action import FakeSecretCommands

REPO = "o/r"
VERIFY = "python3 -m unittest"
HEAD, MERGE = "setup-sha", "merge-sha"


class SetupMergeTest(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()
        self.foreman = Foreman(
            Store(":memory:"), self.github,
            Config(mcp_token="t", repos=frozenset(), git_url="https://github.com/{repo}.git",
                   github_token="factory-token"),
            FakeRelay())
        patch = mock.patch("factory_mcp.gate_secrets.subprocess.run", side_effect=FakeSecretCommands())
        patch.start()
        self.addCleanup(patch.stop)
        self.git = FakeBootstrapGit(self.github).install(self)

    def setup_pr(self, authorize=True):
        """A green canonical setup PR; with `authorize`, the owner's grant on it."""
        pr = self.foreman.bootstrap_repo(REPO, VERIFY, "owner")
        self.assertTrue(pr["needsHumanMerge"])
        branch, files = self.git.pushed[0][1], self.git.held[self.git.pushed[0][1]]
        for name, text in files.items():  # the content the pushed branch holds, at its head
            self.github.contents[(REPO, name, HEAD)] = text
        self.github.on_merge = (lambda: [self.github.contents.__setitem__((REPO, name), text)
                                         for name, text in files.items()])  # what merging delivers
        if authorize:
            self.foreman.authorize_setup_merge(REPO, VERIFY, "amir")
        return pr

    def green(self, sha=HEAD, conclusion="success", status="completed"):
        self.github.add_run(REPO, sha, status=status, conclusion=conclusion)

    def audit(self, action):
        return [entry["detail"] for entry in self.foreman.store.global_history()
                if entry["action"] == action]

    def test_absent_authorization_preserves_the_manual_merge(self):
        pr = self.setup_pr(authorize=False)
        self.green()
        with self.assertRaisesRegex(ToolError, "no setup-merge authorization"):
            self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")
        readback = self.github.pull(REPO, pr["prNumber"])
        self.assertEqual((readback["state"], readback.get("merged_at")), ("open", None))
        self.assertEqual(self.github.deleted, [])

    def test_authorize_pins_the_plan_digest_and_the_verify_command(self):
        grant = self.foreman.authorize_setup_merge(REPO, VERIFY, "amir")
        expected = repos.bootstrap_plan(self.github, REPO, VERIFY)["fingerprint"]
        row = self.foreman.store.setup_merge(REPO)
        self.assertEqual((grant["fingerprint"], row["fingerprint"], row["verify"], row["approvedBy"]),
                         (expected, expected, VERIFY, "amir"))
        self.assertEqual(self.audit("setup_merge_authorized"),
                         [{"repo": REPO, "fingerprint": expected, "by": "amir"}])
        found = tools.build(self.foreman.store, self.foreman)  # discovery tells Day Desk the grant
        self.assertTrue(found["factory_repo_check"].fn({"repo": REPO})["setupMergeAuthorized"])
        self.assertFalse(found["factory_repo_check"].fn({"repo": "o/none"})["setupMergeAuthorized"])
        with self.assertRaisesRegex(ToolError, "a nonempty verify command is required"):
            self.foreman.authorize_setup_merge(REPO, None, "amir")

    def test_a_changed_setup_refuses_before_any_merge(self):
        pr = self.setup_pr()
        self.green()
        self.foreman.bootstrap_repo(REPO, "make test", "owner")  # resets the PR to the new plan
        new_plan = repos.bootstrap_plan(self.github, REPO, "make test")["files"]
        for name, text in new_plan.items():  # the head now holds the changed, non-approved config
            self.github.contents[(REPO, name, "update-sha")] = text
        with self.assertRaisesRegex(ToolError, re.escape(GATE_CONFIG) + " .* differs from the approved"):
            self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")
        readback = self.github.pull(REPO, pr["prNumber"])
        self.assertEqual((readback["state"], readback.get("merged_at")), ("open", None))

    def test_a_stale_base_or_head_refuses_before_any_merge(self):
        self.setup_pr()
        self.github.push(REPO, "main", "base1")  # the default branch moved under the setup PR
        with self.assertRaisesRegex(ToolError, "canonical setup changed"):
            self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")
        self.github.push(REPO, "main", "base0")
        self.green()
        self.github.pulls[0]["head"]["sha"] = "stale-read"  # the PR moved as this call read it
        with self.assertRaisesRegex(ToolError, "head moved off stale-read"):
            self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")
        self.assertEqual(self.audit("setup_merge_journalled"), [])

    def test_added_code_or_noncanonical_content_refuses(self):
        pr = self.setup_pr()
        self.green()
        self.github.files[(REPO, HEAD)] = self.github.files[(REPO, HEAD)] + [
            {"filename": "src/app.py", "status": "added", "additions": 5, "deletions": 0}]
        with self.assertRaisesRegex(ToolError, "src/app.py"):
            self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")
        del self.github.files[(REPO, HEAD)][-1]
        self.github.contents[(REPO, GATE_CONFIG, HEAD)] = '{"verify": "make test"}'  # tampered
        with self.assertRaisesRegex(ToolError, re.escape(GATE_CONFIG) + " .* differs from the approved"):
            self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")
        readback = self.github.pull(REPO, pr["prNumber"])
        self.assertEqual((readback["state"], readback.get("merged_at")), ("open", None))

    def test_missing_pending_or_failing_checks_refuse(self):
        pr = self.setup_pr()  # no check run at all
        with self.assertRaisesRegex(ToolError, "no pr-gatekeeper check run"):
            self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")
        self.green(status="in_progress")
        with self.assertRaisesRegex(ToolError, "in_progress"):
            self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")
        self.green(status="completed", conclusion="failure")
        with self.assertRaisesRegex(ToolError, "concluded failure"):
            self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")
        readback = self.github.pull(REPO, pr["prNumber"])
        self.assertEqual((readback["state"], readback.get("merged_at")), ("open", None))

    def test_branch_protection_denial_leaves_the_pr_open(self):
        pr = self.setup_pr()
        self.green()
        self.github.blocked = True
        with self.assertRaisesRegex(ToolError, "was not merged.*not allowed"):
            self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")
        readback = self.github.pull(REPO, pr["prNumber"])
        self.assertEqual((readback["state"], readback.get("merged_at")), ("open", None))

    def test_the_approved_setup_merges_exactly_and_reads_the_repo_back_ready(self):
        pr = self.setup_pr()
        self.green()
        merged = self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")

        self.assertEqual((merged["prNumber"], merged["mergeSha"], merged["alreadyMerged"]),
                         (pr["prNumber"], MERGE, False))
        self.assertTrue(merged["ready"])  # gate config, workflow and standard all landed
        readback = self.github.pull(REPO, pr["prNumber"])
        self.assertEqual((readback["state"], readback["merged_at"], readback["merge_commit_sha"]),
                         ("closed", "2026-09-24T03:00:00Z", MERGE))
        actions = [entry["action"] for entry in self.foreman.store.global_history()]
        self.assertEqual(actions[-3:],
                         ["setup_merge_authorized", "setup_merge_journalled", "setup_pr_merged"])
        journalled = self.audit("setup_merge_journalled")[0]
        self.assertEqual((journalled["repo"], journalled["prNumber"], journalled["headSha"]),
                         (REPO, pr["prNumber"], HEAD))

    def test_a_repeat_with_the_same_request_id_never_merges_a_second_time(self):
        pr = self.setup_pr()
        self.green()
        first = self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")
        again = self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")  # the answer was seen once
        self.assertEqual((again["prNumber"], again["mergeSha"]), (first["prNumber"], MERGE))
        self.assertTrue(again["alreadyMerged"])
        self.assertEqual(len(self.github.pulls), 1)  # no duplicate PR, no duplicated audit line

    def test_an_ambiguous_merge_response_reconciles_the_same_pr(self):
        pr = self.setup_pr()
        self.green()
        real = self.github.merge_pull

        def lost_response(repo, number, sha):
            real(repo, number, sha)  # GitHub merged; this caller's connection died
            raise GitHubError(500, "connection died")

        self.github.merge_pull = lost_response
        with self.assertRaisesRegex(ToolError, "was not merged"):
            self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")
        retried = self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")  # the same request resumes
        self.assertEqual(retried["mergeSha"], MERGE)
        readback = self.github.pull(REPO, pr["prNumber"])
        self.assertEqual(readback["merge_commit_sha"], MERGE)
        self.assertTrue(retried["alreadyMerged"])

    def test_a_fresh_request_recovering_an_already_merged_setup_reads_it_back(self):
        # The merge landed but the journal never heard of this request: it resumes, not errors.
        pr = self.setup_pr()
        self.green()
        self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")
        for request_id in ("req-2", "req-2"):  # the fresh request, then its own retry
            again = self.foreman.merge_setup_pr(REPO, "day-desk", request_id)
            self.assertEqual((again["prNumber"], again["mergeSha"], again["alreadyMerged"]),
                             (pr["prNumber"], MERGE, True))
            self.assertEqual(self.foreman.store.request(request_id)["phase"], "done")

    def test_a_refreshed_setup_pr_merges_on_the_branch_the_factory_created(self):
        """A plan refreshed after creation renames no branch: the factory's own PR, journalled when
        bootstrap created it, still merges under a grant for the refreshed digest, and an
        interrupted or repeated request reads the same merge back instead of merging again."""
        pr = self.setup_pr()  # the branch names the plan digest the moment bootstrap created it
        self.github.push(REPO, "main", "base1")  # a commit lands; the canonical plan refreshes
        again = self.foreman.bootstrap_repo(REPO, VERIFY, "owner")  # the retry adopts the same PR
        self.assertEqual((again["prNumber"], len(self.github.pulls)), (pr["prNumber"], 1))
        self.foreman.authorize_setup_merge(REPO, VERIFY, "amir")  # the owner approves the refresh
        self.assertNotEqual("factory-bootstrap/" + self.foreman.store.setup_merge(REPO)["fingerprint"][:16],
                            self.github.pulls[0]["head"]["ref"])
        self.green()

        merged = self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")
        self.assertEqual((merged["prNumber"], merged["mergeSha"], merged["alreadyMerged"]),
                         (pr["prNumber"], MERGE, False))
        resumed = self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")
        fresh = self.foreman.merge_setup_pr(REPO, "day-desk", "req-2")
        for readback in (resumed, fresh):
            self.assertEqual((readback["prNumber"], readback["mergeSha"], readback["alreadyMerged"]),
                             (pr["prNumber"], MERGE, True))
        self.assertEqual(len(self.github.pulls), 1)  # no duplicate merge, no duplicate PR

    def test_a_forged_setup_branch_the_factory_never_created_never_merges(self):
        """A branch name is a marker anyone with push access can forge: even the authorized
        digest's exact branch name, carrying canonical content with a green check, refuses when
        the factory's journal records no setup PR it created or adopted there."""
        self.setup_pr()
        self.github.close_issue(REPO, 100)  # the factory's own setup PR was closed by hand
        plan = repos.bootstrap_plan(self.github, REPO, VERIFY)
        branch = "factory-bootstrap/" + plan["fingerprint"][:16]  # the exact authorized name
        self.github.push(REPO, branch, "forged-sha", files=[(name, 1, 0) for name in plan["files"]])
        for name, text in plan["files"].items():
            self.github.contents[(REPO, name, "forged-sha")] = text
        self.github.add_pr(REPO, 9, branch, "forged-sha")
        self.github.add_run(REPO, "forged-sha")

        with self.assertRaisesRegex(ToolError, "the factory's journal never records"):
            self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")
        readback = self.github.pull(REPO, 9)
        self.assertEqual((readback["state"], readback.get("merged_at")), ("open", None))

    def test_a_deputy_pr_on_the_factorys_own_branch_never_merges(self):
        """The journal names the branch and the PR number together: a second PR reopened on the
        factory's own setup branch is a deputy, and never merges instead of the factory's PR."""
        self.setup_pr()
        self.green()
        self.github.close_issue(REPO, 100)
        self.github.add_pr(REPO, 9, self.github.pulls[0]["head"]["ref"], HEAD)

        with self.assertRaisesRegex(ToolError, "the factory's journal never records"):
            self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")
        readback = self.github.pull(REPO, 9)
        self.assertEqual((readback["state"], readback.get("merged_at")), ("open", None))

    def test_revocation_takes_the_authorization_back_and_is_audited(self):
        self.setup_pr()
        self.green()
        self.foreman.revoke_setup_merge(REPO, "amir")
        self.assertIsNone(self.foreman.store.setup_merge(REPO))
        with self.assertRaisesRegex(ToolError, "no setup-merge authorization"):
            self.foreman.merge_setup_pr(REPO, "day-desk", "req-1")
        with self.assertRaisesRegex(ToolError, "no setup-merge authorization"):
            self.foreman.revoke_setup_merge(REPO, "amir")  # revoking an absent grant refuses
        self.assertEqual(self.audit("setup_merge_revoked"), [{"repo": REPO, "by": "amir"}])
        self.foreman.authorize_setup_merge(REPO, VERIFY, "amir")  # the owner may grant it again
        merged = self.foreman.merge_setup_pr(REPO, "day-desk", "req-2")
        self.assertEqual(merged["mergeSha"], MERGE)


if __name__ == "__main__":
    unittest.main()
