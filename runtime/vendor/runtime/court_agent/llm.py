"""Isolated OpenAI Responses decisions for the courtroom controller.

The model returns a proposed action, never an executable hosted tool call.  The
controller must validate and authorize that action before changing the case.
Only this adapter needs Internet access; it contacts the configured API endpoint.
No response IDs, conversations, prompts, or hidden reasoning are reused across
roles.  ``calls`` contains operational metadata, not model inputs or outputs.
"""

from __future__ import annotations

import json
import os
import re
import time
from copy import deepcopy
from typing import Any


class BackendError(RuntimeError):
    """Safe-to-display backend failure with a machine-readable error code."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


API_PROVIDERS = {
    "openai": {"base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY"},
    "packy": {"base_url": "https://cf.api.fan/v1", "key_env": "PACKY_API_KEY"},
}


def api_settings(config: dict) -> dict:
    """Bind a known endpoint to its own credential variable, without redirects.

    OPENAI_BASE_URL, proxy environment variables and arbitrary URL settings are
    intentionally not used. The key value never belongs in the config file.
    """
    if not isinstance(config, dict):
        raise BackendError("invalid_config", "The configuration must be a JSON object.")
    if any(key in config for key in ("api_key", "token", "password")):
        raise BackendError("invalid_config", "Keep credentials out of configuration files; use the provider environment variable or --prompt-key.")
    supplied = config.get("api", {})
    if not isinstance(supplied, dict) or set(supplied) - {"provider", "base_url", "key_env"}:
        raise BackendError("invalid_config", "api may contain only provider, base_url and key_env.")
    provider = supplied.get("provider", "openai")
    if not isinstance(provider, str) or provider not in API_PROVIDERS:
        raise BackendError("invalid_config", "api.provider must be openai or packy.")
    expected = API_PROVIDERS[provider]
    if supplied.get("base_url", expected["base_url"]) != expected["base_url"]:
        raise BackendError("invalid_config", "api.base_url must exactly match the selected provider's approved endpoint.")
    if supplied.get("key_env", expected["key_env"]) != expected["key_env"]:
        raise BackendError("invalid_config", "api.key_env must match the selected provider's credential variable.")
    return {"provider": provider, **expected}


def _get(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _positive_int(config: dict, key: str, default: int) -> int:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BackendError("invalid_config", f"{key} must be a positive integer.")
    return value


def _check_strict_schema(schema: dict) -> None:
    """Reject accidental non-strict object schemas before making a paid call."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise BackendError("invalid_schema", "The action schema must be a JSON object schema.")

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object" or "properties" in node:
            properties = node.get("properties", {})
            required = node.get("required", [])
            if (
                not isinstance(properties, dict)
                or not isinstance(required, list)
                or node.get("additionalProperties") is not False
                or set(required) != set(properties)
            ):
                raise BackendError(
                    "invalid_schema",
                    "Strict object schemas must forbid extra fields and require every property.",
                )
        for key in ("properties", "$defs", "definitions"):
            for child in node.get(key, {}).values():
                visit(child)
        if isinstance(node.get("items"), dict):
            visit(node["items"])
        for key in ("anyOf", "oneOf", "allOf"):
            for child in node.get(key, []):
                visit(child)

    visit(schema)


def _validate_value(value: Any, schema: dict, root: dict, depth: int = 0) -> bool:
    """Validate the small strict schema vocabulary used by our action contract.

    Authorization, available actions, citation validity, and legal procedure are
    the controller's responsibility. This validates shape, not legal meaning.
    """
    if depth > 60:
        return False
    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return False
        resolved: Any = root
        for component in ref[2:].split("/"):
            component = component.replace("~1", "/").replace("~0", "~")
            if not isinstance(resolved, dict) or component not in resolved:
                return False
            resolved = resolved[component]
        return _validate_value(value, resolved, root, depth + 1)
    if "anyOf" in schema and not any(
        _validate_value(value, child, root, depth + 1) for child in schema["anyOf"]
    ):
        return False
    if "oneOf" in schema and sum(
        _validate_value(value, child, root, depth + 1) for child in schema["oneOf"]
    ) != 1:
        return False
    if "allOf" in schema and not all(
        _validate_value(value, child, root, depth + 1) for child in schema["allOf"]
    ):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if "const" in schema and value != schema["const"]:
        return False
    types = schema.get("type")
    if types is not None:
        types = types if isinstance(types, list) else [types]
        checks = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "null": value is None,
        }
        if not any(checks.get(kind, False) for kind in types):
            return False
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not set(schema.get("required", [])).issubset(value):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        for key, child in properties.items():
            if key in value and not _validate_value(value[key], child, root, depth + 1):
                return False
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            return False
        if "items" in schema and not all(
            _validate_value(item, schema["items"], root, depth + 1) for item in value
        ):
            return False
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            return False
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", -float("inf")) or value > schema.get("maximum", float("inf")):
            return False
    return True


