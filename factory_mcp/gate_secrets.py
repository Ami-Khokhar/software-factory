"""A repo's Actions secrets for pr-gatekeeper, set from the owner's login Keychain (ADR-0022).

factory-mcp, not a model, reads each value with `security` and gives it to `gh secret set` on
stdin; gh does GitHub's libsodium encryption, so this stays standard library only. A value never
goes into argv, a result, a log or an error text, and nothing here reads stderr, where a failing
command may echo it.
"""
import json
import os
import subprocess

# The gate workflow passes both. The public wrapper checks out the private Ami-Khokhar/pr-gatekeeper
# with GATEKEEPER_READ_TOKEN, falling back to the repo's own token, which cannot read another
# private repo: so while pr-gatekeeper is private, the gate needs both.
NAMES = ("OPENCODE_GO_API_KEY", "GATEKEEPER_READ_TOKEN")
SERVICE = "software-factory/{name}"
MISSING = "missing: add it with security add-generic-password -s software-factory/{name} -a factory -w"
NOT_IN_KEYCHAIN = 44  # security's exit code for errSecItemNotFound
TIMEOUT = 60


class _Failed(Exception):
    """A command that failed, named with its exit code only."""


def _run(argv, command, env=None, value=None):
    try:
        return subprocess.run(argv, input=value, env=env, capture_output=True, text=True, timeout=TIMEOUT)
    except FileNotFoundError:
        raise _Failed(f"{argv[0]} is not installed") from None
    except subprocess.TimeoutExpired:
        raise _Failed(f"{command} timed out") from None
    except (OSError, ValueError) as exc:  # ValueError: output that is not text
        raise _Failed(f"{command} failed: {type(exc).__name__}") from None


def _gh(args, token, value=None):
    command = "gh " + " ".join(args[:2])
    env = dict(os.environ, GH_TOKEN=token) if token else None
    done = _run(["gh", *args], command, env, value)
    if done.returncode:
        raise _Failed(f"{command} exited {done.returncode}")
    return done.stdout


def _names_on(repo, token):
    out = _gh(["secret", "list", "--repo", repo, "--json", "name"], token)
    try:
        return {item["name"] for item in json.loads(out)}
    except (ValueError, TypeError, KeyError):
        raise _Failed("gh secret list printed no JSON list of names") from None


def _keychain(name):
    """The Keychain's value for `name`, or None when the Keychain has none."""
    command = "security find-generic-password"
    done = _run(["security", "find-generic-password", "-s", SERVICE.format(name=name), "-w"], command)
    if done.returncode == NOT_IN_KEYCHAIN:
        return None
    if done.returncode:
        raise _Failed(f"{command} exited {done.returncode}")
    return done.stdout.removesuffix("\n") or None


def ensure(repo, token):
    """Set each gate secret `repo` lacks from the Keychain; never overwrite one it has.

    Returns [{"name", "status"}], status one of `set`, `already set`, MISSING or `failed: ...`.
    Never raises for a missing or failed secret: the owner fixes it and calls again."""
    try:
        present = _names_on(repo, token)
    except _Failed as exc:
        return [{"name": name, "status": f"failed: {exc}"} for name in NAMES]
    status = {}
    for name in NAMES:
        if name in present:
            status[name] = "already set"
            continue
        try:
            value = _keychain(name)
            if value is None:
                status[name] = MISSING.format(name=name)
                continue
            _gh(["secret", "set", name, "--repo", repo], token, value)
            status[name] = "set"
        except _Failed as exc:
            status[name] = f"failed: {exc}"
    if "set" in status.values():
        try:
            shown, reason = _names_on(repo, token), "gh secret list does not show it"
        except _Failed as exc:
            shown, reason = set(), str(exc)
        for name in NAMES:
            if status[name] == "set" and name not in shown:
                status[name] = f"failed: {reason} after gh secret set"
    return [{"name": name, "status": status[name]} for name in NAMES]
