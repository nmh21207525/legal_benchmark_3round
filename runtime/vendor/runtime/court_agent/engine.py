"""Trusted orchestration: no model can set identity, stage, ACL or jury feed.

This is a development runner. Substantive LLM rulings are explicitly provisional;
the engine enforces their mechanical effects, not their legal correctness.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .schema import (BASE_PROMPT, LAWYER_PROMPT, WITNESS_SCHEMA, QUERY_SCHEMA,
                     ELECTION_SCHEMA, VERDICT_SCHEMA, action_schema, decision_schema,
                     foundation_statement_schema, final_foundation_schema)
from .llm import BackendError, _validate_value
from .search import ControlledSearch

LAWYERS = ("R_PROSECUTOR", "R_DEFENSE")
COURT = ["R_DEFENSE", "R_PROSECUTOR", "R_JUDGE"]
PROTOCOL_VERSION = "foundation_closure_v1"
FOUNDATION_OUTPUT_CONTRACT = "legal_id_enum_v1"


class RunStopped(RuntimeError):
    """Incomplete runs never receive a fabricated verdict or benchmark score."""


class CourtEngine:
    def __init__(self, case, backend, config):
        self.case, self.backend, self.config = case, backend, config
        self.search = ControlledSearch(case)
        self.stage = case.procedure["initial_stage_id"]
        self.disclosed = set(case.disclosed_initial())
        self.events, self.rulings, self.pending_motions, self.pending_evidence = [], [], [], []
        self.exclusions, self.memories = {}, {r: [] for r in case.roles}
        self.review, self.verdict = None, None
        self.active_witness = None
        self.witness_calling_parties = {}
        self.testimony_elected = False
        self.status = "initialized"
        self.action_count = 0
        self.protocol_version = PROTOCOL_VERSION
        self.recovery = None
        self.stop_reason = None
        self.foundation_diagnostics = []

    def emit(self, kind, actor="trusted_orchestrator", text="", references=None,
             audience=None, **meta):
        event = {"event_id": f"E{len(self.events)+1:05d}", "stage_id": self.stage,
                 "round": self.case.stages.get(self.stage, {}).get("simulation_round"),
                 "kind": kind, "actor": actor, "text": text,
                 "references": references or [], "audience": list(audience or COURT),
                 "meta": meta}
        self.events.append(event)
        return event

    def event(self, event_id):
        return next((e for e in self.events if e["event_id"] == event_id), None)

    def visible_record(self, role):
        if role == "R_JURY":
            return self.jury_record()
        return [copy.deepcopy(e) for e in self.events if role in e["audience"]]

    def jury_record(self):
        """Final evidence status controls evidence, not the merits of arguments.

        Closing arguments are retained verbatim and labelled as non-evidence.
        Reference warnings do not decide whether an argument is legally sound:
        discussing an absence of evidence or an exclusion can be legitimate.
        """
        result = []
        for e in self.events:
            if e["kind"] == "delivered_instruction":
                result.append(copy.deepcopy(e))
            elif e["round"] == 2 and e["kind"] == "evidence":
                last = self.last_ruling(e["event_id"])
                if (last and last["decision"] in {"admitted", "limited_use"}
                        and not e["meta"].get("stricken")):
                    clean = copy.deepcopy(e)
                    clean["meta"]["permitted_purposes"] = last["permitted_purposes"]
                    clean["meta"]["admission_state"] = last["decision"]
                    clean["meta"]["limitation"] = last["reason"]
                    result.append(clean)
        evidence = [e for e in result if e["kind"] == "evidence"]
        for event in self.events:
            if (event["round"] == 2 and event["kind"] == "closing_argument"
                    and event["actor"] in LAWYERS and not event["meta"].get("stricken")
                    and all(role in event["audience"] for role in COURT)):
                argument = copy.deepcopy(event)
                argument["meta"].update(
                    not_evidence=True, argument_text_policy="full_unedited_closing_argument",
                    declared_reference_check=self.argument_reference_check(event, evidence),
                    instruction=("律师论述不是证据。引用未准入或已排除材料不会使其成为证据；"
                                 "不得将其中复述的未准入事实用于认定案情。引用警告不等于论证无效，"
                                 "仍应区分指出证据缺口、讨论排除效果与把排除内容当事实使用。"))
                result.append(argument)
        return result

    def argument_reference_check(self, event, admitted_evidence):
        """Check declared IDs only; do not infer support from citation presence."""
        admitted = {e["event_id"] for e in admitted_evidence}
        admitted.update(ref for e in admitted_evidence for ref in e["references"])
        excluded = set(self.exclusions)
        for item in self.events:
            ruling = self.last_ruling(item["event_id"])
            if item["meta"].get("stricken") or (ruling and ruling["decision"] == "excluded"):
                excluded.add(item["event_id"])
                if item["kind"] == "evidence":
                    excluded.update(item["references"])
        legal = self.legal_ids()
        unavailable = [ref for ref in event["references"] if ref not in admitted | legal]
        return {"check_scope": "declared_reference_ids_only",
                "references_outside_admitted_record_or_law": unavailable,
                "references_to_excluded_or_stricken_sources": [r for r in unavailable if r in excluded],
                "semantic_support": "not_scored",
                "argument_validity": "not_determined_by_reference_check"}

    def last_ruling(self, event_id):
        return next((r for r in reversed(self.rulings) if r["target_event_id"] == event_id), None)

    def context(self, role, task=None):
        brief = self.case.public_brief()
        if role == "R_JURY":
            brief = {k: brief[k] for k in ("case_id", "charges") if k in brief}
        result = {"role_id": role, "stage_id": self.stage, "task": task,
                  "role_card": self.case.role_card(role), "case": brief,
                  "record": self.visible_record(role)}
        if role in LAWYERS or role == "R_JUDGE":
            result["disclosed_materials"] = self.catalog()
            result["legal_catalog"] = [
                {k: d[k] for k in ("citation_id", "title") if k in d}
                for d in self.case.legal_documents()]
            result["lawyer_memory"] = copy.deepcopy(self.memories.get(role, []))
            result["admitted_for_jury"] = [
                {"event_id": e["event_id"], "references": e["references"],
                 "permitted_purposes": e["meta"].get("permitted_purposes", [])}
                for e in self.jury_record() if e["kind"] == "evidence"]
        return result

    def catalog(self):
        out = []
        for mid in sorted(self.disclosed):
            for p in self.case.material_paragraphs(mid):
                out.append({k: p[k] for k in ("material_id", "citation_id", "title") if k in p})
        return out

    def allowed_actions(self, role):
        allowed = [a for a in self.case.stages[self.stage]["allowed_actions"]
                   if role in self.case.procedure["action_role_ids"].get(a, [])]
        # Research is a capability of each lawyer, available between courtroom actions.
        allowed += ["web_search", "pass"]
        return sorted(set(allowed))

    def lawyer_turn(self, role):
        allowed = self.allowed_actions(role)
        ctx = self.context(role, "choose_action")
        ctx.update(allowed_actions=allowed,
                   questionable_role_ids=self.case.stages[self.stage].get("questionable_role_ids", []),
                   objection_catalog=self.objection_catalog())
        a = self.backend.decide(role, LAWYER_PROMPT, ctx, action_schema(allowed))
        if a.get("action") not in allowed:
            raise ValueError("action_not_allowed_in_stage")
        self.apply(role, a)
        return a["action"]

    def objection_catalog(self):
        return [{k: o[k] for k in ("objection_id", "trigger", "required_fields") if k in o}
                for o in self.case.procedure.get("objections", [])]

    def source(self, role, reference):
        # Only disclosed, stage-authorized material paragraphs or public witness records.
        for rule in self.case.permissions["material_rules"]:
            mid = rule["material_id"]
            if mid not in self.disclosed or role not in rule["allowed_roles"]:
                continue
            order = list(self.case.stages)
            if order.index(self.stage) < order.index(rule["minimum_stage"]):
                continue
            for p in self.case.material_paragraphs(mid):
                if p["citation_id"] == reference and reference in rule["paragraph_ids"]:
                    return p
        e = self.event(reference)
        if e and role in e["audience"] and e["kind"] == "testimony":
            return {"citation_id": e["event_id"], "text": e["text"],
                    "title": "source-bound testimony", "origin_stage": e["stage_id"]}
        raise ValueError("source_not_authorized")

    def legal_ids(self):
        ids = set()
        for row in self.case.legal_documents():
            ids.update([row["legal_source_id"], row["citation_id"]])
        return ids

    def legal_texts(self):
        # Judge's authorized law packet; not the hidden answer or role knowledge.
        return self.case.legal_documents()

    def judge(self, task, packet, decisions):
        ctx = self.context("R_JUDGE", task)
        ctx.update(packet=packet, legal_sources=self.legal_texts(),
                   motion_rules=self.case.procedure.get("motion_rules", {}))
        prompt = BASE_PROMPT + """你是法官。依据已披露材料及获授权法律提出裁定。
