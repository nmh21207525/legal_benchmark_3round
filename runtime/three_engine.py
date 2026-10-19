"""Three-round extension of the existing source-bound full workflow.

The old tool/artifact implementation is reused.  Case, role, stage, disclosure,
record rendering and jury input remain trusted host state, never model fields.
No rubric or historical outcome is loaded by this module.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

from court_agent.engine import CourtEngine, COURT, LAWYERS, RunStopped
from court_agent.schema import (BASE_PROMPT, LAWYER_PROMPT, WITNESS_SCHEMA,
                                ELECTION_SCHEMA, VERDICT_SCHEMA, action_schema,
                                decision_schema)
from court_agent.search import ControlledSearch
from court_agent.llm import _validate_value
from source_rubric_v2.environment import ACTION_INTERFACE_CONTRACT
from workflow import (FullWorkflowEngine, ArtifactStore, PRETRIAL, TOOL_ACTIONS,
                      VERSION as WORKFLOW_VERSION, validate_workflow, canonical, sha)

VERSION = "source-bound-three-rounds-v1"


class _ThreeEnvironmentBackend:
    def __init__(self, inner, case):
        self.inner, self.case = inner, case

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def decide(self, role_id, system, context, schema):
        if role_id in {*LAWYERS, "R_JUDGE"}:
            context = deepcopy(context)
            context["simulation_contract"] = self.case.simulation_contract()
            if role_id in LAWYERS:
                system += ACTION_INTERFACE_CONTRACT
        return self.inner.decide(role_id, system, context, schema)


class _GatedSearch(ControlledSearch):
    def __init__(self, case, engine):
        super().__init__(case)
        self.engine = engine

    def search(self, **kwargs):
        kwargs["disclosed_ids"] = {
            mid for mid in kwargs["disclosed_ids"] if self.engine._material_released(mid)
        }
        return super().search(**kwargs)


class ThreeRoundEngine(FullWorkflowEngine):
    """Keep the two-round tools/journals, dispatch by the new case procedure."""

    def __init__(self, case, backend, config, *, workflow_root, workflow_spec, artifact_root):
        # SourceBoundCourtEngine's constructor and witness mappings accept only
        # 003/004.  Do not mutate those maps or make new cases impersonate them.
        validate_workflow(workflow_root, workflow_spec, case)
        if case.manifest.get("simulation_round_count") != 3:
            raise ValueError("three_round_engine_requires_three_round_case")
        CourtEngine.__init__(self, case, _ThreeEnvironmentBackend(backend, case), config)
        self.protocol_version = VERSION
        self.workflow_root = Path(workflow_root).resolve()
        self.workflow_spec = deepcopy(workflow_spec)
        self.artifacts = ArtifactStore(artifact_root)
        self.pretrial_actions = 0
        self.pretrial_completed = False
        self._extracted = {}
        self._base_initial_stage = self.stage
        self._stage_order = [PRETRIAL] + list(case.stages)
        self.validated_events = set()
        self.pending_objections = set()
        self.search = _GatedSearch(case, self)
        self.testimony_elected = bool(case.procedure.get("defendant_testimony", {}).get("initially_elected", False))
        self._completed_stages = []
        self._validate_three_round_contract()

    def _validate_three_round_contract(self):
        order, stage = [], self._base_initial_stage
        while stage is not None:
            if stage in order or stage not in self.case.stages:
                raise ValueError("invalid_three_round_stage_chain")
            order.append(stage)
            row = self.case.stages[stage]
            if "trusted_orchestrator" not in row.get("exit_requirements", {}).get("closure_authority", []):
                raise ValueError("stage_closure_not_authorized")
            stage = row.get("next_stage_id")
        if order[-1] != "ended" or {self.case.stages[s].get("simulation_round") for s in order} - {None} != {1, 2, 3}:
            raise ValueError("incomplete_three_round_stage_chain")
        if self.case.procedure.get("jury_instruction_stage_id") != "r3_instructions":
            raise ValueError("third_round_instruction_stage_required")
        if "record_verdict" not in self.case.stages.get("r3_verdict", {}).get("allowed_actions", []):
            raise ValueError("third_round_verdict_stage_required")
        if set(self.case.procedure.get("jury_evidence_rounds", [])) != {2, 3}:
            raise ValueError("three_round_jury_rounds_required")
        for row in self.case.procedure.get("disclosure_rules", []):
            if row.get("trigger_type") == "stage_release":
                if row.get("release_actor_role_ids") != ["trusted_orchestrator"] or row.get("validated_release_required") is not True:
                    raise ValueError("stage_release_contract_invalid")
                stage = self.case.stages.get(row.get("earliest_stage"), {})
                if "release_stage_material" not in stage.get("allowed_actions", []):
                    raise ValueError("stage_release_stage_not_authorized")

    def _effective_stage(self):
        return self._base_initial_stage if self.stage == PRETRIAL else self.stage

    def _stage_reached(self, stage):
        return not stage or (stage in self._stage_order and self._stage_order.index(self._effective_stage()) >= self._stage_order.index(stage))

    def _material_released(self, material_id):
        rules = [r for r in self.case.permissions.get("material_rules", []) if r["material_id"] == material_id]
        return material_id in self.disclosed and any(
            self._stage_reached(r.get("minimum_stage"))
            and (not r.get("required_event") or r["required_event"] in self.validated_events)
            for r in rules)

    def role_card(self, role_id):
        card = self.case.role_card(role_id)
        points = []
        raw = {p["fact_id"]: p for p in self.case.roles[role_id].get("knowledge_points", [])}
        for point in card.get("knowledge_points", []):
            policy = raw.get(point["fact_id"], point)
            gate = policy.get("gated_until") or {}
            minimum = policy.get("available_from_stage") or gate.get("stage_id")
            required = policy.get("required_event") or gate.get("required_event")
            # pretrial workspace has preparation's permissions, not a new route
            # into undisclosed content.  No denied fact or ID goes in the card.
            if minimum and not self._stage_reached(minimum):
                continue
            if required and required not in self.validated_events:
                continue
            clean = deepcopy(point)
            for field in ("source_id", "source_locators", "available_from_stage", "required_event", "gated_until"):
                clean.pop(field, None)
            points.append(clean)
        card["knowledge_points"] = points
        return card

    def _source_at_effective_stage(self, role, ref):
        original = self.stage
        try:
            self.stage = self._effective_stage()
            for rule in self.case.permissions.get("material_rules", []):
                if ref in rule.get("paragraph_ids", []) and not self._material_released(rule["material_id"]):
                    raise ValueError("source_not_authorized")
            if self.case.stages[self.stage].get("simulation_round") == 1 and ref in self.case.procedure.get("first_round_excluded_paragraph_ids", []):
                raise ValueError("source_not_authorized")
            return CourtEngine.source(self, role, ref)
        finally:
            self.stage = original

    def catalog(self):
        result = []
        for mid in sorted(self.disclosed):
            for row in self.case.material_paragraphs(mid):
                try:
                    self._source_at_effective_stage("R_JUDGE", row["citation_id"])
                except ValueError:
                    continue
                result.append({k: row[k] for k in ("material_id", "citation_id", "title") if k in row})
        return result

    def context(self, role, task=None):
        result = FullWorkflowEngine.context(self, role, task)
        result["role_card"] = self.role_card(role)
        if role in LAWYERS:
            result["procedure_contract"] = {
                "simulation_round_count": 3,
                "jury_evidence_rounds": self.case.procedure["jury_evidence_rounds"],
                "jury_instruction_stage_id": self.case.procedure["jury_instruction_stage_id"],
                "summary_presentation": deepcopy(self.case.procedure.get("summary_presentation", {})),
                "record_only_role_ids": [rid for rid, row in self.case.procedure.get("witnesses", {}).items() if row.get("mode") == "record_only"],
                "consultation_stage_ids": [PRETRIAL] + list(self.case.procedure.get("consultation_stage_ids", [])),
                "first_round_record_reuse": self.case.procedure.get("round_one_record_reuse"),
                "round_two_record_retention": self.case.procedure.get("round_two_record_retention"),
                "current_stage_exit_requirements": deepcopy(self.case.stages.get(self.stage, {}).get("exit_requirements", {})),
            }
            # A source presentation index may name future material; expose only
            # the paragraphs actually disclosed to this role now.
            summary = result["procedure_contract"]["summary_presentation"]
            allowed = {r["citation_id"] for r in self.catalog()}
            summary["permitted_paragraph_ids"] = [p for p in summary.get("permitted_paragraph_ids", []) if p in allowed]
        return result

    def allowed_actions(self, role):
        if role not in LAWYERS:
            return []
        actions = set(FullWorkflowEngine.allowed_actions(self, role))
        if self.stage == PRETRIAL and role == "R_DEFENSE" and "consult_client" in self.case.stages[self._base_initial_stage]["allowed_actions"]:
            actions.add("consult_client")
        return sorted(actions)

    def lawyer_turn(self, role):
        allowed = self.allowed_actions(role)
        ctx = self.context(role, "choose_action")
        ctx.update(allowed_actions=allowed,
                   questionable_role_ids=self.case.stages.get(self.stage, {}).get("questionable_role_ids", []),
                   objection_catalog=self.objection_catalog())
        prompt = LAWYER_PROMPT + "\nworkflow字段提供文件、调查、检索与实际文件写入工具。保留每个来源的归属与未知范围。" + \
            "consult_client的text是要问被告的具体问题，回答只对辩方及被告开放。record_rulings读取现有裁定并保存自己的记录。" + \
            "来源材料在第二/三轮作为实体证据时，只能在procedure_contract.summary_presentation指定阶段通过offer_evidence提交；" + \
            "R_SOURCE_RECORD是可信摘要呈现角色，record_only角色不能实时问答。第三轮可针对第二轮已产生的记录定向补充，不得编造新事实。"
        answer = self.backend.decide(role, prompt, ctx, action_schema(allowed))
        if answer.get("action") not in allowed:
            raise ValueError("action_not_allowed_in_stage")
        self.apply(role, answer)
        return answer["action"]

    def apply(self, role, action):
        kind = action["action"]
        if role not in LAWYERS or kind not in self.allowed_actions(role):
            raise ValueError("permission_denied")
        if kind == "record_rulings":
            rows = [deepcopy(e) for e in self.visible_record(role)
                    if e["kind"] in {"motion_ruling", "evidence_ruling", "objection_ruling"}]
            self.memories[role].append({"tool": kind, "result": rows})
            self.emit("tool_result", role, action.get("text", ""), action.get("references", []),
                      audience=[role], tool=kind, result={"ok": True, "rulings": rows, "not_evidence": True})
            return
        if kind == "consult_client":
            if role != "R_DEFENSE" or self._effective_stage() not in self.case.procedure.get("consultation_stage_ids", []):
                raise ValueError("private_consultation_denied")
            if not isinstance(action.get("text"), str) or not action["text"].strip():
                raise ValueError("consultation_question_required")
            request = self.emit("private_consultation_question", role, action["text"], action.get("references", []),
                                audience=["R_DEFENSE", "R_DEFENDANT"], target="R_DEFENDANT")
            response = self.witness_answer("R_DEFENDANT", action["text"], private=True)
            response["meta"]["source_question_event_id"] = request["event_id"]
            request["meta"]["response_event_id"] = response["event_id"]
            return
        return FullWorkflowEngine.apply(self, role, action)

    def disclose(self, role, action):
        rule = next((r for r in self.case.procedure.get("disclosure_rules", []) if r["material_id"] == action.get("target")), None)
        if rule and rule.get("trigger_type") == "stage_release":
            raise ValueError("only_trusted_stage_release_can_disclose_material")
        return CourtEngine.disclose(self, role, action)

    def release_stage_materials(self):
        """Called only by the host at stage entry, never a model action."""
        for rule in self.case.procedure.get("disclosure_rules", []):
            if rule.get("trigger_type") != "stage_release" or self.stage != rule["earliest_stage"]:
                continue
            material = rule["material_id"]
            key = "stage_release:" + material
            if key in self.validated_events:
                continue
            if ("release_stage_material" not in self.case.stages[self.stage]["allowed_actions"]
                    or rule.get("release_actor_role_ids") != ["trusted_orchestrator"]
                    or rule.get("validated_release_required") is not True):
                raise RunStopped("invalid_trusted_stage_release")
            permissions = [r for r in self.case.permissions["material_rules"] if r["material_id"] == material]
            if (len(permissions) != 1 or permissions[0].get("required_event") != key
                    or permissions[0].get("minimum_stage") != self.stage
                    or set(permissions[0]["allowed_roles"]) != set(rule["release_to_role_ids"])):
                raise RunStopped("stage_release_acl_mismatch")
            ids = permissions[0]["paragraph_ids"]
            rows = [r for r in self.case.material_paragraphs(material) if r["citation_id"] in ids]
            if sorted(r["citation_id"] for r in rows) != sorted(ids):
                raise RunStopped("stage_release_missing_authorized_paragraph")
            self.emit("stage_release", "trusted_orchestrator", "指定阶段开放同一来源摘要；不自动准入，不追改第一轮裁定。", ids,
                      audience=rule["release_to_role_ids"], material_id=material,
                      validated_event=key, validated_release=True,
                      released_paragraphs=deepcopy(rows), admitted=False)
            self.validated_events.add(key)
            self.disclosed.add(material)

    def judge(self, task, packet, decisions):
        ctx = self.context("R_JUDGE", task)
        ctx.update(packet=packet, legal_sources=self.legal_texts(),
                   motion_rules=self.case.procedure.get("motion_rules", {}))
        schema = decision_schema(decisions)
        schema["properties"]["legal_source_ids"] = {"type": "array", "minItems": 1,
            "items": {"type": "string", "enum": sorted(self.legal_ids())}}
        schema["properties"]["reason"] = {"type": "string", "minLength": 1, "pattern": r"\S"}
        answer = self.backend.decide("R_JUDGE", BASE_PROMPT + """依据授权材料、法源及公开simulation_contract裁定packet指定事项。
