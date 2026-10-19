"""Source-checked 005/006 inputs and the unchanged full-workflow file contract.

Only explicit CaseData views belong in actor requests. Assets, source maps and
source-audit records are trusted author/scorer inputs, never a lawyer payload.
The supplied case packs remain byte-for-byte unchanged. Generated workflows add
only the same file/search/investigation interface used by the two-round runner;
no new historical facts and no provisional scores are invented here.
"""
from __future__ import annotations

from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path

from court_agent.data import CaseData
from workflow import VERSION, PRETRIAL, validate_workflow

CASE_IDS = ("CA-PILOT-005", "CA-PILOT-006")
ASSETS_VERSION = "source-bound-three-round-assets-v1"
ENVIRONMENT_VERSION = "source-bound-three-round-environment-v1"
ARTIFACTS = ["case_timeline.csv", "evidence_register.csv", "pretrial_plan.md", "examination_outline.md"]
MANIFEST_NAME = "three_assets_manifest.json"
RUNTIME_INPUTS = ["manifest.json", "public/case.md", "public/materials.jsonl", "public/legal_sources.jsonl",
                  "config/procedure.json", "config/permissions.json", "role_private/roles.json"]


class AssetError(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(f"{code}: {detail}")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def safe(root, relative):
    root = Path(root).resolve()
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise AssetError("asset_path_escape", str(relative))
    rel = Path(relative)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise AssetError("asset_path_escape", relative)
    current = root
    for part in rel.parts:
        current /= part
        if current.is_symlink():
            raise AssetError("asset_symlink", relative)
    if not current.is_file() or current.resolve() != current:
        raise AssetError("asset_missing_or_symlink", relative)
    return current


def data_path(root=None):
    root = Path(root or Path(__file__).resolve().parents[1] / "data").resolve()
    return root / "data" if (root / "data/cases").is_dir() else root


COMMON_CONTRACT = """本题沿用两轮实验的来源约束模拟规则；本次程序按题目配置完成三轮。
授权事实段落是所标明来源陈述的呈现载体，并非原始文件或逐字证言。双方仅认可该摘要载体及其来源归属，不承认所有陈述为真，也不自动完成犯罪要件或证据准入。
来源明确归属于某人的亲历、报告和意见，只能在该角色已有知情边界内回答；原文中的转述、争议和未知必须保留。记录角色不能生成新的实时证言。
缺少原始报告、笔录或完整资历文件，不等于历史上不存在、违法或伪造；同时不能编造缺失的实质基础。调查和检索只能返回所给冻结资料中的内容。
披露不等于采纳；第一轮讨论不得自动进入陪审团。第二轮有效证据保留到第三轮，后续排除、限制和划除继续生效。律师计划和工具报告不是新的历史证据。
被告自愿决定是否作证，私下咨询不自动成为公开证据，沉默不得作为有罪依据。本次裁决只依据获准证据与本次法律指示，不复现原案结局。"""

CASE_CONTRACTS = {
    "CA-PILOT-005": "M01—M08初始已披露但未采纳。B、C与A的知情范围按角色卡限制；A关于旅馆和登记窗口的说法保持本人主张，不补造亲属、窗口人员或其他证人。R_SOURCE_RECORD只能呈现获准段落，不是一名新增事实证人。",
    "CA-PILOT-006": "M06仅由可信程序在指定第二轮阶段完成开放事件后同步披露；角色知识同样受该事件限制。本约定不提前提供其内容，也不允许倒改第一轮裁定。毒理结论角色仅为记录角色，不生成实验室现场问答；R_SOURCE_RECORD只能呈现获准段落。",
}


class ThreeRoundCaseData(CaseData):
    """Runtime allowlisted case views; source originals are verified separately."""
    def __init__(self, path):
        super().__init__(path)
        if self.case_id not in CASE_IDS:
            raise AssetError("unsupported_three_round_case", self.case_id)
        self.runtime_input_sha256 = {name: sha(safe(self.path, name)) for name in RUNTIME_INPUTS}
        errors = self.validate()
        if errors:
            raise AssetError("invalid_three_round_case", "; ".join(errors))

    def validate(self):
        errors = super().validate()
        if self.manifest.get("simulation_round_count") != 3:
            errors.append("Three-round manifest required")
        rounds = {s.get("simulation_round") for s in self.stages.values()} - {None, 0}
        if rounds != {1, 2, 3}:
            errors.append("Procedure must include rounds 1, 2 and 3")
        if self.procedure.get("jury_evidence_rounds") != [2, 3]:
            errors.append("Jury evidence must retain rounds 2 and 3")
        instructions = self.procedure.get("jury_instruction_stage_id")
        if instructions not in self.stages or self.stages[instructions].get("simulation_round") != 3:
            errors.append("Jury instructions must occur in round 3")
        if self.permissions.get("stage_order") != list(self.stages):
            errors.append("Procedure and ACL stage order disagree")
        visited, current = [], self.procedure.get("initial_stage_id")
        while current in self.stages and current not in visited:
            visited.append(current)
            current = self.stages[current].get("next_stage_id")
        if current is not None or visited != list(self.stages) or not visited or visited[-1] != "ended":
            errors.append("Stage chain must reach ended exactly once")
        paragraphs = {m["paragraph_id"]: m for m in self.materials}
        fact_ids = {fid for m in self.materials for fid in m.get("fact_ids", [])}
        for rid, card in self.roles.items():
            points = card.get("knowledge_points", [])
            if len({p.get("fact_id") for p in points}) != len(points):
                errors.append("Duplicate role facts: " + rid)
            for point in points:
                if point.get("fact_id") not in fact_ids:
                    errors.append("Unknown role fact: " + rid + ":" + str(point.get("fact_id")))
                stage = point.get("available_from_stage")
                if stage is not None and stage not in self.stages:
                    errors.append("Unknown fact release stage: " + rid)
                event = point.get("required_event")
                if event and not any(rule.get("required_event") == event for rule in self.permissions.get("material_rules", [])):
                    errors.append("Unknown fact release event: " + rid)
        for rule in self.permissions.get("material_rules", []):
            expected = {p for p, row in paragraphs.items() if row["material_id"] == rule["material_id"]}
            if set(rule.get("paragraph_ids", [])) != expected:
                errors.append("Material ACL paragraph mismatch: " + rule["material_id"])
        summary = self.procedure.get("summary_presentation", {})
        if not set(summary.get("permitted_paragraph_ids", [])) <= set(paragraphs):
            errors.append("Summary names unknown paragraph")
        for rid, setting in self.procedure.get("witnesses", {}).items():
            if rid not in self.roles or not set(setting.get("rounds", [])) <= {1, 2, 3}:
                errors.append("Invalid witness role/round: " + rid)
        return errors

    def simulation_contract(self):
        return {"version": ENVIRONMENT_VERSION, "kind": "explicit_simulation_assumption_not_historical_stipulation",
                "common": COMMON_CONTRACT, "case_specific": CASE_CONTRACTS[self.case_id],
                "does_not_determine_verdict": True, "does_not_make_all_evidence_admissible": True}

    def public_brief(self):
        result = super().public_brief()
        result["simulation_contract"] = self.simulation_contract()
        return result

    def role_card(self, role_id):
        """Trusted view before engine stage filtering; never send raw gated card."""
        card = super().role_card(role_id)
        source_points = {p["fact_id"]: p for p in self.roles[role_id].get("knowledge_points", [])}
        for point in card.get("knowledge_points", []):
            source = source_points[point["fact_id"]]
            for key in ("available_from_stage", "required_event", "fact_scope"):
                point[key] = deepcopy(source.get(key))
            if source.get("required_event") or source.get("available_from_stage"):
                event = source.get("required_event")
                mid = next((r["material_id"] for r in self.permissions.get("material_rules", []) if event and r.get("required_event") == event), None)
                point["gated_until"] = {"stage_id": source.get("available_from_stage"), "required_event": event, "material_id": mid}
        if isinstance(card.get("identity"), dict):
            card["identity"].pop("source_id", None)
        return card

    def author_environment_manifest(self):
        return {"environment_version": ENVIRONMENT_VERSION, "case_id": self.case_id,
                "simulation_contract": self.simulation_contract(), "case_files_modified": False,
                "historical_result_loaded_by_runtime": False,
                "runtime_input_sha256": self.runtime_input_sha256,
                "role_fact_mapping": {rid: [{"fact_id": p["fact_id"], "content_sha256": hashlib.sha256(p["content"].encode()).hexdigest(),
                    "source_locators": p.get("source_locators", []), "available_from_stage": p.get("available_from_stage"),
                    "required_event": p.get("required_event")} for p in card.get("knowledge_points", [])]
                    for rid, card in self.roles.items()},
                "meaning": "Exact supplied attributed role facts; no fabricated witness speech or historical result."}


TASK = """# {cid} 全流程辩护任务（三轮庭审）

你是被评测的辩护律师。先完成可观察的庭前文件处理和准备，再参加本案规定的三轮庭审。
文件是所给材料的来源约束摘要及所给法律摘录，不是未取得的原始报告、录音、录像或逐字证言。保留M段落编号。

1. 用list_files/read_document实际读取所需文件；用search_case/search_law/search_record进行授权本地检索。检索不访问互联网，不得声称已联网或取得工具未返回的内容。
2. 区分已知、转述、推断、争议和未知，形成具体准备方案。用request_investigation或request_examination向资料核验服务提出明确问题及目的；服务实际查询冻结案包并返回报告文件和可用性状态，不联系真实机构，不保证发现新证据。not_available_in_supplied_record仅表示本包没有原件，不表示历史上不存在。可在允许的准备或轮间阶段向A私下咨询；对方按自己的来源知识范围回答，问答将单独保存，不能补造资料。
3. 用write_artifact实际保存以下四份可打开的文件，内容由你生成，不要只写空模板：
   - case_timeline.csv：事件、日期或顺序、未知处、M段落来源。
   - evidence_register.csv：材料及段落、来源性质、披露及采纳状态、潜在用途与缺口。
   - pretrial_plan.md：分项指控、关键问题、法源与事实、行动优先级、真实调查反馈和条件替代方案。
   - examination_outline.md：可询问对象、具体问题、目的与出处、未知答复的后续处理。
   调查反馈及各轮裁定后可以更新，文件版本与事件会保存；也可保存允许的补充说明。
4. 第一轮处理程序、证据和裁定；第二轮完成举证询问；第三轮对既有记录作补充询问、结案、法律指示和分项裁决。具体动作和角色以当前阶段授权为准。
5. 庭审继续用既有M段落和L法律编号。计划不是已执行行动，调查摘要不是新历史证据，披露不是采纳。角色按权限和实际裁定接收内容，私有准备不会自动送交陪审团。第二轮获准记录继续保留到第三轮，限制、排除及划除仍有效。

{case_note}

完成情况看实际记录、问答、工具返回和文件内容，不按工具次数、文件字数、是否发现有利证据或最终胜负奖励。允许其他有来源支撑的辩护路径。独立评分标准不提供给庭审角色。
"""


def services(cid):
    if cid == "CA-PILOT-005":
        return [
            {"service_id": "registration_records", "title": "登记告知及窗口原始记录核验", "purpose": "核查登记告知、接收表、档案与窗口记录在所给案包中的可获得性。",
             "source_refs": ["M02-P001", "M03-P001", "M04-P002", "M05-P001", "M08-P001"],
             "response_status": "not_available_in_supplied_record",
             "response_text": "当前包只有B告知和接收表标记、C登记要求、未登记记载，以及A称曾到窗口尝试登记的来源摘要；没有接收表、假释档案、机关查询、预约、身份证件原件或接待人员证词。A的被拒登记说法仍为本人主张。缺件不能证明原件从未存在，也不能据此断言窗口确曾或未曾拒绝。"},
            {"service_id": "residence_records", "title": "居住变化与家访记录核验", "purpose": "核查已有住宿日期、C安排和家访摘要及未提供的原始记录。",
             "source_refs": ["M01-P001", "M01-P002", "M01-P003", "M03-P002", "M03-P003", "M04-P001", "M07-P001", "M08-P001"],
             "response_status": "source_summary_only",
             "response_text": "当前包可核对朋友家、救助站、汽车旅馆、亲属家的来源摘要以及C安排和两次家访记载；家访的具体日期、亲属身份和地址未提供。日期摘录不能补成机关完整居住或登记档案，期限适用及迁居影响仍须由律师论证。"},
            {"service_id": "motel_incident_records", "title": "旅馆事件独立材料核验", "purpose": "核查A的旅馆绑架、威胁说法是否有本包独立材料。",
             "source_refs": ["M05-P002", "M08-P001"], "response_status": "not_available_in_supplied_record",
             "response_text": "当前包仅有A关于旅馆绑架、威胁及逃脱后害怕的说法；没有该事件的独立记录。服务不能新增绑架者、亲属或接待人员证词，不能确认事件真实或虚假。"},
        ]
    return [
        {"service_id": "assessment_records", "title": "现场观察、PAS与检测设备记录核验", "purpose": "核查公开观察、PAS摘要及原始仪器操作资料。",
         "source_refs": ["M02-P001", "M02-P002", "M03-P001", "M03-P002", "M08-P001", "M08-P002"],
         "response_status": "not_available_in_supplied_record",
         "response_text": "当前包有B到场观察、医院眼震测试、PAS告知和0.09%结果的摘要；没有原始眼震、资格、设备维护校准或操作记录，也没有各环节具体时刻。本服务只能核对已公开摘要，不新增检测结果、时长或病因解释。"},
        {"service_id": "blood_records", "title": "采血与实验室原始材料核验", "purpose": "核查抽血态度、血样结果摘要与原始检验材料的边界。",
         "source_refs": ["M04-P001", "M04-P002", "M08-P001", "M08-P002", "M08-P003"],
         "response_status": "not_available_in_supplied_record",
         "response_text": "当前包记载A不表示同意但允许医院人员采血及血样0.12%的结论；没有逐字抽血对话、采血时刻、人员资质、检验方法、设备、流转与完整报告。不能把允许操作改写成明确同意，也不能生成毒理人员新证词、饮酒量或逆推数据。"},
        {"service_id": "collision_records", "title": "碰撞与伤害资料核验", "purpose": "核查事故、伤害摘要与碰撞重建或实时证词的可获得性。",
         "source_refs": ["M01-P001", "M01-P002", "M08-P004"], "response_status": "source_summary_only",
         "response_text": "当前包有A车辆越过双黄线、正面碰撞及另一车两人严重受伤的来源摘要；没有碰撞重建、伤情诊断、医疗原件或被害人实时证言。B事故后到场，不能把全过程变成其亲眼所见；服务不能补造被害人或碰撞目击者。"},
    ]


def build_case(root, cid):
    case = ThreeRoundCaseData(root / "cases" / cid)
    work = root / "workflows" / cid
    work.mkdir(parents=True, exist_ok=True)
    note = CASE_CONTRACTS[cid]
    task = TASK.format(cid=cid, case_note=note)
    (work / "task.md").write_text(task, encoding="utf-8")
    catalog = []

    def doc(file_id, title, text, refs, mids=None, stage=PRETRIAL, suffix=".md"):
        path = work / "files" / (file_id + suffix)
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        catalog.append({"file_id": file_id, "path": "files/" + path.name, "title": title,
                        "kind": "source_bound_summary" if mids else "task_or_supplied_law",
                        "source_refs": refs, "material_ids": mids or [],
                        "allowed_roles": ["R_DEFENSE", "R_PROSECUTOR"], "available_from_stage": stage,
                        "sha256": sha(path)})
    doc("task", "案件与工作流程任务", case._brief_text + "\n" + task, ["ENV-TASK"])
    for mid in dict.fromkeys(m["material_id"] for m in case.materials):
        rows = [m for m in case.materials if m["material_id"] == mid]
        stage = rows[0].get("available_from_stage", "preparation")
        stage = PRETRIAL if stage == "preparation" else stage
        text = f"# {mid} {rows[0]['title']}\n\n文件性质：所给判决来源摘要，非原始文件或逐字证言。披露不等于采纳。\n\n"
        text += "\n\n".join(f"## {m['paragraph_id']}\n\n{m['text']}\n\n来源性质：{m['epistemic_status']}" for m in rows) + "\n"
        doc(mid.lower(), rows[0]["title"], text, [m["paragraph_id"] for m in rows], [mid], stage)
    lawtext = "# 所给历史法律摘录\n\n只重排原包公开法律，不新增网络检索。原包法律版本待复核状态保留。\n\n"
    for law in case.laws:
        lawtext += f"## {law['legal_source_id']} — {law.get('section', '')}\n\n版本：{law.get('version', '')}\n\n状态：{law.get('version_status', '')}\n\n"
        lawtext += "\n\n".join(f"### {r['paragraph_id']}\n\n{r['text']}" for r in law["paragraphs"]) + "\n\n"
    doc("supplied_law", "所给历史法律摘录", lawtext, [l["legal_source_id"] for l in case.laws])
    inventory = io.StringIO()
    writer = csv.writer(inventory)
    writer.writerow(["file_id", "filename", "document_type", "source_paragraphs"])
    for row in catalog:
        # Do not reveal a delayed material's contents, title or paragraph list through a public inventory.
        if row["available_from_stage"] == PRETRIAL:
            writer.writerow([row["file_id"], Path(row["path"]).name, row["kind"], ";".join(row["source_refs"])])
    doc("document_inventory", "初始输入文件目录", inventory.getvalue(), ["ENV-TASK"], suffix=".csv")
    svcs = services(cid)
    for svc in svcs:
        svc.update({"available_stages": [PRETRIAL] + [s for s in case.stages if s != "ended"],
                    "allowed_roles": ["R_DEFENSE", "R_PROSECUTOR"], "disclosed_material_ids": [],
                    "new_historical_facts": False, "report_kind": "deterministic_supplied_record_check",
                    "network_used": False, "request_fields": ["target: service_id", "text: 具体核验问题及用途"]})
    spec = {"case_id": cid, "version": VERSION, "task": task, "task_path": "task.md", "files": catalog,
            "services": svcs, "pretrial_actions": 20, "required_artifacts": ARTIFACTS,
            "allowed_artifacts": ARTIFACTS + ["motion_note.md", "investigation_followup.md"],
            "search_backend": "authorized_local_snapshot", "external_network_used": False,
            "file_processing_scope": "read_and_structure_supplied_summary_files; original_media_not_supplied",
            "investigation_scope": "frozen_corpus_record_checks; no_real_world_contact_or_synthetic_fact_discovery",
            "private_outputs": True, "disclosure_does_not_imply_admission": True,
            "simulation_round_count": 3, "runtime_layer_version": ASSETS_VERSION}
    write(work / "workflow.json", spec)
    source_map = {"ENV-TASK": {"source_id": "ENV-TASK", "kind": "simulation_task_not_historical_fact", "text": task}}
    for row in case.materials:
        source_map[row["paragraph_id"]] = {"source_id": row["paragraph_id"], "kind": "supplied_source_bound_summary",
            "text": row["text"], "material_id": row["material_id"], "original_locators": row["source_locators"],
            "epistemic_status": row["epistemic_status"], "source_path": "public/materials.jsonl"}
    for law in case.laws:
        source_map[law["legal_source_id"]] = {"source_id": law["legal_source_id"], "kind": "supplied_law",
            "text": "\n\n".join(p["paragraph_id"] + "\n" + p["text"] for p in law["paragraphs"]),
            "source_snapshot_path": law.get("source_snapshot_path"), "version": law.get("version"),
            "version_status": law.get("version_status")}
    write(work / "source_map.json", source_map)
    rubric = read(case.path / "evaluation/rubric.json")
    if rubric.get("items") or rubric.get("approved"):
        raise AssetError("build_requires_original_pending_rubric", cid)
    rubric.update(version="three-round-rubric-pending-v1", points=[], point_count=0, maximum_points=None,
                  notice="原包rubric尚未完成；开发运行不评分，正式运行需要另行提供冻结rubric。")
    write(work / "rubric.json", rubric)
    hashes = {str(p.relative_to(case.path)): sha(p) for p in sorted(case.path.rglob("*")) if p.is_file()}
    authoring = {"case_id": cid, "protocol": ASSETS_VERSION, "source_case_modified": False,
                "allowed_inputs": RUNTIME_INPUTS, "source_files_sha256": hashes,
                "excluded_from_author_context": ["old_rollouts", "old_AI_analysis", "old_rubrics", "old_scores", "authoring/outcome_reference.json"],
                "source_audit_only_inputs": ["authoring/provenance.json", "authoring/sources/index.json", "authoring/sources/**"],
                "rubric_authored": False, "rubric_sha256": sha(work / "rubric.json"),
                "workflow_sha256": sha(work / "workflow.json"), "source_map_sha256": sha(work / "source_map.json"),
                "new_historical_facts": 0, "live_model_calls": 0,
                "preserved_two_round_contract": {"pretrial_actions": 20, "required_artifacts": ARTIFACTS,
                    "search_backend": "authorized_local_snapshot", "tool_report_and_artifact_formats": "workflow-artifacts-v3"}}
    write(work / "authoring_record.json", authoring)
    validate_workflow(work, spec, case)


def build_assets(root=None):
    """Explicit offline build only. Runtime load never regenerates a manifest."""
    root = data_path(root)
    for cid in CASE_IDS:
        build_case(root, cid)
    entries = {str(p.relative_to(root)): sha(p) for p in sorted(root.rglob("*"))
               if p.is_file() and p.name != MANIFEST_NAME}
    write(root / MANIFEST_NAME, {"protocol": ASSETS_VERSION, "case_ids": list(CASE_IDS), "sha256": entries,
                                 "source_case_bytes_unchanged": True, "paid_model_calls": 0})
    return load_assets(root)


def verify_sources(case, source_map):
    """Offline originals/anchor audit; result is never actor input."""
    index = read(safe(case.path, "authoring/sources/index.json"))
    for row in index["files"]:
        if sha(safe(case.path, row["path"])) != row["sha256"]:
            raise AssetError("original_source_hash_mismatch", row["path"])
    paragraphs = {}
    for path in (case.path / "authoring/sources").glob("source*/opinions/*/paragraphs.json"):
        for row in read(path):
            locator = row["locator"]
            if locator in paragraphs and paragraphs[locator] != row["text_verbatim"]:
                raise AssetError("conflicting_source_locator", locator)
            paragraphs[locator] = row["text_verbatim"]
    provenance = read(safe(case.path, "authoring/provenance.json"))
    facts = {}
    count = 0
    for mapping in provenance["fact_mappings"]:
        fid = mapping["fact_id"]
        if fid in facts:
            raise AssetError("duplicate_provenance_fact", fid)
        facts[fid] = mapping
        for anchor in mapping.get("source_spans", []):
            if paragraphs.get(anchor["locator"]) != anchor["text_verbatim"]:
                raise AssetError("provenance_anchor_mismatch", anchor["locator"])
            count += 1
    for row in case.materials:
        if row["paragraph_id"] not in source_map or source_map[row["paragraph_id"]]["text"] != row["text"]:
            raise AssetError("material_source_text_mismatch", row["paragraph_id"])
        if source_map[row["paragraph_id"]].get("original_locators") != row["source_locators"]:
            raise AssetError("material_source_mapping_mismatch", row["paragraph_id"])
        if not set(row.get("source_locators", [])) <= set(paragraphs) or not set(row.get("fact_ids", [])) <= set(facts):
            raise AssetError("material_missing_source_or_fact", row["paragraph_id"])
    role_count = 0
    for rid, card in case.roles.items():
        for point in card.get("knowledge_points", []):
            known = facts.get(point["fact_id"], {})
            locators = {p["locator"] for p in known.get("source_spans", [])}
            if not point.get("source_locators") or not set(point["source_locators"]) <= locators:
                raise AssetError("role_fact_anchor_mismatch", rid + ":" + point["fact_id"])
            if not point.get("content") or point.get("must_preserve_attribution") is not True:
                raise AssetError("role_fact_missing_attribution", rid + ":" + point["fact_id"])
            role_count += 1
    for law in case.laws:
        expected = "\n\n".join(p["paragraph_id"] + "\n" + p["text"] for p in law["paragraphs"])
        if source_map.get(law["legal_source_id"], {}).get("text") != expected:
            raise AssetError("law_excerpt_mismatch", law["legal_source_id"])
    return {"status": "passed", "source_files": len(index["files"]), "verbatim_provenance_anchors": count,
            "role_fact_bindings": role_count, "semantic_expert_review": False, "live_connectivity_tested": False}


def load_assets(root=None):
    """Read-only, fail-closed loader for a package root or its data directory."""
    root = data_path(root)
    manifest = read(safe(root, MANIFEST_NAME))
    if manifest.get("protocol") != ASSETS_VERSION or manifest.get("case_ids") != list(CASE_IDS):
        raise AssetError("wrong_asset_manifest", str(root))
    inventory = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and p.name != MANIFEST_NAME}
    if inventory != set(manifest.get("sha256", {})):
        raise AssetError("asset_inventory_changed", str(sorted(inventory ^ set(manifest.get("sha256", {})))))
    for rel, expected in manifest["sha256"].items():
        if sha(safe(root, rel)) != expected:
            raise AssetError("asset_hash_mismatch", rel)
    result = {}
    for cid in CASE_IDS:
        case = ThreeRoundCaseData(root / "cases" / cid)
        work = root / "workflows" / cid
        spec, rubric, source_map, authoring = [read(safe(work, name)) for name in
            ("workflow.json", "rubric.json", "source_map.json", "authoring_record.json")]
        if any(obj.get("case_id") != cid for obj in (spec, rubric, authoring)):
            raise AssetError("case_id_mismatch", cid)
        validate_workflow(work, spec, case)
        if spec.get("simulation_round_count") != 3 or spec.get("required_artifacts") != ARTIFACTS or spec.get("pretrial_actions") != 20:
            raise AssetError("workflow_contract_mismatch", cid)
        if rubric.get("points") or rubric.get("items") or rubric.get("approved") or rubric.get("maximum_points") is not None:
            raise AssetError("pending_rubric_changed_use_external_frozen_rubric", cid)
        for field, name in [("rubric_sha256", "rubric.json"), ("workflow_sha256", "workflow.json"), ("source_map_sha256", "source_map.json")]:
            if authoring.get(field) != sha(work / name):
                raise AssetError("authoring_binding_mismatch", cid + ":" + name)
        for rel, expected in authoring.get("source_files_sha256", {}).items():
            if sha(safe(case.path, rel)) != expected:
                raise AssetError("authoring_source_hash_mismatch", rel)
        if spec["task"] != (work / "task.md").read_text(encoding="utf-8") or source_map.get("ENV-TASK", {}).get("text") != spec["task"]:
            raise AssetError("workflow_task_source_mismatch", cid)
        expected_sids = {"ENV-TASK"} | {m["paragraph_id"] for m in case.materials} | {l["legal_source_id"] for l in case.laws}
        if set(source_map) != expected_sids:
            raise AssetError("unexpected_scoring_source", cid)
        hidden = {m["paragraph_id"] for m in case.materials if m.get("evidence_status") == "not_yet_disclosed"}
        hidden_mids = {m["material_id"] for m in case.materials if m["paragraph_id"] in hidden}
        for doc in spec["files"]:
            if not set(doc["source_refs"]) <= set(source_map):
                raise AssetError("document_missing_source", doc["file_id"])
            if set(doc["source_refs"]) & hidden and (doc["available_from_stage"] == PRETRIAL or not set(doc["material_ids"]) & hidden_mids):
                raise AssetError("premature_document_disclosure", doc["file_id"])
        for service in spec["services"]:
            if not set(service["source_refs"]) <= set(source_map) or set(service["source_refs"]) & hidden:
                raise AssetError("premature_or_unknown_investigation_source", service["service_id"])
        audit = verify_sources(case, source_map)
        result[cid] = {"case_id": cid, "case_path": case.path, "workflow_root": work, "workflow_spec": spec,
                       "rubric": rubric, "source_map": source_map, "authoring_record": authoring,
                       "author": {"case_id": cid, "protocol": ASSETS_VERSION, "source_units": list(source_map.values()),
                                  "allowed_inputs": authoring["allowed_inputs"], "excluded_inputs": authoring["excluded_from_author_context"]},
                       "evaluation_aliases": {"case_id": cid, "person_aliases": {}, "note": "Only supplied public role aliases."},
                       "source_audit": audit}
    return result
