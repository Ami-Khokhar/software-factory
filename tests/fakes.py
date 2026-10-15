"""Shared test doubles and builders."""
from factory_mcp.github import GitHubError


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
        self.on_merge = None  # a test's stand-in for the content a merge puts on the default branch
        self.issues, self.pulls, self.issue_comments, self.runs = {}, [], {}, {}
        self.workflow_run_list = {}  # (repo, sha) -> the gate's workflow runs, newest first
        self.posted = []  # (repo, number, body) of every comment the Foreman wrote
        self.branches, self.files = {}, {}  # (repo, branch) -> sha; (repo, sha) -> changed files
        self.repos = {}  # repo -> fields that differ from a pushable repo; None: GitHub has no such repo
        self.contents = {}  # (repo, path) -> file text on the default branch
        self.labels = {}  # repo -> names of the labels the Foreman created
        self.labelled = []  # (repo, number, labels) of every label the Foreman added to an issue
        self.logs, self.deleted = {}, []  # run id -> failed job log; (repo, branch) of each branch deleted
        self.commits = {}  # (repo, sha) -> GitHub's commit; any other commit is a person's push
        self.blocked = False  # when True, merge_pull refuses as branch protection does

    def add_issue(self, repo, number, labels=("factory:ready",), body=ISSUE_BODY, state="open", author=None):
        self.issues[(repo, number)] = {"number": number, "title": f"Goal {number}", "body": body,
                                       "state": state, "labels": [{"name": n} for n in labels],
                                       "user": {"login": author or repo.split("/")[0]}}

    def add_pr(self, repo, number, branch, sha, state="open", merged=False):
        self.pulls.append({"repo": repo, "number": number, "state": state,
                           "merged_at": "2026-09-24T01:00:00Z" if merged else None,
                           "mergeable": True if state == "open" else None,
                           "head": {"ref": branch, "sha": sha, "repo": {"full_name": repo}},
                           "base": {"ref": "main", "sha": "base0"}})

    def add_verdict(self, repo, pr, outcome, round_, head_sha, findings=()):
        self.issue_comments.setdefault((repo, pr), []).append(
            {"body": marker_comment(outcome, round_, head_sha, findings),
             "created_at": "2026-09-24T02:00:00Z", "user": {"login": "github-actions[bot]"}})

    def add_run(self, repo, sha, status="completed", conclusion="success", started_at="2026-09-24T01:30:00Z"):
        self.runs[(repo, sha)] = [{"name": "pr-gatekeeper", "status": status, "conclusion": conclusion,
                                   "started_at": started_at}]

    def issue(self, repo, number):
        from factory_mcp.github import GitHubError
        if (repo, number) not in self.issues:
            raise GitHubError(404, "Not Found")
        return self.issues[(repo, number)]

    def labelled_issues(self, repo, label):
        return [i for (r, _), i in sorted(self.issues.items()) if r == repo and i["state"] == "open"
                and label in {n["name"] for n in i["labels"]}]

    def labelled_issues_by(self, author, label):
        return [dict(i, repo=r) for (r, _), i in sorted(self.issues.items()) if i["state"] == "open"
                and i["user"]["login"].lower() == author.lower() and label in {n["name"] for n in i["labels"]}]

    def pulls_for_branch(self, repo, branch):
        return [p for p in self.pulls if p["repo"] == repo and p["head"]["ref"] == branch]

    def pull(self, repo, number):
        try:
            return next(p for p in self.pulls if p["repo"] == repo and p["number"] == number)
        except StopIteration:
            raise GitHubError(404, f"{number} is not a pull request on {repo}") from None

    def commit(self, repo, sha):
        return self.commits.get((repo, sha)) or {"sha": sha, "author": None, "committer": None, "parents": [],
                                                 "commit": {"verification": {"verified": False}}}

    def gate_merges_base(self, repo, branch, sha):
        """pr-gatekeeper merges the base branch into a PR branch that fell behind it, through GitHub's
        API with the workflow's token, as it does before it reviews."""
        self.commits[(repo, sha)] = {"sha": sha, "author": {"login": "github-actions[bot]"},
                                     "committer": {"login": "web-flow"},
                                     "parents": [{"sha": self.branches[(repo, branch)]}, {"sha": "base1"}],
                                     "commit": {"verification": {"verified": True}}}
        self.push(repo, branch, sha)

    def open_pulls(self, repo, base):
        return [p for p in self.pulls if p["repo"] == repo and p["state"] == "open" and p["base"]["ref"] == base]

    def comments(self, repo, number):
        return list(self.issue_comments.get((repo, number), []))

    def check_runs(self, repo, sha, name="pr-gatekeeper"):
        return list(self.runs.get((repo, sha), []))

    def workflow_runs(self, repo, sha, name="pr-gatekeeper"):
        return [run for run in self.workflow_run_list.get((repo, sha), []) if run.get("name") == name]

    def run_log_tail(self, repo, run_id, chars=2000):
        return self.logs.get(run_id, "(no log)")[-chars:]

    def delete_branch(self, repo, branch):
        self.deleted.append((repo, branch))
        self.branches.pop((repo, branch), None)

    def repository(self, repo):
        from factory_mcp.github import GitHubError
        if self.repos.get(repo, {}) is None:
            raise GitHubError(404, "Not Found")
        return dict({"full_name": repo, "default_branch": "main", "archived": False,
                     "permissions": {"push": True}}, **self.repos.get(repo, {}))

    def file(self, repo, path, ref=None):
        """The file on the default branch; `contents[(repo, path, ref)]` overrides it at that ref."""
        if (repo, path, ref) in self.contents:
            return self.contents[(repo, path, ref)]
        return self.contents.get((repo, path))

    def branch_head(self, repo, branch):
        return self.branches.get((repo, branch), "base0" if branch == "main" else None)

    def has_refs(self, repo):
        return any(self.branch_head(r, b) for r, b in {*self.branches, (repo, "main")} if r == repo)

    def push(self, repo, branch, sha, files=(("src/app.py", 10, 2),)):
        self.branches[(repo, branch)] = sha
        for pr in self.pulls:  # an open PR follows its branch, as on GitHub
            if pr["repo"] == repo and pr["head"]["ref"] == branch and pr["state"] == "open":
                pr.update(mergeable=None)  # GitHub computes it again for the new head
                pr["head"]["sha"] = sha
        self.files[(repo, sha)] = [f if isinstance(f, dict) else  # a dict is a GitHub file entry as is
                                   {"filename": f[0], "status": "modified", "additions": f[1], "deletions": f[2]}
                                   for f in files]

    def changed_files(self, repo, base, head):
        return self.files[(repo, head)]

    def comment(self, repo, number, body):
        self.posted.append((repo, number, body))
        return {"id": len(self.posted), "body": body}

    def close_issue(self, repo, number):
        # a PR on GitHub closes through the issue endpoint too and reads back through /pulls
        for pr in self.pulls:
            if pr["repo"] == repo and pr["number"] == number and pr["state"] == "open":
                pr["state"] = "closed"
                pr["mergeable"] = None
        issue = self.issues.setdefault((repo, number), {"number": number, "title": f"Goal {number}",
                                                        "body": "", "state": "closed",
                                                        "labels": [], "user": {"login": "o"}})
        issue["state"] = "closed"

    def reopen_issue(self, repo, number):
        self.issues[(repo, number)]["state"] = "open"

    def edit_issue(self, repo, number, body):
        self.issues[(repo, number)]["body"] = body
        return self.issues[(repo, number)]

    def create_label(self, repo, name, color, description):
        self.labels.setdefault(repo, set()).add(name)

    def add_labels(self, repo, number, labels):
        self.labelled.append((repo, number, list(labels)))
        self.issues[(repo, number)]["labels"] += [{"name": name} for name in labels]
        return self.issues[(repo, number)]["labels"]

    def create_issue(self, repo, title, body, labels):
        number = 200 + len(self.issues)
        self.add_issue(repo, number, labels=labels, body=body)
        self.issues[(repo, number)]["title"] = title
        return dict(self.issues[(repo, number)], html_url=f"https://github.com/{repo}/issues/{number}")

    def create_pull(self, repo, head, base, title, body):
        number = 100 + len(self.pulls)
        self.add_pr(repo, number, head, self.branches[(repo, head)])
        self.pulls[-1].update(title=title, body=body)
        return self.pulls[-1]

    def merge_pull(self, repo, number, sha):
        """The merge API with its optimistic head check: a moved head or a protected-branch
        denial is lore GitHub returns 405. On success the PR reads back merged."""
        pr = self.pull(repo, number)
        if self.blocked and not pr.get("merged_at"):
            raise GitHubError(405, "not allowed: changes must be made through a pull request")
        if self.branches.get((repo, pr["head"]["ref"])) != sha:
            raise GitHubError(405, "Pull Request is not mergeable: the head changed")
        pr.update(merged=True, merged_at="2026-09-24T03:00:00Z", merge_commit_sha="merge-sha",
                  state="closed")
        if self.on_merge:
            self.on_merge()
        return {"sha": "merge-sha", "merged": True}

    def edit_pull(self, repo, number, body):
        pr = next(p for p in self.pulls if p["repo"] == repo and p["number"] == number)
        pr["body"] = body
        return pr


