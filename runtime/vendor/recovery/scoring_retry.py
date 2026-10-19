"""Append-only, explicit transport retry extension for fixed rubric scoring.

The original scorer creates one immutable attempt directory and one fresh
backend per dispatch.  This overlay leaves its code/config/manifest untouched.
Eight is the TOTAL per-batch dispatch limit, including pre-overlay attempts.
Only an explicit transport error permits another dispatch; an unfinished
reservation is an unknown outcome and is never silently retried.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

VERSION = "fixed-scoring-transport-extension-1"
MAX_ATTEMPTS = 8
DELAYS = (10, 20, 40, 60)
TRANSIENT_ERRORS = frozenset({"api_timeout", "api_connection", "api_transient", "rate_limited"})
SCORING_RETRY_POLICY = {
    "version": VERSION,
    "total_attempt_limit_including_old": MAX_ATTEMPTS,
    "delays_seconds": list(DELAYS),
    "retryable": sorted(TRANSIENT_ERRORS),
    "unknown_outcomes_retried": False,
}


class ScoringRetryError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _fail(code):
    raise ScoringRetryError(code, "Scoring transport recovery stopped; existing attempts were not reset or changed.")


def _strict_json(text):
    def pairs(items):
        value = {}
        for key, child in items:
            if key in value:
                _fail("scoring_retry_ledger_invalid_json")
            value[key] = child
        return value

    return json.loads(text, object_pairs_hook=pairs,
                      parse_constant=lambda _: _fail("scoring_retry_ledger_invalid_json"))


class _Ledger:
    def __init__(self, path, manifest_digest):
        self.path = Path(path)
        if self.path.is_symlink():
            _fail("unsafe_scoring_retry_ledger")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        if lock_path.is_symlink():
            _fail("unsafe_scoring_retry_lock")
        self.lock = lock_path.open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            _fail("scoring_retry_already_running")
        self.rows = []
        self.previous = "0" * 64
        policy = {**deepcopy(SCORING_RETRY_POLICY), "manifest_digest": manifest_digest}
        try:
            if self.path.exists():
                raw = self.path.read_bytes()
                if not raw or not raw.endswith(b"\n"):
                    _fail("scoring_retry_ledger_incomplete")
                for line in raw.decode("utf-8").splitlines():
                    row = _strict_json(line)
                    if not isinstance(row, dict):
                        _fail("scoring_retry_ledger_invalid_json")
                    payload = {k: v for k, v in row.items() if k != "record_sha256"}
                    if (payload.get("previous_sha256") != self.previous or
                            row.get("record_sha256") != _hash(payload)):
                        _fail("scoring_retry_ledger_changed")
                    self.rows.append(row)
                    self.previous = row["record_sha256"]
                if self.rows[0].get("kind") != "policy" or self.rows[0].get("policy") != policy:
                    _fail("scoring_retry_policy_changed")
            else:
                self.append({"kind": "policy", "policy": policy})
        except BaseException:
            self.close()
            raise

    def append(self, payload):
        row = {**deepcopy(payload), "previous_sha256": self.previous}
        row["record_sha256"] = _hash(row)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(_canonical(row) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.rows.append(row)
        self.previous = row["record_sha256"]

    def for_batch(self, identity):
        return [row for row in self.rows if row.get("batch_id") == identity]

    def close(self):
        self.lock.close()


def _attempts(scoring, folder, batch):
    root = folder / "attempts"
    if root.is_symlink():
        _fail("unsafe_scoring_attempts")
    attempts = sorted(root.glob("attempt-*")) if root.exists() else []
    for number, attempt in enumerate(attempts, 1):
        if (attempt.is_symlink() or not attempt.is_dir() or
                attempt.name != f"attempt-{number:03d}"):
            _fail("scoring_attempt_sequence_changed")
        request_path = attempt / "request.json"
        if not request_path.is_file() or request_path.is_symlink():
            _fail("scoring_attempt_outcome_unknown")
        if scoring.read(request_path) != batch["request"]:
            _fail("scoring_retry_request_changed")
        binding_path = attempt / "request_binding.json"
        if not binding_path.is_file() or binding_path.is_symlink():
            _fail("scoring_attempt_outcome_unknown")
        if scoring.read(binding_path).get("request_sha256") != batch["hash"]:
            _fail("scoring_retry_request_changed")
    return attempts


def _snapshots(attempts):
    snapshots = {}
    for attempt in attempts:
        files = {}
        for path in sorted(attempt.iterdir()):
            if path.is_symlink() or not path.is_file():
                _fail("unsafe_scoring_attempt_file")
            files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        snapshots[attempt.name] = files
    return snapshots


def _verify_saved(rows, snapshots):
    for row in rows:
        saved = row.get("snapshots", {})
        for name, files in saved.items():
            current = snapshots.get(name)
            if current is None or any(current.get(k) != v for k, v in files.items()):
                _fail("scoring_previous_output_changed")
    reservations = {row["attempt_number"]: row for row in rows if row["kind"] == "reserve"}
    finished = {row["attempt_number"]: row for row in rows if row["kind"] == "finish"}
    return [number for number in reservations if number not in finished]


def _previous_error(scoring, attempt):
    if (attempt / "validation_issues.json").exists():
        _fail("scoring_validation_requires_review")
    error_file = attempt / "request_error.json"
    if not error_file.exists():
        _fail("scoring_attempt_outcome_unknown")
    code = scoring.read(error_file).get("error_code")
    if code not in TRANSIENT_ERRORS:
        _fail("scoring_failure_not_transport_retryable")
    return code


@contextmanager
def scoring_retry_overlay(scoring, sidecar_path, manifest_digest="unspecified"):
    """Temporarily wrap score_fixed.run_batch; sidecar_path is one JSONL FILE.

    The caller must also install GPT diagnostic classification around this
    context.  Unknown ``codex_request_failed`` is deliberately not retryable.
    The manifest digest binds this additive policy to the caller's immutable
    extension manifest; no scoring configuration is edited.
    """
    ledger = _Ledger(sidecar_path, manifest_digest)
    original = scoring.run_batch

    def run_batch(folder, cid, batch, data, config, key, factory):
        folder = Path(folder).resolve()
        if scoring.obj_hash(batch["request"]) != batch["hash"]:
            _fail("scoring_retry_request_changed")
        identity = _hash({"folder": str(folder), "case_id": cid, "request_sha256": batch["hash"]})
        expected_config = _hash(config)
        rows = ledger.for_batch(identity)
        if any(row.get("config_sha256", expected_config) != expected_config for row in rows):
            _fail("scoring_retry_config_changed")
        attempts = _attempts(scoring, folder, batch)
        pending = _verify_saved(rows, _snapshots(attempts))
        # This also accepts an already-written VALID raw response after a crash,
        # using the original validation and immutable accepted marker logic.
        cached = scoring.cached_batch(folder, cid, batch, data)
        if cached is not None:
            ledger.append({"kind": "cache_reused", "batch_id": identity,
                           "config_sha256": expected_config, "snapshots": _snapshots(attempts)})
            return cached, []
        if pending:
            _fail("scoring_reserved_outcome_unknown")
        if not rows:
            ledger.append({"kind": "observed", "batch_id": identity,
                           "request_sha256": batch["hash"], "config_sha256": expected_config,
                           "previous_attempt_count": len(attempts), "snapshots": _snapshots(attempts)})

        while True:
            attempts = _attempts(scoring, folder, batch)
            _verify_saved(ledger.for_batch(identity), _snapshots(attempts))
            if len(attempts) >= MAX_ATTEMPTS:
                _fail("scoring_transport_budget_exhausted")
            if attempts:
                _previous_error(scoring, attempts[-1])
                delay = DELAYS[min(len(attempts) - 1, len(DELAYS) - 1)]
                print(f"[评分连接恢复] {cid}；下一次 {len(attempts)+1}/{MAX_ATTEMPTS}；等待 {delay}秒。", flush=True)
                time.sleep(delay)
            number = len(attempts) + 1
            ledger.append({"kind": "reserve", "batch_id": identity, "attempt_number": number,
                           "request_sha256": batch["hash"], "config_sha256": expected_config,
                           "snapshots": _snapshots(attempts)})
            try:
                result = original(folder, cid, batch, data, config, key, factory)
            except BaseException as exc:
                code = getattr(exc, "code", "interrupted" if isinstance(exc, KeyboardInterrupt) else type(exc).__name__)
                after = _attempts(scoring, folder, batch)
                if len(after) != number:
                    # Keep the durable reservation unresolved; never infer that
                    # no API request happened merely because files are absent.
                    raise
                ledger.append({"kind": "finish", "batch_id": identity, "attempt_number": number,
                               "status": "transport_failed" if code in TRANSIENT_ERRORS else "failed",
                               "error_code": code, "snapshots": _snapshots(after)})
                if code not in TRANSIENT_ERRORS:
                    raise
                continue
            after = _attempts(scoring, folder, batch)
            if len(after) != number:
                _fail("scoring_attempt_dispatch_count_changed")
            ledger.append({"kind": "finish", "batch_id": identity, "attempt_number": number,
                           "status": "accepted" if not result[1] else "validation_failed",
                           "snapshots": _snapshots(after)})
            return result

    scoring.run_batch = run_batch
    try:
        yield
    finally:
        scoring.run_batch = original
        ledger.close()
