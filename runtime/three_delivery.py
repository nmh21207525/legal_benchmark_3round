"""Offline three-round delivery, preserving the two-round raw and teacher layouts.

This exporter never calls a model and never invents missing turns or scores.
Teacher pages contain privileged case data; role pages are filtered server-side.
"""
from __future__ import annotations

import csv
import hashlib
from html import escape
import io
import json
import os
from pathlib import Path
import re
import shutil
from urllib.parse import quote
import zipfile

PROTOCOL = "three-round-delivery-v1"
COLUMNS = ("candidate", "model", "case_id", "status", "numerator", "denominator", "rubric_coverage_percent", "error_code", "court_dir", "warning_count")
NAMES = {"seed": "豆包", "deepseek": "DeepSeek", "opus": "Claude_Opus", "gpt": "GPT"}
ROLES = {"R_DEFENSE": "辩方律师", "R_PROSECUTOR": "控方律师", "R_JUDGE": "法官", "R_JURY": "陪审团", "R_DEFENDANT": "被告", "R_OFFICER": "警员", "R_TOXICOLOGIST": "毒理记录", "R_SOURCE_RECORD": "来源记录", "trusted_orchestrator": "环境"}
STAGES = {"pretrial_workspace": "庭前文件阅读、检索与准备", "preparation": "庭前咨询与准备", "r1_motion": "第一轮 · 动议", "r1_inquiry": "第一轮 · 程序询问", "r1_ruling": "第一轮 · 裁定", "between_r1_r2": "第一、二轮之间准备", "r2_opening": "第二轮 · 开庭陈述", "r2_examination": "第二轮 · 举证询问", "r2_evidence": "第二轮 · 证据处理", "between_r2_r3": "第二、三轮之间准备", "r3_clarification": "第三轮 · 补充询问", "r3_closing": "第三轮 · 结案陈词", "r3_instructions": "第三轮 · 法官指示", "r3_verdict": "第三轮 · 裁决", "ended": "结束"}
DATA_EXT = {".json", ".jsonl", ".md", ".txt", ".log", ".csv", ".tsv", ".html", ".htm", ".pdf", ".docx", ".xlsx", ".png", ".jpg", ".jpeg", ".webp"}
RUN_FILES = {"private_model_io.jsonl", "private_model_io.jsonl.head.json", "action_attempts.jsonl", "action_attempts.jsonl.head.json", "court_complete.json", "failure.json", "run_failure.json", "status.json", "journal.jsonl", "journal.jsonl.head.json", "terminal.log", "run_manifest.json"}
ATTEMPT_FILES = {"author_events.jsonl", "jury_record.json", "summary.json", "model_calls.json", "input_hashes.json", "runtime_hashes.json", "review_session.json", "foundation_diagnostics.json", "environment_manifest.json", "workflow_summary.json", "artifact_manifest.json", "replay.html"}
SKIP = {"__pycache__", "node_modules", ".venv", "venv", ".git", ".pytest_cache", "runs", "outputs", "source_packages"}
STYLE = """body{margin:0;background:#f4f6f9;color:#203047;font:16px/1.7 system-ui,sans-serif}main{max-width:1120px;margin:auto;padding:28px 22px}h1{font-size:28px}h2{font-size:22px}a{color:#185d9b}nav,.controls,.line{display:flex;gap:12px;flex-wrap:wrap}.notice{background:#fff0ce;border-left:4px solid #ad8225;padding:13px 16px}article,.card{background:white;border:1px solid #dae1ea;padding:16px 20px;margin:13px 0;border-radius:9px}.line{color:#69788b;font-size:14px}.line b{color:#173d61}p,pre{white-space:pre-wrap;overflow-wrap:anywhere}pre{font-size:13px;background:#f1f4f8;padding:12px;max-height:600px;overflow:auto}table{width:100%;border-collapse:collapse;background:white}td,th{padding:11px;text-align:left;border-bottom:1px solid #dce3eb;vertical-align:top}code{overflow-wrap:anywhere}summary{cursor:pointer;color:#3d5873}.question{border-left:3px solid #3273ad;padding:8px 14px;background:#edf5fc}.answer{border-left:3px solid #308b72;padding:8px 14px;background:#eef8f4}select,input{font:inherit;padding:7px;border:1px solid #bfcbd8;border-radius:5px}.muted{color:#6a7888}section{scroll-margin-top:20px}small{font-weight:400;color:#67778a}article[hidden],section[hidden]{display:none}@media print{.controls,nav{display:none}body{background:white}main{padding:0}article{break-inside:avoid}a{color:inherit}}"""
FILTER_SCRIPT = """(()=>{const role=document.getElementById('role'),stage=document.getElementById('stage'),q=document.getElementById('search'),count=document.getElementById('visible-count');function apply(){let n=0;document.querySelectorAll('article.event').forEach(el=>{el.hidden=!!((role.value&&el.dataset.role!==role.value)||(stage.value&&el.dataset.stage!==stage.value)||(q.value&&!el.textContent.toLowerCase().includes(q.value.toLowerCase())));if(!el.hidden)n++;});document.querySelectorAll('section.stage').forEach(el=>el.hidden=![...el.querySelectorAll('article.event')].some(x=>!x.hidden));count.textContent='当前显示 '+n+' 条实际事件';} [role,stage,q].forEach(el=>el.addEventListener('input',apply));apply();})();"""


