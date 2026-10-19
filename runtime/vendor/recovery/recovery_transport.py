"""Scoped Claude error-envelope repair; no new accepted model aliases.

Audited against the installed official Claude Code 2.1.300 serializer and
SDKAssistantMessage schema. Its API failures are assistant messages with
model='<synthetic>' and is_api_error_message=True. They are failures, never
lawyer decisions. The v2 adapter previously compared that synthetic marker
with the requested model before inspecting the error envelope.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re


VERSION = "opus-error-envelope-repair-1"
ERROR_LABELS = {
    "authentication_failed", "oauth_org_not_allowed", "account_on_hold",
    "verification_required", "billing_error", "rate_limit", "overloaded",
    "invalid_request", "model_not_found", "server_error", "unknown",
    "max_output_tokens", "cloud_credential_error",
}
API_LABELS = {
    "no_response", "tls_untrusted_ca", "gateway_content_type",
    "provider_credentials", "gateway_signin_required", "gateway_session_expired",
    "api_key_auth_disabled", "org_disabled_credential", "invalid_credential_header",
    "max_output_tokens", "dlp_request_denied", "safety_monitor_blocked",
    "claude_code_version_too_old", "effort_requires_thinking",
}


def _safe_model(value):
    # The provider's arbitrary strings must never be copied into reports.
    if value == "<synthetic>":
        return value
    if isinstance(value, str) and re.fullmatch(
        r"(?:claude|gpt|deepseek|doubao)-[A-Za-z0-9_.:-]{1,100}", value
    ):
        return value
    return None


def _observation(source, value):
    result = {"source": source, "reported_model": _safe_model(value)}
    if result["reported_model"] is None:
        result["reported_model_type"] = type(value).__name__
        if isinstance(value, str):
            result["reported_model_sha256"] = hashlib.sha256(value.encode()).hexdigest()
    return result


def _remember_model(diagnostics, source, value):
    observation = _observation(source, value)
    items = diagnostics.setdefault("model_observations", [])
    if observation not in items:
        if len(items) < 12:
            items.append(observation)
        else:
            diagnostics["additional_model_observations"] = diagnostics.get("additional_model_observations", 0) + 1


def _error_metadata(event):
    status = event.get("api_error_status")
    error, api_error = event.get("error"), event.get("api_error")
    return {
        "synthetic_api_error": True,
        "http_status": status if type(status) is int and 400 <= status <= 599 else None,
        "error_kind": error if isinstance(error, str) and error in ERROR_LABELS else "unrecognized",
        "api_error_kind": api_error if isinstance(api_error, str) and api_error in API_LABELS else None,
    }


def _classify_error(diagnostics):
    status, kind, api_kind = (diagnostics.get(k) for k in ("http_status", "error_kind", "api_error_kind"))
    if status == 401 or kind == "authentication_failed":
        return "authentication_failed"
    if status == 403 or kind in {"oauth_org_not_allowed", "account_on_hold", "verification_required"}:
        return "permission_denied"
    if kind == "billing_error":
        return "billing_error"
    if kind == "model_not_found":
        return "model_unavailable"
    # A credential/TLS/gateway configuration diagnostic is not a transient
    # server fault merely because it arrives in a broad server_error wrapper.
    if api_kind in {"tls_untrusted_ca", "gateway_content_type", "provider_credentials",
                    "gateway_signin_required", "gateway_session_expired", "api_key_auth_disabled",
                    "org_disabled_credential", "invalid_credential_header", "claude_code_version_too_old"}:
        return "claude_request_failed"
    if status == 429 or kind == "rate_limit":
        return "rate_limited"
    if (status is not None and 500 <= status <= 599) or kind == "overloaded":
        return "api_transient"
    if kind == "server_error" and api_kind == "no_response":
        return "api_timeout"
    if kind == "max_output_tokens" or api_kind == "max_output_tokens":
        return "incomplete_output"
    return "claude_request_failed"


@contextmanager
def patched_opus_backend(backends_module):
    """Patch only this process and restore on exit; never edit frozen files.

    Caller owns retry policy. This adds no call or retry, accepts no synthetic
    result, and keeps the original normal-message/model/tool validation.
    """
    cls = backends_module.ClaudeCLIBackend
    original_consume, original_decide = cls._consume, cls.decide
    BackendError = backends_module.BackendError

    def consume(self, event, state, metadata):
        diagnostics = getattr(self, "_recovery_opus_diagnostics", None)
        if diagnostics is None:
            diagnostics = self._recovery_opus_diagnostics = {"repair_version": VERSION}
        kind, subtype = event.get("type"), event.get("subtype")
        message = event.get("message")
        if kind == "system" and subtype == "init":
            _remember_model(diagnostics, "system.init", event.get("model"))
        elif kind == "assistant" and isinstance(message, dict):
            _remember_model(diagnostics, "assistant.message", message.get("model"))
        elif kind == "stream_event" and isinstance(event.get("event"), dict):
            inner = event["event"]
            if inner.get("type") == "message_start" and isinstance(inner.get("message"), dict):
                _remember_model(diagnostics, "stream.message_start", inner["message"].get("model"))
        elif kind == "result" and isinstance(event.get("modelUsage"), dict):
            for model in list(event["modelUsage"])[:12]:
                _remember_model(diagnostics, "result.modelUsage", model)
        if kind == "assistant" and isinstance(message, dict) and event.get("is_api_error_message") is True:
            # Preserve tool blocking even on an error-shaped envelope.
            backends_module._reject_tools(event)
            metadata["last_cli_event"] = {"type": "assistant", "subtype": None}
            if message.get("model") != "<synthetic>":
                raise BackendError("claude_protocol_error", "Claude API-error envelope did not use its audited synthetic model marker.")
            blocks = message.get("content")
            if not isinstance(blocks, list) or any(not isinstance(b, dict) or b.get("type") != "text" for b in blocks):
                raise BackendError("claude_protocol_error", "Claude API-error envelope had an unsupported content shape.")
            diagnostics.update(_error_metadata(event))
            code = _classify_error(diagnostics)
            error = BackendError(code, "The official Claude CLI reported an API failure; its synthetic error message was not accepted as a lawyer decision.")
            error.recovery_diagnostics = deepcopy(diagnostics)
            raise error
        try:
            return original_consume(self, event, state, metadata)
        except BackendError as error:
            if error.code == "model_identity_mismatch":
                error.recovery_diagnostics = deepcopy(diagnostics)
            raise

    def decide(self, *args, **kwargs):
        self._recovery_opus_diagnostics = {"repair_version": VERSION}
        before = len(self.calls)
        try:
            return original_decide(self, *args, **kwargs)
        finally:
            diagnostics = deepcopy(self._recovery_opus_diagnostics)
            for call in self.calls[before:]:
                call["recovery_diagnostics"] = diagnostics

    cls._consume, cls.decide = consume, decide
    try:
        yield
    finally:
        cls._consume, cls.decide = original_consume, original_decide


def saved_opus_diagnostics(run_root):
    """Read existing summaries only; no write, API request, or journal repair.

    v2 did not retain the rejected model or synthetic error flag, so this can
    identify the failing event kind but cannot reconstruct a lost response.
    """
    result = []
    for case_id in ("CA-PILOT-003", "CA-PILOT-004"):
        folder = Path(run_root) / "courts" / "opus" / case_id / "court_attempts"
        for path in sorted(folder.glob("attempt-*/model_calls.json")):
            obj = json.loads(path.read_text())
            calls = obj if isinstance(obj, list) else obj.get("calls", [])
            for call in calls:
                if not isinstance(call, dict) or call.get("status") != "model_identity_mismatch":
                    continue
                result.append({"case_id": case_id, "attempt": path.parent.name,
                    "status": call["status"], "last_cli_event": call.get("last_cli_event"),
                    "resolved_model": _safe_model(call.get("resolved_model")),
                    "reported_model_was_not_retained": call.get("resolved_model") is None})
    return result
