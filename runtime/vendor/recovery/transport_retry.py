"""Versioned, process-scoped transport recovery for the frozen v2 ledger.

This is an explicitly different invocation policy, not a reset of the original
two-attempt experiment. No request, accepted response, old policy row, or old
hash-chain record is edited. A reservation is fsynced by the original ledger
before every dispatch. Only explicit transient failures unlock extra attempts.

Accounting is conservative and durable across processes:
* at most eight reservations for one identical logical request;
* at most two nontransport outcomes (unfinished/unknown also consume a slot);
* at most 240 such nontransport reservations across the case;
* at most 120 recovery reservations whose immediately preceding outcome was
  explicitly transient (including a recovery reservation that later succeeds);
* at most 360 reservations overall. Historical recovery reservations are
  inferred from their preceding finish, never relabelled on disk.

The 120 limit counts reservation causes, not predicted future failures. This
allows a strict pre-dispatch limit. Report it as recovery reservations, not as
120 observed failed responses. Unknown outcomes never unlock the extension.
Use in a serial worker process; this patches classes and is not thread-safe.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import re


RETRY_POLICY = {
    "protocol": "three-lawyers-serial-transport-recovery-v1",
    "max_attempts_per_logical_decision": 8,
    "max_nontransport_attempts_per_logical_decision": 2,
    "max_case_nontransport_reservations": 240,
    "max_case_transport_recovery_reservations": 120,
    "max_case_total_reservations": 360,
    "transport_error_codes": ["api_connection", "api_timeout", "api_transient", "rate_limited"],
    "backoff_seconds": [10, 20, 40, 60],
    "retry_request": "identical_no_feedback_no_fallback",
    "nontransport_accounting": "passed_nontransient_failed_and_unfinished_or_unknown",
    "recovery_accounting": "reservation_immediately_after_explicit_transient_failure",
    "unknown_outcomes": "only_original_remaining_total_attempt_budget",
    "accepted_decisions": "journal_replay_only",
    "original_policy_and_ledger_prefix": "preserved",
}
_MARKER = "_serial_transport_retry_overlay_active"
_TRANSIENT = frozenset(RETRY_POLICY["transport_error_codes"])
_REVIEW_ERRORS = frozenset({
    "tool_use_blocked", "codex_tool_use_blocked", "model_identity_mismatch",
    "codex_model_rerouted", "claude_protocol_error", "codex_protocol_error",
    "codex_notice_requires_review", "codex_event_stream_incomplete",
    "claude_prompt_screening_detected", "claude_prompt_changed", "claude_turn_budget",
})


def _transport_finish(finish):
    return bool(finish and finish.get("status") == "failed"
                and finish.get("error_code") in _TRANSIENT)


def _history(ledger):
    finishes = {r["invocation_id"]: r for r in ledger.rows if r["kind"] == "finish"}
    previous, recoveries, nontransport, transport_failures = {}, 0, 0, 0
    for row in ledger.reserves:
        key = (row["logical_decision"], row["request_hash"])
        prior = previous.get(key)
        recovery = bool(prior and _transport_finish(finishes.get(prior["invocation_id"])))
        recoveries += int(recovery)
        result = finishes.get(row["invocation_id"])
        transport_failures += int(_transport_finish(result))
        nontransport += int(not _transport_finish(result))
        previous[key] = row
    return finishes, {
        "total_reservations": len(ledger.reserves),
        "nontransport_reservations": nontransport,
        "transport_recovery_reservations": recoveries,
        "explicit_transient_failed_outcomes": transport_failures,
    }


@contextmanager
def transport_retry_overlay(reliability, manifest_digest=None, *, approvals=(), manifest_sha256=None):
    """Extend transport retries only, restoring all classes and ceilings on exit.

    ``approvals`` is the old reviewed manifest's list of dictionaries containing
    ``ledger_path`` and ``finish_sha256``. Only that exact historical lawyer
    model_identity_mismatch can use its remaining *original* second attempt.
    An approval never accepts the rejected response or allows new identity or
    tool failures. Callers must supply only the externally validated manifest.
    ``manifest_sha256`` is an alias for ``manifest_digest``.
    """
    if getattr(reliability, _MARKER, False):
        raise RuntimeError("transport_retry_overlay_already_active")
    if manifest_sha256 is not None:
        if manifest_digest is not None and manifest_digest != manifest_sha256:
            raise ValueError("conflicting_manifest_digests")
        manifest_digest = manifest_sha256
    if manifest_digest is not None and not re.fullmatch(r"[0-9a-f]{64}", manifest_digest):
        raise ValueError("invalid_manifest_digest")
    approved = frozenset((str(Path(a["ledger_path"]).resolve()), a["finish_sha256"])
                         for a in approvals)
    policy = deepcopy(RETRY_POLICY)
    policy_hash = reliability.value_hash(policy)
    BackendError = reliability.BackendError
    ledger_cls, backend_cls = reliability.AttemptLedger, reliability.ReliableBackend
    old_reserve, old_decide = ledger_cls.reserve, backend_cls.decide
    old_stats, old_safe = backend_cls.stats, reliability.safe_call
    ceilings = {}

    def fail(code, message):
        raise BackendError(code, message)

    def is_approved(ledger, result, route):
        return (route == "lawyer" and result.get("error_code") == "model_identity_mismatch"
                and (str(ledger.path.resolve()), result.get("record_sha256")) in approved)

    def plan(ledger, logical, digest, route):
        if type(logical) is not int or logical < 1 or route not in {"lawyer", "environment"}:
            fail("invalid_attempt_reservation", "The reservation identity or route is invalid.")
        prior = ledger.prior(logical, digest)  # Binds every old hash before dispatch.
        if any(row["route"] != route for row in prior):
            fail("attempt_route_changed", "The logical request changed its saved role route.")
        if ledger.reserves and logical < ledger.reserves[-1]["logical_decision"]:
            fail("attempt_logical_order_changed", "Reservations cannot return to an earlier logical decision.")
        finishes, counts = _history(ledger)
        review_errors = _REVIEW_ERRORS | set(reliability.REVIEW_REQUIRED_ERRORS)
        for row in ledger.reserves:
            if "transport_recovery_protocol" in row:
                if (row["transport_recovery_protocol"] != policy["protocol"]
                        or row.get("transport_recovery_policy_sha256") != policy_hash):
                    fail("transport_recovery_policy_changed", "Recovery history belongs to another effective retry policy.")
                saved_manifest = row.get("transport_recovery_manifest_sha256")
                if saved_manifest != manifest_digest:
                    fail("transport_recovery_manifest_changed", "Recovery history is bound to a different manifest.")
        for row in prior:
            result = finishes.get(row["invocation_id"])
            if result and result["status"] == "passed":
                fail("accepted_decision_not_replayed", "An accepted response must be replayed from its journal; no duplicate dispatch occurred.")
            if (result and result.get("error_code") in review_errors
                    and not is_approved(ledger, result, route)):
                fail("prior_attempt_requires_review", "A safety, identity, or protocol failure remains blocked.")

        original_limit = min(2, ledger.policy[
            "lawyer_max_attempts_per_decision" if route == "lawyer"
            else "environment_max_attempts_per_decision"])
        unknown = any(not finishes.get(row["invocation_id"])
                      or finishes[row["invocation_id"]]["status"] == "interrupted_outcome_unknown"
                      for row in prior)
        if unknown and len(prior) >= original_limit:
            fail("unknown_outcome_requires_review", "Unknown outcomes do not unlock transport recovery beyond the original attempt allowance.")
        nontransport = sum(not _transport_finish(finishes.get(row["invocation_id"])) for row in prior)
        if nontransport >= original_limit:
            fail("decision_nontransport_budget_exhausted", "The original nontransport decision allowance is exhausted.")
        if len(prior) >= policy["max_attempts_per_logical_decision"]:
            fail("decision_attempt_budget_exhausted", "The durable eight-reservation logical request cap is exhausted.")

        last = finishes.get(prior[-1]["invocation_id"]) if prior else None
        recovery = bool(prior and _transport_finish(last))
        if prior and last and last["status"] == "failed" and not recovery:
            code = last.get("error_code")
            if is_approved(ledger, last, route):
                if len(prior) >= original_limit:
                    fail("decision_nontransport_budget_exhausted", "Historical identity review only permits the remaining original attempt.")
            elif not (route == "lawyer" and code in reliability.FORMAT_ERRORS):
                fail("prior_attempt_not_retryable", "The last failed request was not an explicitly retryable transport or allowed lawyer format failure.")
        case_nontransport_limit = min(policy["max_case_nontransport_reservations"],
                                      ledger.policy["max_case_model_invocations"])
        if counts["nontransport_reservations"] >= case_nontransport_limit:
            fail("case_nontransport_budget_exhausted", "The persistent case nontransport allowance is exhausted.")
        if counts["total_reservations"] >= policy["max_case_total_reservations"]:
            fail("case_invocation_budget_exhausted", "The persistent 360-reservation case cap is exhausted.")
        if recovery and counts["transport_recovery_reservations"] >= policy["max_case_transport_recovery_reservations"]:
            fail("case_transport_recovery_budget_exhausted", "The persistent 120 transport recovery reservation cap is exhausted.")
        return prior, finishes, recovery

    def reserve(self, logical, request_hash, route):
        prior, finishes, recovery = plan(self, logical, request_hash, route)
        for row in prior:
            if row["invocation_id"] not in finishes:
                self.append({"kind": "finish", "invocation_id": row["invocation_id"],
                             "status": "interrupted_outcome_unknown", "error_code": "interrupted_outcome_unknown", "calls": []})
        invocation, attempt = len(self.reserves) + 1, len(prior) + 1
        self.append({"kind": "reserve", "invocation_id": invocation, "logical_decision": logical,
                     "request_hash": request_hash, "route": route, "attempt_for_decision": attempt,
                     "transport_recovery_protocol": policy["protocol"],
                     "transport_recovery_policy_sha256": policy_hash,
                     "transport_recovery_manifest_sha256": manifest_digest,
                     "transport_reservation_category": "transport_recovery" if recovery else "original_allowance"})
        return invocation, attempt

    def lift(inner):
        for obj in (inner, getattr(inner, "lawyer_backend", None), getattr(inner, "environment_backend", None)):
            if obj is not None and hasattr(obj, "max_model_calls") and id(obj) not in ceilings:
                ceilings[id(obj)] = (obj, obj.max_model_calls)
                obj.max_model_calls = policy["max_case_total_reservations"]

    def decide(self, role_id, system, context, schema):
        request = deepcopy({"role_id": role_id, "system": system, "context": context, "schema": schema})
        logical, digest = self._logical_request(request)
        route = "lawyer" if role_id == "R_DEFENSE" else "environment"
        lift(self.inner)
        previous_error = None
        while True:
            if self._logical_request(request) != (logical, digest):
                fail("missing_or_changed_journal_request", "The bound logical request changed during recovery.")
            try:
                prior, finishes, recovery = plan(self.ledger, logical, digest, route)
            except BackendError:
                if previous_error is not None:
                    raise previous_error
                raise
            if recovery:
                transport_count = sum(_transport_finish(finishes.get(row["invocation_id"])) for row in prior)
                delay = policy["backoff_seconds"][min(transport_count - 1, 3)]
                print(f"[transport recovery] Decision {logical}: same request, wait {delay}s before next durable attempt.", flush=True)
                reliability.time.sleep(delay)
            invocation, attempt = self.ledger.reserve(logical, digest, route)
            before = len(self.inner.calls)
            print(f"[court] Decision {logical} | {route} | cumulative attempt {attempt}/8 starting", flush=True)
            try:
                # Isolate adapter-local mutation from the next identical
                # request and from the caller's court state.
                result = self.inner.decide(**deepcopy(request))
            except BaseException as exc:
                code = getattr(exc, "code", "interrupted" if isinstance(exc, KeyboardInterrupt) else "local_backend_error")
                self.ledger.finish(invocation, "failed", code, self.inner.calls[before:])
                print(f"[court] Decision {logical} cumulative attempt {attempt}/8 failed: {code}", flush=True)
                # Never replace the provider's final BackendError with a budget
                # error. A later resumed invocation still reports its gate.
                if not isinstance(exc, BackendError) or not (
                    code in _TRANSIENT or (route == "lawyer" and code in reliability.FORMAT_ERRORS)
                ):
                    raise
                previous_error = exc
                continue
            self.ledger.finish(invocation, "passed", None, self.inner.calls[before:])
            return result

    def safe_call(call):
        value = old_safe(call)
        # Only categorical diagnostics produced by audited adapter overlays;
        # arbitrary output, private provider errors, prompts and keys stay out.
        for field in ("transport_diagnostics", "recovery_diagnostics"):
            if isinstance(call.get(field), dict):
                value[field] = deepcopy(call[field])
        return value

    def stats(self):
        value = old_stats.fget(self)
        _, counts = _history(self.ledger)
        value.update({"comparison_policy_is_original": True,
                      "effective_retry_policy": deepcopy(policy),
                      "effective_retry_policy_sha256": policy_hash,
                      "transport_recovery_manifest_sha256": manifest_digest,
                      "transport_recovery_accounting": counts})
        return value

    setattr(reliability, _MARKER, True)
    ledger_cls.reserve, backend_cls.decide = reserve, decide
    backend_cls.stats, reliability.safe_call = property(stats), safe_call
    try:
        yield
    finally:
        ledger_cls.reserve, backend_cls.decide = old_reserve, old_decide
        backend_cls.stats, reliability.safe_call = old_stats, old_safe
        for obj, maximum in ceilings.values():
            obj.max_model_calls = maximum
        delattr(reliability, _MARKER)