class DeliveryError(ValueError):
    pass


def _read(path, default=None):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8-sig")) if p.is_file() else default


def _sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _write(path, value):
    p = Path(path)
    if p.is_symlink():
        raise DeliveryError("symlink_output")
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n" if isinstance(value, (dict, list)) else value
    temp = p.with_name(p.name + ".writing")
    with temp.open("w", encoding="utf-8") as f:
        os.chmod(temp, 0o600)
        f.write(text)
    os.replace(temp, p)


def _safe(root, relative):
    root = Path(root).resolve()
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise DeliveryError("unsafe_relative_path")
    p = root / rel
    if p.is_symlink() or p.resolve() != p:
        raise DeliveryError("symlink_source")
    return p


def _copy(src, dst):
    src, dst = Path(src), Path(dst)
    if src.is_symlink() or dst.is_symlink():
        raise DeliveryError("symlink_file")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.is_file() or src.stat().st_size != dst.stat().st_size or _sha(src) != _sha(dst):
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o600)


def _tree(src, dst, suffixes=DATA_EXT, *, skip=()):
    src, dst = Path(src), Path(dst)
    if src.is_symlink():
        raise DeliveryError("symlink_tree")
    if not src.is_dir():
        return
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if any(x in skip or x.startswith(".") for x in rel.parts):
            continue
        if p.is_symlink():
            raise DeliveryError("symlink_tree_member")
        if p.is_file() and p.suffix.lower() in suffixes:
            _copy(_safe(src, rel), dst / rel)


def _href(path):
    return "/".join(quote(part, safe="") for part in Path(path).parts)


def _link(path, label):
    return '<a href="' + escape(_href(path), quote=True) + '">' + escape(str(label)) + '</a>'


def _page(title, body, *, script=""):
    policy = "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; connect-src 'none'"
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="' + escape(policy, quote=True) + '"><title>' + escape(title) + '</title><style>' + STYLE + '</style></head><body><main><h1>' + escape(title) + '</h1>' + body + '</main>' + ('<script>' + script + '</script>' if script else '') + '</body></html>'


def _events(path):
    if not Path(path).is_file():
        return []
    result = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise DeliveryError(f"invalid_event_at_line_{n}")
            result.append(value)
    ids = [e.get("event_id") for e in result]
    if len(ids) != len(set(ids)):
        raise DeliveryError("duplicate_event_id")
    return result


