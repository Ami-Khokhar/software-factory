#!/usr/bin/env python3
"""Run one factory WorkOrder in the sandbox container and print its factory.result/v1 Result.

agent-relay starts this with the agent's args and the task input as the last argument:

    runner.py [--image IMAGE] [--credential HOST_FILE:CONTAINER_DIR ...] [--env KEY=VALUE ...]
              -- <harness argv...> <task input>

Any harness can run here: the relay agent names its command, the credential files it logs in with
and the env it needs, and the runner knows none of them by name (#98). The one exception: it
inserts `--auto` into an `opencode run` argv itself, so an OpenCode pass auto-approves inside the
sandbox like the Pi workers, which have no permission prompt (ADR-0111).

The relay task's cwd is the persistent checkout of the WorkOrder's repo. It is never mounted into a
container: each pass gets a fresh copy of it, and only the copy is mounted. The harness runs in a
container that sees only that copy and a copy of the harness's model credential, so it holds no
GitHub credential and cannot push (ADR-0017). Host git never runs in a tree the harness could
have written, because hooks, config and filter drivers in `.git` would run on the host. Committing
the harness's work and computing the patch happen in the sandbox, and so does the repo's own gate
`verify` command, so the evidence is not the model's claim. factory-mcp pushes the patch.
"""
import argparse
import errno
import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

IMAGE = "factory-worker:latest"
# Run in the sandbox after the harness: commit what it left, refuse rewritten history, print the
# diff, and put the whole pull request's size on stderr (it counts every commit on the branch,
# measured against the default branch, so a fix adds its lines to the attempt it follows).
EXTRACT = """set -e
git add -A
if ! git diff --cached --quiet; then git -c core.hooksPath=/dev/null commit -q -m "$FACTORY_GOAL"; fi
git merge-base --is-ancestor "$FACTORY_BASE" HEAD || exit 3
if [ -n "$FACTORY_PARENT" ]; then
  git merge-base --is-ancestor "$FACTORY_PARENT" HEAD || exit 3
fi
git diff --binary "$FACTORY_BASE" HEAD
echo "FACTORY_PR_SIZE: $(git diff --shortstat "$(git merge-base "$FACTORY_DEFAULT" HEAD)" HEAD)" >&2
git diff --numstat "$(git merge-base "$FACTORY_DEFAULT" HEAD)" HEAD | sed 's/^/FACTORY_PR_NUMSTAT: /' >&2
"""
MAX_PATCH_BYTES = 2_000_000
CONTAINER_HOME = "/tmp/factory-home"
# Preflight exit codes: one distinct nonzero code per unavailable part of the environment. Any
# nonzero code on a setup phase is the environment to the pause consumer (ADR-0051), so each one
# is evidence of "checkout or setup", never of the work.
NO_DOCKER, NO_IMAGE, NO_HARNESS, NO_CREDENTIAL, NO_VERIFY = 69, 72, 71, 66, 78
# What the setup phase's tail says, without a host path, a credential or the harness's payload.
PREFLIGHT_TAILS = {NO_DOCKER: "preflight: the Docker daemon is not available",
                   NO_IMAGE: "preflight: the configured worker image is not in the local store",
                   NO_HARNESS: "preflight: the configured harness is not in the worker image",
                   NO_CREDENTIAL: "preflight: a configured credential-copy is missing or unreadable",
                   NO_VERIFY: "preflight: verify command missing from the base commit"}
DIAGNOSTIC_TAIL = 4000  # the Result schema's cap for a diagnostic tail
# A failing verification prints the deployment preamble first and the failing tests lower; the
# runner keeps a bounded digest of the FAIL/ERROR identities instead of that preamble, so a retry
# can still see what actually failed after an intervening timeout (#271).
_UNITTEST_FAILURE = re.compile(r"^(?:FAIL|ERROR):\s+\S")
_VERIFY_BLOCK = re.compile(r"^={5,}\s*$")
_VERIFY_RULE = re.compile(r"^[=-]{5,}\s*$")
MAX_SUMMARY_FAILURES = 12
SUMMARY_ENTRY = 200
# The one command the pass runs after the harness. It is not a phase that grows with the pass: the
# eight completed Results in the live relay's memory took 6 to 69 seconds. A quarter of a long wall
# was therefore time the harness lost — at FACTORY_WALL_MS=1800000 twelve of twenty-one MiMo harness
# phases were killed at the old 0.7 cap, ten of them with partial work (#329). So the reserve is
# flat, and a quarter only bounds it on a short pass. A remote verify runs on a GitHub runner
# (ADR-0045) and reserves nothing.
VERIFY_RESERVE_SECONDS = 180
# Collecting the patch, printing the Result and the relay's teardown run after the verify, and the
# old 0.7/0.25 split left 5% of the pass unused for them. A flat verify reserve has to keep that
# float: a collect slow enough to outlast it lets the relay kill the runner, losing the patch the
# salvage path exists to keep.
POST_HARNESS_SECONDS = 60


