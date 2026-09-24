"""Turn a patch Result (hosted providers return a diff) into a pushed commit on the WorkItem branch."""
import base64
import os
import subprocess
import tempfile


class PatchError(Exception):
    pass


def _git(args, cwd, env):
    done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise PatchError(f"git {args[0]} failed: {(done.stderr or done.stdout).strip()[-300:]}")
    return done.stdout.strip()


def apply(url, token, branch, base_sha, patch, message, force):
    """Clone, apply the patch on base_sha, commit, push to `branch`. Returns the new commit sha."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="factory-mcp",
               GIT_AUTHOR_EMAIL="factory-mcp@users.noreply.github.com",
               GIT_COMMITTER_NAME="factory-mcp", GIT_COMMITTER_EMAIL="factory-mcp@users.noreply.github.com")
    if token:  # in the environment, not argv, so it is not visible in the process list
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraheader",
                   GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}")
    with tempfile.TemporaryDirectory() as tmp:
        _git(["clone", "-q", "--no-checkout", url, "repo"], tmp, env)
        work = os.path.join(tmp, "repo")
        _git(["checkout", "-q", "-B", branch, base_sha], work, env)
        with open(os.path.join(tmp, "result.patch"), "w") as handle:
            handle.write(patch["diff"])
        if patch["format"] == "mbox":
            _git(["am", "-q", "../result.patch"], work, env)
        else:
            _git(["apply", "--index", "../result.patch"], work, env)
            _git(["commit", "-q", "-m", message], work, env)
        _git(["push", "-q", *(["--force"] if force else []), "origin", f"HEAD:refs/heads/{branch}"], work, env)
        return _git(["rev-parse", "HEAD"], work, env)
