"""The Day Desk profile (tenant/hermes, ADR-0013, ADR-0021): it sees the factory tools it needs,
every factory write waits for the owner's yes, and its only shell runs in a Docker sandbox that
gets the GitHub token and nothing else from the host."""
import re
import unittest
from pathlib import Path

import yaml

from factory_mcp import tools
from factory_mcp.config import Config

PROFILE = Path(__file__).resolve().parent.parent / "tenant" / "hermes"
CONFIG = yaml.safe_load((PROFILE / "config.yaml").read_text())
SERVER = CONFIG["mcp_servers"]["factory"]
TERMINAL = CONFIG["terminal"]


class ProfileTest(unittest.TestCase):
    def test_day_desk_reaches_factory_mcp_where_it_listens_by_default(self):
        self.assertEqual(SERVER["url"], f"http://127.0.0.1:{Config(mcp_token='t', repos=frozenset()).port}/mcp")

    def test_day_desk_sees_the_tools_it_needs_and_asks_before_each_write(self):
        """Hermes asks the owner before any tool on an untrusted server that is not marked
        read-only (tests/test_mcp_server.py checks the marks). A new tool is added here on purpose."""
        self.assertEqual(SERVER["trust"], "untrusted")
        self.assertIn("factory_bootstrap_repo", SERVER["tools"]["include"])
        self.assertIn("factory_start_plan", SERVER["tools"]["include"])
        real = set(tools.build(None, None))
        self.assertEqual(real - set(SERVER["tools"]["include"]), {"factory_tick", "factory_intake"})
        self.assertLessEqual(set(SERVER["tools"]["include"]), real)

    def test_the_skill_names_only_tools_day_desk_has(self):
        text = (PROFILE / "skills" / "software-factory" / "SKILL.md").read_text()
        self.assertEqual(yaml.safe_load(text.split("---")[1])["name"], "software-factory")
        self.assertLessEqual(set(re.findall(r"factory_[a-z_]+", text)), set(SERVER["tools"]["include"]))

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
        set of keys is fixed. A skill could register variables too, so the skill declares none."""
        self.assertEqual(TERMINAL["backend"], "docker")
        runbook = (PROFILE.parent.parent / "docs" / "RUNBOOK.md").read_text()
        self.assertIn(f"docker build -t {TERMINAL['docker_image']} ", runbook)
        self.assertIs(TERMINAL["container_persistent"], False)
        self.assertEqual(TERMINAL["env_passthrough"], ["DAYDESK_GITHUB_TOKEN"])
        self.assertLessEqual(set(TERMINAL), {"backend", "docker_image", "container_persistent", "cwd",
                                             "env_passthrough"})
        skill = yaml.safe_load((PROFILE / "skills" / "software-factory" / "SKILL.md").read_text()
                               .split("---")[1])
        self.assertFalse({"required_environment_variables", "required_credential_files", "setup",
                          "prerequisites"} & set(skill))

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
