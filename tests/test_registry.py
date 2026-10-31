"""Registering a sandboxed factory worker by copying an approved template (ADR-0077)."""
import json
import os
import stat
import tempfile
import unittest

from factory_mcp import registry
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.store import Store
from tests.fakes import ISSUE_BODY, FakeGitHub, FakeRelay

REPO, ID = "o/r", "o/r#7"
TEMPLATE_ID = "pi-glm-factory"
# The approved template: the sandboxed pi worker, with its credential mount and model pinned.
TEMPLATE = {
    "id": TEMPLATE_ID,
    "name": "Pi (GLM 5.3 Flash), sandboxed factory worker",
    "description": "worker/runner.py: pi in the factory-worker container; no GitHub credential; "
                   "returns a patch",
    "command": "python3",
    "args": ["/x/worker/runner.py", "--credential", "/home/you/.pi/agent/auth.json:/home/node/.pi/agent",
             "--env", "PI_CODING_AGENT_DIR=/home/node/.pi/agent", "--", "pi", "--print",
             "--provider", "opencode-go", "--model", "glm-5.3-flash",
             "--models", "opencode-go/glm-5.3-flash", "--thinking", "off", "--no-session"],
    "cwd": "/x/factory-work",
    "allowedRoots": ["/x/factory-work"],
    "timeoutMs": 1500000,
    "inheritEnv": ["HOME", "PATH"],
}
WORKERS = (("pi-deepseek-factory", "local"), ("pi-glm-factory", "local"),
           ("opencode-glm-factory", "local"))
OPENCODE_TEMPLATE_ID = "opencode-mimo-factory"
# The approved sandboxed OpenCode worker: worker/runner.py under opencode, with the free-model
# credential mount. OpenCode pins its model in one --model provider/model argument.
OPENCODE_TEMPLATE = {
    "id": OPENCODE_TEMPLATE_ID,
    "name": "opencode (mimo-v2.6-flash-free), sandboxed factory worker",
    "description": "worker/runner.py: opencode in the factory-worker container; no GitHub "
                   "credential; returns a patch",
    "command": "python3",
    "args": ["/x/worker/runner.py", "--credential",
             "/Users/you/.local/share/opencode/auth.json:/tmp/factory-home/.local/share/opencode",
             "--", "opencode", "run", "--model", "opencode/mimo-v2.6-flash-free"],
    "cwd": "/x/factory-work",
    "allowedRoots": ["/x/factory-work"],
    "timeoutMs": 1500000,
    "inheritEnv": ["HOME", "PATH"],
    "image": "factory-worker:latest",
}
# What the relay also holds: a discovered, unsandboxed agent that must never be a template.
UNSANDBOXED = {
    "id": "pi-deepseek",
    "command": "pi",
    "args": ["--print", "--provider", "deepseek", "--model", "deepseek-flash"],
}


