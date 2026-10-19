#!/usr/bin/env python3
"""Frozen, serial four-candidate/two-case workflow experiment and safe resume."""
from __future__ import annotations

import argparse
from copy import deepcopy
import csv
from datetime import datetime, timezone
import fcntl
import getpass
import hashlib
import html
import json
import os
from pathlib import Path
import sys
import time
import warnings

import backends

ROOT = Path(__file__).resolve().parent
DEFAULT_PROJECT = Path("/home/nmh/legal_agent_codex_030/legal_court_agent")
CASE_IDS = ("CA-PILOT-003", "CA-PILOT-004")
PREFLIGHT_POLICY = {"max_attempts_per_route": 3, "retry_only": ["api_connection", "api_timeout", "api_transient", "rate_limited"],
                    "delays_seconds": [10, 20], "unknown_outcome_retry": False,
                    "schema": "real_action_schema_short_text_v2", "cached_success_reused": True}


class RunError(RuntimeError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def value_hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise RunError("duplicate_json_key")
            result[key] = value
        return result
    def constant(_):
        raise RunError("invalid_json_number")
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=pairs, parse_constant=constant)


def atomic_json(path, value, *, immutable=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise RunError("output_symlink")
    if immutable and path.exists():
        if read(path) != value:
            raise RunError("immutable_record_changed", path.name)
        return
    temp = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp.exists():
            temp.unlink()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT)
    result.add_argument("--config", type=Path)
    result.add_argument("--source-package", type=Path, default=ROOT / "data")
    result.add_argument("--out", type=Path)
    result.add_argument("--claude-executable", default="/home/nmh/.local/claude-court/node_modules/.bin/claude")
    result.add_argument("--gpt-model", default=None, help="GPT defense candidate; default frozen gpt-5.6-sol")
    result.add_argument("--case-id", action="append", choices=CASE_IDS, help="Repeat to select cases; default both. Changed selection needs a new output directory.")
    result.add_argument("--validate-only", action="store_true")
    result.add_argument("--preflight-only", action="store_true")
    result.add_argument("--prompt-keys", action="store_true")
    result.add_argument("--resume", action="store_true", help="Compatible runs resume automatically; retained for explicit invocation.")
    result.add_argument("--mock", "--offline-demo", action="store_true", help="Zero-network deterministic workflow demonstration; no model scores.")
    result.add_argument("--stop-after-cases", type=int, default=None, help="Stop after this many processed cases at a safe case boundary.")
    return result


def resolve_args(argv=None):
    args = parser().parse_args(argv)
    args.project_root = args.project_root.expanduser().resolve()
    args.config = (args.config or args.project_root / "config.codex.json").expanduser().resolve()
    args.source_package = args.source_package.expanduser().resolve()
    raw_out = args.out or args.project_root / "runs" / ("four-lawyers-full-v3-mock" if args.mock else "four-lawyers-full-v3-pf2")
    if raw_out.is_symlink():
        raise RunError("output_symlink")
    args.out = raw_out.expanduser().resolve()
    args.case_ids = tuple(cid for cid in CASE_IDS if args.case_id is None or cid in args.case_id)
    if args.stop_after_cases is not None and args.stop_after_cases < 1:
        raise RunError("invalid_stop_after_cases")
    return args


def load_config(project_root, path, *, gpt_model=None):
    if not Path(path).is_file():
        raise RunError("config_missing", str(path))
    return backends.normalize_config(read(path), project_root, gpt_model=gpt_model)


def collect_keys(config, enabled=False):
    backends.bootstrap()
    from court_agent.llm import api_settings
    names = {"seed": "ARK_API_KEY", "deepseek": "PACKY_DEEPSEEK_API_KEY",
             "opus": "PACKY_OPUS_API_KEY", "gpt": api_settings(config)["key_env"]}
    keys = {}
    for name, variable in names.items():
        value = os.environ.get(variable, "").strip()
        if not value and enabled:
            if not sys.stdin.isatty():
                raise RunError("prompt_requires_terminal")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass(name + " API key（隐藏输入，不保存）: ").strip()
        if not value:
            raise RunError("missing_credential_environment", variable)
        keys[name] = value
    return keys


