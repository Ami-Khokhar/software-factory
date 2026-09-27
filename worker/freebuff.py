#!/usr/bin/env python3
"""Run one Freebuff prompt through its terminal UI and wait for its local session log.

The worker invokes this as ``python3 /usr/local/bin/factory-freebuff <prompt>``.  Freebuff 0.0.203
has no non-interactive task command, so this module drives its initial picker in a PTY.  It keeps a
fresh HOME for each invocation, copying only the configuration files needed to authenticate; that
also makes the session log for this pass unambiguous.
"""
import argparse
import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


FREEBUFF = "freebuff"
WORKDIR = "/work"
DEFAULT_TIMEOUT = 900
INPUT_SETTLE_SECONDS = 0.2
COMPLETION = "Main prompt finished"
SDK_START = "Sending message with sdk run config"


class HarnessError(Exception):
    """Freebuff did not carry out the task safely."""


def isolated_home(source_home=None):
    """Make a HOME with credentials and settings, but without prior project/session logs."""
    source = Path(source_home or os.environ.get("HOME", "")) / ".config" / "manicode"
    home = Path(tempfile.mkdtemp(prefix="factory-freebuff-"))
    os.chmod(home, 0o700)
    if not (source / "credentials.json").is_file():
        shutil.rmtree(home)
        raise HarnessError("no Freebuff credentials.json is available")
    target = home / ".config" / "manicode"
    target.mkdir(parents=True, mode=0o700)
    for name in ("credentials.json", "settings.json", "freebuff-instance-owner.json",
                 "freebuff-metadata.json"):
        path = source / name
        if path.is_file():
            shutil.copy2(path, target / name)
    return str(home)


