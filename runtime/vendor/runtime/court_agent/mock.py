"""Deterministic plumbing exercise. Outputs are NOT model/legal performance."""
from .schema import action


class MockBackend:
    def __init__(self):
        self.calls = []
        self.stats = {"mock_calls": 0, "paid_api_calls": 0}
        self.turns = {}

    def decide(self, role_id, system, context, schema):
        task = context.get("task")
        self.calls.append({"role_id": role_id, "task": task, "model": "offline_scripted_mock"})
        self.stats["mock_calls"] += 1
        if task == "rewrite_query":
            return {"query": context["query"]}
        if task == "source_bound_answer":
            facts = context["role_card"].get("knowledge_points", [])
            return {"fact_ids": [p["fact_id"] for p in facts[:2]], "unknown": not bool(facts)}
        if task == "testimony_election":
            return {"elect_to_testify": False}
        if task == "foundation_closure_statement":
            return {"submissions": [{"target_event_id": eid, "text": "离线流程测试，不提供额外意见。",
                "references": [], "requested_decision": "no_position", "permitted_purposes": []}
                for eid in context["target_event_ids"]]}
        if task == "finalize_foundation":
            return {"rulings": [{"target_event_id": eid, "decision": "excluded",
                "reason": "离线占位裁定，仅测试待决队列收尾。", "legal_source_ids": ["L_EVID_702"],
                "permitted_purposes": []} for eid in context["target_event_ids"]]}
        if task in {"objection_window", "motion_response"}:
            return action("pass")
        if task in {"rule_objection", "rule_evidence", "rule_motion"}:
            choices = schema["properties"]["decision"]["enum"]
            decision = "overrule" if "overrule" in choices else "deny" if "deny" in choices else "admitted"
            return {"decision": decision, "reason": "离线脚本仅验证流程，不构成法律判断。",
                    "legal_source_ids": ["L_EVID_210"], "permitted_purposes": ["mock_pipeline_only"]}
        if task == "verdict":
            return {"charges": [{"charge_id": c["charge_id"], "decision": "not_guilty",
                    "reason": "离线占位结果，不是模型预测或标准答案。", "record_event_ids": []}
                    for c in context["case"]["charges"]]}
        if task != "choose_action":
            raise ValueError(f"Unexpected mock task: {task}")
        key = (role_id, context["stage_id"])
        i = self.turns.get(key, 0)
        self.turns[key] = i + 1
        stage = context["stage_id"]
        allowed = context["allowed_actions"]
        if stage == "preparation":
            if i == 0:
                return action("web_search", query="案件 证据 构成要件 M01", scope="all")
            if i == 1:
                return action("inspect_disclosed_material", target="M01")
            if i == 2 and role_id == "R_DEFENSE":
                return action("consult_client", text="请说明来源材料中有记载的经历。")
        elif stage == "r1_motion" and i == 0 and role_id == "R_DEFENSE":
            return action("file_motion", target="M02-P001", text="请求审查该段落的证明用途及基础。",
                          references=["L_EVID_210"], purpose="source_foundation")
        elif stage in {"r1_record_review", "r2_recorded_evidence"} and i == 0:
            return action("offer_evidence", target="M03-P001", purpose="source_foundation")
        elif "ask_question" in allowed:
            if stage == "r1_inquiry" and role_id == "R_DEFENSE" and i == 0:
                return action("request_disclosure", target="M05", text="请求详细评估观察材料。")
            witnesses = [w for w in context["questionable_role_ids"] if w != "R_DEFENDANT"]
            if witnesses and i <= (1 if stage == "r1_inquiry" and role_id == "R_DEFENSE" else 0):
                return action("ask_question", target=witnesses[0], text="请说明你亲历且有来源依据的事项。",
                              purpose="case_facts")
        elif stage == "between_rounds" and i == 0:
            return action("prepare_examination", text="按照裁定准备后续询问，不将第一轮记录自动交给陪审团。")
        elif stage == "r2_opening":
            return action("opening_statement", text="请审查本案证据与各项要件。")
        elif stage == "r2_closing":
            refs = [e["event_id"] for e in context["admitted_for_jury"]]
            return action("closing_argument", text="请只依据获准证据和证明标准审查各项指控。", references=refs)
        return action("pass")