def prepare(source_package):
    backends.bootstrap()
    from assets import load_assets
    from source_rubric_v2.environment import SourceBoundCaseData
    from workflow import validate_workflow
    from scoring import validate_rubric
    if not Path(source_package).is_dir():
        raise RunError("case_assets_missing")
    assets = load_assets(source_package)
    if set(assets) != set(CASE_IDS):
        raise RunError("expected_two_cases")
    cases = {}
    for cid, asset in assets.items():
        case = SourceBoundCaseData(asset["case_path"])
        errors = case.validate()
        if errors:
            raise RunError("invalid_case_assets", cid)
        validate_workflow(asset["workflow_root"], asset["workflow_spec"], case=case)
        validate_rubric(asset)
        cases[cid] = case
    return assets, cases


def build_manifest(args, config, assets):
    code = {}
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or (path.suffix not in {".py", ".sh"} and path.name != "requirements.txt"):
            continue
        if "tests" in path.relative_to(ROOT).parts or path.is_relative_to(args.out):
            continue
        if path.is_symlink():
            raise RunError("code_symlink")
        code[path.relative_to(ROOT).as_posix()] = digest(path)
    data = {}
    for path in sorted(args.source_package.rglob("*")):
        if path.is_symlink():
            raise RunError("asset_symlink")
        if path.is_file():
            data[path.relative_to(args.source_package).as_posix()] = digest(path)
    return {"protocol": backends.PROTOCOL, "mock_only": args.mock, "config": config,
            "code_sha256": code, "asset_sha256": data, "case_ids": list(args.case_ids),
            "defense_candidates": {name: backends.candidate_spec(name, config) for name in backends.CANDIDATES},
            "environment_model": backends.ENVIRONMENT_MODEL, "scorer_model": backends.SCORER_MODEL,
            "claude_executable": args.claude_executable, "workers": 1,
            "policies": backends.policies(), "preflight_policy": PREFLIGHT_POLICY,
            "rubric_counts": {cid: len(asset["rubric"]["points"]) for cid, asset in assets.items()},
            "rubric_sent_to_court": False, "old_runs_imported": False,
            "response_journal": "hash_chain_and_atomic_head_checkpoint",
            "native_model_tools_allowed": False, "real_model_availability_verified": False}


def bind_output(out, manifest):
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = out / "matrix_manifest.json"
    if not target.exists() and any((out / name).exists() for name in ("courts", "preflight", "matrix_status.json")):
        raise RunError("used_output_missing_manifest")
    atomic_json(target, manifest, immutable=True)
    return digest(target)


def preflight_request():
    from court_agent.schema import action_schema
    # action_schema reuses module-level STRING / STRINGS members.  Preflight's
    # short text and empty references constraints must never mutate courtroom
    # schemas (including witness fact_ids) for the subsequent live run.
    schema = deepcopy(action_schema(["prepare_examination"]))
    schema["properties"]["text"].update(minLength=16, maxLength=500)
    schema["properties"]["references"]["maxItems"] = 0
    schema["properties"]["details"]["maxItems"] = 0
    return {"system": "接口兼容性测试，不是真实案件。返回一个完整严格JSON动作对象，action为prepare_examination。text用一到两句普通中文说明整理资料的准备步骤，约30至100字，不要换行或重复；不要法律分析。references和details为空数组，其余不适用字段填null。禁止调用任何工具，不输出思维过程。",
            "context": {"task": "short_action_schema_compatibility_check_no_case_data",
                        "allowed_actions": ["prepare_examination"], "case_data_sent": False}, "schema": schema}


