"""The git work factory-mcp does itself: clone a worker checkout, and turn a patch Result into a
pushed commit on the WorkItem branch."""
import base64
import os
import subprocess
import sys
import tempfile

from . import sizing


class PatchError(Exception):
    pass


def _git(args, cwd, env, timeout=120):
    try:
        done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise PatchError(f"git {args[0]} timed out") from None
    if done.returncode != 0:
        raise PatchError(f"git {args[0]} failed: {(done.stderr or done.stdout).strip()[-300:]}")
    return done.stdout.strip()


def is_protected(name, protected_paths):
    """True if a repo path is a protected path or under one (ADR-0125): a protected name matches a run
    of whole path segments anywhere in the path, so worker/.env, worker/secrets/key and
    .github/workflows/ci.yml are refused, and docs/secrets.md is not."""
    parts = name.split("/")
    for p in protected_paths:
        stems = [s for s in p.split("/") if s]
        if any(parts[i:i + len(stems)] == stems for i in range(len(parts) - len(stems) + 1)):
            return True
    return False


def _env(token):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="factory-mcp",
               GIT_AUTHOR_EMAIL="factory-mcp@users.noreply.github.com",
               GIT_COMMITTER_NAME="factory-mcp", GIT_COMMITTER_EMAIL="factory-mcp@users.noreply.github.com")
    if token:  # in the environment, not argv or .git/config, so it is not visible or kept
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraheader",
                   GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}")
    return env


def clone(url, token, target):
    """Clone the worker checkout for a repo at `target`."""
    os.makedirs(os.path.dirname(target), exist_ok=True)
    _git(["clone", "-q", url, target], os.path.dirname(target), _env(token), timeout=900)


def _bootstrap_paths(files):
    if not isinstance(files, dict) or not files:
        raise PatchError("bootstrap files must be a non-empty mapping")
    paths = []
    for name, text in files.items():
        if not isinstance(name, str) or not isinstance(text, str):
            raise PatchError("bootstrap files must have string paths and contents")
        parts = name.split("/")
        if (not name or name.startswith("/") or parts[0] == ".git"
                or any(part in ("", ".", "..") for part in parts)):
            raise PatchError(f"unsafe bootstrap path {name!r}")
        paths.append(name)
    for path in paths:
        if any(other.startswith(path + "/") for other in paths if other != path):
            raise PatchError(f"bootstrap paths collide at {path}")
    return paths


def _write_bootstrap_file(work, name, text):
    """Write one checked repository path without following a symlink in its ancestry."""
    current = work
    for part in name.split("/")[:-1]:
        current = os.path.join(current, part)
        if os.path.lexists(current):
            if os.path.islink(current):
                raise PatchError(f"bootstrap path crosses symlink {name}")
            if not os.path.isdir(current):
                raise PatchError(f"bootstrap path parent is not a directory {name}")
        else:
            os.mkdir(current)
    target = os.path.join(work, name)
    if os.path.islink(target):
        raise PatchError(f"bootstrap path is a symlink {name}")
    if os.path.isdir(target):
        raise PatchError(f"bootstrap path is a directory {name}")
    with open(target, "w") as handle:
        handle.write(text)


def _remote_branch_exists(work, env, branch):
    try:
        done = subprocess.run(["git", "ls-remote", "--exit-code", "--heads", "origin",
                               f"refs/heads/{branch}"], cwd=work, env=env, capture_output=True, text=True,
                              timeout=120)
    except subprocess.TimeoutExpired:
        raise PatchError("git ls-remote timed out") from None
    if done.returncode == 0:
        return True
    if done.returncode == 2:
        return False
    raise PatchError(f"git ls-remote failed: {(done.stderr or done.stdout).strip()[-300:]}")


def _changed_paths(args, work, env):
    return [name for name in _git(args, work, env).split("\0") if name]


