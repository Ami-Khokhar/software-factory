"""Ops numbers computed from the audit history: escalation, the autonomy rate, whether the
lessons the owner approved are working, and the weekly report's numbers."""
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import routing
from .foreman import OUT_OF_TIME
from .workitem import UNGATED

HUMAN_STATES = ("human_waiting", "blocked")
ESCALATED = HUMAN_STATES + ("exhausted",)  # foreman.ATTENTION: entering one tells the owner
REACHED_END = frozenset({"merged", "closed", "cancelled", "exhausted"})
WEEK = timedelta(days=7)
TOP_REASONS = 3
SHA = re.compile(r"\b[0-9a-f]{40}\b")
MOMENT = "%Y-%m-%dT%H:%M:%SZ"
IST = timezone(timedelta(hours=5, minutes=30))  # the owner reads windows in Asia/Kolkata, no DST


def escalation(wi, history):
    """What an escalation tells the owner: the reason, the one decision needed, and what was tried."""
    if wi["state"] not in HUMAN_STATES:
        return None
    tried = list(wi.get("attempts") or []) or [
        f"{h['at']} {h['action']}: {json.dumps(h['detail'], sort_keys=True)}"
        for h in history if not h["action"].startswith("tool:")][-3:]
    return {"reason": wi.get("blockedOn") or wi["state"],
            "needFromYou": f"Decide {wi['id']}: call factory_resolve_human with retry (the factory "
                           "tries again), regate (re-run the gate: on a fix you pushed, or on the same head "
                           "when the gate failed itself) or close.",
            "owner": wi.get("owner"), "tried": tried}


def autonomy(workitems, history_of):
    """Merged with no human state and no human commit ÷ all WorkItems that reached an end state.

    A human commit is a PR head that neither a worker Result nor the gate's merge of the base branch
    (ADR-0039) produced. Merges by pr-gatekeeper are not touches; a merge without its MERGE verdict is.
    A WorkItem rebuilt from GitHub lost the history that decides this, so it counts as unknown.
    """
    ended, unknown, autonomous = [], 0, 0
    for wi in workitems:
        if wi["state"] not in REACHED_END:
            continue
        history = history_of(wi["id"])
        if any(h["action"] == "rebuilt" for h in history):
            unknown += 1
            continue
        ended.append(wi)
        made = {h["detail"]["commitSha"] for h in history if h["action"] == "result"} | {
            h["detail"]["headSha"] for h in history if h["action"] == "reconcile" and h["detail"].get("mergedBase")}
        heads = {h["detail"]["headSha"] for h in history
                 if h["action"] == "reconcile" and h["detail"].get("headSha")}
        touched = any(h["detail"].get("to") in HUMAN_STATES for h in history)
        ungated = (wi.get("terminalReason") or "").startswith(UNGATED)
        if wi["state"] in ("merged", "closed") and not touched and not ungated and heads <= made:
            autonomous += 1
    return {"ended": len(ended), "autonomous": autonomous, "unknown": unknown,
            "rate": round(autonomous / len(ended), 3) if ended else None}


def lesson_effect(lessons, workorders_of):
    """For each lesson, the WorkItems that got it and how many still drew the same fingerprint
    from the gate: a recurrence means the lesson is not working yet (the owner retires or
    rewrites it; three or more is the flag).

    A lesson "recurred" on a WorkItem when a WorkOrder for a gate round after the first one that
    carried the lesson has the same fingerprint in findingsToAddress: the gate reported it on a
    head built after the lesson rode along. Every later WorkOrder carries the lesson too, so
    the first carrier is the line. `workorders_of()` yields every stored WorkOrder.
    Keyed "repo:fingerprint": a fingerprint names a path and a problem, not a repo.
    """
    effect = {}
    stored = list(workorders_of())  # one read of the store for every lesson
    for entry in lessons:
        fingerprint, key = entry["fingerprint"], f"{entry['repo'].lower()}:{entry['fingerprint']}"
        if key in effect:
            continue
        sent, recurred = set(), 0
        in_repo = (o for o in stored if o["repo"].lower() == entry["repo"].lower())
        for workitem_id, orders in sorted(_grouped(in_repo).items()):
            carried = [o for o in orders if any(l["fingerprint"] == fingerprint
                                                for l in o.get("lessons") or [])]
            if not carried:
                continue
            sent.add(workitem_id)
            first_round = min(o["round"] for o in carried)
            if any(o["round"] > first_round and any(f["fingerprint"] == fingerprint
                                                   for f in o.get("findingsToAddress") or [])
                   for o in orders):
                recurred += 1
        effect[key] = {"repo": entry["repo"], "fingerprint": fingerprint, "text": entry["text"],
                       "sent": len(sent), "recurred": recurred,
                       "workItems": sorted(sent), "notWorking": recurred >= 3}
    return effect