class _PreflightAttempts:
    """Three durable transport-only attempts; cached success handled by journal."""
    def __init__(self, inner, path):
        from reliability import AttemptLedger
        self.inner = inner
        self.ledger = AttemptLedger(path, {"lawyer_max_attempts_per_decision": 3,
            "environment_max_attempts_per_decision": 3, "max_case_model_invocations": 3,
            "protocol": "v3-preflight-transport-only-3"})

    @property
    def calls(self):
        return self.inner.calls

    @property
    def stats(self):
        return self.inner.stats

    def close(self):
        self.inner.close()

    def decide(self, role_id, system, context, schema):
        from court_agent.llm import BackendError
        req = {"role_id": role_id, "system": system, "context": context, "schema": schema}
        request_hash = value_hash(req)
        while True:
            prior = self.ledger.prior(1, request_hash)
            finishes = {r["invocation_id"]: r for r in self.ledger.rows if r["kind"] == "finish"}
            if prior:
                latest = finishes.get(prior[-1]["invocation_id"])
                if not latest:
                    raise RunError("preflight_unknown_outcome_requires_review")
                if latest["status"] == "passed":
                    raise RunError("preflight_passed_answer_missing_from_journal")
                if latest.get("error_code") not in PREFLIGHT_POLICY["retry_only"]:
                    raise RunError("preflight_prior_failure_not_retryable")
                if len(prior) >= 3:
                    raise RunError("preflight_attempt_budget_exhausted")
                time.sleep(PREFLIGHT_POLICY["delays_seconds"][min(len(prior)-1, 1)])
            invocation, _ = self.ledger.reserve(1, request_hash, "environment")
            before = len(self.inner.calls)
            try:
                self.inner.check_credentials()
                answer = self.inner.decide(**deepcopy(req))
            except BaseException as exc:
                code = getattr(exc, "code", "interrupted" if isinstance(exc, KeyboardInterrupt) else type(exc).__name__)
                self.ledger.finish(invocation, "failed", code, self.inner.calls[before:])
                if not isinstance(exc, BackendError) or code not in PREFLIGHT_POLICY["retry_only"]:
                    raise
                if len(self.ledger.reserves) >= 3:
                    raise
            else:
                self.ledger.finish(invocation, "passed", None, self.inner.calls[before:])
                return answer


def preflight(args, config, keys, manifest_digest, *, factory=None):
    from journal import HashChainedDecisionJournal
    reports = []
    directory = args.out / "preflight"
    marker_path = directory / "complete.json"
    # Inspect every executable/credential before creating any paid reservation.
    # A user can install a missing CLI and retry the unchanged run safely.
    route_backends = {}
    def build(name):
        tiny = deepcopy(config)
        tiny.update(max_model_calls=3, max_attempts=1, max_input_chars=12000,
                    max_output_tokens=4000, timeout_seconds=180)
        if factory:
            return factory(name, tiny)
        if name in backends.CANDIDATES:
            return backends.create_backend(tiny, keys, name, args.project_root, args.claude_executable,
                diagnostic_dir=directory / name / "private_failed_outputs")
        model = backends.ENVIRONMENT_MODEL if name == "environment" else backends.SCORER_MODEL
        return backends.shared_backend(tiny, keys, model=model,
                diagnostic_dir=directory / name / "private_failed_outputs")
    try:
        if factory is None:
            import importlib.util
            if importlib.util.find_spec("httpx") is None:
                raise RunError("httpx_dependency_missing")
        for name in (*backends.CANDIDATES, "environment", "scorer"):
            inner = build(name)
            route_backends[name] = inner
            inner.check_credentials()
    except BaseException:
        for inner in route_backends.values():
            inner.close()
        raise
    if marker_path.exists():
        marker = read(marker_path)
        if marker.get("manifest_sha256") != manifest_digest:
            raise RunError("preflight_binding_changed")
        roles = (*backends.CANDIDATES, "environment", "scorer")
        if [r.get("role") for r in marker.get("checks", [])] != list(roles) or not all(
                r.get("status") == "passed" for r in marker["checks"]):
            raise RunError("incomplete_preflight_cache")
        required = {name + "/" + filename for name in roles for filename in
                    ("private_model_io.jsonl", "private_model_io.jsonl.head.json", "action_attempts.jsonl")}
        if not required <= set(marker.get("files_sha256", {})):
            raise RunError("incomplete_preflight_cache")
        for rel, expected in marker["files_sha256"].items():
            path = directory / rel
            if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()) or not path.is_file() or digest(path) != expected:
                raise RunError("preflight_cache_changed")
        print("[复用预检] 六条路由已通过；不重复付费。", flush=True)
        for inner in route_backends.values():
            inner.close()
        return marker["checks"]
    request = preflight_request()
    with backends.runtime_overlay(manifest_digest, keys, directory / "diagnostics", court_retries=False):
        for name in (*backends.CANDIDATES, "environment", "scorer"):
            folder = directory / name
            inner = route_backends[name]
            attempts = _PreflightAttempts(inner, folder / "action_attempts.jsonl")
            journal = HashChainedDecisionJournal(attempts, folder / "private_model_io.jsonl", resume=True)
            role = "R_DEFENSE" if name in backends.CANDIDATES else "R_CHECK_ENVIRONMENT"
            try:
                journal.decide(role_id=role, **request)
                journal.ensure_replayed()
                reports.append({"role": name, "status": "passed", "case_data_sent": False,
                                "replayed": journal.replayed, "new_calls": journal.new_calls})
                print(f"[接口预检] {name}：passed", flush=True)
            finally:
                journal.close()
    hashes = {p.relative_to(directory).as_posix(): digest(p) for p in sorted(directory.rglob("*"))
              if p.is_file() and p.name != "complete.json"}
    atomic_json(marker_path, {"manifest_sha256": manifest_digest, "checks": reports,
                             "files_sha256": hashes, "all_passed": True}, immutable=True)
    return reports


