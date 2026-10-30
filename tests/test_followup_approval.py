"""Approval is exercised through the same MCP tool as the owner and Day Desk."""
import copy
import json
import unittest

from factory_mcp import followups, mcp, tools
from tests.test_safety import REPO, foreman
from tests.fakes import FakeGitHub, FakeRelay, ISSUE_BODY

BOT = {"login": "github-actions[bot]", "id": 41898282}
CREATED = "2026-10-17T08:52:27Z"


class FollowupApprovalTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.f = foreman(self.github, self.relay)
        self.github.add_issue(REPO, 60, author=BOT["login"], labels=(), body=(
            "pr-gatekeeper merged #58 with nothing blocking.\n\n"
            "## Minor findings\n- [ ] `app.py:1` — residual defect Fix: fix the defect\n\n" + ISSUE_BODY))
        self.issue = self.github.issues[(REPO, 60)]
        self.issue.update(id=600, created_at=CREATED, user=BOT.copy(), title="Follow-up to #58: Original")
        self.github.add_pr(REPO, 58, "factory/50", "head", state="closed", merged=True)
        self.pr = self.github.pulls[0]
        self.pr.update(title="Original", merged_by=BOT.copy(), merged_at="2026-10-17T08:52:26Z")
        self.pr["base"]["repo"] = {"full_name": REPO}
        self.github.add_verdict(REPO, 58, "MERGE", 1, "head", [
            {"severity": "minor", "path": "app.py", "line": 1,
             "problem": "residual defect", "fix": "fix the defect"}], created_at="2026-10-17T08:52:25Z")
        self.review = self.github.issue_comments[(REPO, 58)][0]
        marker = json.loads(self.review["body"].split("<!-- pr-gatekeeper ")[1].split(" -->")[0])
        self.review.update(id=900, user=BOT.copy(), body="<!-- pr-gatekeeper " + json.dumps(dict(marker, reviewed=True)) + " -->")
        self.link = {"event": "cross-referenced", "actor": BOT.copy(), "created_at": CREATED,
                     "source": {"issue": {"id": 600, "number": 60,
                                          "repository_url": f"https://api.github.com/repos/{REPO}"}}}
        self.github.timelines[(REPO, 58)] = [self.link]
        self.run = {"id": 800, "name": "pr-gatekeeper", "path": ".github/workflows/pr-gatekeeper.yml",
                    "head_sha": "head", "event": "pull_request", "conclusion": "success",
                    "created_at": "2026-10-17T08:43:25Z", "updated_at": "2026-10-17T08:52:33Z",
                    "referenced_workflows": [{"sha": "a" * 40, "path":
                        "Ami-Khokhar/pr-gatekeeper-workflow/.github/workflows/gatekeeper.yml@" + "a" * 40}]}
        self.github.workflow_run_list[(REPO, "head")] = [self.run]
        self.args = {"repo": REPO, "issue": 60, "by": "o", "sourcePr": 58,
                     "issueDigest": followups.digest(self.issue)}

    def call(self, agent=False, **args):
        result = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "factory_approve_issue", "arguments": dict(self.args, **args)}},
            tools.build(self.f.store, self.f, agent=agent))["result"]
        value = json.loads(result["content"][0]["text"])
        return result["isError"], value

    def test_owner_admits_original_and_repeated_approval_is_idempotent(self):
        for _ in range(2):
            failed, result = self.call()
            self.assertFalse(failed, result)
            self.assertEqual(result["workItem"]["id"], "o/r#60")
        self.assertEqual(len(self.f.store.all()), 1)
        self.assertEqual(len(self.github.labelled), 1)
        proof = [h["detail"] for h in self.f.store.history("o/r#60")
                 if h["action"] == "gate_followup_approved"][-1]
        self.assertEqual((proof["by"], proof["validation"], proof["reviewId"], proof["issueId"]),
                         ("o", "verified", 900, 600))
        self.f.tick("o/r#60")
        self.assertEqual(len(self.relay.tasks), 1)

    def test_bot_author_alone_still_refuses_even_an_authentic_followup(self):
        """Guard: bot authorship and a source claim alone never grant admission."""
        self.args.pop("sourcePr")
        self.args.pop("issueDigest")
        failed, result = self.call()
        self.assertTrue(failed)
        self.assertIn("not a trusted author", result["error"])
        self.assertEqual(self.f.store.all(), [])

    def test_agent_and_wrong_owner_cannot_grant_approval(self):
        for options in ({"agent": True}, {"by": "stranger"}):
            failed, result = self.call(**options)
            self.assertTrue(failed)
            self.assertIn("owner token and owner identity", result["error"])
        self.assertEqual((self.github.labelled, self.f.store.all()), ([], []))

    def test_missing_forged_or_ambiguous_records_refuse_before_labelling(self):
        for flaw in ("no link", "duplicate link", "wrong actor", "wrong issue", "human marker",
                     "wrong head", "not merged", "wrong merge actor", "no review", "duplicate review",
                     "unrelated body", "unrelated bot", "no workflow", "wrong workflow",
                     "duplicate workflow", "outside run", "wrong reusable workflow"):
            with self.subTest(flaw=flaw):
                self.setUp()
                if flaw == "no link": self.github.timelines[(REPO, 58)] = []
                if flaw == "duplicate link": self.github.timelines[(REPO, 58)].append(copy.deepcopy(self.link))
                if flaw == "wrong actor": self.link["actor"]["id"] = 1
                if flaw == "wrong issue": self.link["source"]["issue"]["id"] = 601
                if flaw == "human marker": self.review["user"]["login"] = "stranger"
                if flaw == "wrong head": self.pr["head"]["sha"] = "other"
                if flaw == "not merged": self.pr["merged_at"] = None
                if flaw == "wrong merge actor": self.pr["merged_by"]["login"] = "other"
                if flaw == "no review": self.github.issue_comments[(REPO, 58)] = []
                if flaw == "duplicate review": self.github.issue_comments[(REPO, 58)].append(copy.deepcopy(self.review))
                if flaw == "no workflow": self.github.workflow_run_list.clear()
                if flaw == "wrong workflow": self.run["path"] = ".github/workflows/unrelated.yml"
                if flaw == "duplicate workflow": self.github.workflow_run_list[(REPO, "head")].append(copy.deepcopy(self.run))
                if flaw == "outside run": self.run["updated_at"] = "2026-10-17T08:52:26Z"
                if flaw == "wrong reusable workflow": self.run["referenced_workflows"][0]["path"] = "forged"
                if flaw == "unrelated body": self.issue["body"] = ISSUE_BODY
                if flaw == "unrelated bot": self.issue["user"] = {"login": "other[bot]", "id": 1}
                failed, result = self.call(issueDigest=followups.digest(self.issue))
                self.assertTrue(failed, result)
                self.assertIn("provenance", result["error"])
                self.assertEqual((self.github.labelled, self.f.store.all()), ([], []))

    def test_stale_digest_refuses_and_later_edit_cannot_dispatch(self):
        self.issue["body"] += "\nextra scope"
        self.assertTrue(self.call()[0])
        self.issue["body"] = self.issue["body"].removesuffix("\nextra scope")
        self.assertFalse(self.call()[0])
        self.issue["body"] += "\nextra scope"
        self.assertTrue(self.call()[0])
        self.f.tick("o/r#60")
        self.assertEqual(self.f.store.get("o/r#60")["state"], "human_waiting")
        self.assertEqual(self.relay.tasks, {})
        self.assertEqual(self.f.store.workorders("o/r#60"), [])

    def test_a_pending_submission_rechecks_approval_before_relay_resubmission(self):
        self.assertFalse(self.call()[0])
        # A failed submit leaves its durable dispatch intent, as a real relay outage does.
        self.relay.down = True
        self.f.tick("o/r#60")
        self.assertEqual(self.f.store.get("o/r#60")["state"], "dispatching")
        self.issue["body"] += "\nextra scope"
        self.relay.down = False
        self.f.tick("o/r#60")
        self.assertEqual(self.relay.tasks, {})
        self.assertEqual(self.f.store.get("o/r#60")["state"], "human_waiting")

    def test_changed_provenance_cannot_dispatch(self):
        self.assertFalse(self.call()[0])
        self.github.timelines[(REPO, 58)] = []
        self.f.tick("o/r#60")
        self.assertEqual(self.relay.tasks, {})
        self.assertEqual(self.f.store.get("o/r#60")["state"], "human_waiting")

    def test_acceptance_is_still_required(self):
        self.issue["body"] = self.issue["body"].split("Make the relay")[0]
        failed, result = self.call(issueDigest=followups.digest(self.issue))
        self.assertTrue(failed)
        self.assertIn("Acceptance", result["error"])
        self.assertEqual(self.github.labelled, [])
