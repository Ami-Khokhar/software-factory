"""Configuration, read from the environment and the repo manifest (repos.json) at start.

Precedence for a repo's settings: its manifest entry, then the manifest defaults, then the env.
Repo sets (FACTORY_REPOS, FACTORY_LOCAL_ONLY_REPOS) are the union of env and manifest, so a repo
still listed in the env keeps working while it is migrated.
"""
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import repos as manifests


@dataclass(frozen=True)
class Config:
    mcp_token: str
    repos: frozenset
    db_path: str = str(Path.home() / ".local" / "state" / "factory-mcp" / "factory.sqlite3")
    relay_url: str = "http://127.0.0.1:43124"
    github_token: str = ""
    github_api: str = "https://api.github.com"
    host: str = "127.0.0.1"
    port: int = 43125
    tls_cert: str = ""
    tls_key: str = ""
    workers: tuple = (("codex", "hosted"),)  # (relay agentId, "local"|"hosted"), preferred first
    local_only_repos: frozenset = frozenset()
    git_url: str = "https://github.com/{repo}.git"
    owner: str = ""  # the person who owns escalations; default is the repo owner
    poll_seconds: int = 60
    gate_wait_seconds: int = 7200  # no verdict on the head for this long: a person looks
    max_passes: int = 4
    wall_ms: int = 20 * 60 * 1000
    protected_paths: tuple = (".github/", "AGENTS.md")
    trusted_authors: frozenset = frozenset()  # empty: only the repo owner is trusted
    checkout_root: str = ""  # worker checkouts live at <root>/<owner>/<repo>
    worker_image: str = "factory-worker:latest"  # "" skips the verify-tool probe
    repos_file: str = ""
    manifest: manifests.Manifest = field(default_factory=manifests.Manifest)
    env_values: tuple = ()  # the fields a manifest overrides, as they were before any manifest

    OVERRIDDEN = ("repos", "local_only_repos", "workers", "protected_paths", "trusted_authors")

    def with_manifest(self, manifest):
        """This config with `manifest` applied on top of the env-derived values. Reloadable."""
        env = dict(self.env_values) or {name: getattr(self, name) for name in self.OVERRIDDEN}
        return replace(self, manifest=manifest, env_values=tuple(env.items()),
                       repos=env["repos"] | manifest.enabled(),
                       local_only_repos=env["local_only_repos"] | manifest.local_only(),
                       workers=manifest.workers or env["workers"],
                       protected_paths=manifest.protected_paths or env["protected_paths"],
                       trusted_authors=manifest.trusted_authors or env["trusted_authors"])

    def workers_for(self, repo):
        entry = self.manifest.repos.get(repo)
        return (entry and entry.workers) or self.workers

    def protected_for(self, repo):
        entry = self.manifest.repos.get(repo)
        return (entry and entry.protected_paths) or self.protected_paths

    def trusted_for(self, repo):
        """The per-repo list, else the global one, else the repo owner."""
        entry = self.manifest.repos.get(repo)
        return (entry and entry.trusted_authors) or self.trusted_authors \
            or frozenset({repo.split("/")[0]})

    def trusts(self, repo, author):
        return author.lower() in {login.lower() for login in self.trusted_for(repo)}

    def checkout(self, repo):
        return str(Path(self.checkout_root, *repo.split("/"))) if self.checkout_root else ""

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        token = env.get("FACTORY_MCP_TOKEN", "")
        if not token:
            # OpenBot always sends a bearer token to a custom MCP server, and an open Foreman
            # would let anything on the network drive the factory.
            raise ValueError("FACTORY_MCP_TOKEN is required")
        repos = frozenset(r.strip() for r in env.get("FACTORY_REPOS", "").split(",") if r.strip())
        defaults = cls(mcp_token=token, repos=repos)
        config = cls(
            mcp_token=token,
            repos=repos,
            db_path=env.get("FACTORY_DB") or defaults.db_path,
            relay_url=(env.get("FACTORY_RELAY_URL") or defaults.relay_url).rstrip("/"),
            github_token=env.get("FACTORY_GITHUB_TOKEN", ""),
            github_api=(env.get("FACTORY_GITHUB_API") or defaults.github_api).rstrip("/"),
            host=env.get("FACTORY_HOST") or defaults.host,
            port=int(env.get("FACTORY_PORT") or defaults.port),
            tls_cert=env.get("FACTORY_TLS_CERT", ""),
            tls_key=env.get("FACTORY_TLS_KEY", ""),
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
            trusted_authors=frozenset(a.strip() for a in env.get("FACTORY_TRUSTED_AUTHORS", "")
                                      .split(",") if a.strip()),
            checkout_root=os.path.expanduser(env.get("FACTORY_CHECKOUT_ROOT", "")),
            worker_image=env.get("FACTORY_WORKER_IMAGE", defaults.worker_image),
            repos_file=os.path.expanduser(env.get("FACTORY_REPOS_FILE") or str(
                Path.home() / ".config" / "factory-mcp" / "repos.json")),
        )
        return config.with_manifest(manifests.load(config.repos_file))
