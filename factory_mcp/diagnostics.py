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
# of its time, not one that failed early of something else.
TIME_SHARE = 0.9
SETUP_PHASES = ("checkout", "preparation", "environment setup")

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
    return _TOKEN.sub("<redacted>", line)[:limit]


def diagnose(order, result):
    """The cause of a failed attempt, as {"cause": name or None, "context": sanitized text}.

    From the Foreman's own facts and the Result's optional diagnostics, whichever spoke first:
    a completed Result without a patch is an empty patch; a setup or verification phase with a
    failing exit code names itself; a harness execution phase only names a timeout when it ran
    most of the WorkOrder's wall clock; a test evidence entry with a failing exit is a failing
    test. No diagnostics, no cause: a timeout suggested by the reason strings alone stays unknown.
    """
    if isinstance(result, dict):
        if result.get("status") == "completed" and not isinstance(result.get("patch"), dict):
            return {"cause": "empty patch", "context": ""}
        phases = ((result.get("diagnostics") or {}).get("phases") or []) \
            if isinstance(result.get("diagnostics"), dict) else []
        for phase in phases:
            exit_code = phase.get("exitCode")
            if not isinstance(exit_code, int) or exit_code == 0:
                continue
            if phase["phase"] in SETUP_PHASES:
                return {"cause": "checkout or setup",
                        "context": sanitize(f"exit {exit_code}: {phase.get('tail', '')}")}
            if phase["phase"] == "verification":
                return {"cause": "failing test",
                        "context": sanitize(f"exit {exit_code}: {phase.get('tail', '')}")}
            if (phase["phase"] == "harness execution"
                    and phase.get("elapsedMs", 0) >= TIME_SHARE * order["budget"]["wallMs"]):
                return {"cause": "harness timeout",
                        "context": sanitize(f"exit {exit_code}: {phase.get('tail', '')}")}
        for evidence in result.get("evidence") or []:
            if isinstance(evidence, dict) and evidence.get("kind") == "test" \
                    and evidence.get("exit") != 0:
                return {"cause": "failing test",
                        "context": sanitize(f"exit {evidence.get('exit')}: "
                                            f"{evidence.get('tail') or ''}")}
        if isinstance(result.get("diagnostics"), dict):  # no nameable cause, but a tail may still help
            return {"cause": None, "context": sanitize(result["diagnostics"].get("tail", ""))}
        return {"cause": None, "context": ""}
    return {"cause": None, "context": ""}


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
