#!/usr/bin/env python3
"""Provider health pilot: one short synthetic (non-WorkOrder) request through the sandboxed worker path.

    provider_health.py [--image IMAGE] [--deadline SECONDS] [--expect TEXT]
                       [--credential HOST_FILE:CONTAINER_DIR ...] [--env KEY=VALUE ...] --
                       <harness argv...>

This is the pilot behind ADR-0076. It proves, before any production WorkOrder is dispatched:

- the agent runs the same restricted worker path as every factory worker (the runner's preflight
  and `docker run` sandbox, credential copies only, no host shell); the harness never sees a host
  file the runner would not mount;
- the intended model answers: the probe prompt is synthetic and tiny, the response is compared
  against `--expect` (default `ok`), and latency, exit status and byte counts are recorded;
- an unavailable access boundary is reported honestly (its exit code + `unavailable`), never
  bypassed through the host shell and never restyled into a pass.

Privacy: the raw prompt, response and harness output are never printed or archived. The report is
`factory.health/v1`, a JSON object whose events carry only the sanitizer's allowlisted fields
(timestamp, event type, tool name, status, exit code, retry count, byte counts). A provider error is
classified (quota, rate limit, auth) from words in the harness output without that output spilling;
a deadline boundary (exit 124) is reported as `harness_deadline`, distinct from every provider
error, because pi print mode buffers the final response until completion and empty stdout is not
inactivity. The path after a deadline says exactly that, and nothing about credits.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import re
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "worker"))
import runner  # noqa: E402  # the same sandbox machinery the factory workers run (ADR-0076)

SCHEMA = "factory.health/v1"
PROBE = "Health probe: reply with exactly: ok"
# One attempt, no retries: the pilot never re-sends, so a retry count above 0 cannot occur.
RETRIES = 0
# The only fields a report event may carry, the only event types and statuses it may name, and the
# characters a `tool` name may hold. The sanitizer drops or renames everything else, so no raw
# text, path or payload can pass.
ALLOWED_FIELDS = ("ts", "event", "tool", "status", "exitCode", "retries",
                  "latencyMs", "promptBytes", "responseBytes", "diagnosticBytes")
EVENTS = {"preflight", "probe"}
STATUSES = {"ok", "unexpected_response", "harness_deadline", "provider_quota",
            "provider_rate_limit", "provider_auth", "provider_error", "unavailable"}
# Words that pick one distinct provider cause out of the sandbox text. The matched text itself is
# never kept: only the class name survives the sanitizer.
RATE_LIMIT_WORDS = ("429", "rate limit", "too many requests")
QUOTA_WORDS = ("402", "quota", "insufficient balance", "insufficient credit")
AUTH_WORDS = ("401", "403", "unauthorized", "invalid api key", "authentication failed")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify(exit_code, sandbox_text):
    """One cause for a probe outcome. A deadline is checked first: exit 124 means the sandbox's
    `timeout` ended the harness, which is not evidence of a quota, auth or rate problem at all."""
    if exit_code == 124:
        return "harness_deadline"
    if exit_code == 0:
        return "ok"
    lowered = sandbox_text.lower()
    if any(word in lowered for word in RATE_LIMIT_WORDS):
        return "provider_rate_limit"
    if any(word in lowered for word in QUOTA_WORDS):
        return "provider_quota"
    if any(word in lowered for word in AUTH_WORDS):
        return "provider_auth"
    return "provider_error"


def sanitize(event):
    """Keeps only the allowlisted fields, checks named values against their enums, and reduces the
    tool name to the safe characters it may hold. Nothing else survives, by construction."""
    clean = {}
    for field in ALLOWED_FIELDS:
        if field not in event:
            continue
        value = event[field]
        if field == "ts":
            clean[field] = str(value)
        elif field in ("event", "status"):
            allowed = EVENTS if field == "event" else STATUSES
            clean[field] = value if value in allowed else "unknown"
        elif field == "tool":
            name = str(value)
            clean[field] = name if name and all(c.isalnum() or c in "._-" for c in name) else "unnamed"
        else:
            clean[field] = value if isinstance(value, int) and not isinstance(value, bool) else 0
    return clean


def report(events, status):
    """The factory.health/v1 object, with `events` sanitized. Never carries the prompt, the
    response or any harness text: the values above are counts, names and enum members."""
    return json.dumps({"schemaVersion": SCHEMA, "status": status, "finishedAt": now(),
                       "events": [sanitize(event) for event in events]})


def probe(sandbox, argv, seconds, scratch, expect):
    """One synthetic request through the restricted sandbox. Returns (event dict, status)."""
    started = time.monotonic()
    try:
        # the argv a real pass runs (ADR-0111), so a green probe cannot pass a harness the pass fails
        code, out, err = sandbox.run([*runner.harness_command(argv), PROBE], seconds, scratch,
                                    harness=True)
    except runner.WorkerError as exc:
        # Docker could not start the sandbox for the harness phase: unavailable, honestly reported.
        status = "unavailable"
        code, out, err = (exc.code or 1), "", f"docker could not be reached ({exc.code})"
    except OSError as exc:
        status, code, out, err = "unavailable", 69, "", f"docker is not on PATH ({exc})"
    else:
        status = classify(code, out + err)
    if status == "ok" and (out or "").strip() != expect:
        status = "unexpected_response"
    latency = int((time.monotonic() - started) * 1000)
    event = {"ts": now(), "event": "probe", "tool": argv[0], "status": status, "exitCode": code,
             "retries": RETRIES, "latencyMs": latency, "promptBytes": len(PROBE.encode()),
             "responseBytes": len((out or "").encode()), "diagnosticBytes": len((err or "").encode())}
    return event, status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", default=runner.IMAGE)
    parser.add_argument("--deadline", type=int, default=300,
                        help="sandbox seconds for the one probe (default 300)")
    parser.add_argument("--expect", default="ok",
                        help="the exact response that proves the intended model answered")
    parser.add_argument("--credential", action="append", default=[], metavar="HOST_FILE:CONTAINER_DIR")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--probe-id", default="health",
                        help=argparse.SUPPRESS)
    parser.add_argument("harness", nargs="*", help="the sandboxed harness argv, after --")
    args = parser.parse_args(argv)
    if not args.harness:
        parser.error("give the sandboxed harness argv after --")
    if not re.fullmatch(r"[a-f0-9]{32}|health", args.probe_id):
        parser.error("probe id must be a fixed health name or 32 lowercase hex digits")
    scratch_root = os.environ.get("FACTORY_HEALTH_SCRATCH_ROOT")
    if scratch_root and not os.path.isdir(scratch_root):
        parser.error("the private probe scratch root is unavailable")
    scratch = tempfile.mkdtemp(prefix="factory-health-", dir=scratch_root)
    work = tempfile.mkdtemp(prefix="factory-health-work-", dir=scratch_root)
    try:
        return pilot(args, scratch, work)
    finally:
        for path in (scratch, work):
            shutil.rmtree(path, ignore_errors=True)


def pilot(args, scratch, work):
    tool = args.harness[0]
    events, status = [], None
    preflight = None
    try:
        # The pilot sends no WorkOrder; remoteVerify stands in for the base-commit verify check the
        # real preflight runs against a checked-in base.
        order = {"id": "provider-health-pilot", "remoteVerify": True}
        credentials, harness_env = runner.preflight(order, args.image, args.harness,
                                                   args.credential, args.env, scratch, work, None,
                                                   start_docker=False)
        preflight = {"ts": now(), "event": "preflight", "tool": tool, "status": "ok",
                     "exitCode": 0, "retries": RETRIES}
    except runner.WorkerError as exc:
        status = "unavailable"
        preflight = {"ts": now(), "event": "preflight", "tool": tool, "status": "unavailable",
                     "exitCode": exc.code if exc.code else 1, "retries": RETRIES}
    events.append(preflight)
    if status == "unavailable":
        print(report(events, status))
        return preflight["exitCode"] or 1
    sandbox = runner.Sandbox(args.image, f"health-{args.probe_id}", credentials, harness_env)
    event, status = probe(sandbox, args.harness, args.deadline, scratch, args.expect)
    events.append(event)
    print(report(events, status))
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
