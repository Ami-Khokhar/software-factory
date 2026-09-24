#!/usr/bin/env python3
"""Run one factory WorkOrder in the sandbox container and print its factory.result/v1 Result.

agent-relay starts this with the agent's args and the task input as the last argument:

    runner.py [--image IMAGE] -- <harness argv...> <task input>

The relay task's cwd is a checkout of the WorkOrder's repo. The harness runs in a container that
sees only that checkout and a copy of the harness's model credential, so it holds no GitHub
credential and cannot push (D6, ADR-0007). This script is the trusted half: it prepares the
checkout, turns the harness's local commits into a patch, and runs the repo's own gate `verify`
command in the same sandbox, so the evidence is not the model's claim. factory-mcp pushes the patch.
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone

IMAGE = "factory-worker:latest"
MAX_PATCH_BYTES = 2_000_000
PI_AUTH = os.path.expanduser("~/.pi/agent/auth.json")
CONTAINER_AGENT_DIR = "/home/node/.pi/agent"


class WorkerError(Exception):
    """The WorkOrder cannot be carried out; the message becomes the Result's error."""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(cwd, *args, timeout=300):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                          env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    if done.returncode != 0:
        raise WorkerError(f"git {args[0]} failed: {(done.stderr or done.stdout).strip()[-300:]}")
    return done.stdout.strip()


def workorder_from(text):
    marker = "WorkOrder:\n"
    at = text.rfind(marker)
    if at < 0:
        raise WorkerError("the task input has no WorkOrder")
    try:
        return json.loads(text[at + len(marker):])
    except ValueError as exc:
        raise WorkerError(f"the WorkOrder is not JSON: {exc}") from None


def origin_repo(cwd):
    """owner/repo of the checkout's origin, from an https or ssh remote URL (as configured, before
    any `insteadOf` rewrite)."""
    url = git(cwd, "config", "--get", "remote.origin.url")
    match = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return match.group(1) if match else url


