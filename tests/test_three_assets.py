"""No network or model calls: source integrity, delayed files and empty-rubric gates."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "runtime", ROOT / "runtime/vendor", ROOT / "runtime/vendor/runtime"):
    sys.path.insert(0, str(path))
from three_assets import (ARTIFACTS, AssetError, CASE_IDS, MANIFEST_NAME,
                          ThreeRoundCaseData, load_assets)


class ThreeAssetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = load_assets(ROOT)

    def test_original_case_bytes_match_submitted_archive(self):
        for cid in CASE_IDS:
            with self.subTest(case=cid), zipfile.ZipFile(ROOT / "source_packages/主数据包" / (cid + ".zip")) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    target = ROOT / "data/cases" / info.filename
                    self.assertEqual(target.read_bytes(), archive.read(info.filename), info.filename)

    def test_three_round_cases_keep_same_file_contract_without_fake_rubric(self):
        for cid, asset in self.assets.items():
            with self.subTest(case=cid):
                case = ThreeRoundCaseData(asset["case_path"])
                self.assertEqual(case.validate(), [])
                self.assertEqual(asset["workflow_spec"]["required_artifacts"], ARTIFACTS)
                self.assertEqual(asset["workflow_spec"]["pretrial_actions"], 20)
                self.assertEqual(asset["workflow_spec"]["version"], "source-bound-full-workflow-v3")
                self.assertEqual(asset["rubric"]["points"], [])
                self.assertEqual(asset["rubric"]["items"], [])
                self.assertIsNone(asset["rubric"]["maximum_points"])
                self.assertFalse(asset["rubric"]["approved"])
                self.assertEqual(asset["source_audit"]["status"], "passed")

    def test_runtime_case_loader_does_not_read_authoring_or_evaluation(self):
        seen = []
        original = Path.read_text
        def track(path, *args, **kwargs):
            seen.append(str(path))
            return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", track):
            case = ThreeRoundCaseData(self.assets["CA-PILOT-006"]["case_path"])
            case.public_brief()
            case.role_card("R_OFFICER")
        self.assertTrue(seen)
        self.assertFalse(any("/authoring/" in p or "/evaluation/" in p for p in seen))

    def test_sixth_material_gate_is_preserved_in_files_and_role_knowledge(self):
        asset = self.assets["CA-PILOT-006"]
        case = ThreeRoundCaseData(asset["case_path"])
        self.assertNotIn("M06", case.disclosed_initial())
        card = case.role_card("R_OFFICER")
        gated = next(p for p in card["knowledge_points"] if p["fact_id"] == "F10")
        self.assertEqual(gated["available_from_stage"], "r2_examination")
        self.assertEqual(gated["required_event"], "stage_release:M06")
        self.assertEqual(gated["gated_until"]["material_id"], "M06")
        doc = next(f for f in asset["workflow_spec"]["files"] if f["file_id"] == "m06")
        self.assertEqual(doc["material_ids"], ["M06"])
        self.assertEqual(doc["available_from_stage"], "r2_examination")
        inventory = (asset["workflow_root"] / "files/document_inventory.csv").read_text()
        self.assertNotIn("m06", inventory.lower())
        for service in asset["workflow_spec"]["services"]:
            self.assertNotIn("M06-P001", service["source_refs"])
            self.assertFalse(service["network_used"])
            self.assertFalse(service["new_historical_facts"])
        # Original case has no independent toxicologist testimony.
        self.assertEqual(case.procedure["witnesses"]["R_TOXICOLOGIST"]["mode"], "record_only")

    def test_partial_defendant_knowledge_does_not_gain_internal_records(self):
        case = ThreeRoundCaseData(self.assets["CA-PILOT-005"]["case_path"])
        card = case.role_card("R_DEFENDANT")
        points = {p["fact_id"]: p for p in card["knowledge_points"]}
        self.assertNotIn('“Yes”', points["F02"]["content"])
        self.assertIn("不取得B在接收表上的内部标记", points["F02"]["content"])
        self.assertIn("不取得C档案中的内部注记", points["F04"]["content"])
        for point in card["knowledge_points"]:
            self.assertNotIn("source_locators", point)
        self.assertNotIn("source_id", card["identity"])

    def test_runtime_detects_source_and_workflow_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "data"
            shutil.copytree(ROOT / "data", dest)
            target = dest / "workflows/CA-PILOT-006/files/m06.md"
            target.write_text(target.read_text() + "\nchanged\n")
            with self.assertRaisesRegex(AssetError, "asset_hash_mismatch"):
                load_assets(dest)
            # Loader must never silently rebuild and accept edited records.
            manifest_before = (dest / MANIFEST_NAME).read_bytes()
            with self.assertRaises(AssetError):
                load_assets(dest)
            self.assertEqual((dest / MANIFEST_NAME).read_bytes(), manifest_before)


if __name__ == "__main__":
    unittest.main()
