"""Stateless candidate-lawyer adapters; original Codex controls every other role.

No credentials, prompts, response text or reasoning are retained in call logs.
The adapters never retry or choose a fallback. CLI controls are application-level
isolation, not an operating-system egress firewall.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import tempfile
import time
from typing import Any

from court_agent.llm import (BackendError, OpenAIBackend, _check_strict_schema,
                             _json_object, _validate_value, _safe_api_error)
from court_agent.codex_backend import CodexCLIBackend

LAWYER_MODELS = {"seed": "doubao-seed-evolving", "deepseek": "deepseek-flash", "opus": "claude-opus-5-5"}
ENDPOINTS = {"seed": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
             "deepseek": "https://cf.api.fan/v1/messages", "opus": "https://cf.api.fan"}
CLAUDE_VERSION = "2.1.300"
SCHEMA_INSTRUCTION = (
    "\nReturn exactly one complete JSON object matching the following JSON Schema. "
    "Do not use markdown fences or add commentary. Do not invoke tools, inspect "
    "files, or retrieve external information. The courtroom controller executes "
    "authorized proposed actions separately.\nJSON Schema:\n"
)
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens")
COMPARISON_POLICY = {
    "protocol": "three-lawyers-reliable-v2",
    "http_lawyer_max_output_tokens": 16384,
    "lawyer_timeout_seconds": 300,
    "lawyer_max_attempts_per_decision": 2,
    "environment_max_attempts_per_decision": 2,
    "max_case_model_invocations": 240,
    "automatic_retries": "only_whitelisted_format_or_transient_errors",
    "retry_prompt": "identical_no_feedback",
    "attempts_persist_across_resume": True,
    "opus_output_token_cap_enforced": False,
}


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _usage(response: dict, protocol: str) -> dict:
    raw = response.get("usage") or {}
    if not isinstance(raw, dict):
        raw = {}
    if protocol == "chat":
        inp, out = _count(raw.get("prompt_tokens")), _count(raw.get("completion_tokens"))
        cached = _count((raw.get("prompt_tokens_details") or {}).get("cached_tokens"))
        reasoning = _count((raw.get("completion_tokens_details") or {}).get("reasoning_tokens"))
    else:
        cached = _count(raw.get("cache_read_input_tokens"))
        inp = _count(raw.get("input_tokens")) + cached + _count(raw.get("cache_creation_input_tokens"))
        out, reasoning = _count(raw.get("output_tokens")), _count(raw.get("reasoning_tokens"))
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
            "input_tokens_details": {"cached_tokens": cached},
            "output_tokens_details": {"reasoning_tokens": reasoning}}


def _reject_tools(value: Any) -> None:
    """Check typed API/CLI envelopes, never interpret tool names inside prose."""
    if isinstance(value, list):
        for child in value:
            _reject_tools(child)
    elif isinstance(value, dict):
        kind = value.get("type", "")
        if isinstance(kind, str) and ("tool" in kind or kind in {"function_call", "input_json_delta"}):
            raise BackendError("tool_use_blocked", "A provider emitted a forbidden tool event; the decision was rejected.")
        if value.get("tool_calls") or value.get("function_call") or value.get("parent_tool_use_id"):
            raise BackendError("tool_use_blocked", "A provider emitted a forbidden tool call; the decision was rejected.")
        for child in value.values():
            if isinstance(child, (dict, list)):
                _reject_tools(child)


class _LawyerBackend(OpenAIBackend):
    def __init__(self, config: dict, *, lawyer: str, api_key: str | None):
        super().__init__(config, api_key=api_key)
        if self.reasoning_effort != "medium":
            raise BackendError("invalid_config", "This comparison fixes requested reasoning_effort to medium.")
        self.lawyer = lawyer
        self.model = LAWYER_MODELS[lawyer]
        self.max_attempts = 1
        self.stats.update({"transport": "claude_cli" if lawyer == "opus" else "http",
                           "requested_reasoning_effort": "medium",
                           "provider_reasoning_effort": "high" if lawyer == "deepseek" else "medium",
                           "reasoning_effort_equivalent_across_providers": False,
                           "max_output_tokens_enforced": lawyer != "opus",
                           "configured_max_output_tokens": self.max_output_tokens,
                           "automatic_adapter_retries": 0, "fallback_allowed": False})
        if lawyer == "deepseek":
            self.stats["reasoning_mapping_note"] = "DeepSeek documents medium -> high; the proxy's forwarding is not independently verified."

    def check_credentials(self) -> None:
        if not self._api_key:
            raise BackendError("missing_api_key", "The selected lawyer requires its own explicitly supplied credential.")

    def _prepare(self, role_id: str, system: str, context: dict, schema: dict) -> tuple[str, str]:
        if role_id != "R_DEFENSE":
            raise BackendError("invalid_role", "A candidate backend may only decide R_DEFENSE actions.")
        if not isinstance(system, str) or not isinstance(context, dict):
            raise BackendError("invalid_context", "Role instructions and an authorized context object are required.")
        _check_strict_schema(schema)
        try:
            payload = json.dumps(context, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            instructions = system + SCHEMA_INSTRUCTION + json.dumps(schema, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (ValueError, TypeError, RecursionError):
            raise BackendError("invalid_context", "Context and schema must be JSON-serializable.") from None
        if len(payload) + len(instructions) > self.max_input_chars:
            raise BackendError("input_budget_exceeded", "The role context exceeds max_input_chars; no content was truncated.")
        if self.stats["attempted_calls"] >= self.max_model_calls:
            raise BackendError("call_budget_exceeded", "The comparison reached its model-call budget.")
        self.check_credentials()
        return instructions, payload

    def _validate_identity(self, model: Any) -> None:
        accepted = {self.model}
        if self.lawyer == "seed":
            accepted.add("doubao-seed-evolving-latest-version")
        if model not in accepted:
            raise BackendError("model_identity_mismatch", "The provider did not report the requested model or its explicitly accepted alias.")

    def decide(self, role_id: str, system: str, context: dict, schema: dict) -> dict:
        instructions, payload = self._prepare(role_id, system, context, schema)
        self.stats["attempted_calls"] += 1
        started, error = time.monotonic(), None
        # Metadata stays available when validation fails, without retaining text.
        metadata: dict[str, Any] = {"model": None, "usage": {}}
        parsed = None
        try:
            output = self._invoke(instructions, payload, metadata)
            metadata["answer_text_characters"] = len(output) if isinstance(output, str) else 0
            parsed = _json_object(output)
            if not _validate_value(parsed, schema, schema):
                from reliability import schema_errors
                metadata["schema_errors"] = schema_errors(parsed, schema, _validate_value)
                raise BackendError("schema_mismatch", "The candidate action does not match the exact schema and was not accepted.")
        except BackendError as exc:
            error = exc
        except KeyboardInterrupt:
            error = BackendError("cancelled", "The candidate decision was interrupted.")
            raise
        except Exception as exc:
            error = _safe_api_error(exc)
        finally:
            self._record(role_id, self.model, 1, time.monotonic() - started, metadata, error)
            self.calls[-1].update({"transport": self.stats["transport"], "backend_route": "lawyer",
                "candidate": self.lawyer, "requested_reasoning_effort": "medium",
                "provider_reasoning_effort": self.stats["provider_reasoning_effort"],
                "reasoning_effort_equivalent_across_providers": False,
                "reported_model_matches_requested": metadata.get("reported_model_verified", False),
                "model_identity_verified": False,
                "model_identity_source": "provider_reported_only",
                "max_output_tokens_enforced": self.stats["max_output_tokens_enforced"],
                "http_attempt_count": None if self.lawyer == "opus" else 1})
            for field in ("provider_stop_reason", "answer_text_characters", "schema_errors",
                          "cli_notice_counts", "last_cli_event_shape"):
                if field in metadata:
                    self.calls[-1][field] = deepcopy(metadata[field])
            self.calls[-1]["configured_max_output_tokens"] = self.max_output_tokens
            if self.lawyer == "opus":
                self.calls[-1].update({"cli_version": CLAUDE_VERSION,
                                      "cli_reported_turns": metadata.get("cli_reported_turns"),
                                      "last_cli_event": metadata.get("last_cli_event")})
        if error:
            error.backend_route = "lawyer"
            raise error from None
        self.stats["successful_decisions"] += 1
        assert parsed is not None
        return parsed


class HTTPBackend(_LawyerBackend):
    """One stateless, no-redirect HTTP request; no SDK retry machinery."""
    def __init__(self, config: dict, *, lawyer: str, api_key: str | None):
        if lawyer not in {"seed", "deepseek"}:
            raise BackendError("invalid_config", "This HTTP adapter supports only Seed and DeepSeek.")
        super().__init__(config, lawyer=lawyer, api_key=api_key)

    def _request(self, body: dict) -> dict:
        try:
            import httpx
        except ImportError:
            raise BackendError("sdk_missing", "Install httpx to enable the candidate HTTP adapters.") from None
        headers = {"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"}
        if self.lawyer == "deepseek":
            headers["anthropic-version"] = "2023-06-01"
        async def request_once() -> dict:
            # Async cancellation closes the connection when the total deadline
            # expires, including while waiting for the first response byte.
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                         timeout=self.timeout_seconds) as client:
                async with client.stream("POST", ENDPOINTS[self.lawyer], headers=headers, json=body) as response:
                    if response.status_code != 200:
                        exc = RuntimeError()
                        exc.status_code = response.status_code
                        raise _safe_api_error(exc)
                    pieces, received = [], 0
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > 2_000_000:
                            raise BackendError("output_budget_exceeded", "The HTTP response exceeded the adapter byte limit.")
                        pieces.append(chunk)
            return _json_object(b"".join(pieces).decode("utf-8"))

        async def bounded_request() -> dict:
            return await asyncio.wait_for(request_once(), timeout=self.timeout_seconds)

        try:
            return asyncio.run(bounded_request())
        except TimeoutError:
            raise BackendError("api_timeout", "The HTTP decision exceeded its wall-time budget.") from None

    def _invoke(self, instructions: str, payload: str, metadata: dict) -> str:
        body = {"model": self.model, "messages": [{"role": "user", "content": payload}],
                "thinking": {"type": "enabled"}, "stream": False}
        if self.lawyer == "seed":
            body["messages"].insert(0, {"role": "system", "content": instructions})
            body.update({"reasoning_effort": "medium", "max_completion_tokens": self.max_output_tokens})
        else:
            # Official DeepSeek mapping: requested medium -> native high.
            body.update({"system": instructions, "output_config": {"effort": "high"},
                         "max_tokens": self.max_output_tokens})
        response = self._request(body)
        metadata.update({"model": response.get("model"), "id": response.get("id"),
                         "usage": _usage(response, "chat" if self.lawyer == "seed" else "messages")})
        choices = response.get("choices")
        first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        reason = first.get("finish_reason") if self.lawyer == "seed" else response.get("stop_reason")
        known = {"stop", "length", "content_filter", "tool_calls", "function_call", "end_turn", "max_tokens",
                 "stop_sequence", "tool_use", "pause_turn", "refusal", "model_context_window_exceeded"}
        metadata["provider_stop_reason"] = reason if isinstance(reason, str) and reason in known else "missing" if reason is None else "unrecognized"
        _reject_tools(response)
        self._validate_identity(response.get("model"))
        metadata["reported_model_verified"] = True
        if self.lawyer == "seed":
            choices = response.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise BackendError("invalid_response", "Seed returned an invalid choice envelope.")
            if choices[0].get("finish_reason") != "stop":
                raise BackendError("incomplete_output", "Seed did not finish its answer normally.")
            message = choices[0].get("message") or {}
            if message.get("refusal"):
                raise BackendError("model_refusal", "Seed declined the decision.")
            text = message.get("content")
        else:
            if response.get("stop_reason") != "end_turn":
                raise BackendError("incomplete_output", "DeepSeek did not finish its answer normally.")
            content = response.get("content")
            if not isinstance(content, list) or any(not isinstance(b, dict) or b.get("type") not in {"text", "thinking", "redacted_thinking"} for b in content):
                raise BackendError("invalid_response", "DeepSeek returned an unsupported content block.")
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        if not isinstance(text, str) or not text.strip():
            raise BackendError("empty_output", "The candidate returned no action text.")
        return text


class ClaudeCLIBackend(_LawyerBackend):
    def __init__(self, config: dict, *, api_key: str | None, executable: str):
        super().__init__(config, lawyer="opus", api_key=api_key)
        self.executable = executable
        self._resolved_executable = None
        self._active_process = None
        self.stats.update({"cli_version": CLAUDE_VERSION, "http_attempt_count": None,
                           "attempted_calls_unit": "claude_cli_invocations"})

    def _environment(self, directory: str, *, include_key: bool) -> dict:
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
        env.update({"CLAUDE_CONFIG_DIR": directory, "ANTHROPIC_BASE_URL": ENDPOINTS["opus"],
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1", "CLAUDE_CODE_MAX_RETRIES": "0",
            "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1", "API_TIMEOUT_MS": str(self.timeout_seconds * 1000)})
        if include_key:
            env["ANTHROPIC_AUTH_TOKEN"] = self._api_key
        return env

    def check_credentials(self) -> None:
        super().check_credentials()
        if self._resolved_executable:
            return
        binary = shutil.which(self.executable)
        if not binary:
            raise BackendError("claude_missing", "Install the official Claude CLI 2.1.300 or specify its executable path.")
        with tempfile.TemporaryDirectory(prefix="court-claude-check-") as directory:
            try:
                result = subprocess.run([binary, "--version"], cwd=directory,
                    env=self._environment(directory, include_key=False), capture_output=True, text=True, timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                raise BackendError("claude_preflight_failed", "Could not inspect the installed Claude CLI version.") from None
        if result.returncode or not re.search(r"\b2\.1\.300\b", result.stdout):
            raise BackendError("claude_version_mismatch", "This adapter was validated for official Claude CLI 2.1.300 only.")
        self._resolved_executable = str(Path(binary).resolve())

    def _command(self, instructions: str) -> list[str]:
        return [self._resolved_executable, "--bare", "-p", "--model", self.model,
            "--effort", "medium", "--tools", "", "--disable-slash-commands", "--no-chrome",
            "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--no-session-persistence", "--max-turns", "1", "--output-format", "stream-json",
            "--verbose", "--include-partial-messages", "--system-prompt", instructions]

    @staticmethod
    def _operational_notice(event: dict, metadata: dict) -> bool:
        """Recognize only audited 2.1.300 telemetry; never complete a model turn.

        Shapes were checked against the genuine pinned CLI's SDK schema and
        emitters. Thinking estimates are progress only, never billed usage.
        Tools are rejected recursively by _consume before this method runs.
        """
        kind, subtype = event.get("type"), event.get("subtype")
        if kind == "keep_alive":
            if set(event) != {"type"}:
                raise BackendError("claude_protocol_error", "Malformed Claude heartbeat.")
            category = "keep_alive"
        elif kind == "system" and subtype in {"ui_invalidate", "thinking_tokens", "turn_duration"}:
            common = {"type", "subtype", "uuid", "session_id"}
            if not common.issubset(event) or not all(isinstance(event[k], str) for k in common):
                raise BackendError("claude_protocol_error", "Malformed Claude telemetry envelope.")
            if subtype == "ui_invalidate":
                if set(event) - common - {"event", "prompts_screened", "instances"} or event.get("event") != "ui.render":
                    raise BackendError("claude_protocol_error", "Malformed Claude UI refresh notification.")
                if "prompts_screened" in event:
                    if event["prompts_screened"] is not True:
                        raise BackendError("claude_protocol_error", "Malformed Claude input-screening flag.")
                    # A hook may alter the submitted prompt: this is not a
                    # display-only condition in the controlled benchmark.
                    raise BackendError("claude_prompt_screening_detected", "Claude reported possible prompt rewriting; the decision was rejected.")
                if "instances" in event:
                    instances = event["instances"]
                    fields = {"surface", "component", "instance_id"}
                    if not isinstance(instances, list) or any(not isinstance(item, dict) or set(item) != fields or
                            not all(isinstance(value, str) for value in item.values()) for item in instances):
                        raise BackendError("claude_protocol_error", "Malformed Claude UI instance list.")
            elif subtype == "thinking_tokens":
                required = {"estimated_tokens", "estimated_tokens_delta"}
                if not required.issubset(event) or set(event) - common - required - {"user_message_uuid"} or any(
                        type(event[field]) is not int for field in required) or (
                        "user_message_uuid" in event and not isinstance(event["user_message_uuid"], str)):
                    raise BackendError("claude_protocol_error", "Malformed Claude thinking-progress notification.")
            else:
                fields = {"duration_ms", "budget_tokens", "budget_limit", "budget_nudges", "message_count",
                          "pending_background_agent_count", "pending_workflow_count"}
                if "duration_ms" not in event or set(event) - common - fields or any(
                        type(event[field]) is not int for field in fields & event.keys()):
                    raise BackendError("claude_protocol_error", "Malformed Claude turn-duration notification.")
                if event.get("pending_background_agent_count", 0) or event.get("pending_workflow_count", 0):
                    raise BackendError("tool_use_blocked", "Claude reported forbidden background agent or workflow activity.")
                if event.get("budget_nudges", 0):
                    raise BackendError("claude_prompt_changed", "Claude reported an additional budget nudge; the decision was rejected.")
            category = subtype
        else:
            return False
        counts = metadata.setdefault("cli_notice_counts", {})
        counts[category] = counts.get(category, 0) + 1
        return True

    def _consume(self, event: dict, state: dict, metadata: dict) -> None:
        # Only protocol enum-shaped labels are retained for troubleshooting;
        # never keep event content, error text, prompts, or provider payloads.
        def label(value):
            return value if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,48}", value) else None
        metadata["last_cli_event"] = {"type": label(event.get("type")),
                                      "subtype": label(event.get("subtype"))}
        if event.get("type") == "stream_event" and isinstance(event.get("event"), dict):
            metadata["last_cli_event"]["stream_type"] = label(event["event"].get("type"))
        # Keep types of known protocol field names, not unknown key names or
        # values that might contain secrets, reasoning, or submitted text.
        safe_fields = {"type", "subtype", "uuid", "session_id", "event", "status", "content", "message",
                       "estimated_tokens", "estimated_tokens_delta", "user_message_uuid", "duration_ms",
                       "prompts_screened", "instances", "model", "usage", "stop_reason", "result"}
        metadata["last_cli_event_shape"] = {key: type(value).__name__ for key, value in event.items() if key in safe_fields}
        metadata["last_cli_event_shape"]["unknown_field_count"] = len(set(event) - safe_fields)
        _reject_tools(event)
        if self._operational_notice(event, metadata):
            return
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "status" and event.get("status") == "requesting":
            # Claude 2.1.300's stream_request_start serializer emits this exact
            # lifecycle status before contacting the API. It grants no tools.
            if set(event) - {"type", "subtype", "status", "uuid", "session_id"}:
                raise BackendError("claude_protocol_error", "Claude emitted an unsupported request-start status shape.")
            return
        if kind == "system" and event.get("subtype") == "init":
            if event.get("tools") or event.get("mcp_servers"):
                raise BackendError("tool_use_blocked", "Claude initialized forbidden tool capabilities.")
            self._validate_identity(event.get("model"))
            return
        if kind == "assistant":
            message = event.get("message")
            if not isinstance(message, dict):
                raise BackendError("claude_protocol_error", "Claude emitted an invalid assistant envelope.")
            self._validate_identity(message.get("model"))
            blocks = message.get("content", [])
            if any(not isinstance(b, dict) or b.get("type") not in {"text", "thinking", "redacted_thinking"} for b in blocks):
                raise BackendError("claude_protocol_error", "Claude emitted an unsupported content block.")
            metadata["model"] = message["model"]
            if message.get("stop_reason") is not None:
                if message["stop_reason"] != "end_turn":
                    raise BackendError("incomplete_output", "Claude did not finish its answer normally.")
                state["stop_reason"] = "end_turn"
            return
        if kind == "stream_event":
            inner = event.get("event", {})
            if inner.get("type") not in {"message_start", "message_delta", "message_stop", "content_block_start", "content_block_delta", "content_block_stop", "ping"}:
                raise BackendError("claude_protocol_error", "Claude emitted an unsupported streaming event.")
            if inner.get("type") == "message_start":
                self._validate_identity(inner.get("message", {}).get("model"))
            if inner.get("type") == "message_delta" and inner.get("delta", {}).get("stop_reason") is not None:
                if inner["delta"]["stop_reason"] != "end_turn":
                    raise BackendError("incomplete_output", "Claude did not finish its answer normally.")
                state["stop_reason"] = "end_turn"
            return
        if kind == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
            # Official CLI emits this operational notice even on a completed
            # request. It neither invokes a tool nor authorizes a retry.
            return
        if kind == "result":
            if state.get("result_seen"):
                raise BackendError("claude_protocol_error", "Claude emitted multiple final results.")
            state["result_seen"] = True
            metadata["usage"] = _usage(event, "messages")
            metadata["cli_reported_turns"] = event.get("num_turns")
            if event.get("is_error") or event.get("subtype") != "success":
                # Classify common errors without emitting diagnostics or prompts.
                detail = json.dumps(event, ensure_ascii=False).lower()
                code = "permission_denied" if "403" in detail or "forbidden" in detail else "claude_request_failed"
                raise BackendError(code, "The official Claude CLI did not complete the request; no retry or fallback was made.")
            if event.get("num_turns") != 1:
                raise BackendError("claude_turn_budget", "Claude did not report exactly one model turn.")
            # Official SDK schema: with partial messages, assistant blocks can
            # have null stop_reason; the final turn stop_reason is on result.
            final_stop = event.get("stop_reason", state.get("stop_reason"))
            if final_stop != "end_turn":
                raise BackendError("incomplete_output", "Claude did not report a normal end-of-turn stop reason.")
            models = event.get("modelUsage")
            if not isinstance(models, dict) or set(models) != {self.model}:
                raise BackendError("model_identity_mismatch", "Claude did not report usage for exactly the requested model.")
            metadata["model"] = self.model
            metadata["reported_model_verified"] = True
            state["text"] = event.get("result")
            return
        raise BackendError("claude_protocol_error", "Claude emitted an unsupported event; the decision was rejected.")

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        CodexCLIBackend._stop(process)

    def _invoke(self, instructions: str, payload: str, metadata: dict) -> str:
        state: dict = {}
        with tempfile.TemporaryDirectory(prefix="court-opus-") as directory:
            config_dir = Path(directory) / "config"
            config_dir.mkdir(mode=0o700)
            process = subprocess.Popen(self._command(instructions), cwd=directory,
                env=self._environment(str(config_dir), include_key=True), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            self._active_process = process
            pending, buffer, received, started = memoryview(payload.encode("utf-8")), b"", 0, time.monotonic()
            try:
                with selectors.DefaultSelector() as selector:
                    for stream, kind, events in ((process.stdin, "stdin", selectors.EVENT_WRITE),
                            (process.stdout, "stdout", selectors.EVENT_READ), (process.stderr, "stderr", selectors.EVENT_READ)):
                        os.set_blocking(stream.fileno(), False)
                        selector.register(stream, events, kind)
                    while selector.get_map():
                        if time.monotonic() - started > self.timeout_seconds:
                            raise BackendError("api_timeout", "Claude exceeded its wall-time budget and was stopped.")
                        for entry, _ in selector.select(timeout=0.2):
                            stream = entry.fileobj
                            if entry.data == "stdin":
                                try:
                                    pending = pending[os.write(stream.fileno(), pending[:16384]):]
                                except BrokenPipeError:
                                    pending = pending[:0]
                                if not pending:
                                    selector.unregister(stream)
                                    stream.close()
                                continue
                            chunk = os.read(stream.fileno(), 65536)
                            if not chunk:
                                selector.unregister(stream)
                                continue
                            received += len(chunk)
                            if received > 2_000_000:
                                raise BackendError("output_budget_exceeded", "Claude exceeded its event-byte budget and was stopped.")
                            if entry.data == "stderr":
                                continue  # Never retain potentially sensitive diagnostics.
                            buffer += chunk
                            while b"\n" in buffer:
                                raw, buffer = buffer.split(b"\n", 1)
                                if raw.strip():
                                    self._consume(_json_object(raw.decode("utf-8")), state, metadata)
                    if buffer.strip():
                        self._consume(_json_object(buffer.decode("utf-8")), state, metadata)
                    remaining = self.timeout_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        raise BackendError("api_timeout", "Claude exceeded its wall-time budget.")
                    try:
                        returncode = process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        raise BackendError("api_timeout", "Claude did not finish within its wall-time budget.") from None
                    if returncode or not state.get("result_seen"):
                        raise BackendError("claude_request_failed", "Claude exited without a successful result.")
            finally:
                self._stop(process)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
                self._active_process = None
        text = state.get("text")
        if not isinstance(text, str) or not text.strip():
            raise BackendError("empty_output", "Claude returned no action text.")
        return text

    def close(self) -> None:
        if self._active_process is not None:
            self._stop(self._active_process)
            self._active_process = None
        self._api_key = None


class RoutedBackend:
    def __init__(self, lawyer_backend, environment_backend, max_model_calls: int):
        self.lawyer_backend, self.environment_backend = lawyer_backend, environment_backend
        self.max_model_calls = max_model_calls
        self.calls: list[dict] = []

    @property
    def stats(self) -> dict:
        children = {"lawyer": self.lawyer_backend.stats, "environment": self.environment_backend.stats}
        fields = ("attempted_calls", "successful_decisions", "failed_attempts", "latency_seconds", *TOKEN_FIELDS)
        result = {field: sum(child.get(field, 0) for child in children.values()) for field in fields}
        result.update({"transport": "role_routed", "by_route": deepcopy(children), "by_model": {}})
        for child in children.values():
            for model, values in child.get("by_model", {}).items():
                target = result["by_model"].setdefault(model, {})
                for key, value in values.items():
                    target[key] = target.get(key, 0) + value
        return result

    def check_credentials(self) -> None:
        self.lawyer_backend.check_credentials()
        self.environment_backend.check_credentials()

    def decide(self, role_id: str, system: str, context: dict, schema: dict) -> dict:
        if self.stats["attempted_calls"] >= self.max_model_calls:
            raise BackendError("call_budget_exceeded", "The shared run model-call budget is exhausted.")
        route = "lawyer" if role_id == "R_DEFENSE" else "environment"
        backend = self.lawyer_backend if route == "lawyer" else self.environment_backend
        before = len(backend.calls)
        try:
            return backend.decide(role_id, system, context, schema)
        except BackendError as exc:
            exc.backend_route = route
            raise
        finally:
            for call in backend.calls[before:]:
                self.calls.append({**deepcopy(call), "backend_route": route})

    def close(self) -> None:
        try:
            self.lawyer_backend.close()
        finally:
            self.environment_backend.close()


def create_backend(config: dict, keys: dict, lawyer: str, project_root: str | Path,
                   claude_executable: str = "claude", *, attempt_ledger: Path | None = None,
                   candidate_policy: bool = False) -> RoutedBackend:
    """Construct lazily. Keys stay only in memory; no key/endpoint probing occurs."""
    if lawyer not in LAWYER_MODELS:
        raise BackendError("invalid_config", "lawyer must be seed, deepseek or opus.")
    environment_config = deepcopy(config)
    environment_config["models"] = {"lawyer": "gpt-5.6-sol", "environment": "gpt-5.6-sol"}
    environment_config["max_attempts"] = 1
    environment_config.setdefault("codex", {})
    executable = environment_config["codex"].get("executable", "codex")
    if not Path(executable).is_absolute() and ("/" in executable or "\\" in executable):
        environment_config["codex"]["executable"] = str((Path(project_root) / executable).resolve())
    environment = CodexCLIBackend(environment_config, api_key=keys.get("gpt"))
    candidate_config = deepcopy(config)
    if candidate_policy:
        if attempt_ledger is None:
            raise BackendError("missing_attempt_ledger", "The v2 court requires a persistent per-case attempt ledger.")
        candidate_config.update(max_output_tokens=COMPARISON_POLICY["http_lawyer_max_output_tokens"],
                                timeout_seconds=COMPARISON_POLICY["lawyer_timeout_seconds"],
                                max_model_calls=COMPARISON_POLICY["max_case_model_invocations"])
        # Role prompts, model IDs, context and reasoning settings are unchanged.
        environment.max_model_calls = COMPARISON_POLICY["max_case_model_invocations"]
    candidate = (ClaudeCLIBackend(candidate_config, api_key=keys.get("opus"), executable=claude_executable)
                 if lawyer == "opus" else HTTPBackend(candidate_config, lawyer=lawyer, api_key=keys.get(lawyer)))
    routed = RoutedBackend(candidate, environment, COMPARISON_POLICY["max_case_model_invocations"] if candidate_policy else candidate.max_model_calls)
    if candidate_policy:
        from reliability import ReliableBackend
        return ReliableBackend(routed, Path(attempt_ledger), COMPARISON_POLICY)
    return routed
