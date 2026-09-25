"""Phase F: the tenant package and its policy (AGENT_COMPUTER_POLICY), evaluated like OpenBot does."""
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from factory_mcp import tools

TENANT = Path(__file__).resolve().parent.parent / "tenant" / "factory"
POLICY = json.loads((TENANT / "policy.json").read_text())


def holds(expression, context):
    """The CEL subset the policy uses (== != && || ! contains()), with OpenBot's fail-closed rule:
    anything that does not answer true or false counts as a match."""
    python = expression.replace("&&", " and ").replace("||", " or ")
    python = re.sub(r"!(?!=)", " not ", python)
    names = {key: SimpleNamespace(**value) for key, value in context.items()}
    names["contains"] = lambda haystack, needle: needle.lower() in str(haystack).lower()
    try:
        result = eval(python, {"__builtins__": {}}, names)  # the policy file is ours, not input
    except Exception:  # noqa: BLE001
        return True
    return result if isinstance(result, bool) else True


def allowed(bot, server="", tool="", initiator="person"):
    """evaluateActionPolicy: deny first, then allow, else refused. `server` empty = not an MCP call."""
    context = {"bot": {"id": bot}, "tool": {"name": f"mcp__{server}__{tool}" if server else "computer_click"},
               "mcp": {"server": server, "tool": tool, "effect": "write" if server else ""},
               "initiator": {"kind": initiator, "id": ""}}
    if any(holds(rule, context) for rule in POLICY["deny"]):
        return False
    return any(holds(rule, context) for rule in POLICY["allow"])


class PolicyTest(unittest.TestCase):
    def test_enforced(self):
        self.assertEqual(POLICY["mode"], "enforce")

    def test_decisions(self):
        cases = [
            (("factory-foreman", "factory-mcp", "factory_tick", "job"), True),
            (("factory-foreman", "factory-mcp", "factory_resolve_human"), True),
            (("factory-foreman", "factory-mcp", "factory_tick", "routine"), True),
            (("factory-foreman", "factory-mcp", "factory_intake", "routine"), False),
            (("factory-foreman", "factory-mcp", "factory_propose_issue"), False),
            (("factory-foreman", "github", "merge_pull_request"), False),
            (("factory-foreman", "factory-mcp", "factory_merge"), False),
            (("factory-foreman", "jira", "editJiraIssue"), False),
            (("factory-foreman", "routines", "create_routine"), True),
            (("factory-foreman", "routines", "create_routine", "job"), False),
            (("factory-foreman",), False),  # a computer action
            (("factory-planner", "factory-mcp", "factory_propose_issue"), True),
            (("factory-planner", "factory-mcp", "factory_tick"), False),
            (("someone-else", "factory-mcp", "factory_status"), False),
            # Day Desk reads; factory-ops holds the repo and start tools (plan.md realisation B)
            (("day-desk", "factory-mcp", "factory_status"), True),
            (("day-desk", "factory-mcp", "factory_repo_check"), True),
            (("day-desk", "factory-mcp", "factory_repo_status"), True),
            (("day-desk", "factory-mcp", "factory_add_repo"), False),
            (("day-desk", "factory-mcp", "factory_approve_issue"), False),
            (("day-desk", "factory-mcp", "factory_tick"), False),
            (("factory-ops", "factory-mcp", "factory_repo_check"), True),
            (("factory-ops", "factory-mcp", "factory_add_repo"), True),
            (("factory-ops", "factory-mcp", "factory_approve_issue"), True),
            (("factory-ops", "factory-mcp", "factory_approve_issue", "routine"), False),
            (("factory-ops", "factory-mcp", "factory_tick"), False),
            (("factory-ops", "factory-mcp", "factory_propose_issue"), False),
            (("factory-ops", "github", "create_repository"), False),
            (("factory-foreman", "factory-mcp", "factory_add_repo"), False),
            (("factory-planner", "factory-mcp", "factory_approve_issue"), False),
        ]
        for args, expected in cases:
            with self.subTest(call=args):
                self.assertEqual(allowed(*args), expected)


class PackageTest(unittest.TestCase):
    def test_required_files(self):
        for name in ("brand.yaml", "agents.yaml", "channels.yaml", "model.yaml", "knowledge.yaml"):
            self.assertTrue((TENANT / name).is_file(), name)

    def test_names_agree(self):
        agents = set(re.findall(r"^  - id: (\S+)", (TENANT / "agents.yaml").read_text(), re.M))
        self.assertEqual(agents, {"factory-foreman", "factory-planner", "factory-ops"})
        permitted = re.search(r"permitted_agents: \[(.*)\]", (TENANT / "channels.yaml").read_text())
        self.assertEqual({a.strip() for a in permitted.group(1).split(",")}, agents)
        real = set(tools.build(None, None))
        mentioned = set(re.findall(r"factory_[a-z_]+", (TENANT / "agents.yaml").read_text()
                                   + (TENANT / "policy.json").read_text()))
        self.assertEqual(mentioned, real)


if __name__ == "__main__":
    unittest.main()
