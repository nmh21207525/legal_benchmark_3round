"""CLI/launcher contracts: no paid API calls, bounded retry and safe resume."""
from __future__ import annotations

from copy import deepcopy
import csv
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "runtime"))
spec = importlib.util.spec_from_file_location("three_root_runner_for_tests", ROOT / "run.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
from court_agent.llm import BackendError
from court_agent.schema import action
from court_recovery import POLICY, court_recovery_overlay


def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line]


class FakePreflight:
    """Explicit network-free response source for exercising transport policy."""
    def __init__(self, failures=0, error="api_timeout", during_return=None):
        self.calls, self.requests = [], []
        self.stats = {"attempted_calls": 0, "paid_api_calls": 0}
        self.failures, self.error = failures, error
        self.during_return = during_return
        self.model = "offline_preflight_fixture"
        self.lawyer_backend = self
        self.closed = False

    def check_credentials(self):
        pass

    def decide(self, role_id, system, context, schema):
        self.requests.append(deepcopy({"role_id": role_id, "system": system, "context": context, "schema": schema}))
        self.stats["attempted_calls"] += 1
        failed = len(self.calls) < self.failures
        self.calls.append({"role_id": role_id, "model": self.model, "status": "failed" if failed else "completed"})
        if failed:
            raise BackendError(self.error, "offline fixture failure")
        answer = action("prepare_examination", text="离线接口流程测试：整理已获授权的资料，核对来源并记录待核实事项。")
        if self.during_return is not None:
            self.during_return()
        return answer

    def close(self):
        self.closed = True