def _json_object(text: str) -> dict:
    def reject_constant(_: str) -> None:
        raise ValueError("Non-finite JSON number")

    def pairs_hook(pairs: list) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    try:
        parsed = json.loads(text, parse_constant=reject_constant, object_pairs_hook=pairs_hook)
    except (ValueError, TypeError, RecursionError):
        raise BackendError("invalid_json", "Model returned invalid JSON.", retryable=True) from None
    if not isinstance(parsed, dict):
        raise BackendError("invalid_json", "Model action must be a JSON object.", retryable=True)
    return parsed


def _safe_api_error(exc: Exception) -> BackendError:
    """Never include SDK exception bodies, requests, credentials, or prompts."""
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    if status == 401 or name == "AuthenticationError":
        return BackendError("authentication_failed", "API authentication failed; check the selected provider's local API key.")
    if status == 403 or name == "PermissionDeniedError":
        return BackendError("permission_denied", "The API project does not have permission for this request.")
    if status == 404 or name == "NotFoundError":
        return BackendError("model_unavailable", "The configured model is unavailable to this API project.")
    if status == 429 or name == "RateLimitError":
        return BackendError("rate_limited", "API rate or quota limit reached.", retryable=True)
    if name in {"APITimeoutError", "TimeoutException", "TimeoutError"} or status == 408:
        return BackendError("api_timeout", "API request timed out.", retryable=True)
    if name in {"APIConnectionError", "ConnectError", "ConnectionError"}:
        return BackendError("api_connection", "Could not connect to the selected API provider.", retryable=True)
    if isinstance(status, int) and (status >= 500 or status == 409):
        return BackendError("api_transient", "The API returned a temporary service error.", retryable=True)
    if status in {400, 422} or name == "BadRequestError":
        return BackendError("invalid_api_request", "The provider rejected the configured model, schema, or request settings.")
    if isinstance(status, int) and 300 <= status < 400:
        return BackendError("redirect_blocked", "API redirects are disabled; use the configured provider endpoint.")
    return BackendError("api_error", "The API request failed; inspect configuration without logging request secrets.")


