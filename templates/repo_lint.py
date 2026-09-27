#!/usr/bin/env python3
"""Check one repository's agent docs for drift. Run it from the repo root:

    python3 templates/repo_lint.py [--limit N]

Three checks:

1. Every path in backticks in AGENTS.md (a span containing "/" or ending in
   a file extension) exists in the repository. A bare filename is also
   accepted when the repo holds a file of that name, because the docs write
   `foreman.py` next to its section for `factory_mcp/foreman.py`.
2. Every relative Markdown link in AGENTS.md, README.md and docs/*.md
   resolves, relative to the file that carries it.
3. Check every text file that git does not ignore. None is longer than the
   limit (default 800 lines). Files under vendor/ and node_modules/, and
   lock files, are skipped too.

Prints one `path:line: problem` line per problem and exits 1 when there is at
least one; prints nothing and exits 0 when the repository is clean.
Standard library only.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

LINE_LIMIT = 800
LINK_DOCS = ("AGENTS.md", "README.md")
SKIP_DIRS = {".git", "vendor", "node_modules"}
LOCK_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "uv.lock", "poetry.lock", "Cargo.lock", "Gemfile.lock", "composer.lock",
    "go.sum",
}
BACKTICK = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)")
EXTENSION = re.compile(r"\.[A-Za-z0-9]+$")

# What counts as "ends in a file extension" for the backtick check. Anything
# else after a dot (patches.apply) is a module reference, not a path.
FILE_EXTENSIONS = {
    ".py", ".md", ".json", ".yml", ".yaml", ".toml", ".sh", ".txt", ".cfg",
    ".ini", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".c", ".h", ".css",
    ".html", ".rst", ".csv", ".xml", ".sql", ".lock", ".env", ".in",
}


def _lines(path: str):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return []


def _git(root: str, args: list) -> str | None:
    """Joined output of `git <args>` in root; None when git cannot answer."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, check=False
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def _walk(root: str) -> list:
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            paths.append(rel.replace(os.sep, "/"))
    return sorted(paths)


def _skipped(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    return (
        any(part in SKIP_DIRS for part in parts)
        or name in LOCK_NAMES
        or name.endswith(".lock")
    )


def repo_files(root: str) -> list:
    """The relative paths the long-file check looks at.

    Tracked files plus untracked files git does not ignore; a directory
    that is not a git repository falls back to a walk.
    """
    tracked = _git(root, ["ls-files", "-z"])
    if tracked is None:
        return [rel for rel in _walk(root) if not _skipped(rel)]
    others = _git(root, ["ls-files", "--others", "--exclude-standard", "-z"]) or ""
    found = {
        rel for rel in (tracked + others).split("\0") if rel and not _skipped(rel)
    }
    return sorted(found)


def _ends_in_extension(span: str) -> bool:
    match = EXTENSION.search(span)
    return bool(match) and match.group(0).lower() in FILE_EXTENSIONS


def _looks_like_path(span: str) -> bool:
    if not span or any(char.isspace() for char in span):
        return False
    if span[0] in "/~" or "<" in span or ">" in span:
        return False
    if any(char in span for char in "*?[]{}"):
        return False
    if span.startswith(("http:", "https:", "mailto:")):
        return False
    return "/" in span or _ends_in_extension(span)


def _exists(root: str, span: str, files: list) -> bool:
    rel = span.rstrip("/")
    if "/" in span:
        return os.path.exists(os.path.join(root, rel))
    if os.path.exists(os.path.join(root, rel)):
        return True
    return rel in {os.path.basename(path) for path in files}


def path_problems(root: str) -> list:
    """Check 1: backticked paths in AGENTS.md."""
    agents = os.path.join(root, "AGENTS.md")
    if not os.path.isfile(agents):
        return []
    files = repo_files(root)
    problems = []
    for number, line in enumerate(_lines(agents), 1):
        for span in BACKTICK.findall(line):
            if _looks_like_path(span) and not _exists(root, span, files):
                problems.append(
                    "AGENTS.md:%d: path `%s` does not exist" % (number, span)
                )
    return problems


def link_problems(root: str) -> list:
    """Check 2: relative Markdown links in AGENTS.md, README.md, docs/*.md."""
    docs = [rel for rel in LINK_DOCS if os.path.isfile(os.path.join(root, rel))]
    docs_dir = os.path.join(root, "docs")
    if os.path.isdir(docs_dir):
        docs += sorted(
            "docs/" + name
            for name in os.listdir(docs_dir)
            if name.endswith(".md")
            and os.path.isfile(os.path.join(docs_dir, name))
        )
    problems = []
    for rel in docs:
        base = os.path.dirname(rel)
        for number, line in enumerate(_lines(os.path.join(root, rel)), 1):
            for target in LINK.findall(line):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                clean = target.split("#", 1)[0].split("?", 1)[0]
                if not clean:
                    continue
                resolved = os.path.normpath(os.path.join(root, base, clean))
                if not os.path.exists(resolved):
                    problems.append(
                        "%s:%d: broken link: %s" % (rel, number, target)
                    )
    return problems


def long_file_problems(root: str, limit: int) -> list:
    """Check 3: every text file git does not ignore, by line count."""
    problems = []
    for rel in repo_files(root):
        try:
            with open(os.path.join(root, rel), "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        if b"\0" in data:
            continue  # binary
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        count = len(text.splitlines())
        if count > limit:
            problems.append(
                "%s:1: file has %d lines, over the %d-line limit"
                % (rel, count, limit)
            )
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Check this repository's agent docs for drift."
    )
    parser.add_argument(
        "--limit", type=int, default=LINE_LIMIT,
        help="maximum lines for a text file git does not ignore (default %d)"
        % LINE_LIMIT,
    )
    args = parser.parse_args(argv)
    root = os.getcwd()
    problems = (
        path_problems(root)
        + link_problems(root)
        + long_file_problems(root, args.limit)
    )
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