class WorkerError(Exception):
    """The WorkOrder cannot be carried out; the message becomes the Result's error. When the
    failure is a command's own exit code, that code rides along for the phase diagnostics."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


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
        raise WorkerError(f"git {args[0]} failed: {(done.stderr or done.stdout).strip()[-300:]}",
                          done.returncode)
    return done.stdout.strip()


# documents.ASK_CHANNEL in factory_mcp/documents.py. The runner runs in the image without that
# package, so the literal is repeated; tests/test_worker_runner.py holds the two equal.
ASK_CHANNEL = "\nASK-CHANNEL (the runner reads this block and does not pass it to the harness):\n"


def ask_channel_from(text):
    """Split the task input into the prompt the harness sees and the pass's ask block (ADR-0098).
    The block holds the token, so the harness gets it by environment only."""
    at = text.rfind(ASK_CHANNEL)
    if at < 0:
        return text, {}
    start = at + len(ASK_CHANNEL)
    end = text.find("\n", start)
    if end < 0:
        end = len(text)
    try:
        ask = json.loads(text[start:end])
    except ValueError as exc:
        raise WorkerError(f"the ask block is not JSON: {exc}") from None
    return text[:at] + text[end:], ask if isinstance(ask, dict) else {}


# The health check's launch step (#430) sends this instead of a WorkOrder: the relay has started
# this interpreter in the checkout folder, so one reply proves the launch path. It runs no harness
# and copies no checkout (ADR-0139).
LAUNCH_INPUT = "factory-launch-probe"
# The launch reply is its own document, not a Result: it carries no WorkOrder and no patch, so a
# factory.result/v1 label would break that contract (schemas/result.schema.json). tests/
# test_worker_runner.py holds this literal equal to factory_mcp.worker_ops.LAUNCH_SCHEMA.
LAUNCH_SCHEMA = "factory.launch/v1"


def workorder_from(text):
    marker = "WorkOrder:\n"
    at = text.rfind(marker)
    if at < 0:
        raise WorkerError("the task input has no WorkOrder")
    try:
        return json.loads(text[at + len(marker):])
    except ValueError as exc:
        raise WorkerError(f"the WorkOrder is not JSON: {exc}") from None


# The shapes the controller's sanitize also looks for (factory_mcp/diagnostics.py): the worker
# redacts before serializing the Result, and the controller checks behind it (#192).
_ENV_VALUE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})=\S+")
_CREDENTIAL = re.compile(
    r"(?i)\b(access|auth)?[-_ ]?(token|password|passwd|passphrase|secret|bearer|authorization|"
    r"credential|api[-_ ]?key|private[-_ ]?key)s?(\s*[=:]\s*|\s+)\S+")
_SECRET_KEY = re.compile(r"(?i)token|secret|password|passwd|passphrase|bearer|authorization|credential|key")
_SECRET_MIN_LENGTH = 8  # a shorter value is likely metadata ("api", "us-east-1"), not a secret
_TOKEN = re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9]{6,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"
                    r"|\bAKIA[0-9A-Z]{16}\b|\bBearer\s+\S+")
_WORKORDER_PAYLOAD = re.compile(r"(?s)\{.*?factory\.workorder/v1.*?\}")


def redact(text, secrets, limit=DIAGNOSTIC_TAIL):
    """Bounded output for a Result: whitespace collapsed; copied credentials and configured secret
    values matched whole; environment values, credential-shaped text, known tokens and the
    WorkOrder's command payload replaced."""
    if limit <= 0:
        return ""
    line = " ".join((text or "").split())
    for secret in secrets:
        if secret:
            line = line.replace(secret, "<redacted>")
    line = _WORKORDER_PAYLOAD.sub(" ", line)
    line = _ENV_VALUE.sub(r"\1=<redacted>", line)
    line = _CREDENTIAL.sub(r"\g<2>=<redacted>", line)
    return _TOKEN.sub("<redacted>", line)[-limit:]


def credential_secrets(text):
    """Return a copied credential's full contents and JSON string values for output redaction."""
    secrets = set()
    value = text.strip()
    if value:
        secrets.update((value, " ".join(value.split())))
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return secrets

    def collect(item, key=""):
        if isinstance(item, dict):
            for name, child in item.items():
                collect(child, name)
        elif isinstance(item, list):
            for child in item:
                collect(child, key)
        elif isinstance(item, str) and item.strip():
            # A credential-shaped key or a value too long to be metadata (`type`, `region`)
            # is worth redacting; a short metadata value would mangle the tail instead.
            if _SECRET_KEY.search(key) or len(item.strip()) >= _SECRET_MIN_LENGTH:
                secrets.update((item.strip(), " ".join(item.split())))

    collect(parsed)
    return secrets


