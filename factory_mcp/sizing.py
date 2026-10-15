"""One metric for changed-code size: source lines, with tests and docs reported separately.

The small-PR target and the reviewable ceiling count only the source a change touches. Tests and
docs are retained evidence, not size: a pull request that carries many test and doc lines is still
reviewable when its source is small. Both worker/runner.py (which runs standalone in the worker
image) and factory-mcp measure with this module, so the early check, the push check and the gate
policy agree on one number.
"""
import re

# The owner's sizing: 700 source lines is the target, 1000 the reviewable ceiling. Over the
# ceiling is not a failed attempt: the factory proposes an acceptance-preserving decomposition.
SOURCE_TARGET = 700
SOURCE_CEILING = 1000
# A stable marker in the runner's and the pusher's refusal, so the Foreman can tell a size-only
# excess from a scope, budget, access or security finding without relabelling it.
SIZE_CEILING_MARKER = "source-size-ceiling"
SIZE_OK, SIZE_ADVISORY, SIZE_CEILING, SIZE_REFUSED = "ok", "advisory", "ceiling", "refused"


def source_spec(limits):
    """The source-only sizing spec a WorkOrder carries, or None for an older all-lines one.

    The ceiling defaults to the reviewable `SOURCE_CEILING`, not the target: the target is
    advisory, so treating it as the ceiling would turn a target+1 into a size-only recovery. A
    WorkOrder with no source target keeps the older all-lines meaning, so this returns None."""
    target = limits.get("maxSourceChangedLines")
    if target is None:
        return None
    return {"target": target,
            "ceiling": limits.get("maxSourceChangedLinesCeiling", SOURCE_CEILING),
            "advisory": limits.get("maxSourceChangedLinesAdvisory", True)}


def boundary(source, spec):
    """Where a source total sits against a spec ``{"target", "ceiling", "advisory"}``.

    ``SIZE_CEILING`` over the reviewable ceiling (size-only, routed to decomposition),
    ``SIZE_REFUSED`` over a target that is not advisory, ``SIZE_ADVISORY`` over an advisory target,
    and ``SIZE_OK`` at or under the target. Tests and docs are not passed here: the caller reports
    them separately."""
    if source > spec["ceiling"]:
        return SIZE_CEILING
    if source > spec["target"]:
        return SIZE_ADVISORY if spec.get("advisory", True) else SIZE_REFUSED
    return SIZE_OK

_DOC_SUFFIXES = (".md", ".rst", ".adoc", ".mdx")


def kind(path):
    """`source`, `tests` or `docs` for a repository path.

    Docs are markdown and reStructuredText files and anything under ``docs/``. Tests live under
    ``tests/`` or a ``test``/``tests`` directory and in ``test_*``/``*_test`` files. Everything
    else is source, including build files and scripts a change has to touch.
    """
    parts = (path or "").split("/")
    name = parts[-1] if parts else ""
    lowered = [part.lower() for part in parts[:-1]]
    if parts and parts[0].lower() == "docs" or name.lower().endswith(_DOC_SUFFIXES):
        return "docs"
    if (lowered and any(part in ("tests", "test") for part in lowered)
            or name.lower().startswith("test_") or name.lower().endswith("_test.py")):
        return "tests"
    return "source"


def measure(files):
    """Totals for an iterable of ``(path, additions, deletions)``.

    Returns ``{"source", "tests", "docs", "total", "files"}``. ``total`` is the all-lines sum the
    gate has always counted; the target and ceiling are compared against ``source``.
    """
    totals = {"source": 0, "tests": 0, "docs": 0}
    count = 0
    for path, added, deleted in files:
        count += 1
        totals[kind(path)] += (added or 0) + (deleted or 0)
    totals["total"] = totals["source"] + totals["tests"] + totals["docs"]
    totals["files"] = count
    return totals


def parse_numstat(text):
    """``(path, additions, deletions)`` from ``git diff --numstat`` output. Binary lines (`-`) are
    a zero-line change and keep their path, so the file count is right."""
    files = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        try:
            files.append((path, int(added), int(deleted)))
        except ValueError:
            files.append((path, 0, 0))
    return files


def parse_numstat_marker(text, marker="FACTORY_PR_NUMSTAT: "):
    """The numstat records the worker's extract printed with ``marker`` on stderr."""
    lines = [line[len(marker):] for line in (text or "").splitlines() if line.startswith(marker)]
    return parse_numstat("\n".join(lines))


def is_ceiling_error(text):
    """Whether an error is the size-only ceiling refusal, and no other cause claims the text."""
    return SIZE_CEILING_MARKER in (text or "")
