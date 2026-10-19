"""Narrow, offline restoration of an audited legacy stage-end stop.

This is development recovery, not a general checkpoint format. It reconstructs
the recorded state without replaying any model calls; saved views must agree.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .engine import CourtEngine, LAWYERS, COURT


LEGACY_HASHES = {
    "engine.py": "7c8c468affbc5da11822d5216f8a3f18db134f0266b671dd4a0636380b837ab5",
    "schema.py": "60f8b68e0960e821e0c9b30a440e6e77b938092771265ddf6b4b780a03c8d0c4",
    "data.py": "1322986478ae725bf192a0f654b26f08082e984a16779db556e6b7f3939cd91b",
    "search.py": "06020b8e534d9927bfed691f4feeeba2e461f12ca0b5866264b6129b0b55e094",
    "llm.py": "74f8b9af459799bd2ef1d65bd07139d52f75b78c8ccbe47f8b2172a9b42c684b",
    "codex_backend.py": "5fc4fdf6d91a30079a5f997d2992c0cd15376e894da147ef411e3d3b315f6662",
}
FOUNDATION_040_HASHES = {
    **LEGACY_HASHES,
    "engine.py": "492c4b774dd45c9684d279271d8ee4cbb9769574dc31ed365eff5ee9c98e377c",
    "schema.py": "dafaee0eff619b8d851523250f35e13a91024d54efb8766ce3271989e68dd5d3",
    "recovery.py": "146a9da1478630449eae28c0a7e36f17d6d25e34c9143f3c5704f33582d63525",
}
INPUT_NAMES = ("manifest.json", "public/case.md", "public/materials.jsonl",
               "public/legal_sources.jsonl", "role_private/roles.json",
               "config/procedure.json", "config/permissions.json")
EVENT_KINDS = {
    "stage_started", "stage_budget_reached", "run_stopped", "pass", "tool_result",
    "disclosure", "disclosure_notice", "private_consultation", "file_motion",
    "respond_to_motion", "motion_ruling", "call_witness", "question", "testimony",
    "testimony_election", "objection", "objection_ruling", "evidence",
    "evidence_ruling", "foundation_submission", "opening_statement",
    "prepare_examination", "action_rejected",
}
COUNTERS = {"attempted_calls", "successful_decisions", "failed_attempts",
            "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
            "reasoning_tokens", "latency_seconds", "by_model", "cli_notice_counts"}


class RecoveryError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise RecoveryError("recovery_refused: " + reason)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_calls(calls, stats, config):
    require(isinstance(calls, list) and bool(calls) and isinstance(stats, dict), "missing_call_accounting")
    count = len(calls)
    require(stats.get("attempted_calls") == count and stats.get("successful_decisions") == count
            and stats.get("failed_attempts") == 0, "call_count_mismatch")
    sums = {k: 0 for k in ("input_tokens", "output_tokens", "total_tokens",
                           "cached_input_tokens", "reasoning_tokens")}
    by_model, notice_counts = {}, {}
    elapsed = 0.0
    for call in calls:
        require(isinstance(call, dict) and call.get("status") == "completed", "failed_or_unknown_call")
        expected = config["models"]["lawyer" if call.get("role_id") == "R_DEFENSE" else "environment"]
        require(call.get("model") == expected and call.get("transport") == "codex_cli"
                and call.get("adapter_version") == "0.3.4", "unexpected_legacy_model_or_transport")
        usage = call.get("usage", {})
        require(isinstance(usage, dict), "invalid_usage")
        for name in sums:
            value = usage.get(name)
            require(type(value) is int and value >= 0, "invalid_usage")
            sums[name] += value
        bucket = by_model.setdefault(expected, {"calls": 0, "input_tokens": 0,
                                                 "output_tokens": 0, "total_tokens": 0})
        bucket["calls"] += 1
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            bucket[name] += usage[name]
        latency = call.get("latency_seconds")
        require(isinstance(latency, (int, float)) and latency >= 0, "invalid_latency")
        elapsed += latency
        for name, value in call.get("cli_notice_counts", {}).items():
            require(type(value) is int and value >= 0, "invalid_notice_accounting")
            notice_counts[name] = notice_counts.get(name, 0) + value
    require(all(stats.get(k) == v for k, v in sums.items()) and stats.get("by_model") == by_model,
            "token_or_model_accounting_mismatch")
    require(stats.get("cli_notice_counts", {}) == notice_counts, "notice_accounting_mismatch")
    require(abs(stats.get("latency_seconds", -1000) - elapsed) <= 0.0001 * count + 0.001,
            "latency_accounting_mismatch")


def _replay_state(engine, originals, *, allow_foundation_closure=False):
    """Rebuild derived state in event order, including historical exclusions."""
    seen, unresolved_objections = {}, []
    started = []
    expected_stage = engine.case.procedure["initial_stage_id"]
    for index, original in enumerate(originals, 1):
        require(isinstance(original, dict) and set(original) == {
            "event_id", "stage_id", "round", "kind", "actor", "text", "references", "audience", "meta"},
            "unknown_event_shape")
        require(original["event_id"] == f"E{index:05d}", "event_id_not_contiguous")
        e = deepcopy(original)
        kind, meta = e["kind"], e["meta"]
        allowed_kinds = EVENT_KINDS | ({"foundation_closure_started", "foundation_closure_submission"}
                                      if allow_foundation_closure else set())
        require(kind in allowed_kinds and isinstance(meta, dict), "unknown_event_kind")
        require(isinstance(e["text"], str) and isinstance(e["references"], list)
                and all(isinstance(x, str) for x in e["references"]), "invalid_event_payload")
        require(isinstance(e["audience"], list) and bool(e["audience"])
                and set(e["audience"]) <= set(engine.case.roles) | {"reviewer"}, "invalid_event_audience")
        require(e["actor"] in engine.case.roles or e["actor"] == "trusted_orchestrator", "invalid_event_actor")
        if kind == "stage_started":
            require(e["stage_id"] == expected_stage and e["text"] == expected_stage, "stage_path_mismatch")
            engine.stage = expected_stage
            started.append(engine.stage)
            engine.active_witness = None
            expected_stage = engine.case.stages[engine.stage]["next_stage_id"]
        require(bool(started) and e["stage_id"] == engine.stage
                and e["round"] == engine.case.stages[engine.stage].get("simulation_round"), "event_stage_mismatch")
        # Saved event objects have final in-place strike flags. Reconstruct them
        # from the ruling events instead of treating them as earlier knowledge.
        meta.pop("stricken", None)
        engine.events.append(e)
        seen[e["event_id"]] = e
        if kind == "disclosure":
            require(len(e["references"]) == 1 and meta.get("request_validated") is True,
                    "invalid_disclosure")
            mid = e["references"][0]
            rule = next((r for r in engine.case.procedure.get("disclosure_rules", [])
                         if r["material_id"] == mid), None)
            require(rule and e["actor"] in rule["valid_request_actor_role_ids"]
                    and e["audience"] == rule["release_to_role_ids"], "unauthorized_disclosure")
            order = list(engine.case.stages)
            require(order.index(engine.stage) >= order.index(rule["earliest_stage"]), "early_disclosure")
            engine.disclosed.add(mid)
        elif kind == "tool_result":
            require(e["actor"] in LAWYERS and e["audience"] == [e["actor"]], "tool_result_visibility")
            tool = meta.get("tool")
            require(tool in {"web_search", "inspect_disclosed_material"} and "result" in meta,
                    "unknown_tool_result")
            memory = {"tool": tool, "result": deepcopy(meta["result"])}
            if tool == "web_search":
                require(isinstance(meta.get("query"), str) and isinstance(meta.get("rewritten_query"), str),
                        "missing_search_query")
                memory.update(original_query=meta["query"], query=meta["rewritten_query"])
            engine.memories[e["actor"]].append(memory)
        elif kind == "private_consultation":
            require(e["actor"] == "R_DEFENDANT" and e["audience"] == ["R_DEFENSE", "R_DEFENDANT"],
                    "private_context_leak")
        elif kind == "file_motion":
            require(e["actor"] in LAWYERS and isinstance(meta.get("target"), str), "invalid_motion")
            engine.pending_motions.append(e["event_id"])
        elif kind == "evidence":
            require(e["actor"] in LAWYERS and e["references"], "invalid_evidence")
            engine.pending_evidence.append(e["event_id"])
        elif kind in {"motion_ruling", "evidence_ruling", "objection_ruling"}:
            target = seen.get(meta.get("target"))
            require(e["actor"] == "R_JUDGE" and target and target is not e and e["text"]
                    and e["references"] and set(e["references"]) <= engine.legal_ids(), "invalid_ruling")
            r = {"decision": meta.get("decision"), "reason": e["text"],
                 "legal_source_ids": e["references"], "permitted_purposes": meta.get("permitted_purposes", [])}
            if kind == "evidence_ruling":
                require(target["kind"] == "evidence" and r["decision"] in
                        {"admitted", "limited_use", "excluded", "pending_foundation"}, "invalid_evidence_ruling")
                require(isinstance(r["permitted_purposes"], list)
                        and (r["decision"] not in {"admitted", "limited_use"} or r["permitted_purposes"]),
                        "missing_permitted_purposes")
                engine.rulings.append({**r, "target_event_id": target["event_id"],
                                       "ruling_stage": engine.stage, "human_review_required": True})
                if target["event_id"] in engine.pending_evidence:
                    engine.pending_evidence.remove(target["event_id"])
            elif kind == "motion_ruling":
                require(target["kind"] == "file_motion" and target["event_id"] in engine.pending_motions
                        and r["decision"] in {"grant_exclusion", "deny"}, "invalid_motion_ruling")
                if r["decision"] == "grant_exclusion":
                    source = target["meta"]["target"]
                    engine.exclusions[source] = r
                    for prior in engine.events:
                        if prior["kind"] == "evidence" and source in prior["references"]:
                            prior["meta"]["stricken"] = True
                            engine.rulings.append({**r, "decision": "excluded", "target_event_id": prior["event_id"]})
                engine.pending_motions.remove(target["event_id"])
            else:
                require(unresolved_objections and unresolved_objections[-1]["meta"]["target"] == target["event_id"]
                        and r["decision"] in {"sustain", "overrule"}, "invalid_objection_ruling")
                unresolved_objections.pop()
                if r["decision"] == "sustain":
                    target["meta"]["stricken"] = True
                    if target["kind"] == "evidence":
                        engine.rulings.append({**r, "decision": "excluded", "target_event_id": target["event_id"]})
                    struck = {target["event_id"]}
                    for prior in engine.events:
                        if prior["kind"] == "testimony" and prior["meta"].get("source_question_event_id") == target["event_id"]:
                            prior["meta"]["stricken"] = True
                            struck.add(prior["event_id"])
                    for prior in engine.events:
                        if prior["kind"] == "evidence" and struck.intersection(prior["references"]):
                            prior["meta"]["stricken"] = True
                            engine.rulings.append({**r, "decision": "excluded", "target_event_id": prior["event_id"]})
        elif kind == "objection":
            target = seen.get(meta.get("target"))
            require(target and target["kind"] in {"question", "testimony", "evidence"}, "invalid_objection_target")
            unresolved_objections.append(e)
        elif kind == "call_witness":
            require(meta.get("witness") in engine.case.roles, "unknown_witness")
            engine.active_witness = meta["witness"]
        elif kind == "question":
            witness = meta.get("witness")
            require(witness in engine.case.stages[engine.stage].get("questionable_role_ids", [])
                    and e["actor"] in LAWYERS, "unauthorized_question")
            caller = engine.witness_calling_parties.setdefault((engine.stage, witness), e["actor"])
            require(meta.get("examination_mode") == ("direct" if e["actor"] == caller else "cross"),
                    "examination_mode_mismatch")
        elif kind == "testimony_election":
            require(e["actor"] == "R_DEFENDANT" and e["text"] in {"elected", "declined"}, "invalid_election")
            engine.testimony_elected |= e["text"] == "elected"
        elif kind == "run_stopped":
            require(index == len(originals), "earlier_stop_not_supported")
    require(not unresolved_objections, "unresolved_objection")
    engine.pending_evidence = [eid for eid in engine.pending_evidence
                               if not engine.event(eid)["meta"].get("stricken") and not engine.last_ruling(eid)]
    require(engine.events == originals, "strike_state_reconstruction_mismatch")


def restore_pending_run(case, backend, source: Path, config: dict) -> CourtEngine:
    """Read legacy artifacts; return a validated state without any model call.

    The caller must use a new output directory and run(resume_completed_stage=True).
    Neither prior files nor prior model outputs are rewritten by this function.
    """
    try:
        source = Path(source).resolve(strict=True)
        hashes = {}

        def read(name):
            path = source / name
            require(path.is_file() and not path.is_symlink() and path.resolve().parent == source,
                    "missing_or_symlinked_artifact:" + name)
            hashes[name] = digest(path)
            raw = path.read_text(encoding="utf-8")
            return [json.loads(line) for line in raw.splitlines() if line.strip()] if name.endswith(".jsonl") else json.loads(raw)

        summary, saved = read("summary.json"), read("run_config.json")
        require(case.case_id == "CA-PILOT-003" and summary.get("case_id") == case.case_id,
                "only_CA_PILOT_003_supported")
        require(summary.get("status") == "incomplete" and summary.get("current_stage") == "r2_examination"
                and summary.get("verdict") is None and summary.get("formal_benchmark_score") is None,
                "unsupported_stop_state")
        require(saved.get("mode") == "live" and saved.get("backend") == "codex_cli"
                and summary.get("mode") == "live", "only_legacy_live_codex_supported")
        require({k: v for k, v in config.items() if k != "mode"} ==
                {k: v for k, v in saved.items() if k != "mode"}, "config_changed")
        runtime, inputs = read("runtime_hashes.json"), read("input_hashes.json")
        require(all(runtime.get(k) == v for k, v in LEGACY_HASHES.items()), "unaudited_legacy_runtime")
        require(set(inputs) == set(INPUT_NAMES) and all(digest(case.path / name) == inputs[name]
                                                       for name in INPUT_NAMES), "case_inputs_changed")
        originals = read("author_events.jsonl")
        require(bool(originals) and len(originals) == summary.get("event_count"), "event_count_mismatch")
        require(originals[-1].get("kind") == "run_stopped"
                and originals[-1].get("text") == "RunStopped: pending_foundation_requires_resolution"
                and originals[-1].get("audience") == ["reviewer"], "unsupported_stop_reason")
        require(read("review_session.json") is None, "review_already_started")
        calls = read("model_calls.json")
        stats = summary.get("model_stats", {})
        _validate_calls(calls, stats, saved)
        require(not backend.calls and backend.stats.get("attempted_calls", 0) == 0,
                "recovery_requires_fresh_backend")
        engine = CourtEngine(case, backend, deepcopy(saved))
        _replay_state(engine, originals)
        require(engine.stage == "r2_examination" and not engine.pending_motions and not engine.pending_evidence
                and bool(engine.unresolved_foundation()), "unsupported_pending_state")
        count = summary.get("lawyer_actions")
        require(type(count) is int and 0 <= count <= saved.get("max_lawyer_actions", 80), "invalid_action_budget")
        engine.action_count = count
        require(summary.get("rejected_actions") == sum(e["kind"] == "action_rejected" for e in originals),
                "rejected_action_count_mismatch")
        for role in case.roles:
            require(engine.visible_record(role) == read(f"view_{role}.json"), "role_view_mismatch:" + role)
        require(engine.jury_record() == read("jury_record.json"), "jury_record_mismatch")
        engine.status = "initialized"
        engine.recovery = {
            "type": "legacy_stage_end_development_recovery", "mixed_engine_run": True,
            "source_run": str(source), "source_file_hashes": hashes,
            "legacy_runtime_hashes": runtime,
            "recovery_runtime_hashes": {p.name: digest(p) for p in sorted(Path(__file__).parent.glob("*.py"))},
            "source_event_count": len(originals), "source_model_calls": len(calls),
            "source_model_stats": deepcopy(stats), "source_lawyer_actions": count,
            "resume_stage": "r2_examination", "completed_stage_actions_replayed": False,
            "validation": "input_hashes_and_reconstructed_role_and_jury_views_match",
            "normalized_unrecorded_fields": "Motion and objection ruling permitted_purposes were not saved; normalized to [] for exclusions only, never used to admit evidence.",
            "limitation": "Development continuation with a changed closure protocol; not a uniform-version benchmark run.",
        }
        # Carry budget/accounting forward, while retaining the current adapter's
        # metadata. No historical calls are executed or charged again here.
        old_stats = deepcopy(stats)
        old_stats.update({k: deepcopy(v) for k, v in backend.stats.items() if k not in COUNTERS})
        backend.calls, backend.stats = deepcopy(calls), old_stats
        engine.emit("recovery_started", text="已核验旧阶段末状态；只补充未决证据收尾，不重复已完成庭审行动。",
                    audience=["reviewer"], source_event_count=len(originals), mixed_engine_run=True)
        return engine
    except RecoveryError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RecoveryError("recovery_refused: invalid_or_missing_artifacts") from exc


def _foundation_snapshot(engine):
    """Validate the published v0.4.0 closure suffix; do not invent a ruling."""
    starts = [i for i, event in enumerate(engine.events) if event["kind"] == "foundation_closure_started"]
    require(len(starts) == 1, "expected_one_uncommitted_foundation_closure")
    start_index = starts[0]
    start = engine.events[start_index]
    meta = start["meta"]
    targets = engine.unresolved_foundation()
    require(start["actor"] == "trusted_orchestrator" and start["audience"] == COURT
            and start["stage_id"] == "r1_ruling" and meta.get("target_event_ids") == targets
            and meta.get("protocol_version") == "foundation_closure_v1"
            and meta.get("new_testimony_allowed") is False
            and meta.get("native_or_external_tools_allowed") is False, "closure_start_mismatch")
    common = [deepcopy(event) for event in engine.events[:start_index + 1]
              if set(COURT) <= set(event["audience"])]
    common_ids = {event["event_id"] for event in common}
    submissions, rejected = [], []
    seen = {role: [] for role in LAWYERS}
    for event in engine.events[start_index + 1:-1]:
        role, meta = event["actor"], event["meta"]
        require(role in LAWYERS, "unexpected_event_after_foundation_start")
        if event["kind"] == "action_rejected":
            require(event["text"] == "invalid_foundation_closure_submission"
                    and meta.get("closure_submission") is True
                    and event["audience"] == [role] and role not in rejected and not seen[role],
                    "invalid_saved_closure_rejection")
            rejected.append(role)
            continue
        require(event["kind"] == "foundation_closure_submission" and event["audience"] == COURT
                and meta.get("not_evidence") is True and role not in rejected, "invalid_saved_closure_submission")
        target = meta.get("target")
        require(target in targets and target not in seen[role]
                and meta.get("requested_decision") in {"no_position", "admitted", "limited_use", "excluded"}
                and isinstance(meta.get("permitted_purposes"), list)
                and all(isinstance(p, str) for p in meta["permitted_purposes"]), "saved_submission_target_or_shape")
        allowed_refs = common_ids | engine.legal_ids()
        for row in engine.catalog():
            try:
                allowed_refs.add(engine.source(role, row["citation_id"])["citation_id"])
            except ValueError:
                continue
        require(set(event["references"]) <= allowed_refs, "saved_submission_reference_unavailable")
        seen[role].append(target)
        submissions.append(deepcopy(event))
    for role in LAWYERS:
        require((role in rejected and not seen[role]) or sorted(seen[role]) == sorted(targets),
                "incomplete_saved_party_submission")
    # The source engine requested the prosecution's entire batch first.
    actor_sequence = [event["actor"] for event in engine.events[start_index + 1:-1]]
    require(actor_sequence == sorted(actor_sequence, key=LAWYERS.index), "saved_submission_order_mismatch")
    return {
        "target_event_ids": targets, "common_record": common, "submissions": submissions,
        "frozen_evidence": {eid: deepcopy(engine.event(eid)) for eid in targets},
        "closure_start_event_id": start["event_id"], "rejected_role_ids": rejected,
    }


def restore_foundation_finalization_run(case, backend, source: Path, config: dict) -> CourtEngine:
    """Restore only fresh v0.4.0 case 004's rejected final foundation ruling.

    Reuses the already recorded party submissions. The rejected judge JSON was
    not saved by that version and is explicitly unavailable, not reconstructed.
    """
    try:
        source = Path(source).resolve(strict=True)
        hashes = {}

        def read(name):
            path = source / name
            require(path.is_file() and not path.is_symlink() and path.resolve().parent == source,
                    "missing_or_symlinked_artifact:" + name)
            hashes[name] = digest(path)
            raw = path.read_text(encoding="utf-8")
            return [json.loads(line) for line in raw.splitlines() if line.strip()] if name.endswith(".jsonl") else json.loads(raw)

        summary, saved = read("summary.json"), read("run_config.json")
        expected_stop = "RunStopped: invalid_final_foundation_reasons_or_law"
        require(case.case_id == "CA-PILOT-004" and summary.get("case_id") == case.case_id,
                "only_CA_PILOT_004_finalization_supported")
        require(summary.get("status") == "incomplete" and summary.get("current_stage") == "r1_ruling"
                and summary.get("stop_reason") == expected_stop and summary.get("verdict") is None
                and summary.get("formal_benchmark_score") is None and summary.get("recovery") is None
                and summary.get("execution_kind") == "fresh_development"
                and summary.get("protocol_version") == "foundation_closure_v1", "unsupported_finalization_stop")
        require(saved.get("mode") == "live" and saved.get("backend") == "codex_cli"
                and summary.get("mode") == "live", "only_live_codex_supported")
        require({k: v for k, v in config.items() if k != "mode"} ==
                {k: v for k, v in saved.items() if k != "mode"}, "config_changed")
        runtime, inputs = read("runtime_hashes.json"), read("input_hashes.json")
        require(all(runtime.get(k) == v for k, v in FOUNDATION_040_HASHES.items()), "unaudited_040_runtime")
        require(set(inputs) == set(INPUT_NAMES) and all(digest(case.path / name) == inputs[name]
                                                       for name in INPUT_NAMES), "case_inputs_changed")
        originals = read("author_events.jsonl")
        require(bool(originals) and len(originals) == summary.get("event_count"), "event_count_mismatch")
        require(originals[-1].get("kind") == "run_stopped" and originals[-1].get("text") == expected_stop
                and originals[-1].get("audience") == ["reviewer"], "unsupported_stop_reason")
        require(read("review_session.json") is None, "review_already_started")
        calls, stats = read("model_calls.json"), summary.get("model_stats", {})
        _validate_calls(calls, stats, saved)
        require(len(calls) >= 3 and [c.get("role_id") for c in calls[-3:]] == [*LAWYERS, "R_JUDGE"],
                "finalization_call_sequence_mismatch")
        require(not backend.calls and backend.stats.get("attempted_calls", 0) == 0,
                "recovery_requires_fresh_backend")
        engine = CourtEngine(case, backend, deepcopy(saved))
        _replay_state(engine, originals, allow_foundation_closure=True)
        require(engine.stage == "r1_ruling" and not engine.pending_motions and not engine.pending_evidence
                and bool(engine.unresolved_foundation()), "unsupported_pending_state")
        snapshot = _foundation_snapshot(engine)
        count = summary.get("lawyer_actions")
        require(type(count) is int and 0 <= count <= saved.get("max_lawyer_actions", 80), "invalid_action_budget")
        engine.action_count = count
        require(summary.get("rejected_actions") == sum(e["kind"] == "action_rejected" for e in originals),
                "rejected_action_count_mismatch")
        for role in case.roles:
            require(engine.visible_record(role) == read(f"view_{role}.json"), "role_view_mismatch:" + role)
        require(engine.jury_record() == read("jury_record.json"), "jury_record_mismatch")
        engine.status = "initialized"
        engine._recovery_foundation_state = snapshot
        engine.recovery = {
            "type": "040_final_foundation_development_recovery", "mixed_engine_run": True,
            "resume_mode": "foundation_finalization",
            "source_run": str(source), "source_file_hashes": hashes, "legacy_runtime_hashes": runtime,
            "recovery_runtime_hashes": {p.name: digest(p) for p in sorted(Path(__file__).parent.glob("*.py"))},
            "source_event_count": len(originals), "source_model_calls": len(calls),
            "source_model_stats": deepcopy(stats), "source_lawyer_actions": count, "resume_stage": "r1_ruling",
            "completed_stage_actions_replayed": False, "completed_party_submissions_replayed": False,
            "foundation_finalization": {
                "target_event_ids": snapshot["target_event_ids"],
                "closure_start_event_id": snapshot["closure_start_event_id"],
                "submission_event_ids": [e["event_id"] for e in snapshot["submissions"]],
                "rejected_role_ids": snapshot["rejected_role_ids"],
            },
            "missing_rejected_payload": True,
            "rejected_judge_output": "Source v0.4.0 saved call metadata and a generic validation failure only; the rejected final ruling JSON is unavailable and has not been fabricated.",
            "validation": "Input hashes, closure submissions, call accounting, and reconstructed role/jury views match.",
            "normalized_unrecorded_fields": "Motion/objection permitted_purposes normalized to [] only for exclusions; never used to admit evidence.",
            "limitation": "Development continuation under a changed ruling contract; not a uniform-version benchmark result.",
        }
        old_stats = deepcopy(stats)
        old_stats.update({k: deepcopy(v) for k, v in backend.stats.items() if k not in COUNTERS})
        backend.calls, backend.stats = deepcopy(calls), old_stats
        engine.emit("recovery_started", text="已核验旧基础审查及双方意见；仅重新请求未获采纳的最终裁定，再继续后续庭审。",
                    audience=["reviewer"], source_event_count=len(originals), mixed_engine_run=True,
                    party_submissions_reused=True, rejected_judge_payload_available=False)
        return engine
    except RecoveryError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RecoveryError("recovery_refused: invalid_or_missing_artifacts") from exc
