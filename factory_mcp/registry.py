"""The relay registry (agents.json): add a factory worker by copying an approved template.

A factory worker is a relay agent whose args pin a harness, a model and a credential mount
(ADR-0028). The relay has no API to add one, so registering a worker means editing the same
`~/.config/agent-relay/agents.json` the owner edits by hand and reloading the relay. To keep that
write inside the credential boundary, a new worker copies an approved template (the sandboxed
`pi-glm-factory`) and changes only the model it pins: the command, credential mount, env, cwd,
allowedRoots, image and timeout are the template's, so a registered worker cannot reach anything a
factory worker could not already reach. It never touches FACTORY_WORKERS, so a registered worker
receives no WorkOrder until the owner lists it there and its provider-health pilot is green
(ADR-0076, ADR-0077).
"""
import json
import os
import re
import stat
import tempfile
from pathlib import Path

# A worker id becomes a relay agent id; a model becomes a CLI argument. Neither may hold a path
# separator, whitespace or a leading dash (which the harness would read as another flag).
WORKER_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def build_agent(template, worker_id, model):
    """The new agent: `template` with its id, name, description and model args set for this worker.
    The harness, credential mount, env, cwd, allowedRoots, image and timeout are the template's. The
    provider stays the template's too — each provider's exact model alias is its own (ADR-0076)."""
    if not WORKER_ID.match(worker_id or ""):
        raise ValueError(f"{worker_id!r} is not a worker id: use lowercase letters, digits, '.', "
                         "'-' or '_', starting with a letter or digit")
    if not MODEL.match(model or ""):
        raise ValueError(f"{model!r} is not a model name: use letters, digits, '.', '-' or '_', "
                         "with no space and no leading dash")
    args = list(template.get("args") or [])
    if "--model" not in args or "--models" not in args:
        raise ValueError(f"the template {template.get('id')!r} pins no model, so no worker can be "
                         "registered from it")
    provider = args[args.index("--models") + 1].split("/", 1)[0]
    args[args.index("--model") + 1] = model
    args[args.index("--models") + 1] = f"{provider}/{model}"
    return dict(template, id=worker_id, name=f"Pi ({model}), sandboxed factory worker",
                description=("worker/runner.py: pi in the factory-worker container; no GitHub "
                             "credential; returns a patch"), args=args)


def register(path, template_id, worker_id, model):
    """Add the new worker to the registry at `path` and return it. Refuses a duplicate id. Every
    check runs before the file is touched, and the write is atomic, so a reload never reads a
    half-written registry."""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)  # mkstemp would leave the replacement 0600
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"the relay registry at {path} cannot be read: {exc}") from None
    agents = data.get("agents")
    if not isinstance(agents, list):
        raise ValueError(f"the relay registry at {path} has no agents list")
    if any(agent.get("id") == worker_id for agent in agents):
        raise ValueError(f"{worker_id} is already a worker in the relay registry")
    template = next((agent for agent in agents if agent.get("id") == template_id), None)
    if template is None:
        raise ValueError(f"{template_id} is not in the relay registry, so there is no template to "
                         "copy; register it there first")
    agent = build_agent(template, worker_id, model)
    agents.append(agent)
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".agents-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, mode)  # the relay reads this file; keep the permissions it was created with
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return agent
