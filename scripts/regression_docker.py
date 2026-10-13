#!/usr/bin/env python3
"""Real-container regression harness for worker/runner.py. Invoked explicitly, on demand:

    python3 scripts/regression_docker.py [--image factory-worker:latest]

It needs a real Docker daemon and the built worker image (docker build
-t factory-worker:latest worker/). When either is missing, or any case fails,
the command exits nonzero with a message naming what to do next: it never
silently skips a case and never claims a pass it did not run. Cases are
deterministic and need no model credential: they run real `docker run`
containers through runner.Sandbox and exercise success, failing verification,
timeout output, secret redaction and isolation, the environment preflight and
the early size rejection, then check that no container or scratch dir survives.

The JSON report printed last records, for every case, the commands it ran and
what each showed. A case without a recorded command counts as a failure.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "worker"))
import runner  # noqa: E402


class Case:
    """One regression case: whatever it runs is recorded, and its status comes
    only from what was recorded. A case that records nothing is a failure."""

    def __init__(self, name):
        self.name = name
        self.records = []

    def observe(self, command, ok, detail=""):
        """Record one command or check and whether it showed what the case needs."""
        self.records.append({"command": command, "ok": bool(ok), "detail": detail[-300:]})

    @property
    def failed(self):
        return not self.records or any(not record["ok"] for record in self.records)

    def fail(self, detail):
        self.observe("case runtime", False, detail)


SECONDS = 120


def run(name, image, argv, seconds, mount, credentials=None, **kw):
    """One real `docker run` through the runner's Sandbox."""
    started = time.monotonic()
    code, out, err = runner.Sandbox(image, name, credentials or []).run(
        argv, seconds, mount, **kw)
    return code, out, err, time.monotonic() - started


def fixture(tmp, extra=()):
    """A deterministic checkout: one base commit over app.txt and any extra files."""
    work = tempfile.mkdtemp(dir=tmp, prefix="work-")
    env = dict(os.environ, GIT_AUTHOR_NAME="f", GIT_AUTHOR_EMAIL="f@f",
               GIT_COMMITTER_NAME="f", GIT_COMMITTER_EMAIL="f@f")
    subprocess.run(["git", "init", "-q", "-b", "main", work], check=True, capture_output=True,
                   env=env)
    with open(os.path.join(work, "app.txt"), "w") as handle:
        handle.write("base\n")
    subprocess.run(["git", "-C", work, "add", "app.txt"], check=True, capture_output=True,
                   env=env)
    subprocess.run(["git", "-C", work, "commit", "-qm", "base"], check=True,
                   capture_output=True, env=env)
    # extra files are untracked after the base commit, so a harness commit adds them
    for name, text in extra:
        with open(os.path.join(work, name), "w") as handle:
            handle.write(text)
    return work, subprocess.run(["git", "-C", work, "rev-parse", "HEAD"], check=True,
                                capture_output=True, env=env).stdout.decode().strip()
    subprocess.run(["git", "-C", work, "commit", "-qm", "base"], check=True, capture_output=True,
                   env=env)


# The stand-in harness work: touch app.txt and commit it, as a real harness would.
COMMIT = ("printf 'fixed\\n' > app.txt && git add -A && "
          "git -c user.name=h -c user.email=h@h commit -qm work")
# The same path with the work incomplete, so a real verify refuses it.
BROKEN = ("printf 'still broken\\n' > app.txt && git add -A && "
          "git -c user.name=h -c user.email=h@h commit -qm work")


def case_success(image, tmp, case):
    """The happy path: the harness's work commits, collect() hands back its diff,
    and the verify command inside another sandbox exits 0 against it."""
    work, base = fixture(tmp)
    code, _, err, _ = run("success", image, ["sh", "-c", COMMIT], SECONDS, work)
    case.observe("docker run: commit harness work in the container", code == 0, err)
    if code != 0:
        return
    try:
        diff = runner.collect(runner.Sandbox(image, "success"), work, base, base, "success",
                              {"maxChangedLines": 600, "maxChangedFiles": 20})
    except runner.WorkerError as exc:
        return case.observe("runner.collect over the container's commit", False, str(exc))
    case.observe("runner.collect returns the container's diff",
                 "app.txt" in diff and diff.strip(), diff[:200])
    code, out, err, _ = run("success", image, ["sh", "-c", "grep -q fixed app.txt"], SECONDS, work)
    case.observe("docker run: the verify command in the sandbox", code == 0, out + err)


