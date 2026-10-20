"""Configuration, read from the environment and repos.json once at start."""
import os
from dataclasses import dataclass
from pathlib import Path

from . import jev_rank
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
    remote_verify_repos: frozenset = frozenset()  # repos whose verify runs on a GitHub macOS runner (ADR-0045)
    remote_verify_seconds: int = 1800  # no finished run for this long: the attempt fails
    git_url: str = "https://github.com/{repo}.git"
    owner: str = ""  # the person who owns escalations; default is the repo owner
    poll_seconds: int = 60
    gate_wait_seconds: int = 7200  # no verdict on the head for this long: a person looks
    max_passes: int = 4
    passes_per_repo: int = 2  # worker passes of one repo that run at once (ADR-0036)
    wall_ms: int = 20 * 60 * 1000
    source_target: int = 700  # source-only small-PR target (ADR-0079)
    source_ceiling: int = 1000  # source-only reviewable ceiling; over it: decomposition, not a wall
    protected_paths: tuple = (".github/", "AGENTS.md")
    checkout_root: str = ""  # worker checkouts live at <root>/<owner>/<repo>; empty: the agent's cwd
    trusted_authors: frozenset = frozenset()  # issue authors a worker may act for; empty: repo owner
    repos_file: str = ""  # repos.json (ADR-0014); empty: FACTORY_REPOS is the whole allowlist
    lessons_file: str = ""  # lessons.json (ADR-0020); empty: the factory keeps no lessons
    agent_token: str = ""  # Day Desk's driving token (ADR-0030); empty: only the owner's token works
    agents_file: str = ""  # the relay's agents.json; empty: the factory may not register a worker
    worker_template: str = "pi-glm-factory"  # the approved worker a new one copies (ADR-0077)
    salvage: bool = True  # a timed-out pass's partial work is kept (ADR-0086); FACTORY_SALVAGE=off
    rank_lessons: bool = False  # Jev ranks the repo's lessons per dispatch (ADR-0058)
    jev_api_key: str = ""  # TYPESAFE_API_KEY: only jev_rank sends it, in one Authorization header
    jev_url: str = jev_rank.HOST  # the System One endpoint; tests point it at a local fake

    def listed(self, repo):
        """The allowlist's spelling of `repo`, or None. GitHub repo names ignore case."""
        return next((name for name in self.repos if name.lower() == repo.lower()), None)

    def local_only(self, repo):
        return repo.lower() in {name.lower() for name in self.local_only_repos}

    def remote_verify(self, repo):
        return repo.lower() in {name.lower() for name in self.remote_verify_repos}

    def checkout(self, repo):
        """The worker checkout for `repo`, sent to the relay as the task cwd, or None."""
        return str(Path(self.checkout_root, *repo.split("/"))) if self.checkout_root else None

    @property
    def ask_url(self):
        """Where a worker container reaches the ask route (ADR-0098). Docker Desktop maps the name
        to the host's loopback, where factory-mcp listens, so the route stays off the network."""
        return f"http://host.docker.internal:{self.port}/worker/ask"

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
        agent_token = env.get("FACTORY_AGENT_TOKEN", "")
        if agent_token and agent_token == token:
            # The token is how factory-mcp tells the agent from the owner; one token would hide it.
            raise ValueError("FACTORY_AGENT_TOKEN must differ from FACTORY_MCP_TOKEN")
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
        passes_per_repo = int(env.get("FACTORY_PASSES_PER_REPO") or defaults.passes_per_repo)
        if passes_per_repo < 1:
            raise ValueError("FACTORY_PASSES_PER_REPO must be 1 or more; below 1, no repo would get a pass")
        remote_verify_seconds = int(env.get("FACTORY_REMOTE_VERIFY_SECONDS") or defaults.remote_verify_seconds)
        if remote_verify_seconds < 1:
            raise ValueError("FACTORY_REMOTE_VERIFY_SECONDS must be 1 or more")
        rank_lessons = env.get("FACTORY_JEV_LESSON_RANKING", "") in ("1", "true", "yes")
        source_target = int(env.get("FACTORY_SOURCE_CHANGED_LINES") or defaults.source_target)
        if source_target < 1:
            raise ValueError("FACTORY_SOURCE_CHANGED_LINES must be 1 or more")
        source_ceiling = int(env.get("FACTORY_SOURCE_CHANGED_LINES_CEILING") or defaults.source_ceiling)
        if source_ceiling < source_target:
            raise ValueError("FACTORY_SOURCE_CHANGED_LINES_CEILING must be at least "
                             "FACTORY_SOURCE_CHANGED_LINES")
        if rank_lessons and not env.get("TYPESAFE_API_KEY", ""):
            # A flag without a key would rank nothing on every dispatch: say what is missing.
            raise ValueError("FACTORY_JEV_LESSON_RANKING is on, but TYPESAFE_API_KEY is not set; "
                             "set the key in the factory service environment, or turn ranking off")
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
            remote_verify_repos=frozenset(r.strip() for r in env.get("FACTORY_REMOTE_VERIFY_REPOS", "")
                                          .split(",") if r.strip()),
            remote_verify_seconds=remote_verify_seconds,
            git_url=env.get("FACTORY_GIT_URL") or defaults.git_url,
            owner=env.get("FACTORY_OWNER", ""),
            poll_seconds=int(env.get("FACTORY_POLL_SECONDS") or defaults.poll_seconds),
            gate_wait_seconds=int(env.get("FACTORY_GATE_WAIT_SECONDS") or defaults.gate_wait_seconds),
            max_passes=int(env.get("FACTORY_MAX_PASSES") or defaults.max_passes),
            passes_per_repo=passes_per_repo,
            wall_ms=int(env.get("FACTORY_WALL_MS") or defaults.wall_ms),
            source_target=source_target,
            source_ceiling=source_ceiling,
            checkout_root=env.get("FACTORY_CHECKOUT_ROOT", ""),
            trusted_authors=frozenset(a.strip() for a in env.get("FACTORY_TRUSTED_AUTHORS", "").split(",")
                                      if a.strip()),
            repos_file=repos_file,
            lessons_file=env.get("FACTORY_LESSONS_FILE", ""),
            agent_token=agent_token,
            rank_lessons=rank_lessons,
            jev_api_key=env.get("TYPESAFE_API_KEY", ""),
            agents_file=env.get("FACTORY_AGENTS_FILE", ""),
            worker_template=env.get("FACTORY_WORKER_TEMPLATE") or defaults.worker_template,
            salvage=env.get("FACTORY_SALVAGE", "on").lower() != "off",
        )
