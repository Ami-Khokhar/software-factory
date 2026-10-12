"""Narrow host controls for Docker (OrbStack), without a host shell or socket in Day Desk."""
import subprocess
import time

from .mcp import ToolError


def run(*argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        raise ToolError(f"Docker control could not run {argv[0]}; check the host installation") from None


def status():
    return {"running": run("docker", "info").returncode == 0}


def control(action, store, lock):
    if action not in ("start", "stop"):
        raise ToolError("Docker action must be start or stop")
    with lock:  # dispatch uses this same lock, so stop cannot race a new factory worker
        if action == "stop":
            active = [wi["id"] for wi in store.all()
                      if wi["state"] in ("ready", "changes_requested", "dispatching", "implementing")]
            if active:
                raise ToolError("Docker stop refused: factory work is queued or running")
        running = status()["running"]
        if running == (action == "start"):
            return {"running": running, "changed": False}
        if action == "stop":
            containers = run("docker", "ps", "-q")
            if containers.returncode != 0:
                raise ToolError("Docker stop refused: could not check running containers")
            if containers.stdout.strip():
                raise ToolError("Docker stop refused: containers are running, including other apps or Day Desk")
        if run("orb", action).returncode != 0:
            raise ToolError(f"OrbStack {action} failed; check OrbStack on the host")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            running = status()["running"]
            if running == (action == "start"):
                return {"running": running, "changed": True}
            time.sleep(2)
        raise ToolError(f"Docker did not finish {action} within 120 seconds; check OrbStack")
