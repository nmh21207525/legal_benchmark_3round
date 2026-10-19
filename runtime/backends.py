"""Four isolated defense candidates, fixed environment/scorer, audited transports.

No provider-native tools are enabled. The workflow controller executes the
validated proposed actions. Failed final answers are private diagnostic data;
they are never repaired, accepted, or inserted into the courtroom history.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import uuid

ROOT = Path(__file__).resolve().parent
CANDIDATES = ("seed", "deepseek", "opus", "gpt")
DEFAULT_MODELS = {"seed": "doubao-seed-evolving", "deepseek": "deepseek-flash",
                  "opus": "claude-opus-5-5", "gpt": "gpt-5.6-sol"}
ENVIRONMENT_MODEL = "gpt-5.6-sol"
SCORER_MODEL = "gpt-6-sol"
PROTOCOL = "legal-three-rounds-workflow-v1"
JSON_WRAPPER_POLICY = {"protocol": "strict-single-json-fence-v1",
    "allowed": "one complete json/unlabelled Markdown fence, whitespace outside only",
    "inner": "strict JSON object; duplicate keys/nonfinite numbers/multiple objects rejected",
    "llm_repair": False, "applies_to": "all candidate and shared Codex final answers",
    "raw_normalized_answers_retained_privately": True}


def bootstrap():
    """Resolve only vendored modules, never an adjacent old experiment."""
    for relative in ("vendor/runtime", "vendor", "vendor/v2", "vendor/recovery"):
        path = str(ROOT / relative)
        if path not in sys.path:
            sys.path.insert(0, path)
    for name, expected in (("court_agent", ROOT / "vendor/runtime/court_agent/__init__.py"),
                           ("source_rubric_v2", ROOT / "vendor/source_rubric_v2/__init__.py"),
                           ("reliability", ROOT / "vendor/v2/reliability.py")):
        module = importlib.import_module(name)
        if Path(module.__file__).resolve() != expected:
            raise RuntimeError("unexpected_runtime_module:" + name)
    name = "legal_full_workflow_v3_vendor_backends"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "vendor/v2/backends.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def normalize_config(value, project_root, *, gpt_model=None):
    config = deepcopy(value)
    def inspect(obj):
        if isinstance(obj, dict):
            for key, item in obj.items():
                if str(key).lower() in {"api_key", "token", "password", "secret", "authorization"}:
                    raise ValueError("credentials_must_not_be_in_config")
                inspect(item)
        elif isinstance(obj, list):
            for item in obj:
                inspect(item)
    inspect(config)
    if config.get("backend", "codex_cli") != "codex_cli":
        raise ValueError("environment_and_scorer_require_codex_cli")
    for key, expected in (("environment_model", ENVIRONMENT_MODEL), ("scorer_model", SCORER_MODEL)):
        if config.get(key, expected) != expected:
            raise ValueError("fixed_model_changed:" + key)
        config[key] = expected
    models = dict(DEFAULT_MODELS)
    overrides = config.get("defense_models", {})
    if not isinstance(overrides, dict) or set(overrides) - set(CANDIDATES):
        raise ValueError("invalid_defense_models")
    models.update(overrides)
    if gpt_model is not None:
        models["gpt"] = gpt_model
    if any(models[k] != DEFAULT_MODELS[k] for k in ("seed", "deepseek", "opus")):
        raise ValueError("fixed_candidate_model_changed")
    if not isinstance(models["gpt"], str) or not re.fullmatch(r"gpt-[A-Za-z0-9_.-]{1,100}", models["gpt"]):
        raise ValueError("invalid_gpt_candidate_model")
    if config.get("reasoning_effort", "medium") != "medium":
        raise ValueError("reasoning_effort_must_be_medium")
    config.update(backend="codex_cli", defense_models=models, reasoning_effort="medium", max_attempts=1)
    # This shared configuration is not the candidate model selection.
    config["models"] = {"lawyer": SCORER_MODEL, "environment": ENVIRONMENT_MODEL}
    prior_input_limit = config.get("max_input_chars", 700000)
    if type(prior_input_limit) is not int or prior_input_limit <= 0:
        raise ValueError("invalid_max_input_chars")
    config["max_input_chars"] = max(700000, prior_input_limit)
    config.setdefault("max_output_tokens", 16384)
    config.setdefault("max_model_calls", 240)
    config.setdefault("timeout_seconds", 300)
    executable_text = str(config.setdefault("codex", {}).get("executable", ".tools/codex"))
    executable = Path(executable_text)
    if executable.is_absolute():
        executable_text = str(executable.resolve())
    elif "/" in executable_text or "\\" in executable_text:
        executable_text = str((Path(project_root) / executable).resolve())
    # Bare executable names intentionally use PATH in the audited CLI adapter.
    config["codex"]["executable"] = executable_text
    bootstrap()
    from court_agent.llm import api_settings
    api_settings(config)
    return config


def candidate_spec(name, config):
    vendor = bootstrap()
    from court_agent.llm import api_settings
    if name not in CANDIDATES:
        raise ValueError("unknown_candidate")
    transport = "codex_cli" if name == "gpt" else "claude_cli" if name == "opus" else "http"
    return {"model": config["defense_models"][name], "transport": transport,
            "endpoint": api_settings(config)["base_url"] if name == "gpt" else vendor.ENDPOINTS[name],
            "requested_reasoning_effort": "medium",
            "provider_reasoning_effort": "high" if name == "deepseek" else "medium",
            "output_token_cap_enforced": name in {"seed", "deepseek"},
            "configured_max_output_tokens": 16384,
            "native_tools_allowed": False, "fallback_allowed": False}


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def normalize_final_text(text):
    """Only remove a complete syntactic wrapper; never extract/repair JSON."""
    if not isinstance(text, str):
        return text, False
    match = re.fullmatch(r"\s*```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```\s*", text)
    if not match:
        return text, False
    bootstrap()
    from court_agent.llm import _json_object
    inner = match.group(1)
    _json_object(inner)  # Raises on invalid/non-object/duplicate/nonfinite output.
    return inner, True


class _FinalCapture:
    """Capture only final-answer text; never thinking blocks or raw API streams."""
    diagnostic_dir = None
    candidate_label = None

    def _invoke(self, *args, **kwargs):
        value = super()._invoke(*args, **kwargs)
        if isinstance(value, str):
            self._last_final = value
        normalized, removed = normalize_final_text(value)
        self._format_normalized = removed
        return normalized

    def _consume(self, event, *args, **kwargs):
        if isinstance(event, dict) and event.get("type") == "result" and isinstance(event.get("result"), str):
            self._last_final = event["result"]
        return super()._consume(event, *args, **kwargs)

    def _run_process(self, arguments, payload, directory, state):
        completed = False
        try:
            result = super()._run_process(arguments, payload, directory, state)
            completed = True
            return result
        finally:
            path = Path(directory) / "decision.json"
            if path.is_file() and not path.is_symlink() and path.stat().st_size <= 1_000_000:
                self._last_final = path.read_text(encoding="utf-8", errors="strict")
                if completed:
                    value, removed = normalize_final_text(self._last_final)
                    self._format_normalized = removed
                    if removed:
                        path.write_text(value, encoding="utf-8")

    def decide(self, role_id, system, context, schema):
        self._last_final = None
        self._format_normalized = False
        try:
            answer = super().decide(role_id, system, context, schema)
            if self._format_normalized and self.diagnostic_dir is not None:
                self._save_failure(role_id, system, context, schema, None)
            return answer
        except BaseException as exc:
            if self.diagnostic_dir is not None:
                self._save_failure(role_id, system, context, schema, exc)
            raise
        finally:
            if getattr(self, "calls", None):
                self.calls[-1]["final_json_wrapper_removed"] = self._format_normalized
                if isinstance(self._last_final, str):
                    self.calls[-1]["raw_final_sha256"] = hashlib.sha256(self._last_final.encode()).hexdigest()

    def _save_failure(self, role, system, context, schema, exc):
        directory = Path(self.diagnostic_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        request = {"role_id": role, "system": system, "context": context, "schema": schema}
        value = {"kind": "rejected_final_output" if exc else "accepted_normalized_final_output", "created_at": datetime.now(timezone.utc).isoformat(),
                 "role_id": role, "candidate": self.candidate_label,
                 "requested_model": getattr(self, "model", None) or
                    (self.lawyer_model if role == "R_DEFENSE" else self.environment_model),
                 "request_sha256": _hash(request), "error_code": getattr(exc, "code", type(exc).__name__) if exc else None,
                 "final_text_available": isinstance(self._last_final, str),
                 "final_text": self._last_final, "accepted": exc is None,
                 "json_wrapper_removed": self._format_normalized,
                 "no_thinking_or_provider_stream_retained": True}
        path = directory / ("failure-" + uuid.uuid4().hex + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())


def _captured(base, *args, diagnostic_dir=None, candidate=None, **kwargs):
    instance = type("Captured" + base.__name__, (_FinalCapture, base), {})(*args, **kwargs)
    instance.diagnostic_dir, instance.candidate_label = diagnostic_dir, candidate
    return instance


def shared_backend(config, keys, *, model, diagnostic_dir=None):
    bootstrap()
    from court_agent.codex_backend import CodexCLIBackend
    cfg = deepcopy(config)
    cfg["models"] = {"lawyer": model, "environment": model}
    cfg["max_attempts"] = 1
    return _captured(CodexCLIBackend, cfg, api_key=keys.get("gpt"),
                     diagnostic_dir=diagnostic_dir, candidate=None)


def create_backend(config, keys, candidate, project_root=None, claude_executable="claude", *, attempt_ledger=None, diagnostic_dir=None):
    vendor = bootstrap()
    from court_agent.codex_backend import CodexCLIBackend
    from reliability import ReliableBackend
    if candidate not in CANDIDATES:
        raise ValueError("unknown_candidate")
    cfg = deepcopy(config)
    cfg.update(max_attempts=1, max_output_tokens=16384, timeout_seconds=300, max_model_calls=240)
    cfg["models"] = {"lawyer": cfg["defense_models"][candidate], "environment": ENVIRONMENT_MODEL}
    diagnostics = diagnostic_dir or (Path(attempt_ledger).parent / "private_failed_outputs" if attempt_ledger else None)
    environment = shared_backend(cfg, keys, model=ENVIRONMENT_MODEL, diagnostic_dir=diagnostics)
    if candidate == "gpt":
        lawyer = _captured(CodexCLIBackend, cfg, api_key=keys.get("gpt"),
                           diagnostic_dir=diagnostics, candidate=candidate)
        lawyer.model = cfg["defense_models"][candidate]  # Common audited-router diagnostic interface.
    elif candidate == "opus":
        lawyer = _captured(vendor.ClaudeCLIBackend, cfg, api_key=keys.get("opus"),
                           executable=claude_executable, diagnostic_dir=diagnostics, candidate=candidate)
    else:
        lawyer = _captured(vendor.HTTPBackend, cfg, lawyer=candidate, api_key=keys.get(candidate),
                           diagnostic_dir=diagnostics, candidate=candidate)
    routed = vendor.RoutedBackend(lawyer, environment, 240)
    if attempt_ledger is not None:
        return ReliableBackend(routed, Path(attempt_ledger), deepcopy(vendor.COMPARISON_POLICY))
    return routed


@contextmanager
def runtime_overlay(manifest_digest, keys, diagnostic_dir, *, court_retries=True):
    vendor = bootstrap()
    import reliability
    import court_agent.codex_backend as codex_backend
    import recovery_transport
    from opus_protocol import install_opus_protocol
    from transport_retry import transport_retry_overlay
    from gpt_diagnostics import install_gpt_diagnostics
    directory = Path(diagnostic_dir)
    def append(value):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(directory / "transport_diagnostics.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    with ExitStack() as stack:
        stack.enter_context(install_gpt_diagnostics(codex_backend, sink=append, secrets=tuple(keys.values())))
        stack.enter_context(install_opus_protocol(vendor, recovery_transport_module=recovery_transport))
        if court_retries:
            stack.enter_context(transport_retry_overlay(reliability, manifest_digest, approvals=()))
            # The old overlay allowed one unknown outcome within its original
            # allowance. New v3 has no historical migration need: fail closed.
            original_decide = reliability.ReliableBackend.decide
            def guarded_decide(self, role_id, system, context, schema):
                request = {"role_id": role_id, "system": system, "context": context, "schema": schema}
                logical, request_hash = self._logical_request(request)
                finishes = {r["invocation_id"]: r for r in self.ledger.rows if r["kind"] == "finish"}
                for reservation in self.ledger.prior(logical, request_hash):
                    finish = finishes.get(reservation["invocation_id"])
                    if finish is None or finish.get("status") == "interrupted_outcome_unknown":
                        raise reliability.BackendError("unknown_outcome_requires_review",
                            "A previous dispatch has no known outcome; no automatic resend was made.")
                return original_decide(self, role_id, system, context, schema)
            reliability.ReliableBackend.decide = guarded_decide
            stack.callback(setattr, reliability.ReliableBackend, "decide", original_decide)
        yield


def policies():
    vendor = bootstrap()
    from transport_retry import RETRY_POLICY
    from scoring_retry import SCORING_RETRY_POLICY
    return {"original_attempt_accounting": deepcopy(vendor.COMPARISON_POLICY),
            "effective_court_retry_policy": deepcopy(RETRY_POLICY),
            "scoring_retry_policy": deepcopy(SCORING_RETRY_POLICY),
            "json_wrapper_policy": deepcopy(JSON_WRAPPER_POLICY),
            "no_historical_retry_approvals": True,
            "court_unknown_outcome_retry": False,
            "failed_final_output_storage": "private_complete_final_text_when_available_no_thinking"}
