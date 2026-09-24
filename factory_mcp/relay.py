"""agent-relay's HTTP API (src/server.py): submit, read and cancel tasks. Never waits."""
import json
import urllib.error
import urllib.request


class RelayError(Exception):
    pass


class Relay:
    def __init__(self, url):
        self.url = url

    def _call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"{self.url}{path}", data,
                                         {"content-type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as err:
            if err.code == 404 and method == "GET":
                return None
            raise RelayError(f"relay {err.code}: {err.read().decode('utf-8', 'replace')[:300]}") from None
        except urllib.error.URLError as err:
            raise RelayError(f"relay unreachable: {err.reason}") from None

    def submit(self, agent_id, text, request_id, timeout_ms):
        """POST /v1/tasks. A repeated request_id returns the existing task (relay idempotency).
        The task runs in the agent's own cwd from the relay registry."""
        return self._call("POST", "/v1/tasks", {"agentId": agent_id, "input": text,
                                                "requestId": request_id, "timeoutMs": timeout_ms})

    def get(self, task_id):
        """The task, or None when the relay no longer knows it (it keeps tasks in memory)."""
        return self._call("GET", f"/v1/tasks/{task_id}")
