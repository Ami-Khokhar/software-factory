"""Guarded worker health probes and fixed relay registry reloads."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import time
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

from . import routing
from .mcp import ToolError
from .relay import RelayError

HEALTH_SCHEMA = "factory.health/v1"
HEALTH_TTL = 15 * 60
PROBE_SECONDS = 120
PROCESS_SECONDS = 390
HARNESS_NAMES = {"pi", "codex", "opencode"}
HEALTH_STATUSES = {"ok", "unexpected_response", "harness_deadline", "provider_quota",
                   "provider_rate_limit", "provider_auth", "provider_error", "unavailable"}
HEALTH_EVENTS = {"preflight", "probe", "launch"}
# The launch step: one tiny relay task whose cwd is the configured checkout folder, so the check
# proves the whole launch path the probe cannot see (#430): the interpreter the agent entry names,
# the task cwd and macOS privacy denials on that folder. One attempt, no retries, never a WorkOrder.
LAUNCH_PROMPT = "Health probe: reply with exactly: ok"
LAUNCH_SECONDS = 120
HEALTH_FIELDS = {"ts", "event", "tool", "status", "exitCode", "retries", "latencyMs",
                 "promptBytes", "responseBytes", "diagnosticBytes"}
SECRET_ENV = re.compile(r"(?:SECRET|TOKEN|KEY|PASSWORD|CREDENTIAL|AUTH)", re.IGNORECASE)
SAFE_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?\Z")
HARNESS_FLAGS = {
    "pi": {"--print": False, "--provider": True, "--model": True, "--models": True,
           "--thinking": True, "--no-session": False, "--no-extensions": False,
           "--no-skills": False, "--no-prompt-templates": False},
    "codex": {"exec": False, "--model": True, "--skip-git-repo-check": False},
    "opencode": {"run": False, "--model": True},
}
THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh"}
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}\Z")
CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}\Z")
ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "worker" / "runner.py"
HEALTH_SCRIPT = ROOT / "scripts" / "provider_health.py"


def _now():
    return datetime.now(timezone.utc)


def _stamp(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_registry(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        agents = data["agents"]
    except (OSError, ValueError, KeyError, TypeError):
        raise ToolError("the relay worker registry is unavailable or malformed") from None
    if not isinstance(agents, list):
        raise ToolError("the relay worker registry has no agents list")
    return agents


def _one(args, flag, required=False):
    positions = [index for index, value in enumerate(args) if value == flag]
    if len(positions) > 1 or required and (len(positions) != 1 or positions[0] + 1 >= len(args)):
        raise ToolError("the worker configuration does not have one valid model pin")
    if not positions:
        return None
    position = positions[0]
    if position + 1 >= len(args) or args[position + 1].startswith("-"):
        raise ToolError("the worker configuration does not have one valid model pin")
    return args[position + 1]


def _candidate(config, worker_id):
    if not isinstance(worker_id, str) or not worker_id:
        raise ToolError("worker must name a registered relay agent")
    if not config.agents_file:
        raise ToolError("FACTORY_AGENTS_FILE is not set")
    agents = _read_registry(config.agents_file)
    agent = next((row for row in agents if isinstance(row, dict) and row.get("id") == worker_id), None)
    if agent is None:
        raise ToolError("worker is not registered in the relay")
    args = agent.get("args")
    if (agent.get("command") != "python3" or not isinstance(args, list)
            or not all(isinstance(arg, str) for arg in args)
            or not args or args[0] != str(RUNNER)):
        raise ToolError("worker does not run this deployment's worker/runner.py")
    if any("\0" in arg for arg in args):
        raise ToolError("worker configuration contains an invalid argument")
    if "--" not in args or args.index("--") < 1:
        raise ToolError("worker has no sandboxed harness after the runner")
    stop = args.index("--")
    image, image_seen, credentials, environment = "factory-worker:latest", False, [], []
    index = 1
    while index < stop:
        flag = args[index]
        if flag not in ("--image", "--credential", "--env") or index + 1 >= stop:
            raise ToolError("worker uses an unsupported runner option")
        value = args[index + 1]
        if flag == "--image":
            if image_seen:
                raise ToolError("worker repeats its image option")
            if not IMAGE.fullmatch(value):
                raise ToolError("worker image name is invalid")
            image = value
            image_seen = True
        elif flag == "--credential":
            host, separator, target = value.rpartition(":")
            if not separator or not host or "\0" in host or not target.startswith("/"):
                raise ToolError("worker has an invalid credential mount")
            mount = PurePosixPath("/" + target.lstrip("/"))
            if (".." in mount.parts or str(mount) == "/" or any(
                    str(mount) == root or str(mount).startswith(root + "/")
                    for root in ("/work", "/proc", "/sys", "/dev", "/etc", "/usr", "/bin", "/sbin", "/lib", "/run"))):
                raise ToolError("worker credential mount overlaps a protected container path")
            credentials.append((os.path.expanduser(host), target))
        else:
            key, separator, _ = value.partition("=")
            if not separator or not SAFE_ENV_KEY.fullmatch(key) or len(value) > 8192:
                raise ToolError("worker has an invalid environment entry")
            if SECRET_ENV.search(key):
                raise ToolError("worker environment contains a secret-bearing key")
            environment.append(value)
        index += 2
    harness = args[stop + 1:]
    if not harness or harness[0] not in HARNESS_NAMES:
        raise ToolError("worker harness must be pi, codex or opencode")
    flags = HARNESS_FLAGS[harness[0]]
    index = 1
    while index < len(harness):
        flag = harness[index]
        if flag not in flags or flags[flag] and (index + 1 >= len(harness)
                                                  or harness[index + 1].startswith("-")):
            # Only flag names are diagnostic data. Values, positional text and =payload stay private.
            name = flag if re.fullmatch(r"--[a-z][a-z-]{0,39}", flag) else "[redacted]"
            raise ToolError(f"worker harness uses an unsupported option {name}; "
                            "use the approved registered template flags with required values")
        if flags[flag]:
            value = harness[index + 1]
            if flag in ("--model", "--models") and (
                    not MODEL.fullmatch(value) or value.lower() in {"latest", "auto", "default"}):
                raise ToolError("worker model pin is invalid")
            if flag == "--provider" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
                raise ToolError("worker provider pin is invalid")
            if flag == "--thinking" and value not in THINKING_LEVELS:
                raise ToolError("worker thinking setting is unsupported")
        index += 2 if flags[flag] else 1
    if harness[0] == "pi":
        model = _one(harness, "--model", required=True)
        models = _one(harness, "--models", required=True)
        provider = _one(harness, "--provider", required=True)
        if ("/" not in models or models.split("/", 1)[0] != provider
                or models.split("/")[-1] != model.split("/")[-1]
                or "/" in model and model.split("/", 1)[0] != provider):
            raise ToolError("worker model pins do not agree")
    else:
        model = _one(harness, "--model", required=True)
    if not MODEL.fullmatch(model) or model.lower() in {"latest", "auto", "default"}:
        raise ToolError("worker model pin is invalid")
    if not credentials:
        raise ToolError("worker has no copied credential mount")
    return {"agent": agent, "args": args, "image": image, "credentials": credentials,
            "environment": environment, "harness": harness, "model": model}


def _credential_metadata(credentials):
    metadata = []
    for path, target in credentials:
        try:
            info = os.stat(path)
            readable = os.path.isfile(path) and os.access(path, os.R_OK)
        except OSError:
            raise ToolError("a configured credential file is unavailable") from None
        if not readable:
            raise ToolError("a configured credential file is unavailable")
        metadata.append({"pathHash": hashlib.sha256(path.encode()).hexdigest(), "target": target,
                         "size": info.st_size, "mtimeNs": info.st_mtime_ns,
                         "mode": info.st_mode & 0o777})
    return metadata


def _image_id(image):
    try:
        done = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                              capture_output=True, text=True, timeout=10)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    value = done.stdout.strip() if done.returncode == 0 else ""
    return value if IMAGE_ID.fullmatch(value) else None


def _binding(candidate, credential_metadata, image_id):
    value = {"agent": candidate["agent"], "model": candidate["model"], "image": candidate["image"],
             "imageId": image_id, "credentials": credential_metadata}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _safe_report(stdout, returncode, harness):
    try:
        raw = json.loads(stdout)
        if (raw.get("schemaVersion") != HEALTH_SCHEMA or raw.get("status") not in HEALTH_STATUSES
                or not isinstance(raw.get("events"), list) or len(raw["events"]) > 2):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        return {"schemaVersion": HEALTH_SCHEMA, "status": "unavailable", "finishedAt": _stamp(_now()),
                "events": []}
    events = []
    for event in raw["events"]:
        if not isinstance(event, dict):
            continue
        clean = {key: value for key, value in event.items() if key in HEALTH_FIELDS}
        if clean.get("event") not in HEALTH_EVENTS or clean.get("status") not in HEALTH_STATUSES:
            continue
        clean["tool"] = harness[0]
        if not isinstance(clean.get("ts"), str) or not re.fullmatch(
                r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", clean["ts"]):
            clean["ts"] = _stamp(_now())
        for key in HEALTH_FIELDS - {"ts", "event", "tool", "status"}:
            if key in clean and (type(clean[key]) is not int or clean[key] < 0):
                clean[key] = 0
        events.append(clean)
    has_probe = any(event.get("event") == "probe" and event.get("status") == "ok" for event in events)
    status = (raw["status"] if returncode == 0 or raw["status"] != "ok" else "unavailable")
    if status == "ok" and (not has_probe or not any(
            event.get("event") == "preflight" and event.get("status") == "ok" for event in events)):
        status = "unavailable"
    finished = raw.get("finishedAt")
    if not isinstance(finished, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", finished):
        finished = _stamp(_now())
    return {"schemaVersion": HEALTH_SCHEMA, "status": status, "finishedAt": finished,
            "events": events}


def _stop_probe_containers(probe_id):
    label = f"factory-worker=health-{probe_id}"
    try:
        done = subprocess.run(["docker", "ps", "-q", "--filter", f"label={label}"],
                              capture_output=True, text=True, timeout=10)
        ids = [value for value in done.stdout.split() if CONTAINER_ID.fullmatch(value)]
        if done.returncode == 0 and ids:
            subprocess.run(["docker", "stop", "--time", "5", *ids], capture_output=True, timeout=10)
        return done.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _terminate_probe(process, probe_id):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
    return _stop_probe_containers(probe_id)


def _run(candidate):
    probe_id = uuid.uuid4().hex
    try:
        scratch_root = tempfile.mkdtemp(prefix=f"factory-health-{probe_id}-")
    except OSError:
        return ({"schemaVersion": HEALTH_SCHEMA, "status": "unavailable",
                 "finishedAt": _stamp(_now()), "events": []}, True)
    argv = [sys.executable, str(HEALTH_SCRIPT), "--image", candidate["image"], "--deadline",
            str(PROBE_SECONDS), "--probe-id", probe_id]
    for path, target in candidate["credentials"]:
        argv.extend(("--credential", f"{path}:{target}"))
    for value in candidate["environment"]:
        argv.extend(("--env", value))
    argv.extend(("--", *candidate["harness"]))
    process = None
    child_env = {key: os.environ[key] for key in (
        "PATH", "HOME", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
        "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY") if key in os.environ}
    child_env["FACTORY_HEALTH_SCRATCH_ROOT"] = scratch_root
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True,
                                   cwd=ROOT, env=child_env)
        stdout, _ = process.communicate(timeout=PROCESS_SECONDS)
    except subprocess.TimeoutExpired:
        cleanup_ok = _terminate_probe(process, probe_id)
        report = {"schemaVersion": HEALTH_SCHEMA, "status": "unavailable",
                  "finishedAt": _stamp(_now()), "events": []}
        return report, cleanup_ok
    except (OSError, ValueError):
        report = {"schemaVersion": HEALTH_SCHEMA, "status": "unavailable",
                  "finishedAt": _stamp(_now()), "events": []}
        return report, True
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)
    return _safe_report(stdout, process.returncode, candidate["harness"]), True


def _launch(relay, config, repo, worker_id):
    """One relay task that proves the relay can start the agent's interpreter in the checkout
    folder: its cwd is `config.checkout(repo)`, the same value a real WorkOrder submit sends.
    Returns (event, reason); (None, None) when this check carries no launch step (no relay, or
    no checkout folder for the repo), which keeps the check to the synthetic provider steps."""
    cwd = config.checkout(repo) if repo else None
    if not cwd:
        return None, None

    def failed(status, exit_code, reason, started):
        return ({"ts": _stamp(_now()), "event": "launch", "tool": "relay", "status": status,
                 "exitCode": exit_code, "retries": 0,
                 "latencyMs": max(0, int((_now() - started).total_seconds() * 1000))}, reason)

    started = _now()
    try:
        task = relay.submit(worker_id, LAUNCH_PROMPT, f"health-launch-{uuid.uuid4().hex}",
                            LAUNCH_SECONDS * 1000, cwd=cwd)
    except RelayError:
        return failed("unavailable", 1, "the launch step did not run: the relay refused the "
                      "launch task", started)
    deadline = time.monotonic() + LAUNCH_SECONDS
    while True:
        try:
            task = relay.get(task["id"])
        except RelayError:
            return failed("unavailable", 1, "the launch step did not run: the relay lost the "
                          "launch task", started)
        task_status = task.get("status") if task else None
        if task is None or task_status not in (None, "queued", "running"):
            break
        if time.monotonic() >= deadline:
            return failed("unavailable", 1, f"the launch task did not finish within "
                          f"{LAUNCH_SECONDS} seconds", started)
        time.sleep(0.25)
    if task is None:
        return failed("unavailable", 1, "the launch step did not run: the relay lost the "
                      "launch task", started)
    if task_status != "completed":
        return failed("provider_error", 1, f"the launch step failed: task {task_status}", started)
    latency = max(0, int((_now() - started).total_seconds() * 1000))
    if (task.get("output") or "").strip() != LAUNCH_PROMPT.rsplit(": ", 1)[-1]:
        return ({"ts": _stamp(_now()), "event": "launch", "tool": "relay",
                 "status": "unexpected_response", "exitCode": 0, "retries": 0,
                 "latencyMs": latency}, "the launch step ran but the worker did not reply `ok`")
    return ({"ts": _stamp(_now()), "event": "launch", "tool": "relay", "status": "ok",
             "exitCode": 0, "retries": 0, "latencyMs": latency}, None)


def check(store, config, worker_id, by, relay=None, repo=None, *, agent=False):
    if not isinstance(by, str) or not by.strip():
        raise ToolError("checking a worker needs the name of the person who asked")
    candidate = _candidate(config, worker_id)
    credential_metadata = _credential_metadata(candidate["credentials"])
    image_id = _image_id(candidate["image"])
    binding = _binding(candidate, credential_metadata, image_id)
    if image_id is None:
        report, cleanup_ok = ({"schemaVersion": HEALTH_SCHEMA, "status": "unavailable",
                              "finishedAt": _stamp(_now()), "events": []}, True)
    else:
        report, cleanup_ok = _run(candidate)
    launch_event, launch_reason = _launch(relay, config, repo or (
        next(iter(config.repos), None) if len(config.repos) == 1 else None), worker_id)
    if launch_event is not None:
        report = {**report, "events": [*report.get("events", []), launch_event],
                  "status": "unavailable" if launch_reason else report["status"]}
    status = "healthy" if report["status"] == "ok" and cleanup_ok and not launch_reason \
        else "quarantined"
    checked = _now()
    valid_until = checked + timedelta(seconds=HEALTH_TTL)
    report_json = json.dumps(report, sort_keys=True, separators=(",", ":"))
    store.put_worker_health(worker_id, binding, status, _stamp(checked), _stamp(valid_until), report_json)
    store.log("worker_health_checked", {"worker": worker_id, "status": status,
                                         "result": report["status"], "binding": binding,
                                         **({"reason": launch_reason} if launch_reason else {}),
                                         "by": by, **({"agent": True} if agent else {})})
    return {"worker": worker_id, "recordStatus": status, "checkedAt": _stamp(checked),
            "validUntil": _stamp(valid_until), "cleanup": "ok" if cleanup_ok else "unavailable",
            "health": report, "reason": launch_reason}


def reload(store, relay, by, *, agent=False):
    if not isinstance(by, str) or not by.strip():
        raise ToolError("reloading workers needs the name of the person who asked")
    try:
        relay.reload()
    except RelayError:
        store.log("worker_registry_reload", {"status": "unavailable", "by": by,
                                             **({"agent": True} if agent else {})})
        return {"status": "unavailable"}
    store.log("worker_registry_reload", {"status": "reloaded", "by": by,
                                         **({"agent": True} if agent else {})})
    return {"status": "reloaded"}


def current_binding(config, worker_id):
    candidate = _candidate(config, worker_id)
    image_id = _image_id(candidate["image"])
    if image_id is None:
        raise ToolError("worker image is unavailable; repair it and rerun factory_check_worker")
    return _binding(candidate, _credential_metadata(candidate["credentials"]), image_id)


def set_enabled(store, config, worker_id, enabled, by, note, *, agent=False, owner_asked=False):
    if owner_asked and not agent:
        raise ToolError("ownerAsked is Day Desk's flag; use the owner token directly without it")
    if (type(enabled) is not bool or not isinstance(by, str) or not by.strip()
            or not isinstance(note, str) or not note.strip() or len(note) > 1000):
        raise ToolError("worker activation needs enabled boolean, by, and a reason note up to 1000 characters")
    if not isinstance(worker_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}", worker_id):
        raise ToolError("worker must name a registered relay agent")
    binding = ""
    if enabled:
        candidate = _candidate(config, worker_id)
        # Routing's independent-review guard reads the id. Do not admit a misleading alias.
        if routing.family(worker_id) != routing.family(candidate["model"].split("/")[-1]):
            raise ToolError("worker id must name its pinned model family before activation")
        binding = current_binding(config, worker_id)
        health = store.worker_health(worker_id)
        now = _stamp(_now())
        if (not health or health["status"] != "healthy" or health["binding"] != binding
                or not health["checkedAt"] <= now < health["validUntil"]):
            raise ToolError("worker needs fresh successful health for this config/image; run factory_check_worker")
    elif (worker_id not in dict(config.workers)
          and worker_id not in {row["worker"] for row in store.worker_activations()}):
        _candidate(config, worker_id)  # disabling remains possible after an active registry entry breaks
    store.set_worker_activation(worker_id, enabled, binding,
                                {"worker": worker_id, "enabled": enabled, "binding": binding,
                                 "by": by, "note": note, "agent": agent, "ownerAsked": owner_asked})
    return {"worker": worker_id, "enabled": enabled}


def _honour(store, config, worker_id, row):
    """(binding, None) when an enabled activation row can be honoured, else (None, why not).

    The reason a person should read in factory_status or an escalation: the row is enabled, so
    "no local worker" is never the truth about why it does not route (ADR-0113)."""
    try:
        binding = current_binding(config, worker_id)
    except ToolError as exc:
        return None, f"{worker_id} is enabled but cannot be used: {exc}"
    health = store.worker_health(worker_id)
    if binding != row["binding"]:
        return None, (f"{worker_id} is enabled but its stored binding no longer matches this "
                      "config or image; run factory_check_worker")
    if not (health and health["status"] == "healthy" and health["binding"] == binding):
        return None, (f"{worker_id} is enabled but it has no fresh healthy check for this binding; "
                      "run factory_check_worker")
    return binding, None


def unusable_reasons(store, config):
    """Why each enabled activation row is missing from effective workers; [] when none fails."""
    reasons = []
    for row in store.worker_activations():
        if row["enabled"]:
            _, reason = _honour(store, config, row["worker"], row)
            if reason:
                reasons.append(reason)
    return reasons


def effective_workers(store, config):
    workers = dict(config.workers)
    for row in store.worker_activations():
        worker_id = row["worker"]
        workers.pop(worker_id, None)
        if row["enabled"]:
            binding, reason = _honour(store, config, worker_id, row)
            if reason is None:
                workers[worker_id] = dict(config.workers).get(worker_id, "hosted")
    # Retain the configured preference order, appending newly activated hosted workers.
    order = dict.fromkeys([*(name for name, _ in config.workers), *workers])
    return tuple((name, workers[name]) for name in order if name in workers)
