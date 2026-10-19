"""Private reviewer delivery: actual traces, files, source inputs and runnable code.

Exports only a named case's run directories and an allowlisted runtime/data tree.
No credentials, host environment dump, arbitrary server directories or invented
conversation records are used. A role view is a separately filtered HTML file,
not a browser-side hiding of private event content.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
from html import escape
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import tempfile
from urllib.parse import quote, urlsplit

PROTOCOL = 'full-workflow-reviewer-delivery-v3'
CASE_FILES = ('manifest.json', 'public/case.md', 'public/materials.jsonl',
              'public/legal_sources.jsonl', 'role_private/roles.json',
              'config/procedure.json', 'config/permissions.json')
WORKFLOW_FILES = ('workflow.json', 'rubric.json', 'source_map.json', 'authoring_record.json', 'task.md')
DATA_SUFFIXES = {'.json', '.jsonl', '.md', '.txt', '.csv', '.tsv', '.pdf', '.docx', '.xlsx', '.html', '.htm', '.png', '.jpg', '.jpeg', '.webp'}
RUN_FILES = {'private_model_io.jsonl', 'private_model_io.jsonl.head.json', 'action_attempts.jsonl',
             'action_attempts.jsonl.head.json', 'court_complete.json', 'failure.json', 'status.json',
             'journal.jsonl', 'journal.jsonl.head.json', 'terminal.log', 'run_manifest.json'}
ATTEMPT_FILES = {'author_events.jsonl', 'jury_record.json', 'summary.json', 'model_calls.json',
                 'input_hashes.json', 'runtime_hashes.json', 'review_session.json',
                 'foundation_diagnostics.json', 'environment_manifest.json', 'workflow_summary.json', 'artifact_manifest.json'}
SAFE_CONFIG = {'backend', 'api', 'models', 'defense_models', 'reasoning_effort', 'max_input_chars',
               'max_output_tokens', 'max_model_calls', 'max_lawyer_actions',
               'default_actions_per_role_per_stage', 'timeout_seconds', 'codex', 'claude',
               'progress', 'strict_json', 'temperature', 'mode', 'transport_retry', 'retry_policy',
               'environment_model', 'scorer_model', 'max_attempts'}
SECRET_KEYS = re.compile(r'(^|_)(api_key|token|password|secret|authorization|credential|access_key|private_key)(s|$)', re.I)
TOKEN_COUNTS = {'max_output_tokens', 'max_input_tokens', 'max_tokens', 'output_tokens', 'input_tokens', 'total_tokens', 'cached_input_tokens', 'reasoning_tokens', 'thinking_tokens', 'max_completion_tokens'}
ENV_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


class DeliveryError(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(f'{code}: {detail}')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _safe(root, relative, *, require=True):
    root = Path(root).resolve()
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise DeliveryError('unsafe_path', str(relative))
    path = root / rel
    if path.resolve() != path or path.is_symlink():
        raise DeliveryError('symlink_or_path_escape', str(relative))
    if require and not path.is_file():
        raise DeliveryError('missing_file', str(relative))
    return path


def _private_dirs(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write(path, value):
    path = Path(path)
    _private_dirs(path.parent)
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    with path.open('w', encoding='utf-8') as handle:
        handle.write(value)
    os.chmod(path, 0o600)


def _copy(source, dest):
    _private_dirs(Path(dest).parent)
    shutil.copyfile(source, dest)
    os.chmod(dest, 0o600)


def _tree(root, dest, suffixes, *, skip=()):
    if Path(root).is_symlink():
        raise DeliveryError('symlink_input', Path(root).name)
    root = Path(root).resolve()
    if not root.is_dir():
        return
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root)
        if any(part in skip or part.startswith('.') for part in rel.parts):
            continue
        if p.is_symlink():
            raise DeliveryError('symlink_input', str(rel))
        if p.is_file() and p.suffix.lower() in suffixes:
            _copy(_safe(root, rel), Path(dest) / rel)


def sanitize_config(config):
    """Return portable config, rejecting embedded credentials, not redacting blindly."""
    if not isinstance(config, dict):
        raise DeliveryError('invalid_config', 'configuration must be an object')
    def check(value, trail=''):
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise DeliveryError('invalid_config_key', trail)
                safe_token_count = key in TOKEN_COUNTS and type(child) is int and child >= 0
                if SECRET_KEYS.search(key) and not key.endswith('_env') and not safe_token_count:
                    raise DeliveryError('embedded_credentials', 'Use environment-variable names, never credential values')
                check(child, trail + '.' + key)
        elif isinstance(value, list):
            for child in value:
                check(child, trail)
        elif isinstance(value, str):
            if re.search(r'\b(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+\S+)', value):
                raise DeliveryError('embedded_credentials', 'Credential-shaped text is not exportable')
            if value.startswith(('http://', 'https://')):
                parsed = urlsplit(value)
                if parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise DeliveryError('unsafe_endpoint', 'URLs must not contain authentication, queries or fragments')
    check(config)
    result = {k: v for k, v in config.items() if k in SAFE_CONFIG}
    result = json.loads(json.dumps(result))
    for name, executable in (('codex', 'codex'), ('claude', 'claude')):
        if isinstance(result.get(name), dict) and 'executable' in result[name]:
            result[name]['executable'] = executable
    return result


def _dependencies():
    """Installed names/versions only; no pip freeze URLs or host environment values."""
    selected, missing = {}, []
    queue = ['httpx', 'pypdf']
    seen = set()
    while queue:
        name = queue.pop(0)
        normalized = name.lower().replace('_', '-')
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            dist = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)
            continue
        selected[dist.metadata['Name']] = dist.version
        for requirement in dist.requires or []:
            # Ignore optional extras and platform constraints: only installed names
            # are collected, without executing requirement text or exporting URLs.
            if 'extra ==' in requirement or 'extra==' in requirement:
                continue
            match = re.match(r'^([A-Za-z0-9_.-]+)', requirement)
            if match:
                queue.append(match.group(1))
    return {'python': platform.python_version(), 'implementation': platform.python_implementation(),
            'platform': platform.system(), 'machine': platform.machine(),
            'installed_dependencies': dict(sorted(selected.items())), 'missing_dependencies': missing,
            'captured_at': datetime.now(timezone.utc).isoformat(),
            'note': 'Export-host versions; live run preflight metadata is preserved separately when available. No connectivity claim.'}


def _events(path):
    rows = []
    if not Path(path).is_file():
        return rows
    for index, line in enumerate(Path(path).read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise DeliveryError('malformed_event_log', f'line {index}') from None
        if not isinstance(row, dict):
            raise DeliveryError('invalid_event_record', f'line {index}')
        rows.append(row)
    return rows


def _href(relative):
    return '/'.join(quote(part, safe='') for part in Path(relative).parts)


def _replay_html(case_id, candidate, events, artifacts=(), *, audience=None, role_links=(), status='unknown'):
    title = f'{case_id} · {candidate} · ' + ('教师审阅' if audience is None else f'{audience} 可见事件')
    notice = ('本页为教师／作者私有视图，包含庭前准备、私下咨询、工具调用及调查反馈；不能交给参评律师。'
              '模型原始失败输出单独保存在 reviewer_private，不会嵌入角色视图。'
              if audience is None else
              '本文件只包含该角色获准看到的事件，不含其他角色私有记录；它是运行记录的审阅视图，不是新的模型输入。')
    sections = []
    groups = defaultdict(list)
    for e in events:
        if audience is None or audience in e.get('audience', []) or '*' in e.get('audience', []):
            groups[str(e.get('stage_id', 'unknown'))].append(e)
    nav = []
    for idx, (stage, rows) in enumerate(groups.items()):
        nav.append(f'<a href="#stage-{idx}">{escape(stage)} ({len(rows)})</a>')
        cards = []
        for event in rows:
            metadata = json.dumps(event.get('meta', {}), ensure_ascii=False, indent=2)
            references = json.dumps(event.get('references', []), ensure_ascii=False)
            cards.append('<article><div class="line"><b>' + escape(str(event.get('actor', ''))) +
                         '</b><span>' + escape(str(event.get('kind', ''))) + '</span><small>' +
                         escape(str(event.get('event_id', ''))) + '</small></div><p>' +
                         escape(str(event.get('text', ''))) + '</p><details><summary>引用、可见范围与实际工具结果</summary><pre>' +
                         escape(references + '\n' + metadata) + '</pre><small>' +
                         escape(', '.join(str(x) for x in event.get('audience', []))) + '</small></details></article>')
        sections.append(f'<section id="stage-{idx}"><h2>{escape(stage)} <small>{len(rows)} 条</small></h2>' + ''.join(cards) + '</section>')
    artifact_rows = []
    if audience is None:
        for item in artifacts:
            artifact_rows.append('<tr><td><a href="' + escape(_href(item['delivery_path']), quote=True) + '">' +
                                 escape(str(item.get('name', item['path']))) + '</a></td><td>' +
                                 escape(str(item.get('kind', ''))) + '</td><td>' + escape(str(item.get('actor', ''))) +
                                 '</td><td>' + escape(str(item.get('version', ''))) + '</td><td><code>' +
                                 escape(item['sha256'][:16]) + '</code></td></tr>')
    artifact_section = ('<section><h2>实际文件产物</h2><table><thead><tr><th>文件</th><th>类型</th><th>作者</th><th>版本</th><th>SHA-256 前缀</th></tr></thead><tbody>' +
                        ''.join(artifact_rows) + '</tbody></table></section>') if artifact_rows else ''
    roles = ''
    if audience is None and role_links:
        roles = '<p>独立角色视图：' + ' · '.join('<a href="' + escape(_href(path), quote=True) + '">' + escape(role) + '</a>' for role, path in role_links) + '</p>'
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>''' + escape(title) + '''</title><style>
body{margin:0;background:#f2f5f9;color:#17263a;font:16px/1.65 system-ui,sans-serif}main{max-width:1100px;margin:auto;padding:32px 24px}h1{font-size:28px;margin-bottom:6px}h2{font-size:21px;margin-top:32px}small{color:#68788b;font-weight:400}a{color:#155e9c}nav{display:flex;gap:12px;flex-wrap:wrap}.notice{background:#fff1d0;border-left:4px solid #b78c24;padding:14px 18px}article{background:#fff;border:1px solid #dce3eb;padding:18px 22px;margin:12px 0;border-radius:9px}.line{display:flex;gap:16px;flex-wrap:wrap}.line span{color:#246c68}p,pre{white-space:pre-wrap;overflow-wrap:anywhere}pre{font-size:13px;background:#f4f7fa;padding:12px}summary{cursor:pointer;color:#53667d}table{width:100%;border-collapse:collapse;background:#fff}td,th{text-align:left;border-bottom:1px solid #dce3eb;padding:10px;overflow-wrap:anywhere}@media print{body{background:#fff}main{padding:0}article{break-inside:avoid}details{display:block}nav{display:none}a{color:inherit;text-decoration:none}}
</style></head><body><main><h1>''' + escape(title) + '</h1><p>记录状态：' + escape(str(status)) + '</p><p class="notice">' + escape(notice) + '</p>' + roles + '<nav>' + ' · '.join(nav) + '</nav>' + artifact_section + ''.join(sections) + ('<p>尚无已保存事件。此包不代表实验已完成。</p>' if not groups else '') + '</main></body></html>'


def _selected_attempt(run):
    marker = run / 'court_complete.json'
    if marker.exists():
        data = _json(_safe(run, marker.name))
        rel = data.get('attempt', '')
        root = _safe(run, rel + '/summary.json').parent
        hashes = data.get('files_sha256') or data.get('hashes')
        if not isinstance(hashes, dict) or not hashes:
            raise DeliveryError('completion_marker_without_hashes', 'court_complete.json')
        hash_root = run if 'files_sha256' in data else root
        for name, expected in hashes.items():
            if _sha(_safe(hash_root, name)) != expected:
                raise DeliveryError('completed_trace_hash_mismatch', str(name))
        return root, True
    attempts = run / 'court_attempts'
    options = sorted(p for p in attempts.glob('attempt-*') if p.is_dir()) if attempts.is_dir() else []
    if options:
        if options[-1].resolve() != options[-1] or options[-1].is_symlink():
            raise DeliveryError('unsafe_attempt', options[-1].name)
        return options[-1], False
    return None, False


def _export_run(run, dest, case_id, candidate):
    run = Path(run).resolve()
    _private_dirs(dest)
    selected, complete = _selected_attempt(run)
    for name in RUN_FILES:
        path = _safe(run, name, require=False)
        if path.is_file():
            _copy(path, dest / 'reviewer_private' / name)
    # All actual attempts are retained, including failed ones. Config is exported
    # only through the sanitizer below, never copied from attempt/run dumps.
    attempts = run / 'court_attempts'
    if attempts.exists():
        for attempt in sorted(attempts.glob('attempt-*')):
            if attempt.is_symlink() or attempt.resolve() != attempt:
                raise DeliveryError('unsafe_attempt', attempt.name)
            for name in ATTEMPT_FILES:
                path = _safe(attempt, name, require=False)
                if path.is_file():
                    _copy(path, dest / 'reviewer_private/court_attempts' / attempt.name / name)
            _tree(attempt / 'artifacts', dest / 'reviewer_private/court_attempts' / attempt.name / 'artifacts', DATA_SUFFIXES)
            for path in sorted(attempt.glob('view_*.json')):
                _copy(_safe(attempt, path.name), dest / 'reviewer_private/court_attempts' / attempt.name / path.name)
    for name in ('raw_responses', 'private_raw_responses', 'private_failed_outputs', 'private_scoring_outputs', 'raw_model_io', 'full_workflow_score'):
        src = run / name
        if src.is_symlink():
            raise DeliveryError('symlink_run_directory', name)
        _tree(src, dest / 'reviewer_private' / name, {'.json', '.jsonl', '.txt', '.html', '.log'})
    artifact_items = []
    artifact_root = selected / 'artifacts' if selected and (selected / 'artifacts').exists() else run / 'artifacts'
    if artifact_root.is_symlink():
        raise DeliveryError('symlink_artifact_directory', 'artifacts')
    if (artifact_root / 'manifest.json').is_file():
        manifest_path = _safe(artifact_root, 'manifest.json')
        manifest = _json(manifest_path)
        for item in manifest.get('artifacts', []):
            path = _safe(artifact_root, item['path'])
            if _sha(path) != item.get('sha256'):
                raise DeliveryError('artifact_hash_mismatch', str(item.get('artifact_id', '')))
            _copy(path, dest / 'artifacts' / item['path'])
            artifact_items.append({**item, 'delivery_path': 'artifacts/' + item['path']})
        _copy(manifest_path, dest / 'artifacts/manifest.json')
    events = _events(selected / 'author_events.jsonl') if selected else []
    summary = _json(selected / 'summary.json') if selected and (selected / 'summary.json').is_file() else {}
    run_mode = summary.get('mode', 'unknown')
    display_status = ('MOCK 离线演练，非模型实验、无正式成绩 · ' if run_mode == 'mock' else ('LIVE · ' if run_mode == 'live' else '模式未记录 · ')) + str(summary.get('status', 'incomplete'))
    roles = sorted({str(r) for e in events for r in e.get('audience', []) if r != '*'})
    links = []
    for i, role in enumerate(roles):
        path = f'role_views/role-{i:02d}.html'
        _write(dest / path, _replay_html(case_id, candidate, events, audience=role, status=display_status))
        links.append((role, path))
    replay = _replay_html(case_id, candidate, events, artifact_items, role_links=links, status=display_status)
    private_files = sorted(p for p in (dest / 'reviewer_private').rglob('*') if p.is_file()) if (dest / 'reviewer_private').is_dir() else []
    private_links = ''.join('<li><a href="' + escape(_href(p.relative_to(dest)), quote=True) + '">' + escape(str(p.relative_to(dest / 'reviewer_private'))) + '</a></li>' for p in private_files)
    reviewer_section = '<section><h2>教师私有原始记录</h2><p>模型请求、已接受回答、失败最终文本（仅在确实保存时）和各次尝试。仅供审阅，不提供给庭审角色。</p><details><summary>查看 ' + str(len(private_files)) + ' 个保存文件</summary><ul>' + private_links + '</ul></details></section>'
    _write(dest / 'replay.html', replay.replace('</main>', reviewer_section + '</main>'))
    score = run / 'full_workflow_score/score.json'
    score_status = _json(score).get('status', 'present') if score.is_file() else 'not_available'
    return {'candidate': candidate, 'court_complete': complete, 'selected_attempt': selected.name if selected else None,
            'events': len(events), 'artifacts': len(artifact_items), 'score_status': score_status,
            'mode': run_mode, 'mock_only': run_mode == 'mock',
            'replay': f'runs/{candidate}/replay.html', 'raw_failures_exported_only_if_saved': True}


def _runtime(root, dest):
    root = Path(root).resolve()
    for path in root.iterdir():
        if path.is_symlink():
            raise DeliveryError('symlink_runtime', path.name)
        if path.is_file() and (path.suffix in {'.py', '.sh'} or path.name in {'README.md', 'requirements.txt', 'requirements.lock', 'config.example.json'}):
            if path.name == 'config.example.json':
                _write(dest / path.name, sanitize_config(_json(path)))
            else:
                _copy(_safe(root, path.name), dest / path.name)
    _tree(root / 'vendor', dest / 'vendor', {'.py'})


def export_case_runs(source_package, case_id, run_dirs, output_dir, runtime_root=None, config_path=None):
    """Export one case across candidates; return readiness + exact file inventory.

    All two-case immutable input files are included because the current asset
    validator binds both cases together. The index and run logs cover only the
    selected case. Existing output may be refreshed only if created by us.
    """
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', str(case_id)):
        raise DeliveryError('unsafe_case_id', 'case identifier')
    source = Path(source_package).resolve()
    if (source / 'data/assets_manifest.json').is_file():
        source = source / 'data'
    runtime = Path(runtime_root or Path(__file__).parent).resolve()
    output = Path(output_dir).absolute()
    if output.resolve() != output or output.is_symlink():
        raise DeliveryError('unsafe_output', str(output))
    for run in run_dirs.values():
        r = Path(run).resolve()
        if output == r or output in r.parents or r in output.parents:
            raise DeliveryError('output_overlaps_input', str(output))
    if output == source or output in source.parents or source in output.parents:
        raise DeliveryError('output_overlaps_input', str(output))
    if output.exists():
        marker = output / 'delivery_manifest.json'
        if not marker.is_file() or marker.is_symlink() or _json(marker).get('protocol') != PROTOCOL:
            raise DeliveryError('refuse_unowned_output', str(output))
    candidates = list(run_dirs)
    if not candidates or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,49}', c) for c in candidates):
        raise DeliveryError('invalid_candidates', 'provide named run directories')
    missing = []
    for rel in ['assets_manifest.json'] + [f'cases/{case_id}/{name}' for name in CASE_FILES] + [f'workflows/{case_id}/{name}' for name in WORKFLOW_FILES]:
        p = _safe(source, rel, require=False)
        if not p.is_file():
            missing.append('data/' + rel)
    for name in ('runner.py', 'assets.py', 'workflow.py', 'scoring.py', 'requirements.txt'):
        if not (runtime / name).is_file():
            missing.append('runtime/' + name)
    config = sanitize_config(_json(config_path)) if config_path else None
    if config is None:
        example = runtime / 'config.example.json'
        config = sanitize_config(_json(example)) if example.is_file() else {}
        missing.append('runtime configuration supplied by operator')
    _private_dirs(output.parent)
    temp = Path(tempfile.mkdtemp(prefix='.' + output.name + '.export-', dir=output.parent))
    try:
        env_root = temp / 'environment'
        _runtime(runtime, env_root)
        # Exact data manifest inventories only approved data branches; never
        # include runs, credentials, previous outputs or arbitrary source files.
        asset_manifest = _json(source / 'assets_manifest.json') if (source / 'assets_manifest.json').is_file() else None
        if asset_manifest:
            entries = asset_manifest.get('sha256', {})
            for rel, expected in entries.items():
                parts = Path(rel).parts
                if len(parts) < 3 or parts[0] not in {'cases', 'workflows', 'sources', 'rubrics'} or Path(rel).suffix.lower() not in DATA_SUFFIXES:
                    raise DeliveryError('unapproved_asset_path', rel)
                path = _safe(source, rel)
                if _sha(path) != expected:
                    raise DeliveryError('input_asset_hash_mismatch', rel)
                _copy(path, env_root / 'data' / rel)
            _copy(_safe(source, 'assets_manifest.json'), env_root / 'data/assets_manifest.json')
        _write(env_root / 'config.delivery.json', config)
        names = {'ARK_API_KEY', 'PACKY_DEEPSEEK_API_KEY', 'PACKY_OPUS_API_KEY'}
        api_env = config.get('api', {}).get('key_env', 'PACKY_API_KEY')
        if not isinstance(api_env, str) or not ENV_NAME.fullmatch(api_env):
            raise DeliveryError('invalid_key_env_name', 'api.key_env')
        names.add(api_env)
        _write(env_root / '.env.example', '# Names only. Enter credentials locally; this file is not auto-loaded.\n' + '\n'.join(f'{name}=' for name in sorted(names)) + '\n')
        versions = _dependencies()
        observed_cli = []
        runtime_binding_checks = []
        effective_config = None
        matrix_roots = {Path(run).resolve().parents[2] for run in run_dirs.values()
                        if Path(run).resolve().parent.parent.name == 'courts'}
        for n, matrix_root in enumerate(sorted(matrix_roots)):
            private_meta = temp / 'reviewer_private' / f'matrix-{n + 1}'
            preflight_dir = matrix_root / 'preflight'
            if preflight_dir.is_symlink():
                raise DeliveryError('symlink_preflight', 'preflight')
            _tree(preflight_dir, private_meta / 'preflight', {'.json', '.jsonl', '.txt'})
            matrix_path = matrix_root / 'matrix_manifest.json'
            if matrix_path.is_file():
                matrix = _json(_safe(matrix_root, 'matrix_manifest.json'))
                recorded_config = sanitize_config(matrix.get('config', {}))
                if effective_config is not None and effective_config != recorded_config:
                    raise DeliveryError('mixed_run_configurations', 'Candidates do not share one effective configuration')
                effective_config = recorded_config
                code_mismatches = []
                for rel, expected in matrix.get('code_sha256', {}).items():
                    code_file = _safe(runtime, rel, require=False)
                    if not code_file.is_file() or _sha(code_file) != expected:
                        code_mismatches.append(rel)
                data_mismatches = []
                for rel, expected in matrix.get('asset_sha256', {}).items():
                    data_file = _safe(source, rel, require=False)
                    if not data_file.is_file() or _sha(data_file) != expected:
                        data_mismatches.append(rel)
                runtime_binding_checks.append({'matrix': n + 1, 'matches_recorded_runtime': not code_mismatches,
                                               'matches_recorded_data': not data_mismatches,
                                               'changed_or_missing_data': data_mismatches,
                                               'changed_or_missing_files': code_mismatches})
                if data_mismatches:
                    missing.append('data bytes differ from recorded run: ' + ', '.join(data_mismatches))
                if code_mismatches:
                    missing.append('runtime bytes differ from recorded run: ' + ', '.join(code_mismatches))
                public_matrix = {k: v for k, v in matrix.items() if k not in {'config', 'claude_executable'}}
                public_matrix['original_matrix_manifest_sha256'] = _sha(matrix_path)
                public_matrix['config_export'] = 'environment/config.delivery.json; portable executable paths'
                _write(private_meta / 'matrix_binding.json', public_matrix)
            for name in ('runtime_versions.json', 'dependency_versions.json'):
                # Version records must be structured metadata, never environment dumps.
                candidate_path = matrix_root / name
                if candidate_path.is_file():
                    value = _json(_safe(matrix_root, name))
                    sanitize_config(value)  # rejects credentials recursively
                    _write(private_meta / name, value)
        if effective_config is not None:
            config = effective_config
            _write(env_root / 'config.delivery.json', config)
            api_env = config.get('api', {}).get('key_env', 'PACKY_API_KEY')
            if not isinstance(api_env, str) or not ENV_NAME.fullmatch(api_env):
                raise DeliveryError('invalid_key_env_name', 'api.key_env')
            names = {'ARK_API_KEY', 'PACKY_DEEPSEEK_API_KEY', 'PACKY_OPUS_API_KEY', api_env}
            _write(env_root / '.env.example', '# Names only. Enter credentials locally; this file is not auto-loaded.\n' + '\n'.join(f'{name}=' for name in sorted(names)) + '\n')
        # CLI versions recorded by actual calls, including failed calls, are
        # more relevant than probing the export host's unrelated executables.
        def collect_cli(value):
            if isinstance(value, dict):
                if isinstance(value.get('cli_version'), str):
                    item = {k: value[k] for k in ('cli_version', 'model', 'requested_model', 'transport', 'role_id') if k in value}
                    if item not in observed_cli:
                        observed_cli.append(item)
                for child in value.values():
                    collect_cli(child)
            elif isinstance(value, list):
                for child in value:
                    collect_cli(child)
        for run in run_dirs.values():
            for calls in Path(run).glob('court_attempts/attempt-*/model_calls.json'):
                collect_cli(_json(calls))
        for matrix_root in matrix_roots:
            for ledger in (matrix_root / 'preflight').glob('*/action_attempts.jsonl'):
                collect_cli(_events(ledger))
        versions['actual_call_cli_observations'] = observed_cli
        _write(env_root / 'export_host_versions.json', versions)
        _write(env_root / 'export_host_requirements.lock', '# Export-host observed packages; missing packages are listed in export_host_versions.json.\n' + '\n'.join(f'{name}=={version}' for name, version in versions['installed_dependencies'].items()) + '\n')
        results = [_export_run(Path(run), temp / 'runs' / candidate, case_id, candidate) for candidate, run in run_dirs.items()]
        readme = f'''# {case_id} 教师审阅交付\n\n这是私有审阅包。index.html 为总入口，每个候选模型均有 replay.html、按角色单独过滤的视图、实际文件产物和 reviewer_private 原始记录。不得把整包交给参评律师。\n\n## 环境与重跑\n\nenvironment/ 包含执行代码、明确依赖版本、原案/法条来源、新版任务、角色和权限、工具反馈规则及评分标准。正式 v3 标准为 data/workflows/{case_id}/rubric.json；data/cases/{case_id}/evaluation/rubric.json 是原始题包中的旧稿，不用于本次评分。为保持两题共享数据清单的哈希校验完整，本包包含两题共同的数据集；本交付仅展示 {case_id} 的实际运行。原案来源是判决与法条；律师输入是有来源映射的授权摘要，不能称为真实警方报告、原视频或物证原件。\n\n使用 Python 3.10+ 建立新环境，安装 environment/requirements.txt。Codex 和 Claude CLI 需另行安装为本次运行所记录的版本；并在 PATH 中可用。config.delivery.json 已移除机器专用可执行路径；填入本机配置及 .env.example 指出的环境变量，切勿交付密钥。\n\n```bash\ncd environment\npython3 -m venv .venv\n.venv/bin/python -m pip install -r requirements.txt\n.venv/bin/python -B runner.py --project-root "$PWD" --config config.delivery.json --claude-executable claude --case-id {case_id} --out ../rerun --validate-only\n```\n\n实际重跑（隐式续跑新目录；会先检查六条模型路由，随后串行跑本题四模型）：

```bash
.venv/bin/python -B -u runner.py --project-root "$PWD" --config config.delivery.json --claude-executable claude --case-id {case_id} --out ../rerun --prompt-keys
```

完整运行说明见 environment/README.md；预检及正式运行会调用付费模型。导出不调用任何模型，不验证联网状态；仅有保存过的响应才会出现在记录里，不补写缺失对话或失败文本。\n\n本包生成时缺失项：{json.dumps(missing, ensure_ascii=False)}。缺失项非空时不能宣称环境已完整。运行完成性由 delivery_manifest.json 的 runs 明确记录；环境可启动与模型实验成功为不同状态。\n\n## 完整性\n\ndelivery_manifest.json 逐文件保存 SHA-256 和字节数；verify_delivery.py 只读验证且零 API 调用。更新导出包不会修改原始运行记录。作者私有源文件、旧失败尝试及评分证据保存在包内；角色视图不包含别的角色私有事件。\n'''
        _write(temp / 'README.md', readme)
        _write(temp / 'verify_delivery.py', "import hashlib,json,pathlib,sys\np=pathlib.Path(__file__).resolve().parent\nm=json.loads((p/'delivery_manifest.json').read_text())\nerrors=[]\nfor rel,v in m['files'].items():\n f=p/rel\n if f.resolve()!=f or f.is_symlink() or not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest()!=v['sha256']: errors.append(rel)\nprint(json.dumps({'status':'failed' if errors else 'verified','paid_api_calls':0,'errors':errors},ensure_ascii=False))\nsys.exit(bool(errors))\n")
        cards = ''.join('<li><a href="' + escape(_href(item['replay']), quote=True) + '">' + escape(item['candidate']) + '</a> — ' + ('MOCK 离线演练，无模型成绩；' if item['mock_only'] else '') + '庭审' + ('已完成' if item['court_complete'] else '未完成') + f'；事件 {item["events"]}；文件 {item["artifacts"]}</li>' for item in results)
        _write(temp / 'index.html', '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'"><title>' + escape(case_id) + ' 交付</title><style>body{font:17px/1.8 system-ui;max-width:960px;margin:50px auto;padding:20px;color:#203047}a{color:#155e9c}.notice{background:#fff1d0;padding:18px}</style><h1>' + escape(case_id) + ' · 教师审阅交付</h1><p class="notice">私有作者视图：含角色私密信息、原案与评分标准。不得交给参评模型。</p><ul>' + cards + '</ul><p><a href="README.md">环境与重跑说明</a> · <a href="delivery_manifest.json">逐文件完整性清单</a></p><p>律师文件为授权摘要；原始判决与法条单独保留。尚未保存的对话或文件不会被补造。</p></html>')
        inventory = {}
        for path in sorted(temp.rglob('*')):
            if path.is_file():
                inventory[str(path.relative_to(temp))] = {'sha256': _sha(path), 'bytes': path.stat().st_size}
        manifest = {'protocol': PROTOCOL, 'case_id': case_id, 'audience': 'teacher_author_private',
                    'environment_files_complete': not missing, 'live_connectivity_tested_by_export': False,
                    'missing_inputs': missing, 'runs': results, 'files': inventory,
                    'expected_candidates': ['seed', 'deepseek', 'opus', 'gpt'],
                    'missing_candidates': sorted(set(['seed', 'deepseek', 'opus', 'gpt']) - set(candidates)),
                    'generated_at': datetime.now(timezone.utc).isoformat(),
                    'runtime_binding_checks': runtime_binding_checks,
                    'data_scope': 'Two-case immutable shared input bundle; selected-case actual run traces only',
                    'source_limitations': 'Published judgments/law and authorized mapped summaries; no invented original police/video/physical evidence.'}
        _write(temp / 'delivery_manifest.json', manifest)
        backup = None
        if output.exists():
            backup = Path(tempfile.mkdtemp(prefix='.' + output.name + '.old-', dir=output.parent))
            backup.rmdir()
            os.replace(output, backup)
        try:
            os.replace(temp, output)
        except BaseException:
            if backup is not None:
                os.replace(backup, output)
            raise
        if backup is not None:
            shutil.rmtree(backup)
        return manifest
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def export_case(source_package, case_id, run_dir, output_dir, runtime_root=None, config_path=None):
    """Backward-compatible single-candidate export called by the matrix runner."""
    run = Path(run_dir).resolve()
    candidate = run.parent.name if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,49}', run.parent.name) else 'candidate'
    return export_case_runs(source_package, case_id, {candidate: run}, output_dir, runtime_root, config_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-package', type=Path, default=Path(__file__).parent / 'data')
    parser.add_argument('--case-id', required=True)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--runtime-root', type=Path, default=Path(__file__).parent)
    parser.add_argument('--config', type=Path)
    args = parser.parse_args(argv)
    report = export_case(args.source_package, args.case_id, args.run_dir, args.out, args.runtime_root, args.config)
    print(json.dumps({k: report[k] for k in ('case_id', 'environment_files_complete', 'missing_inputs', 'runs')}, ensure_ascii=False, indent=2))
    return 0 if report['environment_files_complete'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
