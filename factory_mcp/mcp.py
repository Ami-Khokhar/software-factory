"""The MCP protocol: one JSON-RPC 2.0 message in, one response (or None) out. No I/O here.

Only what OpenBot's client uses is implemented: initialize, ping, tools/list and tools/call.
"""
import json
from dataclasses import dataclass
from typing import Callable

from . import __version__

# Newest first. The client's version is echoed when supported, as the specification asks.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


class ToolError(Exception):
    """A tool refused or failed. Reported to the caller as an isError result, not a protocol error."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    effect: str  # "read" or "write"
    fn: Callable[[dict], dict]

    def listing(self):
        # Hermes asks the owner before running any tool without readOnlyHint (ADR-0013).
        return {"name": self.name, "description": self.description,
                "inputSchema": self.input_schema,
                "annotations": {"readOnlyHint": self.effect == "read"}}


def _result(message_id, result):
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id, code, text):
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}}


def handle(message, tools, audit=lambda name, args, ok: None):
    """Answer one message. Returns None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" \
            or not isinstance(message.get("method"), str):
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
    if "id" not in message:
        return None
    message_id, method = message["id"], message["method"]
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _error(message_id, -32602, "params must be an object")
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(message_id, {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "factory-mcp", "version": __version__},
        })
    if method == "ping":
        return _result(message_id, {})
    if method == "tools/list":
        return _result(message_id, {"tools": [tool.listing() for tool in tools.values()]})
    if method == "tools/call":
        tool = tools.get(params.get("name"))
        args = params.get("arguments") or {}
        if tool is None or not isinstance(args, dict):
            return _error(message_id, -32602, f"Unknown tool or bad arguments: {params.get('name')}")
        try:
            value, ok = tool.fn(args), True
        except ToolError as exc:
            value, ok = {"error": str(exc)}, False
        except Exception as exc:  # noqa: BLE001 - GitHub or relay failure: tell the caller, stay up
            value, ok = {"error": f"{type(exc).__name__}: {exc}"}, False
        audit(tool.name, args, ok)
        return _result(message_id, {"content": [{"type": "text", "text": json.dumps(value, indent=1)}],
                                    "isError": not ok})
    return _error(message_id, -32601, f"Method not found: {method}")
