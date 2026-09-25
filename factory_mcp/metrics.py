"""Ops numbers computed from the audit history: escalation and the autonomy rate."""
import json

from .workitem import UNGATED

HUMAN_STATES = ("human_waiting", "blocked")
REACHED_END = frozenset({"merged", "closed", "cancelled", "exhausted"})


def escalation(wi, history):
    """What OpenBot's job_blocked needs: the reason, the one action, and at least two tries."""
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