def initial_commit(url, token, branch, files, message):
    """Push the first commit of an empty repo to ``branch``, holding only ``files``.

    A PR needs a base commit, so an empty repo gets this one first. It is pushed only when the
    remote has no refs at all, and the push never replaces a branch that appeared since.
    """
    paths = _bootstrap_paths(files)
    env = _env(token)
    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, "repo")
        _git(["init", "-q", work], tmp, env)
        _git(["remote", "add", "origin", url], work, env)
        if _git(["ls-remote", "origin"], work, env):
            raise PatchError("the repo is not empty: it has refs but no default branch head, "
                             "so no initial commit was pushed; a person must inspect it")
        _git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], work, env)
        for name in paths:
            _write_bootstrap_file(work, name, files[name])
        _git(["add", "--", *paths], work, env)
        _git(["-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", message], work, env)
        try:  # not forced: a branch that got a commit since the check rejects this push
            _git(["push", "-q", "origin", f"HEAD:refs/heads/{branch}"], work, env)
        except PatchError as exc:
            raise PatchError(f"the initial commit was not pushed to {branch}; the repo may have got a "
                             f"commit since the check, so nothing more was pushed ({exc})") from None
        return _git(["rev-parse", "HEAD"], work, env)


def bootstrap(url, token, branch, base_sha, files, message):
    """Push supplied bootstrap files on a new branch rooted at ``base_sha``.

    This path deliberately does not share ``apply``: bootstrap is the one human-reviewed setup
    change allowed to touch protected files, and it never replaces an existing remote branch.
    """
    paths = _bootstrap_paths(files)
    env = _env(token)
    with tempfile.TemporaryDirectory() as tmp:
        _git(["clone", "-q", "--no-checkout", url, "repo"], tmp, env)
        work = os.path.join(tmp, "repo")
        if _remote_branch_exists(work, env, branch):
            raise PatchError(f"bootstrap branch {branch} already exists")
        _git(["checkout", "-q", "-b", branch, base_sha], work, env)
        for name in paths:
            _write_bootstrap_file(work, name, files[name])
        _git(["add", "--", *paths], work, env)
        staged = _changed_paths(["diff", "--cached", "--name-only", "--no-renames", "-z"], work, env)
        if set(staged) != set(paths) or len(staged) != len(paths):
            raise PatchError("bootstrap staged paths do not match requested files")
        _git(["-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", message], work, env)
        changed = _changed_paths(["diff", "--name-only", "--no-renames", "-z", base_sha, "HEAD"], work, env)
        if set(changed) != set(paths) or len(changed) != len(paths):
            raise PatchError("bootstrap committed paths do not match requested files")
        _git(["push", "-q", "origin", f"HEAD:refs/heads/{branch}"], work, env)
        return _git(["rev-parse", "HEAD"], work, env)


def update_bootstrap(url, token, branch, head_sha, files, message):
    """Bring the setup branch at ``head_sha`` up to date with ``files`` in one new commit.

    Returns the new commit, or None when the branch already holds ``files``. The push is not
    forced: a branch that moved since ``head_sha`` rejects it, and nothing is overwritten.
    """
    paths = _bootstrap_paths(files)
    env = _env(token)
    with tempfile.TemporaryDirectory() as tmp:
        _git(["clone", "-q", "--no-checkout", url, "repo"], tmp, env)
        work = os.path.join(tmp, "repo")
        _git(["checkout", "-q", "-b", "setup", head_sha], work, env)
        for name in paths:
            _write_bootstrap_file(work, name, files[name])
        _git(["add", "--", *paths], work, env)
        staged = _changed_paths(["diff", "--cached", "--name-only", "--no-renames", "-z"], work, env)
        if not staged:
            return None
        if not set(staged) <= set(paths):
            raise PatchError("bootstrap staged paths do not match requested files")
        _git(["-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", message], work, env)
        try:
            _git(["push", "-q", "origin", f"HEAD:refs/heads/{branch}"], work, env)
        except PatchError as exc:
            raise PatchError(f"{branch} cannot fast-forward from {head_sha[:12]}, so it was not "
                             f"updated ({exc})") from None
        return _git(["rev-parse", "HEAD"], work, env)


