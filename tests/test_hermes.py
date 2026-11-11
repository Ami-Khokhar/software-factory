"""The Day Desk profile (tenant/hermes, ADR-0021, ADR-0030, ADR-0041): it drives the factory with the
agent token and runs the other factory tools on the owner's token, with no yes per call, and its
shell runs in a Docker sandbox that gets the GitHub token and nothing else from the host."""
import os
import re
import subprocess
import tempfile
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
    def test_dream_script_finds_factory_on_hermes_path(self):
        with tempfile.TemporaryDirectory() as home:
            local_bin = Path(home) / ".local" / "bin"
            local_bin.mkdir(parents=True)
            factory = local_bin / "factory"
            factory.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n")
            factory.chmod(0o755)
            env = dict(os.environ, HOME=home, PATH="/usr/bin:/bin", FACTORY_BIN="")
            result = subprocess.run(["/bin/sh", str(PROFILE / "scripts" / "factory-dream.sh")],
                                    check=True, capture_output=True, text=True, env=env)
            self.assertEqual(result.stdout.strip(), "call factory_dream_run {}")

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
        for name in ("factory_check_worker", "factory_reload_workers", "factory_set_worker_enabled"):
            listing = driving[name].listing()
            self.assertFalse(listing["annotations"]["readOnlyHint"])
            self.assertFalse(listing["inputSchema"]["additionalProperties"])
        self.assertTrue(driving["factory_runtime_status"].listing()["annotations"]["readOnlyHint"])
        self.assertTrue(driving["factory_project_status"].listing()["annotations"]["readOnlyHint"])
        self.assertTrue(driving["factory_memory_events"].listing()["annotations"]["readOnlyHint"])
        self.assertTrue(driving["factory_memory_search"].listing()["annotations"]["readOnlyHint"])
        self.assertTrue(driving["factory_dream_status"].listing()["annotations"]["readOnlyHint"])
        self.assertTrue(driving["factory_improvement_status"].listing()["annotations"]["readOnlyHint"])
        real = set(tools.build(None, None))
        owner_only = {"factory_extend_project", "factory_stop_project"}
        self.assertTrue(owner_only <= real - set(driving))
        # These owner CLI tools deliberately stay outside Day Desk's owner-server include.
        self.assertEqual(set(OWNER["tools"]["include"]),
                         real - tools.AGENT_TOOLS - {"factory_tick", "factory_intake"} - owner_only)

    def test_the_skill_names_only_tools_day_desk_has(self):
        text = (PROFILE / "skills" / "software-factory" / "SKILL.md").read_text()
        self.assertEqual(yaml.safe_load(text.split("---")[1])["name"], "software-factory")
        self.assertLessEqual(set(re.findall(r"factory_[a-z_]+", text)),
                             set(tools.AGENT_TOOLS) | set(OWNER["tools"]["include"]))

    def test_the_skill_has_the_lead_sections(self):
        text = (PROFILE / "skills" / "software-factory" / "SKILL.md").read_text()
        headings = ("## Lead a Project", "## Decide what the work needs", "## What goes to the owner")
        for heading in headings:
            self.assertIn(heading, text)
        lead = text[text.index(headings[0]):text.index(headings[1])]
        for number, step in enumerate(("Brainstorm.", "Brief.", "Blueprint.", "Plan.",
                                       "Start.", "Report the start"), 1):
            self.assertIn(f"{number}. **{step}**", lead)
        for phrase in ("2 sentences and one concrete example", "at most 3 questions",
                       "unless the owner asks", "at most 12 lines", "`doneWhen`",
                       "budget in passes", "deadline in IST", '"Say go to start."',
                       "Before the owner's go", "README, AGENTS.md, the tree of its source folder",
                       "files the goal touches", "`blueprint-template.md`", "at most about 400",
                       "`Paths:` lines", "functions, signatures and data shapes",
                       "- <claim> — done when: <a test name or command>", "what must not change",
                       "Set `after` only", "independent issues run in parallel", "at most 9 issues",
                       "factory adds the acceptance issue", "go, yes, start or an equal word",
                       "`factory_start_project`", "owner's exact message in `ownerWords`",
                       "Do not ask again", "Project id, the number of issues, the budget",
                       "expected finish in IST", "say nothing until the Project is done or paused"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, lead)
        owner = text[text.index(headings[2]):text.index("## What the factory can run")]
        self.assertEqual(len(re.findall(r"(?m)^- ", owner)), 4)
        for phrase in ("how to try the product", "budget, deadline or a failed acceptance end",
                       "cost to continue", "safety stop or an environment fault", "new paid provider",
                       "a secret", "protected controls such as `.github/`", "change to acceptance"):
            self.assertIn(phrase, owner)

    def test_the_skill_carries_principles_and_no_decision_table(self):
        """The factory's operator decides; a table of conditions is a workflow wearing a skill's name.
        The prompt says what each state means, what a pass costs and what may be decided, and it says
        the case it does not name is still the agent's (ADR-0136)."""
        text = (PROFILE / "skills" / "software-factory" / "SKILL.md").read_text()
        self.assertNotIn("What the attempts and state show", text)
        decide = text[text.index("## Decide what the work needs"):text.index("## What goes to the owner")]
        for phrase in ("is still yours", "What a state means", "What deciding costs",
                       "What you can decide", "`attempts.left`", "Spend a pass only to find out",
                       "read the WorkItem's `attempts` and the audit",
                       "reasons that do not fit together"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, decide)
        for tool in ("factory_resolve_human", "factory_set_worker", "factory_replace_issue",
                     "factory_approve_issue", "factory_answer_question"):
            self.assertIn(tool, decide)
    def test_the_skill_tells_day_desk_to_drain_the_queue_before_recovering_a_member(self):
        """A queued issue reaches no worker on its own (ADR-0135), so the skill carries the claim, the
        read before the decision, the approve, and the case that stays with the owner."""
        text = (PROFILE / "skills" / "software-factory" / "SKILL.md").read_text()
        self.assertIn("## Work the queue", text)
        queue = text[text.index("## Work the queue"):text.index("## Decide what the work needs")]
        self.assertLess(text.index("## Work the queue"), text.index("## Decide what the work needs"))
        for phrase in ("Drain the queue before you recover a member", "`factory_queue_next`",
                       '`by: "day-desk"`', "`factory:urgent` one first", "returns none when the queue is empty",
                       "`factory_status` and its id before you decide", "`factory_queue_approve`",
                       "leave it queued, do not approve it", "do not claim it again",
                       "You do not edit issues", "Never approve an issue you have not read"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, queue)
        self.assertLess(queue.index("`factory_queue_next`"), queue.index("`factory_queue_approve`"))

    def test_the_skill_names_only_project_tools_that_exist(self):
        folder = PROFILE / "skills" / "software-factory"
        real = set(tools.build(None, None))
        for name in ("SKILL.md", "blueprint-template.md"):
            with self.subTest(file=name):
                self.assertLessEqual(set(re.findall(r"\bfactory_[a-zA-Z0-9_]+\b",
                                                    (folder / name).read_text())), real)

    def test_the_skill_decides_members_from_the_packet_and_the_owner(self):
        """Replaces the test of the deleted decision table: a Project member is decided the same way as
        any other WorkItem, from the facts the claim carries, and every rule the table guaranteed that
        still holds is here — the safety stop and the environment fault, the gate's one silent retry,
        and the drive section's own rules, which the old test also guarded (ADR-0136)."""
        text = (PROFILE / "skills" / "software-factory" / "SKILL.md").read_text()
        recovery = text[text.index("## Decide what the work needs"):text.index("## What goes to the owner")]
        for phrase in ("`leadBlockedBy` names a safety stop or an environment fault",
                       "Retry the same failure with the same note twice",
                       "Spend a pass only to find out what the issue says",
                       "Ask the owner about ordinary work",
                       "retried once", "the second silence goes to the owner",
                       "`factory_status` says whether you may answer"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, recovery)
        # the drive rules the deleted table test guarded, which still live in the drive section
        drive = self.drive_section()
        for phrase in ("answer again even when the escalation is unchanged", "(non-members)",
                       "safety stop, an environment fault"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, drive)

    def test_the_blueprint_template_names_the_design_and_runtime_checks(self):
        text = (PROFILE / "skills" / "software-factory" / "blueprint-template.md").read_text()
        self.assertEqual(text.splitlines()[0], "At most 20000 characters. Name real paths and real "
                         "function names. A worker copies these names.")
        sections = re.findall(r"(?m)^## (.+)\n([^#]+)", text)
        self.assertEqual([heading for heading, _ in sections],
                         ["Goal and example", "Out of scope", "Modules", "Data shapes",
                          "Order of work", "Conventions", "Done when"])
        for heading, example in sections:
            with self.subTest(heading=heading):
                self.assertEqual(len(example.strip().splitlines()), 1)
        self.assertIn("`worker/runner.py`", text)
        self.assertIn("`ask_channel_from(text)`", text)
        self.assertIn("`id: str`, `text: str`, `askedAt: str`", text)
        self.assertIn("python3 -m unittest discover -s tests -p 'test_*.py'", text)

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
        """ADR-0069/ADR-0075/ADR-0095: the driving job reads the current scoped approved allowances
        first, then durably queues factory_queue_retry on an in-scope, actionable item with
        allowance and pass budget left, and reports only the factory_status readback of that
        queued request — never a recovery this phase did not run."""
        section = self.drive_section()
        route = section.index("factory_queue_retry")
        fallback = section.index("factory_resolve_human")
        self.assertLess(route, fallback)  # the allowance is the route when the facts line up
        for phrase in ("planAllowances", "planId", "`retry` ", "`remaining` at least 1",
                       "pass budget", "day-desk", "requestId", "pendingRetry", "workitem_busy",
                       "saved request id and `queued` status"):
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
