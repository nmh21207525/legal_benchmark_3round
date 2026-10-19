#!/usr/bin/env python3
"""Freeze a rubric from a verified development run or explicit pre-run authoring."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "runtime"))
import backends
backends.bootstrap()
import runner
from three_assets import load_assets
from three_scoring import FREEZE_BASES, freeze_rubric


def verified_development_run(a):
    """The original path still requires a completed live three-round trial."""
    source = a.development_out.expanduser().resolve()
    manifest_path = source / "matrix_manifest.json"
    manifest = runner.read(manifest_path)
    if manifest.get("mode") != "development" or manifest.get("mock_only") is not False:
        raise runner.RunError("live_development_trial_required", "离线演示不能作为真实开发试跑或正式冻结依据。")
    if a.case_id not in manifest.get("case_ids", []) or a.candidate not in manifest.get("candidates", []):
        raise runner.RunError("development_selection_mismatch")
    attempt = runner.verify_complete(source / "courts" / a.candidate / a.case_id, runner.digest(manifest_path))
    if attempt is None:
        raise runner.RunError("development_court_not_complete")
    summary = runner.read(attempt / "summary.json")
    if summary.get("mode") != "live" or summary.get("current_stage") != "ended" or not summary.get("verdict"):
        raise runner.RunError("live_three_round_court_not_complete")
    for relative, expected in manifest.get("asset_sha256", {}).items():
        if a.case_id in Path(relative).parts:
            path = a.data / relative
            if not path.is_file() or runner.digest(path) != expected:
                raise runner.RunError("development_case_inputs_changed", relative)
    for relative, expected in manifest.get("code_sha256", {}).items():
        if relative.startswith("runtime/"):
            path = ROOT / relative
            if not path.is_file() or runner.digest(path) != expected:
                raise runner.RunError("development_runtime_changed", "运行代码已改变，需要在当前版本重新做开发试跑。")
    return runner.digest(manifest_path) + ":" + a.candidate + ":" + a.case_id


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--case-id", choices=("CA-PILOT-005", "CA-PILOT-006"), required=True)
    p.add_argument("--candidate", choices=backends.CANDIDATES, default="gpt")
    p.add_argument("--draft", type=Path, required=True)
    p.add_argument("--basis", choices=FREEZE_BASES, default="development_reviewed",
                   help="默认验证真实开发试跑；pre_run_authoring 明确采用用户授权、未经独立人工复核的预先标准。")
    p.add_argument("--development-out", type=Path)
    p.add_argument("--data", type=Path, default=ROOT / "data")
    p.add_argument("--output", type=Path)
    a = p.parse_args(argv)
    if a.basis == "development_reviewed" and a.development_out is None:
        p.error("development_reviewed 必须提供 --development-out；仅提前定稿时显式使用 --basis pre_run_authoring。")
    if a.basis == "pre_run_authoring" and a.development_out is not None:
        p.error("pre_run_authoring 不接受 --development-out，避免混淆预先定稿和试跑后定稿。")
    assets = load_assets(a.data)
    asset = assets[a.case_id]
    runs = [verified_development_run(a)] if a.basis == "development_reviewed" else []
    output = a.output or ROOT / "rubrics" / (a.case_id + ".json")
    frozen = freeze_rubric(asset, a.draft, output, development_run_ids=runs, basis=a.basis)
    print(json.dumps({"status": "frozen", "case_id": a.case_id, "point_count": frozen["point_count"],
                      "maximum_points": frozen["maximum_points"], "output": str(output.resolve()),
                      "freeze_basis": frozen.get("freeze_basis", "development_reviewed"),
                      "authoring_provenance": frozen.get("authoring_provenance"),
                      "paid_model_calls": 0}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "stopped", "error_code": getattr(exc, "code", type(exc).__name__),
                          "detail": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