def verification_summary(output, limit=DIAGNOSTIC_TAIL):
    """A bounded digest of a failing verification run: one line per FAIL/ERROR identity in output
    order, each with its last traceback line, and an explicit marker when identities are dropped.
    No identity is found -> the end of the whitespace-collapsed output, where a pytest or linter
    summary sits, so an unknown runner's own failing tail still rides along. The caller redacts the
    digest against the pass's secrets before it serializes."""
    blocks, current = [], []
    for line in (output or "").splitlines():
        if _VERIFY_BLOCK.match(line.strip()):
            blocks.append(current)
            current = []
        else:
            current.append(line)
    blocks.append(current)
    digest = []
    for block in blocks:
        header = next((line.strip() for line in block if _UNITTEST_FAILURE.match(line.strip())), None)
        if not header:
            continue
        body = [line.strip() for line in block if line.strip()
                and not _VERIFY_RULE.match(line.strip())
                and not _UNITTEST_FAILURE.match(line.strip())]
        entry = f"{header} {body[-1]}".strip() if body else header
        if len(entry) > SUMMARY_ENTRY:
            entry = entry[:SUMMARY_ENTRY - len("...<truncated>")] + "...<truncated>"
        digest.append(entry)
    if not digest:
        return " ".join((output or "").split())[-limit:]
    omitted = len(digest) - MAX_SUMMARY_FAILURES
    shown = digest[:MAX_SUMMARY_FAILURES]
    if omitted > 0:
        shown.append(f"<truncated: {omitted} more failures/errors omitted>")
    return " | ".join(shown)[:limit]


# A harness that dies on its own (a terminal provider error, an auth failure) says so only in its
# own log in the sandbox home, never on stdout or stderr, so the pass would end as a bare exit
# code (#560). opencode writes it under its state directory, pi in its newest session file under
# its agent dir, and those directories are exactly the pass's credential mounts: the log survives
# the throwaway container on the host side of the mount, where the runner reads it.
HARNESS_ERROR_NOTE = 320  # a bounded note, so a tail it joins stays under the Result's cap
_ERROR_LINE = re.compile(r"(?i)error")


def tail_bytes(path, limit=DIAGNOSTIC_TAIL):
    """The last `limit` bytes of a regular file, leniently decoded; "" when it is missing,
    unreadable, or not a plain regular file. The untrusted harness may plant anything at its log
    path: O_NOFOLLOW refuses a symlink instead of following it out of the sandbox, O_NONBLOCK
    keeps a FIFO's open() from holding the pass forever, and fstat re-checks what was opened."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return ""
    with os.fdopen(fd, "rb") as handle:
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return ""
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - limit))
            return handle.read().decode("utf-8", "replace")
        except OSError:
            return ""


def inside_copy(path, copy):
    """Whether a file the harness controls stays inside the credential copy `copy` once symlinks
    resolve: a log or session linked out of the sandbox must not point this host read at a host
    file, which the sandbox exists to prevent."""
    try:
        root = os.path.realpath(copy)
        return os.path.commonpath((os.path.realpath(path), root)) == root
    except (OSError, ValueError):
        return False


def last_error_line(log):
    """The last line of a harness log that names an error, whitespace-collapsed and bounded."""
    lines = [line for line in (log or "").splitlines() if _ERROR_LINE.search(line)]
    return " ".join(lines[-1].split())[-HARNESS_ERROR_NOTE:] if lines else ""


def newest_session(directory, copy):
    """The most recently modified .jsonl session file under a pi agent dir that is a plain file
    inside the copy; None when it is missing or holds none. A session symlink is skipped, not
    followed."""
    newest = None
    for root, _, names in os.walk(directory or ""):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(root, name)
            try:
                status = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(status.st_mode) or not inside_copy(path, copy):
                continue
            if newest is None or status.st_mtime > newest[0]:
                newest = (status.st_mtime, path)
    return newest[1] if newest else None


def harness_error_log(harness, harness_env, credentials):
    """The pinned harness's own log in the sandbox home, read from the host side of the credential
    mount that backs its state directory; "" when this harness has no known log or no mount backs
    it. A missing or unreadable log is not an error, and a log the harness replaced with a symlink
    or a FIFO reads as unreadable: the pass keeps today's text."""
    name = os.path.basename(harness[0]) if harness else ""
    if name == "opencode":
        state, relative = f"{CONTAINER_HOME}/.local/share/opencode", ("log", "opencode.log")
    elif name == "pi":
        state = harness_env.get("PI_CODING_AGENT_DIR") or f"{CONTAINER_HOME}/.pi/agent"
        relative = ("sessions",)
    else:
        return ""
    copy = next((scratch for scratch, target in credentials if target == state), None)
    if copy is None:
        return ""
    if name == "opencode":
        path = os.path.join(copy, *relative)
    else:
        path = newest_session(os.path.join(copy, *relative), copy)
    return tail_bytes(path) if path and inside_copy(path, copy) else ""