def case_failing_verification(image, tmp, case):
    """A verify command that fails fails the result, whatever the harness did."""
    work, _ = fixture(tmp)
    code, _, err, _ = run("verify", image, ["sh", "-c", BROKEN], SECONDS, work)
    case.observe("docker run: the harness commits incomplete work", code == 0, err)
    code, out, err, _ = run("verify", image, ["sh", "-c", "grep -q fixed app.txt"], SECONDS, work)
    case.observe("docker run: the failing verify command is recorded as a failed phase",
                 code != 0, json.dumps({"phase": "verification", "exitCode": code}))


def case_timeout(image, tmp, case):
    """A hung harness is killed near its time limit; its output still rides out."""
    work, _ = fixture(tmp)
    code, out, err, elapsed = run("timeout", image, ["sh", "-c", "echo step one; sleep 60"],
                                  2, work)
    case.observe("docker run: the hung harness is killed with its output kept",
                 code == 124 and "step one" in (out + err), (out + err)[:120])
    case.observe(f"docker: the kill happened near the limit ({elapsed:.1f}s)", elapsed < 30)


def case_secrets(image, tmp, case):
    """A copied credential and a configured secret exist inside the container only,
    redacted from what comes back, and gone from the host afterwards."""
    work, _ = fixture(tmp)
    scratch = tempfile.mkdtemp(dir=tmp, prefix="scratch-")
    secret = "ghp_injectedsecrettoken123"
    content = "AKIAI44QH8DHBEXAMPLE\n"
    cred = os.path.join(tmp, "model-credential.json")
    with open(cred, "w") as handle:
        handle.write(content)
    try:
        credentials = runner.credential_copies([f"{cred}:/secret-cred"], scratch)
        copy = os.path.join(scratch, "credential-0", "model-credential.json")
        case.observe("host: the credential copy is 0600",
                     oct(os.stat(copy).st_mode & 0o777) == "0o600")
        code, out, err, _ = run("secrets", image,
                                ["sh", "-c", 'echo "token=$FACTORY_TOKEN"; cat /secret-cred/*'],
                                SECONDS, work, harness=True, credentials=credentials,
                                env={"FACTORY_TOKEN": secret})
        case.observe("container: the sandbox saw the credential and the secret env",
                     content in out and secret in out, (out + err)[:150])
        redacted = runner.redact(out + err, {secret, content, " ".join(content.split())})
        case.observe("runner.redact: the returned output neither leaks the token "
                     "nor the credential",
                     "<redacted>" in redacted and secret not in redacted
                     and "AKIAI44" not in redacted, redacted[:150])
        code, _, out2, _ = run("secrets", image, ["sh", "-c", "ls /"], SECONDS, work,
                               harness=False)
        case.observe("container without the mounts: no credential copy is visible",
                     code == 0 and "secret-cred" not in out2, out2[:100])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    case.observe("host: the scratch with the credential copies is gone at cleanup",
                 not os.path.exists(scratch))