def verify_complete(case_dir, manifest_digest):
    marker_path = case_dir / "court_complete.json"
    if not marker_path.exists():
        return None
    marker = read(marker_path)
    if marker.get("manifest_sha256") != manifest_digest:
        raise RunError("court_completion_binding_changed")
    import re
    if not re.fullmatch(r"court_attempts/attempt-\d{3,}", marker.get("attempt", "")):
        raise RunError("invalid_completed_attempt_path")
    required = {"private_model_io.jsonl", "private_model_io.jsonl.head.json", "action_attempts.jsonl"}
    required |= {marker["attempt"] + "/" + name for name in
                 ("summary.json", "author_events.jsonl", "jury_record.json", "artifacts/manifest.json")}
    if not required <= set(marker.get("files_sha256", {})):
        raise RunError("incomplete_court_completion_binding")
    for rel, expected in marker["files_sha256"].items():
        path = case_dir / rel
        if path.is_symlink() or not path.resolve().is_relative_to(case_dir.resolve()) or not path.is_file() or digest(path) != expected:
            raise RunError("completed_court_modified")
    attempt = case_dir / marker["attempt"]
    if read(attempt / "summary.json").get("status") != "completed_development_run":
        raise RunError("completed_court_status_changed")
    return attempt


def run_case(args, config, asset, case, candidate, keys, manifest_digest, *, engine_factory=None, backend_factory=None):
    from journal import HashChainedDecisionJournal
    from court_agent.replay import write_replay
    from reliability import ReliableBackend
    case_dir = args.out / "courts" / candidate / case.case_id
    case_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    complete = verify_complete(case_dir, manifest_digest)
    if complete:
        print(f"[复用庭审] {candidate}/{case.case_id}", flush=True)
        return complete
    attempts = case_dir / "court_attempts"
    attempts.mkdir(exist_ok=True)
    previous = sorted(attempts.glob("attempt-*"))
    if any(p.name != f"attempt-{n:03d}" or not p.is_dir() or p.is_symlink() for n, p in enumerate(previous, 1)):
        raise RunError("invalid_attempt_inventory")
    attempt = attempts / f"attempt-{len(previous)+1:03d}"
    attempt.mkdir()
    if backend_factory is not None:
        inner = backend_factory(config, candidate, case)
        backend = ReliableBackend(inner, case_dir / "action_attempts.jsonl", backends.bootstrap().COMPARISON_POLICY)
        backend.is_offline_fixture = bool(getattr(inner, "is_offline_fixture", False))
        if backend.is_offline_fixture:
            backend.replay_cached_decision = inner.decide
    elif args.mock:
        from demo import create_backend
        inner = create_backend(config, candidate, case)
        backend = ReliableBackend(inner, case_dir / "action_attempts.jsonl", backends.bootstrap().COMPARISON_POLICY)
        backend.is_offline_fixture = True
        backend.replay_cached_decision = inner.decide
    else:
        backend = backends.create_backend(config, keys, candidate, args.project_root,
                    args.claude_executable, attempt_ledger=case_dir / "action_attempts.jsonl")
    journal = HashChainedDecisionJournal(backend, case_dir / "private_model_io.jsonl", resume=True)
    if engine_factory is None:
        from workflow import FullWorkflowEngine
        engine_factory = FullWorkflowEngine
    run_config = deepcopy(config)
    run_config["mode"] = "mock" if args.mock else "live"
    run_config["models"] = {"lawyer": config["defense_models"][candidate], "environment": backends.ENVIRONMENT_MODEL}
    engine = engine_factory(case, journal, run_config, workflow_root=asset["workflow_root"],
                            workflow_spec=asset["workflow_spec"], artifact_root=attempt / "artifacts")
    print(f"[开庭] {candidate}/{case.case_id}；mode={run_config['mode']}", flush=True)
    try:
        engine.run()
        journal.ensure_replayed()
    finally:
        engine.save(attempt)
        write_replay(attempt, engine)
        journal.close()
    if engine.summary()["status"] != "completed_development_run":
        reason = str(engine.summary().get("stop_reason") or "court_incomplete")
        code = reason.split(":", 1)[0]
        import re
        raise RunError(code if re.fullmatch(r"[a-z][a-z0-9_]{0,100}", code) else "court_incomplete", reason)
    artifact_manifest = read(attempt / "artifacts" / "manifest.json")
    if artifact_manifest.get("replay_consumed_prior_artifacts") is False:
        raise RunError("artifact_replay_incomplete")
    tracked = [p for p in attempt.rglob("*") if p.is_file()]
    tracked += [case_dir / name for name in ("private_model_io.jsonl", "private_model_io.jsonl.head.json", "action_attempts.jsonl")]
    tracked += list((case_dir / "private_failed_outputs").glob("*.json"))
    marker = {"protocol": backends.PROTOCOL, "manifest_sha256": manifest_digest,
              "attempt": attempt.relative_to(case_dir).as_posix(), "mock_only": args.mock,
              "files_sha256": {p.relative_to(case_dir).as_posix(): digest(p) for p in sorted(tracked)}}
    atomic_json(case_dir / "court_complete.json", marker, immutable=True)
    return attempt


