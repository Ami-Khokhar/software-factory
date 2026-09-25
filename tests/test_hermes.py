"""The Day Desk profile (tenant/hermes, ADR-0013): it sees the factory tools it needs, every write
waits for the owner's yes, and its shell cannot do what those tools guard."""
import fnmatch
import re
import unittest
from pathlib import Path

import yaml

from factory_mcp import tools

PROFILE = Path(__file__).resolve().parent.parent / "tenant" / "hermes"
CONFIG = yaml.safe_load((PROFILE / "config.yaml").read_text())
SERVER = CONFIG["mcp_servers"]["factory"]


class ProfileTest(unittest.TestCase):
    def test_day_desk_sees_the_tools_it_needs_and_asks_before_each_write(self):
        """Hermes asks the owner before any tool on an untrusted server that is not marked
        read-only (tests/test_mcp_server.py checks the marks). A new tool is added here on purpose."""
        self.assertEqual(SERVER["trust"], "untrusted")
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
        self.assertLessEqual(used, {env["name"] for env in manifest["env_requires"]})

    def test_the_shell_cannot_do_what_the_tools_guard(self):
        def denied(command):  # Hermes matches each rule as a case-insensitive glob on the command
            return any(fnmatch.fnmatchcase(command.lower(), rule.lower()) for rule in CONFIG["approvals"]["deny"])

        for command in ("factory retry o/r#7 'try the old path'", "factory call factory_tick",
                        "factory new o/r Title 'claim | check'", "factory add-repo o/r",
                        "gh pr merge 12 --squash", "gh label create factory:ready -R o/r",
                        "gh api -X POST repos/o/r/issues/7/labels", "cat ~/.config/factory-mcp/secrets",
                        "echo $FACTORY_MCP_TOKEN"):
            with self.subTest(command):
                self.assertTrue(denied(command))
        for command in ("factory status", "factory logs", "gh pr view 12", "git log -3"):
            with self.subTest(command):
                self.assertFalse(denied(command))


if __name__ == "__main__":
    unittest.main()
