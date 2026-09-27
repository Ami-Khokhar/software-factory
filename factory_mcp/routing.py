"""Which worker takes the next attempt (ADR-0011): locality first, then one that has not failed here."""


def route(workers, local_only, failed):
    """workers: (agentId, "local"|"hosted") pairs in preference order. None if none may run."""
    allowed = [agent for agent, locality in workers if locality == "local" or not local_only]
    fresh = [agent for agent in allowed if agent not in failed]
    return (fresh or allowed or [None])[0]