def publish(out, rows, *, mock=False):
    complete = all(row["status"] == ("mock_complete" if mock else "complete") for row in rows)
    result = {"protocol": backends.PROTOCOL, "mock_only": mock,
              "status": "complete" if complete else "incomplete", "rows": rows}
    atomic_json(out / "matrix_status.json", result)
    fields = ["candidate", "model", "case_id", "status", "numerator", "denominator", "rubric_coverage_percent", "error_code", "court_dir"]
    with (out / "results.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    body = "".join("<tr>" + "".join("<td>" + html.escape(str(row.get(k) if row.get(k) is not None else "—")) + "</td>" for k in fields[:8]) + "</tr>" for row in rows)
    page = '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>四律师全流程实验</title><style>body{font:16px/1.6 system-ui;margin:32px}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:8px}</style><h1>四律师 × 两案</h1><p>' + ("离线固定流程演练；没有真实模型调用，不产生能力分数。" if mock else "环境 gpt-5.6-sol；独立评分 gpt-6-sol。未完成/校验失败不记零分。") + '</p><table><tr>' + ''.join('<th>'+html.escape(k)+'</th>' for k in fields[:8]) + '</tr>' + body + '</table>'
    (out / "index.html").write_text(page, encoding="utf-8")


def run_matrix(args, config, assets, cases, keys, manifest, manifest_digest):
    rows = [{"candidate": name, "model": config["defense_models"][name], "case_id": cid,
             "status": "pending", "numerator": None, "denominator": 2 * len(assets[cid]["rubric"]["points"]),
             "rubric_coverage_percent": None, "error_code": None, "court_dir": None}
            for name in backends.CANDIDATES for cid in args.case_ids]
    publish(args.out, rows, mock=args.mock)
    processed = 0
    with backends.runtime_overlay(manifest_digest, keys, args.out / "private_diagnostics"):
        for row in rows:
            abort = False
            candidate, cid = row["candidate"], row["case_id"]
            case_dir = args.out / "courts" / candidate / cid
            score_path = case_dir / "full_workflow_score" / "score.json"
            previously_complete = False
            if (case_dir / "court_complete.json").exists() and score_path.exists():
                try:
                    previously_complete = read(score_path).get("status") == ("mock_only_not_scored" if args.mock else "complete")
                except (OSError, ValueError, RunError):
                    pass
            try:
                court_dir = run_case(args, config, assets[cid], cases[cid], candidate, keys, manifest_digest)
                row["court_dir"] = court_dir.relative_to(args.out).as_posix()
                if args.mock:
                    row["status"] = "mock_complete"
                    atomic_json(case_dir / "full_workflow_score" / "score.json", {
                        "case_id": cid, "status": "mock_only_not_scored", "rubric_coverage_percent": None,
                        "numerator": None, "denominator": row["denominator"], "model_calls": 0})
                else:
                    from scoring import score_case
                    def scorer_factory(cfg, attempt_dir):
                        return backends.shared_backend(cfg, keys, model=backends.SCORER_MODEL,
                            diagnostic_dir=case_dir / "private_scoring_outputs")
                    score = score_case(assets[cid], cases[cid], case_dir, court_dir, config, scorer_factory, resume=True)
                    for key in ("numerator", "denominator", "rubric_coverage_percent"):
                        row[key] = score.get(key)
                    row["status"] = "complete" if score.get("status") == "complete" else "scoring_incomplete"
                    if row["status"] != "complete":
                        issues = score.get("issues", [])
                        first = issues[0] if issues else "scoring_incomplete"
                        row["error_code"] = first.get("error_code", first.get("code", "scoring_incomplete")) if isinstance(first, dict) else str(first)
            except KeyboardInterrupt:
                row.update(status="interrupted", error_code="interrupted")
                publish(args.out, rows, mock=args.mock)
                raise
            except Exception as exc:
                row.update(status="failed", error_code=getattr(exc, "code", type(exc).__name__))
                print(f"[未完成] {candidate}/{cid}：{row['error_code']}", flush=True)
                abort = row["error_code"] in {"authentication_failed", "permission_denied", "missing_api_key", "billing_error"}
            finally:
                publish(args.out, rows, mock=args.mock)
            processed += int(not previously_complete)
            if abort:
                print("[停止] 凭证/权限/计费失败；停止后续付费任务。", flush=True)
                break
            if args.stop_after_cases is not None and processed >= args.stop_after_cases:
                print("[安全停止] 已在场次边界停止；同命令可恢复。", flush=True)
                break
    try:
        from delivery import export_case_runs
        for cid in args.case_ids:
            runs = {name: args.out / "courts" / name / cid for name in backends.CANDIDATES
                    if (args.out / "courts" / name / cid).exists()}
            if runs:
                export_case_runs(args.source_package, cid, runs, args.out / "deliveries" / cid,
                                 runtime_root=ROOT, config_path=args.config)
    except ImportError:
        raise RunError("delivery_module_missing")
    return rows


def main(argv=None):
    args = None
    keys = {}
    lock = None
    try:
        args = resolve_args(argv)
        config = load_config(args.project_root, args.config, gpt_model=args.gpt_model)
        assets, cases = prepare(args.source_package)
        manifest = build_manifest(args, config, assets)
        if args.validate_only:
            print(json.dumps({"status": "validated", "paid_model_calls": 0, "candidate_models": config["defense_models"],
                "environment_model": backends.ENVIRONMENT_MODEL, "scorer_model": backends.SCORER_MODEL,
                "planned_courts": len(args.case_ids) * len(backends.CANDIDATES), "rubric_counts": manifest["rubric_counts"], "mock_only": args.mock}, ensure_ascii=False))
            return 0
        args.out.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = (args.out / ".matrix.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunError("already_running") from None
        manifest_digest = bind_output(args.out, manifest)
        if not args.mock:
            keys = collect_keys(config, args.prompt_keys)
            preflight(args, config, keys, manifest_digest)
        if args.preflight_only:
            print("[预检完成] " + ("mock：无模型调用。" if args.mock else "六条真实路由已验证短JSON动作接口；未发送案件资料。"))
            return 0
        rows = run_matrix(args, config, assets, cases, keys, manifest, manifest_digest)
        expected = "mock_complete" if args.mock else "complete"
        passed = sum(row["status"] == expected for row in rows)
        print(f"[完成] {passed}/{len(rows)}；结果：{args.out / 'results.csv'}", flush=True)
        return 0 if passed == len(rows) else 3
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        code = getattr(exc, "code", "interrupted" if isinstance(exc, KeyboardInterrupt) else type(exc).__name__)
        print(json.dumps({"status": "stopped", "error_code": code,
                          "message": "运行已停止；旧记录和累计额度保留。"}, ensure_ascii=False), file=sys.stderr, flush=True)
        if args and args.out.exists() and lock is not None and code != "already_running":
            atomic_json(args.out / ("stopped-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json"),
                        {"error_code": code, "mock_only": args.mock})
        return 130 if isinstance(exc, KeyboardInterrupt) else 2
    finally:
        keys.clear()
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