class RegistryUnitTest(unittest.TestCase):
    """The pure registry functions: a template copy that differs only in the model."""

    def test_build_agent_copies_every_field_but_the_model(self):
        agent = registry.build_agent(TEMPLATE, "pi-qwen-factory", "qwen-3-max")
        self.assertEqual(agent["id"], "pi-qwen-factory")
        # the credential boundary, sandbox path and timeout are the template's, verbatim
        for field in ("command", "cwd", "allowedRoots", "timeoutMs", "inheritEnv"):
            self.assertEqual(agent[field], TEMPLATE[field], field)
        self.assertEqual(agent["args"][:5], TEMPLATE["args"][:5])  # runner.py + credential + env
        # only the model args changed, and the provider stays the template's (ADR-0076)
        self.assertEqual(agent["args"][agent["args"].index("--model") + 1], "qwen-3-max")
        self.assertEqual(agent["args"][agent["args"].index("--models") + 1], "opencode-go/qwen-3-max")

    def test_build_agent_copies_the_opencode_template_with_the_exact_provider_model_pin(self):
        agent = registry.build_agent(OPENCODE_TEMPLATE, "opencode-flash-factory",
                                     "opencode/mimo-v2.6-flash-free")
        expected = list(OPENCODE_TEMPLATE["args"])
        expected[expected.index("--model") + 1] = "opencode/mimo-v2.6-flash-free"
        self.assertEqual(agent["args"], expected)  # one --model argument, exactly as asked
        for field in ("command", "cwd", "allowedRoots", "timeoutMs", "inheritEnv", "image"):
            self.assertEqual(agent[field], OPENCODE_TEMPLATE[field], field)
        self.assertEqual(agent["args"][:3], OPENCODE_TEMPLATE["args"][:3])  # runner + credential

    def test_build_agent_keeps_the_providers_own_spelling_of_the_model(self):
        """provider/name under the template's provider is the same worker as the bare name."""
        self.assertEqual(registry.build_agent(TEMPLATE, "pi-qwen-factory", "opencode-go/qwen-3-max"),
                         registry.build_agent(TEMPLATE, "pi-qwen-factory", "qwen-3-max"))
        self.assertEqual(
            registry.build_agent(OPENCODE_TEMPLATE, "opencode-flash-factory", "mimo-v2.6-flash-free"),
            registry.build_agent(OPENCODE_TEMPLATE, "opencode-flash-factory",
                                 "opencode/mimo-v2.6-flash-free"))

    def test_build_agent_refuses_a_model_under_another_provider(self):
        for template, model in ((TEMPLATE, "opencode/mimo-v2.6-flash-free"),
                                (OPENCODE_TEMPLATE, "opencode-go/glm-5.3-flash"),
                                (dict(OPENCODE_TEMPLATE, args=OPENCODE_TEMPLATE["args"][:-1]
                                      + ["glm-5.3-flash"]), "opencode/mimo-v2.6-flash-free")):
            with self.subTest(template=template["id"], model=model):
                with self.assertRaisesRegex(ValueError, "provider"):
                    registry.build_agent(template, "ok-factory", model)

    def test_build_agent_refuses_a_template_that_does_not_run_the_runner(self):
        for template in (UNSANDBOXED, dict(TEMPLATE, command="bash"),
                         dict(TEMPLATE, args=["/tmp/evil.py", "--", "pi"]),
                         dict(TEMPLATE, args=["/x/worker/../evil/runner.py", "--", "pi"]),
                         dict(OPENCODE_TEMPLATE, args=["/x/worker/runner.py"]),  # no harness
                         dict(OPENCODE_TEMPLATE,
                              args=["/x/worker/runner.py", "--", "opencode", "run"])):  # no model
            with self.subTest(template=template["id"], command=template.get("command")):
                with self.assertRaises(ValueError):
                    registry.build_agent(template, "ok-factory", "ok-1")

    def test_build_agent_refuses_an_unsafe_id_or_model(self):
        for worker_id, model in (("../escape", "ok-1"), ("has space", "ok-1"), ("-leading", "ok-1"),
                                 ("ok-1", "has space"), ("ok-1", "-leading-flag"), ("ok-1", "a//b"),
                                 ("ok-1", "a/b/c"), ("ok-1", "a/-b"), ("ok-1", "a/b/")):
            with self.subTest(worker_id=worker_id, model=model):
                with self.assertRaises(ValueError):
                    registry.build_agent(TEMPLATE, worker_id, model)


