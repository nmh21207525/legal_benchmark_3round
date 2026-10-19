"""Offline checks of delivery integrity, privacy and saved interactions."""
import csv
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import unquote, urlsplit
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from three_delivery import DeliveryError, publish, replay_html


class Hrefs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.ids = [], set()
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "href" in a:
            self.links.append(a["href"])
        if "id" in a:
            self.ids.add(a["id"])


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value, encoding="utf-8")


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / "package"
        self.out = self.root / "out"
        self.case = self.package / "data/cases/CA-PILOT-005"
        self.workflow = self.package / "data/workflows/CA-PILOT-005"
        write(self.package / "start.sh", "#!/bin/sh\nexit 0\n")
        write(self.package / "runtime/requirements.txt", "httpx\n")
        write(self.package / "runtime/config.example.json", {"api": {"key_env": "PACKY_API_KEY"}})
        write(self.package / ".env", "PACKY_API_KEY=hidden-live-key")
        write(self.package / "config.local.json", {"api_key": "hidden-live-key"})
        write(self.package / "runs/old/terminal.log", "secret-old-output")
        write(self.case / "manifest.json", {"case_id": "CA-PILOT-005", "simulation_round_count": 3})
        write(self.case / "public/case.md", "三轮题目")
        write(self.case / "public/materials.jsonl", '{"paragraph_id":"M01-P001","text":"案卷"}\n')
        write(self.case / "config/procedure.json", {"stage_order": ["preparation", "r3_verdict", "ended"]})
        write(self.case / "role_private/roles.json", {"private": "only teachers"})
        write(self.workflow / "task.md", "阅读、咨询、调查、产出文件并参与三轮庭审。")
        write(self.workflow / "inputs/M01.md", "案卷内容")
        self.asset = {"case_id": "CA-PILOT-005", "case_path": self.case, "workflow_root": self.workflow, "workflow_spec": {"files": [{"path": "inputs/M01.md", "file_id": "M01", "title": "第一份材料"}]}, "rubric": {"status": "pending_development_trial", "items": [], "approved": False}}
        self.rows = [{"candidate": "gpt", "model": "gpt-5.6-sol", "case_id": "CA-PILOT-005", "status": "pending", "numerator": None, "denominator": None, "rubric_coverage_percent": None, "court_dir": None}]

    def publish(self, **kwargs):
        return publish(self.out, self.rows, {"CA-PILOT-005": self.asset}, ["CA-PILOT-005"], mode=kwargs.pop("mode", "development"), package_root=self.package, **kwargs)

    def completed(self):
        self.run = self.out / "courts/gpt/CA-PILOT-005"
        self.attempt = self.run / "court_attempts/attempt-001"
        self.events = [
            {"event_id": "E00001", "stage_id": "preparation", "kind": "private_consultation", "actor": "R_DEFENDANT", "audience": ["R_DEFENSE", "R_DEFENDANT"], "text": "我只能依据来源回答。", "meta": {"question": "请确认此前是否收到通知？<script>alert(1)</script>"}},
            {"event_id": "E00002", "stage_id": "pretrial_workspace", "kind": "tool_call", "actor": "R_DEFENSE", "audience": ["R_DEFENSE"], "text": "读取材料", "meta": {"tool": "read_document", "arguments": {"target": "M01"}}},
            {"event_id": "E00003", "stage_id": "pretrial_workspace", "kind": "tool_result", "actor": "R_DEFENSE", "audience": ["R_DEFENSE"], "text": "", "meta": {"tool": "read_document", "call_event_id": "E00002", "result": {"ok": True, "text": "案卷内容"}}},
            {"event_id": "E00004", "stage_id": "r3_verdict", "kind": "verdict", "actor": "R_JURY", "audience": ["R_DEFENSE", "R_PROSECUTOR", "R_JURY"], "text": "裁决内容", "meta": {}}
        ]
        write(self.attempt / "author_events.jsonl", ''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in self.events))
        write(self.attempt / "summary.json", {"status": "completed_development_run", "current_stage": "ended", "mode": "live"})
        artifact = self.attempt / "artifacts/R_DEFENSE/preparation/v0001.md"
        write(artifact, "实际文件，不是网页编造。")
        self.artifact_row = {"artifact_id": "A00001", "path": "R_DEFENSE/preparation/v0001.md", "name": "准备.md", "actor": "R_DEFENSE", "version": 1, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
        write(self.attempt / "artifacts/manifest.json", {"artifacts": [self.artifact_row]})
        self.rebind()
        self.rows[0].update(status="development_complete", court_dir=self.attempt.relative_to(self.out).as_posix())

    def rebind(self):
        hashes = {p.relative_to(self.run).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.attempt.rglob("*") if p.is_file()}
        write(self.run / "court_complete.json", {"attempt": "court_attempts/attempt-001", "files_sha256": hashes})

    def test_initial_dashboard_pending_no_scores(self):
        report = self.publish()
        self.assertEqual(report["status"], "incomplete")
        self.assertFalse(report["teacher_packages_created"])
        self.assertEqual(report["rubric_pending_cases"], ["CA-PILOT-005"])
        self.assertFalse(list(self.out.glob("*.zip")))
        with (self.out / "results.csv").open() as f:
            row = next(csv.DictReader(f))
        self.assertEqual(row["numerator"], "")
        self.assertEqual(row["denominator"], "")
        self.assertTrue((self.out / "index.html").is_file())
        env = self.out / "交付材料/第005题/04_运行环境"
        self.assertTrue((env / "start.sh").exists())
        self.assertFalse((env / ".env").exists())
        self.assertFalse((env / "config.local.json").exists())
        self.assertFalse((env / "runs").exists())
        self.check_links()

    def test_saved_consultation_tool_links_and_privacy(self):
        self.completed()
        report = self.publish()
        self.assertEqual(report["status"], "development_complete")
        self.assertEqual(report["complete_courts"], 1)
        root = self.out / "交付材料/第005题/03_实验结果/GPT"
        html = (root / "对话回放.html").read_text()
        self.assertIn("辩方律师提问（原记录）", html)
        self.assertIn("我只能依据来源回答。", html)
        self.assertIn('href="#E00003"', html)
        self.assertIn("第三轮 · 裁决", html)
        self.assertIn('id="search"', html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        prosecutor = (root / "角色视图/R_PROSECUTOR.html").read_text()
        self.assertNotIn("此前是否收到通知", prosecutor)
        self.assertNotIn("案卷内容", prosecutor)
        self.assertIn("裁决内容", prosecutor)
        teacher = (root / "角色视图/教师完整视图.html").read_text()
        self.assertIn("此前是否收到通知", teacher)
        self.assertEqual((root / "原始记录/court_attempts/attempt-001/author_events.jsonl").read_bytes(), (self.attempt / "author_events.jsonl").read_bytes())
        self.check_links()

    def test_repeated_publish_preserves_every_original_run_byte(self):
        self.completed()
        def snapshot():
            return {p.relative_to(self.run).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.run.rglob("*") if p.is_file()}
        original = snapshot()
        for _ in range(3):
            self.publish()
            self.assertEqual(snapshot(), original)
        self.rows.append({"candidate": "seed", "model": "doubao-seed-evolving",
                          "case_id": "CA-PILOT-005", "status": "pending"})
        self.publish()
        self.assertEqual(snapshot(), original)

    def test_completed_hash_tamper_stops_export(self):
        self.completed()
        write(self.attempt / "author_events.jsonl", "")
        with self.assertRaisesRegex(DeliveryError, "completed_run_hash_mismatch"):
            self.publish()

    def test_artifact_path_escape_rejected(self):
        self.completed()
        self.artifact_row["path"] = "../../outside.txt"
        write(self.attempt / "artifacts/manifest.json", {"artifacts": [self.artifact_row]})
        self.rebind()
        with self.assertRaisesRegex(DeliveryError, "unsafe_relative_path"):
            self.publish()

    def test_formal_requires_saved_score_and_frozen_rubric(self):
        self.completed()
        self.rows[0].update(status="complete", numerator=1, denominator=2, rubric_coverage_percent=50.0)
        not_ready = self.publish(mode="formal")
        self.assertFalse(not_ready["teacher_packages_created"])
        self.asset["rubric"] = {"case_id": "CA-PILOT-005", "approved": True, "frozen": True, "status": "frozen", "protocol": "three-round-frozen-rubric-v1", "freeze_sha256": "abc", "points": [{"point_id": "P1", "score_anchors": {"0": "无", "1": "部分", "2": "完整"}}]}
        write(self.run / "direct_score/score.json", {"status": "complete", "points": [{"point_id": "P1", "score": 1, "reason": "部分完成"}], "numerator": 1, "denominator": 2, "rubric_freeze_sha256": "abc"})
        score_path = self.run / "direct_score/score.json"
        write(self.run / "direct_score/score_complete.json", {"files_sha256": {"score.json": hashlib.sha256(score_path.read_bytes()).hexdigest()}})
        report = self.publish(mode="formal")
        self.assertTrue(report["teacher_packages_created"])
        self.assertEqual(report["status"], "complete")
        self.assertEqual(len(report["packages"]), 2)
        with zipfile.ZipFile(self.out / "teacher-delivery-three-rounds.zip") as z:
            self.assertTrue(any(x.endswith("角色视图/教师完整视图.html") for x in z.namelist()))
            self.assertFalse(any(".env" in x or "config.local" in x for x in z.namelist()))
        self.check_links()

    def test_mock_never_claims_formal_completion(self):
        self.completed()
        self.rows[0].update(status="complete", numerator=1, denominator=2)
        write(self.run / "direct_score/score.json", {"status": "complete", "mock_only": True, "points": []})
        report = self.publish(mode="formal", mock=True)
        self.assertFalse(report["teacher_packages_created"])
        self.assertNotEqual(report["status"], "complete")
        self.assertIn("MOCK", (self.out / "index.html").read_text())

    def test_safe_token_policy_metadata_is_exported(self):
        write(self.out / "matrix_manifest.json", {"policy": {"output_token_cap_enforced": False,
            "max_output_tokens": 16384, "http_lawyer_max_output_tokens": 16384,
            "opus_output_token_cap_enforced": False}, "config": {"api": {"key_env": "PACKY_API_KEY"}}})
        self.publish()
        self.assertTrue((self.out / "交付材料/第005题/05_来源与校验/matrix_manifest.json").is_file())
        write(self.out / "matrix_manifest.json", {"config": {"api_key": "hidden"}})
        with self.assertRaisesRegex(DeliveryError, "embedded_credentials"):
            self.publish()

    def test_environment_embedded_secret_refused(self):
        write(self.package / "runtime/config.example.json", {"api": {"api_key": "abc"}})
        with self.assertRaises(ValueError):
            self.publish()

    def test_missing_consultation_question_not_invented(self):
        html = replay_html("CA-PILOT-005", "gpt", [{"event_id": "E1", "stage_id": "preparation", "kind": "private_consultation", "actor": "R_DEFENDANT", "text": "保存的回答", "meta": {}, "audience": []}])
        self.assertNotIn("辩方律师提问（原记录）", html)
        self.assertIn("保存的回答", html)

    def check_links(self):
        for file in self.out.rglob("*.html"):
            parser = Hrefs()
            parser.feed(file.read_text(encoding="utf-8"))
            for href in parser.links:
                url = urlsplit(href)
                if url.scheme or url.netloc:
                    continue
                target = file.parent / unquote(url.path) if url.path else file
                self.assertTrue(target.is_file(), (file, href))
                if url.fragment:
                    other = Hrefs()
                    other.feed(target.read_text(encoding="utf-8"))
                    self.assertIn(unquote(url.fragment), other.ids, (file, href))


if __name__ == "__main__":
    unittest.main()
