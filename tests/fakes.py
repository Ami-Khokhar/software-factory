"""Shared test doubles and builders."""


def workitem(**fields):
    doc = {"schemaVersion": "factory.workitem/v1", "id": "o/r#1", "repo": "o/r", "issueNumber": 1,
           "goal": "g", "acceptance": [{"claim": "c", "doneWhen": "d"}], "state": "issued",
           "stateVersion": 0, "createdAt": "2026-09-24T00:00:00Z", "updatedAt": "2026-09-24T00:00:00Z"}
    doc.update(fields)
    return doc


ISSUE_BODY = """Make the relay fail loud.

## Acceptance
- default_config_path() raises when the user registry is missing — done when: `python3 -m unittest tests.test_config` passes
- [ ] README names the registry path (done when: README.md contains ~/.config/agent-relay/agents.json)
"""


def marker_comment(outcome, round_, head_sha, findings=()):
    import json
    payload = json.dumps({"round": round_, "outcome": outcome, "head_sha": head_sha,
                          "findings": list(findings)}).replace(">", "\\u003e")
    return f"### pr-gatekeeper: {outcome}\n\n<!-- pr-gatekeeper {payload} -->"


class FakeGitHub:
    """In-memory GitHub with the same methods as factory_mcp.github.GitHub."""

    def __init__(self):
        self.issues, self.pulls, self.issue_comments, self.runs = {}, [], {}, {}

    def add_issue(self, repo, number, labels=("factory:ready",), body=ISSUE_BODY, state="open"):
        self.issues[(repo, number)] = {"number": number, "title": f"Goal {number}", "body": body,
                                       "state": state, "labels": [{"name": n} for n in labels]}

    def add_pr(self, repo, number, branch, sha, state="open", merged=False):
        self.pulls.append({"repo": repo, "number": number, "state": state,
                           "merged_at": "2026-09-24T01:00:00Z" if merged else None,
                           "head": {"ref": branch, "sha": sha}, "base": {"ref": "main", "sha": "base0"}})

    def add_verdict(self, repo, pr, outcome, round_, head_sha, findings=()):
        self.issue_comments.setdefault((repo, pr), []).append(
            {"body": marker_comment(outcome, round_, head_sha, findings),
             "created_at": "2026-09-24T02:00:00Z"})

    def add_run(self, repo, sha, status="completed", conclusion="success"):
        self.runs[(repo, sha)] = [{"name": "pr-gatekeeper", "status": status, "conclusion": conclusion}]

    def issue(self, repo, number):
        from factory_mcp.github import GitHubError
        if (repo, number) not in self.issues:
            raise GitHubError(404, "Not Found")
        return self.issues[(repo, number)]

    def labelled_issues(self, repo, label):
        return [i for (r, _), i in sorted(self.issues.items()) if r == repo and i["state"] == "open"
                and label in {n["name"] for n in i["labels"]}]

    def pulls_for_branch(self, repo, branch):
        return [p for p in self.pulls if p["repo"] == repo and p["head"]["ref"] == branch]

    def comments(self, repo, number):
        return list(self.issue_comments.get((repo, number), []))

    def check_runs(self, repo, sha, name="pr-gatekeeper"):
        return list(self.runs.get((repo, sha), []))
