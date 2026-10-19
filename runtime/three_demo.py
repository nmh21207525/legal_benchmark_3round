"""Deterministic, zero-network 005/006 integration fixture; never AI results.

This exercises the actual workflow, permissions, court engine and output writer.
Its scripted acts, admission decisions and verdicts have no legal or scoring
meaning. The fixture never reads authoring answers or evaluation/rubric files.
"""
from __future__ import annotations

from copy import deepcopy
import json

import backends
backends.bootstrap()
from court_agent.mock import MockBackend
from court_agent.schema import action


LABEL = "离线流程测试，不是模型表现、法律意见或标准答案。"


class ThreeRoundMockBackend(MockBackend):
    is_offline_fixture = True

    def __init__(self, case, candidate):
        super().__init__()
        self.case, self.candidate = case, candidate
        # ReliableBackend uses this identity for its progress label only.
        self.model = "offline_scripted_mock"
        self.lawyer_backend = self
        self.pretrial_sequence = None
        self.closed = False

    def _count(self, role_id, context):
        self.stats["mock_calls"] += 1
        self.calls.append({"role_id": role_id, "task": context.get("task"),
                           "stage_id": context.get("stage_id"),
                           "model": "offline_scripted_mock", "candidate_label": self.candidate,
                           "status": "completed", "transport": "offline_fixture"})

    def _artifact(self, name, refs, *, update=False):
        if name.endswith(".csv"):
            text = "entry,status,source\n1,offline_fixture_only," + refs[0] + "\n"
        elif name.endswith(".json"):
            text = json.dumps({"mock_only": True, "notice": LABEL, "references": refs}, ensure_ascii=False)
        else:
            text = "# 离线流程产物" + ("更新" if update else "") + "\n\n" + LABEL + "\n来源：" + "、".join(refs)
        return action("write_artifact", target=name, text=text, references=refs)

    def _prepare_sequence(self, context):
        workflow = context["workflow"]
        files = workflow["files"]
        # Only the role-scoped file catalog is used: 006 M06 is absent here.
        readable = [f for f in files if f["file_id"].startswith("m")]
        refs = next((f["source_refs"][:1] for f in readable if f["source_refs"]), [])
        if not refs:
            raise ValueError("fixture_expected_authorized_material_source")
        seq = [action("list_files")]
        seq.extend(action("read_document", target=f["file_id"]) for f in readable)
        seq.extend([
            action("search_case", query="来源 材料 事实", scope="case"),
            action("search_law", query="证据 个人知情", scope="law"),
        ])
        services = workflow.get("services", [])
        if not services:
            raise ValueError("fixture_expected_pretrial_service")
        seq.append(action("request_investigation", target=services[0]["service_id"],
                          text=LABEL + "请核对资料来源和缺口，未知事实保持未知。", references=refs))
        seq.extend(self._artifact(name, refs) for name in workflow["required_artifacts"])
        seq.append(action("pass"))
        if len(seq) > workflow["pretrial_action_budget"]:
            raise ValueError("fixture_pretrial_budget_too_small")
        return seq

    def decide(self, role_id, system, context, schema):
        task, stage = context.get("task"), context.get("stage_id")
        if task != "choose_action":
            # Non-lawyer tasks use the strict mock interfaces. Witness facts are
            # chosen solely from the already filtered role card, including the
            # released 006 F10 only after its trusted disclosure event.
            if task == "source_bound_answer":
                self._count(role_id, context)
                facts = context["role_card"].get("knowledge_points", [])
                return {"fact_ids": [p["fact_id"] for p in facts], "unknown": not bool(facts)}
            result = super().decide(role_id, system, context, schema)
            self.calls[-1].update(stage_id=stage, candidate_label=self.candidate,
                                  status="completed", transport="offline_fixture")
            return result

        self._count(role_id, context)
        key = (role_id, stage)
        index = self.turns.get(key, 0)
        self.turns[key] = index + 1
        allowed = set(context["allowed_actions"])
        if stage == "pretrial_workspace":
            if self.pretrial_sequence is None:
                self.pretrial_sequence = self._prepare_sequence(context)
            return deepcopy(self.pretrial_sequence[index]) if index < len(self.pretrial_sequence) else action("pass")
        if stage == "preparation":
            if role_id == "R_DEFENSE" and index == 0:
                return action("consult_client", text=LABEL + "请只说明你已知且来源中明确记载的事项，不能确定的请说明未知。")
            if (role_id == "R_DEFENSE" and index == 1) or (role_id == "R_PROSECUTOR" and index == 0):
                return action("inspect_disclosed_material", target="M01")
        elif stage == "r1_motion" and role_id == "R_DEFENSE" and index == 0:
            return action("file_motion", target="M02-P001", text=LABEL + "请求就指定材料审查证明用途。",
                          references=["L_EVID_210"], purpose="offline_fixture_only")
        elif stage in {"between_r1_r2", "between_r2_r3"} and role_id == "R_DEFENSE":
            if index == 0:
                return action("consult_client", text=LABEL + "针对已经发生的阶段还有哪些来源记载可以说明？")
            if index == 1 and "write_artifact" in allowed:
                return self._artifact("pretrial_plan.md", ["M01-P001"], update=True)
        elif stage == "r2_opening" and index == 0:
            return action("opening_statement", text=LABEL + "请依据获准证据分别审查指控。")
        elif stage == "r2_evidence" and index == 0:
            return action("offer_evidence", target="M03-P001", purpose="offline_fixture_only", text=LABEL)
        elif stage == "r3_clarification" and index == 1:
            return action("offer_evidence", target="M04-P001", purpose="offline_fixture_only", text=LABEL)
        elif stage == "r3_closing" and index == 0:
            return action("closing_argument", text=LABEL + "请仅依获准记录与交付指示逐项裁决。",
                          references=[e["event_id"] for e in context.get("admitted_for_jury", [])])
        if "ask_question" in allowed and index == 0:
            witnesses = [w for w in context.get("questionable_role_ids", []) if w != "R_DEFENDANT"]
            if not witnesses:
                raise ValueError("fixture_expected_live_nondefendant_witness")
            return action("ask_question", target=witnesses[0], text=LABEL + "请说明目前获授权且由你知情的来源事项。",
                          purpose="offline_fixture_only")
        return action("pass")

    def close(self):
        self.closed = True

    def check_credentials(self):
        return None


def create_backend(config, candidate, case):
    """Runner-compatible factory. No credential or network handling exists."""
    return ThreeRoundMockBackend(case, candidate)
