"""Which worker takes the next attempt (ADR-0011): locality first, then one that has not failed here.

ADR-0081 adds the recovery preference: after a timeout-class failure, the next attempt prefers a
worker from another model family, because the family that just ran out of the clock is the one
suspect. ADR-0085 adds the hard wall before it: no pass runs in the gate reviewer's model family
(`exclude_families` in `route`), because a pass built there answers its own reviewer. `family()`
reads the family from a worker id or a model id; neither rule widens the eligible set, they only
filter and order it.
"""
import re

# `<harness>-<model>-factory` worker ids group by `<model>`: `pi-glm-factory` and
# `opencode-glm-factory` are one family, `pi-deepseek-factory` another. An id that does not have
# that shape is its own family.
FAMILY = re.compile(r"^[^-]+-(.+)-factory$")


def family(name):
    """The model family a worker id or a model id names: `pi-glm-factory` and `opencode-glm-factory`
    give `glm`, and the gate's `deepseek-v4.1-flash` gives `deepseek` (ADR-0085), so a model id and
    a worker id of that model compare equal. An id that names no separator — `codex` — is its own
    family."""
    match = FAMILY.match(name or "")
    if match:
        return match.group(1)
    parts = (name or "").split("-")
    return parts[0] if len(parts) > 1 else (name or "")


def route(workers, local_only, failed, named=(), avoid_families=(), exclude_families=()):
    """workers: (agentId, "local"|"hosted") pairs in preference order. None if none may run.
    named: the agents an issue's `Worker:` lines choose, in order; then only they may run.
    exclude_families: a hard filter (ADR-0085) after the `Worker:` lines: no pass runs in these
    model families, whatever the order says. The gate reviews a PR from inside its own family, so
    a pass built there would answer its own reviewer.
    avoid_families: when non-empty, prefer the first eligible worker outside them (ADR-0081);
    when no eligible worker is outside them — or the preference is empty — the order is as
    before, so the fallback can only pick what the old rule picked."""
    if named:
        localities = dict(workers)
        workers = [(agent, localities[agent]) for agent in named if agent in localities]
    if exclude_families:
        workers = [row for row in workers if family(row[0]) not in exclude_families]
    allowed = [agent for agent, locality in workers if locality == "local" or not local_only]
    fresh = [agent for agent in allowed if agent not in failed]
    if avoid_families:
        preferred = [agent for agent in fresh if family(agent) not in avoid_families] or [
            agent for agent in allowed if family(agent) not in avoid_families]
        if preferred:
            return preferred[0]
    return (fresh or allowed or [None])[0]
