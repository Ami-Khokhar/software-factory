"""The Day Desk profile (tenant/hermes, ADR-0021, ADR-0030, ADR-0041): it drives the factory with the
agent token and runs the other factory tools on the owner's token, with no yes per call, and its
shell runs in a Docker sandbox that gets the GitHub token and nothing else from the host."""
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

    def test_day_desk_drives_with_the_agent_token_and_runs_the_rest_on_the_owner_token(self):
        """ADR-0030: the driving tools run on the agent token, which makes factory-mcp list only
        them, so the profile keeps no include list to go stale (ADR-0048). ADR-0041: the other
        tools run on the owner's token, also with no yes per call. A new tool is added here on
        purpose."""
        self.assertEqual((DRIVE["trust"], DRIVE["headers"]["Authorization"]), ("full", "Bearer ${FACTORY_AGENT_TOKEN}"))
        self.assertNotIn("tools", DRIVE)  # the token's AGENT_TOOLS is the list (ADR-0048)
        self.assertEqual((OWNER["trust"], OWNER["headers"]["Authorization"]), ("full", "Bearer ${FACTORY_MCP_TOKEN}"))
        driving = tools.build(None, None, agent=True)
        timeout = driving["factory_set_timeout"].listing()
        self.assertFalse(timeout["annotations"]["readOnlyHint"])
        self.assertFalse(timeout["inputSchema"]["additionalProperties"])
        self.assertTrue(driving["factory_runtime_status"].listing()["annotations"]["readOnlyHint"])
        real = set(tools.build(None, None))
        self.assertEqual(set(OWNER["tools"]["include"]), real - tools.AGENT_TOOLS - {"factory_tick", "factory_intake"})

    def test_the_skill_names_only_tools_day_desk_has(self):
        text = (PROFILE / "skills" / "software-factory" / "SKILL.md").read_text()
        self.assertEqual(yaml.safe_load(text.split("---")[1])["name"], "software-factory")
        self.assertLessEqual(set(re.findall(r"factory_[a-z_]+", text)),
                             set(tools.AGENT_TOOLS) | set(OWNER["tools"]["include"]))

    def test_the_install_asks_for_every_secret_the_config_uses(self):
        manifest = yaml.safe_load((PROFILE / "distribution.yaml").read_text())
        used = set(re.findall(r"\$\{([A-Z_]+)\}", (PROFILE / "config.yaml").read_text()))
        used |= set(TERMINAL["env_passthrough"])
        self.assertLessEqual(used, {env["name"] for env in manifest["env_requires"]})

    def test_the_gateway_is_standalone_so_the_sandbox_gets_its_token(self):
        """ADR-0041: on a gateway Hermes treats as multi-profile, the sandbox's token is read
        outside any secret scope and every shell call fails."""
        self.assertIs(CONFIG["gateway"]["standalone"], True)

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


    def drive_section(self):
        text = (PROFILE / "skills" / "software-factory" / "SKILL.md").read_text()
        return text[text.index("## Drive the factory"):text.index("## Teach the factory a lesson")]

    def test_the_drive_guidance_routes_a_scoped_remaining_item_through_the_allowance(self):
        """ADR-0069/ADR-0075: the driving job reads the current scoped approved allowances first,
        spends factory_use_allowance on an in-scope, actionable item with allowance and pass budget
        left, and reports only the exact factory_status readback of that one usage."""
        section = self.drive_section()
        route = section.index("factory_use_allowance")
        fallback = section.index("factory_resolve_human")
        self.assertLess(route, fallback)  # the allowance is the route when the facts line up
        for phrase in ("planAllowances", "planId", "`retry` ", "`remaining` at least 1",
                       "pass budget", "day-desk", "allowanceUse", "longer waiting",
                       "one lower", "readback of the one usage"):
            self.assertIn(phrase, section)

    def test_the_drive_guidance_sends_uncovered_and_unsafe_items_to_the_owner(self):
        """Absent, legacy-unscoped, out-of-scope, exhausted-allowance and exhausted-budget items
        cannot spend; safety/access/environment and unknown timeouts stay with the owner; a race
        or an unclear answer is reconciled by reading the history, never by a second spend."""
        section = self.drive_section()
        for phrase in ("without a scope", "`remaining` is 0", "buys no\n   fresh budget",
                       "environment limit", "protected path", "HUMAN verdict", "access trouble",
                       "unknown cause", "ownerAsked", "factory_resolve_human"):
            self.assertIn(phrase, section)
        self.assertIn("times out", section)  # no repeat before the history is read back

    def test_the_runbook_verifies_the_runtime_drive_job_and_representative_history(self):
        """Rollout checks the installed prompt, the deployed skill and the live job state, and the
        representative readback of the authorized history; scheduler ok alone is not success."""
        runbook = (PROFILE.parent.parent / "docs" / "RUNBOOK.md").read_text()
        for phrase in ("verify the runtime job, not only the source",
                       "hermes -p day-desk cron list", "mcp test factory_drive",
                       "factory_resolve_human", "allowanceUse", "allowance_retry",
                       "A green scheduler is not success", "blocker"):
            self.assertIn(phrase, runbook)


if __name__ == "__main__":
    unittest.main()
