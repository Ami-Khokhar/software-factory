"""Ops numbers computed from the audit history: escalation, the autonomy rate, and whether the
lessons the owner approved are working."""
import json

from .workitem import UNGATED

HUMAN_STATES = ("human_waiting", "blocked")
REACHED_END = frozenset({"merged", "closed", "cancelled", "exhausted"})


def escalation(wi, history):
    """What an escalation tells the owner: the reason, the one decision needed, and what was tried."""
    if wi["state"] not in HUMAN_STATES:
        return None
    tried = list(wi.get("attempts") or []) or [
        f"{h['at']} {h['action']}: {json.dumps(h['detail'], sort_keys=True)}"
        for h in history if not h["action"].startswith("tool:")][-3:]
    return {"reason": wi.get("blockedOn") or wi["state"],
            "needFromYou": f"Decide {wi['id']}: call factory_resolve_human with retry (the factory "
                           "tries again), regate (you pushed a fix; re-run the gate) or close.",
            "owner": wi.get("owner"), "tried": tried}


def autonomy(workitems, history_of):
    """Merged with no human state and no human commit ÷ all WorkItems that reached an end state.

    A human commit is a PR head no worker Result produced. Merges by pr-gatekeeper are not touches;
    a merge without its MERGE verdict is.
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
        worker_shas = {h["detail"]["commitSha"] for h in history if h["action"] == "result"}
        heads = {h["detail"]["headSha"] for h in history
                 if h["action"] == "reconcile" and h["detail"].get("headSha")}
        touched = any(h["detail"].get("to") in HUMAN_STATES for h in history)
        ungated = (wi.get("terminalReason") or "").startswith(UNGATED)
        if wi["state"] in ("merged", "closed") and not touched and not ungated and heads <= worker_shas:
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
    for entry in lessons:
        fingerprint, key = entry["fingerprint"], f"{entry['repo'].lower()}:{entry['fingerprint']}"
        if key in effect:
            continue
        sent, recurred = set(), 0
        in_repo = (o for o in workorders_of() if o["repo"].lower() == entry["repo"].lower())
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


def _grouped(orders):
    """Stored WorkOrders grouped by WorkItem, in the order `orders` yields them."""
    groups = {}
    for order in orders:
        groups.setdefault(order["workItemId"], []).append(order)
    return groups