def prepare(cwd, order, branch):
    """Put the checkout on `branch` at the commit this WorkOrder builds on. Returns that commit."""
    git(cwd, "fetch", "-q", "--prune", "origin")
    if order["kind"] == "fix":
        base = order.get("headSha")
        git(cwd, "fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    else:
        base = order.get("baseSha") or git(cwd, "rev-parse", "origin/HEAD")
    if not base:
        raise WorkerError("the WorkOrder names no commit to build on")
    git(cwd, "cat-file", "-e", f"{base}^{{commit}}")
    git(cwd, "checkout", "-q", "-f", "-B", branch, base)
    git(cwd, "clean", "-q", "-ffdx")
    return base


def verify_command(cwd, base):
    """The gate's verify command as the base commit has it. The harness may have edited the
    working tree copy; the base copy is the one the gate itself will run."""
    try:
        config = json.loads(git(cwd, "show", f"{base}:.github/pr-gatekeeper.json"))
    except (WorkerError, ValueError):
        return None
    return config.get("verify") if isinstance(config.get("verify"), str) else None


class Sandbox:
    """`docker run` for one WorkOrder. Killed on SIGTERM, because the relay kills this process on a
    timeout or a cancel and a container outlives the docker CLI that started it."""

    def __init__(self, image, cwd, agent_dir, label):
        self.image, self.cwd, self.agent_dir, self.label = image, cwd, agent_dir, label
        self.names = []
        signal.signal(signal.SIGTERM, self._stop)

    def _stop(self, *_):
        for name in self.names:
            subprocess.Popen(["docker", "kill", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sys.exit(143)

    def run(self, argv, seconds, with_agent_dir):
        name = f"factory-worker-{uuid.uuid4().hex[:12]}"
        self.names.append(name)
        cmd = ["docker", "run", "--rm", "--init", "--name", name, "--label", f"factory-worker={self.label}",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "1024",
               "--memory", "6g", "--cpus", "4", "-v", f"{self.cwd}:/work", "-w", "/work"]
        if with_agent_dir:
            cmd += ["-v", f"{self.agent_dir}:{CONTAINER_AGENT_DIR}", "-e", f"PI_CODING_AGENT_DIR={CONTAINER_AGENT_DIR}"]
        # `timeout` inside the container ends it on its own if this process is killed first.
        cmd += [self.image, "timeout", "--kill-after=10", str(int(seconds)), *argv]
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 60)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True)
            return 124, "timed out"
        except OSError as exc:
            raise WorkerError(f"docker is not available: {exc}") from None
        if done.returncode == 125:
            raise WorkerError(f"docker could not start the sandbox: {done.stderr.strip()[-300:]}")
        return done.returncode, (done.stdout + done.stderr)


def collect(cwd, base, goal):
    """The harness's work as one unified diff against `base`. Uncommitted changes are committed
    first, so a harness that forgot to commit still hands its work over."""
    if git(cwd, "status", "--porcelain"):
        git(cwd, "add", "-A")
        git(cwd, "-c", "user.name=factory-worker", "-c", "user.email=factory-worker@users.noreply.github.com",
            "commit", "-q", "-m", goal)
    head = git(cwd, "rev-parse", "HEAD")
    if head == base:
        raise WorkerError("the harness made no change")
    try:
        git(cwd, "merge-base", "--is-ancestor", base, head)
    except WorkerError:
        raise WorkerError("the harness rewrote history below the commit it was given") from None
    diff = git(cwd, "diff", "--binary", base, head) + "\n"
    if len(diff.encode()) > MAX_PATCH_BYTES:
        raise WorkerError(f"the patch is over {MAX_PATCH_BYTES} bytes")
    return diff


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    rest = args.rest[1:] if args.rest[:1] == ["--"] else args.rest
    if len(rest) < 2:
        parser.error("expected <harness argv...> <task input>")
    harness, text = rest[:-1], rest[-1]
    cwd = os.getcwd()
    result = {"schemaVersion": "factory.result/v1", "workOrderId": None, "status": "failed",
              "evidence": [], "startedAt": now()}
    agent_dir = tempfile.mkdtemp(prefix="factory-agent-")
    try:
        order = workorder_from(text)
        result["workOrderId"] = order["id"]
        repo = origin_repo(cwd)
        if repo != order["repo"]:
            raise WorkerError(f"the checkout {cwd} is {repo}, not {order['repo']}")
        branch = "factory/" + order["workItemId"].rsplit("#", 1)[1]
        base = prepare(cwd, order, branch)
        verify = verify_command(cwd, base)
        if not verify:
            raise WorkerError("the base commit has no .github/pr-gatekeeper.json verify command")
        if not os.path.isfile(PI_AUTH):
            raise WorkerError(f"no model credential at {PI_AUTH}")
        shutil.copyfile(PI_AUTH, os.path.join(agent_dir, "auth.json"))
        seconds = max(120, order["budget"]["wallMs"] // 1000 - 60)
        sandbox = Sandbox(args.image, cwd, agent_dir, re.sub(r"[^A-Za-z0-9_.-]", "_", order["id"]))
        code, output = sandbox.run([*harness, text], seconds * 0.75, with_agent_dir=True)
        print(output[-4000:], flush=True)  # for the relay log; the Result line comes last
        if code == 124:
            raise WorkerError("the harness timed out")
        diff = collect(cwd, base, order["goal"])
        code, output = sandbox.run(["sh", "-c", verify], seconds * 0.25, with_agent_dir=False)
        tail = output[-2000:].replace("```", "")
        result["evidence"] = [{"kind": "test", "command": verify, "exit": code, "tail": tail}]
        if code != 0:
            raise WorkerError(f"verify exited {code}: {' '.join(output[-400:].split())}")
        result.update(status="completed", patch={"format": "unified", "diff": diff, "baseSha": base})
    except WorkerError as exc:
        result["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - any failure must still end in a Result line
        result["error"] = f"runner failed: {type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(agent_dir, ignore_errors=True)
    result["finishedAt"] = now()
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
