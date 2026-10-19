"""Offline end-to-end gates for real three-round flow and exact recovery.

No model response is obtained and no score is produced by these tests.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
import backends
backends.bootstrap()
import runner
from three_assets import ThreeRoundCaseData, load_assets
from three_demo import create_backend
from three_engine import ThreeRoundEngine


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line]


def configuration():
    return backends.normalize_config({
        "api": {"provider": "packy"}, "codex": {"executable": "codex"},
        "max_lawyer_actions": 120, "default_actions_per_role_per_stage": 3,
        "max_model_calls": 360,
    }, ROOT)


class ThreeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = load_assets(ROOT / "data")

    def run_case(self, out, cid, *, engine_factory=ThreeRoundEngine):
        case = ThreeRoundCaseData(self.assets[cid]["case_path"])
        args = SimpleNamespace(out=Path(out), project_root=ROOT, mock=True,
                               claude_executable="unused_offline")
        # Fail loudly if a regression tries a network connection from the fixture.
        with patch("socket.socket.connect", side_effect=AssertionError("offline_test_attempted_network")):
            return runner.run_case(args, configuration(), self.assets[cid], case,
                                   "gpt", {}, "0" * 64,
                                   engine_factory=engine_factory, backend_factory=create_backend)

    def assert_complete(self, attempt, cid):
        summary = read(attempt / "summary.json")
        self.assertEqual(summary["status"], "completed_development_run", summary.get("stop_reason"))
        self.assertEqual(summary["current_stage"], "ended")
        self.assertEqual(summary["mode"], "mock")
        self.assertIsNone(summary["formal_benchmark_score"])
        self.assertEqual(summary["model_stats"].get("paid_api_calls", 0), 0)
        self.assertEqual(summary["rejected_actions"], 0)
        events = jsonl(attempt / "author_events.jsonl")
        by_kind = lambda kind: [e for e in events if e["kind"] == kind]
        started = {e["stage_id"] for e in by_kind("stage_started")}
        self.assertTrue({"r1_inquiry", "r2_examination", "r3_clarification",
                         "r3_instructions", "r3_verdict"} <= started)
        instructions = by_kind("delivered_instruction")
        self.assertTrue(instructions)
        self.assertEqual({e["stage_id"] for e in instructions}, {"r3_instructions"})
        verdicts = by_kind("simulated_verdict")
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["stage_id"], "r3_verdict")
        self.assertFalse(verdicts[0]["meta"]["is_ground_truth"])
        self.assertEqual(len(summary["verdict"]["charges"]), 1 if cid.endswith("005") else 2)
        consultations = by_kind("private_consultation")
        self.assertTrue(consultations)
        self.assertIn("preparation", {e["stage_id"] for e in consultations})
        for event in consultations:
            self.assertTrue(event["meta"]["question"])
            self.assertTrue(event["text"])
            self.assertNotIn("R_PROSECUTOR", event["audience"])
            self.assertNotIn("R_JURY", event["audience"])
            question = next(e for e in events if e["event_id"] == event["meta"]["source_question_event_id"])
            self.assertEqual(question["kind"], "private_consultation_question")
            self.assertEqual(question["text"], event["meta"]["question"])
            self.assertEqual(question["meta"]["response_event_id"], event["event_id"])
        tools = {e["meta"].get("tool") for e in by_kind("tool_result")}
        self.assertTrue({"read_document", "search_case", "search_law", "request_investigation", "write_artifact"} <= tools)
        workflow = read(attempt / "workflow_summary.json")
        self.assertEqual(workflow["missing_defense_artifacts"], [])
        artifacts = read(attempt / "artifacts/manifest.json")["artifacts"]
        outputs = {a["name"] for a in artifacts if a["kind"] == "lawyer_output"}
        self.assertTrue({"case_timeline.csv", "evidence_register.csv", "pretrial_plan.md", "examination_outline.md"} <= outputs)
        jury_record = read(attempt / "jury_record.json")
        if isinstance(jury_record, dict):
            jury_record = jury_record.get("events", jury_record.get("record", []))
        jury_evidence = [e for e in jury_record if e["kind"] == "evidence"]
        self.assertTrue(any(e["stage_id"].startswith("r2_") for e in jury_evidence))
        self.assertTrue(any(e["stage_id"].startswith("r3_") for e in jury_evidence))
        self.assertFalse(any(e["stage_id"].startswith("r1_") for e in jury_evidence))
        for name in ("replay.html", "artifact_manifest.json", "environment_manifest.json",
                     "review_session.json", "summary.json", "author_events.jsonl", "jury_record.json"):
            self.assertTrue((attempt / name).is_file(), name)
        self.assertFalse(list(attempt.rglob("score.json")))
        return events

    def test_005_and_006_complete_three_rounds_with_pretrial_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            for cid in ("CA-PILOT-005", "CA-PILOT-006"):
                with self.subTest(case_id=cid):
                    attempt = self.run_case(directory, cid)
                    self.assert_complete(attempt, cid)
                    marker = attempt.parent.parent / "court_complete.json"
                    marker_before = marker.read_bytes()
                    self.assertEqual(self.run_case(directory, cid), attempt)
                    self.assertEqual(marker.read_bytes(), marker_before)

    def test_006_late_source_stays_private_until_trusted_release(self):
        with tempfile.TemporaryDirectory() as directory:
            attempt = self.run_case(directory, "CA-PILOT-006")
            events = self.assert_complete(attempt, "CA-PILOT-006")
            requests = [r["request"] for r in jsonl(attempt.parent.parent / "private_model_io.jsonl") if r["kind"] == "request"]
            early_stages = {"pretrial_workspace", "preparation", "r1_motion", "r1_inquiry", "r1_ruling", "between_r1_r2", "r2_opening"}
            for request in requests:
                ctx = request["context"]
                if ctx.get("stage_id") not in early_stages:
                    continue
                if request["role_id"] in {"R_DEFENSE", "R_PROSECUTOR", "R_JUDGE"}:
                    self.assertNotIn("M06-P001", {r.get("citation_id") for r in ctx.get("disclosed_materials", [])})
                    self.assertNotIn("m06", {r.get("file_id") for r in ctx.get("workflow", {}).get("files", [])})
                if ctx.get("task") == "source_bound_answer" and request["role_id"] == "R_OFFICER":
                    self.assertNotIn("F10", {f["fact_id"] for f in ctx["role_card"]["knowledge_points"]})
            later = [r["context"] for r in requests if r["role_id"] == "R_OFFICER" and r["context"].get("task") == "source_bound_answer" and r["context"].get("stage_id") == "r2_examination"]
            self.assertTrue(later)
            self.assertTrue(any("F10" in {f["fact_id"] for f in c["role_card"]["knowledge_points"]} for c in later))
            releases = [e for e in events if e["kind"] == "stage_release" and e["meta"].get("material_id") == "M06"]
            self.assertEqual(len(releases), 1)
            self.assertEqual(releases[0]["stage_id"], "r2_examination")
            self.assertEqual(releases[0]["references"], ["M06-P001"])
            self.assertTrue(releases[0]["meta"]["validated_release"])
            self.assertEqual(releases[0]["meta"]["validated_event"], "stage_release:M06")

    def test_safe_boundary_interruption_replays_without_duplicate_reservations(self):
        class StopAfterPreparation(ThreeRoundEngine):
            def lawyer_turn(self, role):
                result = super().lawyer_turn(role)
                if self.stage == "preparation" and role == "R_DEFENSE":
                    raise KeyboardInterrupt("offline safe-boundary interruption")
                return result

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with self.assertRaises(KeyboardInterrupt):
                self.run_case(out / "resumed", "CA-PILOT-005", engine_factory=StopAfterPreparation)
            case_dir = out / "resumed/courts/gpt/CA-PILOT-005"
            before = jsonl(case_dir / "action_attempts.jsonl")
            before_reserves = [r for r in before if r["kind"] == "reserve"]
            self.assertTrue(before_reserves)
            resumed = self.run_case(out / "resumed", "CA-PILOT-005")
            clean = self.run_case(out / "clean", "CA-PILOT-005")
            self.assertEqual(resumed.name, "attempt-002")
            self.assert_complete(resumed, "CA-PILOT-005")
            self.assertEqual(jsonl(resumed / "author_events.jsonl"), jsonl(clean / "author_events.jsonl"))
            after = jsonl(case_dir / "action_attempts.jsonl")
            self.assertEqual(after[:len(before)], before)
            clean_rows = jsonl(clean.parent.parent / "action_attempts.jsonl")
            after_reserves = [r for r in after if r["kind"] == "reserve"]
            clean_reserves = [r for r in clean_rows if r["kind"] == "reserve"]
            self.assertEqual(len(after_reserves), len(clean_reserves))
            self.assertEqual(len({r["logical_decision"] for r in after_reserves}), len(after_reserves))
            journal_rows = jsonl(case_dir / "private_model_io.jsonl")
            self.assertEqual(len([r for r in journal_rows if r["kind"] == "response"]), len(clean_reserves))
            self.assertEqual(read(resumed / "summary.json")["model_stats"]["replayed_decisions"], len(before_reserves))


if __name__ == "__main__":
    unittest.main()