def ended_at(history):
    """When a WorkItem reached an end state: the time of the first audit entry that moved it into
    one, or None."""
    return next((h["at"] for h in history if h["detail"].get("to") in REACHED_END), None)


def ended_between(workitems, history_of, since, until):
    """The WorkItems that reached an end state after `since` and up to `until`. Times are audit
    timestamps (store.now()), which compare as text; the end is inclusive, so a report counts
    what was logged in its own second, and one week never shares an entry with the next."""
    ended = []
    for wi in workitems:
        at = ended_at(history_of(wi["id"])) if wi["state"] in REACHED_END else None
        if at is not None and since < at <= until:
            ended.append(wi)
    return ended


def escalation_reasons(workitems, history_of, since, until):
    """Why WorkItems went to human_waiting, blocked or exhausted after `since` and up to `until`,
    as {"reason", "count"}, most frequent first. The audit entry of each move records the reason
    as `escalation`; equal reasons are one group once commit shas are taken out of them."""
    counts = Counter()
    for wi in workitems:
        for h in history_of(wi["id"]):
            to = h["detail"].get("to")
            if to in ESCALATED and h["detail"].get("from") != to and since < h["at"] <= until:
                reason = h["detail"].get("escalation") or f"{to}, reason not recorded ({h['action']})"
                counts[SHA.sub("<sha>", reason)] += 1
    return [{"reason": reason, "count": count}
            for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def _moment(stamp):
    """An audit timestamp (store.now()) as an aware datetime."""
    return datetime.strptime(stamp, MOMENT).replace(tzinfo=timezone.utc)


def _ist_window(since, until):
    """The gauge window in the owner's time: both audit stamps as Asia/Kolkata wall clock."""
    return (f"{_moment(since).astimezone(IST).strftime('%Y-%m-%d %H:%M')} to "
            f"{_moment(until).astimezone(IST).strftime('%Y-%m-%d %H:%M')} IST")


def _first_verdict(history):
    """The first gate verdict a WorkItem's history shows, as (at, outcome, round), or None.
    Rows written before ADR-0083 carry no outcome: the state move infers it (merged was a MERGE,
    changes_requested a CHANGES, and a HUMAN says so in its escalation)."""
    known = []
    for h in history:
        if h["action"] != "reconcile":
            continue
        detail = h["detail"]
        if "verdict" in detail:
            known.append((h["at"], detail["verdict"], detail.get("round") or 1))
        elif detail.get("to") == "merged":
            known.append((h["at"], "MERGE", 1))
        elif detail.get("to") == "changes_requested":
            known.append((h["at"], "CHANGES", 1))
        elif detail.get("to") == "human_waiting" and "decided HUMAN" in (detail.get("escalation") or ""):
            known.append((h["at"], "HUMAN", 1))
    return min(known) if known else None


def first_pass(workitems, history_of, workorders_of, since, until, gate_pins):
    """The first-pass gauges (ADR-0083) for the window `since` < t <= `until`: first-verdict MERGE
    rate, clean one-shot rate, rounds to merge, accepted Results by pass kind and worker family,
    the timeout share of failed passes, and dependency-wait hours. Every gauge names its n, the IST
    window and the gate pin, so two windows under different pins or budgets never read as one
    trend. A pass is a dispatch row; it was accepted when a result row carries its WorkOrder."""
    orders = {order["id"]: order for order in workorders_of()}
    window, firsts, kinds, families = _ist_window(since, until), [], {}, {}
    clean = failed = timeouts = waits = 0
    wait_hours, rounds = 0.0, Counter()
    for wi in workitems:
        history = history_of(wi["id"])
        results = {h["detail"].get("workOrder") for h in history if h["action"] == "result"}
        first = _first_verdict(history)
        if first is not None and since < first[0] <= until:
            firsts.append(first[1])
            dispatches = [h for h in history if h["action"] == "dispatch"]
            if (first[1] == "MERGE" and wi["state"] == "merged" and len(dispatches) == 1
                    and not any(h["action"] == "attempt_failed" for h in history)
                    and not any(h["detail"].get("to") in HUMAN_STATES for h in history)
                    and dispatches[0]["detail"].get("workOrder") in results):
                clean += 1
        merges = [h for h in history if h["action"] == "reconcile"
                  and h["detail"].get("to") == "merged"]
        if merges and since < merges[0]["at"] <= until:
            verdicts = [h for h in history if h["action"] == "reconcile"
                        and h["detail"].get("verdict") == "MERGE"]
            rounds[(verdicts[-1]["detail"].get("round") if verdicts
                    else wi.get("gateRound")) or 1] += 1
        for h in history:
            if not since < h["at"] <= until:
                continue
            if h["action"] == "dispatch":
                order = orders.get(h["detail"].get("workOrder")) or {}
                accepted = h["detail"].get("workOrder") in results
                for bucket, key in ((kinds, order.get("kind") or "unknown"),
                                    (families, routing.family(h["detail"].get("worker")) or "unknown")):
                    bucket.setdefault(key, {"n": 0, "accepted": 0})
                    bucket[key]["n"] += 1
                    bucket[key]["accepted"] += 1 if accepted else 0
            elif h["action"] == "attempt_failed":
                failed += 1
                text = f"{h['detail'].get('reason') or ''} {h['detail'].get('diagnosis') or ''}"
                timeouts += 1 if OUT_OF_TIME.search(text) else 0
        started = None  # one dependency wait: the first `waiting` row to the `dependency_merged`
        for h in history:
            if h["action"] == "waiting":
                started = started or _moment(h["at"])
            elif h["action"] == "dependency_merged" and started is not None:
                wait_hours += (_moment(h["at"]) - started).total_seconds() / 3600
                waits += 1
                started = None
            elif h["action"] not in ("waiting", "dependency_merged"):
                started = None  # the wait ended by another route: not a dependency wait
    merge, known = sum(1 for outcome in firsts if outcome == "MERGE"), len(firsts)
    passes = sum(bucket["n"] for bucket in kinds.values())

    def gauge(n, **rest):
        return {"n": n, "window": window, "gatePin": gate_pins, **rest}

    return {"firstVerdictMerge": gauge(known, merge=merge,
                                       rate=round(merge / known, 3) if known else None),
            "oneShot": gauge(known, clean=clean, rate=round(clean / known, 3) if known else None),
            "roundsToMerge": gauge(sum(rounds.values()),
                                   byRound={str(round_): count
                                            for round_, count in sorted(rounds.items())}),
            "acceptedByKind": gauge(passes, kinds=kinds),
            "acceptedByFamily": gauge(passes, families=families),
            "timeouts": gauge(failed, timeouts=timeouts,
                              share=round(timeouts / failed, 3) if failed else None),
            "dependencyWait": gauge(waits, hours=round(wait_hours, 1))}


def weekly(workitems, history_of, candidates, effect, now, workorders_of=None, gate_pins=None):
    """The weekly report's numbers for the 7 days before `now` (an aware datetime: the clock) and
    the 7 days before those. `candidates` are lessons.candidates of each allowlisted repo, each
    with its repo; `effect` is lesson_effect of the approved lessons; `workorders_of` and
    `gate_pins` stamp the first-pass gauges (ADR-0083)."""
    until, since, before = (_stamp(now - WEEK * weeks) for weeks in (0, 1, 2))
    reasons = escalation_reasons(workitems, history_of, since, until)
    allowances = sum(1 for wi in workitems for h in history_of(wi["id"])
                     if h["action"] == "allowance_spent" and since < h["at"] <= until)
    allowances_before = sum(1 for wi in workitems for h in history_of(wi["id"])
                            if h["action"] == "allowance_spent" and before < h["at"] <= since)
    return {"since": since, "until": until,
            "autonomy": autonomy(ended_between(workitems, history_of, since, until), history_of),
            "autonomyWeekBefore": autonomy(ended_between(workitems, history_of, before, since),
                                           history_of),
            "escalated": sum(group["count"] for group in reasons),
            "topReasons": reasons[:TOP_REASONS],
            "allowanceUses": allowances, "allowanceUsesWeekBefore": allowances_before,
            "newCandidates": [c for c in candidates if since < c["candidateSince"] <= until],
            "notWorking": [e for e in effect.values() if e["notWorking"]],
            "gauges": first_pass(workitems, history_of, workorders_of or (lambda: []),
                                 since, until, gate_pins or {})}


def _stamp(moment):
    """An aware datetime in the audit log's format (store.now())."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _grouped(orders):
    """Stored WorkOrders grouped by WorkItem, in the order `orders` yields them."""
    groups = {}
    for order in orders:
        groups.setdefault(order["workItemId"], []).append(order)
    return groups
