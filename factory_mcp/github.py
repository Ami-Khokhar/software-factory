"""GitHub REST over urllib. Only the calls the Foreman needs; every return value is plain JSON."""
import base64
import datetime
import email.utils
import http.client
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request


GET_ATTEMPTS = 3
GET_RETRY_WAIT_SECONDS = 2


class GitHubError(Exception):
    def __init__(self, status, message):
        super().__init__(f"GitHub {status}: {message}")
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # the caller follows it, without the token
        return None


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
        waited = 0
        for attempt in range(GET_ATTEMPTS if method == "GET" else 1):
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    raw = response.read()
                    return (json.loads(raw) if raw else None), response.headers.get("Link", "")
            except urllib.error.HTTPError as err:
                message = err.read().decode("utf-8", "replace")[:300]
                if method != "GET" or (err.code != 429 and not 500 <= err.code <= 599):
                    raise GitHubError(err.code, message) from None
                delay = self._retry_delay(err.headers, attempt)
                if attempt + 1 == GET_ATTEMPTS or waited + delay > GET_RETRY_WAIT_SECONDS:
                    raise GitHubError(err.code, message) from None
            except (urllib.error.URLError, ConnectionResetError, TimeoutError,
                    http.client.HTTPException) as exc:
                if method != "GET":
                    raise
                if attempt + 1 == GET_ATTEMPTS:
                    raise GitHubError(503, self._transport_error(attempt + 1, exc)) from None
                delay = self._retry_delay({}, attempt)
                if waited + delay > GET_RETRY_WAIT_SECONDS:
                    raise GitHubError(503, self._transport_error(attempt + 1, exc)) from None
            time.sleep(delay)
            waited += delay

    @staticmethod
    def _retry_delay(headers, attempt):
        """Honor Retry-After when it fits the GET retry's bounded wait; otherwise back off briefly."""
        value = headers.get("Retry-After")
        if value:
            try:
                delay = float(value)
                return max(0, delay) if math.isfinite(delay) else GET_RETRY_WAIT_SECONDS + 1
            except ValueError:
                try:
                    when = email.utils.parsedate_to_datetime(value)
                    return max(0, (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        return 0.1 * (attempt + 1)

    @staticmethod
    def _transport_error(attempts, error):
        return f"transient GitHub GET transport failure after {attempts} attempts ({type(error).__name__})"

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

    def all_issues(self, repo):
        """Include closed issues when adopting durable improvement identities."""
        return [issue for issue in self._get_all(f"/repos/{repo}/issues", state="all")
                if "pull_request" not in issue]

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

    def commit(self, repo, sha):
        """One commit with its parents, its GitHub author and committer, and its signature check."""
        return self._request("GET", f"{self.api}/repos/{repo}/commits/{sha}")[0]

    def open_pulls(self, repo, base):
        """The open PRs into `base`."""
        return self._get_all(f"/repos/{repo}/pulls", state="open", base=base)

    def comments(self, repo, number):
        return self._get_all(f"/repos/{repo}/issues/{number}/comments")

    def check_runs(self, repo, sha, name="pr-gatekeeper"):
        return self._get_all(f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs", check_name=name)

    def workflow_runs(self, repo, sha, name="pr-gatekeeper"):
        """The runs of workflow `name` on commit `sha`, newest first."""
        runs = self._get_all(f"/repos/{repo}/actions/runs", key="workflow_runs", head_sha=sha)
        return [run for run in runs if run.get("name") == name]

    def run_log_tail(self, repo, run_id, chars=2000):
        """The last `chars` of the log of the first failed job of a workflow run, or why there is none."""
        try:
            jobs = self._request("GET", f"{self.api}/repos/{repo}/actions/runs/{run_id}/jobs")[0]["jobs"]
            failed = next((j for j in jobs if j.get("conclusion") not in ("success", "skipped")), None)
            if failed is None:
                return "(no failed job in the run)"
            request = urllib.request.Request(f"{self.api}/repos/{repo}/actions/jobs/{failed['id']}/logs", headers={
                "Accept": "application/vnd.github+json", "User-Agent": "factory-mcp",
                "Authorization": f"Bearer {self.token}"})
            try:  # GitHub answers with a redirect to a signed URL, which gets no token
                with urllib.request.build_opener(_NoRedirect).open(request, timeout=20) as response:
                    text = response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as err:
                if err.code not in (301, 302, 307) or not err.headers.get("Location"):
                    raise
                with urllib.request.urlopen(err.headers["Location"], timeout=60) as response:
                    text = response.read().decode("utf-8", "replace")
            return text[-chars:]
        except (GitHubError, urllib.error.URLError, OSError, KeyError) as exc:  # OSError: a read that timed out
            return f"(the run's log could not be read: {exc})"

    def delete_branch(self, repo, branch):
        self._request("DELETE", f"{self.api}/repos/{repo}/git/refs/heads/{urllib.parse.quote(branch)}")

    def repository(self, repo):
        return self._request("GET", f"{self.api}/repos/{repo}")[0]

    def file(self, repo, path, ref=None):
        """A file's text on the default branch (or at `ref`), or None if there is no such file."""
        query = "?" + urllib.parse.urlencode({"ref": ref}) if ref else ""
        try:
            doc = self._request("GET", f"{self.api}/repos/{repo}/contents/{urllib.parse.quote(path)}{query}")[0]
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

    def edit_issue(self, repo, number, body):
        """Replace an issue's body."""
        return self._request("PATCH", f"{self.api}/repos/{repo}/issues/{number}", {"body": body})[0]

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

    def merge_pull(self, repo, number, sha):
        """Merge PR `number` with the API, naming `sha` so GitHub refuses when the head moved
        between this call's earlier reads and the merge. Failures surface as GitHubError."""
        return self._request("PUT", f"{self.api}/repos/{repo}/pulls/{number}/merge",
                             {"sha": sha, "merge_method": "squash"})[0]

    def create_pull(self, repo, head, base, title, body):
        return self._request("POST", f"{self.api}/repos/{repo}/pulls",
                             {"head": head, "base": base, "title": title, "body": body})[0]

    def edit_pull(self, repo, number, body):
        """Replace a PR's body."""
        return self._request("PATCH", f"{self.api}/repos/{repo}/pulls/{number}", {"body": body})[0]
