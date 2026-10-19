"""Validated loader for the independent v3 data, with no old rubric assumptions.

This module is trusted orchestration code. Never pass whole assets to a lawyer:
only WorkflowCaseData's scoped views are intended for trial-model contexts.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path

CASE_IDS = ("CA-PILOT-003", "CA-PILOT-004")
SCOPES = {"pretrial", "investigation", "artifact", "trial"}


class AssetError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _safe(root, relative):
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise AssetError("asset_path_escape", str(relative))
    candidate = root / rel
    if not candidate.is_file() or candidate.is_symlink() or candidate.resolve() != candidate:
        raise AssetError("asset_missing_or_symlink", str(relative))
    return candidate


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_data_manifest(data_root):
    root = Path(data_root).resolve()
    # A caller may supply the package directory rather than its data child.
    if not (root / "assets_manifest.json").is_file() and (root / "data/assets_manifest.json").is_file():
        root = root / "data"
    manifest = _read(_safe(root, "assets_manifest.json"))
    if manifest.get("protocol") != "full-workflow-assets-v3" or manifest.get("case_ids") != list(CASE_IDS):
        raise AssetError("wrong_asset_manifest", "Expected the new two-case full-workflow assets")
    entries = manifest.get("sha256", {})
    if not entries:
        raise AssetError("empty_asset_manifest", str(root))
    actual = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and p.name != "assets_manifest.json"}
    if actual != set(entries):
        raise AssetError("asset_inventory_changed", str(sorted(actual ^ set(entries))))
    for rel, expected in entries.items():
        if _sha(_safe(root, rel)) != expected:
            raise AssetError("asset_hash_mismatch", rel)
    return root, manifest


def _validate_sources(case_path, materials, laws, source_map):
    """Verify selected public texts and all bundled original hash/anchor chains."""
    index = _read(case_path / "authoring/sources/index.json")
    for entry in index["files"]:
        path = _safe(case_path, entry["path"])
        if _sha(path) != entry["sha256"]:
            raise AssetError("original_source_hash_mismatch", entry["path"])
    original_paragraphs = {}
    for path in (case_path / "authoring/sources").glob("source*/opinions/*/paragraphs.json"):
        for row in _read(path):
            original_paragraphs[row["locator"]] = row["text_verbatim"]
    mappings = _read(case_path / "authoring/provenance.json")["fact_mappings"]
    anchors = 0
    for mapping in mappings:
        for span in mapping.get("source_spans", []):
            loc, quote = span["locator"], span["text_verbatim"]
            if loc not in original_paragraphs or quote != original_paragraphs[loc]:
                raise AssetError("provenance_anchor_mismatch", loc)
            anchors += 1
    for row in materials:
        sid = row["paragraph_id"]
        if sid not in source_map or source_map[sid].get("original_locators") != row.get("source_locators"):
            raise AssetError("material_source_mapping_mismatch", sid)
        # View-only neutralization of an outcome-implying temporal phrase; it
        # never changes the unchanged source bytes or underlying factual claim.
        expected_text = row["text"].replace("A的定罪前个人说法", "A的个人说法").replace("A在定罪前的个人陈述", "A的个人陈述")
        if source_map[sid].get("text") != expected_text:
            raise AssetError("material_source_text_mismatch", sid)
        if any(loc not in original_paragraphs for loc in row.get("source_locators", [])):
            raise AssetError("missing_original_paragraph", sid)
    for law in laws:
        sid = law["legal_source_id"]
        expected = "\n\n".join(p["paragraph_id"] + "\n" + p["text"] for p in law["paragraphs"])
        if source_map.get(sid, {}).get("text") != expected:
            raise AssetError("law_excerpt_mismatch", sid)
    return {"source_files": len(index["files"]), "verbatim_provenance_anchors": anchors,
            "status": "passed", "semantic_expert_review": False}


def load_assets(data_root=None):
    """Return {case_id: asset}; paths are absolute and protocol hashes verified."""
    root, manifest = validate_data_manifest(data_root or Path(__file__).resolve().parent / "data")
    result = {}
    for cid in CASE_IDS:
        case = root / "cases" / cid
        work = root / "workflows" / cid
        spec, rubric, source_map, authoring = [_read(_safe(work, f)) for f in
            ("workflow.json", "rubric.json", "source_map.json", "authoring_record.json")]
        if spec.get("case_id") != cid or rubric.get("case_id") != cid or authoring.get("case_id") != cid:
            raise AssetError("case_id_mismatch", cid)
        if rubric.get("version") != "full-workflow-rubric-v3":
            raise AssetError("wrong_rubric_version", cid)
        points = rubric.get("points", [])
        ids = [p.get("point_id") for p in points]
        if not points or len(ids) != len(set(ids)) or set(p.get("scope") for p in points) != SCOPES:
            raise AssetError("invalid_point_inventory", cid)
        if rubric.get("maximum_points") != 2*len(points) or rubric.get("point_count") != len(points):
            raise AssetError("invalid_rubric_denominator", cid)
        for p in points:
            if p.get("weight") != 1 or set(p.get("score_anchors", {})) != {"0", "1", "2"}:
                raise AssetError("invalid_score_anchors", p["point_id"])
            if not p.get("source_ids") or not set(p["source_ids"]) <= set(source_map):
                raise AssetError("rubric_missing_source", p["point_id"])
            if p["scope"] == "artifact" and not p.get("artifact_requirements"):
                raise AssetError("artifact_requires_real_file", p["point_id"])
        materials = [json.loads(l) for l in (case / "public/materials.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        laws = [json.loads(l) for l in (case / "public/legal_sources.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        private_refs = {m["paragraph_id"] for m in materials if m.get("evidence_status") == "not_yet_disclosed"}
        for doc in spec.get("files", []):
            path = _safe(work, doc["path"])
            if _sha(path) != doc.get("sha256"):
                raise AssetError("input_document_hash_mismatch", doc["file_id"])
            if set(doc.get("source_refs", [])) & private_refs:
                raise AssetError("premature_document_disclosure", doc["file_id"])
        for service in spec.get("services", []):
            if not set(service.get("source_refs", [])) <= set(source_map):
                raise AssetError("investigation_source_missing", service["service_id"])
            if set(service.get("source_refs", [])) & private_refs:
                raise AssetError("premature_investigation_disclosure", service["service_id"])
            if service.get("new_historical_facts") is not False or service.get("network_used") is not False:
                raise AssetError("unsupported_investigation_claim", service["service_id"])
        if authoring.get("rubric_sha256") != _sha(work / "rubric.json"):
            raise AssetError("rubric_authoring_binding_mismatch", cid)
        if spec.get("task") != (work / "task.md").read_text(encoding="utf-8") or source_map.get("ENV-TASK", {}).get("text") != spec.get("task"):
            raise AssetError("workflow_task_source_mismatch", cid)
        source_hashes = authoring.get("source_files_sha256", {})
        if set(authoring.get("allowed_inputs", [])) != set(source_hashes) or not source_hashes:
            raise AssetError("authoring_source_inventory_mismatch", cid)
        for relative, expected_hash in source_hashes.items():
            if _sha(_safe(case, relative)) != expected_hash:
                raise AssetError("authoring_source_hash_mismatch", relative)
        expected_sids = {"ENV-TASK"} | {m["paragraph_id"] for m in materials} | {l["legal_source_id"] for l in laws}
        if set(source_map) != expected_sids:
            raise AssetError("unexpected_scoring_source", cid)
        if not {"old_rollouts", "old_AI_analysis", "old_rubrics", "old_scores"} <= set(authoring.get("excluded_from_author_context", [])):
            raise AssetError("authoring_isolation_missing", cid)
        source_audit = _validate_sources(case, materials, laws, source_map)
        result[cid] = {
            "case_id": cid, "case_path": case, "workflow_root": work, "workflow_spec": spec,
            "rubric": rubric, "source_map": source_map, "authoring_record": authoring,
            "author": {"case_id": cid, "protocol": "full-workflow-author-input-v3", "source_units": list(source_map.values()),
                       "allowed_inputs": authoring["allowed_inputs"], "excluded_inputs": authoring["excluded_from_author_context"]},
            "evaluation_aliases": {"case_id": cid, "person_aliases": {}, "note": "Scoring uses public A/B/C/D aliases only."},
            "source_audit": source_audit,
        }
    return result


if __name__ == "__main__":
    assets = load_assets()
    print(json.dumps({"status": "validated", "paid_api_calls": 0, "cases": {
        cid: {"points": len(a["rubric"]["points"]), "maximum_points": a["rubric"]["maximum_points"],
              "scopes": dict(Counter(p["scope"] for p in a["rubric"]["points"])), "source_audit": a["source_audit"]}
        for cid,a in assets.items()}}, ensure_ascii=False, indent=2))
