"""Ops numbers computed from the audit history: escalation, the autonomy rate, whether the
lessons the owner approved are working, and the weekly report's numbers."""
import json
import re
from collections import Counter
from datetime import timedelta, timezone

from .workitem import UNGATED

HUMAN_STATES = ("human_waiting", "blocked")
ESCALATED = HUMAN_STATES + ("exhausted",)  # foreman.ATTENTION: entering one tells the owner
REACHED_END = frozenset({"merged", "closed", "cancelled", "exhausted"})
WEEK = timedelta(days=7)
TOP_REASONS = 3
SHA = re.compile(r"\b[0-9a-f]{40}\b")


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


def weekly(workitems, history_of, candidates, effect, now):
    """The weekly report's numbers for the 7 days before `now` (an aware datetime: the clock) and
    the 7 days before those. `candidates` are lessons.candidates of each allowlisted repo, each
    with its repo; `effect` is lesson_effect of the approved lessons."""
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
            "notWorking": [e for e in effect.values() if e["notWorking"]]}


def _stamp(moment):
    """An aware datetime in the audit log's format (store.now())."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _grouped(orders):
    """Stored WorkOrders grouped by WorkItem, in the order `orders` yields them."""
    groups = {}
    for order in orders:
        groups.setdefault(order["workItemId"], []).append(order)
    return groups