def replay_html(case_id, candidate, events, artifacts=(), *, audience=None, status="pending", prefix=""):
    """Render only saved records; consultation question is a saved meta field."""
    events = [e for e in events if audience is None or audience in e.get("audience", []) or "*" in e.get("audience", [])]
    by_id = {e.get("event_id"): e for e in events}
    stage_names = list(dict.fromkeys(str(e.get("stage_id", "unknown")) for e in events))
    actors = sorted({str(e.get("actor", "unknown")) for e in events})
    status_text = f"状态：{status}。本页展示已保存过程，不会补写缺失对话。"
    notice = "教师审阅视图，包含角色私有资料和庭前交互，不得整体作为参评模型输入。" if audience is None else f"仅包含 {audience} 获准看到的事件；不嵌入其他角色私有事件。"
    body = '<p class="notice">' + escape(notice) + '</p><p>' + escape(status_text) + '</p>'
    body += '<div class="controls"><label>角色 <select id="role"><option value="">全部</option>' + ''.join('<option value="' + escape(r, quote=True) + '">' + escape(ROLES.get(r, r)) + '</option>' for r in actors) + '</select></label><label>阶段 <select id="stage"><option value="">全部</option>' + ''.join('<option value="' + escape(s, quote=True) + '">' + escape(STAGES.get(s, s)) + '</option>' for s in stage_names) + '</select></label><label>搜索 <input id="search" type="search" placeholder="问题、回答、工具、产物"></label><small id="visible-count"></small></div>'
    body += '<nav>' + ' · '.join('<a href="#stage-' + escape(s, quote=True) + '">' + escape(STAGES.get(s, s)) + '</a>' for s in stage_names) + '</nav>'
    artifact_ids = {a.get("artifact_id"): a for a in artifacts if audience is None or a.get("actor") == audience}
    for stage in stage_names:
        body += '<section class="stage" id="stage-' + escape(stage, quote=True) + '"><h2>' + escape(STAGES.get(stage, stage)) + '</h2>'
        for e in [x for x in events if str(x.get("stage_id", "unknown")) == stage]:
            eid, actor, kind = str(e.get("event_id", "")), str(e.get("actor", "")), str(e.get("kind", ""))
            meta = e.get("meta", {}) if isinstance(e.get("meta", {}), dict) else {}
            body += '<article class="event" id="' + escape(eid, quote=True) + '" data-role="' + escape(actor, quote=True) + '" data-stage="' + escape(stage, quote=True) + '"><div class="line"><b>' + escape(ROLES.get(actor, actor)) + '</b><span>' + escape(kind) + '</span><small>' + escape(eid) + '</small></div>'
            if kind == "private_consultation":
                if meta.get("question") is not None:
                    question = meta["question"] if isinstance(meta["question"], str) else json.dumps(meta["question"], ensure_ascii=False)
                    body += '<div class="question"><b>辩方律师提问（原记录）</b><p>' + escape(question) + '</p></div>'
                body += '<div class="answer"><b>' + escape(ROLES.get(actor, actor)) + '回答（原记录）</b><p>' + escape(str(e.get("text", ""))) + '</p></div>'
            else:
                body += '<p>' + escape(str(e.get("text", ""))) + '</p>'
            if kind == "tool_call":
                body += '<p><b>工具请求：' + escape(str(meta.get("tool", ""))) + '</b></p><pre>' + escape(json.dumps(meta.get("arguments", {}), ensure_ascii=False, indent=2)) + '</pre>'
                replies = [x for x in events if x.get("kind") == "tool_result" and x.get("meta", {}).get("call_event_id") == eid]
                body += '<p>' + (' · '.join('<a href="#' + escape(x["event_id"], quote=True) + '">查看实际工具反馈</a>' for x in replies) if replies else '尚无已保存工具反馈。') + '</p>'
            if kind == "tool_result":
                target = meta.get("call_event_id")
                if target in by_id:
                    body += '<a href="#' + escape(str(target), quote=True) + '">对应工具请求</a>'
                body += '<pre>' + escape(json.dumps(meta.get("result", {}), ensure_ascii=False, indent=2)) + '</pre>'
            aid = meta.get("artifact", {}).get("artifact_id") if isinstance(meta.get("artifact"), dict) else None
            if aid in artifact_ids:
                body += '<p>' + _link(prefix + artifact_ids[aid]["delivery_path"], "打开实际文件：" + artifact_ids[aid].get("name", aid)) + '</p>'
            body += '<details><summary>引用、可见角色与原始事件字段</summary><pre>' + escape(json.dumps({"references": e.get("references", []), "audience": e.get("audience", []), "meta": meta}, ensure_ascii=False, indent=2)) + '</pre></details></article>'
        body += '</section>'
    if not events:
        body += '<p>尚无已保存庭审事件。运行后在场次边界更新。</p>'
    if artifact_ids:
        body += '<h2>实际文件产物</h2><ul>' + ''.join('<li>' + _link(prefix + a["delivery_path"], a.get("name", a["path"])) + ' · 版本 ' + escape(str(a.get("version", ""))) + ' · ' + escape(str(a.get("actor", ""))) + '</li>' for a in artifact_ids.values()) + '</ul>'
    return _page(case_id + ' · ' + NAMES.get(candidate, candidate) + ' · 全流程回放', body, script=FILTER_SCRIPT)


