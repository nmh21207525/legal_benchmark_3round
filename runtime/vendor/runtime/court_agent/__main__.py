from __future__ import annotations

import argparse
from copy import deepcopy
import getpass
import json
import os
from pathlib import Path
import sys
import warnings

from .data import CaseData
from .engine import CourtEngine
from .llm import BackendError, OpenAIBackend, api_settings
from .mock import MockBackend
from .replay import write_replay


CHECK_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["ok"]}},
    "required": ["status"],
    "additionalProperties": False,
}


def make_backend(config, api_key=None):
    backend = config.get("backend", "responses")
    if backend == "responses":
        return OpenAIBackend(config, api_key=api_key)
    if backend == "codex_cli":
        from .codex_backend import CodexCLIBackend
        return CodexCLIBackend(config, api_key=api_key)
    raise BackendError("invalid_config", "backend must be responses or codex_cli.")


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def read_config(path):
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise BackendError("invalid_config", "Could not read the configuration as UTF-8 JSON; check --config.") from None
    api_settings(config)
    return config


def prompt_api_key(config, enabled):
    settings = api_settings(config)
    if not enabled or os.environ.get(settings["key_env"], "").strip():
        return None
    # Do not use getpass's echoing fallback (e.g. VS Code Debug Console).
    if not sys.stdin.isatty():
        raise BackendError("prompt_unavailable", "--prompt-key requires an interactive terminal; use the VS Code integrated terminal.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass.getpass(f"{settings['provider']} API key (hidden, this process only): ").strip()
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        raise BackendError("prompt_cancelled", "The secure API key prompt was cancelled or unavailable.") from None
    if not value:
        raise BackendError("missing_api_key", "No API key was entered.")
    return value


def check_api(config, api_key=None):
    """One tiny structured Responses request per unique configured model.

    No case data, verdicts or hidden material are sent. A pass demonstrates a
    small request's access and schema compatibility, not complete court support.
    """
    check_config = deepcopy(config)
    check_config.update({"max_attempts": 1, "max_model_calls": 2,
                         "max_output_tokens": 1000, "max_input_chars": 4000,
                         "timeout_seconds": 45})
    if check_config.get("backend") == "codex_cli":
        check_config.setdefault("codex", {})["diagnostics"] = True
    backend = make_backend(check_config, api_key=api_key)
    results = []
    seen = set()
    try:
        backend.check_credentials()
        for role_id, model in (("R_DEFENSE", backend.lawyer_model),
                               ("R_CHECK_ENVIRONMENT", backend.environment_model)):
            if model in seen:
                continue
            seen.add(model)
            try:
                backend.decide(role_id, 'Return the JSON object {"status":"ok"}. This is a connectivity and JSON-schema check.',
                               {"task": "API compatibility check; no case data"}, CHECK_SCHEMA)
                results.append({"model": model, "status": "passed"})
            except BackendError as exc:
                results.append({"model": model, "status": "failed", "error_code": exc.code, "message": str(exc)})
        report = {
            "command": "check-api", "backend": config.get("backend", "responses"), "provider": backend.api["provider"],
            "base_url": backend.api["base_url"], "models": results,
            "all_passed": all(item["status"] == "passed" for item in results),
            "model_stats": backend.stats,
            "limits": ["Small structured output check only; full courtroom compatibility is not verified.",
                       "No case data sent; one decision per unique configured model; no application retry or model fallback."]
        }
        return report
    finally:
        backend.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Source-bound courtroom development runner")
    parser.add_argument("command", choices=["validate", "mock", "run", "check-api"])
    parser.add_argument("--case", type=Path)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.json")
    parser.add_argument("--lawyer-model", choices=["gpt-6-sol", "gpt-6-astra"])
    parser.add_argument("--out", type=Path)
    parser.add_argument("--prompt-key", action="store_true", help="Securely prompt in the terminal if the selected provider's key is unset; do not save it.")
    args = parser.parse_args(argv)
    if args.command != "check-api" and args.case is None:
        parser.error("validate/mock/run require --case")
    if args.prompt_key and args.command not in {"run", "check-api"}:
        parser.error("--prompt-key is only available for run/check-api")
    try:
        if args.command == "check-api":
            config = read_config(args.config)
            if args.lawyer_model:
                if not isinstance(config.get("models", {}), dict):
                    raise BackendError("invalid_config", "models must be a JSON object.")
                config.setdefault("models", {})["lawyer"] = args.lawyer_model
            # Validate all ordinary settings before prompting for a credential.
            make_backend(config)
            result = check_api(config, prompt_api_key(config, args.prompt_key))
            print_json(result)
            return 0 if result["all_passed"] else 2

        case = CaseData(args.case)
        errors = case.validate()
        if errors or args.command == "validate":
            print_json({"case_id": case.case_id, "runtime_structure_ok": not errors,
                        "errors": errors, "formal_legal_validation": False})
            return 1 if errors else 0
        if args.out is None:
            parser.error("mock/run require --out pointing to a new output directory")
        if args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir())):
            parser.error("--out must be new or empty; previous run results are never overwritten")
        config = read_config(args.config)
        if args.lawyer_model:
            if not isinstance(config.get("models", {}), dict):
                raise BackendError("invalid_config", "models must be a JSON object.")
            config.setdefault("models", {})["lawyer"] = args.lawyer_model
        config["mode"] = "mock" if args.command == "mock" else "live"
        if args.command == "mock":
            backend = MockBackend()
        else:
            make_backend(config)
            backend = make_backend(config, api_key=prompt_api_key(config, args.prompt_key))
            backend.check_credentials()
        try:
            engine = CourtEngine(case, backend, config)
            summary = engine.run()
            engine.save(args.out)
            write_replay(args.out, engine)
        finally:
            if hasattr(backend, "close"):
                backend.close()
        print_json(summary)
        return 0 if summary["status"] == "completed_development_run" else 2
    except BackendError as exc:
        print_json({"status": "failed", "error_code": exc.code, "message": str(exc)})
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        print_json({"status": "failed", "error_code": "input_or_output_error",
                    "message": "Could not read valid case/config data or write the output; check the paths, JSON files and directory permissions."})
        return 2


if __name__ == "__main__":
    sys.exit(main())
