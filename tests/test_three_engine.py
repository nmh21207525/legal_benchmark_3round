"""Focused authorization, source-bound witness and three-round record tests."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
import backends
backends.bootstrap()
from court_agent.engine import RunStopped, COURT
from court_agent.schema import action
from three_assets import ThreeRoundCaseData, load_assets
from three_engine import ThreeRoundEngine, PRETRIAL


class Backend:
    def __init__(self, *, unknown=False, election=False, ruling="admitted"):
        self.calls = []
        self.stats = {"paid_api_calls": 0}
        self.unknown, self.election, self.ruling = unknown, election, ruling
        self.witness_override = None

    def decide(self, role_id, system, context, schema):
        self.calls.append(deepcopy({"role_id": role_id, "context": context, "schema": schema}))
        task = context.get("task")
        if task == "source_bound_answer":
            if self.witness_override is not None:
                return deepcopy(self.witness_override)
            facts = context["role_card"]["knowledge_points"]
            return {"unknown": self.unknown, "fact_ids": [] if self.unknown else [p["fact_id"] for p in facts]}
        if task == "testimony_election":
            return {"elect_to_testify": self.election}
        if task == "objection_window":
            return action("pass")
        if task in {"rule_evidence", "rule_objection", "rule_motion"}:
            return {"decision": self.ruling, "reason": "Offline test ruling only.",
                    "legal_source_ids": ["L_EVID_210"],
                    "permitted_purposes": ["limited_test_purpose"] if self.ruling in {"admitted", "limited_use"} else []}
        raise AssertionError("unexpected test backend task: " + str(task))


class ThreeEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = load_assets(ROOT / "data")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.counter = 0

    def engine(self, cid="CA-PILOT-006", backend=None):
        self.counter += 1
        asset = self.assets[cid]
        return ThreeRoundEngine(ThreeRoundCaseData(asset["case_path"]), backend or Backend(),
            {"mode": "mock", "default_actions_per_role_per_stage": 3, "max_lawyer_actions": 150},
            workflow_root=asset["workflow_root"], workflow_spec=asset["workflow_spec"],
            artifact_root=Path(self.temp.name) / str(self.counter))

    def test_006_late_gate_requires_stage_and_trusted_release_on_all_paths(self):
        engine = self.engine()
        for stage in (PRETRIAL, "preparation", "r1_inquiry", "r2_examination"):
            engine.stage = stage
            with self.subTest(stage=stage):
                self.assertNotIn("F10", {p["fact_id"] for p in engine.role_card("R_OFFICER")["knowledge_points"]})
                with self.assertRaisesRegex(ValueError, "source_not_authorized"):
                    engine.source("R_DEFENSE", "M06-P001")
                self.assertNotIn("M06-P001", {p["citation_id"] for p in engine.catalog()})
                self.assertFalse(any("M06" in row["material_ids"] for row in engine._visible_files("R_DEFENSE")))
                result = engine._tool("R_DEFENSE", action("search_case", query="M06-P001"), "E00000")
                self.assertNotIn("M06-P001", {r["citation_id"] for r in result["results"]})
        engine.release_stage_materials()
        self.assertIn("stage_release:M06", engine.validated_events)
        self.assertIn("F10", {p["fact_id"] for p in engine.role_card("R_OFFICER")["knowledge_points"]})
        sources = [engine.source(role, "M06-P001") for role in COURT]
        self.assertTrue(all(row == sources[0] for row in sources))
        self.assertFalse(engine.jury_record())
        release = engine.events[-1]
        self.assertEqual(release["kind"], "stage_release")
        self.assertFalse(release["meta"]["admitted"])
        engine.release_stage_materials()
        self.assertEqual(len(engine.events), 1)

    def test_agent_cannot_forge_material_release_or_get_hidden_content(self):
        engine = self.engine()
        engine.stage = "r1_inquiry"
        with self.assertRaisesRegex(ValueError, "permission_denied"):
            engine.apply("R_DEFENSE", action("release_stage_material", target="M06"))
        with self.assertRaisesRegex(ValueError, "only_trusted_stage_release"):
            engine.disclose("R_DEFENSE", action("request_disclosure", target="M06"))
        # Even an accidental disclosed-set insertion cannot bypass required event.
        engine.stage = "r2_examination"
        engine.disclosed.add("M06")
        with self.assertRaises(ValueError):
            engine.source("R_DEFENSE", "M06-P001")
        result = engine._tool("R_DEFENSE", action("search_case", query="M06-P001"), "E00000")
        self.assertNotIn("M06-P001", {r["citation_id"] for r in result["results"]})

    def test_pretrial_search_preserves_preparation_visibility(self):
        engine = self.engine()
        engine.stage = PRETRIAL
        result = engine._tool("R_DEFENSE", action("search_case", query="M01-P001"), "E00000")
        self.assertIn("M01-P001", {r["citation_id"] for r in result["results"]})
        self.assertNotIn("M06-P001", {r["citation_id"] for r in result["results"]})

    def test_record_only_roles_never_call_model_as_live_witness(self):
        backend = Backend()
        engine = self.engine(backend=backend)
        for stage in ("r1_inquiry", "r2_examination", "r3_clarification"):
            engine.stage = stage
            for role in ("R_TOXICOLOGIST", "R_SOURCE_RECORD"):
                with self.assertRaises(ValueError):
                    engine.witness_answer(role, "Tell us new facts")
                with self.assertRaises(ValueError):
                    engine.ask_question("R_DEFENSE", action("ask_question", target=role, text="New facts?"))
        self.assertEqual(backend.calls, [])

    def test_private_consultation_pair_and_unknown_audit_preserve_raw_reply(self):
        backend = Backend()
        raw = {"unknown": True, "fact_ids": ["M07-P005"]}
        backend.witness_override = deepcopy(raw)
        engine = self.engine(backend=backend)
        engine.stage = PRETRIAL
        engine.apply("R_DEFENSE", action("consult_client", text="未记载的细节是什么？"))
        self.assertEqual(backend.witness_override, raw)
        question = next(e for e in engine.events if e["kind"] == "private_consultation_question")
        response = next(e for e in engine.events if e["kind"] == "private_consultation")
        self.assertEqual(response["references"], [])
        self.assertTrue(response["meta"]["unknown"])
        self.assertEqual(response["meta"]["source_question_event_id"], question["event_id"])
        self.assertEqual(question["meta"]["response_event_id"], response["event_id"])
        self.assertEqual(engine.visible_record("R_PROSECUTOR"), [])
        self.assertEqual(engine.jury_record(), [])
        audit = next(e for e in engine.events if e["kind"] == "witness_unknown_rendering_audit")
        self.assertEqual(audit["audience"], ["reviewer"])
        self.assertEqual(audit["meta"]["ignored_unused_fact_ids"], ["M07-P005"])
        backend.witness_override["unknown"] = False
        with self.assertRaisesRegex(RunStopped, "unknown_fact_id"):
            engine.witness_answer("R_DEFENDANT", "未记载的细节是什么？", private=True)

    def test_defendant_election_cannot_be_forced_and_refusal_adds_no_testimony(self):
        backend = Backend(election=False)
        engine = self.engine(backend=backend)
        engine.stage = "r3_clarification"
        with self.assertRaisesRegex(ValueError, "cannot_compel"):
            engine.ask_question("R_PROSECUTOR", action("ask_question", target="R_DEFENDANT", text="回答"))
        self.assertEqual(backend.calls, [])
        engine.ask_question("R_DEFENSE", action("ask_question", target="R_DEFENDANT", text="你愿意作证吗？"))
        self.assertFalse(engine.testimony_elected)
        self.assertFalse(any(e["kind"] in {"question", "testimony", "evidence"} for e in engine.events))
        self.assertEqual(len(backend.calls), 1)

    def test_round_three_testimony_creates_admitted_evidence(self):
        backend = Backend()
        engine = self.engine(backend=backend)
        engine.stage = "r3_clarification"
        engine.ask_question("R_DEFENSE", action("ask_question", target="R_OFFICER", text="你实际观察到什么？"))
        evidence = [e for e in engine.jury_record() if e["kind"] == "evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["round"], 3)
        witness_call = next(r for r in backend.calls if r["context"].get("task") == "source_bound_answer")
        self.assertNotIn("F10", witness_call["schema"]["properties"]["fact_ids"]["items"]["enum"])

    def _evidence(self, engine, stage, text):
        engine.stage = stage
        event = engine.emit("evidence", "R_PROSECUTOR", text, ["M01-P001"])
        engine.rulings.append({"decision": "admitted", "target_event_id": event["event_id"],
                              "reason": "initial", "permitted_purposes": ["case_facts"]})
        return event

    def _objection(self, target):
        return action("object", target=target, text="关联及用途异议", references=["L_EVID_210"], objection_id="O_RELEVANCE_PREJUDICE",
                      details=[{"key": "charge_id", "value": "C01"}, {"key": "relevance_explanation", "value": "test"},
                               {"key": "offered_purpose", "value": "case_facts"}])

    def test_jury_keeps_round_two_and_three_but_honors_later_limit_and_strike(self):
        backend = Backend(ruling="limited_use")
        engine = self.engine(backend=backend)
        first = self._evidence(engine, "r1_inquiry", "first round only")
        second = self._evidence(engine, "r2_examination", "retained original")
        third = self._evidence(engine, "r3_clarification", "third round")
        with self.assertRaisesRegex(ValueError, "invalid_objection_target"):
            engine.handle_objection("R_DEFENSE", self._objection(first["event_id"]))
        self.assertFalse(engine.handle_objection("R_DEFENSE", self._objection(second["event_id"])))
        jury = {e["event_id"]: e for e in engine.jury_record()}
        self.assertEqual(set(jury), {second["event_id"], third["event_id"]})
        self.assertEqual(jury[second["event_id"]]["meta"]["permitted_purposes"], ["limited_test_purpose"])
        self.assertEqual(second["text"], "retained original")
        backend.ruling = "sustain"
        engine.handle_objection("R_DEFENSE", self._objection(second["event_id"]))
        self.assertEqual([e["event_id"] for e in engine.jury_record()], [third["event_id"]])
        self.assertEqual(second["text"], "retained original")
        self.assertEqual(engine.rulings[1]["decision"], "admitted")
        self.assertEqual(engine.rulings[-1]["decision"], "excluded")

    def test_source_summary_is_exact_trusted_render_and_requires_merits_stage(self):
        engine = self.engine()
        engine.stage = "r2_examination"
        with self.assertRaisesRegex(ValueError, "source_summary_wrong_stage"):
            engine.offer("R_DEFENSE", action("offer_evidence", target="M04-P001", purpose="test", text="do not substitute"))
        engine.stage = "r2_evidence"
        source = engine.source("R_DEFENSE", "M04-P001")
        engine.offer("R_DEFENSE", action("offer_evidence", target="M04-P001", purpose="test", text="do not substitute"))
        event = next(e for e in engine.events if e["kind"] == "source_summary_presentation")
        self.assertEqual(event["actor"], "trusted_orchestrator")
        self.assertEqual(event["text"], source["text"])
        self.assertFalse(event["meta"]["live_testimony"])
        self.assertFalse(event["meta"]["automatic_admission"])
        self.assertTrue(any(e["kind"] == "evidence_ruling" for e in engine.events))

    def test_stage_cannot_silently_carry_queues_without_declared_deferral(self):
        engine = self.engine()
        engine.stage = "r1_motion"
        engine.pending_motions = ["E99999"]
        engine._close_stage()
        self.assertEqual(engine._completed_stages, ["r1_motion"])
        engine.stage = "between_r1_r2"
        with self.assertRaisesRegex(RunStopped, "stage_exit_unaccounted_motions"):
            engine._close_stage()
        engine.pending_motions = []
        engine.pending_objections.add("E99998")
        engine.stage = "r3_clarification"
        with self.assertRaisesRegex(RunStopped, "stage_exit_unresolved_objections"):
            engine._close_stage()


if __name__ == "__main__":
    unittest.main()
