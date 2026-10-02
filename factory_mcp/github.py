"""GitHub REST over urllib. Only the calls the Foreman needs; every return value is plain JSON."""
import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request


class GitHubError(Exception):
    def __init__(self, status, message):
        super().__init__(f"GitHub {status}: {message}")
        self.status = status


class GitHub:
    def __init__(self, token, api="https://api.github.com"):
        self.token = token
        self.api = api

    def _request(self, method, url, body=None):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "factory-mcp"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
                return (json.loads(raw) if raw else None), response.headers.get("Link", "")
        except urllib.error.HTTPError as err:
            raise GitHubError(err.code, err.read().decode("utf-8", "replace")[:300]) from None

    def _get_all(self, path, key=None, **query):
        url = f"{self.api}{path}?" + urllib.parse.urlencode(dict(query, per_page=100))
        items = []
        while url:
            page, link = self._request("GET", url)
            items.extend(page[key] if key else page)
            match = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = match.group(1) if match else None
        return items

    def issue(self, repo, number):
        return self._request("GET", f"{self.api}/repos/{repo}/issues/{number}")[0]

    def labelled_issues(self, repo, label):
        return [i for i in self._get_all(f"/repos/{repo}/issues", labels=label, state="open")
                if "pull_request" not in i]

    def labelled_issues_by(self, author, label):
        """Open issues labelled `label` that `author` opened, in any repo the token can see. Each
        carries its `repo`, which the search API gives only as a URL."""
        items = self._get_all("/search/issues", key="items", q=f'is:issue is:open label:"{label}" author:{author}')
        return [dict(i, repo=i["repository_url"].split("/repos/", 1)[1]) for i in items]

    def pulls_for_branch(self, repo, branch):
        owner = repo.split("/")[0]
        return self._get_all(f"/repos/{repo}/pulls", head=f"{owner}:{branch}", state="all")

    def pull(self, repo, number):
        """One PR with `mergeable`, which the list endpoints leave out: False when the PR conflicts
        with its base, None while GitHub computes it."""
        return self._request("GET", f"{self.api}/repos/{repo}/pulls/{number}")[0]

    def open_pulls(self, repo, base):
        """The open PRs into `base`."""
        return self._get_all(f"/repos/{repo}/pulls", state="open", base=base)

    def comments(self, repo, number):
        return self._get_all(f"/repos/{repo}/issues/{number}/comments")

    def check_runs(self, repo, sha, name="pr-gatekeeper"):
        return self._get_all(f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs", check_name=name)

    def repository(self, repo):
        return self._request("GET", f"{self.api}/repos/{repo}")[0]

    def file(self, repo, path):
        """A file's text on the default branch, or None if there is no such file."""
        try:
            doc = self._request("GET", f"{self.api}/repos/{repo}/contents/{urllib.parse.quote(path)}")[0]
        except GitHubError as err:
            if err.status == 404:
                return None
            raise
        return base64.b64decode(doc["content"]).decode("utf-8", "replace")

    def branch_head(self, repo, branch):
        """The commit a branch points at, or None if it does not exist."""
        try:
            return self._request("GET", f"{self.api}/repos/{repo}/branches/{branch}")[0]["commit"]["sha"]
        except GitHubError as err:
            if err.status == 404:
                return None
            raise

    def has_refs(self, repo):
        """Whether the repo has any ref, a branch or a tag. An empty repo has none."""
        try:
            return bool(self._request("GET", f"{self.api}/repos/{repo}/git/matching-refs/")[0])
        except GitHubError as err:
            if err.status == 409:  # GitHub's answer for an empty repo: "Git Repository is empty."
                return False
            raise

    def changed_files(self, repo, base, head):
        """[{filename, additions, deletions, status}] between base and head (three-dot)."""
        return self._request("GET", f"{self.api}/repos/{repo}/compare/{base}...{head}")[0]["files"]

    def comment(self, repo, number, body):
        return self._request("POST", f"{self.api}/repos/{repo}/issues/{number}/comments", {"body": body})[0]

    def close_issue(self, repo, number):
        return self._request("PATCH", f"{self.api}/repos/{repo}/issues/{number}",
                             {"state": "closed", "state_reason": "completed"})[0]

    def create_label(self, repo, name, color, description):
        """Create a label. One that exists already is left as it is."""
        try:
            self._request("POST", f"{self.api}/repos/{repo}/labels",
                          {"name": name, "color": color, "description": description})
        except GitHubError as err:
            if err.status != 422 or "already_exists" not in str(err):
                raise

    def add_labels(self, repo, number, labels):
        return self._request("POST", f"{self.api}/repos/{repo}/issues/{number}/labels", {"labels": labels})[0]

    def create_issue(self, repo, title, body, labels):
        return self._request("POST", f"{self.api}/repos/{repo}/issues",
                             {"title": title, "body": body, "labels": labels})[0]

    def create_pull(self, repo, head, base, title, body):
        return self._request("POST", f"{self.api}/repos/{repo}/pulls",
                             {"head": head, "base": base, "title": title, "body": body})[0]

    def edit_pull(self, repo, number, body):
        """Replace a PR's body."""
        return self._request("PATCH", f"{self.api}/repos/{repo}/pulls/{number}", {"body": body})[0]
