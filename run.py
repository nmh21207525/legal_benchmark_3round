#!/usr/bin/env python3
"""Independent three-round workspace with shared pre-run rubrics and per-case launchers."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import threading
import warnings

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
sys.path.insert(0, str(RUNTIME))
import backends
backends.bootstrap()
import runner as legacy_runner
from court_recovery import court_recovery_overlay, POLICY

PROTOCOL = "legal-three-rounds-workspace-v2"
CASES = ("CA-PILOT-005", "CA-PILOT-006")
read = legacy_runner.read
atomic_json = legacy_runner.atomic_json
digest = legacy_runner.digest
RunError = legacy_runner.RunError


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=("development", "formal"), default="development")
    p.add_argument("--case-id", action="append", choices=CASES)
    p.add_argument("--candidate", action="append", choices=(*backends.CANDIDATES, "all"))
    p.add_argument("--data", type=Path, default=ROOT / "data")
    p.add_argument("--rubric-dir", type=Path, default=ROOT / "rubrics")
    p.add_argument("--config", type=Path)
    p.add_argument("--project-root", type=Path, help="Only resolves relative Codex executable paths; does not select case data.")
    p.add_argument("--claude-executable", default=None)
    p.add_argument("--out", type=Path)
    p.add_argument("--validate-only", action="store_true")
    p.add_argument("--preflight-only", action="store_true")
    p.add_argument("--mock", "--offline-demo", action="store_true")
    p.add_argument("--prompt-keys", action="store_true")
    p.add_argument("--background", action="store_true")
    p.add_argument("--resume", action="store_true", help="Identical runs resume automatically; budgets never reset.")
    p.add_argument("--score-only", action="store_true", help="Score already complete formal courts; never starts a court.")
    p.add_argument("--stop-after-cases", type=int)
    args = p.parse_args(argv)
    if args.mock and args.mode == "formal":
        raise RunError("mock_requires_development_mode", "离线演示只用于开发流程检查，不启动真实评分器或生成正式成绩。")
    args.case_ids = tuple(x for x in CASES if not args.case_id or x in args.case_id)
    args.candidates = tuple(x for x in backends.CANDIDATES if not args.candidate or "all" in args.candidate or x in args.candidate)
    args.data = args.data.expanduser().resolve()
    args.source_package = args.data
    args.rubric_dir = args.rubric_dir.expanduser().resolve()
    suffix = "-".join(x.rsplit("-", 1)[1] for x in args.case_ids)
    label = "all" if len(args.candidates) == 4 else "-".join(args.candidates)
    raw = (args.out or ROOT / "runs" / f"{'mock-' if args.mock else ''}{args.mode}-{label}-{suffix}").expanduser()
    if raw.is_symlink():
        raise RunError("output_symlink")
    args.out = raw.resolve()
    for protected in (RUNTIME, args.data, args.rubric_dir):
        if args.out == protected or protected in args.out.parents or args.out in protected.parents:
            raise RunError("output_overlaps_code_or_data")
    if args.score_only and args.mode != "formal":
        raise RunError("score_only_requires_formal_mode")
    if args.stop_after_cases is not None and args.stop_after_cases < 1:
        raise RunError("invalid_stop_after_cases")
    if args.background and args.validate_only:
        raise RunError("validate_only_does_not_need_background")
    if args.claude_executable is None:
        known = Path("/home/nmh/.local/claude-court/node_modules/.bin/claude")
        args.claude_executable = shutil.which("claude") or (str(known) if known.is_file() else "claude")
    return args


def load_config(args):
    path = args.config
    if path is None:
        # Both collaborators start from the repository configuration. Never
        # inherit one server's older experiment parameters implicitly.
        path = next((x for x in (ROOT / "config.local.json", ROOT / "config.example.json") if x.is_file()), None)
    if path is None or not Path(path).is_file():
        raise RunError("config_missing")
    path = Path(path).expanduser().resolve()
    args.config = path
    args.project_root = (args.project_root or path.parent).expanduser().resolve()
    raw = read(path)
    fixed = {"max_lawyer_actions": 150, "max_model_calls": 360,
             "max_output_tokens": 16384, "timeout_seconds": 300,
             "default_actions_per_role_per_stage": 3, "max_input_chars": 700000}
    for key, expected in fixed.items():
        if raw.get(key, expected) != expected:
            raise RunError("fixed_experiment_setting_changed", key)
    if raw.get("actions_per_role_per_stage"):
        raise RunError("fixed_experiment_setting_changed", "actions_per_role_per_stage")
    config = backends.normalize_config(raw, args.project_root)
    if config["defense_models"] != backends.DEFAULT_MODELS:
        raise RunError("fixed_experiment_setting_changed", "defense_models")
    config.update(fixed)
    config["mode"] = "mock" if args.mock else "live"
    return config


def prepare(args):
    from three_assets import load_assets, ThreeRoundCaseData
    from workflow import validate_workflow
    from three_scoring import validate_rubric
    assets = load_assets(args.data)
    cases = {}
    for cid in args.case_ids:
        if cid not in assets:
            raise RunError("case_missing", cid)
        asset = assets[cid]
        case = ThreeRoundCaseData(asset["case_path"])
        errors = case.validate()
        if errors:
            raise RunError("invalid_case_assets", f"{cid}: {errors}")
        validate_workflow(asset["workflow_root"], asset["workflow_spec"], case)
        frozen = args.rubric_dir / f"{cid}.json"
        if args.mode == "formal":
            if not frozen.is_file():
                raise RunError("formal_requires_frozen_rubric", f"{cid}: 缺少本题冻结rubric；请使用包含rubrics目录的完整工作区。")
            asset["rubric"] = read(frozen)
            asset["rubric_path"] = frozen
            validate_rubric(asset, require_frozen=True)
        cases[cid] = case
    return {cid: assets[cid] for cid in args.case_ids}, cases


def build_manifest(args, config, assets):
    code = {}
    files = list(RUNTIME.rglob("*.py")) + list(ROOT.glob("*.py")) + list(ROOT.glob("*.sh"))
    files += list((ROOT / "scripts").rglob("*.py")) + list((ROOT / "scripts").rglob("*.sh"))
    files += [ROOT / "requirements.txt"]
    for path in sorted(files):
        if path.is_symlink():
            raise RunError("code_symlink")
        if path.is_file():
            code[path.relative_to(ROOT).as_posix()] = digest(path)
    data = {}
    for cid in args.case_ids:
        for base in (Path(assets[cid]["case_path"]), Path(assets[cid]["workflow_root"])):
            for path in sorted(base.rglob("*")):
                if path.is_symlink():
                    raise RunError("asset_symlink")
                if path.is_file():
                    data[path.relative_to(args.data).as_posix()] = digest(path)
    return {"protocol": PROTOCOL, "mode": args.mode, "mock_only": args.mock,
            "case_ids": list(args.case_ids), "candidates": list(args.candidates), "round_count": 3,
            "code_sha256": code, "asset_sha256": data, "config": config,
            "defense_candidates": {c: backends.candidate_spec(c, config) for c in args.candidates},
            "environment_model": backends.ENVIRONMENT_MODEL, "scorer_model": backends.SCORER_MODEL,
            "court_policy": POLICY, "workers": 1, "rubric_sent_to_court": False,
            "rubrics": {cid: {"sha256": legacy_runner.value_hash(a["rubric"]),
                              "status": a["rubric"].get("status"),
                              "frozen_before_run": args.mode == "formal"} for cid, a in assets.items()},
            "claude_executable": args.claude_executable if "opus" in args.candidates else None,
            "old_runs_imported": False, "development_scores_are_not_formal_results": True}


def collect_keys(config, args):
    from court_agent.llm import api_settings
    names = {"seed": "ARK_API_KEY", "deepseek": "PACKY_DEEPSEEK_API_KEY",
             "opus": "PACKY_OPUS_API_KEY", "gpt": api_settings(config)["key_env"]}
    selected = ("gpt",) if args.score_only else tuple(dict.fromkeys((*args.candidates, "gpt")))
    keys = {}
    for name in selected:
        value = os.environ.get(names[name], "").strip()
        if not value and args.prompt_keys:
            if not sys.stdin.isatty():
                raise RunError("prompt_requires_terminal", "请在交互终端输入密钥；后台请使用 --background，程序会先询问密钥再脱离终端。")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass(name + " API key（隐藏输入，不保存）: ").strip()
        if not value:
            raise RunError("missing_credential_environment", names[name])
        keys[name] = value
    return keys


class StopControl:
    def __init__(self, out):
        self.path = out / "STOP_REQUESTED"
        self.event = threading.Event()

    def requested(self):
        return self.event.is_set() or self.path.exists()

    def handler(self, signum, frame):
        self.event.set()
        print("[停止请求] 当前模型调用将完成并落盘；之后停止。请勿强制 kill -9。", flush=True)


@contextmanager
def tee_log(path):
    class Tee:
        def __init__(self, original, log):
            self.original, self.log = original, log
        def write(self, text):
            self.original.write(text)
            self.log.write(text)
            self.log.flush()
            return len(text)
        def flush(self):
            self.original.flush()
            self.log.flush()
        def isatty(self):
            return self.original.isatty()
        @property
        def encoding(self):
            return "utf-8"
    log = Path(path).open("a", encoding="utf-8", buffering=1)
    previous = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = Tee(sys.stdout, log), Tee(sys.stderr, log)
    try:
        yield
    finally:
        sys.stdout, sys.stderr = previous
        log.close()


def detach(args):
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid:
        print(f"[后台已启动] PID={pid}\n[日志] {args.out / 'launcher.log'}\n[结果] {args.out / 'results.csv'}\n[页面] {args.out / 'index.html'}", flush=True)
        return False
    os.setsid()
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    fd = os.open(os.devnull, os.O_RDWR)
    os.dup2(fd, 0)
    # tee_log writes one log copy; original stdout/stderr are discarded in background.
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    if fd > 2:
        os.close(fd)
    return True


def preflight(args, config, keys, binding, stop):
    from journal import HashChainedDecisionJournal
    from reliability import ReliableBackend
    routes = ("scorer",) if args.score_only else (*args.candidates, "environment", *(("scorer",) if args.mode == "formal" else ()))
    directory = args.out / "preflight"
    report = []
    back = {}
    try:
        # Check every selected executable/credential before making a paid request.
        for name in routes:
            folder = directory / name
            if name in backends.CANDIDATES:
                inner = backends.create_backend(config, keys, name, args.project_root, args.claude_executable,
                                               diagnostic_dir=folder / "private_failed_outputs")
            else:
                model = backends.SCORER_MODEL if name == "scorer" else backends.ENVIRONMENT_MODEL
                inner = backends.shared_backend(config, keys, model=model, diagnostic_dir=folder / "private_failed_outputs")
            back[name] = inner
            inner.check_credentials()
        request = legacy_runner.preflight_request()
        for name, inner in back.items():
            folder = directory / name
            reliable = ReliableBackend(inner, folder / "action_attempts.jsonl", backends.bootstrap().COMPARISON_POLICY)
            journal = HashChainedDecisionJournal(reliable, folder / "private_model_io.jsonl", resume=True)
            try:
                journal.decide(role_id="R_DEFENSE" if name in backends.CANDIDATES else "R_CHECK_ENVIRONMENT", **request)
                journal.ensure_replayed()
                row = {"route": name, "status": "passed", "new_calls": journal.new_calls, "replayed": journal.replayed}
                report.append(row)
                print(f"[接口预检] {name}：通过；新增决策 {journal.new_calls}，复用 {journal.replayed}。", flush=True)
            finally:
                journal.close()
            if stop.requested():
                raise RunError("operator_requested_stop")
        atomic_json(directory / "report.json", {"manifest_sha256": binding, "checks": report})
    finally:
        for inner in back.values():
            inner.close()


def initial_rows(args, config, assets):
    rows = []
    for candidate in args.candidates:
        for cid in args.case_ids:
            points = assets[cid]["rubric"].get("points", assets[cid]["rubric"].get("items", []))
            rows.append({"candidate": candidate, "model": config["defense_models"][candidate], "case_id": cid,
                         "status": "pending", "numerator": None,
                         "denominator": 2 * len(points) if args.mode == "formal" else None,
                         "rubric_coverage_percent": None, "error_code": None, "court_dir": None,
                         "warning_count": 0})
    return rows


def hydrate_rows(args, rows, assets, binding):
    """Keep verified completed cases visible before any resumed network request."""
    for row in rows:
        case_dir = args.out / "courts" / row["candidate"] / row["case_id"]
        attempt = legacy_runner.verify_complete(case_dir, binding)
        if attempt is None:
            prior = sorted((case_dir / "court_attempts").glob("attempt-*"))
            if prior and (prior[-1] / "summary.json").is_file():
                summary = read(prior[-1] / "summary.json")
                row.update(status="failed", court_dir=prior[-1].relative_to(args.out).as_posix(),
                           error_code=(str(summary.get("stop_reason") or "court_incomplete").split(":", 1)[0]))
            continue
        summary = read(attempt / "summary.json")
        if summary.get("simulation_round_count") != 3 or not summary.get("verdict") or "r3_verdict" not in summary.get("completed_stages", []):
            raise RunError("completed_court_missing_third_round")
        row.update(court_dir=attempt.relative_to(args.out).as_posix(),
                   status="mock_complete" if args.mock else "development_complete")
        if args.mode != "formal":
            continue
        row["status"] = "scoring_pending"
        folder = case_dir / "direct_score"
        marker = folder / "score_complete.json"
        if not marker.is_file():
            continue
        binding_data = read(marker)
        files = binding_data.get("files_sha256", {})
        if "score.json" not in files:
            raise RunError("completed_score_missing_binding")
        for name, expected in files.items():
            path = folder / name
            if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()) or not path.is_file() or digest(path) != expected:
                raise RunError("completed_score_changed")
        score = read(folder / "score.json")
        if score.get("rubric_freeze_sha256") != assets[row["case_id"]]["rubric"].get("freeze_sha256") or score.get("status") != "complete":
            raise RunError("completed_score_rubric_mismatch")
        for key in ("numerator", "denominator", "rubric_coverage_percent", "warning_count"):
            row[key] = score.get(key)
        row["status"] = "mock_complete" if args.mock else "complete"
    return rows


def execute(args, config, assets, cases, keys, binding, stop):
    from three_engine import ThreeRoundEngine
    from three_demo import create_backend
    from three_delivery import publish
    from three_scoring import score_case
    rows = hydrate_rows(args, initial_rows(args, config, assets), assets, binding)
    def publish_now():
        return publish(args.out, rows, assets, cases, mode=args.mode, mock=args.mock, package_root=ROOT)
    publish_now()
    with backends.runtime_overlay(binding, keys, args.out / "private_diagnostics", court_retries=False):
        with court_recovery_overlay(binding, package_backends=backends, stop_requested=stop.requested):
            finished_states = {"complete", "development_complete", "mock_complete"}
            if not args.mock and (args.preflight_only or any(r["status"] not in finished_states for r in rows)):
                preflight(args, config, keys, binding, stop)
            elif not args.mock:
                print("[复用完成记录] 所选庭审和当前模式所需评分均已完成；不再次调用模型。", flush=True)
            if args.preflight_only:
                print("[接口预检结束] 尚未启动庭审。", flush=True)
                return 0
            for index, row in enumerate(rows, 1):
                candidate, cid = row["candidate"], row["case_id"]
                case_dir = args.out / "courts" / candidate / cid
                if stop.requested():
                    break
                print(f"[三轮庭审 {index}/{len(rows)}] {candidate}/{cid}；{args.mode}", flush=True)
                try:
                    if args.score_only:
                        attempt = legacy_runner.verify_complete(case_dir, binding)
                        if attempt is None:
                            raise RunError("score_only_requires_complete_court")
                    else:
                        attempt = legacy_runner.run_case(args, config, assets[cid], cases[cid], candidate, keys, binding,
                            engine_factory=ThreeRoundEngine, backend_factory=create_backend if args.mock else None)
                    row["court_dir"] = attempt.relative_to(args.out).as_posix()
                    row["status"] = "mock_complete" if args.mock else "development_complete"
                    if args.mode == "formal":
                        if stop.requested():
                            row.update(status="scoring_pending", error_code="operator_requested_stop")
                        else:
                            def factory(cfg, diagnostic_dir):
                                return backends.shared_backend(cfg, keys, model=backends.SCORER_MODEL, diagnostic_dir=diagnostic_dir)
                            score = score_case(assets[cid], cases[cid], case_dir, attempt, config, factory, mock=args.mock)
                            for key in ("numerator", "denominator", "rubric_coverage_percent", "warning_count"):
                                row[key] = score.get(key)
                            row["status"] = "mock_complete" if args.mock else ("complete" if score.get("status") == "complete" else "scoring_incomplete")
                            if row["status"] == "scoring_incomplete":
                                row["error_code"] = score.get("error_code", "scoring_incomplete")
                    print(f"[场次结束] {candidate}/{cid}：{row['status']}" +
                          (f"；{row['numerator']}/{row['denominator']}" if row["numerator"] is not None else "；未生成正式分数。"), flush=True)
                except Exception as exc:
                    code = getattr(exc, "code", type(exc).__name__)
                    row.update(status="stopped" if stop.requested() else "failed", error_code=code)
                    prior = sorted((case_dir / "court_attempts").glob("attempt-*"))
                    if prior:
                        row["court_dir"] = prior[-1].relative_to(args.out).as_posix()
                    atomic_json(case_dir / "run_failure.json", {"error_code": code, "detail": str(exc)[:1000]})
                    print(f"[场次未完成] {candidate}/{cid}：{code}；记录与累计额度保留。", flush=True)
                publish_now()
                if stop.requested() or (args.stop_after_cases and index >= args.stop_after_cases):
                    break
    completed = sum(r["status"] in {"complete", "development_complete", "mock_complete"} for r in rows)
    publish_now()
    print(f"[本轮结束] {completed}/{len(rows)}；{args.mode}；mock={args.mock}", flush=True)
    print(f"[CSV] {args.out / 'results.csv'}\n[HTML] {args.out / 'index.html'}\n[日志] {args.out / 'launcher.log'}", flush=True)
    return 0 if completed == len(rows) else (130 if stop.requested() else 3)


def main(argv=None):
    keys, lock, args = {}, None, None
    try:
        args = arguments(argv)
        config = load_config(args)
        assets, cases = prepare(args)
        manifest = build_manifest(args, config, assets)
        if args.validate_only:
            print(json.dumps({"status": "validated", "mode": args.mode, "rounds": 3,
                "planned_courts": len(args.candidates)*len(args.case_ids), "case_ids": args.case_ids,
                "candidate_models": {c: config['defense_models'][c] for c in args.candidates},
                "rubric_status": {cid: a['rubric'].get('status') for cid, a in assets.items()},
                "pretrial": True, "tool_calls": True, "paid_model_calls": 0,
                "live_connectivity_tested": False, "out": str(args.out)}, ensure_ascii=False, indent=2))
            return 0
        args.out.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(args.out / ".matrix.lock", os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        lock = os.fdopen(fd, "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunError("already_running", "这个输出目录已经有运行中的进程。") from None
        binding = legacy_runner.bind_output(args.out, manifest)
        existing = hydrate_rows(args, initial_rows(args, config, assets), assets, binding)
        needs_live = args.preflight_only or any(r["status"] not in {"complete", "development_complete", "mock_complete"} for r in existing)
        if not args.mock and needs_live:
            keys = collect_keys(config, args)
        stop = StopControl(args.out)
        stop.path.unlink(missing_ok=True)
        if args.background and not detach(args):
            return 0
        signal.signal(signal.SIGTERM, stop.handler)
        signal.signal(signal.SIGINT, stop.handler)
        atomic_json(args.out / "process.json", {"pid": os.getpid(), "status": "running", "mode": args.mode,
                    "case_ids": args.case_ids, "candidates": args.candidates})
        with tee_log(args.out / "launcher.log"):
            print(f"[工作区] {ROOT}\n[输出目录] {args.out}\n[模式] {args.mode}；串行；每场三轮；庭前准备与工具调用已启用。", flush=True)
            if args.mock:
                print("[离线演示] 不调用模型，不代表模型表现，不生成正式成绩。", flush=True)
            elif args.mode == "development":
                print("[开发试跑] rubric 待完善；本轮保存完整过程与产物，不生成正式成绩。", flush=True)
            try:
                code = execute(args, config, assets, cases, keys, binding, stop)
            except Exception as exc:
                print(json.dumps({"status": "stopped", "error_code": getattr(exc, "code", type(exc).__name__),
                                  "detail": str(exc)[:1000]}, ensure_ascii=False), flush=True)
                code = 2
            atomic_json(args.out / "process.json", {"pid": os.getpid(), "status": "exited", "exit_code": code,
                        "mode": args.mode, "case_ids": args.case_ids, "candidates": args.candidates})
            return code
    except Exception as exc:
        print(json.dumps({"status": "stopped", "error_code": getattr(exc, "code", type(exc).__name__),
                          "detail": str(exc)[:1000]}, ensure_ascii=False), file=sys.stderr, flush=True)
        return 2
    finally:
        keys.clear()
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
