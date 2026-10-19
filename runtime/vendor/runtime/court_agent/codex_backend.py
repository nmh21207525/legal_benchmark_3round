"""Role decisions through the genuine, locally installed Codex CLI.

The court controller still owns every courtroom tool. Each CLI invocation gets
a new temporary working directory, role instructions and an authorized context;
no Codex thread is resumed. This is an application-level capability restriction,
not a claim of an OS firewall or of identical prompts to the raw Responses API.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import quote, quote_plus

from .llm import BackendError, OpenAIBackend, _check_strict_schema, _json_object, _validate_value


ADAPTER_VERSION = "0.3.4"
CODE_MODE_DISABLED_NOTICE = (
    "Code Mode is unavailable because code-mode host is disabled. "
    "Code mode will fail closed; enable `features.code_mode_host` "
    "and install `codex-code-mode-host`."
)
TOOL_ITEM_TYPES = {
    "command_execution", "file_change", "mcp_tool_call", "collab_tool_call",
    "web_search", "todo_list",
}

REQUIRED_FLAGS = {
    "--ignore-user-config", "--ephemeral", "--json", "--output-schema",
    "--output-last-message", "--skip-git-repo-check", "--sandbox",
}
REQUIRED_FEATURES = {
    "shell_tool", "unified_exec", "apps", "plugins", "hooks", "memories",
    "multi_agent", "view_image", "skill_search", "skip_host_skill_discovery",
    "unbounded_connection_retries",
}
# Match installed capabilities rather than passing unsupported feature names.
CAPABILITY_WORDS = re.compile(
    r"shell|exec|code_mode|js_repl|plugin|(^|_)apps?($|_)|browser|computer_use|"
    r"image_generation|view_image|skill|memor|multi_agent|agent_message|artifact|"
    r"workspace_dependencies|hooks|goals|tool|daemon_auto_start|chronicle|"
    r"realtime|request_permissions|request_rule|send_message|external_agent|"
    r"unbounded_connection_retries|step_model_switching|worktrees|guardian_conversation"
)


def _redact_diagnostic(text: str, secrets: tuple[str, ...]) -> str:
    """Used only by the case-free connection check; redact before truncating."""
    for secret in sorted(set(secrets), key=len, reverse=True):
        if not secret:
            continue
        variants = {secret, json.dumps(secret, ensure_ascii=True)[1:-1],
                    json.dumps(secret, ensure_ascii=False)[1:-1],
                    quote(secret, safe=""), quote_plus(secret, safe="")}
        for value in sorted(variants, key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    text = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r'''(?i)((?:api[_-]?key|access[_-]?token|authorization|password|secret)["']?\s*[:=]\s*)["']?[^\s,;}]+''', r"\1[REDACTED]", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    # Print as JSON at the boundary. Removing control characters also keeps
    # reports readable if a user opens the JSON directly in an editor.
    text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32)
    return text[:4000] + (" [truncated]" if len(text) > 4000 else "")


def _capture_diagnostic(state: dict, category: str, message: str) -> None:
    if not state.get("diagnostics_enabled"):
        return
    entries = state.setdefault("cli_diagnostics", [])
    if len(entries) >= 16:
        return
    entry = {"category": category,
             "message": _redact_diagnostic(message, state.get("_secrets", ())) }
    if entry not in entries:
        entries.append(entry)


def _cli_error(text: str) -> BackendError:
    """Classify diagnostic text without ever returning credentials or raw logs."""
    lowered = text.lower()
    if "403" in lowered or "标准 codex" in lowered or "permissiondenied" in lowered or "forbidden" in lowered:
        return BackendError("permission_denied", "The provider rejected the standard Codex CLI request; ask its administrator to check this client and token group.")
    if "401" in lowered or "unauthorized" in lowered or "authentication" in lowered:
        return BackendError("authentication_failed", "Codex CLI authentication failed for the selected provider.")
    if "model" in lowered and any(term in lowered for term in ("not found", "not exist", "unsupported", "not available", "不存在")):
        return BackendError("model_unavailable", "The provider does not expose the configured model to this token; verify the exact model identifier.")
    if "429" in lowered or "rate limit" in lowered or "quota" in lowered:
        return BackendError("rate_limited", "The provider returned a rate or quota limit; no automatic retry was made.")
    if "timed out" in lowered or "timeout" in lowered:
        return BackendError("api_timeout", "The Codex CLI request timed out; no automatic retry was made.")
    if "unexpected argument" in lowered or "unknown feature" in lowered or "error loading config" in lowered:
        return BackendError("codex_config_rejected", "The installed Codex CLI rejected the adapter settings; upgrade the official client or inspect its supported flags.")
    if lowered.startswith("codex is ignoring ") and "unrecognized configuration setting" in lowered:
        return BackendError("codex_config_rejected", "Codex ignored a configuration setting; inspect cli_diagnostics for the exact setting. The decision was not accepted.")
    return BackendError("codex_request_failed", "The Codex CLI request failed; no model fallback or fabricated decision was used.")


def _consume_event(event: dict, state: dict) -> None:
    """Accept text/usage and verified startup advisories; never enable tools.

    Codex 0.160.0 serializes warnings as item.completed / item.type=error.
    This differs from a fatal top-level error. Allow the isolation-feature
    advisory and exact missing-metadata notice for the requested model. The
    latter retains the model slug (official rust-v0.160.0 model_info.rs) and is
    recorded as a transport limitation. The exact Code Mode disabled warning
    is also allowed when this invocation explicitly disables its host; it
    confirms the intended restriction. Rerouting and lost events still fail.
    """
    kind = event.get("type")
    if not isinstance(kind, str):
        raise BackendError("codex_protocol_error", "Codex returned a malformed event type.")
    state["last_event_type"] = kind[:80]
    if kind in {"item.started", "item.updated", "item.completed"}:
        item = event.get("item")
        if not isinstance(item, dict):
            raise BackendError("codex_protocol_error", "Codex returned a malformed item event.")
        item_type = item.get("type")
        if not isinstance(item_type, str):
            raise BackendError("codex_protocol_error", "Codex returned a malformed item type.")
        if item_type in {"agent_message", "reasoning"}:
            return
        if item_type == "error":
            message = item.get("message")
            if (kind != "item.completed" or not isinstance(message, str)
                    or not isinstance(item.get("id"), str)
                    or set(item) - {"id", "type", "message"}):
                raise BackendError("codex_protocol_error", "Codex returned a malformed diagnostic item.")
            lowered = message.lower()
            _capture_diagnostic(state, "cli_notice", message)
            if lowered.startswith("model rerouted:"):
                raise BackendError("codex_model_rerouted", "Codex reported a model change; the decision was stopped rather than accepting a fallback model.")
            if lowered.startswith("in-process app-server event stream lagged;"):
                raise BackendError("codex_event_stream_incomplete", "Codex reported dropped events; tool-use auditing is incomplete and this decision was stopped.")
            if (message.startswith("Under-development features enabled:")
                    and "Under-development features are incomplete and may behave unpredictably." in message):
                notices = state.setdefault("cli_notice_counts", {})
                notices["unstable_features_advisory"] = notices.get("unstable_features_advisory", 0) + 1
                return
            if (message == CODE_MODE_DISABLED_NOTICE and state.get("code_mode_host_disabled") is True
                    and state.get("cli_version") == "0.160.0"):
                notices = state.setdefault("cli_notice_counts", {})
                notices["code_mode_disabled_advisory"] = notices.get("code_mode_disabled_advisory", 0) + 1
                return
            model = state.get("requested_model")
            if isinstance(model, str) and message == (
                    f"Model metadata for `{model}` not found. Defaulting to fallback metadata; "
                    "this can degrade performance and cause issues."):
                notices = state.setdefault("cli_notice_counts", {})
                notices["fallback_model_metadata"] = notices.get("fallback_model_metadata", 0) + 1
                state["used_fallback_model_metadata"] = True
                return
            # Keep provider errors meaningful without exposing diagnostic text,
            # paths, requests or credentials. Unrecognized notices do not pass.
            classified = _cli_error(message)
            if classified.code != "codex_request_failed":
                raise classified
            raise BackendError("codex_notice_requires_review", "Codex reported an unrecognized diagnostic notice; no tool use was inferred and no decision was accepted.")
        if isinstance(item_type, str) and item_type in TOOL_ITEM_TYPES:
            raise BackendError("codex_tool_use_blocked", f"Codex emitted a forbidden {item_type} event; this decision is invalid and the process was stopped.")
        raise BackendError("codex_protocol_error", "Codex emitted an unsupported item type; this decision was not accepted.")
    if kind == "turn.completed":
        usage = event.get("usage", {})
        if not isinstance(usage, dict):
            raise BackendError("codex_protocol_error", "Codex returned malformed usage metadata.")
        # Sum completed turns if the client reports more than one. Do not label
        # this number as HTTP requests: the CLI does not expose that count.
        for field in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens"):
            value = usage.get(field, 0)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                state["usage"][field] = state["usage"].get(field, 0) + value
        state["completed_turns"] += 1
        return
    if kind in {"error", "turn.failed"}:
        # No agent text, tool payloads, or complete request objects are logged.
        detail = event.get("message") if kind == "error" else event.get("error", {})
        if isinstance(detail, dict):
            detail = detail.get("message")
        if isinstance(detail, str):
            _capture_diagnostic(state, "cli_failure", detail)
        raise _cli_error(json.dumps(event, ensure_ascii=False))
    if kind not in {"thread.started", "turn.started"}:
        raise BackendError("codex_protocol_error", "Codex emitted an unrecognized event; this decision was not accepted.")


class CodexCLIBackend(OpenAIBackend):
    """An isolated CLI invocation per decision, with no retries or fallback."""

    def __init__(self, config: dict, *, api_key: str | None = None):
        super().__init__(config, api_key=api_key)
        settings = config.get("codex", {})
        if not isinstance(settings, dict) or set(settings) - {"executable", "progress", "diagnostics"}:
            raise BackendError("invalid_config", "codex may contain only executable, progress and diagnostics.")
        self.executable = settings.get("executable", "codex")
        if not isinstance(self.executable, str) or not self.executable.strip():
            raise BackendError("invalid_config", "codex.executable must be an executable path or command name.")
        self.progress = settings.get("progress", True)
        if not isinstance(self.progress, bool):
            raise BackendError("invalid_config", "codex.progress must be a boolean.")
        self.diagnostics = settings.get("diagnostics", False)
        if not isinstance(self.diagnostics, bool):
            raise BackendError("invalid_config", "codex.diagnostics must be a boolean.")
        self.max_attempts = 1
        self.cli_version: str | None = None
        self._features: dict[str, str] = {}
        self._resolved_executable: str | None = None
        self._active_process: subprocess.Popen | None = None
        self.stats.update({
            "transport": "codex_cli", "attempted_calls_unit": "codex_exec_invocations",
            "adapter_version": ADAPTER_VERSION, "cli_notice_counts": {},
            "http_attempt_count": None, "usage_source": "reported_codex_turn_usage_only",
            "configured_max_output_tokens": self.max_output_tokens,
            "max_output_tokens_enforced": False,
        })
        if self.diagnostics:
            self.stats["cli_diagnostics"] = []

    def _preflight(self) -> None:
        if self._resolved_executable is not None:
            return
        binary = shutil.which(self.executable)
        if binary:
            binary = str(Path(binary).resolve())
        if not binary:
            raise BackendError("codex_missing", "Install the official Codex CLI, or set codex.executable to its path.")
        try:
            with tempfile.TemporaryDirectory(prefix="court-codex-preflight-") as directory:
                results = []
                for arguments in (("--version",), ("exec", "--help"), ("features", "list"), ("mcp", "list", "--json")):
                    result = subprocess.run([binary, *arguments], cwd=directory, env=self._subprocess_environment(include_key=False), capture_output=True, text=True, timeout=20, check=False)
                    if result.returncode != 0:
                        raise BackendError("codex_preflight_failed", "Could not inspect the official Codex CLI's local capabilities.")
                    results.append(result.stdout)
        except (OSError, subprocess.TimeoutExpired):
            raise BackendError("codex_preflight_failed", "Could not start the installed Codex CLI for local checks.") from None
        version = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", results[0])
        if not version or tuple(map(int, version.groups())) < (0, 160, 0):
            raise BackendError("codex_upgrade_required", "This adapter requires official Codex CLI 0.160.0 or later for its isolation controls.")
        if any(flag not in results[1] for flag in REQUIRED_FLAGS):
            raise BackendError("codex_upgrade_required", "The installed Codex CLI is missing a required non-interactive isolation option.")
        features = {}
        for line in results[2].splitlines():
            match = re.fullmatch(r"([a-z][a-z0-9_.]*)\s+(.+?)\s+(?:true|false)\s*", line)
            if match:
                features[match.group(1)] = match.group(2).strip()
        if not REQUIRED_FEATURES.issubset(features):
            raise BackendError("codex_upgrade_required", "The installed Codex CLI is missing required tool or skill isolation controls.")
        try:
            servers = json.loads(results[3])
            if not isinstance(servers, list):
                raise ValueError("Expected server list")
            for server in servers:
                if not isinstance(server, dict) or not isinstance(server.get("name"), str) or not server["name"]:
                    raise ValueError("Expected server name")
            if servers:
                raise BackendError("codex_mcp_config_unsupported", "This demo adapter requires a Codex host with no configured MCP servers; existing configuration was not modified and no model request was made.")
        except (ValueError, TypeError):
            raise BackendError("codex_preflight_failed", "Could not enumerate configured MCP servers for per-run isolation.") from None
        self.cli_version = version.group(0)
        self._features = features
        self._resolved_executable = binary
        self.stats["cli_version"] = self.cli_version

    def check_credentials(self) -> None:
        super().check_credentials()
        self._preflight()

    def _subprocess_environment(self, *, include_key: bool) -> dict[str, str]:
        # Inherit these values unchanged. In particular, never replace HOME or
        # CODEX_HOME to evade local configuration or credential boundaries.
        names = {
            "PATH", "HOME", "CODEX_HOME", "USER", "LOGNAME", "LANG", "LANGUAGE",
            "LC_ALL", "LC_CTYPE", "TMPDIR", "TMP", "TEMP", "TZ",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY",
            "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
        }
        environment = {key: value for key, value in os.environ.items() if key in names}
        # No inherited debug/telemetry exporters, alternate authentication,
        # executable injection variables or model-provider overrides.
        if include_key:
            environment[self.api["key_env"]] = self._api_key or os.environ.get(self.api["key_env"], "").strip()
        return environment

    def _build_command(self, workdir: str, model: str, schema_path: Path, output_path: Path, instructions_path: Path) -> list[str]:
        assert self._resolved_executable
        provider = "court_" + self.api["provider"]
        values: dict[str, Any] = {
            "model_provider": provider,
            f"model_providers.{provider}.name": "Court " + self.api["provider"],
            f"model_providers.{provider}.base_url": self.api["base_url"],
            f"model_providers.{provider}.env_key": self.api["key_env"],
            f"model_providers.{provider}.wire_api": "responses",
            f"model_providers.{provider}.requires_openai_auth": False,
            f"model_providers.{provider}.request_max_retries": 0,
            f"model_providers.{provider}.stream_max_retries": 0,
            f"model_providers.{provider}.stream_idle_timeout_ms": self.timeout_seconds * 1000,
            "model_reasoning_effort": self.reasoning_effort,
            "model_instructions_file": str(instructions_path),
            "project_doc_max_bytes": 0,
            "developer_instructions": "Return one JSON role decision. Only the supplied context is available. Do not invoke tools or inspect files.",
            "web_search": "disabled", "approval_policy": "never",
            "memories.use_memories": False, "memories.generate_memories": False,
            "apps._default.enabled": False,
            "shell_environment_policy.inherit": "none",
            "analytics.enabled": False,
        }
        arguments = [self._resolved_executable, "exec", "--ignore-user-config", "--ephemeral", "--json", "--skip-git-repo-check", "--sandbox", "read-only", "--color", "never", "--model", model,
                     "--output-schema", str(schema_path), "--output-last-message", str(output_path)]
        for key, value in values.items():
            arguments.extend(["-c", key + "=" + json.dumps(value, ensure_ascii=False)])
        arguments.extend(["-c", "mcp_servers={}"])
        # Empty config maps alone do not clear inherited entries. Preflight
        # requires the local MCP list to be empty instead of assuming this does.
        for feature, stage in sorted(self._features.items()):
            if stage == "removed":
                continue
            if feature == "skip_host_skill_discovery":
                arguments.extend(["--enable", feature])
            elif CAPABILITY_WORDS.search(feature):
                arguments.extend(["--disable", feature])
        arguments.append("-")
        return arguments

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def _run_process(self, arguments: list[str], prompt: str, workdir: str, state: dict) -> None:
        """Stream event checks while the process runs, never persist raw logs."""
        environment = self._subprocess_environment(include_key=True)
        process = subprocess.Popen(arguments, cwd=workdir, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self._active_process = process
        assert process.stdin and process.stdout and process.stderr
        pending = memoryview(prompt.encode("utf-8"))
        stdout_buffer = b""
        stderr_buffer = b""
        received = 0
        started = time.monotonic()
        try:
            with selectors.DefaultSelector() as selector:
                for stream, kind, events in ((process.stdin, "stdin", selectors.EVENT_WRITE), (process.stdout, "stdout", selectors.EVENT_READ), (process.stderr, "stderr", selectors.EVENT_READ)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, events, kind)
                while selector.get_map():
                    if time.monotonic() - started > self.timeout_seconds:
                        raise BackendError("api_timeout", "The Codex CLI decision exceeded its wall-time budget; the process was stopped.")
                    for entry, _ in selector.select(timeout=0.2):
                        stream = entry.fileobj
                        if entry.data == "stdin":
                            try:
                                written = os.write(stream.fileno(), pending[:16384])
                                pending = pending[written:]
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
                        if entry.data == "stderr":
                            stderr_buffer = (stderr_buffer + chunk)[-65536:]
                            continue
                        received += len(chunk)
                        if received > 2_000_000:
                            raise BackendError("codex_output_budget", "Codex emitted too much event data; the process was stopped.")
                        stdout_buffer += chunk
                        while b"\n" in stdout_buffer:
                            raw, stdout_buffer = stdout_buffer.split(b"\n", 1)
                            if raw.strip():
                                try:
                                    event = json.loads(raw)
                                except (ValueError, UnicodeError):
                                    raise BackendError("codex_protocol_error", "Codex returned a malformed JSONL event.") from None
                                if not isinstance(event, dict):
                                    raise BackendError("codex_protocol_error", "Codex returned a non-object event.")
                                _consume_event(event, state)
                if stdout_buffer.strip():
                    try:
                        event = json.loads(stdout_buffer)
                    except (ValueError, UnicodeError):
                        raise BackendError("codex_protocol_error", "Codex returned an incomplete JSONL event.") from None
                    if not isinstance(event, dict):
                        raise BackendError("codex_protocol_error", "Codex returned a non-object event.")
                    _consume_event(event, state)
                remaining = self.timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise BackendError("api_timeout", "The Codex CLI decision exceeded its wall-time budget.")
                try:
                    returncode = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    raise BackendError("api_timeout", "The Codex CLI did not finish within its wall-time budget.") from None
                if returncode != 0:
                    raise _cli_error(stderr_buffer.decode("utf-8", errors="replace"))
                if not state["completed_turns"]:
                    raise BackendError("incomplete_output", "Codex exited without a completed model turn.")
        except BackendError:
            # Stop first. Drain only diagnostics already queued in local pipes;
            # do not keep a rejected session alive to gather more notices.
            self._stop(process)
            if state.get("diagnostics_enabled"):
                for stream, kind in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                    remaining_bytes = 65536
                    while remaining_bytes > 0:
                        try:
                            chunk = os.read(stream.fileno(), min(8192, remaining_bytes))
                        except (OSError, ValueError):
                            break
                        if not chunk:
                            break
                        remaining_bytes -= len(chunk)
                        if kind == "stderr":
                            stderr_buffer = (stderr_buffer + chunk)[-65536:]
                        else:
                            stdout_buffer += chunk
                for raw in stdout_buffer.splitlines():
                    try:
                        queued = json.loads(raw)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(queued, dict):
                        continue
                    item = queued.get("item")
                    if (queued.get("type") == "item.completed" and isinstance(item, dict)
                            and item.get("type") == "error" and isinstance(item.get("message"), str)
                            and not (set(item) - {"id", "type", "message"})):
                        _capture_diagnostic(state, "queued_cli_notice", item["message"])
                if stderr_buffer:
                    _capture_diagnostic(state, "stderr_tail", stderr_buffer.decode("utf-8", errors="replace"))
            raise
        finally:
            self._stop(process)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            self._active_process = None

    def decide(self, role_id: str, system: str, context: dict, schema: dict) -> dict:
        if not isinstance(role_id, str) or not role_id or not isinstance(system, str) or not isinstance(context, dict):
            raise BackendError("invalid_context", "A role identifier, role instructions and an authorized context are required.")
        _check_strict_schema(schema)
        try:
            payload = json.dumps(context, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            schema_text = json.dumps(schema, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise BackendError("invalid_context", "Context and schema must be JSON-serializable.") from None
        if len(system) + len(payload) + len(schema_text) > self.max_input_chars:
            raise BackendError("input_budget_exceeded", "The role context exceeds max_input_chars; no content was silently truncated.")
        if self.stats["attempted_calls"] >= self.max_model_calls:
            raise BackendError("call_budget_exceeded", "The run reached max_model_calls; no further Codex invocations are allowed.")
        self.check_credentials()
        model = self.lawyer_model if role_id == "R_DEFENSE" else self.environment_model
        # Diagnostic free text is permitted only for this exact, case-free
        # check. Merely enabling the setting on a courtroom run cannot expose
        # case content reflected by a child process in its diagnostic output.
        is_connection_check = (context == {"task": "API compatibility check; no case data"}
            and schema == {"type": "object", "properties": {"status": {"type": "string", "enum": ["ok"]}},
                           "required": ["status"], "additionalProperties": False}
            and system == 'Return the JSON object {"status":"ok"}. This is a connectivity and JSON-schema check.')
        diagnostic_key = self._api_key or os.environ.get(self.api["key_env"], "").strip()
        state: dict[str, Any] = {"usage": {}, "completed_turns": 0,
            "cli_version": self.cli_version,
            "requested_model": model, "diagnostics_enabled": self.diagnostics and is_connection_check,
            "_secrets": (diagnostic_key,)}
        self.stats["attempted_calls"] += 1
        if self.progress:
            safe_role = re.sub(r"[^A-Za-z0-9_.-]", "_", role_id)[:64]
            safe_model = re.sub(r"[^A-Za-z0-9_.-]", "_", model)[:96]
            print(f"[court] Codex decision {self.stats['attempted_calls']} | {safe_role} | {safe_model} starting", file=sys.stderr, flush=True)
        started = time.monotonic()
        error: BackendError | None = None
        parsed: dict | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="court-role-") as directory:
                workdir = Path(directory)
                schema_path = workdir / "output-schema.json"
                output_path = workdir / "decision.json"
                instructions_path = workdir / "role-instructions.txt"
                schema_path.write_text(schema_text, encoding="utf-8")
                instructions_path.write_text(system + "\nReturn one complete JSON object matching the output schema. Do not call tools, inspect files, or retrieve any external information. The controller executes proposed courtroom actions separately.", encoding="utf-8")
                arguments = self._build_command(directory, model, schema_path, output_path, instructions_path)
                state["code_mode_host_disabled"] = ("--disable", "code_mode_host") in zip(arguments, arguments[1:])
                self._run_process(arguments, payload, directory, state)
                if not output_path.is_file() or output_path.is_symlink() or output_path.stat().st_size > 1_000_000:
                    raise BackendError("empty_output", "Codex did not produce an acceptable final decision file.")
                parsed = _json_object(output_path.read_text(encoding="utf-8"))
                if not _validate_value(parsed, schema, schema):
                    raise BackendError("schema_mismatch", "The Codex decision did not match the action schema; no retry was made.")
        except BackendError as exc:
            error = exc
        except KeyboardInterrupt:
            error = BackendError("cancelled", "The user interrupted the Codex CLI decision.")
            raise
        except (OSError, UnicodeError, ValueError):
            error = BackendError("codex_execution_failed", "Could not complete the local Codex CLI invocation; no synthetic decision was substituted.")
        finally:
            usage = state["usage"]
            response = {"model": None, "usage": {
                "input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                "input_tokens_details": {"cached_tokens": usage.get("cached_input_tokens", 0)},
                "output_tokens_details": {"reasoning_tokens": usage.get("reasoning_output_tokens", 0)},
            }}
            self._record(role_id, model, 1, time.monotonic() - started, response, error)
            self.calls[-1].update({"transport": "codex_cli", "cli_version": self.cli_version,
                "adapter_version": ADAPTER_VERSION, "cli_notice_counts": dict(state.get("cli_notice_counts", {})),
                "model_identity_verified": False, "http_attempt_count": None,
                "completed_turns": state["completed_turns"], "max_output_tokens_enforced": False,
                "code_mode_host_disabled": bool(state.get("code_mode_host_disabled")),
                "used_fallback_model_metadata": bool(state.get("used_fallback_model_metadata"))})
            if state.get("diagnostics_enabled"):
                diagnostic = {"call": self.stats["attempted_calls"], "model": model,
                    "error_code": error.code if error else None,
                    "last_event_type": state.get("last_event_type"),
                    "entries": state.get("cli_diagnostics", [])}
                self.stats["cli_diagnostics"].append(diagnostic)
                if error and self.progress:
                    print("[court] CLI_DIAGNOSTICS " + json.dumps(diagnostic, ensure_ascii=True), file=sys.stderr, flush=True)
            state.pop("_secrets", None)
            for notice, count in state.get("cli_notice_counts", {}).items():
                self.stats["cli_notice_counts"][notice] = self.stats["cli_notice_counts"].get(notice, 0) + count
        if error:
            if self.progress:
                print(f"[court] Codex decision failed: {error.code}", file=sys.stderr, flush=True)
            raise error from None
        self.stats["successful_decisions"] += 1
        if self.progress:
            print(f"[court] Codex decision completed ({time.monotonic() - started:.1f}s)", file=sys.stderr, flush=True)
        assert parsed is not None
        return parsed

    def close(self) -> None:
        if self._active_process is not None:
            self._stop(self._active_process)
            self._active_process = None
        self._api_key = None
