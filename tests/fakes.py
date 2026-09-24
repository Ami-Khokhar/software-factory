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
        self.branches, self.files = {}, {}  # (repo, branch) -> sha; (repo, sha) -> changed files

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

    def repository(self, repo):
        return {"full_name": repo, "default_branch": "main"}

    def branch_head(self, repo, branch):
        return self.branches.get((repo, branch), "base0" if branch == "main" else None)

    def push(self, repo, branch, sha, files=(("src/app.py", 10, 2),)):
        self.branches[(repo, branch)] = sha
        for pr in self.pulls:  # an open PR follows its branch, as on GitHub
            if pr["repo"] == repo and pr["head"]["ref"] == branch and pr["state"] == "open":
                pr["head"]["sha"] = sha
        self.files[(repo, sha)] = [{"filename": f, "status": "modified", "additions": a, "deletions": d}
                                   for f, a, d in files]

    def changed_files(self, repo, base, head):
        return self.files[(repo, head)]

    def create_pull(self, repo, head, base, title, body):
        number = 100 + len(self.pulls)
        self.add_pr(repo, number, head, self.branches[(repo, head)])
        self.pulls[-1].update(title=title, body=body)
        return self.pulls[-1]


class FakeRelay:
    """agent-relay's task API in memory, including requestId idempotency."""

    def __init__(self):
        self.tasks, self.by_request, self.down = {}, {}, False

    def submit(self, agent_id, text, request_id, timeout_ms):
        from factory_mcp.relay import RelayError
        if self.down:
            raise RelayError("relay unreachable: connection refused")
        if request_id not in self.by_request:
            task_id = f"task-{len(self.tasks) + 1}"
            self.tasks[task_id] = {"id": task_id, "agentId": agent_id, "input": text, "status": "running",
                                   "sessionId": f"s-{task_id}", "timeoutMs": timeout_ms}
            self.by_request[request_id] = task_id
        return self.tasks[self.by_request[request_id]]

    def get(self, task_id):
        return self.tasks.get(task_id)

    def finish(self, task_id, status="completed", output="", error=None):
        self.tasks[task_id].update(status=status, output=output, error=error)


def result_line(order_id, sha, branch="factory/7", status="completed", exit_code=0):
    import json
    return "working...\n" + json.dumps({
        "schemaVersion": "factory.result/v1", "workOrderId": order_id, "status": status,
        "artifact": {"branch": branch, "commitSha": sha},
        "evidence": [{"kind": "test", "command": "python3 -m unittest", "exit": exit_code, "tail": "OK"}],
        "startedAt": "2026-09-24T00:00:00Z", "finishedAt": "2026-09-24T00:10:00Z"})
