"""Fresh-run v4 court protocol; never edits or imports old court ledgers."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time
import threading
import uuid

PROTOCOL = "legal-three-rounds-court-v1"
POLICY = {
    "protocol": PROTOCOL,
    "max_attempts_per_logical_decision": 8,
    "max_case_model_invocations": 360,
    "retryable": ["invalid_json", "schema_mismatch", "empty_output", "incomplete_output",
                  "api_connection", "api_timeout", "api_transient", "rate_limited"],
    "unknown_outcome": "stop_without_resend",
    "retry_request": "identical_no_feedback_no_fallback",
    "timeout_seconds": 300, "configured_max_output_tokens": 16384,
    "nullable_default_fields": ["purpose", "query", "scope", "date_from", "date_to", "objection_id"],
    "empty_array_default_fields": ["details"],
    "core_fields_never_synthesized": ["action", "text", "target", "references"],
    "normalization_candidate": "opus_only_strict_valid_json",
    "judge_contract": "authorized_law_enums_nonblank_reason_admission_purpose_required",
}
FORMAT_INSTRUCTION = (
    "\n输出格式补充：严格输出一个完整 JSON 对象，包含 schema 的所有 required 字段。"
    "无适用内容的可空字段仍须显式写 null；若 schema 包含 details，且无参数，则写 []。"
    "字符串内的引号、反斜线和换行必须按 JSON 规则转义；请完整序列化，勿拼接 JSON 片段。"
    "保留实质论述，不用占位文字代替回答；不输出 Markdown 包裹或思维过程。"
)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def request_contract(role, system, context, schema):
    schema = deepcopy(schema)
    if role == "R_JUDGE":
        laws = sorted({row[key] for row in context.get("legal_sources", []) if isinstance(row, dict)
                       for key in ("legal_source_id", "citation_id") if isinstance(row.get(key), str)})
        targets = [schema]
        rulings = schema.get("properties", {}).get("rulings", {})
        if isinstance(rulings.get("items"), dict):
            targets.append(rulings["items"])
        for target in targets:
            props = target.get("properties", {})
            if {"decision", "reason", "legal_source_ids"}.issubset(props) and laws:
                props["legal_source_ids"] = {"type": "array", "minItems": 1,
                                              "items": {"type": "string", "enum": laws}}
                props["reason"] = {**props["reason"], "minLength": 1, "pattern": r"\S"}
    return system + FORMAT_INSTRUCTION, schema


def nullable_defaults(value, schema, validate):
    """Only fill the listed absent representation fields; never repair JSON."""
    if not isinstance(value, dict) or not {"action", "text", "target", "references"}.issubset(value):
        return value, []
    props = schema.get("properties", {})
    if not {"action", "text", "target", "references", "details"}.issubset(props):
        return value, []
    normalized, fields = deepcopy(value), []
    for field in POLICY["nullable_default_fields"]:
        if field not in value and field in schema.get("required", []) and field in props:
            if validate(None, props[field], schema):
                normalized[field] = None
                fields.append(field)
    if "details" not in value and "details" in schema.get("required", []):
        if validate([], props["details"], schema):
            normalized["details"] = []
            fields.append("details")
    if fields and validate(normalized, schema, schema):
        return normalized, fields
    return value, []


def judge_purpose_valid(role, answer):
    if role != "R_JUDGE" or not isinstance(answer, dict):
        return True
    rows = answer.get("rulings", [answer])
    if not isinstance(rows, list):
        return True  # The ordinary schema validator reports the shape failure.
    return all(not isinstance(row, dict) or row.get("decision") not in {"admitted", "limited_use"}
               or (isinstance(row.get("permitted_purposes"), list) and row["permitted_purposes"]
                   and all(isinstance(x, str) and x.strip() for x in row["permitted_purposes"]))
               for row in rows)


@contextmanager
def court_recovery_overlay(manifest_digest, *, package_backends=None, sleep=time.sleep, stop_requested=None):
    """Use inside runtime_overlay(..., court_retries=False), around fresh run_case.

    Journal inputs are tightened BEFORE their durable request is created, so the
    journal, attempt ledger and actual adapter request retain identical bytes.
    The new ledger policy rejects any old two-attempt experiment ledger.
    """
    if package_backends is None:
        import backends as package_backends
    vendor = package_backends.bootstrap()
    import reliability
    import journal
    from court_agent.llm import BackendError, _json_object, _validate_value

    if (not isinstance(manifest_digest, str) or len(manifest_digest) != 64
            or any(c not in "0123456789abcdef" for c in manifest_digest)):
        raise ValueError("invalid_v4_manifest_digest")
    if getattr(reliability, "_v4_court_recovery_active", False):
        raise RuntimeError("v4_court_overlay_already_active")
    if getattr(reliability, "_serial_transport_retry_overlay_active", False):
        raise RuntimeError("v4_requires_runtime_overlay_court_retries_false")
    old_policy = vendor.COMPARISON_POLICY
    policy = {**deepcopy(old_policy), "protocol": PROTOCOL,
              "lawyer_max_attempts_per_decision": 8, "environment_max_attempts_per_decision": 8,
              "max_case_model_invocations": 360, "automatic_retries": "format_and_transient_all_roles",
              "v4_policy": deepcopy(POLICY), "v4_manifest_sha256": manifest_digest}
    patches, ceilings = [], {}

    def patch(obj, name, value):
        patches.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def fail(code):
        raise BackendError(code, "V4 court request stopped; durable history was not reset.")

    old_journal_decide = journal.HashChainedDecisionJournal.decide
    def journal_decide(self, role_id, system, context, schema):
        if stop_requested is not None and stop_requested():
            fail("operator_requested_stop")
        system, schema = request_contract(role_id, system, context, schema)
        return old_journal_decide(self, role_id, system, context, schema)

    def reserve(self, logical, request_hash, route):
        if self.policy != policy:
            fail("v4_ledger_policy_mismatch")
        rows = self.prior(logical, request_hash)
        finishes = {r["invocation_id"]: r for r in self.rows if r["kind"] == "finish"}
        if any(r["invocation_id"] not in finishes or finishes[r["invocation_id"]]["status"] == "interrupted_outcome_unknown"
               for r in self.reserves):
            fail("unknown_outcome_requires_review")
        if any(row["route"] != route for row in rows):
            fail("attempt_route_changed")
        if self.reserves and logical < self.reserves[-1]["logical_decision"]:
            fail("attempt_logical_order_changed")
        if any(finishes[r["invocation_id"]]["status"] == "passed" for r in rows):
            fail("accepted_decision_not_replayed")
        if rows and finishes[rows[-1]["invocation_id"]].get("error_code") not in POLICY["retryable"]:
            fail("prior_attempt_not_retryable")
        if len(rows) >= 8:
            fail("decision_attempt_budget_exhausted")
        if len(self.reserves) >= POLICY["max_case_model_invocations"]:
            fail("case_invocation_budget_exhausted")
        invocation, attempt = len(self.reserves) + 1, len(rows) + 1
        self.append({"kind": "reserve", "invocation_id": invocation, "logical_decision": logical,
                     "request_hash": request_hash, "route": route, "attempt_for_decision": attempt,
                     "recovery_protocol": PROTOCOL, "recovery_policy_sha256": digest(POLICY),
                     "recovery_manifest_sha256": manifest_digest})
        return invocation, attempt

    def decide(self, role_id, system, context, schema):
        request = deepcopy({"role_id": role_id, "system": system, "context": context, "schema": schema})
        logical, request_hash = self._logical_request(request)
        route = "lawyer" if role_id == "R_DEFENSE" else "environment"
        for obj in (self.inner, getattr(self.inner, "lawyer_backend", None), getattr(self.inner, "environment_backend", None)):
            if obj is not None and hasattr(obj, "max_model_calls") and id(obj) not in ceilings:
                ceilings[id(obj)] = (obj, obj.max_model_calls)
                obj.max_model_calls = POLICY["max_case_model_invocations"]
        while True:
            if stop_requested is not None and stop_requested():
                fail("operator_requested_stop")
            invocation, attempt = self.ledger.reserve(logical, request_hash, route)
            before = len(self.inner.calls)
            stage = context.get("stage_id", "preflight")
            task = context.get("task", "decision")
            print(f"[court] Decision {logical} | {role_id} | {route} | {stage} | {task} | attempt {attempt}/8 starting", flush=True)
            started = time.monotonic()
            heartbeat_done = threading.Event()
            def heartbeat():
                while not heartbeat_done.wait(30):
                    print(f"[等待模型] Decision {logical} | {role_id} | {stage} | 已等待 {time.monotonic()-started:.0f}s；请求尚未返回。", flush=True)
            threading.Thread(target=heartbeat, daemon=True).start()
            try:
                answer = self.inner.decide(**deepcopy(request))
            except BaseException as exc:
                code = getattr(exc, "code", "interrupted" if isinstance(exc, KeyboardInterrupt) else "local_backend_error")
                self.ledger.finish(invocation, "failed", code, self.inner.calls[before:])
                print(f"[court] Decision {logical} attempt {attempt}/8 failed: {code}", flush=True)
                if not isinstance(exc, BackendError) or code not in POLICY["retryable"] or attempt >= 8:
                    raise
                delay = (10, 20, 40, 60)[min(attempt - 1, 3)] if code in reliability.TRANSIENT_ERRORS else 3
                print(f"[retry] Same request; wait {delay}s. Rejected output is not accepted.", flush=True)
                heartbeat_done.set()
                sleep(delay)
                continue
            finally:
                heartbeat_done.set()
            print(f"[court] Decision {logical} completed ({time.monotonic()-started:.1f}s) | {role_id} | {stage}", flush=True)
            self.ledger.finish(invocation, "passed", None, self.inner.calls[before:])
            return answer

    capture = package_backends._FinalCapture
    old_capture_decide, old_invoke, old_process = capture.decide, capture._invoke, capture._run_process

    def capture_decide(self, role_id, system, context, schema):
        self._v4_request = {"role_id": role_id, "system": system, "context": context, "schema": schema}
        try:
            return old_capture_decide(self, role_id, system, context, schema)
        finally:
            self._v4_request = None

    def invoke(self, *args, **kwargs):
        text = old_invoke(self, *args, **kwargs)
        req = getattr(self, "_v4_request", None)
        if not req or getattr(self, "candidate_label", None) != "opus":
            return text
        value = _json_object(text)  # Syntax errors remain errors, never repaired.
        normalized, fields = nullable_defaults(value, req["schema"], _validate_value)
        if not fields:
            return text
        directory = getattr(self, "diagnostic_dir", None)
        if directory is None:
            fail("normalization_audit_directory_missing")
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        audit = {"kind": "nullable_field_normalization", "protocol": PROTOCOL,
                 "request_sha256": digest(req), "filled_fields": fields,
                 "original_final_text": self._last_final, "normalized_response": normalized,
                 "controller_acceptance": "consult_private_model_io_journal", "thinking_retained": False}
        path = directory / ("normalization-" + uuid.uuid4().hex + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(audit, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    def run_process(self, arguments, payload, directory, state):
        result = old_process(self, arguments, payload, directory, state)
        req = getattr(self, "_v4_request", None)
        path = Path(directory) / "decision.json"
        if req and req["role_id"] == "R_JUDGE" and path.is_file() and not path.is_symlink():
            answer = _json_object(path.read_text(encoding="utf-8"))
            if not judge_purpose_valid(req["role_id"], answer):
                raise BackendError("schema_mismatch", "Admitted evidence requires explicit nonblank permitted purposes; no purpose was invented.")
        return result

    reliability._v4_court_recovery_active = True
    try:
        patch(vendor, "COMPARISON_POLICY", policy)
        patch(journal.HashChainedDecisionJournal, "decide", journal_decide)
        patch(reliability.AttemptLedger, "reserve", reserve)
        patch(reliability.ReliableBackend, "decide", decide)
        patch(capture, "decide", capture_decide)
        patch(capture, "_invoke", invoke)
        patch(capture, "_run_process", run_process)
        yield deepcopy(POLICY)
    finally:
        for obj, name, original in reversed(patches):
            setattr(obj, name, original)
        for obj, maximum in ceilings.values():
            obj.max_model_calls = maximum
        del reliability._v4_court_recovery_active