class FakeRelay:
    """agent-relay's task API in memory, including requestId idempotency."""

    def __init__(self):
        self.tasks, self.by_request, self.down = {}, {}, False

    def submit(self, agent_id, text, request_id, timeout_ms, cwd=None):
        from factory_mcp.relay import RelayError
        if self.down:
            raise RelayError("relay unreachable: connection refused")
        if request_id not in self.by_request:
            task_id = f"task-{len(self.tasks) + 1}"
            self.tasks[task_id] = {"id": task_id, "agentId": agent_id, "input": text, "status": "running",
                                   "sessionId": f"s-{task_id}", "timeoutMs": timeout_ms, "cwd": cwd}
            self.by_request[request_id] = task_id
        return self.tasks[self.by_request[request_id]]

    def get(self, task_id):
        return self.tasks.get(task_id)

    def reload(self):
        from factory_mcp.relay import RelayError
        if self.down:
            raise RelayError("relay unreachable: connection refused")
        self.reloads = getattr(self, "reloads", 0) + 1
        return {"ok": True, "agents": 0}

    def finish(self, task_id, status="completed", output="", error=None):
        self.tasks[task_id].update(status=status, output=output, error=error)


class FakeGit:
    """factory_mcp.patches.apply at the git boundary. Pushing a patch puts `sha`, changing `files`,
    on the branch in FakeGitHub, unless `lands` is false: then GitHub does not show the push."""

    def __init__(self, github):
        self.github, self.pushes = github, []
        self.sha, self.files, self.lands = "c1", (("src/app.py", 10, 2),), True

    def apply(self, url, token, branch, base_sha, patch, message, force, protected=(), limits=None):
        self.pushes.append({"branch": branch, "base": base_sha, "force": force})
        if self.lands:
            self.github.push(url.removeprefix("https://github.com/").removesuffix(".git"), branch, self.sha,
                             self.files)
        return self.sha

    def install(self, test):
        """Stand in for patches.apply until `test` ends."""
        from unittest import mock
        from factory_mcp import patches
        patcher = mock.patch.object(patches, "apply", self.apply)
        patcher.start()
        test.addCleanup(patcher.stop)
        return self


