"""Trusted, allowlisted loader for the CA pilot format.

The loader deliberately never opens authoring/ or evaluation/.  Raw attributes
are backend data; only explicit view methods are safe model payloads.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from typing import Any


class CaseData:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.manifest = self._json("manifest.json")
        self.case_id = self.manifest["case_id"]
        self.procedure = self._json("config/procedure.json")
        self.permissions = self._json("config/permissions.json")
        cards = self._json("role_private/roles.json")["roles"]
        self.roles = {card["role_id"]: card for card in cards}
        self.materials = self._jsonl("public/materials.jsonl")
        self.laws = self._jsonl("public/legal_sources.jsonl")
        self.stages = {stage["stage_id"]: stage for stage in self.procedure["stages"]}
        self._brief_text = self._read("public/case.md")

    def _read(self, relative: str) -> str:
        # Only constants above reach here.  Still reject symlink/path escapes.
        allowed = {
            "manifest.json", "config/procedure.json", "config/permissions.json",
            "role_private/roles.json", "public/materials.jsonl",
            "public/legal_sources.jsonl", "public/case.md",
        }
        if relative not in allowed:
            raise ValueError("File is not in the runtime data allowlist")
        candidate = self.path / relative
        resolved = candidate.resolve()
        if resolved != candidate or not resolved.is_relative_to(self.path):
            raise ValueError("Runtime inputs may not use symlinks or path escapes")
        return candidate.read_text(encoding="utf-8")

    def _json(self, relative: str) -> Any:
        return json.loads(self._read(relative))

    def _jsonl(self, relative: str) -> list[dict]:
        return [json.loads(line) for line in self._read(relative).splitlines() if line.strip()]

    def public_brief(self) -> dict:
        text = self._brief_text
        if self.case_id == "CA-PILOT-004":
            # View-only sanitation: do not mutate the source package or hashes.
            text = text.replace(
                "本题聚焦定罪前的三项指控。原案武器与严重伤害认定、一审刑罚、定罪后更换律师争议及上诉处置存入制作参考。律师可检查伤情证明缺口，但本题不新增来源未明确的加重条款，不执行量刑或定罪后更换律师程序。",
                "本题仅处理C01—C03，不包含量刑、加重条款和后续程序。相关制作参考不向庭审角色开放。律师可以检查现有伤情证明的缺口。",
            ).replace("A的定罪前个人说法", "A的个人说法")
        charge_fields = {
            "charge_id", "name", "original_offense_label", "original_statute",
            "legal_source_ids", "scope", "statute_mapping_status",
        }
        return {
            "case_id": self.case_id,
            "title": self.manifest.get("title", self.case_id),
            "charges": [{k: deepcopy(v) for k, v in c.items() if k in charge_fields}
                        for c in self.manifest.get("charges", [])],
            "legal_applicability_date": self.manifest.get("legal_applicability_date"),
            "retrieval_cutoff_date": self.manifest.get("retrieval_cutoff_date"),
            "task": text,
        }

    def role_card(self, role_id: str) -> dict:
        if role_id not in self.roles:
            raise ValueError("Unknown runtime role")
        card = self.roles[role_id]
        allowed = {
            "role_id", "display_name", "role_type", "personality", "identity",
            "knowledge_points", "unknown_or_unavailable", "unknown_response",
            "private_consultation_with_role_ids", "court_information_rule", "must_not_do",
        }
        result = {k: deepcopy(v) for k, v in card.items() if k in allowed}
        result["knowledge_points"] = [
            {k: deepcopy(v) for k, v in point.items() if k in {
                "fact_id", "content", "knowledge_basis", "disclosure_condition",
                "must_preserve_attribution",
            }} for point in card.get("knowledge_points", [])
        ]
        # Historical testimony metadata and original-case source locator IDs
        # are provenance, not facts needed to answer as the current character.
        if isinstance(result.get("identity"), dict):
            result["identity"].pop("basis_source_id", None)
        result["unknown_or_unavailable"] = [
            s.replace("本人定罪前的陈述与证言", "本人的陈述与证言")
            for s in result.get("unknown_or_unavailable", [])
        ]
        return result

    def disclosed_initial(self) -> set[str]:
        return {
            row["material_id"] for row in self.materials
            if row.get("available_from_stage", "preparation") == "preparation"
            and row.get("evidence_status") != "not_yet_disclosed"
            and any(rule.get("material_id") == row["material_id"]
                    and not rule.get("required_event")
                    and rule.get("minimum_stage", "preparation") == "preparation"
                    for rule in self.permissions.get("material_rules", []))
        }

    def material_paragraphs(self, material_id: str) -> list[dict]:
        """Trusted service method. Caller must check disclosure/role/stage first."""
        fields = {
            "material_id", "paragraph_id", "title", "text", "language",
            "fact_ids", "epistemic_status", "material_type", "evidence_status",
            "available_from_stage", "published_at", "publication_date",
        }
        return [dict(
            {k: deepcopy(v) for k, v in row.items() if k in fields},
            citation_id=row["paragraph_id"], source_kind="case",
            temporal_status="frozen_case_pack",
        ) for row in self.materials if row["material_id"] == material_id and self.publication_permitted(row)]

    def publication_permitted(self, row: dict) -> bool:
        """Known future/invalid dates cannot bypass search via direct reading."""
        publication = row.get("publication_date") or row.get("published_at")
        if not publication:
            return True  # Explicit frozen-case-pack policy, not historical verification.
        try:
            return date.fromisoformat(str(publication)[:10]) <= date.fromisoformat(self.manifest["retrieval_cutoff_date"])
        except (ValueError, KeyError):
            return False

    def legal_documents(self) -> list[dict]:
        """Flatten only the supplied public legal excerpts, never source files."""
        documents = []
        for law in self.laws:
            if not self.publication_permitted(law):
                continue
            for paragraph in law.get("paragraphs", []):
                documents.append({
                    "citation_id": paragraph["paragraph_id"],
                    "legal_source_id": law["legal_source_id"],
                    "title": law.get("section", law["legal_source_id"]),
                    "text": paragraph["text"],
                    "authority_type": law.get("authority_type"),
                    "official_url": law.get("official_url"),
                    "version": law.get("version"),
                    "version_status": law.get("version_status"),
                    "applies_as_of": law.get("applies_as_of"),
                    "published_at": law.get("published_at"),
                    "publication_date": law.get("publication_date"),
                    "source_kind": "law",
                    "temporal_status": "frozen_case_pack",
                    "law_applicability_unverified": True,
                })
        return documents

    def validate(self) -> list[str]:
        """Structural runtime errors only; [] is not expert legal validation."""
        errors = []
        if self.permissions.get("default") != "deny":
            errors.append("permissions.default must be deny")
        if self.permissions.get("agents_have_external_search_access") is not False:
            errors.append("External search must be disabled in the case permissions")
        for obj in (self.procedure, self.permissions):
            if obj.get("case_id") != self.case_id:
                errors.append("Case IDs disagree across runtime inputs")
        initial = self.procedure.get("initial_stage_id")
        if initial not in self.stages:
            errors.append("Initial stage is missing")
        for stage in self.stages.values():
            if stage.get("next_stage_id") and stage["next_stage_id"] not in self.stages:
                errors.append(f"Unknown next stage: {stage['stage_id']}")
        ids = [x.get("paragraph_id") for x in self.materials]
        ids += [x.get("citation_id") for x in self.legal_documents()]
        if not all(ids) or len(ids) != len(set(ids)):
            errors.append("Missing or duplicate paragraph/citation IDs")
        material_ids = {m.get("material_id") for m in self.materials}
        for rule in self.permissions.get("material_rules", []):
            if rule.get("material_id") not in material_ids:
                errors.append("Material ACL names a missing material")
            if rule.get("minimum_stage") not in self.stages:
                errors.append("Material ACL names a missing stage")
        if set(self.manifest.get("role_ids", [])) != set(self.roles):
            errors.append("Role manifest and role cards disagree")
        return errors
