"""What a failed attempt means, read from the Result's optional diagnostics (consumer of #189).

The Foreman logs every attempt that failed with a cause it can defend: a phase that failed in
the Result's own diagnostics, a patch missing from a completed Result, or the worker's own test
evidence. Whatever the reason strings say, a timeout with no supporting diagnostics stays an
unknown cause, so the blocked guidance never equates a known timeout with too much scope (#190).

Text that rides with the cause — into the audit, the escalation and the next WorkOrder — is
sanitized first: one bounded line with anything that looks like a credential's contents, an
environment value, or the WorkOrder's own command payload taken out. The worker's producer may
redact too; this is the controller's second line.
"""
import re

CAUSES = ("checkout or setup", "harness timeout", "empty patch", "failing test")
# What one attempt's diagnosis may add to an attempt entry, an escalation, or the next WorkOrder.
MAX_CONTEXT = 300
# A harness phase that ran at least this share of the WorkOrder's wall clock is one that ran out
# of its time, not one that failed early of something else. Exit 124 is the runner's own kill for
# time, so it names the timeout whatever the clock says; the runner's share of the wall is always
# under this share, which it can satisfy only through its exit code.
TIME_SHARE = 0.9
TIMEOUT_EXIT = 124
SETUP_PHASES = ("checkout", "preparation", "environment setup")
# The one cause the next pass must still see after a later pass with no actionable diagnosis: a
# failing test names code to fix, and an intervening timeout must not bury it (#271).
ACTIONABLE = "failing test"
PREVIOUS_FINGERPRINT = "previous-attempt"

# An environment value as an exporter writes it: a name in caps, then = and the value. Lowercase
# word=word, as "(failures=2)" reads in a test tail, is not a value to hide.
_ENV_VALUE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})=\S+")
_CREDENTIAL = re.compile(
    r"(?i)\b(access|auth)?[-_ ]?(token|password|passwd|passphrase|secret|bearer|authorization|"
    r"credential|api[-_ ]?key|private[-_ ]?key)s?(\s*[=:]\s*|\s+)\S+")
_TOKEN = re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9]{6,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"
                    r"|\bAKIA[0-9A-Z]{16}\b|\bBearer\s+\S+")
_WORKORDER_PAYLOAD = re.compile(r"(?s)\{.*?factory\.workorder/v1.*?\}")


def sanitize(text, limit=MAX_CONTEXT):
    """One bounded line for the audit and the WorkOrder: whitespace collapsed, credentials,
    environment values and the WorkOrder's command payload redacted."""
    line = _WORKORDER_PAYLOAD.sub(" ", " ".join((text or "").split()))
    line = _ENV_VALUE.sub(r"\1=<redacted>", line)
    line = _CREDENTIAL.sub(r"\g<2>=<redacted>", line)
    return _TOKEN.sub("<redacted>", line)[-limit:]


def diagnose(order, result):
    """The cause of a failed attempt, as {"cause": name or None, "context": sanitized text}.

    From the Foreman's own facts and the Result's optional diagnostics, whichever spoke first:
    a completed Result without a patch is an empty patch; a setup or verification phase with a
    failing exit code names itself; a harness execution phase names a timeout when it exited 124,
    the runner's own kill for time, or ran most of the WorkOrder's wall clock; a test evidence
    entry with a failing exit is a failing test. No diagnostics, no cause: a timeout suggested by
    the reason strings alone stays unknown.
    """
    if isinstance(result, dict):
        if result.get("status") == "completed" and not isinstance(result.get("patch"), dict):
            return {"cause": "empty patch", "context": ""}
        diagnostics = result.get("diagnostics")
        phases = diagnostics.get("phases") if isinstance(diagnostics, dict) else None
        for phase in phases if isinstance(phases, list) else ():
            # Only the shapes the Result check would have accepted name a cause; this diagnose also
            # runs on the Results that check rejected (foreman.poll), which must fail, not raise.
            if not isinstance(phase, dict) or not isinstance(phase.get("phase"), str):
                continue
            exit_code = phase.get("exitCode")
            if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code == 0:
                continue
            tail = phase.get("tail", "")
            if not isinstance(tail, str):
                tail = ""
            if phase["phase"] in SETUP_PHASES:
                return {"cause": "checkout or setup",
                        "context": sanitize(f"exit {exit_code}: {tail}")}
            if phase["phase"] == "verification":
                return {"cause": "failing test",
                        "context": sanitize(f"exit {exit_code}: {tail}")}
            elapsed = phase.get("elapsedMs", 0)
            if (phase["phase"] == "harness execution"
                    and (exit_code == TIMEOUT_EXIT
                         or (isinstance(elapsed, int) and not isinstance(elapsed, bool)
                             and elapsed >= 0 and elapsed >= TIME_SHARE * order["budget"]["wallMs"]))):
                return {"cause": "harness timeout",
                        "context": sanitize(f"exit {exit_code}: {tail}")}
        evidence = result.get("evidence")
        for entry_ in evidence if isinstance(evidence, list) else ():
            if not isinstance(entry_, dict) or entry_.get("kind") != "test":
                continue
            test_exit = entry_.get("exit")
            if not isinstance(test_exit, int) or isinstance(test_exit, bool) or test_exit == 0:
                continue  # an entry with no exit, or a bool one, is not evidence of a failing test
            tail = entry_.get("tail") if isinstance(entry_.get("tail"), str) else ""
            return {"cause": "failing test",
                    "context": sanitize(f"exit {test_exit}: {tail}")}
        if isinstance(diagnostics, dict) and isinstance(diagnostics.get("tail"), str):  # no nameable cause, but a tail may still help
            return {"cause": None, "context": sanitize(diagnostics["tail"])}
        return {"cause": None, "context": ""}
    return {"cause": None, "context": ""}


def attempt_findings(attempts):
    """The `findingsToAddress` entries a retry carries: the latest attempt, and when that attempt is
    not an actionable failing-test finding, the most recent earlier one that is, so an intervening
    empty timeout still leaves the test failures a later pass has to fix. Chronological and bounded
    by the attempts themselves (#271)."""
    entries = [a for a in (attempts or []) if isinstance(a, str) and a]
    if not entries:
        return []
    latest = entries[-1]
    findings = [{"fingerprint": PREVIOUS_FINGERPRINT, "problem": latest}]
    marker = f"diagnosis: {ACTIONABLE}:"
    if marker not in latest:
        carried = next((a for a in reversed(entries[:-1]) if marker in a), None)
        if carried and carried != latest:
            findings.insert(0, {"fingerprint": PREVIOUS_FINGERPRINT, "problem": carried})
    return findings


def environment_fault(cause):
    """Whether a cause names the environment, not the work (ADR-0051): a setup or checkout phase the
    harness's own diagnostics reported as failed. A failing test is the work, an empty patch is the
    worker's output, and a harness timeout — a phase that only ran out of the clock — is not evidence
    against the environment; those keep the normal retry.
    """
    return cause == "checkout or setup"


def entry(reason, diagnosis):
    """The attempt text the WorkItem and the next WorkOrder carry: the reason plus the sanitized
    diagnosis, or the reason alone when there is nothing to say."""
    context = diagnosis.get("context") or ""
    if diagnosis.get("cause"):
        text = f"{diagnosis['cause']}: {context}" if context else diagnosis["cause"]
    elif context:
        text = f"unknown cause, diagnostics tail: {context}"
    else:
        return reason
    return f"{reason}; diagnosis: {text}" if reason else f"diagnosis: {text}"
