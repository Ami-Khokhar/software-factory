"""agent-relay's HTTP API (src/server.py): submit, read and cancel tasks. Never waits."""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path


class RelayError(Exception):
    pass


def token():
    """The relay's API token, found as agent-relay's own MCP client finds it: AGENT_RELAY_TOKEN,
    else the file at AGENT_RELAY_TOKEN_FILE (default ~/.config/agent-relay/token), which the relay
    writes on its first start. Read on every call: launchd starts the relay and factory-mcp
    together, so the file may appear after factory-mcp starts. None when neither exists, for a
    relay that takes no token. Never logged, printed or put in an error."""
    value = os.environ.get("AGENT_RELAY_TOKEN", "").strip()
    if value:
        return value
    path = os.environ.get("AGENT_RELAY_TOKEN_FILE") or Path.home() / ".config" / "agent-relay" / "token"
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


class Relay:
    def __init__(self, url):
        self.url = url

    def _call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {"content-type": "application/json"}
        if secret := token():
            headers["Authorization"] = f"Bearer {secret}"
        request = urllib.request.Request(f"{self.url}{path}", data, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as err:
            if err.code == 404 and method == "GET":
                return None
            raise RelayError(f"relay {err.code}: {err.read().decode('utf-8', 'replace')[:300]}") from None
        except urllib.error.URLError as err:
            raise RelayError(f"relay unreachable: {err.reason}") from None

    def submit(self, agent_id, text, request_id, timeout_ms, cwd=None):
        """POST /v1/tasks. A repeated request_id returns the existing task (relay idempotency).
        The task runs in `cwd` (checked against the agent's allowedRoots), else the agent's cwd."""
        body = {"agentId": agent_id, "input": text, "requestId": request_id, "timeoutMs": timeout_ms}
        if cwd:
            body["cwd"] = cwd
        return self._call("POST", "/v1/tasks", body)

    def get(self, task_id):
        """The task, or None when the relay no longer knows it (it keeps tasks in memory)."""
        return self._call("GET", f"/v1/tasks/{task_id}")

    def reload(self):
        """POST /v1/admin/reload: the relay re-reads agents.json, so a worker registered since it
        started is discovered without a restart. Localhost-only in the relay."""
        return self._call("POST", "/v1/admin/reload")