class FakeBootstrapGit:
    """The bootstrap's pushes in factory_mcp.patches, at the git boundary: initial_commit, bootstrap
    and update_bootstrap, each with its real signature. A push lands on the branch in FakeGitHub."""

    def __init__(self, github):
        self.github = github
        self.initial, self.pushed, self.updates = [], [], []  # the arguments of each call
        self.held = {}  # branch -> the files it holds

    @staticmethod
    def _repo(url):
        return url.removeprefix("https://github.com/").removesuffix(".git")

    def initial_commit(self, url, token, branch, files, message):
        self.initial.append((url, branch, files))
        self.github.branches[(self._repo(url), branch)] = "readme-sha"
        return "readme-sha"

    def bootstrap(self, url, token, branch, base_sha, files, message):
        self.pushed.append((url, branch, base_sha, files, message))
        self.held[branch] = dict(files)
        self.github.push(self._repo(url), branch, "setup-sha", files=[(name, 1, 0) for name in files])
        return "setup-sha"

    def update_bootstrap(self, url, token, branch, head_sha, files, message):
        """The new head, or None when the branch holds `files` already, as patches.update_bootstrap."""
        self.updates.append((branch, head_sha, files))
        held = self.held.setdefault(branch, {})
        if all(held.get(name) == text for name, text in files.items()):
            return None
        held.update(files)
        self.github.push(self._repo(url), branch, "update-sha", files=[(name, 1, 0) for name in held])
        return "update-sha"

    def install(self, test):
        """Stand in for the three bootstrap pushes until `test` ends."""
        from unittest import mock
        from factory_mcp import patches
        for name in ("initial_commit", "bootstrap", "update_bootstrap"):
            patcher = mock.patch.object(patches, name, getattr(self, name))
            patcher.start()
            test.addCleanup(patcher.stop)
        return self


def result_line(order_id, diff="diff --git a/src/app.py b/src/app.py\n", fmt="unified", base_sha=None):
    """A worker's task output: what it printed, then its Result, which hands back a patch."""
    import json
    patch = {"format": fmt, "diff": diff, **({"baseSha": base_sha} if base_sha else {})}
    return "working...\n" + json.dumps({
        "schemaVersion": "factory.result/v1", "workOrderId": order_id, "status": "completed",
        "patch": patch,
        "evidence": [{"kind": "test", "command": "python3 -m unittest", "exit": 0, "tail": "OK"}],
        "startedAt": "2026-09-24T00:00:00Z", "finishedAt": "2026-09-24T00:10:00Z"})