class RunnerContractTests(unittest.TestCase):
    def args(self, directory, extra=()):
        args = app.arguments(["--out", str(Path(directory) / "run"), "--candidate", "seed", "--case-id", "CA-PILOT-005", *extra])
        config = app.load_config(args)
        return args, config

    def cli(self, *arguments, env=None):
        clean = dict(os.environ if env is None else env)
        for name in ("ARK_API_KEY", "PACKY_API_KEY", "PACKY_DEEPSEEK_API_KEY", "PACKY_OPUS_API_KEY"):
            clean.pop(name, None)
        return subprocess.run([sys.executable, "-B", str(ROOT / "run.py"), *map(str, arguments)],
                              cwd=ROOT, text=True, capture_output=True, env=clean, timeout=60)

    def test_defaults_development_and_preserved_limits(self):
        args = app.arguments([])
        config = app.load_config(args)
        self.assertEqual(args.mode, "development")
        self.assertEqual(args.case_ids, ("CA-PILOT-005", "CA-PILOT-006"))
        self.assertEqual(args.candidates, ("seed", "deepseek", "opus", "gpt"))
        self.assertEqual(config["timeout_seconds"], 300)
        self.assertEqual(config["max_output_tokens"], 16384)
        self.assertEqual(config["max_model_calls"], 360)
        self.assertEqual(POLICY["max_attempts_per_logical_decision"], 8)
        self.assertFalse(args.background)
        self.assertFalse(args.score_only)

    def test_cli_validate_needs_no_key_and_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "validate"
            result = self.cli("--validate-only", "--out", out)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["status"], "validated")
            self.assertEqual(data["planned_courts"], 8)
            self.assertEqual(data["rounds"], 3)
            self.assertEqual(data["paid_model_calls"], 0)
            self.assertFalse(data["live_connectivity_tested"])
            self.assertFalse(out.exists())

    def test_formal_pending_rubric_fails_before_keys_or_output(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "formal"
            result = self.cli("--mode", "formal", "--validate-only", "--out", out,
                              "--rubric-dir", Path(directory) / "empty_rubrics")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stderr)["error_code"], "formal_requires_frozen_rubric")
            self.assertFalse(out.exists())

    def test_offline_formal_is_rejected_before_output_or_api(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "offline_formal"
            result = self.cli("--mock", "--mode", "formal", "--out", out)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stderr)["error_code"], "mock_requires_development_mode")
            self.assertFalse(out.exists())

    def test_verified_complete_live_resume_skips_credentials_and_preflight(self):
        # Integrity verification itself is covered by real completion markers in
        # integration tests. Stub its successful outcome here to isolate the
        # launcher's no-network/no-password path for already complete live runs.
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "completed"
            attempt = out / "courts/gpt/CA-PILOT-005/court_attempts/attempt-001"
            attempt.mkdir(parents=True)
            def verified_rows(args, values, assets, binding):
                for value in values:
                    value.update(status="development_complete", court_dir=attempt.relative_to(out).as_posix())
                return values
            with patch.object(app, "hydrate_rows", side_effect=verified_rows), \
                 patch.object(app, "collect_keys", side_effect=AssertionError("completed_run_requested_keys")) as collect, \
                 patch.object(app, "preflight", side_effect=AssertionError("completed_run_requested_preflight")) as preflight, \
                 patch.object(app.legacy_runner, "bind_output", return_value="e" * 64), \
                 patch.object(app.legacy_runner, "run_case", return_value=attempt) as reuse, \
                 patch.object(app.backends, "create_backend", side_effect=AssertionError("completed_run_created_live_backend")), \
                 patch.object(app.backends, "shared_backend", side_effect=AssertionError("completed_run_created_shared_backend")), \
                 patch("three_delivery.publish", return_value={}) as publish, \
                 patch.object(app.signal, "signal"), \
                 patch("sys.stdout", new_callable=io.StringIO):
                code = app.main(["--candidate", "gpt", "--case-id", "CA-PILOT-005", "--out", str(out), "--resume"])
            self.assertEqual(code, 0)
            collect.assert_not_called()
            preflight.assert_not_called()
            reuse.assert_called_once()
            self.assertTrue(publish.called)
            self.assertTrue(all(call.args[1][0]["status"] == "development_complete" for call in publish.call_args_list))
            state = json.loads((out / "process.json").read_text())
            self.assertEqual((state["status"], state["exit_code"]), ("exited", 0))

    def test_live_without_key_does_not_start_models(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "no_key"
            result = self.cli("--candidate", "gpt", "--case-id", "CA-PILOT-005", "--out", out)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stderr)["error_code"], "missing_credential_environment")
            self.assertFalse((out / "courts").exists())
            self.assertFalse((out / "preflight").exists())

    def test_existing_run_lock_blocks_second_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "locked"
            out.mkdir()
            with (out / ".matrix.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = self.cli("--mock", "--candidate", "gpt", "--case-id", "CA-PILOT-005", "--out", out)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stderr)["error_code"], "already_running")
            self.assertFalse((out / "courts").exists())

    def test_selected_preflight_uses_eight_attempts_unchanged_request_and_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            args, config = self.args(directory)
            stop = app.StopControl(args.out)
            created, configs = [], []
            def build(*positional, **kwargs):
                configs.append(deepcopy(positional[0]))
                # Also exercise retries for a format failure in environment.
                fixture = FakePreflight(7, "schema_mismatch")
                created.append(fixture)
                return fixture
            with patch.object(app.backends, "create_backend", side_effect=build) as candidate_factory, \
                 patch.object(app.backends, "shared_backend", side_effect=build) as shared_factory, \
                 court_recovery_overlay("a" * 64, package_backends=app.backends, sleep=lambda seconds: None):
                app.preflight(args, config, {}, "a" * 64, stop)
                self.assertEqual([len(b.calls) for b in created], [8, 8])
                self.assertEqual(candidate_factory.call_count, 1)
                self.assertEqual(candidate_factory.call_args.args[2], "seed")
                self.assertEqual(shared_factory.call_count, 1)
                self.assertEqual(shared_factory.call_args.kwargs["model"], app.backends.ENVIRONMENT_MODEL)
                for fixture in created:
                    self.assertTrue(all(request == fixture.requests[0] for request in fixture.requests))
                app.preflight(args, config, {}, "a" * 64, stop)
                self.assertEqual([len(b.calls) for b in created], [8, 8, 0, 0])
            self.assertTrue(all(c["max_output_tokens"] == 16384 and c["timeout_seconds"] == 300 for c in configs))
            self.assertEqual({p.name for p in (args.out / "preflight").iterdir()}, {"seed", "environment", "report.json"})
            for route in ("seed", "environment"):
                saved = rows(args.out / "preflight" / route / "action_attempts.jsonl")
                self.assertEqual(len([r for r in saved if r["kind"] == "reserve"]), 8)
                self.assertEqual(saved[0]["policy"]["environment_max_attempts_per_decision"], 8)
                self.assertEqual(saved[0]["policy"]["lawyer_max_attempts_per_decision"], 8)

    def test_preflight_exhaustion_survives_resume_without_extra_call(self):
        with tempfile.TemporaryDirectory() as directory:
            args, config = self.args(directory)
            created = []
            def build(*args, **kwargs):
                fixture = FakePreflight(100)
                created.append(fixture)
                return fixture
            with patch.object(app.backends, "create_backend", side_effect=build), \
                 patch.object(app.backends, "shared_backend", side_effect=build), \
                 court_recovery_overlay("b" * 64, package_backends=app.backends, sleep=lambda seconds: None):
                with self.assertRaises(BackendError):
                    app.preflight(args, config, {}, "b" * 64, app.StopControl(args.out))
                self.assertEqual(sum(len(b.calls) for b in created), 8)
                with self.assertRaises(BackendError) as raised:
                    app.preflight(args, config, {}, "b" * 64, app.StopControl(args.out))
                self.assertEqual(raised.exception.code, "decision_attempt_budget_exhausted")
                self.assertEqual(sum(len(b.calls) for b in created), 8)

    def test_stop_during_preflight_persists_current_answer_then_stops_next_call(self):
        with tempfile.TemporaryDirectory() as directory:
            args, config = self.args(directory)
            stop = app.StopControl(args.out)
            created = []
            def build(*unused, **kwargs):
                fixture = FakePreflight(during_return=stop.event.set if not created else None)
                created.append(fixture)
                return fixture
            with patch.object(app.backends, "create_backend", side_effect=build), \
                 patch.object(app.backends, "shared_backend", side_effect=build), \
                 court_recovery_overlay("c" * 64, package_backends=app.backends, sleep=lambda seconds: None, stop_requested=stop.requested):
                with self.assertRaises(app.RunError) as raised:
                    app.preflight(args, config, {}, "c" * 64, stop)
                self.assertEqual(raised.exception.code, "operator_requested_stop")
                self.assertEqual([len(b.calls) for b in created], [1, 0])
                saved = rows(args.out / "preflight/seed/private_model_io.jsonl")
                self.assertEqual(saved[-1]["kind"], "response")
                attempts = rows(args.out / "preflight/seed/action_attempts.jsonl")
                self.assertEqual(attempts[-1]["status"], "passed")
                stop.event.clear()
                app.preflight(args, config, {}, "c" * 64, stop)
                self.assertEqual([len(b.calls) for b in created], [1, 0, 0, 1])

    def test_completed_rows_survive_limited_resume_without_new_decisions(self):
        # Real courts/journals with a minimal CSV publisher to isolate launcher
        # state handling from HTML and ZIP work (covered by delivery tests).
        def csv_publish(out, values, *unused, **kwargs):
            out.mkdir(parents=True, exist_ok=True)
            with (out / "results.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(values[0]))
                writer.writeheader()
                writer.writerows(values)
            return {}
        with tempfile.TemporaryDirectory() as directory:
            args = app.arguments(["--mock", "--candidate", "gpt", "--out", str(Path(directory) / "run")])
            config = app.load_config(args)
            assets, cases = app.prepare(args)
            with patch("three_delivery.publish", side_effect=csv_publish):
                code = app.execute(args, config, assets, cases, {}, "d" * 64, app.StopControl(args.out))
                self.assertEqual(code, 0)
                ledger_before = {cid: (args.out / "courts/gpt" / cid / "action_attempts.jsonl").read_bytes() for cid in args.case_ids}
                args.stop_after_cases = 1
                code = app.execute(args, config, assets, cases, {}, "d" * 64, app.StopControl(args.out))
            self.assertEqual(code, 0)
            with (args.out / "results.csv").open() as stream:
                results = list(csv.DictReader(stream))
            self.assertEqual([r["status"] for r in results], ["mock_complete", "mock_complete"])
            for cid in args.case_ids:
                self.assertEqual((args.out / "courts/gpt" / cid / "action_attempts.jsonl").read_bytes(), ledger_before[cid])


if __name__ == "__main__":
    unittest.main()
