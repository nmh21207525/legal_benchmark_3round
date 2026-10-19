"""Finite identical-request retries, durably counted before dispatch.

The source DecisionJournal has already appended the role-scoped request when
this backend is called. Accepted decisions are still replayed by that journal.
This separate ledger counts rejected and interrupted attempts across resumes;
it contains metadata, never keys, prompt text, answers, or reasoning.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time

from court_agent.llm import BackendError

TRANSIENT_ERRORS = {"api_timeout", "api_connection", "api_transient", "rate_limited"}
FORMAT_ERRORS = {"schema_mismatch", "invalid_json", "empty_output", "incomplete_output"}
REVIEW_REQUIRED_ERRORS = {
    "tool_use_blocked", "codex_tool_use_blocked", "model_identity_mismatch", "codex_model_rerouted",
    "claude_protocol_error", "codex_protocol_error", "codex_notice_requires_review",
    "codex_event_stream_incomplete", "claude_prompt_screening_detected", "claude_prompt_changed", "claude_turn_budget",
}
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens")


def value_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def strict_json(text):
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                raise ValueError("duplicate_json_key")
            d[k] = v
        return d
    def constant(_):
        raise ValueError("invalid_json_number")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def safe_call(call):
    fields = {"role_id", "model", "attempt", "status", "latency_seconds", "backend_route",
              "transport", "candidate", "provider_stop_reason", "answer_text_characters", "schema_errors",
              "configured_max_output_tokens", "max_output_tokens_enforced", "last_cli_event",
              "last_cli_event_shape", "cli_notice_counts", "cli_version", "cli_reported_turns"}
    result = {k: deepcopy(v) for k, v in call.items() if k in fields}
    usage = call.get("usage", {})
    result["usage"] = {k: v for k in TOKEN_FIELDS if type(v := usage.get(k)) is int and v >= 0}
    return result


class AttemptLedger:
    def __init__(self, path: Path, policy: dict):
        self.path = path
        self.policy = deepcopy(policy)
        self.rows = []
        self.previous_hash = "0" * 64
        if path.exists():
            try:
                raw = path.read_bytes()
                if not raw.endswith(b"\n"):
                    raise ValueError("truncated_ledger")
                for line in raw.decode("utf-8").splitlines():
                    record = strict_json(line)
                    payload = {k: v for k, v in record.items() if k != "record_sha256"}
                    if payload.get("previous_sha256") != self.previous_hash or value_hash(payload) != record.get("record_sha256"):
                        raise ValueError("ledger_hash_mismatch")
                    self.rows.append(record)
                    self.previous_hash = record["record_sha256"]
                if not self.rows or self.rows[0].get("kind") != "policy" or self.rows[0].get("policy") != self.policy:
                    raise ValueError("ledger_policy_mismatch")
                self._validate_records()
            except (OSError, UnicodeError, ValueError, KeyError, TypeError):
                raise BackendError("attempt_ledger_invalid", "Attempt history failed validation; it was not reset or edited.") from None
        else:
            # A missing ledger beside an existing accepted/error journal is not
            # a fresh experiment; it could otherwise reset all consumed budgets.
            journal = self.path.parent / "private_model_io.jsonl"
            if journal.exists() and journal.stat().st_size:
                raise BackendError("attempt_ledger_missing", "A prior court journal exists without its v2 attempt ledger.")
            path.parent.mkdir(parents=True, exist_ok=True)
            self.append({"kind": "policy", "policy": self.policy})

    def _validate_records(self):
        reserves, finished, last_logical = {}, set(), 0
        for row in self.rows[1:]:
            n = row.get("invocation_id")
            if type(n) is not int or n < 1:
                raise ValueError("bad_invocation")
            if row.get("kind") == "reserve":
                logical = row.get("logical_decision")
                if n != len(reserves) + 1 or type(logical) is not int or logical < last_logical:
                    raise ValueError("bad_reservation_order")
                if row.get("route") not in {"lawyer", "environment"} or not isinstance(row.get("request_hash"), str):
                    raise ValueError("bad_reservation")
                reserves[n] = row
                last_logical = logical
            elif row.get("kind") == "finish":
                if n not in reserves or n in finished or row.get("status") not in {"passed", "failed", "interrupted_outcome_unknown"}:
                    raise ValueError("bad_finish")
                finished.add(n)
            else:
                raise ValueError("bad_ledger_kind")

    def append(self, payload):
        record = {**deepcopy(payload), "previous_sha256": self.previous_hash}
        record["record_sha256"] = value_hash(record)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.rows.append(record)
        self.previous_hash = record["record_sha256"]

    @property
    def reserves(self):
        return [r for r in self.rows if r["kind"] == "reserve"]

    def prior(self, logical, request_hash):
        rows = [r for r in self.reserves if r["logical_decision"] == logical]
        if any(r["request_hash"] != request_hash for r in rows):
            raise BackendError("attempt_request_changed", "This logical decision has a different saved request.")
        return rows

    def reserve(self, logical, request_hash, route):
        rows = self.prior(logical, request_hash)
        limit = self.policy["lawyer_max_attempts_per_decision" if route == "lawyer" else "environment_max_attempts_per_decision"]
        if len(rows) >= limit:
            raise BackendError("decision_attempt_budget_exhausted", "This logical decision already used its two allowed attempts; resume cannot reset them.")
        if len(self.reserves) >= self.policy["max_case_model_invocations"]:
            raise BackendError("case_invocation_budget_exhausted", "The persistent per-case invocation budget is exhausted.")
        finishes = {r["invocation_id"]: r for r in self.rows if r["kind"] == "finish"}
        for previous in rows:
            result = finishes.get(previous["invocation_id"])
            if result and result["status"] == "passed":
                raise BackendError("accepted_decision_not_replayed", "A successful response was not recovered from its court journal; no duplicate request was made.")
            if result and result.get("error_code") in REVIEW_REQUIRED_ERRORS:
                raise BackendError("prior_attempt_requires_review", "A prior safety or protocol failure needs review; it is not retried.")
            if result is None:
                self.append({"kind": "finish", "invocation_id": previous["invocation_id"],
                             "status": "interrupted_outcome_unknown", "error_code": "interrupted_outcome_unknown", "calls": []})
        n = len(self.reserves) + 1
        self.append({"kind": "reserve", "invocation_id": n, "logical_decision": logical,
                     "request_hash": request_hash, "route": route, "attempt_for_decision": len(rows) + 1})
        return n, len(rows) + 1

    def finish(self, invocation, status, error_code, calls):
        self.append({"kind": "finish", "invocation_id": invocation, "status": status,
                     "error_code": error_code, "calls": [safe_call(c) for c in calls]})

    def totals(self):
        reserves = self.reserves
        finishes = [r for r in self.rows if r["kind"] == "finish"]
        keys = {(r["logical_decision"], r["request_hash"]) for r in reserves if r["route"] == "lawyer"}
        lawyer_attempts = sum(r["route"] == "lawyer" for r in reserves)
        usage = {field: sum(c.get("usage", {}).get(field, 0) for r in finishes for c in r.get("calls", [])) for field in TOKEN_FIELDS}
        known = {r["invocation_id"] for r in finishes if r["status"] != "interrupted_outcome_unknown"}
        return {"invocation_reservations": len(reserves), "lawyer_attempts": lawyer_attempts,
                "lawyer_retry_attempts": lawyer_attempts - len(keys),
                "unknown_outcome_invocations": sum(r["invocation_id"] not in known for r in reserves), "reported_usage": usage}


class ReliableBackend:
    def __init__(self, inner, ledger_path: Path, policy: dict):
        self.inner = inner
        self.ledger = AttemptLedger(ledger_path, policy)
        self.policy = deepcopy(policy)
        self.journal_path = ledger_path.parent / "private_model_io.jsonl"

    @property
    def calls(self):
        return self.inner.calls

    @property
    def stats(self):
        return {**deepcopy(self.inner.stats), "comparison_policy": deepcopy(self.policy),
                "cumulative_case_attempts": self.ledger.totals()}

    def check_credentials(self):
        self.inner.check_credentials()

    def _logical_request(self, request):
        try:
            lines = self.journal_path.read_text(encoding="utf-8").splitlines()
            last = strict_json(lines[-1])
            n = last["logical_decision"]
            digest = value_hash(request)
            if (last.get("kind") != "request" or type(n) is not int or n < 1
                    or last.get("request_hash") != digest or last.get("request") != request):
                raise ValueError("request_binding")
            return n, digest
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, IndexError):
            raise BackendError("missing_or_changed_journal_request", "The requested decision is not bound to the court journal.") from None

    def decide(self, role_id, system, context, schema):
        req = {"role_id": role_id, "system": system, "context": context, "schema": schema}
        logical, digest = self._logical_request(req)
        route = "lawyer" if role_id == "R_DEFENSE" else "environment"
        max_tries = self.policy["lawyer_max_attempts_per_decision" if route == "lawyer" else "environment_max_attempts_per_decision"]
        retryable = TRANSIENT_ERRORS | (FORMAT_ERRORS if route == "lawyer" else set())
        while True:
            invocation, attempt = self.ledger.reserve(logical, digest, route)
            before = len(self.inner.calls)
            if route == "lawyer":
                print(f"[court] Lawyer decision {logical} | {self.inner.lawyer_backend.model} | attempt {attempt}/{max_tries} starting", flush=True)
            started = time.monotonic()
            try:
                result = self.inner.decide(role_id, system, context, schema)
            except BaseException as exc:
                code = getattr(exc, "code", "interrupted" if isinstance(exc, KeyboardInterrupt) else "local_backend_error")
                self.ledger.finish(invocation, "failed", code, self.inner.calls[before:])
                print(f"[court] Decision {logical} attempt {attempt}/{max_tries} failed: {code}", flush=True)
                if not isinstance(exc, BackendError) or code not in retryable or attempt >= max_tries:
                    raise
                delay = 15 if code == "rate_limited" else 3 if code in TRANSIENT_ERRORS else 0
                print(f"[court] Retrying the same request once; rejected output is not accepted. Wait {delay}s.", flush=True)
                if delay:
                    time.sleep(delay)
                continue
            self.ledger.finish(invocation, "passed", None, self.inner.calls[before:])
            if route == "lawyer":
                print(f"[court] Lawyer decision {logical} completed ({time.monotonic()-started:.1f}s)", flush=True)
            return result

    def close(self):
        self.inner.close()


def schema_errors(value, schema, validate, root=None, path="$", depth=0):
    """Explain strict validation without printing values or unknown field names."""
    root = schema if root is None else root
    if validate(value, schema, root):
        return []
    if depth > 40:
        return [{"path": path, "error": "depth_limit"}]
    if "$ref" in schema:
        resolved = root
        try:
            for key in schema["$ref"][2:].split("/"):
                resolved = resolved[key.replace("~1", "/").replace("~0", "~")]
            return schema_errors(value, resolved, validate, root, path, depth + 1)
        except (KeyError, TypeError):
            return [{"path": path, "error": "schema_reference"}]
    errors = []
    checks = {"object": isinstance(value, dict), "array": isinstance(value, list),
              "string": isinstance(value, str), "integer": type(value) is int,
              "number": type(value) in (int, float), "boolean": type(value) is bool, "null": value is None}
    types = schema.get("type")
    types = types if isinstance(types, list) else [types] if types else []
    if types and not any(checks.get(t, False) for t in types):
        return [{"path": path, "error": "wrong_type", "expected": types, "actual_type": type(value).__name__}]
    if "enum" in schema and value not in schema["enum"]:
        errors.append({"path": path, "error": "not_in_allowed_enum"})
    if "const" in schema and value != schema["const"]:
        errors.append({"path": path, "error": "wrong_constant"})
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append({"path": path + "/" + key, "error": "missing_required_field"})
        extras = set(value) - set(props)
        if extras and schema.get("additionalProperties") is False:
            errors.append({"path": path, "error": "extra_fields", "count": len(extras)})
        for key, child in props.items():
            if key in value:
                errors.extend(schema_errors(value[key], child, validate, root, path + "/" + key, depth + 1))
    elif isinstance(value, list) and "items" in schema:
        for i, child in enumerate(value):
            errors.extend(schema_errors(child, schema["items"], validate, root, path + f"/{i}", depth + 1))
            if len(errors) >= 40:
                break
    return (errors or [{"path": path, "error": "schema_constraint_failed"}])[:40]