class RegisterWorkerTest(unittest.TestCase):
    """factory_register_worker through the Foreman: the registry write, the refusals, and the
    guarantee that a registered worker is not dispatchable until its health is checked and it is enabled."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.agents_file = os.path.join(self.tmp.name, "agents.json")
        self.write_registry([TEMPLATE])
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.github.add_issue(REPO, 7, body=ISSUE_BODY)
        self.foreman = Foreman(
            Store(":memory:"), self.github,
            Config(mcp_token="t", repos=frozenset({REPO}), workers=WORKERS,
                   agents_file=self.agents_file, worker_template=TEMPLATE_ID),
            self.relay)
        self.foreman.intake(REPO, 7)

    def tearDown(self):
        self.tmp.cleanup()

    def write_registry(self, agents):
        with open(self.agents_file, "w") as handle:
            json.dump({"agents": agents}, handle)

    def read_registry(self):
        with open(self.agents_file) as handle:
            return json.load(handle)["agents"]

    def test_day_desk_registers_without_approval_or_implicit_activation(self):
        from factory_mcp import tools
        before = self.foreman.store.get(ID)
        tool = tools.build(self.foreman.store, self.foreman, agent=True)["factory_register_worker"]
        result = tool.fn({"worker": "pi-qwen-factory", "model": "qwen-3-max", "by": "day-desk"})
        self.assertEqual(result["agent"]["id"], "pi-qwen-factory")
        self.assertTrue(result["reloaded"])
        self.assertEqual(self.read_registry()[-1]["id"], "pi-qwen-factory")
        self.assertEqual(self.foreman.store.worker_activations(), [])
        self.assertEqual(self.foreman.store.get(ID), before)
        self.assertEqual(self.foreman.store.all_workorders(), [])
        self.assertEqual(self.relay.tasks, {})
        self.assertTrue(self.foreman.store.global_history()[-1]["detail"]["agent"])

    def test_register_writes_the_agent_and_reloads_the_relay(self):
        result = self.foreman.register_worker("pi-qwen-factory", "qwen-3-max", "day-desk")
        agents = self.read_registry()
        self.assertEqual([a["id"] for a in agents], [TEMPLATE_ID, "pi-qwen-factory"])
        self.assertEqual(result["reloaded"], True)
        self.assertEqual(self.relay.reloads, 1)
        # the note says it is not yet dispatchable, and how to make it so
        self.assertIn("NOT in FACTORY_WORKERS", result["note"])

    def test_a_registered_worker_is_not_dispatchable_until_promoted(self):
        """The safety property (ADR-0077): registering does not add the agent to FACTORY_WORKERS,
        so an issue that names it never reaches the new worker; it falls back to an allowed one (ADR-0110)."""
        self.foreman.register_worker("pi-qwen-factory", "qwen-3-max", "day-desk")
        self.assertEqual([w for w, _ in self.foreman.config.workers],
                         ["pi-deepseek-factory", "pi-glm-factory", "opencode-glm-factory"])
        self.github.edit_issue(REPO, 7, f"Worker: pi-qwen-factory\n\n{ISSUE_BODY}")
        wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "implementing")
        self.assertEqual([t["agentId"] for t in self.relay.tasks.values()], ["pi-deepseek-factory"])

    def test_register_keeps_the_registry_files_permissions(self):
        """mkstemp creates 0600; the registry is replaced atomically, so the mode must be carried
        over or a relay running as another user loses every agent at once."""
        os.chmod(self.agents_file, 0o644)
        self.foreman.register_worker("pi-qwen-factory", "qwen-3-max", "day-desk")
        self.assertEqual(stat.S_IMODE(os.stat(self.agents_file).st_mode), 0o644)

    def test_register_refuses_a_duplicate_id_and_leaves_the_registry_alone(self):
        with self.assertRaisesRegex(ToolError, "already a worker"):
            self.foreman.register_worker(TEMPLATE_ID, "qwen-3-max", "day-desk")
        self.assertEqual(self.read_registry(), [TEMPLATE])  # unchanged
        self.assertEqual(getattr(self.relay, "reloads", 0), 0)  # no reload on a refusal

    def test_register_refuses_an_unsafe_id_or_model(self):
        for worker_id, model in (("../escape", "ok-1"), ("ok-1", "has space"), ("ok-1", "-leading")):
            with self.subTest(worker_id=worker_id, model=model):
                with self.assertRaises(ToolError):
                    self.foreman.register_worker(worker_id, model, "day-desk")
        self.assertEqual(self.read_registry(), [TEMPLATE])  # unchanged

    def test_register_without_an_agents_file_is_refused(self):
        """No FACTORY_AGENTS_FILE means the factory may not write the relay registry: it says so
        rather than guessing a path."""
        foreman = Foreman(Store(":memory:"), FakeGitHub(),
                          Config(mcp_token="t", repos=frozenset({REPO}), workers=WORKERS),
                          FakeRelay())
        with self.assertRaisesRegex(ToolError, "FACTORY_AGENTS_FILE"):
            foreman.register_worker("pi-qwen-factory", "qwen-3-max", "day-desk")


class OpenCodeRegisterTest(unittest.TestCase):
    """factory_register_worker on the approved sandboxed OpenCode template: the exact free-model
    pin lands verbatim under the template's provider, every runner/credential/isolation field is
    the template's, and every guard runs before the registry is touched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.agents_file = os.path.join(self.tmp.name, "agents.json")
        self.agents = [TEMPLATE, OPENCODE_TEMPLATE, UNSANDBOXED]
        self.write_registry(self.agents)
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.github.add_issue(REPO, 7, body=ISSUE_BODY)
        self.foreman = Foreman(
            Store(":memory:"), self.github,
            Config(mcp_token="t", repos=frozenset({REPO}), workers=WORKERS,
                   agents_file=self.agents_file, worker_template=TEMPLATE_ID),
            self.relay)
        self.foreman.intake(REPO, 7)

    def tearDown(self):
        self.tmp.cleanup()

    def write_registry(self, agents):
        with open(self.agents_file, "w") as handle:
            json.dump({"agents": agents}, handle)

    def read_registry(self):
        with open(self.agents_file) as handle:
            return json.load(handle)["agents"]

    def test_register_pins_the_exact_free_model_and_keeps_every_boundary_field(self):
        result = self.foreman.register_worker(
            "opencode-flash-factory", "opencode/mimo-v2.6-flash-free", "day-desk",
            template=OPENCODE_TEMPLATE_ID)
        agent = result["agent"]
        self.assertEqual(agent["id"], "opencode-flash-factory")
        expected_args = list(OPENCODE_TEMPLATE["args"])
        expected_args[expected_args.index("--model") + 1] = "opencode/mimo-v2.6-flash-free"
        self.assertEqual(agent["args"], expected_args)
        for field in ("command", "cwd", "allowedRoots", "timeoutMs", "inheritEnv", "image"):
            self.assertEqual(agent[field], OPENCODE_TEMPLATE[field], field)
        self.assertEqual([a["id"] for a in self.read_registry()],
                         [TEMPLATE_ID, OPENCODE_TEMPLATE_ID, "pi-deepseek",
                          "opencode-flash-factory"])
        self.assertEqual(result["reloaded"], True)
        self.assertEqual(self.relay.reloads, 1)
        self.assertIn("NOT in FACTORY_WORKERS", result["note"])

    def test_a_bare_model_name_lands_under_the_templates_provider(self):
        self.foreman.register_worker("opencode-flash-factory", "mimo-v2.6-flash-free", "day-desk",
                                     template=OPENCODE_TEMPLATE_ID)
        agent = self.read_registry()[-1]
        self.assertEqual(agent["args"][agent["args"].index("--model") + 1],
                         "opencode/mimo-v2.6-flash-free")

    def test_another_providers_model_is_refused_before_any_write(self):
        for model in ("opencode-go/glm-5.3-flash", "deepseek/deepseek-flash"):
            with self.subTest(model=model):
                with self.assertRaisesRegex(ToolError, "provider"):
                    self.foreman.register_worker("opencode-flash-factory", model, "day-desk",
                                                 template=OPENCODE_TEMPLATE_ID)
        self.assertEqual(self.read_registry(), self.agents)  # unchanged
        self.assertEqual(getattr(self.relay, "reloads", 0), 0)

    def test_an_unsafe_model_name_is_refused_before_any_write(self):
        for model in ("a//b", "a/b/c", "has space", "-leading", "a/b/"):
            with self.subTest(model=model):
                with self.assertRaises(ToolError):
                    self.foreman.register_worker("opencode-flash-factory", model, "day-desk",
                                                 template=OPENCODE_TEMPLATE_ID)
        self.assertEqual(self.read_registry(), self.agents)  # unchanged
        self.assertEqual(getattr(self.relay, "reloads", 0), 0)

    def test_an_unsandboxed_or_unknown_template_is_refused_before_any_write(self):
        for template, reason in (("pi-deepseek", "sandboxed"),
                                 ("no-such-factory", "not in the relay registry")):
            with self.subTest(template=template):
                with self.assertRaisesRegex(ToolError, reason):
                    self.foreman.register_worker(
                        "opencode-flash-factory", "opencode/mimo-v2.6-flash-free", "day-desk",
                        template=template)
        self.assertEqual(self.read_registry(), self.agents)  # unchanged
        self.assertEqual(getattr(self.relay, "reloads", 0), 0)

    def test_a_registered_opencode_worker_is_not_dispatchable_until_promoted(self):
        """Registering is not promoting, for an OpenCode worker too (ADR-0077): FACTORY_WORKERS
        and the pilot stay the owner's, so nothing is dispatched to the new worker (ADR-0110)."""
        self.foreman.register_worker("opencode-flash-factory", "opencode/mimo-v2.6-flash-free",
                                     "day-desk", template=OPENCODE_TEMPLATE_ID)
        self.assertEqual([w for w, _ in self.foreman.config.workers],
                         ["pi-deepseek-factory", "pi-glm-factory", "opencode-glm-factory"])
        self.github.edit_issue(REPO, 7, f"Worker: opencode-flash-factory\n\n{ISSUE_BODY}")
        wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "implementing")
        self.assertEqual([t["agentId"] for t in self.relay.tasks.values()], ["pi-deepseek-factory"])


if __name__ == "__main__":
    unittest.main()
