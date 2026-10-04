#!/usr/bin/env python3
"""Run one factory WorkOrder in the sandbox container and print its factory.result/v1 Result.

agent-relay starts this with the agent's args and the task input as the last argument:

    runner.py [--image IMAGE] [--credential HOST_FILE:CONTAINER_DIR ...] [--env KEY=VALUE ...]
              -- <harness argv...> <task input>

Any harness can run here: the relay agent names its command, the credential files it logs in with
and the env it needs, and the runner knows none of them by name (#98).

The relay task's cwd is the persistent checkout of the WorkOrder's repo. It is never mounted into a
container: each pass gets a fresh copy of it, and only the copy is mounted. The harness runs in a
container that sees only that copy and a copy of the harness's model credential, so it holds no
GitHub credential and cannot push (ADR-0017). Host git never runs in a tree the harness could
have written, because hooks, config and filter drivers in `.git` would run on the host. Committing
the harness's work and computing the patch happen in the sandbox, and so does the repo's own gate
`verify` command, so the evidence is not the model's claim. factory-mcp pushes the patch.
"""
import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

IMAGE = "factory-worker:latest"
# Run in the sandbox after the harness: commit what it left, refuse rewritten history, print the diff.
EXTRACT = """set -e
git add -A
if ! git diff --cached --quiet; then git -c core.hooksPath=/dev/null commit -q -m "$FACTORY_GOAL"; fi
git merge-base --is-ancestor "$FACTORY_BASE" HEAD || exit 3
git diff --binary "$FACTORY_BASE" HEAD
"""
MAX_PATCH_BYTES = 2_000_000
CONTAINER_HOME = "/tmp/factory-home"


class WorkerError(Exception):
    """The WorkOrder cannot be carried out; the message becomes the Result's error."""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# A fetch starts `git maintenance run --auto`, which detaches by default and can delete a pack
# while the clone that follows copies it (#94). In the foreground, it is done before the clone.
FOREGROUND_MAINTENANCE = (("maintenance.autoDetach", "false"), ("gc.autoDetach", "false"))


def git_env():
    """The environment with FOREGROUND_MAINTENANCE added after any GIT_CONFIG_* it already has."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    base = int(env.get("GIT_CONFIG_COUNT") or 0)
    for n, (key, value) in enumerate(FOREGROUND_MAINTENANCE, base):
        env[f"GIT_CONFIG_KEY_{n}"], env[f"GIT_CONFIG_VALUE_{n}"] = key, value
    env["GIT_CONFIG_COUNT"] = str(base + len(FOREGROUND_MAINTENANCE))
    return env


def git(cwd, *args, timeout=300):
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              env=git_env())
    except subprocess.TimeoutExpired:
        raise WorkerError(f"git {args[0]} timed out after {timeout}s") from None
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


@contextmanager
def checkout_lock(cwd):
    """Passes of one repo run at once (ADR-0036) but share its persistent checkout, and two fetches
    in it fail on git's ref locks: one runner at a time fetches, copies and reads it. Yields the
    whole seconds it waited, which the pass's time must cover too."""
    common = git(cwd, "rev-parse", "--git-common-dir")
    with open(os.path.join(cwd, common, "factory-runner.lock"), "w") as handle:
        started = time.monotonic()
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("waiting for another pass of this repo to release its checkout", flush=True)
            fcntl.flock(handle, fcntl.LOCK_EX)  # released when the file closes
        yield int(time.monotonic() - started)


