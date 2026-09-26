"""The git work factory-mcp does itself: clone a worker checkout, and turn a patch Result into a
pushed commit on the WorkItem branch."""
import base64
import os
import subprocess
import tempfile


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
    """True if a repo path is a protected path, or is under a protected directory (one ending in /)."""
    return any(name == p or (p.endswith("/") and name.startswith(p)) for p in protected_paths)


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


def apply(url, token, branch, base_sha, patch, message, force, protected=()):
    """Clone, apply the patch on base_sha, commit, push to `branch`. Returns the new commit sha.
    A patch that touches a protected path is refused before the push: a pushed workflow file would
    already run (`on: push`) with the repo's secrets, whatever checks come after."""
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
        _git(["push", "-q", *(["--force"] if force else []), "origin", f"HEAD:refs/heads/{branch}"], work, env)
        return _git(["rev-parse", "HEAD"], work, env)
