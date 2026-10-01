"""The Day Desk profile (tenant/hermes, ADR-0013, ADR-0021, ADR-0030): it drives the factory with the
agent token, every other factory write waits for the owner's yes, and its only shell runs in a
Docker sandbox that gets the GitHub token and nothing else from the host."""
import re
import unittest
from pathlib import Path

import yaml

from factory_mcp import tools
from factory_mcp.config import Config

PROFILE = Path(__file__).resolve().parent.parent / "tenant" / "hermes"
CONFIG = yaml.safe_load((PROFILE / "config.yaml").read_text())
DRIVE, OWNER = CONFIG["mcp_servers"]["factory_drive"], CONFIG["mcp_servers"]["factory"]
TERMINAL = CONFIG["terminal"]


class ProfileTest(unittest.TestCase):
    def test_day_desk_reaches_factory_mcp_where_it_listens_by_default(self):
        for server in (DRIVE, OWNER):
            self.assertEqual(server["url"], f"http://127.0.0.1:{Config(mcp_token='t', repos=frozenset()).port}/mcp")

    def test_day_desk_drives_with_the_agent_token_and_asks_the_owner_for_the_rest(self):
        """ADR-0030: the driving tools run with no yes per call, on the agent token, which makes
        factory-mcp list only them; the other writes wait for the owner on an untrusted server. A
        new tool is added here on purpose."""
        self.assertEqual((DRIVE["trust"], DRIVE["headers"]["Authorization"]), ("full", "Bearer ${FACTORY_AGENT_TOKEN}"))
        self.assertEqual(set(DRIVE["tools"]["include"]), tools.AGENT_TOOLS)
        self.assertEqual((OWNER["trust"], OWNER["headers"]["Authorization"]), ("untrusted", "Bearer ${FACTORY_MCP_TOKEN}"))
        real = set(tools.build(None, None))
        self.assertEqual(set(OWNER["tools"]["include"]), real - tools.AGENT_TOOLS - {"factory_tick", "factory_intake"})

    def test_the_skill_names_only_tools_day_desk_has(self):
        text = (PROFILE / "skills" / "software-factory" / "SKILL.md").read_text()
        self.assertEqual(yaml.safe_load(text.split("---")[1])["name"], "software-factory")
        self.assertLessEqual(set(re.findall(r"factory_[a-z_]+", text)),
                             set(DRIVE["tools"]["include"]) | set(OWNER["tools"]["include"]))

    def test_the_install_asks_for_every_secret_the_config_uses(self):
        manifest = yaml.safe_load((PROFILE / "distribution.yaml").read_text())
        used = set(re.findall(r"\$\{([A-Z_]+)\}", (PROFILE / "config.yaml").read_text()))
        used |= set(TERMINAL["env_passthrough"])
        self.assertLessEqual(used, {env["name"] for env in manifest["env_requires"]})

    def test_only_the_shell_is_on_and_the_host_side_toolsets_stay_off(self):
        """The shell runs in the sandbox (ADR-0021). The rest run on the host with no approval
        step, so they are off. The trial in docs/RUNBOOK.md checks the running gateway."""
        disabled = set(CONFIG["agent"]["disabled_toolsets"])
        self.assertNotIn("terminal", disabled)
        self.assertLessEqual({"code_execution", "file", "browser", "computer_use", "delegation"}, disabled)

    def test_the_shell_runs_in_docker_with_the_github_token_as_its_only_secret(self):
        """Any other terminal key could mount a host path or forward another variable, so the
        set of keys is fixed. A skill could register variables too, at the top of its frontmatter
        or under metadata.hermes, so the skill declares none in either place."""
        self.assertEqual(TERMINAL["backend"], "docker")
        runbook = (PROFILE.parent.parent / "docs" / "RUNBOOK.md").read_text()
        self.assertIn(f"docker build -t {TERMINAL['docker_image']} ", runbook)
        self.assertIs(TERMINAL["container_persistent"], False)
        self.assertEqual(TERMINAL["env_passthrough"], ["DAYDESK_GITHUB_TOKEN"])
        self.assertLessEqual(set(TERMINAL), {"backend", "docker_image", "container_persistent", "cwd",
                                             "env_passthrough"})
        skill = yaml.safe_load((PROFILE / "skills" / "software-factory" / "SKILL.md").read_text()
                               .split("---")[1])
        hermes = (skill.get("metadata") or {}).get("hermes") or {}
        for declared in (set(skill), set(hermes)):
            self.assertFalse({"required_environment_variables", "required_credential_files", "setup",
                              "prerequisites"} & declared)

    def test_the_sandbox_hands_gh_the_token_the_config_passes_in(self):
        dockerfile = (PROFILE / "sandbox" / "Dockerfile").read_text()
        self.assertIn(f'GH_TOKEN="${TERMINAL["env_passthrough"][0]}" exec /usr/bin/gh', dockerfile)
        self.assertRegex(dockerfile, r"(?m)^USER daydesk$")
        self.assertIn(f"--home-dir {TERMINAL['cwd']} ", dockerfile)

    def test_a_first_commit_in_the_sandbox_is_on_main(self):
        """For an empty repo, the first branch pushed becomes the default one (ADR-0008)."""
        dockerfile = (PROFILE / "sandbox" / "Dockerfile").read_text()
        self.assertRegex(dockerfile, r"\[init\]\n\tdefaultBranch = main\n")


if __name__ == "__main__":
    unittest.main()