def with_note(tail, note):
    """A diagnostic tail with the harness's own error line after it, still within the Result's cap:
    the note stays whole and the pass's own tail keeps its end."""
    if not tail:
        return note
    room = DIAGNOSTIC_TAIL - len(note) - 1
    if room <= 0:
        return note[-DIAGNOSTIC_TAIL:]
    return f"{note} {tail[-room:]}"


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
        base = order.get("continuationSha") or order.get("headSha")
        git(cwd, "fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
        if order.get("continuationSha"):
            remote_head = git(cwd, "rev-parse", f"refs/remotes/origin/{branch}")
            if remote_head != order.get("headSha"):
                raise WorkerError("the reviewed PR head moved after the checkpoint was checked")
    else:
        base = order.get("continuationSha") or order.get("baseSha") or git(cwd, "rev-parse", "origin/HEAD")
    if not base:
        raise WorkerError("the WorkOrder names no commit to build on")
    git(cwd, "cat-file", "-e", f"{base}^{{commit}}")
    # The default branch's head, where the whole pull request's size is read from (ADR-0040).
    default = git(cwd, "rev-parse", "origin/HEAD")
    git(cwd, "cat-file", "-e", f"{default}^{{commit}}")
    if order.get("continuationSha"):
        checkpoint = order["workItemId"].rsplit("#", 1)[-1]
        scratch = git(cwd, "rev-parse", f"refs/remotes/origin/factory-wip/{checkpoint}")
        if scratch != order["continuationSha"]:
            raise WorkerError("the checkpoint branch moved after it was checked")
        if default != order.get("baseSha"):
            raise WorkerError("the default branch base moved after the checkpoint was checked")
        # A completed implement continuation collects the whole work against the original baseSha
        # (#451), so the checkpoint must still build on that base or the collected patch would not
        # apply on it, and the WorkOrder must name the base at all.
        if order["kind"] == "implement":
            if not order.get("baseSha"):
                raise WorkerError("an implement continuation names no original base")
            try:
                git(cwd, "merge-base", "--is-ancestor", order["baseSha"], order["continuationSha"])
            except WorkerError:
                raise WorkerError("the checkpoint does not build on the WorkOrder's base") from None
    git(os.path.dirname(work), "clone", "-q", "--no-hardlinks", "--no-checkout", cwd, work)
    git(work, "fetch", "-q", cwd, "+refs/remotes/origin/*:refs/remotes/source/*")
    git(work, "remote", "remove", "origin")
    git(work, "checkout", "-q", "-f", "-B", branch, base)
    return base, default


def verify_command(cwd, base):
    """The gate's verify command as the base commit has it. The harness may have edited the
    working tree copy; the base copy is the one the gate itself will run."""
    try:
        config = json.loads(git(cwd, "show", f"{base}:.github/pr-gatekeeper.json"))
    except (WorkerError, ValueError):
        return None
    return config.get("verify") if isinstance(config.get("verify"), str) else None


def docker_status():
    """The exit code of `docker info`, or None when it could not run at all."""
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode
    except (OSError, subprocess.TimeoutExpired):
        return None


def ensure_docker(wait=120, poll=2, start=True):
    """Start Docker (OrbStack) when it does not answer, so the first pass after a reboot needs no
    person. Health-only probes pass `start=False` to avoid changing host service state."""
    if docker_status() == 0:
        return
    if not start:
        raise WorkerError("docker is not available", NO_DOCKER)
    for cmd in (["orb", "start"], ["open", "-g", "-a", "OrbStack"]):
        try:
            if subprocess.run(cmd, capture_output=True, timeout=wait).returncode == 0:
                break
        except (OSError, subprocess.TimeoutExpired):
            continue
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if docker_status() == 0:
            return
        time.sleep(poll)
    raise WorkerError("docker is not available: OrbStack did not start", NO_DOCKER)


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
        except subprocess.TimeoutExpired as exc:
            subprocess.run(["docker", "kill", name], capture_output=True)
            # Whatever the sandbox printed before the kill rides on the exception (#192);
            # communicate hands back bytes even in text mode.
            out = textify(exc.stdout)
            return 124, out, textify(exc.stderr) or ("" if out else "timed out")
        except OSError as exc:
            raise WorkerError(f"docker is not available: {exc}") from None
        if done.returncode == 125:
            raise WorkerError(f"docker could not start the sandbox: {done.stderr.strip()[-300:]}")
        return done.returncode, done.stdout, done.stderr


def preflight(order, image, harness_argv, credential_specs, env_specs, scratch, work, verify,
              start_docker=True):
    """Checks the environment before the coding harness runs: the Docker daemon, the configured
    worker image, the harness inside it, the configured credential files and the base commit's
    verify command. Each failure raises WorkerError with its preflight code (ADR-0051), so the
    failed pass pauses as the environment instead of spending the budget on a broken one.
    Returns the credentials' 0700 copies and the harness env.
    """
    if start_docker:
        ensure_docker()
    else:
        ensure_docker(start=False)
    try:
        done = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise WorkerError("docker is not available", NO_DOCKER) from None
    if done.returncode != 0:
        raise WorkerError(f"the configured worker image {image} is not in the local Docker store",
                          NO_IMAGE)
    credentials = credential_copies(credential_specs, scratch)
    harness_env = env_pairs(env_specs)
    if not verify and not order.get("remoteVerify"):
        raise WorkerError("the base commit has no .github/pr-gatekeeper.json verify command",
                          NO_VERIFY)
    probe = Sandbox(image, re.sub(r"[^A-Za-z0-9_.-]", "_", order["id"]))
    code, _, _ = probe.run(["sh", "-c", 'command -v -- "$1"', "sh", harness_argv[0]], 60, work)
    if code != 0:
        raise WorkerError(f"the configured harness {harness_argv[0]!r} is not in the worker image",
                          NO_HARNESS)
    return credentials, harness_env


PR_SIZE = re.compile(r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?")
SIZE_CEILING_MARKER = "source-size-ceiling"  # matches factory_mcp/sizing.py
SOURCE_CEILING = 1000  # matches factory_mcp/sizing.py SOURCE_CEILING
_DOC_SUFFIXES = (".md", ".rst", ".adoc", ".mdx")


def change_kind(path):
    """`source`, `tests` or `docs` for a repository path (factory_mcp/sizing.py)."""
    parts = (path or "").split("/")
    name = parts[-1] if parts else ""
    lowered = [part.lower() for part in parts[:-1]]
    if parts and parts[0].lower() == "docs" or name.lower().endswith(_DOC_SUFFIXES):
        return "docs"
    if (lowered and any(part in ("tests", "test") for part in lowered)
            or name.lower().startswith("test_") or name.lower().endswith("_test.py")):
        return "tests"
    return "source"


def change_totals(files):
    """Source/tests/docs/total lines for ``(path, added, deleted)`` records."""
    totals = {"source": 0, "tests": 0, "docs": 0}
    for path, added, deleted in files:
        totals[change_kind(path)] += (added or 0) + (deleted or 0)
    totals["total"] = totals["source"] + totals["tests"] + totals["docs"]
    totals["files"] = len(files)
    return totals


def numstat_totals(err):
    """The per-file source/tests/docs totals the extract printed on stderr."""
    files = []
    for line in (err or "").splitlines():
        marker = "FACTORY_PR_NUMSTAT: "
        if not line.startswith(marker):
            continue
        parts = line[len(marker):].split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        try:
            files.append((path, int(added), int(deleted)))
        except ValueError:
            files.append((path, 0, 0))  # a binary change: zero lines, still a file
    return change_totals(files)


def collect(sandbox, work, base, default, goal, limits, parent=None):
    """The harness's work as one unified diff against `base`, computed in the sandbox. Uncommitted
    changes are committed first, so a harness that forgot to commit still hands its work over.
    Refuses the work when the whole pull request — every commit on the branch against the default
    branch's merge base, so a fix is measured with the attempt it follows (ADR-0040). The size
    counts source additions and deletions only; tests and docs are reported separately (ADR-0079).
    Over the source target warns, over the source ceiling refuses as a size-only excess the Foreman
    routes to decomposition, and over the file count refuses: that stands in for an expensive verify,
    and factory-mcp's own prepush check stays for a base that advanced under the pass. `parent`
    names a second commit that must also be an ancestor of HEAD — for a completed implement
    continuation, the launch checkpoint, so a harness that rewrote history below it cannot hand
    back a patch that silently drops the retained work."""
    env = {"FACTORY_BASE": base, "FACTORY_DEFAULT": default, "FACTORY_GOAL": goal}
    if parent is not None and parent != base:
        env["FACTORY_PARENT"] = parent
    code, diff, err = sandbox.run(["sh", "-c", EXTRACT], 300, work, env=env)
    if code == 3:
        raise WorkerError("the harness rewrote history below the commit it was given", 3)
    if code != 0:
        raise WorkerError(f"collecting the work failed ({code}): {' '.join(err.split())[-300:]}", code)
    if not diff.strip():
        raise WorkerError("the harness made no change")
    if len(diff.encode()) > MAX_PATCH_BYTES:
        raise WorkerError(f"the patch is over {MAX_PATCH_BYTES} bytes")
    stat = PR_SIZE.search(err)
    files, added, deleted = (int(n or 0) for n in (stat.groups() if stat else (None, None, None)))
    lines = added + deleted
    max_lines, max_files = limits["maxChangedLines"], limits["maxChangedFiles"]
    # The source metric (ADR-0079) counts source additions and deletions, tests and docs separately.
    # An old WorkOrder without maxSourceChangedLines keeps the all-lines target.
    source_target = limits.get("maxSourceChangedLines")
    if source_target is not None:
        totals = numstat_totals(err)
        source = totals["source"]
        # The ceiling defaults to the reviewable one, not the advisory target, so a WorkOrder that
        # names only the target does not turn a target+1 into a size-only recovery (ADR-0079).
        ceiling = limits.get("maxSourceChangedLinesCeiling", SOURCE_CEILING)
        advisory = limits.get("maxSourceChangedLinesAdvisory", True)
        if source > ceiling:
            raise WorkerError(
                f"{SIZE_CEILING_MARKER}: the whole pull request changes {source} source lines "
                f"({totals['tests']} test, {totals['docs']} doc) in {totals['files']} files, over the "
                f"{ceiling}-line reviewable ceiling; this is a size-only excess, not a failed verify")
        if source > source_target and not advisory:
            raise WorkerError(
                f"the proposed pull request changes {source} source lines "
                f"({totals['tests']} test, {totals['docs']} doc) in {totals['files']} files; the "
                f"WorkOrder allows {source_target} source lines")
        if source > source_target:
            print(f"factory advisory: the whole pull request is {source} source lines "
                  f"({totals['tests']} test, {totals['docs']} doc) in {totals['files']} files, over "
                  f"the WorkOrder's {source_target}-source-line target with "
                  f"{source_target - source} lines left; this is advisory, not a rejection",
                  file=sys.stderr, flush=True)
    elif lines > max_lines:
        # Line count is advisory (maxChangedLinesAdvisory, default true for WorkOrders without the
        # field): a modest exceedance warns and the pass continues. Only the line count warns; the file
        # count, the patch-byte ceiling and the protected paths stay binding (ADR-0069).
        advisory = limits.get("maxChangedLinesAdvisory", True)
        if not advisory:
            raise WorkerError(
                f"the proposed pull request is {lines} lines in {files} files; the WorkOrder allows "
                f"{max_lines} lines and {max_files} files, which leaves {max_lines - lines} lines "
                f"and {max_files - files} files")
        print(f"factory advisory: the whole pull request is {lines} lines in {files} files, over the "
              f"WorkOrder's {max_lines}-line target with {max_lines - lines} lines left; this is "
              "advisory, not a rejection", file=sys.stderr, flush=True)
    if files > max_files:
        raise WorkerError(
            f"the proposed pull request is {lines} lines in {files} files; the WorkOrder allows "
            f"{max_files} files, which leaves {max_files - files} files")
    return diff if diff.endswith("\n") else diff + "\n"


def textify(output):
    """Subprocess output the kill left on a TimeoutExpired: bytes even when text was asked for."""
    if isinstance(output, bytes):
        return output.decode("utf-8", "replace")
    return output or ""


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
            raise WorkerError(f"no model credential at {host}", NO_CREDENTIAL)
        if not os.access(host, os.R_OK):
            raise WorkerError(f"the model credential at {host} is not readable", NO_CREDENTIAL)
        directory = os.path.join(scratch, f"credential-{n}")
        os.mkdir(directory, 0o700)
        copy = os.path.join(directory, os.path.basename(host))
        try:
            shutil.copyfile(host, copy)
        except PermissionError:
            raise WorkerError(f"the model credential at {host} is not readable", NO_CREDENTIAL) from None
        os.chmod(copy, 0o600)
        copies.append((directory, target))
    return copies


def harness_command(argv):
    """The harness argv as it runs in the container. The relay runs `opencode run` non-interactively,
    so every permission OpenCode would ask for is auto-rejected ("The user rejected permission to
    use this specific tool call") and the pass ends with no change. The container is the sandbox
    (ADR-0017) and the Pi workers have no permission prompt at all, so `--auto` gives OpenCode the
    same freedom inside it; a tool OpenCode's config explicitly denies stays denied (ADR-0111)."""
    if (len(argv) > 1 and os.path.basename(argv[0]) == "opencode" and argv[1] == "run"
            and "--auto" not in argv):
        return [*argv[:2], "--auto", *argv[2:]]
    return argv


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
    result = {"schemaVersion": "factory.result/v1", "workOrderId": None, "status": "failed",
              "evidence": [], "startedAt": now()}
    scratch = None
    started = time.monotonic()
    phases, secrets, echoed, note = [], set(), "", ""

    if text.strip() == LAUNCH_INPUT:
        # The launch step: nothing to run, but the task cwd must be readable, which is what a
        # macOS privacy denial takes away from the worker.
        launch = {"schemaVersion": LAUNCH_SCHEMA, "status": "failed", "startedAt": now()}
        try:
            os.stat(os.getcwd())
        except OSError as exc:
            launch["error"] = (f"launch probe could not read the task cwd: "
                               f"{type(exc).__name__}; errno={exc.errno}")
        else:
            launch["status"] = "completed"
        launch["elapsedMs"] = max(0, int((time.monotonic() - started) * 1000))
        launch["finishedAt"] = now()
        print(json.dumps(launch), flush=True)
        return 0

    def record(name, begin, code=None, output=""):
        """One phase's diagnostics: elapsed milliseconds, the exit code where there is one, and a
        redacted tail. Failures where no exit code exists still name the phase."""
        entry = {"phase": name, "elapsedMs": int((time.monotonic() - begin) * 1000)}
        if code is not None:
            entry["exitCode"] = code
        tail = redact(output, secrets)
        if tail:
            entry["tail"] = tail
        phases.append(entry)
        return entry

    try:
        text, ask = ask_channel_from(text)
        order = workorder_from(text)
        result["workOrderId"] = order["id"]
        directory_start = time.monotonic()
        try:
            cwd = os.getcwd()
            os.stat(cwd)
        except OSError as exc:
            # Exception messages can contain paths or task input. Keep only OS metadata.
            detail = (f"startup working directory: source=process cwd "
                      f"(relay task/agent/inherited unresolved); {type(exc).__name__}; "
                      f"errno={exc.errno}; no coding started; inspect the relay launch directory "
                      "under its service identity")
            if exc.errno == errno.EPERM and sys.platform == "darwin":
                detail += ("; macOS privacy controls may block this folder: keep "
                           "FACTORY_CHECKOUT_ROOT outside ~/Documents, ~/Desktop, ~/Downloads "
                           "and iCloud (RUNBOOK)")
            record("preparation", directory_start, exc.errno or 1, detail)
            raise WorkerError(detail) from None
        temp_start = time.monotonic()
        try:
            scratch = tempfile.mkdtemp(prefix="factory-run-")
        except OSError as exc:
            # A failing tempdir is preparation too: one bounded Result, no path, no message.
            detail = (f"startup temp directory: {type(exc).__name__}; errno={exc.errno}; "
                      "no coding started; inspect the relay service's TMPDIR")
            record("preparation", temp_start, exc.errno or 1, detail)
            raise WorkerError(detail) from None
        work = os.path.join(scratch, "work")
        repo = origin_repo(cwd)
        if repo != order["repo"]:
            raise WorkerError(f"the checkout {cwd} is {repo}, not {order['repo']}")
        branch = "factory/" + order["workItemId"].rsplit("#", 1)[1]
        checkout_start = time.monotonic()
        try:
            with checkout_lock(cwd) as waited:
                base, default = prepare(cwd, order, branch, work)
                verify = verify_command(cwd, base)
        except WorkerError as exc:
            record("checkout", checkout_start, getattr(exc, "code", None))
            raise
        record("checkout", checkout_start, 0)
        setup_start = time.monotonic()
        try:
            credentials, harness_env = preflight(order, args.image, harness, args.credential,
                                                 args.env, scratch, work, verify)
        except WorkerError as exc:
            code = getattr(exc, "code", None)
            record("environment setup", setup_start, code, PREFLIGHT_TAILS.get(code, ""))
            raise
        record("environment setup", setup_start, 0)
        seconds = max(120, order["budget"]["wallMs"] // 1000 - 60 - waited)
        reserve = 0 if order.get("remoteVerify") else min(seconds // 4, VERIFY_RESERVE_SECONDS)
        # Collecting the patch, printing the Result and the relay's teardown run after the verify.
        # The 0.7/0.25 split left 5% of the pass for them; a flat verify reserve must keep that.
        post = min(seconds // 20, POST_HARNESS_SECONDS)
        sandbox = Sandbox(args.image, re.sub(r"[^A-Za-z0-9_.-]", "_", order["id"]),
                          credentials, harness_env)
        secrets = {value for value in harness_env.values() if value}
        ask_wait = max(30, min(360, seconds - reserve - post - 180))
        ask_env = ({"FACTORY_ASK_URL": str(ask["url"]), "FACTORY_ASK_TOKEN": str(ask["token"]),
                    "FACTORY_ASK_WAIT": str(ask_wait)}
                   if ask.get("url") and ask.get("token") else None)
        if ask_env:
            secrets.add(ask_env["FACTORY_ASK_TOKEN"])  # never echo it in the relay log
        # A copied credential's whole content is a secret, in both the raw and collapsed form:
        # the tail is whitespace-collapsed before any replace, so a multi-line credential (a
        # pretty-printed auth JSON, a PEM key) matches only through its collapsed form.
        for directory, _ in credentials:
            for name in os.listdir(directory):
                with open(os.path.join(directory, name)) as handle:
                    secrets.update(credential_secrets(handle.read()))
        harness_start = time.monotonic()
        code, out, err = sandbox.run([*harness_command(harness), text], seconds - reserve - post, work,
                                     harness=True,
                                     env=ask_env)
        echoed = out + err
        harness_phase = record("harness execution", harness_start, code, echoed)
        # for the relay log; the Result line comes last. Redacted by the same rules as the tail.
        print(redact(echoed[-4000:], secrets), flush=True)
        note = ""
        if code != 0:
            line = last_error_line(harness_error_log(harness, harness_env, credentials))
            if line:
                note = redact(f"harness log: {line}", secrets, limit=HARNESS_ERROR_NOTE)
                harness_phase["tail"] = with_note(harness_phase.get("tail", ""), note)
        if code == 124:
            # Salvage (ADR-0086): the harness's clock ran out, but the checkout may hold its work.
            # Collect it without verify as a partial patch on this failed Result; collect() commits
            # uncommitted work and applies the size and file limits, so a refused or empty partial
            # leaves no patch. The timeout stays the error either way: the Foreman names timeout
            # incidents by this text (ADR-0081).
            salvage = time.monotonic()
            try:
                partial = collect(sandbox, work, base, default, order["goal"], order["constraints"])
            except WorkerError as exc:
                record("patch collection", salvage, getattr(exc, "code", None), str(exc))
            else:
                record("patch collection", salvage, 0)
                result["patch"] = {"format": "unified", "diff": partial, "baseSha": base}
            raise WorkerError("the harness timed out")
        if code != 0:
            detail = " ".join((err or out).split())[-300:]
            raise WorkerError(f"the harness exited {code}" + (f": {detail}" if detail else ""))
        # A completed implement continuation finalizes the retained checkpoint plus any new edits
        # against the WorkOrder's original baseSha, so the Foreman's apply path keeps the checkpoint
        # content (#451); the pass still launched from and verifies the checkpoint (launch_base above).
        # A fix keeps the checkpoint as the applied base: its patch continues the reviewed head.
        collected_base = base
        if order["kind"] == "implement" and order.get("continuationSha"):
            collected_base = order["baseSha"]
        patch_start = time.monotonic()
        try:
            diff = collect(sandbox, work, collected_base, default, order["goal"], order["constraints"],
                           parent=base)
        except WorkerError as exc:
            record("patch collection", patch_start, getattr(exc, "code", None), str(exc))
            raise
        record("patch collection", patch_start, 0)
        if not order.get("remoteVerify"):  # else a GitHub runner runs the verify (ADR-0045)
            verify_start = time.monotonic()
            code, out, err = sandbox.run(["sh", "-c", verify], reserve, work)
            output = out + err
            echoed += output
            summary = verification_summary(output)
            record("verification", verify_start, code, summary)
            tail = redact(summary, secrets).replace("```", "")
            result["evidence"] = [{"kind": "test", "command": verify, "exit": code, "tail": tail}]
            if code != 0:
                detail = redact(output[-400:], secrets, limit=400)
                raise WorkerError(f"verify exited {code}: {detail}")
        result.update(status="completed",
                      patch={"format": "unified", "diff": diff, "baseSha": collected_base})
    except WorkerError as exc:
        result["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - any failure must still end in a Result line
        result["error"] = f"runner failed: {type(exc).__name__}: {exc}"
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    diagnostics = {"elapsedMs": max(0, int((time.monotonic() - started) * 1000))}
    if phases:
        diagnostics["phases"] = phases
    tail = redact(echoed, secrets)
    if note:
        tail = with_note(tail, note)
    if tail:
        diagnostics["tail"] = tail
    result["diagnostics"] = diagnostics
    result["finishedAt"] = now()
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