def _select_run(out, row):
    default = out / "courts" / row["candidate"] / row["case_id"]
    run, attempt = default, None
    value = row.get("court_dir")
    if value:
        p = Path(value)
        p = p if p.is_absolute() else _safe(out, p)
        if p.name.startswith("attempt-") and p.parent.name == "court_attempts":
            attempt, run = p, p.parent.parent
        else:
            run = p
    marker = _read(run / "court_complete.json", {})
    complete = False
    if marker:
        attempt = _safe(run, marker.get("attempt", ""))
        hashes = marker.get("files_sha256") or marker.get("hashes")
        if not hashes:
            raise DeliveryError("completed_run_missing_hashes")
        base = run if marker.get("files_sha256") else attempt
        for name, digest in hashes.items():
            file = _safe(base, name)
            if not file.is_file() or _sha(file) != digest:
                raise DeliveryError("completed_run_hash_mismatch:" + name)
        complete = True
    if attempt is None:
        candidates = sorted((run / "court_attempts").glob("attempt-*"))
        attempt = candidates[-1] if candidates else None
    if run.is_symlink() or (attempt and attempt.is_symlink()):
        raise DeliveryError("symlink_run")
    summary = _read(attempt / "summary.json", {}) if attempt else {}
    complete = complete and summary.get("current_stage") == "ended" and summary.get("status") in {"completed_development_run", "complete", "completed"}
    return run, attempt, complete, summary


def _copy_raw(run, dest):
    for name in RUN_FILES:
        if (run / name).is_file():
            _copy(_safe(run, name), dest / name)
    for attempt in sorted((run / "court_attempts").glob("attempt-*")):
        if attempt.is_symlink():
            raise DeliveryError("symlink_attempt")
        for p in attempt.iterdir():
            if p.is_file() and (p.name in ATTEMPT_FILES or re.fullmatch(r"view_[A-Za-z0-9_-]+\.json", p.name)):
                _copy(_safe(attempt, p.name), dest / "court_attempts" / attempt.name / p.name)
        _tree(attempt / "artifacts", dest / "court_attempts" / attempt.name / "artifacts")
    for name in ("private_failed_outputs", "raw_responses", "private_raw_responses", "private_scoring_outputs", "full_workflow_score", "direct_score", "score"):
        _tree(run / name, dest / name, DATA_EXT | {".lock"})


def _score_page(score, row):
    if not score:
        return _page("评分尚未生成", '<p>本次尚无可用评分。开发模式或 rubric 未冻结时不生成正式成绩，空白不代表 0 分。</p>')
    body = '<p>状态：' + escape(str(score.get("status", row.get("status", "unknown")))) + '</p>'
    body += '<p>得分：' + escape(str(row.get("numerator", ""))) + ' / ' + escape(str(row.get("denominator", ""))) + '</p>'
    for point in score.get("points", score.get("items", [])):
        body += '<article><b>' + escape(str(point.get("point_id", point.get("id", "")))) + '</b> · ' + escape(str(point.get("score", ""))) + '/2<p>' + escape(str(point.get("reason", ""))) + '</p>'
        if point.get("review_warnings"):
            body += '<p class="notice">复核提示：' + escape('；'.join(map(str, point["review_warnings"]))) + '</p>'
        body += '<details><summary>保存的评分依据</summary><pre>' + escape(json.dumps(point, ensure_ascii=False, indent=2)) + '</pre></details></article>'
    return _page("独立 rubric 评分", body)