def log_finished(log):
    """True only when this session started an SDK request and that request completed."""
    started = False
    try:
        lines = Path(log).read_text(errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue  # Freebuff can be writing the final JSON line while we read it.
        if SDK_START in str(record.get("msg", "")):
            started = True
        elif started and record.get("msg") == COMPLETION:
            return True
    return False


class FreebuffPty:
    def __init__(self, home):
        master, slave = pty.openpty()
        os.set_blocking(master, False)
        env = dict(os.environ, HOME=home, XDG_CONFIG_HOME=os.path.join(home, ".config"))
        try:
            self.process = subprocess.Popen([FREEBUFF, "--cwd", WORKDIR], cwd=WORKDIR, stdin=slave,
                                            stdout=slave, stderr=slave, env=env, preexec_fn=os.setsid)
        except OSError as exc:
            os.close(master)
            raise HarnessError(f"could not start Freebuff: {exc}") from None
        finally:
            os.close(slave)
        self.master = master
        self.output = ""
        self.declined_repo_prompt = False
        self.check_prompts = True

    def write(self, text, deadline=None):
        data, written = text.encode(), 0
        deadline = deadline or time.monotonic() + 5
        while written < len(data):
            if self.process.poll() is not None:
                raise HarnessError(f"Freebuff exited while accepting input (exit {self.process.returncode})")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarnessError("timed out sending input to Freebuff")
            _, writable, _ = select.select([], [self.master], [], min(0.1, remaining))
            if not writable:
                self.pump(0)
                continue
            try:
                written += os.write(self.master, data[written:])
            except BlockingIOError:
                continue
            self.pump(0)

    def close(self):
        try:
            os.close(self.master)
        except OSError:
            pass

    def stop(self):
        if self.process.poll() is None:
            try:
                self.write("/exit")
                time.sleep(INPUT_SETTLE_SECONDS)
                self.write("\r")
            except HarnessError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
        self.close()

    def _check_prompts(self, chunk):
        if not getattr(self, "check_prompts", True):
            return
        recent = (self.output[-4000:] + chunk).lower()
        normalized = re.sub(r"\s+", " ", recent)
        load_prompt = ("load and run these? [y/n]" in normalized or
                       "load and run these? (y/n)" in normalized)
        if load_prompt and not self.declined_repo_prompt:
            self.declined_repo_prompt = True
            self.write("n\r")
        if any(word in normalized for word in ("authentication required", "sign in to freebuff",
                                               "payment required", "permission denied")):
            raise HarnessError("Freebuff requested authentication, payment, or permission")

    def pump(self, timeout):
        if self.process.poll() is not None:
            return False
        ready, _, _ = select.select([self.master], [], [], timeout)
        if not ready:
            return False
        try:
            data = os.read(self.master, 4096)
        except OSError:
            return False
        if not data:
            return False
        chunk = data.decode(errors="replace")
        self._check_prompts(chunk)
        self.output = (self.output + chunk)[-8192:]
        return True

    def wait_for(self, text, deadline):
        while text not in self.output:
            if self.process.poll() is not None:
                raise HarnessError(f"Freebuff exited before {text!r} (exit {self.process.returncode})")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarnessError(f"timed out waiting for Freebuff {text!r}")
            self.pump(min(0.2, remaining))


def session_log(home, deadline, process=None):
    root = Path(home) / ".config" / "manicode" / "projects" / Path(WORKDIR).name / "chats"
    while time.monotonic() < deadline:
        logs = list(root.glob("*/log.jsonl")) if root.exists() else []
        if len(logs) == 1:
            return logs[0]
        if len(logs) > 1:
            raise HarnessError("Freebuff created more than one local session log")
        if process is not None:
            process.pump(0)
            if process.process.poll() is not None:
                raise HarnessError(f"Freebuff exited before creating a local session log (exit {process.process.returncode})")
        time.sleep(0.1)
    raise HarnessError("Freebuff did not create a local session log")


def run(prompt, timeout, source_home=None):
    """Run *prompt*, returning only after Freebuff records completion in the new session log."""
    if timeout <= 0:
        raise HarnessError("timeout must be positive")
    if not prompt.strip():
        raise HarnessError("prompt must not be empty")
    if any((ord(char) < 32 and char not in "\n\t") or 127 <= ord(char) < 160
           for char in prompt):
        raise HarnessError("WorkOrder contains terminal control characters")
    home = isolated_home(source_home)
    process = None
    deadline = time.monotonic() + timeout
    try:
        process = FreebuffPty(home)
        process.wait_for("Start coding for free", deadline)
        process.wait_for("GLM 5.3 Flash", deadline)
        print("factory-freebuff: GLM picker ready", file=sys.stderr, flush=True)
        process.write("\r", deadline)
        process.wait_for("Enter a coding task or / for commands", deadline)
        process.check_prompts = False
        print("factory-freebuff: composer ready", file=sys.stderr, flush=True)
        process.write("\x1b[200~" + prompt + "\x1b[201~", deadline)
        time.sleep(INPUT_SETTLE_SECONDS)  # Freebuff needs the composer update before Enter submits it.
        process.write("\r", deadline)
        log = session_log(home, deadline, process)
        print("factory-freebuff: waiting for task completion", file=sys.stderr, flush=True)
        while time.monotonic() < deadline:
            if log_finished(log):
                print("factory-freebuff: prompt finished", file=sys.stderr, flush=True)
                return
            if process.process.poll() is not None:
                raise HarnessError(f"Freebuff exited before completing the prompt (exit {process.process.returncode})")
            process.pump(min(0.1, deadline - time.monotonic()))
        raise HarnessError("Freebuff did not record prompt completion before the timeout")
    finally:
        if process:
            process.stop()
        shutil.rmtree(home, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="run a Freebuff task in the factory worker")
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("FREEBUFF_TIMEOUT_SECONDS", DEFAULT_TIMEOUT)))
    parser.add_argument("prompt")
    args = parser.parse_args(argv)
    try:
        run(args.prompt, args.timeout)
    except HarnessError as exc:
        print(f"factory-freebuff failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