来源摘要是本题已有记录的呈现载体，不等于原件、逐字证言或当然真实；载体可用与具体内容/用途是否准入分别判断。
保留他人报告、个人意见与未知事实的边界。第一轮记录不自动进入陪审团，第二轮获准记录在第三轮延续并适用最新限制/排除。
不得用后来开放的资料倒改第一轮裁定；R_SOURCE_RECORD与record_only角色不作实时证言。准入须提供非空permitted_purposes；缺基础可pending_foundation。
只引用提供的完整法源编号。不得读取或推测原案裁决、隐藏底稿和rubric，不得以预设胜负反推裁定。
""", ctx, schema)
        if not _validate_value(answer, schema, schema):
            raise RunStopped("invalid_judge_decision_or_citation")
        if answer["decision"] in {"admitted", "limited_use"} and not all(isinstance(p, str) and p.strip() for p in answer.get("permitted_purposes", [])):
            raise RunStopped("admission_missing_permitted_purpose")
        if answer["decision"] in {"admitted", "limited_use"} and not answer.get("permitted_purposes"):
            raise RunStopped("admission_missing_permitted_purpose")
        return answer

    def witness_answer(self, witness, question, private=False):
        spec = self.case.procedure.get("witnesses", {}).get(witness, {})
        if (witness not in self.case.roles or spec.get("mode") != "source_bound_live"
                or self.case.roles[witness].get("role_type") in {"recorded_witness", "record_renderer"}):
            raise ValueError("recorded_witness_cannot_give_live_testimony")
        if private:
            if witness != "R_DEFENDANT" or self._effective_stage() not in self.case.procedure.get("consultation_stage_ids", []):
                raise ValueError("private_witness_not_authorized")
        elif (witness not in self.case.stages[self.stage].get("questionable_role_ids", [])
              or self.case.stages[self.stage].get("simulation_round") not in spec.get("rounds", [])
              or (witness == "R_DEFENDANT" and not self.testimony_elected)):
            raise ValueError("witness_not_questionable")
        card = self.role_card(witness)
        facts = {p["fact_id"]: p["content"] for p in card.get("knowledge_points", [])}
        schema = deepcopy(WITNESS_SCHEMA)
        schema["properties"]["fact_ids"] = {"type": "array", "maxItems": len(facts),
            "items": {"type": "string", "enum": sorted(facts) or ["__NO_AUTHORIZED_FACT__"]}}
        ctx = {"task": "source_bound_answer", "role_id": witness, "stage_id": self.stage,
               "role_card": card, "question": question, "authorized_fact_ids": sorted(facts)}
        answer = self.backend.decide(witness, BASE_PROMPT +
            "只选择输入authorized_fact_ids中回答问题所需的编号；无来源则unknown=true且fact_ids=[]。"
            "不能把材料编号当fact_id，不得新增事实。引用的角色事实按原文呈现，保留来源归属与不确定性。", ctx, schema)
        selected = answer.get("fact_ids", [])
        if not isinstance(selected, list) or any(not isinstance(f, str) for f in selected) or type(answer.get("unknown")) is not bool:
            raise RunStopped("invalid_witness_answer_shape")
        ignored = []
        if answer["unknown"]:
            # Same explicit V7 rendering rule, never rewrite the accepted journal
            # response or convert a known assertion into an unknown one.
            ignored, selected = selected, []
        elif not set(selected) <= set(facts):
            raise RunStopped("witness_attempted_unknown_fact_id")
        selected = list(dict.fromkeys(selected))
        text = "\n".join(facts[f] for f in selected) if selected else card.get("unknown_response", "来源未记载，不能补充事实。")
        if ignored:
            self.emit("witness_unknown_rendering_audit", text="unknown=true，仅呈现既定未知回答；原模型回复保留。", audience=["reviewer"],
                      witness=witness, ignored_unused_fact_ids=ignored, factual_content_added=False)
        return self.emit("private_consultation" if private else "testimony", witness, text, selected,
                         audience=["R_DEFENSE", "R_DEFENDANT"] if private else COURT,
                         question=question, rendering="exact_authorized_role_fact_text", unknown=not bool(selected),
                         environment_version=VERSION)

    def ask_question(self, role, action):
        witness = action.get("target")
        spec = self.case.procedure.get("witnesses", {}).get(witness, {})
        if (witness not in self.case.stages[self.stage].get("questionable_role_ids", [])
                or spec.get("mode") != "source_bound_live"
                or self.case.stages[self.stage].get("simulation_round") not in spec.get("rounds", [])):
            raise ValueError("witness_not_questionable")
        if witness == "R_DEFENDANT" and not self.testimony_elected:
            if role != "R_DEFENSE":
                raise ValueError("cannot_compel_defendant_testimony")
            answer = self.backend.decide("R_DEFENDANT", BASE_PROMPT + "是否自愿选择作证？可以拒绝。不得创造新事实。",
                {"task": "testimony_election", "stage_id": self.stage, "role_card": self.role_card("R_DEFENDANT"),
                 "defense_request": action["text"],
                 "private_consultation": [deepcopy(e) for e in self.events if e["kind"] in {"private_consultation", "private_consultation_question"}]}, ELECTION_SCHEMA)
            if not _validate_value(answer, ELECTION_SCHEMA, ELECTION_SCHEMA):
                raise RunStopped("invalid_defendant_election")
            self.testimony_elected = answer["elect_to_testify"]
            self.emit("testimony_election", "R_DEFENDANT", "elected" if self.testimony_elected else "declined")
            if not self.testimony_elected:
                return
        if self.active_witness != witness:
            self.active_witness = witness
            self.emit("call_witness", "R_JUDGE", "依程序允许该证人接受询问。", witness=witness)
        calling = self.witness_calling_parties.setdefault((self.stage, witness), role)
        question = self.emit("question", role, action["text"], action["references"], witness=witness,
                             examination_mode="direct" if role == calling else "cross")
        if self.objection_window(role, question):
            return
        answer = self.witness_answer(witness, action["text"])
        answer["meta"].update(source_question_event_id=question["event_id"], examination_mode=question["meta"]["examination_mode"])
        if self.objection_window(role, answer):
            return
        if self.case.stages[self.stage].get("simulation_round") in self.case.procedure["jury_evidence_rounds"]:
            evidence = self.emit("evidence", role, answer["text"], [answer["event_id"]], source_type="testimony",
                                 purpose=action.get("purpose") or "case_facts", source_event=answer["event_id"], question=question["text"])
            self.rule_evidence(evidence)

    def handle_objection(self, role, action):
        # Round 3 may challenge retained round-2 records.  Their original text,
        # source IDs and old rulings remain, followed by a new ruling event.
        if role not in LAWYERS or "object" not in self.case.stages[self.stage]["allowed_actions"]:
            raise ValueError("objection_not_authorized")
        target = self.event(action.get("target"))
        retained = bool(target and self.stage == "r3_clarification" and target["round"] == 2)
        if (not target or role not in target["audience"]
                or (target["stage_id"] != self.stage and not retained)
                or target["kind"] not in {"question", "testimony", "evidence"}):
            raise ValueError("invalid_objection_target")
        spec = next((o for o in self.case.procedure.get("objections", []) if o["objection_id"] == action.get("objection_id")), None)
        if not spec:
            raise ValueError("unknown_objection")
        details = self._details(action)
        details["target_event_id"] = action["target"]
        if spec["objection_id"] == "O_LEADING" and target["meta"].get("examination_mode"):
            details["examination_mode"] = target["meta"]["examination_mode"]
        if action.get("references"):
            details["legal_source_id"] = action["references"][0]
        missing = [k for k in spec["required_fields"] if not details.get(k)]
        if missing:
            raise ValueError("missing_objection_fields:" + ",".join(missing))
        if action.get("references") and not set(action["references"]) <= self.legal_ids():
            raise ValueError("invalid_objection_law")
        event = self.emit("objection", role, action["text"], action["references"],
                          target=action["target"], objection_id=spec["objection_id"], details=details,
                          retained_prior_round_record=retained)
        key = event["event_id"]
        self.pending_objections.add(key)
        decisions = ["sustain", "overrule"]
        if retained and target["kind"] == "evidence":
            decisions.append("limited_use")
        ruling = self.judge("rule_objection", {"target": target, "objection": event,
            "last_ruling": self.last_ruling(target["event_id"]),
            "retained_prior_round_record": retained}, decisions)
        self.emit("objection_ruling", "R_JUDGE", ruling["reason"], ruling["legal_source_ids"],
                  target=action["target"], decision=ruling["decision"],
                  permitted_purposes=ruling["permitted_purposes"], human_review_required=True)
        self.pending_objections.discard(key)
        if ruling["decision"] == "limited_use":
            self.rulings.append({**ruling, "target_event_id": target["event_id"],
                                "ruling_stage": self.stage, "human_review_required": True})
            self.emit("evidence_ruling", "R_JUDGE", ruling["reason"], ruling["legal_source_ids"],
                      target=target["event_id"], decision="limited_use",
                      permitted_purposes=ruling["permitted_purposes"], human_review_required=True)
            return False
        if ruling["decision"] != "sustain":
            return False
        target["meta"]["stricken"] = True
        if target["kind"] == "evidence":
            self.rulings.append({**ruling, "decision": "excluded", "target_event_id": target["event_id"],
                                "ruling_stage": self.stage, "human_review_required": True})
        struck_ids = {target["event_id"]}
        for row in self.events:
            if row["kind"] == "testimony" and row["meta"].get("source_question_event_id") == target["event_id"]:
                row["meta"]["stricken"] = True
                struck_ids.add(row["event_id"])
        for row in self.events:
            if row["kind"] == "evidence" and struck_ids.intersection(row["references"]):
                row["meta"]["stricken"] = True
                self.rulings.append({**ruling, "decision": "excluded", "target_event_id": row["event_id"],
                                    "ruling_stage": self.stage, "human_review_required": True})
        return True

    def offer(self, role, action):
        if not action.get("purpose"):
            raise ValueError("evidence_purpose_required")
        source = self.source(role, action.get("target"))
        if source["citation_id"] in self.exclusions:
            raise ValueError("source_excluded_by_motion")
        original = self.event(source["citation_id"])
        if original and original["meta"].get("stricken"):
            raise ValueError("source_testimony_stricken")
        spec = self.case.procedure.get("summary_presentation", {})
        material = original is None
        merits = self.case.stages[self.stage].get("simulation_round") in self.case.procedure["jury_evidence_rounds"]
        if material and merits and (self.stage not in spec.get("stage_ids", []) or source["citation_id"] not in spec.get("permitted_paragraph_ids", [])):
            raise ValueError("source_summary_wrong_stage_or_paragraph")
        existing = next((e for e in reversed(self.events) if e["kind"] == "evidence"
                         and e["stage_id"] == self.stage and e["references"] == [source["citation_id"]]
                         and e["meta"].get("purpose") == action["purpose"]
                         and (self.last_ruling(e["event_id"]) or {}).get("decision") == "pending_foundation"), None)
        if existing:
            event = existing
            self.emit("foundation_submission", role, action["text"], action["references"], target=event["event_id"])
        else:
            presentation = None
            if material and merits:
                presentation = self.emit("source_summary_presentation", "trusted_orchestrator", source["text"], [source["citation_id"]],
                    content_role_id=spec["content_role_id"], requested_by=role,
                    summary_label=spec["required_label"], source_form=spec["source_form"],
                    live_testimony=False, automatic_admission=False)
            event = self.emit("evidence", role, source["text"], [source["citation_id"]],
                              source_type="source_bound_summary" if material else "testimony",
                              summary_label=spec.get("required_label") if material else None,
                              purpose=action["purpose"], source=source,
                              presentation_event_id=presentation["event_id"] if presentation else None)
        if self.objection_window(role, event):
            return
        if "rule_evidence" in self.case.stages[self.stage]["allowed_actions"]:
            self.rule_evidence(event)
        elif event["event_id"] not in self.pending_evidence:
            self.pending_evidence.append(event["event_id"])

    def jury_record(self):
        rounds = set(self.case.procedure["jury_evidence_rounds"])
        result = []
        for event in self.events:
            if event["kind"] == "delivered_instruction" and event["stage_id"] == self.case.procedure["jury_instruction_stage_id"]:
                result.append(deepcopy(event))
            elif event["round"] in rounds and event["kind"] == "evidence":
                ruling = self.last_ruling(event["event_id"])
                if ruling and ruling["decision"] in {"admitted", "limited_use"} and not event["meta"].get("stricken"):
                    item = deepcopy(event)
                    item["meta"].update(permitted_purposes=deepcopy(ruling["permitted_purposes"]),
                                        admission_state=ruling["decision"], limitation=ruling["reason"])
                    result.append(item)
        evidence = [e for e in result if e["kind"] == "evidence"]
        for event in self.events:
            if (event["round"] in rounds and event["kind"] == "closing_argument" and event["actor"] in LAWYERS
                    and not event["meta"].get("stricken") and set(COURT) <= set(event["audience"])):
                item = deepcopy(event)
                item["meta"].update(not_evidence=True, argument_text_policy="full_unedited_closing_argument",
                    declared_reference_check=self.argument_reference_check(event, evidence),
                    instruction="律师陈词不是证据；不得将复述的未准入或已排除事实用于认定案情。可考虑对证据缺口及证明责任的论证，引用提示本身不判定论证有效性。")
                result.append(item)
        return result

    def _close_stage(self):
        requirements = self.case.stages[self.stage]["exit_requirements"]
        resolved = set(requirements.get("resolved_queues", []))
        deferred = requirements.get("deferred_queues", {})
        if "motions" in resolved:
            self.resolve_round_one()
        if "evidence_rulings" in resolved:
            for eid in list(self.pending_evidence):
                self.rule_evidence(self.event(eid))
                self.pending_evidence.remove(eid)
            self.close_pending_foundation()
        queues = {"motions": list(self.pending_motions),
                  "evidence_rulings": list(self.pending_evidence) + self.unresolved_foundation(),
                  "objections": list(self.pending_objections)}
        if (set(resolved) | set(deferred)) - set(queues):
            raise RunStopped("unknown_stage_exit_queue")
        for name, entries in queues.items():
            if not entries:
                continue
            if name in resolved:
                raise RunStopped("stage_exit_unresolved_" + name)
            destination = deferred.get(name)
            if destination not in self.case.stages or self._stage_order.index(destination) <= self._stage_order.index(self.stage):
                raise RunStopped("stage_exit_unaccounted_" + name)
        self.emit("stage_completed", text=self.stage, resolved_queues=sorted(resolved),
                  deferred_queues=deepcopy(deferred), queue_counts={k: len(v) for k, v in queues.items()})
        self._completed_stages.append(self.stage)

    def _verdict(self):
        if self.pending_motions or self.pending_evidence or self.pending_objections or self.unresolved_foundation():
            raise RunStopped("unresolved_rulings_before_verdict")
        if not any(e["kind"] == "delivered_instruction" and e["stage_id"] == self.case.procedure["jury_instruction_stage_id"] for e in self.events):
            raise RunStopped("third_round_instructions_not_delivered")
        self.review_session()
        schema = deepcopy(VERDICT_SCHEMA)
        ids = [c["charge_id"] for c in self.case.manifest["charges"]]
        schema["properties"]["charges"].update(minItems=len(ids), maxItems=len(ids))
        schema["properties"]["charges"]["items"]["properties"]["charge_id"] = {"type": "string", "enum": ids}
        answer = self.backend.decide("R_JURY", BASE_PROMPT +
            "逐项作出模拟裁决，事实只依获准证据和最新限定用途，律师陈词不是证据。"
            "第一轮未准入记录、被排除事实、私下咨询和原案结果不可用于判断。"
            "遵守第三轮已交付指示，不能以沉默推断有罪；每个charge_id恰好裁决一次。",
            self.context("R_JURY", "verdict"), schema)
        if not _validate_value(answer, schema, schema) or sorted(c["charge_id"] for c in answer["charges"]) != sorted(ids):
            raise RunStopped("verdict_charge_ids_mismatch")
        jury_ids = {e["event_id"] for e in self.jury_record()}
        if any(not set(c["record_event_ids"]) <= jury_ids for c in answer["charges"]):
            raise RunStopped("verdict_cites_unavailable_record")
        if any(not c["reason"].strip() for c in answer["charges"]):
            raise RunStopped("verdict_reason_missing")
        self.verdict = answer
        self.emit("simulated_verdict", "R_JURY", json.dumps(answer, ensure_ascii=False), audience=COURT + ["R_JURY"], is_ground_truth=False)

    def run(self, *, resume_completed_stage=False, resume_foundation_finalization=False):
        if resume_completed_stage or resume_foundation_finalization:
            raise RunStopped("three_round_uses_exact_decision_journal_replay_only")
        self.status, self.stop_reason = "running", None
        try:
            if not self.pretrial_completed:
                self.stage = PRETRIAL
                self.emit("stage_started", text=PRETRIAL)
                for _ in range(self.workflow_spec["pretrial_actions"]):
                    self.pretrial_actions += 1
                    try:
                        if self.lawyer_turn("R_DEFENSE") == "pass":
                            break
                    except ValueError as exc:
                        self.emit("action_rejected", "R_DEFENSE", str(exc), audience=["R_DEFENSE"])
                else:
                    self.emit("stage_budget_reached", "R_DEFENSE", "庭前准备行动预算用尽；保留实际产物并进入庭审。", audience=["R_DEFENSE"], count=self.pretrial_actions)
                self.pretrial_completed = True
                self.emit("pretrial_completed", "R_DEFENSE", audience=["R_DEFENSE"], actions=self.pretrial_actions, artifacts=self.artifacts.visible("R_DEFENSE"))
                self.stage = self._base_initial_stage
            while self.stage != "ended":
                if self.stage in self._completed_stages:
                    raise RunStopped("stage_chain_cycle")
                self.active_witness = None
                self.emit("stage_started", text=self.stage)
                self.release_stage_materials()
                allowed = self.case.stages[self.stage]["allowed_actions"]
                if self.stage == self.case.procedure["jury_instruction_stage_id"]:
                    self.deliver_instructions()
                elif "record_verdict" in allowed:
                    self._verdict()
                elif "rule_motion" in allowed and not any(r in LAWYERS for a in allowed for r in self.case.procedure["action_role_ids"].get(a, [])):
                    self.resolve_round_one()
                else:
                    self.run_lawyer_stage()
                self._close_stage()
                self.stage = self.case.stages[self.stage]["next_stage_id"]
            if self.verdict is None or "r3_verdict" not in self._completed_stages:
                raise RunStopped("missing_actual_third_round_verdict")
            if len(self.artifacts.entries) < len(self.artifacts.expected):
                raise RunStopped("unreplayed_artifact_history")
            self.status = "completed_development_run"
        except Exception as exc:
            self.status, self.verdict = "incomplete", None
            self.stop_reason = getattr(exc, "code", type(exc).__name__) + ": " + str(exc)[:180]
            self.emit("run_stopped", text=self.stop_reason, audience=["reviewer"])
        return self.summary()

    def summary(self):
        result = CourtEngine.summary(self)
        result.update(environment_version=VERSION, simulation_round_count=3,
                      completed_stages=list(self._completed_stages),
                      validated_release_events=sorted(self.validated_events),
                      rubric_visible_to_court_roles=False, workflow_version=WORKFLOW_VERSION,
                      pretrial_actions=self.pretrial_actions, pretrial_completed=self.pretrial_completed,
                      workflow_artifact_count=len(self.artifacts.entries),
                      workflow_scope="supplied_file_processing_and_frozen_record_checks_no_new_historical_facts")
        result["limitations"].append("source_excerpt_simulation_not_full_historical_record")
        return result

    def save(self, out):
        CourtEngine.save(self, out)
        out = Path(out)
        (out / "artifact_manifest.json").write_text(canonical({"version": "workflow-artifacts-v3", "artifacts": self.artifacts.entries,
            "artifact_root_relative": os.path.relpath(self.artifacts.root, out.resolve()),
            "replay_consumed_prior_artifacts": len(self.artifacts.entries) >= len(self.artifacts.expected)}), encoding="utf-8")
        (out / "workflow_summary.json").write_text(canonical({"version": WORKFLOW_VERSION,
            "workflow_spec_sha256": sha(canonical(self.workflow_spec).encode()), "pretrial_actions": self.pretrial_actions,
            "required_artifacts": self.workflow_spec["required_artifacts"],
            "missing_defense_artifacts": sorted(set(self.workflow_spec["required_artifacts"]) - {a["name"] for a in self.artifacts.entries if a["actor"] == "R_DEFENSE" and a["kind"] == "lawyer_output"}),
            "hidden_service_answers_in_actor_context": False, "new_historical_facts": False,
            "network_used_by_workflow_tools": False}), encoding="utf-8")
        manifest = self.case.author_environment_manifest()
        manifest.update(environment_version=VERSION, adapter_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        (out / "environment_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