def _export_run(out, row, dest, *, mode, mock):
    run, attempt, complete, summary = _select_run(out, row)
    _copy_raw(run, dest / "原始记录")
    events = _events(attempt / "author_events.jsonl") if attempt else []
    artifacts = []
    if attempt:
        artifact_root = attempt / "artifacts"
        manifest = _read(artifact_root / "manifest.json", {})
        for item in manifest.get("artifacts", []):
            file = _safe(artifact_root, item["path"])
            if not file.is_file() or _sha(file) != item.get("sha256"):
                raise DeliveryError("artifact_hash_mismatch")
            _copy(file, dest / "文件产物" / item["path"])
            artifacts.append({**item, "delivery_path": "文件产物/" + item["path"]})
        if (artifact_root / "manifest.json").is_file():
            _copy(artifact_root / "manifest.json", dest / "文件产物/manifest.json")
    display = ("MOCK 离线演练，非模型实验" if mock else ("开发试跑，非正式成绩" if mode != "formal" else "正式模式")) + " · " + str(row.get("status", "pending"))
    _write(dest / "对话回放.html", replay_html(row["case_id"], row["candidate"], events, artifacts, status=display))
    roles = sorted({r for e in events for r in e.get("audience", []) if r != "*"})
    for role in roles:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", role):
            raise DeliveryError("unsafe_role_id")
        _write(dest / "角色视图" / (role + ".html"), replay_html(row["case_id"], row["candidate"], events, artifacts, audience=role, status=display, prefix="../"))
    _write(dest / "角色视图/教师完整视图.html", replay_html(row["case_id"], row["candidate"], events, artifacts, status=display, prefix="../"))
    scores = [run / "direct_score/score.json", run / "full_workflow_score/score.json", run / "score/score.json"]
    score_path = next((p for p in scores if p.is_file()), None)
    score = _read(score_path, {}) if score_path else None
    score_verified = False
    if score_path:
        score_marker = _read(score_path.parent / "score_complete.json", {})
        hashes = score_marker.get("files_sha256", {})
        if hashes:
            for name, digest in hashes.items():
                path = _safe(score_path.parent, name)
                if not path.is_file() or _sha(path) != digest:
                    raise DeliveryError("completed_score_hash_mismatch:" + name)
            score_verified = ("score.json" in hashes and score.get("status") == "complete"
                              and score.get("mock_only") is not True
                              and score.get("numerator") == row.get("numerator")
                              and score.get("denominator") == row.get("denominator"))
    if score_path:
        _tree(score_path.parent, dest / "原始记录/当前评分", DATA_EXT | {".lock"})
    _write(dest / "评分.html", _score_page(score, row))
    links = _link("对话回放.html", "庭前准备与三轮庭审回放") + ' · ' + _link("评分.html", "独立评分")
    if roles:
        links += '<p>角色视图：' + ' · '.join(_link("角色视图/" + r + ".html", ROLES.get(r, r)) for r in roles) + '</p>'
    _write(dest / "index.html", _page(row["case_id"] + ' · ' + NAMES.get(row["candidate"], row["candidate"]), '<p>' + escape(display) + '</p>' + links))
    return {"candidate": row["candidate"], "case_id": row["case_id"], "court_complete": complete, "score_present": score is not None, "score_verified": score_verified, "score_rubric_freeze_sha256": (score or {}).get("rubric_freeze_sha256"), "run_mode": summary.get("mode"), "events": len(events), "artifacts": len(artifacts), "status": row.get("status"), "attempt": attempt.name if attempt else None, "court_summary": summary.get("status")}


def _assert_no_secrets(value):
    """Inspect metadata without mistaking token-budget policy fields for keys."""
    names = {"api_key", "apikey", "token", "access_token", "refresh_token", "password",
             "secret", "authorization", "credential", "credentials", "access_key", "private_key"}
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower().replace("-", "_") in names and child not in (None, ""):
                raise DeliveryError("embedded_credentials")
            _assert_no_secrets(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_secrets(child)
    elif isinstance(value, str) and re.search(r"\b(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9_.~-]{12,})", value):
        raise DeliveryError("embedded_credentials")


def _environment(package_root, destination):
    """Only source code, declared data and example config; never copy live config."""
    root = Path(package_root).resolve()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in SKIP or part.startswith(".") for part in rel.parts):
            continue
        if p.is_symlink():
            raise DeliveryError("symlink_environment")
        if not p.is_file():
            continue
        is_data = rel.parts[0] == "data"
        allowed = (is_data and p.suffix.lower() in DATA_EXT) or p.suffix in {".py", ".sh", ".md"} or p.name in {"requirements.txt", "requirements.lock", "config.example.json", "protocol.json"}
        if not allowed or (p.name.startswith("config") and p.suffix == ".json" and p.name != "config.example.json"):
            continue
        if p.name == "config.example.json":
            from delivery import sanitize_config
            _write(destination / rel, sanitize_config(_read(p)))
        else:
            _copy(p, destination / rel)
    _write(destination / "交付环境说明.md", "# 运行环境\n\n此目录保留程序内部文件名和数据路径。使用根目录 README 中的安装、校验、开发及正式运行步骤。API 密钥由接收者另行提供，不在交付包中。运行需要对应模型服务和 CLI 可用；已保存记录可以离线查看，但重新请求模型不能保证逐字重现。\n")


