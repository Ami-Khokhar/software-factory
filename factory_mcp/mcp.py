"""The MCP protocol: one JSON-RPC 2.0 message in, one response (or None) out. No I/O here.

Only what the clients use is implemented: initialize, ping, tools/list and tools/call.
"""
import json
from dataclasses import dataclass
from typing import Callable

from . import __version__

# Newest first. The client's version is echoed when supported, as the specification asks.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

# A declared argument type, matched against what json.loads produced. bool is an int subclass in
# Python, so it is checked separately from integer and number.
JSON_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
              "array": list, "object": dict, "null": type(None)}


class ToolError(Exception):
    """A tool refused or failed. Reported to the caller as an isError result, not a protocol error."""


def _type_names(types):
    names = ["null" if kind == "null" else kind for kind in types] if isinstance(types, list) \
        else [types]
    return " or ".join(names)


def _valid_type(spec, value):
    kinds = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
    for kind in kinds:
        expected = JSON_TYPES[kind]
        if isinstance(value, expected) and not (isinstance(value, bool) and kind in
                                               ("integer", "number")):
            return True
    return False


def validate(tool, args):
    """Refuse, with a message that names the tool and the argument, what the tool's schema does
    not accept: a missing required argument, one it does not declare, or a value of the wrong
    type. Schema keywords beyond these (enum, min/max, nested items) stay with the tool itself."""
    schema = tool.input_schema
    properties = schema.get("properties") or {}
    for name in schema.get("required", ()):
        if name not in args:
            spec = properties.get(name) or {}
            shape = _type_names(spec.get("type", "object"))
            if spec.get("description"):
                shape += f" — {spec['description']}"
            raise ToolError(f"{tool.name} is missing the required argument {name}: {shape}")
    for name in args:
        spec = properties.get(name)
        if spec is None and schema.get("additionalProperties") is False:
            raise ToolError(f"{tool.name} does not take the argument {name}; it takes "
                            + (", ".join(sorted(properties)) or "no arguments"))
        if spec is not None and "type" in spec and not _valid_type(spec, args[name]):
            actual = type(args[name]).__name__
            raise ToolError(f"{tool.name}: {name} must be {_type_names(spec['type'])}, "
                            f"not {actual if actual != 'NoneType' else 'null'}")


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
        return _result(message_id, {"tools": [tool.listing() for tool in tools.values()],
                                    "ttlMs": 0, "cacheScope": "private"})
    if method == "tools/call":
        tool = tools.get(params.get("name"))
        args = params.get("arguments") or {}
        if tool is None or not isinstance(args, dict):
            return _error(message_id, -32602, f"Unknown tool or bad arguments: {params.get('name')}")
        try:
            validate(tool, args)
            value, ok = tool.fn(args), True
        except ToolError as exc:
            value, ok = {"error": str(exc)}, False
        except Exception as exc:  # noqa: BLE001 - GitHub or relay failure: tell the caller, stay up
            value, ok = {"error": f"{type(exc).__name__}: {exc}"}, False
        audit(tool.name, args, ok)
        return _result(message_id, {"content": [{"type": "text", "text": json.dumps(value, indent=1)}],
                                    "isError": not ok})
    return _error(message_id, -32601, f"Method not found: {method}")
