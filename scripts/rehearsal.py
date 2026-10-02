#!/usr/bin/env python3
"""Dress rehearsal: one issue from factory:ready to closed, with everything real except GitHub.

Runs the real agent-relay (from a checkout) with a scripted worker that edits a real git repo,
the real factory-mcp server over MCP HTTP, and a stand-in GitHub REST API backed by a bare repo.
Like worker/runner.py, the worker commits locally, never pushes, and hands back a patch Result;
factory-mcp pushes the patch to the bare repo (FACTORY_GIT_URL), as it pushes to GitHub in
production. The harness plays pr-gatekeeper: one CHANGES round, then MERGE.

    python3 scripts/rehearsal.py /path/to/agent-relay [--lose-store]

--lose-store kills factory-mcp and deletes its SQLite file once the PR is open, so the rest of
the run depends on rebuilding from GitHub (ADR-0009).
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

REPO = "Ami-Khokhar/demo"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISSUE = """The greeting is wrong.

## Acceptance
- greet() returns "hello, factory" — done when: `python3 -m unittest test_app` passes
"""

WORKER = r'''
import json, subprocess, sys, datetime
text = sys.argv[-1]
order = json.loads(text.split("WorkOrder:\n", 1)[1])
branch = "factory/" + order["workItemId"].rsplit("#", 1)[1]
def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout
started = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
git("fetch", "-q", "origin")
base = order["headSha"] if order["kind"] == "fix" else order["baseSha"]
git("checkout", "-q", "-B", branch, base)
if order["kind"] == "fix":
    open("test_app.py", "a").write("\n    def test_names_factory(self):\n        self.assertIn(\"factory\", greet())\n")
else:
    open("app.py", "w").write('def greet():\n    return "hello, factory"\n')
git("commit", "-q", "-am", order["kind"] + ": " + order["goal"])
tests = subprocess.run([sys.executable, "-m", "unittest", "test_app"], capture_output=True, text=True)
print("worker log line that is not JSON")
print(json.dumps({"schemaVersion": "factory.result/v1", "workOrderId": order["id"], "status": "completed",
    "patch": {"format": "unified", "diff": git("diff", "--binary", base, "HEAD"), "baseSha": base},
    "evidence": [{"kind": "test", "command": "python3 -m unittest test_app", "exit": tests.returncode,
                  "tail": tests.stderr[-200:]}],
    "startedAt": started, "finishedAt": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}))
'''


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="w", GIT_AUTHOR_EMAIL="w@x",
                                   GIT_COMMITTER_NAME="w", GIT_COMMITTER_EMAIL="w@x")).stdout.strip()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class GitHub:
    """The REST endpoints factory_mcp.github uses, backed by a bare repository."""

    def __init__(self, bare):
        self.bare, self.issues, self.pulls, self.comments, self.runs = bare, {}, [], {}, {}
        self.requests = []

    def head(self, branch):
        try:
            return git("rev-parse", f"refs/heads/{branch}", cwd=self.bare)
        except subprocess.CalledProcessError:
            return None

    def pull_view(self, pr):
        return dict(pr, mergeable=True if pr["state"] == "open" else None,
                    head={"ref": pr["branch"], "sha": pr["sha"] if pr["state"] == "closed"
                              else self.head(pr["branch"])}, base={"ref": "main", "sha": self.head("main")})

    def route(self, method, path, query, body):
        base = f"/repos/{REPO}"
        if not path.startswith(base):
            return 404, {"message": "Not Found"}
        rest = path[len(base):]
        if method == "GET" and rest == "":
            return 200, {"full_name": REPO, "default_branch": "main"}
        if match := re.fullmatch(r"/issues/(\d+)", rest):
            issue = self.issues.get(int(match.group(1)))
            if issue and method == "PATCH":
                issue.update(state=body["state"])
            return (200, issue) if issue else (404, {"message": "Not Found"})
        if rest == "/issues" and method == "GET":
            label = query["labels"][0]
            return 200, [i for i in self.issues.values() if i["state"] == query["state"][0]
                         and label in [x["name"] for x in i["labels"]]]
        if rest == "/issues" and method == "POST":
            number = 1 + max(self.issues)
            self.issues[number] = {"number": number, "title": body["title"], "body": body["body"],
                                   "state": "open", "labels": [{"name": n} for n in body["labels"]],
                                   "user": {"login": REPO.split("/")[0]}}
            return 201, self.issues[number]
        if match := re.fullmatch(r"/issues/(\d+)/comments", rest):
            return 200, self.comments.get(int(match.group(1)), [])
        if rest == "/pulls" and method == "GET":
            branch = query["head"][0].split(":", 1)[1]
            return 200, [self.pull_view(p) for p in self.pulls if p["branch"] == branch]
        if (match := re.fullmatch(r"/pulls/(\d+)", rest)) and method == "GET":
            return 200, self.pull_view(self.pulls[int(match.group(1)) - 100])
        if rest == "/pulls" and method == "POST":
            pr = {"number": 100 + len(self.pulls), "state": "open", "merged_at": None,
                  "branch": body["head"], "sha": None, "title": body["title"], "body": body["body"]}
            self.pulls.append(pr)
            return 201, self.pull_view(pr)
        if match := re.fullmatch(r"/branches/(.+)", rest):
            sha = self.head(match.group(1))
            return (200, {"commit": {"sha": sha}}) if sha else (404, {"message": "Branch not found"})
        if match := re.fullmatch(r"/compare/(.+)\.\.\.(.+)", rest):
            rows = git("diff", "--numstat", f"{match.group(1)}...{match.group(2)}", cwd=self.bare)
            return 200, {"files": [{"filename": f, "status": "modified", "additions": int(a),
                                    "deletions": int(d)}
                                   for a, d, f in (r.split("\t") for r in rows.splitlines())]}
        if rest == "/actions/runs" and method == "GET":
            runs = [dict(run, name="pr-gatekeeper") for run in self.runs.get(query["head_sha"][0], [])]
            return 200, {"total_count": len(runs), "workflow_runs": runs}
        if match := re.fullmatch(r"/commits/([^/]+)/check-runs", rest):
            return 200, {"total_count": 1, "check_runs": self.runs.get(match.group(1), [])}
        return 404, {"message": f"no route {method} {rest}"}

    def gate(self, pr, outcome, round_, findings=()):
        sha = self.head(self.pulls[pr - 100]["branch"])
        self.runs[sha] = [{"name": "pr-gatekeeper", "status": "completed",
                           "conclusion": "success" if outcome == "MERGE" else "failure"}]
        marker = json.dumps({"round": round_, "outcome": outcome, "head_sha": sha,
                             "findings": list(findings)}).replace(">", "\\u003e")
        self.comments.setdefault(pr, []).append(
            {"body": f"### pr-gatekeeper: {outcome}\n<!-- pr-gatekeeper {marker} -->",
             "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})

    def merge(self, pr):
        view = self.pulls[pr - 100]
        view.update(state="closed", merged_at="2026-09-24T12:00:00Z", sha=self.head(view["branch"]))
        # The gate merges with its workflow token, so GitHub leaves #7 open; factory-mcp closes it.


def serve_github(github):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_any(self):
            url = urlsplit(self.path)
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            github.requests.append((self.command, url.path, self.headers.get("Authorization")))
            status, value = github.route(self.command, url.path, parse_qs(url.query), body)
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PATCH = handle_any

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


class Mcp:
    def __init__(self, url, token):
        self.url, self.token, self.id = url, token, 0

    def call(self, method, params=None):
        self.id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.id, "method": method, "params": params or {}})
        request = urllib.request.Request(self.url, body.encode(), {
            "content-type": "application/json", "accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(request, timeout=60) as response:
            reply = json.loads(response.read())
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["result"]

    def tool(self, name, **args):
        result = self.call("tools/call", {"name": name, "arguments": args})
        value = json.loads(result["content"][0]["text"])
        if result["isError"]:
            raise RuntimeError(f"{name}: {value['error']}")
        return value


def wait_http(url, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"{url} did not come up")


def main(relay_checkout, lose_store=False):
    tmp = tempfile.mkdtemp(prefix="rehearsal-")
    bare, work = os.path.join(tmp, "demo.git"), os.path.join(tmp, "worker")
    git("init", "-q", "--bare", "-b", "main", bare, cwd=tmp)
    git("clone", "-q", bare, work, cwd=tmp)
    with open(os.path.join(work, "app.py"), "w") as f:
        f.write('def greet():\n    return "hello"\n')
    with open(os.path.join(work, "test_app.py"), "w") as f:
        f.write("import unittest\nfrom app import greet\n\n\nclass T(unittest.TestCase):\n"
                "    def test_greet(self):\n        self.assertEqual(greet(), \"hello, factory\")\n")
    git("add", ".", cwd=work)
    git("commit", "-q", "-m", "init", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    with open(os.path.join(tmp, "worker.py"), "w") as f:
        f.write(WORKER)

    github = GitHub(bare)
    github.issues[7] = {"number": 7, "title": "Fix the greeting", "body": ISSUE, "state": "open",
                        "labels": [{"name": "factory:ready"}], "user": {"login": REPO.split("/")[0]}}
    github_url = serve_github(github)

    relay_port, mcp_port, token = free_port(), free_port(), "rehearsal-token"
    # The relay and factory-mcp share this token, so neither reads or writes ~/.config/agent-relay/token.
    relay_token = "rehearsal-relay-token"
    agents = os.path.join(tmp, "agents.json")
    with open(agents, "w") as f:
        json.dump({"agents": [{"id": "scripted", "command": sys.executable,
                               "args": [os.path.join(tmp, "worker.py")], "cwd": work,
                               "inheritEnv": ["GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                                              "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"]}]}, f)
    ident = {"GIT_AUTHOR_NAME": "worker", "GIT_AUTHOR_EMAIL": "worker@x",
             "GIT_COMMITTER_NAME": "worker", "GIT_COMMITTER_EMAIL": "worker@x"}
    relay = subprocess.Popen([sys.executable, os.path.join(relay_checkout, "src", "server.py")],
                             env=dict(os.environ, AGENT_RELAY_AGENTS_FILE=agents,
                                      AGENT_RELAY_PORT=str(relay_port), AGENT_RELAY_TOKEN=relay_token,
                                      **ident),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    db = os.path.join(tmp, "factory.sqlite3")

    def start_factory():
        return subprocess.Popen([sys.executable, "-m", "factory_mcp"], cwd=ROOT, env=dict(
            os.environ, FACTORY_MCP_TOKEN=token, FACTORY_REPOS=REPO, FACTORY_GITHUB_API=github_url,
            FACTORY_GITHUB_TOKEN="gh-token", FACTORY_RELAY_URL=f"http://127.0.0.1:{relay_port}",
            FACTORY_WORKERS="scripted:local", FACTORY_POLL_SECONDS="0", FACTORY_PORT=str(mcp_port),
            FACTORY_DB=db, FACTORY_GIT_URL=bare, AGENT_RELAY_TOKEN=relay_token),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    factory = start_factory()
    try:
        wait_http(f"http://127.0.0.1:{relay_port}/healthz")
        wait_http(f"http://127.0.0.1:{mcp_port}/mcp")
        mcp = Mcp(f"http://127.0.0.1:{mcp_port}/mcp", token)
        mcp.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "rehearsal", "version": "1"}})
        print("tools:", [t["name"] for t in mcp.call("tools/list")["tools"]])
        wid = f"{REPO}#7"

        def tick_until(state, limit=40):
            for _ in range(limit):
                result = mcp.tool("factory_tick", id=wid)
                if result["to"] == state:
                    print(f"  -> {state}")
                    return result["workItem"]
                time.sleep(0.25)
            status = mcp.tool("factory_status", id=wid)
            raise RuntimeError(f"stuck in {status['workItem']['state']}: "
                               f"{json.dumps(status['history'][-4:], indent=1)}")

        print("rebuild:", mcp.tool("factory_tick")["rebuilt"])
        wi = tick_until("pr_open")
        pr = wi["prNumber"]
        print(f"PR #{pr} opened; body headings:",
              re.findall(r"^## .+", github.pulls[0]["body"], re.M))
        if lose_store:
            factory.terminate()
            factory.communicate(timeout=10)
            os.remove(db)
            factory = start_factory()
            wait_http(f"http://127.0.0.1:{mcp_port}/mcp")
            print("store deleted; rebuild:", mcp.tool("factory_tick")["rebuilt"],
                  "state:", mcp.tool("factory_status", id=wid)["workItem"]["state"])
        github.gate(pr, "CHANGES", 1, [{"severity": "blocking", "path": "test_app.py", "line": 7,
                                         "problem": "No test that the greeting names the factory",
                                         "fix": None}])
        tick_until("reviewing")
        github.gate(pr, "MERGE", 2)
        tick_until("merge_pending")
        github.merge(pr)
        tick_until("closed")
        status = mcp.tool("factory_status")
        one = mcp.tool("factory_status", id=wid)
        authors = set(git("log", "--format=%an", f"main..{one['workItem']['branch']}",
                          cwd=bare).splitlines())
        print("autonomy:", status["autonomy"], "worker passes:", one["passes"],
              "branch commits by:", sorted(authors))
        auth = {a for _, _, a in github.requests}
        assert auth == {"Bearer gh-token"}, auth
        assert authors == {"factory-mcp"}, authors  # the worker hands back a patch; factory-mcp pushes
        if not lose_store:  # the history that proves "no human commit" died with the store
            assert status["autonomy"]["rate"] == 1.0, status
        print("REHEARSAL PASSED")
    finally:
        for proc in (factory, relay):
            proc.terminate()
            out = proc.communicate(timeout=10)[0]
            if out.strip():
                print(f"--- {proc.args[1]} output ---\n{out[-2000:]}")


if __name__ == "__main__":
    main(sys.argv[1], "--lose-store" in sys.argv[2:])