def _case_data(asset, dest):
    source, workflow = Path(asset["case_path"]), Path(asset["workflow_root"])
    _tree(source, dest / "底层案卷_按权限披露")
    for name in ("case.md", "materials.jsonl", "legal_sources.jsonl"):
        p = source / "public" / name
        if p.is_file():
            _copy(p, dest / {"case.md": "案件说明.md", "materials.jsonl": "材料段落.jsonl", "legal_sources.jsonl": "法律资料.jsonl"}[name])
    _tree(source / "config", dest / "流程与权限")
    _tree(source / "role_private", dest / "角色私有设定_教师审阅")
    _tree(source / "authoring", dest / "来源与制作参考_教师审阅")
    if (workflow / "task.md").is_file():
        _copy(workflow / "task.md", dest / "题目要求.md")
    _tree(workflow, dest / "庭前工作区定义")
    spec = asset.get("workflow_spec", {})
    documents = []
    for item in spec.get("files", []):
        p = _safe(workflow, item["path"])
        if p.is_file():
            target = Path("输入材料") / Path(item["path"])
            _copy(p, dest / target)
            documents.append('<li>' + _link(target, item.get("title", item.get("file_id", p.name))) + '</li>')
    body = '<p class="notice">教师审阅数据含私有角色与制作资料。正式角色只读取引擎按阶段授权的视图。</p>'
    if (dest / "题目要求.md").is_file():
        body += '<h2>题目要求</h2><pre>' + escape((dest / "题目要求.md").read_text(encoding="utf-8")) + '</pre>'
    body += '<h2>输入材料</h2><ul>' + ''.join(documents) + '</ul><h2>底层数据</h2><ul>' + ''.join('<li>' + _link(p.relative_to(dest), str(p.relative_to(dest))) + '</li>' for p in sorted(dest.rglob("*")) if p.is_file() and p.suffix in {".json", ".jsonl", ".md"}) + '</ul>'
    _write(dest / "index.html", _page(asset["case_id"] + " · 题目与数据", body))


def _rubric(asset, dest):
    value = asset.get("rubric", {})
    _write(dest / "rubric.json", value)
    points = value.get("points", value.get("items", []))
    ready = (bool(points) and value.get("approved") is True and value.get("frozen") is True
             and value.get("status") == "frozen" and value.get("protocol") == "three-round-frozen-rubric-v1")
    body = '<p>' + ("已提供评分项；冻结状态以运行校验及清单为准。" if points else "rubric 尚未完成，当前不生成正式分数。") + '</p>'
    body += '<p>项目数：' + str(len(points)) + ' · 状态：' + escape(str(value.get("status", "未标注"))) + '</p>'
    for p in points:
        body += '<article><b>' + escape(str(p.get("point_id", p.get("id", "")))) + '</b><pre>' + escape(json.dumps(p, ensure_ascii=False, indent=2)) + '</pre></article>'
    body += '<p>' + _link("rubric.json", "完整 rubric JSON") + '</p>'
    _write(dest / "index.html", _page(asset["case_id"] + " · rubric", body))
    return ready, len(points)


def _inventory(root):
    return {p.relative_to(root).as_posix(): {"sha256": _sha(p), "bytes": p.stat().st_size} for p in sorted(root.rglob("*")) if p.is_file() and p.name != "文件校验清单.json" and not p.name.endswith(".writing")}


def _zip(source, target):
    temp = target.with_name(target.name + ".writing")
    with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for p in sorted(source.rglob("*")):
            if p.is_file():
                archive.write(p, arcname=source.name + "/" + p.relative_to(source).as_posix())
    os.replace(temp, target)
    return {"file": target.name, "sha256": _sha(target), "bytes": target.stat().st_size}