不得推测证人未知事实。区分材料披露和证据准入，第一轮记录不能自动作为第二轮证据。
source_bound_opinion_summary 是判决摘要，不是真实警察报告或逐字笔录，不具有当然真实性。
admitted 表示允许用于明确目的；limited_use 必须限制用途；基础未补齐可 pending_foundation。
只处理 packet 指定对象；法律依据必须使用提供的 legal_source_ids。
reason 是简短公开理由；你的实体法律判断标记为待人工核验，不能作为评分标准答案。
"""
        r = self.backend.decide("R_JUDGE", prompt, ctx, decision_schema(decisions))
        if r.get("decision") not in decisions or not set(r.get("legal_source_ids", [])) <= self.legal_ids():
            raise RunStopped("invalid_judge_decision_or_citation")
        if not r.get("reason") or not r.get("legal_source_ids"):
            raise RunStopped("judge_ruling_requires_reasons_and_law")
        return r

    def apply(self, role, a):
        kind = a["action"]
        if role not in LAWYERS or kind not in self.allowed_actions(role):
            raise ValueError("permission_denied")
        if kind == "pass":
            self.emit("pass", role, audience=[role])
        elif kind == "web_search":
            query = a.get("query") or a.get("text")
            if not query:
                raise ValueError("empty_query")
            rewritten = query
            if self.config.get("research_assistant", True):
                result = self.backend.decide("R_RESEARCH", BASE_PROMPT +
                    "你是同一律师团队的检索助手。只改写检索词，不回答案情；保留原问题，不增加假定事实。",
                    {"task": "rewrite_query", "query": query,
                     "authorized_catalog": self.catalog()}, QUERY_SCHEMA)
                rewritten = result["query"]
            result = self.search.search(role_id=role, stage_id=self.stage,
                disclosed_ids=self.disclosed, query=rewritten, scope=a.get("scope") or "all",
                date_from=a.get("date_from"), date_to=a.get("date_to"), top_k=5,
                record=self.visible_record(role))
            self.memories[role].append({"tool": "web_search", "original_query": query,
                                        "query": rewritten, "result": result})
            self.emit("tool_result", role, audience=[role], tool="web_search",
                      query=query, rewritten_query=rewritten, result=result)
        elif kind == "inspect_disclosed_material":
            mid = a.get("target")
            if mid not in self.disclosed:
                raise ValueError("material_not_authorized")
            paragraphs = self.case.material_paragraphs(mid)
            safe = [self.source(role, p["citation_id"]) for p in paragraphs]
            self.memories[role].append({"tool": "inspect_disclosed_material", "result": safe})
            self.emit("tool_result", role, audience=[role], tool=kind, result=safe)
        elif kind == "request_disclosure":
            self.disclose(role, a)
        elif kind == "consult_client":
            if role != "R_DEFENSE":
                raise ValueError("private_consultation_denied")
            self.witness_answer("R_DEFENDANT", a["text"], private=True)
        elif kind == "file_motion":
            source = self.source(role, a.get("target"))
            if not a.get("references") or not set(a["references"]) <= self.legal_ids():
                raise ValueError("motion_requires_legal_citations")
            e = self.emit(kind, role, a["text"], a["references"],
                          target=source["citation_id"], purpose=a.get("purpose"), source=source)
            self.pending_motions.append(e["event_id"])
            other = "R_DEFENSE" if role == "R_PROSECUTOR" else "R_PROSECUTOR"
            ctx = self.context(other, "motion_response")
            ctx["motion"] = e
            response = self.backend.decide(other, LAWYER_PROMPT, ctx,
                                           action_schema(["respond_to_motion", "pass"]))
            if response["action"] == "respond_to_motion" and response.get("target") == e["event_id"]:
                self.emit("respond_to_motion", other, response["text"], response["references"], target=e["event_id"])
        elif kind == "respond_to_motion":
            if a.get("target") not in self.pending_motions:
                raise ValueError("no_matching_pending_motion")
            self.emit(kind, role, a["text"], a["references"], target=a["target"])
        elif kind == "ask_question":
            self.ask_question(role, a)
        elif kind == "offer_evidence":
            self.offer(role, a)
        elif kind == "object":
            self.handle_objection(role, a)
        elif kind in {"opening_statement", "closing_argument"}:
            # Retain the lawyer's actual performance; citation checks never rewrite it.
            self.emit(kind, role, a["text"], a["references"], not_evidence=True)
        elif kind == "prepare_examination":
            self.emit(kind, role, a["text"], a["references"], audience=[role])
        else:
            raise ValueError("unsupported_action")

    def disclose(self, role, a):
        target = a.get("target")
        if target in self.disclosed:
            self.emit("disclosure_notice", role, "已披露，无状态变化。", [target])
            return
        rule = next((r for r in self.case.procedure.get("disclosure_rules", [])
                     if r["material_id"] == target), None)
        order = list(self.case.stages)
        if (not rule or role not in rule.get("valid_request_actor_role_ids", [])
                or order.index(self.stage) < order.index(rule["earliest_stage"])):
            raise ValueError("disclosure_not_available")
        self.disclosed.add(target)
        self.emit("disclosure", role, "同一原文已同时向获授权各方披露。", [target],
                  audience=rule["release_to_role_ids"], request_validated=True)

    def objection_window(self, actor, target):
        other = "R_DEFENSE" if actor == "R_PROSECUTOR" else "R_PROSECUTOR"
        ctx = self.context(other, "objection_window")
        ctx.update(target_event=target, objection_catalog=self.objection_catalog())
        a = self.backend.decide(other, LAWYER_PROMPT +
            "现在只处理这个 target_event；有适用异议则 object，否则 pass。",
            ctx, action_schema(["object", "pass"]))
        if a["action"] == "object":
            if a.get("target") != target["event_id"]:
                self.emit("action_rejected", other, "objection_target_mismatch", audience=[other])
                return False
            try:
                return self.handle_objection(other, a)
            except ValueError as ex:
                self.emit("action_rejected", other, str(ex), audience=[other])
        return False

    def handle_objection(self, role, a):
        if role not in LAWYERS or "object" not in self.case.stages[self.stage]["allowed_actions"]:
            raise ValueError("objection_not_authorized")
        target = self.event(a.get("target"))
        if (not target or role not in target["audience"] or target["stage_id"] != self.stage
                or target["kind"] not in {"question", "testimony", "evidence"}):
            raise ValueError("invalid_objection_target")
        catalog = self.case.procedure.get("objections", [])
        spec = next((o for o in catalog if o["objection_id"] == a.get("objection_id")), None)
        if not spec:
            raise ValueError("unknown_objection")
        details = {p["key"]: p["value"] for p in a.get("details", [])}
        details["target_event_id"] = a["target"]
        if spec["objection_id"] == "O_LEADING" and target["meta"].get("examination_mode"):
            details["examination_mode"] = target["meta"]["examination_mode"]
        if a.get("references"):
            details["legal_source_id"] = a["references"][0]
        missing = [k for k in spec["required_fields"] if not details.get(k)]
        if missing:
            raise ValueError("missing_objection_fields:" + ",".join(missing))
        if a.get("references") and not set(a["references"]) <= self.legal_ids():
            raise ValueError("invalid_objection_law")
        event = self.emit("objection", role, a["text"], a["references"],
                          target=a["target"], objection_id=spec["objection_id"], details=details)
        r = self.judge("rule_objection", {"target": target, "objection": event}, ["sustain", "overrule"])
        self.emit("objection_ruling", "R_JUDGE", r["reason"], r["legal_source_ids"],
                  target=a["target"], decision=r["decision"], human_review_required=True)
        if r["decision"] == "sustain":
            target["meta"]["stricken"] = True
            if target["kind"] == "evidence":
                self.rulings.append({**r, "decision": "excluded", "target_event_id": target["event_id"]})
            # If a testimony has already been offered, revoke all derived evidence records.
            struck_ids = {target["event_id"]}
            for e in self.events:
                if e["kind"] == "testimony" and e["meta"].get("source_question_event_id") == target["event_id"]:
                    e["meta"]["stricken"] = True
                    struck_ids.add(e["event_id"])
            for e in self.events:
                if e["kind"] == "evidence" and struck_ids.intersection(e["references"]):
                    e["meta"]["stricken"] = True
                    self.rulings.append({**r, "decision": "excluded", "target_event_id": e["event_id"]})
            return True
        return False

    def witness_answer(self, witness, question, private=False):
        card = self.case.role_card(witness)
        ctx = {"task": "source_bound_answer", "role_id": witness, "stage_id": self.stage,
               "role_card": card, "question": question}
        answer = self.backend.decide(witness, BASE_PROMPT +
            "只选择回答问题所需、自己知情的 fact_ids；无来源则 unknown=true。不得新增事实。",
            ctx, WITNESS_SCHEMA)
        facts = {p["fact_id"]: p["content"] for p in card.get("knowledge_points", [])}
        selected = answer.get("fact_ids", [])
        if not isinstance(selected, list) or not set(selected) <= set(facts):
            raise RunStopped("witness_attempted_unknown_fact_id")
        if answer.get("unknown") or not selected:
            text = card.get("unknown_response", "来源未记载，不能补充事实。")
            selected = []
        else:
            # Extractive rendering is deliberate: wording generation cannot invent facts.
            text = "\n".join(facts[f] for f in dict.fromkeys(selected))
        audience = ["R_DEFENSE", "R_DEFENDANT"] if private else COURT
        return self.emit("private_consultation" if private else "testimony", witness, text,
                         selected, audience=audience, question=question,
                         rendering="exact_role_fact_text", unknown=not bool(selected))

    def ask_question(self, role, a):
        witness = a.get("target")
        eligible = self.case.stages[self.stage].get("questionable_role_ids", [])
        if witness not in eligible or self.case.roles[witness]["role_type"] == "recorded_witness":
            raise ValueError("witness_not_questionable")
        if witness == "R_DEFENDANT" and not self.testimony_elected:
            if role != "R_DEFENSE":
                raise ValueError("cannot_compel_defendant_testimony")
            elected = self.backend.decide("R_DEFENDANT", BASE_PROMPT +
                "是否自愿选择作证？可以拒绝。不得创造新事实。",
                {"task": "testimony_election", "role_card": self.case.role_card("R_DEFENDANT"),
                 "defense_request": a["text"],
                 "private_consultation": [e for e in self.events if e["kind"] == "private_consultation"]},
                ELECTION_SCHEMA)["elect_to_testify"]
            self.emit("testimony_election", "R_DEFENDANT", "elected" if elected else "declined")
            if not elected:
                return
            self.testimony_elected = True
        if self.active_witness != witness:
            self.active_witness = witness
            self.emit("call_witness", "R_JUDGE", "依程序允许该证人接受询问。", witness=witness)
        calling_party = self.witness_calling_parties.setdefault((self.stage, witness), role)
        question = self.emit("question", role, a["text"], a["references"], witness=witness,
                             examination_mode="direct" if role == calling_party else "cross")
        if self.objection_window(role, question):
            return  # Sustained question: no witness call to model.
        answer = self.witness_answer(witness, a["text"])
        answer["meta"]["source_question_event_id"] = question["event_id"]
        answer["meta"]["examination_mode"] = question["meta"]["examination_mode"]
        if self.objection_window(role, answer):
            return
        if self.case.stages[self.stage].get("simulation_round") == 2:
            evidence = self.emit("evidence", role, answer["text"], [answer["event_id"]],
                                 source_type="testimony", purpose=a.get("purpose") or "case_facts",
                                 source_event=answer["event_id"], question=question["text"])
            self.rule_evidence(evidence)

    def offer(self, role, a):
        if not a.get("purpose"):
            raise ValueError("evidence_purpose_required")
        source = self.source(role, a.get("target"))
        if source["citation_id"] in self.exclusions:
            raise ValueError("source_excluded_by_motion")
        original = self.event(source["citation_id"])
        if original and original["meta"].get("stricken"):
            raise ValueError("source_testimony_stricken")
        special = self.case.procedure.get("recorded_presentation", {})
        recorded = source["citation_id"] in special.get("permitted_paragraph_ids", [])
        if recorded and self.case.stages[self.stage].get("simulation_round") == 2:
            if self.stage != special["stage_id"]:
                raise ValueError("recorded_summary_wrong_stage")
        # Reoffering the same pending item in the same round is a bounded
        # opportunity to supply foundation and request a new ruling.
        event = next((e for e in reversed(self.events) if e["kind"] == "evidence"
                      and e["stage_id"] == self.stage and e["references"] == [source["citation_id"]]
                      and e["meta"].get("purpose") == a["purpose"]
                      and (self.last_ruling(e["event_id"]) or {}).get("decision") == "pending_foundation"), None)
        if event:
            self.emit("foundation_submission", role, a["text"], a["references"], target=event["event_id"])
        else:
            event = self.emit("evidence", role, source["text"], [source["citation_id"]],
                              source_type="recorded_testimony_summary" if recorded else "source_bound_summary",
                              summary_label="判决记载的证言摘要；不是原始录像或逐字笔录" if recorded else None,
                              purpose=a["purpose"], source=source)
        if self.objection_window(role, event):
            return
        if "rule_evidence" in self.case.stages[self.stage]["allowed_actions"]:
            self.rule_evidence(event)
        else:
            self.pending_evidence.append(event["event_id"])

    def rule_evidence(self, event):
        if event["meta"].get("stricken"):
            return
        r = self.judge("rule_evidence", event,
                       ["admitted", "limited_use", "excluded", "pending_foundation"])
        if r["decision"] in {"admitted", "limited_use"} and not r["permitted_purposes"]:
            raise RunStopped("admission_missing_permitted_purpose")
        self.rulings.append({**r, "target_event_id": event["event_id"],
                             "ruling_stage": self.stage, "human_review_required": True})
        self.emit("evidence_ruling", "R_JUDGE", r["reason"], r["legal_source_ids"],
                  target=event["event_id"], decision=r["decision"],
                  permitted_purposes=r["permitted_purposes"], human_review_required=True)

    def resolve_round_one(self):
        for eid in list(self.pending_motions):
            motion = self.event(eid)
            r = self.judge("rule_motion", motion, ["grant_exclusion", "deny"])
            if r["decision"] == "grant_exclusion":
                self.exclusions[motion["meta"]["target"]] = r
                for e in self.events:
                    if e["kind"] == "evidence" and motion["meta"]["target"] in e["references"]:
                        self.rulings.append({**r, "decision": "excluded", "target_event_id": e["event_id"]})
                        e["meta"]["stricken"] = True
            self.emit("motion_ruling", "R_JUDGE", r["reason"], r["legal_source_ids"],
                      target=eid, decision=r["decision"], human_review_required=True)
            self.pending_motions.remove(eid)
        for eid in list(self.pending_evidence):
            self.rule_evidence(self.event(eid))
            self.pending_evidence.remove(eid)

    def unresolved_foundation(self):
        return [e["event_id"] for e in self.events if e["kind"] == "evidence"
                and not e["meta"].get("stricken")
                and (self.last_ruling(e["event_id"]) or {}).get("decision") == "pending_foundation"]

    def close_pending_foundation(self):
        """Bounded stage closure: two submissions, one batch of final rulings.

        This is a versioned development procedure, not a legal gold label.
        Never alter an evidence item's original words, references or source.
        """
        targets = self.unresolved_foundation()
        if not targets:
            return
        frozen = {eid: copy.deepcopy(self.event(eid)) for eid in targets}
        self.emit("foundation_closure_started", text="阶段询问已结束；双方各一次现有材料意见，随后完成待决证据裁定。",
                  target_event_ids=targets, protocol_version=self.protocol_version,
                  new_testimony_allowed=False, native_or_external_tools_allowed=False)
        # Freeze the common record before either party submits. No private
        # consultation, private retrieval results or other party's memory.
        common = [copy.deepcopy(e) for e in self.events if set(COURT) <= set(e["audience"])]
        statements = []
        for role in LAWYERS:
            sources = []
            for row in self.catalog():
                try:
                    sources.append(self.source(role, row["citation_id"]))
                except ValueError:
                    continue
            ctx = {"task": "foundation_closure_statement", "role_id": role,
                   "role_card": self.case.role_card(role), "stage_id": self.stage,
                   "case": self.case.public_brief(), "record": copy.deepcopy(common),
                   "pending_items": [{"evidence": frozen[eid], "last_ruling": self.last_ruling(eid)} for eid in targets],
                   "target_event_ids": targets, "authorized_materials": sources,
                   "legal_sources": self.legal_texts(), "protocol_version": self.protocol_version}
            allowed_refs = {e["event_id"] for e in common} | self.legal_ids() | {s["citation_id"] for s in sources}
            schema = foundation_statement_schema(targets)
            answer = self.backend.decide(role, BASE_PROMPT + """本阶段常规询问已结束。现在只就各待补基础证据提交一次简短意见。