def apply(url, token, branch, base_sha, patch, message, force, protected=(), limits=None):
    """Clone, apply the patch on base_sha, commit, push to `branch`. Returns the new commit sha.
    A patch that touches a protected path is refused before the push: a pushed workflow file would
    already run (`on: push`) with the repo's secrets, whatever checks come after. `limits` is
    (default branch, lines, files, advisory): a push that takes the branch over the file count is
    refused, and over the line count refused too unless `advisory` — the 4th element; absent, the
    old hard bound stands — in which case the size is reported as a warning and the push proceeds
    (ADR-0070). A 5th element, a source spec
    (``{"target", "ceiling", "advisory"}`` or None, ADR-0079) switches the line metric to source
    only: over the ceiling is refused as a size-only excess the Foreman routes to decomposition,
    over the target warns (or refuses when its advisory flag is false). A push to an
    open PR stays on it, and the next fix builds on it (ADR-0040)."""
    env = _env(token)
    with tempfile.TemporaryDirectory() as tmp:
        _git(["clone", "-q", "--no-checkout", url, "repo"], tmp, env)
        work = os.path.join(tmp, "repo")
        # One commit id for the checkout and for the protected-path diff below: a name such as HEAD
        # would mean the new commit by then, and the diff would be empty
        base = _git(["rev-parse", "--verify", "--end-of-options", f"{base_sha}^{{commit}}"], work, env)
        _git(["checkout", "-q", "-B", branch, base], work, env)
        try:
            with open(os.path.join(tmp, "result.patch"), "w", encoding="utf-8") as handle:
                handle.write(patch["diff"])
        except UnicodeEncodeError as exc:
            raise PatchError(f"the patch is not UTF-8 text: {exc.reason}") from None
        if patch["format"] == "mbox":
            _git(["am", "-q", "../result.patch"], work, env)
        else:
            _git(["apply", "--index", "../result.patch"], work, env)
            _git(["commit", "-q", "-m", message], work, env)
        touched = _git(["diff", "--name-only", "--no-renames", "-z", base, "HEAD"], work, env)
        refused = [name for name in touched.split("\0") if name and is_protected(name, protected)]
        if refused:
            raise PatchError(f"changes protected path {', '.join(refused)}")
        if limits:
            source = limits[4] if len(limits) > 4 else None
            totals = _totals(work, env, f"origin/{limits[0]}")
            if totals["files"] > limits[2]:
                raise PatchError(f"{totals['total']} lines in {totals['files']} files exceeds the "
                                 f"WorkOrder constraint of {limits[2]} files")
            if source:
                _check_source(totals, source)
            else:
                advisory = limits[3] if len(limits) > 3 else False  # a caller without the flag keeps hard
                if totals["total"] > limits[1]:
                    if not advisory:
                        raise PatchError(f"{totals['total']} lines in {totals['files']} files exceeds "
                                         f"the WorkOrder constraint of {limits[1]} lines")
                    print(f"factory advisory: the whole pull request is {totals['total']} lines in "
                          f"{totals['files']} files, over the WorkOrder's {limits[1]}-line target with "
                          f"{limits[1] - totals['total']} lines left; this is advisory, not a rejection",
                          file=sys.stderr, flush=True)
        _git(["push", "-q", *(["--force"] if force else []), "origin", f"HEAD:refs/heads/{branch}"], work, env)
        return _git(["rev-parse", "HEAD"], work, env)


def _check_source(totals, source):
    """Refuse a source total over the reviewable ceiling (size-only, recoverable), or over the
    target when it is not advisory; warn over an advisory target (ADR-0079)."""
    verdict = sizing.boundary(totals["source"], source)
    if verdict == sizing.SIZE_CEILING:
        raise PatchError(
            f"{sizing.SIZE_CEILING_MARKER}: the whole pull request changes {totals['source']} source "
            f"lines ({totals['tests']} test, {totals['docs']} doc) in {totals['files']} files, over the "
            f"{source['ceiling']}-line reviewable ceiling; this is a size-only excess, not a failed verify")
    if verdict == sizing.SIZE_REFUSED:
        raise PatchError(f"the proposed pull request changes {totals['source']} source lines "
                         f"({totals['tests']} test, {totals['docs']} doc) in {totals['files']} files; "
                         f"the WorkOrder allows {source['target']} source lines")
    if verdict == sizing.SIZE_ADVISORY:
        print(f"factory advisory: the whole pull request is {totals['source']} source lines "
              f"({totals['tests']} test, {totals['docs']} doc) in {totals['files']} files, over the "
              f"WorkOrder's {source['target']}-source-line target with "
              f"{source['target'] - totals['source']} lines left; this is advisory, not a rejection",
              file=sys.stderr, flush=True)


def _totals(work, env, default):
    """The source/tests/docs/total line totals from where HEAD left `default` to HEAD."""
    base = _git(["merge-base", default, "HEAD"], work, env)
    return sizing.measure(sizing.parse_numstat(_git(["diff", "--numstat", base, "HEAD"], work, env)))