def publish(out, rows, assets, cases, *, mode, mock=False, package_root=None):
    """Refresh an offline dashboard and teacher-format copies, without model calls.

    `court_dir` accepts the existing relative court_attempts/attempt-NNN path.
    Formal ZIPs require saved complete courts, scores, and approved rubric for
    every selected row. Development exports remain explicitly unscored.
    """
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    rows = [dict(r) for r in rows]
    selected = list(cases) if cases else list(dict.fromkeys(r["case_id"] for r in rows))
    for cid in selected:
        if not re.fullmatch(r"CA-PILOT-\d+", cid) or cid not in assets:
            raise DeliveryError("invalid_case_id")
    for r in rows:
        if r.get("case_id") not in selected or not re.fullmatch(r"[A-Za-z0-9_-]+", r.get("candidate", "")):
            raise DeliveryError("invalid_result_row")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    _write(out / "results.csv", stream.getvalue())
    delivery = out / "交付材料"
    delivery.mkdir(exist_ok=True)
    _write(delivery / "评分汇总.csv", stream.getvalue())
    package_root = Path(package_root) if package_root else Path(__file__).resolve().parents[1]
    case_reports, all_runs, case_dirs = [], [], []
    for cid in selected:
        asset = assets[cid]
        dest = delivery / ("第" + cid.rsplit("-", 1)[1] + "题")
        case_dirs.append(dest)
        _case_data(asset, dest / "01_题目与数据")
        ready, count = _rubric(asset, dest / "02_rubric")
        _environment(package_root, dest / "04_运行环境")
        if ready:
            _write(dest / "04_运行环境/rubrics" / (cid + ".json"), asset["rubric"])
        candidates = [r for r in rows if r["case_id"] == cid]
        run_reports, links = [], []
        for row in candidates:
            target = Path("03_实验结果") / NAMES.get(row["candidate"], row["candidate"])
            report = _export_run(out, row, dest / target, mode=mode, mock=mock)
            run_reports.append(report)
            links.append('<li>' + _link(target / "index.html", NAMES.get(row["candidate"], row["candidate"])) + ' · ' + escape(str(row.get("status", "pending"))) + '</li>')
        all_runs.extend(run_reports)
        evidence = {"protocol": PROTOCOL, "case_id": cid, "mode": mode, "mock_only": mock, "rubric_ready": ready, "rubric_points": count, "rubric_freeze_sha256": asset.get("rubric", {}).get("freeze_sha256"), "runs": run_reports, "live_connectivity_tested_by_export": False, "paid_api_calls": 0}
        _write(dest / "05_来源与校验/交付记录.json", evidence)
        for filename in ("matrix_manifest.json", "run_manifest.json", "validation.json", "protocol.json"):
            path = out / filename
            if path.is_file():
                data = _read(path)
                # Inspect recursively for embedded credentials before exporting.
                _assert_no_secrets(data)
                _copy(path, dest / "05_来源与校验" / filename)
        if (Path(asset["case_path"]) / "manifest.json").is_file():
            _copy(Path(asset["case_path"]) / "manifest.json", dest / "05_来源与校验/题目原清单.json")
        readme = f"# {cid} 三轮全流程交付\n\n打开 index.html。01 是题目、输入材料、角色和权限；02 是本题 rubric；03 是各模型实际对话、产物和原始记录；04 是可安装运行的代码与数据；05 是来源和校验记录。\n\n当前模式：{mode}；离线演练：{mock}。开发运行不等同于正式评分。庭前咨询页面展示实际保存的提问与回答，未发生的交互不补造。工具检索以环境声明为准，不把离线资料检索称为联网检索。重新运行会调用模型，结果可能与本次记录不同。\n"
        _write(dest / "阅读说明.md", readme)
        body = '<p class="notice">' + escape("离线演练，无真实模型成绩。" if mock else ("开发试跑，rubric 未冻结时不生成正式成绩。" if mode != "formal" else "正式实验审阅；复核提示需要另行审阅。")) + '</p><nav>' + ' · '.join(_link(p, l) for p, l in (("01_题目与数据/index.html", "题目与数据"), ("02_rubric/index.html", "rubric"), ("阅读说明.md", "阅读说明"), ("04_运行环境/交付环境说明.md", "运行环境"), ("05_来源与校验/交付记录.json", "来源与校验"))) + '</nav><h2>模型运行</h2><ul>' + ''.join(links) + '</ul>'
        _write(dest / "index.html", _page(cid + " · 三轮全流程", body))
        _write(dest / "05_来源与校验/文件校验清单.json", {"protocol": PROTOCOL, "files": _inventory(dest)})
        case_reports.append(evidence)
    all_complete = bool(rows) and len(all_runs) == len(rows) and all(x["court_complete"] for x in all_runs)
    formal_complete = mode == "formal" and not mock and all_complete and all(x["rubric_ready"] for x in case_reports) and all(x["score_verified"] and x["run_mode"] == "live" and x["score_rubric_freeze_sha256"] == assets[x["case_id"]]["rubric"].get("freeze_sha256") for x in all_runs) and all(r.get("status") == "complete" and isinstance(r.get("numerator"), (int, float)) and isinstance(r.get("denominator"), (int, float)) and r["denominator"] > 0 for r in rows)
    status = {"protocol": PROTOCOL, "status": "complete" if formal_complete else ("development_complete" if all_complete and mode != "formal" else "incomplete"), "mode": mode, "mock_only": bool(mock), "complete_courts": sum(x["court_complete"] for x in all_runs), "planned_courts": len(rows), "complete_courts_and_scores": sum(x["court_complete"] and x["score_verified"] for x in all_runs), "teacher_packages_created": formal_complete, "development_export_created": mode != "formal", "review_status": "needs_review" if any(r.get("warning_count") for r in rows) else "clean", "rubric_pending_cases": [x["case_id"] for x in case_reports if not x["rubric_ready"]], "warning_count": sum(int(r.get("warning_count") or 0) for r in rows), "paid_api_calls": 0, "note": "paid_api_calls 仅指本次导出；不代表实验无模型调用。", "cases": case_reports, "packages": []}
    table = '<table><thead><tr><th>题目</th><th>模型</th><th>状态</th><th>得分</th><th>覆盖率</th></tr></thead><tbody>'
    for row in rows:
        rel = Path("交付材料") / ("第" + row["case_id"].rsplit("-", 1)[1] + "题") / "03_实验结果" / NAMES.get(row["candidate"], row["candidate"]) / "index.html"
        score = "待评分" if row.get("numerator") is None else str(row["numerator"]) + "/" + str(row.get("denominator", ""))
        percent = "—" if row.get("rubric_coverage_percent") is None else str(row["rubric_coverage_percent"]) + "%"
        table += '<tr><td>' + escape(row["case_id"]) + '</td><td>' + _link(rel, NAMES.get(row["candidate"], row["candidate"])) + '</td><td>' + escape(str(row.get("status", "pending"))) + '</td><td>' + escape(score) + '</td><td>' + escape(percent) + '</td></tr>'
    table += '</tbody></table>'
    notice = "MOCK 离线演练，无真实模型调用和正式成绩。" if mock else ("开发试跑，正式 rubric 尚未冻结时保持空白分数。" if mode != "formal" else "正式模式。仅完整庭审与有效评分计入完成；复核提示单独保留。")
    body = '<p class="notice">' + escape(notice) + '</p><p>已完成庭审 ' + str(status["complete_courts"]) + '/' + str(len(rows)) + '。网页在场次边界刷新，可离线查看。</p>' + table + '<p>' + _link("results.csv", "下载结果 CSV") + ' · ' + _link("delivery_status.json", "交付状态") + '</p><h2>每题完整目录</h2><ul>' + ''.join('<li>' + _link(Path("交付材料") / d.name / "index.html", d.name) + '</li>' for d in case_dirs) + '</ul>'
    page = _page("三轮庭审 · 全流程实验", body)
    _write(out / "index.html", page)
    _write(out / "report.html", page)
    _write(delivery / "index.html", page.replace('href="' + _href("交付材料") + '/', 'href="').replace('href="results.csv"', 'href="' + _href("评分汇总.csv") + '"').replace('href="delivery_status.json"', 'href="' + _href("交付状态.json") + '"'))
    _write(delivery / "00_先读我.md", "# 三轮庭审交付\n\n打开 index.html。每题目录沿用两轮交付结构，包含庭前准备、工具调用、三轮庭审、实际文件、rubric、运行环境和来源校验。所有页面可离线查看。开发试跑与 MOCK 均不产生正式分数。此包是教师私有审阅材料，不得整体输入参评模型。\n")
    _write(delivery / "核对文件.py", "import hashlib,json,pathlib,sys\np=pathlib.Path(__file__).resolve().parent\nm=json.loads((p/'文件校验清单.json').read_text(encoding='utf-8'))\nerrors=[]\nfor rel,v in m['files'].items():\n f=p/rel\n if f.is_symlink() or not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest()!=v['sha256']: errors.append(rel)\nprint(json.dumps({'status':'failed' if errors else 'verified','paid_api_calls':0,'errors':errors},ensure_ascii=False))\nsys.exit(bool(errors))\n")
    _write(delivery / "交付状态.json", {**status, "packages": []})
    _write(delivery / "文件校验清单.json", {"protocol": PROTOCOL, "files": _inventory(delivery)})
    if formal_complete:
        for dest in case_dirs:
            status["packages"].append(_zip(dest, out / (dest.name + "-完整交付.zip")))
        status["packages"].append(_zip(delivery, out / "teacher-delivery-three-rounds.zip"))
    _write(out / "delivery_status.json", status)
    return status
