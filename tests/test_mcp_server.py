"""Speak Streamable HTTP to a real factory-mcp server on an ephemeral port."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone

from factory_mcp.config import Config
from factory_mcp.server import make_server
from factory_mcp.store import Store
from tests.fakes import workitem

TOKEN = "test-token"
AGENT = "agent-token"


class McpServerTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        config = Config(mcp_token=TOKEN, repos=frozenset({"o/r"}), port=0, agent_token=AGENT)
        self.server = make_server(config, self.store)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, body, headers=None, method="POST"):
        headers = {"Authorization": f"Bearer {TOKEN}", "content-type": "application/json",
                   "accept": "application/json, text/event-stream", **(headers or {})}
        request = urllib.request.Request(self.url, json.dumps(body).encode(), headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            return err.code, None

    def rpc(self, method, params=None, token=TOKEN):
        status, body = self.post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                                 {"Authorization": f"Bearer {token}"})
        self.assertEqual(status, 200)
        return body

    def test_agent_reads_memory_evidence_over_http_but_cannot_read_disabled_repos(self):
        self.store.put(workitem())
        self.store.log("attempt_failed", {"reason": "verification failed"}, "o/r#1")
        for repo, refused in (("o/r", False), ("another/private", True)):
            result = self.rpc("tools/call", {"name": "factory_memory_events",
                                           "arguments": {"repo": repo}}, token=AGENT)["result"]
            self.assertEqual(result["isError"], refused)
            if not refused:
                events = json.loads(result["content"][0]["text"])["events"]
                self.assertEqual(events[0]["detail"]["reason"], "verification failed")

    def test_dream_status_returns_ready_and_cooldown_over_http(self):
        self.store.put(workitem())
        self.store.log("attempt_failed", {"reason": "verification failed"}, "o/r#1")
        response = self.rpc("tools/call", {"name": "factory_dream_status", "arguments": {}})["result"]
        self.assertFalse(response["isError"])
        ready = json.loads(response["content"][0]["text"])
        self.assertEqual(ready, {"eligible": True, "reason": "ready", "nextEligibleAt": 0})

        claimed = self.store.dreaming.claim(self.server.foreman.config, datetime.now(timezone.utc))
        self.assertIsNotNone(claimed)
        response = self.rpc("tools/call", {"name": "factory_dream_status", "arguments": {}})["result"]
        cooldown = json.loads(response["content"][0]["text"])
        self.assertEqual(cooldown["reason"], "cooldown")
        self.assertEqual(cooldown["nextEligibleAt"], claimed["nextEligibleAt"])

    def test_memory_write_is_owner_only_and_audit_omits_content(self):
        args = {"repo": "o/r", "key": "presentation", "kind": "preference",
                "text": "Use concise diagrams", "source": "owner:message-1"}
        response = self.rpc("tools/call", {"name": "factory_memory_put", "arguments": args})
        self.assertFalse(response["result"]["isError"])
        trail = json.dumps(self.store.global_history())
        self.assertNotIn(args["text"], trail)
        self.assertNotIn(args["source"], trail)
        refused = self.rpc("tools/call", {"name": "factory_memory_put", "arguments": args}, token=AGENT)
        self.assertIn("error", refused)

    def test_memory_id_lookups_remain_in_the_global_tool_audit(self):
        args = {"repo": "o/r", "key": "presentation", "kind": "preference",
                "text": "Use concise diagrams", "source": "owner:message-1"}
        made = self.rpc("tools/call", {"name": "factory_memory_put", "arguments": args})
        node = json.loads(made["result"]["content"][0]["text"])
        result = self.rpc("tools/call", {"name": "factory_memory_search",
                         "arguments": {"repo": "o/r", "id": node["id"]}}, token=AGENT)
        self.assertFalse(result["result"]["isError"])
        calls = [row for row in self.store.global_history()
                 if row["action"] == "tool:factory_memory_search"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["detail"]["args"]["id"], node["id"])
        self.assertEqual(self.store.history(node["id"]), [])

    def test_handshake(self):
        result = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "hermes", "version": "1"}})["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "factory-mcp")
        self.assertIn("tools", result["capabilities"])
        status, body = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((status, body), (202, None))

    def test_discovery_is_private_and_expires_cached_schemas(self):
        result = self.rpc("tools/list")["result"]
        self.assertEqual((result["ttlMs"], result["cacheScope"]), (0, "private"))

    def test_every_tool_declares_whether_it_writes(self):
        """An MCP client decides from readOnlyHint whether to ask a person first (Hermes does), so a
        write tool marked read would skip that question. A new tool must be added here on purpose."""
        tools = self.rpc("tools/list")["result"]["tools"]
        reads = {t["name"] for t in tools if t["annotations"]["readOnlyHint"] is True}
        writes = {t["name"] for t in tools if t["annotations"]["readOnlyHint"] is False}
        self.assertEqual(reads, {"factory_runtime_status", "factory_status", "factory_repo_check", "factory_lessons",
                                 "factory_report", "factory_memory_events", "factory_memory_search", "factory_dream_status", "factory_improvement_status", "factory_docker_status", "factory_project_status"})
        self.assertEqual(writes, {"factory_improvement_validate", "factory_dream_run", "factory_memory_put", "factory_intake", "factory_tick", "factory_resolve_human",
                                  "factory_answer_question",
                                  "factory_propose_issue", "factory_add_repo", "factory_approve_issue",
                                  "factory_bootstrap_repo", "factory_approve_lesson",
                                  "factory_retire_lesson", "factory_start_plan",
                                  "factory_extend_project", "factory_stop_project",
                                  "factory_start_project",
                                  "factory_set_dependencies", "factory_replace_issue",
                                  "factory_docker_control", "factory_approve_plan_allowance",
                                  "factory_use_allowance", "factory_queue_retry", "factory_authorize_setup_merge",
                                  "factory_revoke_setup_merge", "factory_merge_setup_pr",
                                  "factory_set_worker", "factory_register_worker", "factory_set_timeout",
                                  "factory_check_worker", "factory_reload_workers"})
        self.assertTrue(all(t["inputSchema"]["type"] == "object" for t in tools))

    def test_the_agent_timeout_tool_persists_without_starting_a_worker(self):
        from tests.fakes import FakeRelay
        self.server.foreman.relay = FakeRelay()
        self.store.put(workitem(state="blocked", blockedOn="timeout"))
        response = self.rpc("tools/call", {"name": "factory_set_timeout", "arguments": {
            "id": "o/r#1", "wallMs": 3600000, "by": "owner"}}, token=AGENT)["result"]
        self.assertFalse(response["isError"])
        saved = json.loads(response["content"][0]["text"])["workItem"]
        self.assertEqual((saved["state"], saved["wallMsOverride"]), ("blocked", 3600000))
        self.assertEqual(self.store.workorders("o/r#1"), [])
        self.assertEqual(self.server.foreman.relay.tasks, {})

    def test_the_register_worker_tool_names_a_template_and_forbids_other_fields(self):
        """A caller may choose which approved sandboxed template to copy, and nothing else: the
        schema carries `template` and forbids additional properties, so a command, credential,
        env or image cannot ride along on the call."""
        tools = {t["name"]: t for t in self.rpc("tools/list")["result"]["tools"]}
        schema = tools["factory_register_worker"]["inputSchema"]
        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(set(schema["required"]), {"worker", "model", "by"})
        self.assertIn("template", schema["properties"])

    def test_the_agent_token_lists_and_runs_only_the_driving_tools(self):
        """Day Desk drives with FACTORY_AGENT_TOKEN (ADR-0030). The token, not the model, decides what
        it may call. A new tool joins AGENT_TOOLS on purpose."""
        listed = {t["name"] for t in self.rpc("tools/list", token=AGENT)["result"]["tools"]}
        self.assertEqual(listed, {"factory_runtime_status", "factory_status", "factory_repo_check", "factory_lessons", "factory_report", "factory_memory_events", "factory_memory_search", "factory_dream_status", "factory_improvement_status",
                                  "factory_approve_issue", "factory_propose_issue", "factory_start_plan",
                                  "factory_start_project",
                                  "factory_set_dependencies", "factory_replace_issue", "factory_resolve_human",
                                  "factory_answer_question",
                                  "factory_use_allowance", "factory_queue_retry", "factory_merge_setup_pr",
                                  "factory_docker_status", "factory_docker_control",
                                  "factory_set_worker", "factory_register_worker", "factory_set_timeout",
                                  "factory_check_worker", "factory_reload_workers",
                                  "factory_project_status"})
        refused = self.rpc("tools/call", {"name": "factory_bootstrap_repo",
                                          "arguments": {"repo": "o/r", "by": "day-desk"}}, token=AGENT)
        self.assertIn("error", refused)  # an unknown tool for this token, not a call that ran

    def test_worker_health_and_reload_are_bounded_driving_writes(self):
        from unittest import mock
        from tests.fakes import FakeRelay
        from factory_mcp import tools as factory_tools
        self.server.foreman.relay = FakeRelay()
        with mock.patch.object(factory_tools.worker_ops, "check", return_value={"status": "ok"}) as check:
            result = self.rpc("tools/call", {"name": "factory_check_worker", "arguments": {
                "worker": "opencode-mimo-factory", "by": "Day Desk"}}, token=AGENT)["result"]
        self.assertFalse(result["isError"])
        check.assert_called_once_with(self.store, self.server.foreman.config,
                                      "opencode-mimo-factory", "Day Desk", agent=True)
        result = self.rpc("tools/call", {"name": "factory_reload_workers", "arguments": {
            "by": "Day Desk"}}, token=AGENT)["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(self.server.foreman.relay.reloads, 1)
        self.assertEqual(self.server.foreman.relay.tasks, {})

    def test_a_call_with_the_agent_token_is_audited_as_the_agents(self):
        self.store.put(workitem())
        self.rpc("tools/call", {"name": "factory_status", "arguments": {"id": "o/r#1"}}, token=AGENT)
        self.assertEqual(self.store.history("o/r#1")[0]["detail"]["caller"], "agent")

    def test_a_queued_retry_tool_audit_sanitizes_its_evidence(self):
        self.server.audit["agent"]("factory_queue_retry",
                                    {"id": "o/r#1", "evidence": "TOKEN=ghp_SECRETabcdef"}, True)
        detail = self.store.history("o/r#1")[0]["detail"]
        self.assertEqual(detail["args"]["evidence"], "TOKEN=<redacted>")

    def test_status_call_is_audited(self):
        self.store.put(workitem())
        result = self.rpc("tools/call", {"name": "factory_status", "arguments": {}})["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["workItems"][0]["id"], "o/r#1")
        result = self.rpc("tools/call", {"name": "factory_status", "arguments": {"id": "o/r#1"}})["result"]
        self.assertEqual(json.loads(result["content"][0]["text"])["workItem"]["state"], "issued")
        self.assertEqual(self.store.history("o/r#1")[0]["action"], "tool:factory_status")
        missing = self.rpc("tools/call", {"name": "factory_status", "arguments": {"id": "o/r#9"}})["result"]
        self.assertTrue(missing["isError"])

    def test_a_summary_names_why_an_exhausted_workitem_stopped(self):
        self.store.put(dict(workitem(), state="exhausted", terminalReason="4 worker passes used"))
        result = self.rpc("tools/call", {"name": "factory_status", "arguments": {}})["result"]
        self.assertFalse(result["isError"])
        row = json.loads(result["content"][0]["text"])["workItems"][0]
        self.assertEqual(row["blockedOn"], "4 worker passes used")

    def test_a_summary_exposes_progress_fields_without_a_second_status_read(self):
        self.store.put(dict(workitem(), state="reviewing", prNumber=15, headSha="c2", gateRound=2,
                            worker="codex"))
        result = self.rpc("tools/call", {"name": "factory_status", "arguments": {}})["result"]
        row = json.loads(result["content"][0]["text"])["workItems"][0]
        self.assertEqual((row["headSha"], row["prNumber"], row["gateRound"], row["worker"]),
                         ("c2", 15, 2, "codex"))

    def test_protocol_errors(self):
        self.assertEqual(self.rpc("nope")["error"]["code"], -32601)
        self.assertEqual(self.rpc("tools/call", {"name": "factory_merge"})["error"]["code"], -32602)

    def test_http_guards(self):
        body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        self.assertEqual(self.post(body, {"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.post(body, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.post(body, method="GET")[0], 405)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")  # plain HTTP never leaves the machine


if __name__ == "__main__":
    unittest.main()
