"""Observe rejected Claude protocol events without granting any tool permission.

The inspected v2 command uses a JSON schema in its system prompt, ``--tools ''``
and one turn. It does NOT request Claude's structured-output tool mechanism.
Consequently a tool_use block, including one named StructuredOutput, cannot be
accepted as a harmless schema return. Preserve the original rejection until its
actual protocol has been established. This overlay adds a documented deny-all
request flag, safe diagnostics, and the existing synthetic API-error repair.
It does not claim that the unobserved failing block was harmless.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib

VERSION = "opus-deny-all-and-observability-1"
TOOL_DENY_DOCUMENTATION = "https://code.claude.com/docs/en/cli-reference"
_MARKER = "_serial_opus_protocol_active"
_EVENT_TYPES = frozenset({"system", "assistant", "stream_event", "result",
                          "user", "rate_limit_event", "keep_alive"})
_STREAM_TYPES = frozenset({"message_start", "message_delta", "message_stop",
                           "content_block_start", "content_block_delta",
                           "content_block_stop", "ping"})
_BLOCK_TYPES = frozenset({"text", "thinking", "redacted_thinking", "tool_use",
                          "server_tool_use", "tool_result", "input_json_delta",
                          "text_delta", "thinking_delta", "signature_delta"})
# These names are diagnostic labels, never a permission allowlist.
_KNOWN_TOOL_NAMES = frozenset({"Bash", "Read", "Write", "Edit", "Glob", "Grep",
                               "WebSearch", "WebFetch", "Task", "Agent",
                               "TodoWrite", "StructuredOutput"})
_ERROR_CODES = frozenset({"tool_use_blocked", "claude_protocol_error",
                          "model_identity_mismatch", "incomplete_output"})


def _kind(value, known):
    return value if isinstance(value, str) and value in known else "unrecognized"


def _shape(value):
    if isinstance(value, dict):
        return {"type": "object", "field_count": len(value)}
    if isinstance(value, list):
        return {"type": "array", "item_count": len(value)}
    if value is None:
        return {"type": "null"}
    return {"type": {str: "string", bool: "boolean", int: "integer", float: "number"}.get(type(value), "other")}


def _block(block):
    if not isinstance(block, dict):
        return {"shape": _shape(block)}
    info = {"type": _kind(block.get("type"), _BLOCK_TYPES), "field_count": len(block)}
    name = block.get("name")
    if isinstance(name, str) and name in _KNOWN_TOOL_NAMES:
        info["tool_name"] = name
    elif isinstance(name, str):
        info["tool_name"] = "unrecognized"
        info["tool_name_sha256"] = hashlib.sha256(name.encode("utf-8", errors="replace")).hexdigest()
    if "id" in block:
        info["tool_id_present"] = True
    if "input" in block:
        info["input_shape"] = _shape(block["input"])
    if "partial_json" in block:
        info["partial_json_shape"] = _shape(block["partial_json"])
    # No content, arguments, IDs, arbitrary key names, or raw provider strings.
    return info


def describe_rejected_event(event):
    """Return only enum labels, counts and a hash of an unknown tool name."""
    if not isinstance(event, dict):
        return {"event_shape": _shape(event)}
    info = {"event_type": _kind(event.get("type"), _EVENT_TYPES),
            "field_count": len(event),
            "has_parent_tool_use_id": bool(event.get("parent_tool_use_id")),
            "has_tool_calls": bool(event.get("tool_calls")),
            "is_api_error_message": event.get("is_api_error_message") is True}
    inner = event.get("event")
    if event.get("type") == "stream_event" and isinstance(inner, dict):
        info["stream_type"] = _kind(inner.get("type"), _STREAM_TYPES)
        if "content_block" in inner:
            info["content_block"] = _block(inner["content_block"])
        if "delta" in inner:
            info["delta"] = _block(inner["delta"])
    message = event.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), list):
        blocks = message["content"]
        info["content_blocks"] = [_block(b) for b in blocks[:8]]
        info["content_block_count"] = len(blocks)
    if event.get("type") == "system":
        info["declared_tools_shape"] = _shape(event.get("tools"))
        info["declared_mcp_shape"] = _shape(event.get("mcp_servers"))
    return info


def _command_profile(backend):
    # Never log the real instructions, executable path, key, or command itself.
    command = backend._command("")
    def argument(flag):
        try:
            return command[command.index(flag) + 1]
        except (ValueError, IndexError):
            return None
    return {"json_schema_flag_present": "--json-schema" in command,
            "tools_flag_present": "--tools" in command,
            "tools_explicitly_empty": argument("--tools") == "",
            "deny_all_tools_requested": argument("--disallowedTools") == "*",
            "one_turn_requested": argument("--max-turns") == "1"}


@contextmanager
def install_opus_protocol(backends_module, *, recovery_transport_module=None):
    """Install a deny-all request flag, diagnostics and synthetic-error repair.

    Process scoped, no retry, no new accepted output/model/tool. Instantiate and
    call backends inside the context. Do not nest patched_opus_backend separately.
    Diagnostic data uses recovery_diagnostics so existing ledger safe_call keeps
    it. Genuine errors keep their original code and remain rejected.
    """
    if getattr(backends_module, _MARKER, False):
        raise RuntimeError("Opus protocol diagnostics already installed.")
    if recovery_transport_module is None:
        import recovery_transport as recovery_transport_module
    cls = backends_module.ClaudeCLIBackend
    BackendError = backends_module.BackendError
    setattr(backends_module, _MARKER, True)
    try:
        with recovery_transport_module.patched_opus_backend(backends_module):
            original_consume, original_decide, original_command = cls._consume, cls.decide, cls._command

            def command(self, instructions):
                # Official CLI reference: --tools affects built-ins, whereas
                # a bare --disallowedTools '*' removes every tool, including
                # MCP tools. subprocess receives an argv list: '*' is literal,
                # never shell expanded. No prompt/model/schema value changes.
                return [*original_command(self, instructions), "--disallowedTools", "*"]

            def consume(self, event, state, metadata):
                try:
                    return original_consume(self, event, state, metadata)
                except BackendError as error:
                    if error.code in _ERROR_CODES:
                        observation = {"version": VERSION,
                                       "error_code": error.code,
                                       "event": describe_rejected_event(event),
                                       "command_profile": _command_profile(self),
                                       "decision_accepted": False,
                                       "tool_permission_relaxed": False}
                        self._serial_opus_observation = observation
                        diagnostics = deepcopy(getattr(error, "recovery_diagnostics", {}))
                        diagnostics["opus_protocol"] = deepcopy(observation)
                        error.recovery_diagnostics = diagnostics
                    raise

            def decide(self, *args, **kwargs):
                self._serial_opus_observation = None
                before = len(self.calls)
                try:
                    return original_decide(self, *args, **kwargs)
                finally:
                    observation = self._serial_opus_observation
                    if observation is not None:
                        for call in self.calls[before:]:
                            call.setdefault("recovery_diagnostics", {})["opus_protocol"] = deepcopy(observation)

            cls._consume, cls.decide, cls._command = consume, decide, command
            try:
                yield
            finally:
                cls._consume, cls.decide, cls._command = original_consume, original_decide, original_command
    finally:
        delattr(backends_module, _MARKER)