def prepare(cwd, order, branch, work):
    """Fetch in the persistent checkout, then make `work` a fresh copy on `branch` at the commit this
    WorkOrder builds on. Returns that commit. The copy has no remote, so a push has nowhere to go."""
    git(cwd, "fetch", "-q", "--prune", "origin")
    if order["kind"] == "fix":
        base = order.get("headSha")
        git(cwd, "fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    else:
        base = order.get("baseSha") or git(cwd, "rev-parse", "origin/HEAD")
    if not base:
        raise WorkerError("the WorkOrder names no commit to build on")
    git(cwd, "cat-file", "-e", f"{base}^{{commit}}")
    git(os.path.dirname(work), "clone", "-q", "--no-hardlinks", "--no-checkout", cwd, work)
    git(work, "fetch", "-q", cwd, "+refs/remotes/origin/*:refs/remotes/source/*")
    git(work, "remote", "remove", "origin")
    git(work, "checkout", "-q", "-f", "-B", branch, base)
    return base


def verify_command(cwd, base):
    """The gate's verify command as the base commit has it. The harness may have edited the
    working tree copy; the base copy is the one the gate itself will run."""
    try:
        config = json.loads(git(cwd, "show", f"{base}:.github/pr-gatekeeper.json"))
    except (WorkerError, ValueError):
        return None
    return config.get("verify") if isinstance(config.get("verify"), str) else None


def docker_answers():
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_docker(wait=120, poll=2):
    """Start Docker (OrbStack) when it does not answer, so the first pass after a reboot needs no
    person."""
    if docker_answers():
        return
    for cmd in (["orb", "start"], ["open", "-g", "-a", "OrbStack"]):
        try:
            if subprocess.run(cmd, capture_output=True, timeout=wait).returncode == 0:
                break
        except (OSError, subprocess.TimeoutExpired):
            continue
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if docker_answers():
            return
        time.sleep(poll)
    raise WorkerError("docker is not available: OrbStack did not start")


class Sandbox:
    """`docker run` for one WorkOrder. Killed on SIGTERM, because the relay kills this process on a
    timeout or a cancel and a container outlives the docker CLI that started it."""

    def __init__(self, image, label, credentials=(), harness_env=None):
        """credentials: (scratch dir, container dir) pairs; they and harness_env reach the harness only."""
        self.image, self.label = image, label
        self.credentials, self.harness_env = list(credentials), dict(harness_env or {})
        self.names = []
        signal.signal(signal.SIGTERM, self._stop)

    def _stop(self, *_):
        for name in self.names:
            subprocess.Popen(["docker", "kill", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sys.exit(143)

    def run(self, argv, seconds, mount, harness=False, env=None):
        """Returns (exit code, stdout, stderr). Only `mount` (and, for the harness, its credentials)
        is visible."""
        name = f"factory-worker-{uuid.uuid4().hex[:12]}"
        self.names.append(name)
        cmd = ["docker", "run", "--rm", "--init", "--name", name, "--label", f"factory-worker={self.label}",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "1024",
               "--memory", "6g", "--cpus", "4", "-v", f"{mount}:/work", "-w", "/work"]
        # All phases modify or inspect the host-owned work copy. The scratch home is mode 1777
        # in the image, so this UID can also create tool state without using the host's HOME.
        cmd += ["--user", f"{os.getuid()}:{os.getgid()}", "-e", f"HOME={CONTAINER_HOME}"]
        if harness:
            # Each 0600 credential copy is in a 0700 scratch dir owned by the host user, which the
            # container runs as, so the harness can read it and write its own state next to it.
            for scratch, target in self.credentials:
                cmd += ["-v", f"{scratch}:{target}"]
            env = dict(self.harness_env, **(env or {}))
        for key, value in (env or {}).items():
            cmd += ["-e", f"{key}={value}"]
        # `timeout` inside the container ends it on its own if this process is killed first.
        cmd += [self.image, "timeout", "--kill-after=10", str(int(seconds)), *argv]
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 60)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True)
            return 124, "", "timed out"
        except OSError as exc:
            raise WorkerError(f"docker is not available: {exc}") from None
        if done.returncode == 125:
            raise WorkerError(f"docker could not start the sandbox: {done.stderr.strip()[-300:]}")
        return done.returncode, done.stdout, done.stderr


def collect(sandbox, work, base, goal):
    """The harness's work as one unified diff against `base`, computed in the sandbox. Uncommitted
    changes are committed first, so a harness that forgot to commit still hands its work over."""
    code, diff, err = sandbox.run(["sh", "-c", EXTRACT], 300, work,
                                  env={"FACTORY_BASE": base, "FACTORY_GOAL": goal})
    if code == 3:
        raise WorkerError("the harness rewrote history below the commit it was given")
    if code != 0:
        raise WorkerError(f"collecting the work failed ({code}): {' '.join(err.split())[-300:]}")
    if not diff.strip():
        raise WorkerError("the harness made no change")
    if len(diff.encode()) > MAX_PATCH_BYTES:
        raise WorkerError(f"the patch is over {MAX_PATCH_BYTES} bytes")
    return diff if diff.endswith("\n") else diff + "\n"


def credential_copies(specs, scratch):
    """Copy each HOST_FILE of `HOST_FILE:CONTAINER_DIR` into its own 0700 dir under `scratch`, as
    0600. Returns (scratch dir, container dir) pairs. A missing file fails before any container."""
    copies = []
    for n, spec in enumerate(specs):
        host, sep, target = spec.rpartition(":")
        if not sep or not host or not target.startswith("/"):
            raise WorkerError(f"--credential {spec!r} is not HOST_FILE:/container/dir")
        host = os.path.expanduser(host)
        if not os.path.isfile(host):
            raise WorkerError(f"no model credential at {host}")
        directory = os.path.join(scratch, f"credential-{n}")
        os.mkdir(directory, 0o700)
        copy = os.path.join(directory, os.path.basename(host))
        shutil.copyfile(host, copy)
        os.chmod(copy, 0o600)
        copies.append((directory, target))
    return copies


def env_pairs(specs):
    pairs = {}
    for spec in specs:
        key, sep, value = spec.partition("=")
        if not sep or not key:
            raise WorkerError(f"--env {spec!r} is not KEY=VALUE")
        pairs[key] = value
    return pairs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--credential", action="append", default=[])
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    rest = args.rest[1:] if args.rest[:1] == ["--"] else args.rest
    if len(rest) < 2:
        parser.error("expected <harness argv...> <task input>")
    harness, text = rest[:-1], rest[-1]
    cwd = os.getcwd()
    result = {"schemaVersion": "factory.result/v1", "workOrderId": None, "status": "failed",
              "evidence": [], "startedAt": now()}
    scratch = tempfile.mkdtemp(prefix="factory-run-")
    work = os.path.join(scratch, "work")
    try:
        order = workorder_from(text)
        result["workOrderId"] = order["id"]
        repo = origin_repo(cwd)
        if repo != order["repo"]:
            raise WorkerError(f"the checkout {cwd} is {repo}, not {order['repo']}")
        branch = "factory/" + order["workItemId"].rsplit("#", 1)[1]
        with checkout_lock(cwd) as waited:
            base = prepare(cwd, order, branch, work)
            verify = verify_command(cwd, base)
        if not verify and not order.get("remoteVerify"):
            raise WorkerError("the base commit has no .github/pr-gatekeeper.json verify command")
        credentials = credential_copies(args.credential, scratch)
        harness_env = env_pairs(args.env)
        seconds = max(120, order["budget"]["wallMs"] // 1000 - 60 - waited)
        ensure_docker()
        sandbox = Sandbox(args.image, re.sub(r"[^A-Za-z0-9_.-]", "_", order["id"]), credentials, harness_env)
        code, out, err = sandbox.run([*harness, text], seconds * 0.7, work, harness=True)
        print((out + err)[-4000:], flush=True)  # for the relay log; the Result line comes last
        if code == 124:
            raise WorkerError("the harness timed out")
        if code != 0:
            detail = " ".join((err or out).split())[-300:]
            raise WorkerError(f"the harness exited {code}" + (f": {detail}" if detail else ""))
        diff = collect(sandbox, work, base, order["goal"])
        if not order.get("remoteVerify"):  # else a GitHub runner runs the verify (ADR-0045)
            code, out, err = sandbox.run(["sh", "-c", verify], seconds * 0.25, work)
            output = out + err
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
        shutil.rmtree(scratch, ignore_errors=True)
    result["finishedAt"] = now()
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
