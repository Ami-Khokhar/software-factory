"""Configuration, read from the environment and repos.json once at start."""
import os
from dataclasses import dataclass
from pathlib import Path

from . import repos as manifest


@dataclass(frozen=True)
class Config:
    mcp_token: str
    repos: frozenset  # the allowlist: FACTORY_REPOS and the repos in repos.json
    db_path: str = str(Path.home() / ".local" / "state" / "factory-mcp" / "factory.sqlite3")
    relay_url: str = "http://127.0.0.1:43124"
    github_token: str = ""
    github_api: str = "https://api.github.com"
    port: int = 43125  # on 127.0.0.1 only: factory-mcp speaks plain HTTP
    workers: tuple = (("codex", "hosted"),)  # (relay agentId, "local"|"hosted"), preferred first
    local_only_repos: frozenset = frozenset()
    git_url: str = "https://github.com/{repo}.git"
    owner: str = ""  # the person who owns escalations; default is the repo owner
    poll_seconds: int = 60
    gate_wait_seconds: int = 7200  # no verdict on the head for this long: a person looks
    max_passes: int = 4
    wall_ms: int = 20 * 60 * 1000
    protected_paths: tuple = (".github/", "AGENTS.md")
    checkout_root: str = ""  # worker checkouts live at <root>/<owner>/<repo>; empty: the agent's cwd
    trusted_authors: frozenset = frozenset()  # issue authors a worker may act for; empty: repo owner
    repos_file: str = ""  # repos.json (ADR-0014); empty: FACTORY_REPOS is the whole allowlist

    def listed(self, repo):
        """The allowlist's spelling of `repo`, or None. GitHub repo names ignore case."""
        return next((name for name in self.repos if name.lower() == repo.lower()), None)

    def local_only(self, repo):
        return repo.lower() in {name.lower() for name in self.local_only_repos}

    def checkout(self, repo):
        """The worker checkout for `repo`, sent to the relay as the task cwd, or None."""
        return str(Path(self.checkout_root, *repo.split("/"))) if self.checkout_root else None

    def trusts(self, repo, author):
        """Issue text reaches a worker only when a trusted person wrote it (ADR-0007). GitHub logins
        ignore case."""
        trusted = self.trusted_authors or {repo.split("/")[0]}
        return (author or "").lower() in {login.lower() for login in trusted}

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        token = env.get("FACTORY_MCP_TOKEN", "")
        if not token:
            # An open Foreman would let anything that reaches the port drive the factory.
            raise ValueError("FACTORY_MCP_TOKEN is required")
        repos_file = env.get("FACTORY_REPOS_FILE", "")
        repos = frozenset(r.strip() for r in env.get("FACTORY_REPOS", "").split(",") if r.strip()) \
            | frozenset(entry["name"] for entry in manifest.load(repos_file))
        spellings = {}
        for name in sorted(repos):
            if name.lower() in spellings:
                raise ValueError(f"{spellings[name.lower()]} and {name} are one repo: list it once, "
                                 "in FACTORY_REPOS or repos.json")
            spellings[name.lower()] = name
        defaults = cls(mcp_token=token, repos=repos)
        return cls(
            mcp_token=token,
            repos=repos,
            db_path=env.get("FACTORY_DB") or defaults.db_path,
            relay_url=(env.get("FACTORY_RELAY_URL") or defaults.relay_url).rstrip("/"),
            github_token=env.get("FACTORY_GITHUB_TOKEN", ""),
            github_api=(env.get("FACTORY_GITHUB_API") or defaults.github_api).rstrip("/"),
            port=int(env.get("FACTORY_PORT") or defaults.port),
            workers=tuple(tuple(w.strip().split(":", 1)) for w in env["FACTORY_WORKERS"].split(",")
                          if w.strip()) if env.get("FACTORY_WORKERS") else defaults.workers,
            local_only_repos=frozenset(r.strip() for r in env.get("FACTORY_LOCAL_ONLY_REPOS", "")
                                       .split(",") if r.strip()),
            git_url=env.get("FACTORY_GIT_URL") or defaults.git_url,
            owner=env.get("FACTORY_OWNER", ""),
            poll_seconds=int(env.get("FACTORY_POLL_SECONDS") or defaults.poll_seconds),
            gate_wait_seconds=int(env.get("FACTORY_GATE_WAIT_SECONDS") or defaults.gate_wait_seconds),
            max_passes=int(env.get("FACTORY_MAX_PASSES") or defaults.max_passes),
            wall_ms=int(env.get("FACTORY_WALL_MS") or defaults.wall_ms),
            checkout_root=env.get("FACTORY_CHECKOUT_ROOT", ""),
            trusted_authors=frozenset(a.strip() for a in env.get("FACTORY_TRUSTED_AUTHORS", "").split(",")
                                      if a.strip()),
            repos_file=repos_file,
        )