只能引用输入中的公共庭审记录、已披露材料和法律，不得检索、新问证人或编造补充事实。
每个target_event_id必须恰好出现一次；可以选择no_position。references只能用输入提供的原文/记录/法律编号。
请区分原证据本身与后来独立取得的证词：后来的证词不会改写原先“未知/未记载”的回答。
你的意见不是证据。法律与事实基础仍不足时可主张排除，或说明确有依据的限定用途。每项意见尽量简短。
""", ctx, schema)
            rows = answer.get("submissions", []) if isinstance(answer, dict) else []
            valid = (_validate_value(answer, schema, schema)
                     and sorted(r["target_event_id"] for r in rows) == sorted(targets)
                     and all(set(r["references"]) <= allowed_refs for r in rows))
            if not valid:
                self.emit("action_rejected", role, "invalid_foundation_closure_submission",
                          audience=[role], closure_submission=True)
                continue
            for row in rows:
                event = self.emit("foundation_closure_submission", role, row["text"], row["references"],
                    target=row["target_event_id"], requested_decision=row["requested_decision"],
                    permitted_purposes=row["permitted_purposes"], not_evidence=True)
                statements.append(copy.deepcopy(event))
        self._finalize_foundation(targets, frozen, common, statements)

    def _reject_final_foundation(self, targets, answer, errors, code):
        diagnostic = {"error_code": code, "target_event_ids": list(targets),
                      "candidate": copy.deepcopy(answer), "validation_errors": errors,
                      "candidate_is_accepted_ruling": False}
        self.foundation_diagnostics.append(copy.deepcopy(diagnostic))
        self.emit("foundation_decision_rejected", text=code,
                  audience=["reviewer"], **diagnostic)
        raise RunStopped(code)

    def _finalize_foundation(self, targets, frozen, common, statements):
        """One final judge call, using the recorded party submissions exactly."""
        if (sorted(targets) != sorted(self.unresolved_foundation())
                or set(frozen) != set(targets)
                or any(self.event(eid) != frozen[eid] for eid in targets)):
            raise RunStopped("foundation_finalization_state_mismatch")
        judge_ctx = {"task": "finalize_foundation", "role_id": "R_JUDGE",
            "role_card": self.case.role_card("R_JUDGE"), "stage_id": self.stage,
            "case": self.case.public_brief(), "record": copy.deepcopy(common),
            "pending_items": [{"evidence": frozen[eid], "last_ruling": self.last_ruling(eid)} for eid in targets],
            "target_event_ids": targets, "submissions": statements,
            "legal_sources": self.legal_texts(), "protocol_version": self.protocol_version}
        # Only the judge's authorized disclosed source text may be added.
        judge_ctx["authorized_materials"] = []
        for row in self.catalog():
            try:
                judge_ctx["authorized_materials"].append(self.source("R_JUDGE", row["citation_id"]))
            except ValueError:
                continue
        schema = final_foundation_schema(targets, self.legal_ids())
        judge_ctx["allowed_legal_source_ids"] = sorted(self.legal_ids())
        judge_ctx["output_contract"] = FOUNDATION_OUTPUT_CONTRACT
        try:
            answer = self.backend.decide("R_JUDGE", BASE_PROMPT + """现在进行本阶段待补基础证据的最终裁定。
