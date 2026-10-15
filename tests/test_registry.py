"""Registering a sandboxed factory worker by copying an approved template (ADR-0077)."""
import json
import os
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

    def test_build_agent_refuses_an_unsafe_id_or_model(self):
        for worker_id, model in (("../escape", "ok-1"), ("has space", "ok-1"), ("-leading", "ok-1"),
                                 ("ok-1", "has space"), ("ok-1", "-leading-flag"), ("ok-1", "a/b")):
            with self.subTest(worker_id=worker_id, model=model):
                with self.assertRaises(ValueError):
                    registry.build_agent(TEMPLATE, worker_id, model)


class RegisterWorkerTest(unittest.TestCase):
    """factory_register_worker through the Foreman: the registry write, the refusals, and the
    guarantee that a registered worker is not dispatchable until the owner promotes it."""

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
        so an issue that names it still goes to a person, not to the new worker."""
        self.foreman.register_worker("pi-qwen-factory", "qwen-3-max", "day-desk")
        self.assertEqual([w for w, _ in self.foreman.config.workers],
                         ["pi-deepseek-factory", "pi-glm-factory", "opencode-glm-factory"])
        self.github.edit_issue(REPO, 7, f"Worker: pi-qwen-factory\n\n{ISSUE_BODY}")
        wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual(wi["state"], "human_waiting")
        self.assertIn("names pi-qwen-factory, which is not in FACTORY_WORKERS", wi["blockedOn"])
        self.assertEqual(self.relay.tasks, {})  # nothing was dispatched

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


if __name__ == "__main__":
    unittest.main()
