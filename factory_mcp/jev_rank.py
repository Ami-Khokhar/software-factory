"""One opt-in Jev call: the repo's approved lessons ranked by their relevance to the task.

Off by default: the owner sets `FACTORY_JEV_LESSON_RANKING` and puts the key in
`TYPESAFE_API_KEY` (config reads both from the factory service environment, once, and
refuses to start with the flag on and the key missing). At dispatch time anything short
of a usable answer — the key missing, too many lessons, an oversized request, a timeout,
a bad HTTP status, an abstention, a malformed or unknown answer, or a top probability
below `MIN_PROBABILITY` — returns None: rank nothing, and the caller keeps the
newest-first selection. Every stop logs a fixed non-secret category on the WorkItem. A
good answer only reorders the lessons: the same entries come back, the winning lesson
first (eligibility and the cap stay code-owned, ADR-0020, ADR-0007). The confidence is
validated where it comes back, but the gate is the winner's probability: a Choice of n
options returns confidence (p_max - 1/n)/(1 - 1/n), so the 0.5 floor already implies one
near 0.25 at every option count this repo can produce.
"""
import json
import socket
import urllib.error
import urllib.request

HOST = "https://api.typesafe.ai/v1/systemone"  # one POST, one answer back (live HTTP API docs)
MODEL = "jev-latest"
TIMEOUT_SECONDS = 5  # bounded over the Foreman's reconcile; no retry loop in the tick
MAX_OPTIONS = 255  # the endpoint's documented limit for one Choice question
MIN_PROBABILITY = 0.5  # the winning lesson must hold most of the probability mass
MAX_BYTES = 60_000  # a conservative bound on the serialized request, checked before sending
ABSTAIN = ("none of the lessons applies to this work", "uncertain")

# The rubric stays in code. The request's state holds only this task: the goal, the acceptance
# bullets, this dispatch's findingsToAddress — never comments, command output, diffs or secrets.
INSTRUCTIONS = {
    "question": (
        "`query` holds the task: a goal the work must reach, the acceptance checks it, and the "
        "problems findingsToAddress say to resolve on the way (there when the WorkOrder has any). "
        "Each option below names one lesson; its description is that lesson's full text. Which "
        "ONE lesson is most useful for the worker to avoid repeating in THIS work? In plain "
        "meaning, for THIS task only."
    ),
    "method": (
        "Read the query as a whole, then each option's description. A lesson is most useful when "
        "its subject plausibly recurs in the code this task may write or change, in plain "
        "meaning (synonyms and paraphrases count; exact paths need not match), or when the "
        "findings repeat a problem like the lesson's. A lesson about other files, subsystems or "
        "work goes below any lesson this task could make relevant."
    ),
    "test": (
        "A lesson about an endpoint's error tests matters for endpoint work; a lint rule's "
        "lesson matters for any code change; a SQLite lesson for store queries; a README-only "
        "lesson for README work. Findings that name a subject count like work touching it."
    ),
}


def _state(wi, findings):
    state = {"goal": wi["goal"],
             "acceptance": [f"{c['claim']} — done when: {c['doneWhen']}"
                            for c in wi.get("acceptance") or []]}
    if findings:
        state["findingsToAddress"] = [f["problem"] for f in findings if f.get("problem")]
    return state


def _fallback(store, wi, category):
    """The one audit row a stop leaves: the category, and nothing from the call."""
    store.log("lesson_rank_fallback", {"category": category}, wi["id"])
    return None


def rank(lessons, wi, findings, store, config):
    """The eligible lessons ranked by relevance, or None: keep newest first.

    `lessons` are one repo's eligible entries from `lessons.for_repo`, newest first (ADR-0020:
    eligibility never depends on the answer); `findings` are the exact `findingsToAddress` of
    the WorkOrder built. Returns the same entries, the winning lesson first, or None. Never
    raises; never logs request or response text, the key, or a provider error body.
    """
    if not config.rank_lessons or not lessons:
        return None
    if not config.jev_api_key:  # a service that started before the key reached its environment
        return _fallback(store, wi, "no-key")
    if len(lessons) > MAX_OPTIONS - len(ABSTAIN):  # no silent cut: rank nothing at all
        return _fallback(store, wi, "too-many-lessons")
    # One Choice: every eligible lesson an option (its criterion is its full text), two abstentions.
    question = {"type": "choice", "instructions": INSTRUCTIONS,
                "criteria": {**{e["fingerprint"]: e["text"] for e in lessons},
                             **dict.fromkeys(ABSTAIN, None)}}
    body = {"model": MODEL, "state": {"query": _state(wi, findings)},
            "questions": {"relevant": question}}
    payload = json.dumps(body).encode()
    if len(payload) > MAX_BYTES:
        return _fallback(store, wi, "request-bound")
    try:
        request = urllib.request.Request(
            config.jev_url, payload,
            {"Content-Type": "application/json", "Authorization": f"Bearer {config.jev_api_key}"})
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200:
                return _fallback(store, wi, f"http-{response.status}")
            answer = json.loads(response.read().decode())["answers"]["relevant"]
    except urllib.error.HTTPError as exc:  # an error body may echo the request: never logged
        return _fallback(store, wi, f"http-{exc.code}")
    except socket.timeout:  # 3.9 spells the read timeout its own way; 3.10+: it is TimeoutError
        return _fallback(store, wi, "timeout")
    except OSError as exc:  # the endpoint is not there, or the connect timed out under URLError
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        return _fallback(store, wi, "timeout" if isinstance(reason, socket.timeout) else "unreachable")
    except (ValueError, KeyError, TypeError) as exc:  # bad JSON, or a shape rank() rejects
        return _fallback(store, wi, "bad-response")
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        return _fallback(store, wi, "bad-response")
    chosen = answer.get("choice")
    probabilities = answer.get("probabilities")
    confidence = answer.get("confidence")
    if not isinstance(chosen, str):  # the answer names a string; a list or object is malformed
        return _fallback(store, wi, "bad-response")
    if chosen in ABSTAIN:  # the model's own two ways of saying it cannot rank
        return _fallback(store, wi, "abstain")
    if chosen not in {e["fingerprint"] for e in lessons}:  # not a lesson this request named
        return _fallback(store, wi, "invalid-answer")
    # The answer's own shape: every probability a number in range, the confidence too (API docs).
    if not isinstance(probabilities, dict) or not isinstance(confidence, (int, float)) \
            or not 0 <= confidence <= 1 \
            or any(not isinstance(p, (int, float)) or not 0 <= p <= 1
                   for p in probabilities.values()):
        return _fallback(store, wi, "bad-response")
    if probabilities.get(chosen, 0) < MIN_PROBABILITY:
        return _fallback(store, wi, "low-confidence")
    order = list(enumerate(lessons))
    # A lesson the answer leaves out reads as 0; the newest-first order breaks every tie.
    return [e for _, e in sorted(order, key=lambda pair:
            (-probabilities.get(pair[1]["fingerprint"], 0.0), pair[0]))]