class OpenAIBackend:
    """A budgeted, stateless backend with one isolated request per decision.

    Constructing this class performs no network calls and does not require the
    optional SDK. The first decision lazily loads the SDK and local API key.
    ``max_attempts`` includes the initial request and is hard-capped at two.
    There are no SDK automatic retries, so every attempted call is accounted for.
    """

    def __init__(self, config: dict, *, api_key: str | None = None):
        self.api = api_settings(config)
        self.config = deepcopy(config)
        models = config.get("models", {})
        if not isinstance(models, dict):
            raise BackendError("invalid_config", "models must be a JSON object.")
        self.lawyer_model = models.get("lawyer", "gpt-6-sol")
        self.environment_model = models.get("environment", "gpt-5.6-sol")
        if not all(isinstance(item, str) and item.strip() for item in (self.lawyer_model, self.environment_model)):
            raise BackendError("invalid_config", "Both model identifiers must be non-empty strings.")
        self.reasoning_effort = config.get("reasoning_effort", "medium")
        if self.reasoning_effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise BackendError("invalid_config", "Unsupported reasoning_effort setting.")
        if self.lawyer_model == "gpt-6-astra" and self.reasoning_effort == "none":
            raise BackendError("invalid_config", "gpt-6-astra requires reasoning_effort low or above.")
        self.max_output_tokens = _positive_int(config, "max_output_tokens", 4000)
        self.max_model_calls = _positive_int(config, "max_model_calls", 120)
        self.max_input_chars = _positive_int(config, "max_input_chars", 160000)
        self.timeout_seconds = _positive_int(config, "timeout_seconds", 90)
        self.max_attempts = min(_positive_int(config, "max_attempts", 2), 2)
        self.calls: list[dict] = []
        self.stats: dict[str, Any] = {
            "attempted_calls": 0,
            "successful_decisions": 0,
            "failed_attempts": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
            "latency_seconds": 0.0,
            "by_model": {},
        }
        self._client: Any = None
        self._api_key = api_key.strip() if isinstance(api_key, str) else None

    def check_credentials(self) -> None:
        if not self._api_key and not os.environ.get(self.api["key_env"], "").strip():
            raise BackendError("missing_api_key", f"Set {self.api['key_env']} for the selected provider, or use --prompt-key in an interactive terminal.")

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        self.check_credentials()
        api_key = self._api_key or os.environ.get(self.api["key_env"], "").strip()
        try:
            from openai import OpenAI
            import httpx
        except ImportError:
            raise BackendError("sdk_missing", "Install the project requirements to enable OpenAI API calls.") from None
        try:
            self._client = OpenAI(
                api_key=api_key,
                base_url=self.api["base_url"],
                max_retries=0,
                timeout=self.timeout_seconds,
                http_client=httpx.Client(trust_env=False, follow_redirects=False, timeout=self.timeout_seconds),
            )
        except Exception:
            raise BackendError("client_initialization", "Could not initialize the API client.") from None
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        self._api_key = None

    def _record(self, role: str, model: str, attempt: int, elapsed: float, response: Any, error: BackendError | None) -> None:
        usage = _get(response, "usage", {}) or {}
        token_counts = {}
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            raw = _get(usage, key, 0)
            token_counts[key] = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0
        raw_cached = _get(_get(usage, "input_tokens_details", {}) or {}, "cached_tokens", 0)
        raw_reasoning = _get(_get(usage, "output_tokens_details", {}) or {}, "reasoning_tokens", 0)
        token_counts["cached_input_tokens"] = raw_cached if isinstance(raw_cached, int) and raw_cached >= 0 else 0
        token_counts["reasoning_tokens"] = raw_reasoning if isinstance(raw_reasoning, int) and raw_reasoning >= 0 else 0
        self.calls.append({
            "role_id": role,
            "model": model,
            "resolved_model": _get(response, "model", model),
            "attempt": attempt,
            "response_id": _get(response, "id"),
            "status": error.code if error else "completed",
            "latency_seconds": round(elapsed, 4),
            "usage": token_counts,
        })
        self.stats["latency_seconds"] += elapsed
        self.stats["failed_attempts"] += int(error is not None)
        for key, count in token_counts.items():
            self.stats[key] += count
        per_model = self.stats["by_model"].setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
        per_model["calls"] += 1
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            per_model[key] += token_counts[key]

    def decide(self, role_id: str, system: str, context: dict, schema: dict) -> dict:
        """Return a schema-conforming proposal, or explicitly fail the run.

        The caller constructs role-specific context; this adapter cannot infer
        which case facts a role may see. Never pass the complete authoring pack.
        An exhausted call/token budget is an error, never a synthetic verdict.
        """
        if not isinstance(role_id, str) or not role_id:
            raise BackendError("invalid_role", "A trusted role identifier is required.")
        if not isinstance(system, str) or not isinstance(context, dict):
            raise BackendError("invalid_context", "Role instructions and a context object are required.")
        _check_strict_schema(schema)
        try:
            payload = json.dumps(context, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            schema_text = json.dumps(schema, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (ValueError, TypeError, RecursionError):
            raise BackendError("invalid_context", "Context and schema must be JSON-serializable.") from None
        if len(system) + len(payload) + len(schema_text) > self.max_input_chars:
            raise BackendError("input_budget_exceeded", "The role context exceeds max_input_chars; no content was silently truncated.")
        model = self.lawyer_model if role_id == "R_DEFENSE" else self.environment_model
        client = self._get_client()
        last_error: BackendError | None = None
        for attempt in range(1, self.max_attempts + 1):
            if self.stats["attempted_calls"] >= self.max_model_calls:
                raise BackendError("call_budget_exceeded", "The run reached max_model_calls; no further model requests are allowed.")
            retry_note = ""
            if last_error and last_error.code in {"invalid_json", "schema_mismatch", "incomplete_output", "empty_output"}:
                retry_note = "\nReturn exactly one complete JSON action matching the provided schema. Keep text fields concise."
            if len(system) + len(retry_note) + len(payload) + len(schema_text) > self.max_input_chars:
                raise BackendError("input_budget_exceeded", "The retry would exceed max_input_chars.")
            self.stats["attempted_calls"] += 1
            started = time.monotonic()
            response = None
            error: BackendError | None = None
            parsed: dict | None = None
            try:
                response = client.responses.create(
                    model=model,
                    instructions=system + retry_note,
                    input=[{"role": "user", "content": payload}],
                    text={"format": {"type": "json_schema", "name": "court_action", "strict": True, "schema": schema}},
                    reasoning={"effort": self.reasoning_effort},
                    max_output_tokens=self.max_output_tokens,
                    store=False,
                )
                for item in _get(response, "output", []) or []:
                    for content in _get(item, "content", []) or []:
                        if _get(content, "type") == "refusal":
                            raise BackendError("model_refusal", "The model declined this decision; the run requires review.")
                status = _get(response, "status")
                if status == "incomplete":
                    reason = _get(_get(response, "incomplete_details", {}) or {}, "reason")
                    if reason == "content_filter":
                        raise BackendError("model_refusal", "The model could not complete this decision because of a content filter.")
                    raise BackendError("incomplete_output", "Model output was incomplete; adjust max_output_tokens if this persists.", retryable=True)
                if status != "completed":
                    raise BackendError("response_failed", "The OpenAI response did not complete successfully.")
                output_text = _get(response, "output_text", "")
                if not isinstance(output_text, str) or not output_text.strip():
                    raise BackendError("empty_output", "Model response contained no action text.", retryable=True)
                parsed = _json_object(output_text)
                if not _validate_value(parsed, schema, schema):
                    raise BackendError("schema_mismatch", "Model action did not match the action schema.", retryable=True)
            except BackendError as exc:
                error = exc
            except Exception as exc:
                error = _safe_api_error(exc)
            self._record(role_id, model, attempt, time.monotonic() - started, response, error)
            if error is None:
                self.stats["successful_decisions"] += 1
                assert parsed is not None
                return parsed
            last_error = error
            if not error.retryable or attempt == self.max_attempts:
                raise error from None
        raise BackendError("decision_failed", "The decision failed without producing an authorized action.")
