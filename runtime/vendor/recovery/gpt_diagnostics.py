"""Narrow, in-memory error observability for the unchanged Codex adapter.

No prompt, output, CLI event acceptance rule, model choice, or retry budget is
changed here. Only explicit HTTP/transport failures previously collapsed into
``codex_request_failed`` receive a more precise error code. Public diagnostic
fields are categorical templates. An optional private sink retains redacted,
bounded error text separately for diagnosis of previously unknown failures.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import re
from urllib.parse import quote, quote_plus


VERSION = "gpt-diagnostics-1.0.0"
_MARKER = "_court_recovery_gpt_diagnostics_active"
_STATUS = re.compile(
    r"\b(?:unexpected\s+status(?:\s+code)?\s*:?|"
    r"HTTP(?:/\d(?:\.\d)?)?\s+(?:status(?:\s+code)?\s*:?\s*)?|"
    r"status(?:[_\s]+code)?\s*[:=])\s*([1-5]\d{2})\b",
    re.IGNORECASE,
)
_REQUEST_ID = re.compile(
    r"\brequest(?:[_\s]+(?:ori[_\s]+)?id)\s*[\"']?\s*[:=]\s*[\"']?"
    r"([A-Za-z0-9][A-Za-z0-9_-]{5,127})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_TOKEN = re.compile(r"\b(?:sk|ark)-[A-Za-z0-9_-]+|\bBearer\s+[^\s\"'<>]+", re.IGNORECASE)
_TRANSPORT_MARKERS = (
    ("stream_disconnected", "stream disconnected before completion"),
    ("connection_reset", "connection reset by peer"),
    ("connection_closed", "connection closed before message completed"),
    ("request_send_failed", "error sending request"),
)
_TLS_MARKERS = ("certificate", "invalid peer", "tls handshake", "ssl error")


def redact_private_failure(text, *, secrets=(), limit=4000):
    """Redact credentials before bounding a server-private diagnostic.

    This string can still contain provider-reflected case text. It must only be
    sent to the explicit private sink (0600), never to a public report/console.
    """
    text = text if isinstance(text, str) else ""
    values = tuple(s for s in secrets if isinstance(s, str) and s)
    variants = set()
    for secret in values:
        variants.update((secret, json.dumps(secret, ensure_ascii=True)[1:-1],
                         json.dumps(secret, ensure_ascii=False)[1:-1],
                         quote(secret, safe=""), quote_plus(secret, safe="")))
    for variant in sorted(variants, key=len, reverse=True):
        text = text.replace(variant, "[REDACTED]")
    text = _TOKEN.sub("[REDACTED]", text)
    text = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(
        r'''(?i)((?:api[_-]?key|access[_-]?token|authorization|password|secret)["']?\s*[:=]\s*)["']?[^\s,;}]+''',
        r"\1[REDACTED]", text,
    )
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32)
    marker = " [truncated]"
    return text if len(text) <= limit else text[:max(0, limit - len(marker))] + marker


def _message(text):
    """Read only the error envelope message, never traverse model/user content."""
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return text, None
    if not isinstance(value, dict):
        return text, None
    event = value.get("type")
    if event == "error" and isinstance(value.get("message"), str):
        return value["message"], "error"
    if event == "turn.failed" and isinstance(value.get("error"), dict):
        detail = value["error"].get("message")
        if isinstance(detail, str):
            return detail, "turn.failed"
    # No recursively collected messages: unrecognized envelopes remain unknown.
    return "", event if event in {"error", "turn.failed"} else None


def describe_failure(text, original_code, *, secrets=()):
    """Return a safe diagnostic and optional precise replacement error code.

    ``original_code`` always has priority unless it is the adapter's catchall.
    Ambiguous status lines and unknown provider messages are not retried merely
    because they contain a number. All returned strings come from this module
    except a tightly filtered request identifier and a SHA-256 fingerprint.
    """
    text = text if isinstance(text, str) else ""
    values = tuple(s for s in secrets if isinstance(s, str) and s)
    redacted = text
    for secret in sorted(set(values), key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = _TOKEN.sub("[REDACTED]", redacted)
    detail, event_type = _message(redacted)
    statuses = sorted({int(s) for s in _STATUS.findall(detail)})
    request_ids = []
    for found in _REQUEST_ID.findall(detail):
        if (found.lower().startswith(("sk-", "ark-"))
                or any(secret in found or found in secret for secret in values)):
            continue
        if found not in request_ids:
            request_ids.append(found)
    safe = {
        "protocol": VERSION,
        "http_status": statuses[0] if len(statuses) == 1 else None,
        "error_family": "unclassified",
        "request_ids": request_ids[:4],
        "message_sha256": hashlib.sha256(redacted.encode("utf-8", errors="replace")).hexdigest(),
        "last_event_type": event_type,
        "detail": "Unclassified Codex failure; no free-text body is persisted.",
    }
    replacement = None
    if len(statuses) > 1:
        safe.update(error_family="ambiguous_http_status", detail="Multiple HTTP status values were reported; original classification is retained.")
    elif statuses:
        status = statuses[0]
        safe.update(error_family="http_error", detail=f"Provider returned HTTP {status}.")
        if original_code == "codex_request_failed":
            if status in {500, 502, 503, 504}:
                replacement = "api_transient"
                safe["error_family"] = "http_transient"
            elif status == 408:
                replacement = "api_timeout"
                safe["error_family"] = "http_timeout"
    else:
        lowered = detail.lower()
        if any(marker in lowered for marker in _TLS_MARKERS):
            safe.update(error_family="tls_configuration", detail="A certificate or TLS configuration failure was reported; no automatic retry classification was added.")
        else:
            for family, marker in _TRANSPORT_MARKERS:
                if marker in lowered:
                    safe.update(error_family=family, detail=f"Codex reported the recognized transport condition: {family}.")
                    if original_code == "codex_request_failed":
                        replacement = "api_connection"
                    break
    return safe, replacement


@contextmanager
def install_gpt_diagnostics(codex_module, *, sink=None, private_sink=None, secrets=()):
    """Temporarily instrument one worker process; not a thread-global service.

    The optional sink receives only the same safe metadata saved in model_calls.
    The separate private_sink receives the redacted, bounded provider error,
    including unknown messages. Its owner must write a private 0600 JSONL file
    and must not copy this field into public results or terminal output.
    Use separate processes for concurrent courts. The original methods are
    restored even if a court or sink raises. No on-disk runtime is modified.
    """
    if getattr(codex_module, _MARKER, False):
        raise RuntimeError("GPT diagnostic instrumentation is already installed in this process.")
    original_classifier = codex_module._cli_error
    cls = codex_module.CodexCLIBackend
    had_local_record = "_record" in cls.__dict__
    original_record = cls._record
    secrets = tuple(secrets)

    def classify(text):
        error = original_classifier(text)
        original_code = getattr(error, "code", None)
        safe, replacement = describe_failure(text, getattr(error, "code", None), secrets=secrets)
        if replacement:
            message = ("The provider explicitly reported a transient HTTP failure."
                       if replacement == "api_transient" else
                       "The provider explicitly reported an HTTP request timeout."
                       if replacement == "api_timeout" else
                       "Codex explicitly reported a transport connection failure.")
            error = type(error)(replacement, message)
        error.transport_diagnostics = safe
        if private_sink is not None:
            # Record at classification time: an unrecognized diagnostic notice
            # can later be wrapped by the unchanged event parser, losing custom
            # exception attributes. This observation is not a retry or decision.
            private_sink({
                "source": "codex_cli_classifier",
                "original_error_code": original_code,
                "classified_error_code": getattr(error, "code", None),
                "transport_diagnostics": deepcopy(safe),
                "provider_error": redact_private_failure(text, secrets=secrets),
            })
        return error

    def record(self, role, model, attempt, elapsed, response, error):
        original_record(self, role, model, attempt, elapsed, response, error)
        safe = getattr(error, "transport_diagnostics", None) if error else None
        if not isinstance(safe, dict):
            return
        self.calls[-1]["transport_diagnostics"] = deepcopy(safe)
        if sink is not None:
            sink({"role_id": role, "model": model,
                  "error_code": getattr(error, "code", None),
                  "transport_diagnostics": deepcopy(safe)})

    setattr(codex_module, _MARKER, True)
    codex_module._cli_error = classify
    cls._record = record
    try:
        yield
    finally:
        codex_module._cli_error = original_classifier
        if had_local_record:
            cls._record = original_record
        else:
            delattr(cls, "_record")
        delattr(codex_module, _MARKER)
