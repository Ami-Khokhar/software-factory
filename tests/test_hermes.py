"""The Day Desk profile (tenant/hermes, ADR-0013): it sees the factory tools it needs, every write
waits for the owner's yes, and it has no shell or file tools to go around them."""
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

    def test_day_desk_has_no_way_around_the_factory_tools(self):
        """Hermes has no approval step for these toolsets, so they are off. The trial in
        docs/RUNBOOK.md checks the running gateway honours this."""
        self.assertLessEqual({"terminal", "code_execution", "file", "browser", "computer_use", "delegation"},
                             set(CONFIG["agent"]["disabled_toolsets"]))

if __name__ == "__main__":
    unittest.main()