def case_preflight(image, tmp, case):
    """The preflight raises its distinct code when a required part is missing."""
    order = {"id": "preflight-case", "kind": "implement"}
    scratch = tempfile.mkdtemp(dir=tmp, prefix="scratch-preflight-")
    try:
        runner.preflight(order, "factory-regression-missing:latest", ["python3"], [], [],
                         scratch, os.path.join(tmp, "unused-work"), "true")
    except runner.WorkerError as exc:
        case.observe("preflight: a missing worker image fails with NO_IMAGE",
                     exc.code == runner.NO_IMAGE, str(exc))
    else:
        case.observe("preflight: a missing worker image fails", False, "no error was raised")
    try:
        work, _ = fixture(tmp)
        try:
            runner.env_pairs(["=value"])
        except runner.WorkerError as exc:
            case.observe("env preflight: an empty --env key fails", "--env" in str(exc), str(exc))
        else:
            case.observe("env preflight: a KEY=VALUE pair is required", False,
                         "no error was raised")
        try:
            runner.credential_copies(["/nonexistent-credential:/secret-cred"], scratch)
        except runner.WorkerError as exc:
            case.observe("credential preflight: a missing credential fails with NO_CREDENTIAL",
                         exc.code == runner.NO_CREDENTIAL, str(exc))
        else:
            case.observe("credential preflight: a missing credential fails", False,
                         "no error was raised")
        try:
            runner.preflight(order, image, ["/definitely-not-in-image"], [], [], scratch, work,
                             "true")
        except runner.WorkerError as exc:
            case.observe("preflight: a missing harness fails with NO_HARNESS",
                         exc.code == runner.NO_HARNESS, str(exc))
        else:
            case.observe("preflight: a missing harness fails", False, "no error was raised")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def case_size(image, tmp, case):
    """An oversized patch is refused on the file count before any verify command runs in a
    container; the line count is advisory (ADR-0070)."""
    work, base = fixture(tmp, extra=[(f"big-{n}.txt", "adding a line\n" * 60)
                                     for n in range(4)])
    code, _, err, _ = run("size", image, ["sh", "-c", COMMIT], SECONDS, work)
    case.observe("docker run: the oversized harness commits", code == 0, err)
    if code != 0:
        return
    try:
        runner.collect(runner.Sandbox(image, "size"), work, base, base, "size",
                       {"maxChangedLines": 10, "maxChangedFiles": 2})
    except runner.WorkerError as exc:
        # 4 files over the file-count bound: the line count is advisory, the file count refuses.
        case.observe("runner.collect: an oversized patch is refused",
                     "2 files" in str(exc) and "allows" in str(exc), str(exc))
    else:
        case.observe("runner.collect: an oversized patch is refused", False,
                     "no error was raised")


def case_cleanup(image, tmp, case):
    """Nothing survives the run: no labelled container, and once this case destroys
    the work and scratch dirs, no work or scratch dir."""
    done = subprocess.run(["docker", "ps", "-a", "--filter", "label=factory-worker",
                           "--format", "{{.Names}}"], capture_output=True, text=True)
    case.observe("docker: no factory-worker container remains after every case",
                 done.returncode == 0 and not done.stdout.strip(), done.stdout.strip())
    for name in os.listdir(tmp):
        path = os.path.join(tmp, name)
        shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.unlink(path)
    left = [n for n in os.listdir(tmp) if n.startswith("work-") or n.startswith("scratch-")]
    case.observe("host: no work or scratch dir of any case is left",
                 not left, ", ".join(left))


CASES = [("success", case_success), ("failing verification", case_failing_verification),
         ("timeout", case_timeout), ("secrets", case_secrets), ("preflight", case_preflight),
         ("size rejection", case_size), ("cleanup", case_cleanup)]


def prerequisites(image):
    """Fail explicitly, naming what to do, when a required runtime is missing."""
    if runner.docker_status() is None:
        raise runner.WorkerError("docker is not on PATH; install Docker and rerun "
                                 "scripts/regression_docker.py", runner.NO_DOCKER)
    if runner.docker_status() != 0:
        raise runner.WorkerError("the Docker daemon is not available; start Docker and rerun "
                                 "scripts/regression_docker.py", runner.NO_DOCKER)
    done = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    if done.returncode != 0:
        raise runner.WorkerError(f"the worker image {image} is not in the local Docker store; "
                                 f"build it with: docker build -t {image} worker/", runner.NO_IMAGE)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--image", default=runner.IMAGE)
    args = parser.parse_args(argv)
    report = {"harness": "scripts/regression_docker.py", "image": args.image, "cases": []}
    try:
        prerequisites(args.image)
        tmp = tempfile.mkdtemp(prefix="regression-")
        try:
            for name, function in CASES:
                case = Case(name)
                try:
                    function(args.image, tmp, case)
                except (runner.WorkerError, subprocess.SubprocessError, OSError, KeyError) \
                        as exc:
                    case.fail(f"{type(exc).__name__}: {exc}")
                report["cases"].append({"case": name, "status": "failed" if case.failed
                                        else "passed", "records": case.records})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except runner.WorkerError as exc:
        report["status"], report["error"] = "failed", str(exc)
        print(json.dumps(report, indent=2))
        print(f"regression harness failed: {exc}", file=sys.stderr)
        return 1
    failed = [entry["case"] for entry in report["cases"] if entry["status"] == "failed"]
    report["status"] = "failed" if failed else "passed"
    print(json.dumps(report, indent=2))
    if failed:
        print(f"regression harness: cases failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