阶段询问已结束，双方已获得一次基于现有记录提出意见的机会。每个目标恰好裁定一次，不能继续pending_foundation。
只能选择admitted、limited_use或excluded；基础仍不足时应根据适用法律处理，不得为推进流程而准入。
证据对象及其原文不变。后来的独立证词不能把早先“未知/未记载”的回答变成肯定事实，也不能把意见当证据。
每项reason必须是非空白理由。legal_source_ids必须至少选一个allowed_legal_source_ids中的完整编号，不能使用证据编号、简称或自行改写编号。
准入或限定使用必须列出明确用途。排除时permitted_purposes为空。
不得读取隐藏底稿、原案判决或rubric。你的实体法律裁定仍须人工核验。
""", judge_ctx, schema)
        except BackendError as error:
            diagnostic = {"error_code": error.code, "target_event_ids": list(targets),
                "candidate": None, "candidate_is_accepted_ruling": False,
                "validation_errors": [{"field": "backend_response", "error": error.code}],
                "limitation": "Adapter did not return a structured candidate; raw output unavailable."}
            self.foundation_diagnostics.append(copy.deepcopy(diagnostic))
            self.emit("foundation_decision_rejected", text=error.code,
                      audience=["reviewer"], **diagnostic)
            raise
        rows = answer.get("rulings", []) if isinstance(answer, dict) else []
        shape = final_foundation_schema(targets)
        if (not _validate_value(answer, shape, shape)
                or sorted(r["target_event_id"] for r in rows) != sorted(targets)):
            self._reject_final_foundation(targets, answer,
                [{"field": "rulings", "error": "invalid_targets_or_shape"}],
                "invalid_final_foundation_targets_or_decisions")
        errors = []
        for row in rows:
            def add_error(field, code, **extra):
                errors.append({"target_event_id": row["target_event_id"],
                               "field": field, "error": code, **extra})
            if not row["reason"].strip():
                add_error("reason", "final_foundation_empty_reason")
            if not row["legal_source_ids"]:
                add_error("legal_source_ids", "final_foundation_missing_law")
            invalid_ids = sorted(set(row["legal_source_ids"]) - self.legal_ids())
            if invalid_ids:
                add_error("legal_source_ids", "final_foundation_unknown_law_id", invalid_ids=invalid_ids)
            if row["decision"] in {"admitted", "limited_use"}:
                if not row["permitted_purposes"] or any(not p.strip() for p in row["permitted_purposes"]):
                    add_error("permitted_purposes", "admission_missing_permitted_purpose")
            elif row["permitted_purposes"]:
                add_error("permitted_purposes", "excluded_foundation_has_permitted_purpose")
        if errors:
            self._reject_final_foundation(targets, answer, errors, errors[0]["error"])
        if any(self.event(eid) != frozen[eid] for eid in targets):
            raise RunStopped("foundation_evidence_changed_during_closure")
        # No partial batch commit: validate every decision before adding any.
        for row in rows:
            self.rulings.append({**row, "ruling_stage": self.stage,
                                 "human_review_required": True, "final_foundation": True})
            self.emit("evidence_ruling", "R_JUDGE", row["reason"], row["legal_source_ids"],
                      target=row["target_event_id"], decision=row["decision"],
                      permitted_purposes=row["permitted_purposes"],
                      human_review_required=True, final_foundation=True)
        self.emit("foundation_closure_completed", text="待补基础证据已逐项获得最终裁定；证据原文未改写。",
                  target_event_ids=targets, protocol_version=self.protocol_version)

    def deliver_instructions(self):
        required = {"L_PEN_1096"}
        for charge in self.case.manifest["charges"]:
            required.update(charge.get("legal_source_ids", []))
        # Deliberately exact source excerpts, not unreviewed model-written instructions.
        for row in self.case.laws:
            if row["legal_source_id"] in required and self.case.publication_permitted(row):
                self.emit("delivered_instruction", "R_JUDGE",
                          "\n".join(p["text"] for p in row.get("paragraphs", [])),
                          [row["legal_source_id"]],
                          label="开发试跑法规摘录；正式陪审团指示待专业核验")
        self.emit("delivered_instruction", "R_JUDGE",
                  "控方承担证明责任。律师陈述不是证据。只依获准证据和限定用途判断；"
                  "不得以被告保持沉默推断有罪。以下裁决是模拟结果，不是标准答案。")

    def review_session(self):
        """Mechanical pre-verdict audit; no model edits or hidden scoring inputs.

        Future rubric evaluation must run separately from the courtroom and must
        not alter the jury feed. This development runner does not implement it.
        """
        jury = self.jury_record()
        evidence = [e for e in jury if e["kind"] == "evidence"]
        claims = []
        for event in self.events:
            if event["kind"] in {"opening_statement", "closing_argument"}:
                claims.append({"event_id": event["event_id"], "actor": event["actor"],
                    "references": event["references"],
                    **self.argument_reference_check(event, evidence),
                    "text": event["text"]})
        self.review = {"stage": "pre_verdict_review", "case_id": self.case.case_id,
            "review_mode": "mechanical_procedural_audit",
            "rubric_status": "not_loaded_not_scored", "formal_score": None,
            "rubric_scoring_implemented": False,
            "ground_truth_used_by_trial_agents": False,
            "argument_handling": "full_eligible_closing_arguments_no_model_selection",
            "reference_warnings_invalidate_arguments": False,
            "hidden_scores_change_jury_input": False,
            "claim_reference_checks": claims, "unresolved_foundation": self.unresolved_foundation(),
            "pending_motions": list(self.pending_motions), "pending_evidence": list(self.pending_evidence),
            "all_substantive_judge_rulings_require_human_review": True,
            "jury_record_event_ids": [e["event_id"] for e in jury]}
        self.emit("review_session_completed", text="程序与引用编号检查已保存；未调用评分模型、未修改律师陈词。",
                  audience=["reviewer"])

    def run(self, *, resume_completed_stage=False, resume_foundation_finalization=False):
        self.status = "running"
        self.stop_reason = None
        try:
            if resume_completed_stage and resume_foundation_finalization:
                raise RunStopped("conflicting_recovery_modes")
            if resume_foundation_finalization:
                state = getattr(self, "_recovery_foundation_state", None)
                if (not self.recovery or self.recovery.get("resume_mode") != "foundation_finalization"
                        or self.case.case_id != "CA-PILOT-004" or self.stage != "r1_ruling"
                        or not isinstance(state, dict)):
                    raise RunStopped("unsupported_foundation_recovery_boundary")
                for item in state["common_record"] + state["submissions"]:
                    if self.event(item["event_id"]) != item or not set(COURT) <= set(item["audience"]):
                        raise RunStopped("foundation_recovery_record_mismatch")
                self.emit("foundation_finalization_resumed", audience=["reviewer"],
                          text="重用原双方意见，仅重新请求未获准的最终裁定；原调用和用量保留。")
                self._finalize_foundation(state["target_event_ids"], state["frozen_evidence"],
                                          state["common_record"], state["submissions"])
                if self.unresolved_foundation() or self.pending_motions or self.pending_evidence:
                    raise RunStopped("unresolved_rulings_after_recovery_closure")
                self.stage = self.case.stages[self.stage]["next_stage_id"]
            if resume_completed_stage:
                if not self.recovery or self.stage != "r2_examination":
                    raise RunStopped("unsupported_recovery_boundary")
                # Recovery.py verifies the old stage finished its action loop.
                # Never repeat witness questions or consume those calls again.
                self.close_pending_foundation()
                if self.unresolved_foundation() or self.pending_motions or self.pending_evidence:
                    raise RunStopped("unresolved_rulings_after_recovery_closure")
                self.stage = self.case.stages[self.stage]["next_stage_id"]
            while self.stage != "ended":
                self.active_witness = None
                self.emit("stage_started", text=self.stage)
                if self.stage == "r1_ruling":
                    self.resolve_round_one()
                elif self.stage == "r2_instructions":
                    self.deliver_instructions()
                elif self.stage == "r2_verdict":
                    self.review_session()
                    if self.pending_motions or self.pending_evidence or self.unresolved_foundation():
                        raise RunStopped("unresolved_rulings_before_verdict")
                    ctx = self.context("R_JURY", "verdict")
                    self.verdict = self.backend.decide("R_JURY", BASE_PROMPT +
                        "逐项作出模拟裁决。事实认定仅使用获准证据并遵守限定用途；律师发言不是证据。"
                        "即使陈词复述未准入或已排除事实，也不得据此认定案情。引用警告本身不判定论证有效性；"
                        "可以考虑律师对证据缺口和证明责任的论证。遵守已交付的陪审团指示，不能以沉默推断有罪。",
                        ctx, VERDICT_SCHEMA)
                    ids = [c["charge_id"] for c in self.verdict["charges"]]
                    required = [c["charge_id"] for c in self.case.manifest["charges"]]
                    if sorted(ids) != sorted(required):
                        raise RunStopped("verdict_charge_ids_mismatch")
                    jury_ids = {e["event_id"] for e in self.jury_record()}
                    if any(not set(c["record_event_ids"]) <= jury_ids for c in self.verdict["charges"]):
                        raise RunStopped("verdict_cites_unavailable_record")
                    self.emit("simulated_verdict", "R_JURY", json.dumps(self.verdict, ensure_ascii=False),
                              audience=COURT + ["R_JURY"], is_ground_truth=False)
                else:
                    self.run_lawyer_stage()
                if self.stage in {"r1_ruling", "r2_recorded_evidence", "r2_examination"}:
                    self.close_pending_foundation()
                    if self.unresolved_foundation():
                        raise RunStopped("pending_foundation_requires_resolution")
                self.stage = self.case.stages[self.stage]["next_stage_id"]
            self.status = "completed_development_run"
        except Exception as exc:
            self.status = "incomplete"
            self.verdict = None
            self.stop_reason = getattr(exc, "code", type(exc).__name__) + ": " + str(exc)[:180]
            self.emit("run_stopped", text=self.stop_reason,
                      audience=["reviewer"])
        return self.summary()

    def run_lawyer_stage(self):
        finished = set()
        limit = self.config.get("actions_per_role_per_stage", {}).get(self.stage,
                    self.config.get("default_actions_per_role_per_stage", 3))
        counts = {r: 0 for r in LAWYERS}
        for _ in range(limit):
            for role in LAWYERS:
                if role in finished:
                    continue
                if self.action_count >= self.config.get("max_lawyer_actions", 80):
                    raise RunStopped("lawyer_action_budget_exhausted")
                self.action_count += 1
                counts[role] += 1
                try:
                    kind = self.lawyer_turn(role)
                except ValueError as ex:
                    self.emit("action_rejected", role, str(ex), audience=[role])
                    continue
                if kind == "pass" or kind in {"opening_statement", "closing_argument"}:
                    finished.add(role)
        for role in LAWYERS:
            if role not in finished:
                self.emit("stage_budget_reached", role,
                          "阶段行动预算用尽；程序推进，记录本阶段未主动结束。", audience=[role], count=counts[role])

    def summary(self):
        return {"case_id": self.case.case_id, "status": self.status,
                "stop_reason": self.stop_reason, "protocol_version": self.protocol_version,
                "foundation_output_contract": FOUNDATION_OUTPUT_CONTRACT,
                "execution_kind": "recovered_development" if self.recovery else "fresh_development",
                "recovery": self.recovery,
                "current_stage": self.stage, "event_count": len(self.events),
                "lawyer_actions": self.action_count,
                "rejected_actions": sum(e["kind"] == "action_rejected" for e in self.events),
                "verdict": self.verdict, "formal_benchmark_score": None,
                "mode": self.config.get("mode", "live"),
                "backend": self.config.get("backend", "responses") if self.config.get("mode") != "mock" else "mock",
                "model_stats": getattr(self.backend, "stats", {}),
                "limitations": ["development_only", "substantive_rulings_need_expert_review",
                    "historical_law_version_review_pending", "extractive_witness_renderer",
                    "rubric_not_loaded", "tool_allowlist_is_not_an_OS_firewall"]}

    def save(self, out):
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        def write(name, data):
            (out / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        write("summary.json", self.summary())
        write("review_session.json", self.review)
        write("foundation_diagnostics.json", self.foundation_diagnostics)
        write("jury_record.json", self.jury_record())
        write("model_calls.json", getattr(self.backend, "calls", []))
        write("run_config.json", self.config)
        if self.recovery:
            write("recovery_manifest.json", self.recovery)
        source_hashes = {}
        for name in ("manifest.json", "public/case.md", "public/materials.jsonl", "public/legal_sources.jsonl",
                     "role_private/roles.json", "config/procedure.json", "config/permissions.json"):
            source_hashes[name] = hashlib.sha256((self.case.path / name).read_bytes()).hexdigest()
        write("input_hashes.json", source_hashes)
        write("runtime_hashes.json", {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in sorted(Path(__file__).parent.glob("*.py"))})
        (out / "author_events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n"
                                                     for e in self.events), encoding="utf-8")
        for role in self.case.roles:
            write(f"view_{role}.json", self.visible_record(role))
