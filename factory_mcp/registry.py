"""The relay registry (agents.json): add a factory worker by copying an approved template.

A factory worker is a relay agent whose args pin a harness, a model and a credential mount
(ADR-0028). The relay has no API to add one, so registering a worker means editing the same
`~/.config/agent-relay/agents.json` the owner edits by hand and reloading the relay. To keep that
write inside the credential boundary, a new worker copies an approved template — the sandboxed
`pi-glm-factory`, or another sandboxed template the owner approves, such as the OpenCode
`opencode-mimo-factory` — and changes only the model it pins: the command, credential mount, env,
cwd, allowedRoots, image and timeout are the template's, so a registered worker cannot reach
anything a factory worker could not already reach. A template must run `worker/runner.py` under
`python3` itself, so no discovered, unsandboxed relay agent can be copied. The model may name its
provider (`opencode/mimo-v2.6-flash-free`) or omit it, but it must land under the template's own
provider — a change of provider is a different approval (ADR-0076). It never touches
FACTORY_WORKERS. A registered worker needs successful health and explicit activation
through factory_set_worker_enabled by Day Desk or the owner (ADR-0127) before receiving a WorkOrder.
"""
import json
import os
import re
import stat
import tempfile
from pathlib import Path

# A worker id becomes a relay agent id; a model becomes a CLI argument. Neither may hold a path
# separator (except the model's one provider segment), whitespace or a leading dash (which the
# harness would read as another flag).
NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
WORKER_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MODEL = re.compile(rf"^{NAME}(/{NAME})?$")
# A template must run the factory runner itself: python3 on an absolute worker/runner.py path, no
# '..' segment, with a harness handed over after '--'. Anything else in the relay registry (the
# unsandboxed discovered agents) is not a template to copy.
RUNNER = re.compile(r"^/(?:[A-Za-z0-9._/-]+/)?worker/runner\.py\Z")


def _value(args, flag):
    """The index of the value after `flag`, or -1 when the flag is absent or trails with nothing
    after it (so it pins nothing)."""
    if flag in args and args.index(flag) + 1 < len(args):
        return args.index(flag) + 1
    return -1


def _provider(pin):
    """The provider a pinned model names, or None when the pin carries no usable `provider/name`
    of its own."""
    if isinstance(pin, str) and "/" in pin:
        asked = pin.split("/", 1)[0]
        if re.fullmatch(NAME, asked):
            return asked
    return None


def build_agent(template, worker_id, model):
    """The new agent: `template` with its id, name, description and model args set for this worker.
    The harness, credential mount, env, cwd, allowedRoots, image and timeout are the template's. The
    provider stays the template's too — each provider's exact model alias is its own (ADR-0076), so
    `model` may be bare or `provider/name` under the template's provider and nothing else. Every
    check runs on the template before the copy, so an unsandboxed, injected or unpinned template
    never produces an agent."""
    if not isinstance(worker_id, str) or not WORKER_ID.match(worker_id):
        raise ValueError(f"{worker_id!r} is not a worker id: use lowercase letters, digits, '.', "
                         "'-' or '_', starting with a letter or digit")
    if not isinstance(model, str) or not MODEL.match(model):
        raise ValueError(f"{model!r} is not a model name: use letters, digits, '.', '-' or '_', "
                         "with one optional 'provider/' prefix, no space and no leading dash")
    template_id = template.get("id")
    if template.get("command") != "python3":
        raise ValueError(f"the template {template_id!r} does not run python3 "
                         "worker/runner.py in the factory-worker container, so it is not a "
                         "sandboxed template to copy")
    args = list(template.get("args") or [])
    if (not args or not isinstance(args[0], str) or ".." in args[0]
            or not RUNNER.match(args[0])):
        raise ValueError(f"the template {template_id!r} does not run worker/runner.py, so no "
                         "worker can be registered from it")
    harness_at = _value(args, "--")
    if harness_at < 0:
        raise ValueError(f"the template {template_id!r} hands no harness to worker/runner.py, so "
                         "no worker can be registered from it")
    model_at = _value(args, "--model")
    if model_at < 0:
        raise ValueError(f"the template {template_id!r} pins no model, so no worker can be "
                         "registered from it")
    # pi writes the provider in `--models`; OpenCode pins it in `--model provider/name`.
    provider = None
    for at in (_value(args, "--models"), model_at):
        if at >= 0:
            provider = _provider(args[at])
            if provider is not None:
                break
    if provider is None:
        raise ValueError(f"the template {template_id!r} names no provider of its own, so no model "
                         "can be registered from it")
    if "/" in model:
        asked, name = model.split("/", 1)
        if asked != provider:
            raise ValueError(f"{model!r} names the provider {asked!r}, but the template "
                             f"{template_id!r} is a {provider} worker: a worker may change only "
                             "within its provider (ADR-0076)")
    else:
        name = model
    models_at = _value(args, "--models")
    if models_at < 0:  # one --model argument, which carries the provider itself
        args[model_at] = f"{provider}/{name}"
    else:
        args[model_at] = name
        args[models_at] = f"{provider}/{name}"
    harness = args[harness_at]
    return dict(template, id=worker_id, name=f"{harness} ({name}), sandboxed factory worker",
                description=(f"worker/runner.py: {harness} in the factory-worker container; no "
                             "GitHub credential; returns a patch"), args=args)


def register(path, template_id, worker_id, model):
    """Add the new worker to the registry at `path` and return it, copying the template
    `template_id`. Refuses a duplicate id. Every check runs before the file is touched, and the
    write is atomic, so a reload never reads a half-written registry."""
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
