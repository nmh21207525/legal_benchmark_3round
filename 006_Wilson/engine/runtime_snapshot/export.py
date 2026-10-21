"""Export auditable experiment deliveries without changing frozen source files.

The final gate verifies recorded evidence, not external provider authenticity or
legal correctness. ``allow_partial`` is an explicit, visibly labelled escape
for sharing unfinished work; it can never produce a final-labelled archive.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import zipfile

from .store import EvidenceError, Store, canonical, digest, jsonl, now, read_json

FORMAL_RUNS = ('gpt_r1', 'gpt_r2', 'seed_r1', 'seed_r2')
CALIBRATION_RUNS = ('calibration_reference_r1', 'calibration_weak_r1')
GRADERS = ('grader_gpt', 'grader_seed')
CASE_NAMES = ('005_Williams', '006_Wilson')
STATIC_CASE_DIRS = ('world', 'legal_corpus', 'rubric', 'engine', 'source_reference')
ROOT_DIRS = ('shared_engine', 'tools', 'docs', 'teacher_reference')
ROOT_FILES = ('README.md', 'ENVIRONMENT.json', 'requirements.txt', 'config.json')
SKIP_NAMES = {'__pycache__', '.venv', 'venv', 'node_modules', '.git'}
SKIP_FILES = {'active_pipeline.json', 'background.json', 'STOP_REQUESTED.json', 'stop_requested.json'}


def _require(condition, message):
    if not condition:
        raise EvidenceError(message)


def _same(actual, expected, message):
    _require(actual == expected, message)


def _rows_by_id(rows, label):
    _require(isinstance(rows, list), label + '必须为数组')
    result = {}
    for row in rows:
        _require(isinstance(row, dict) and isinstance(row.get('id'), str), label + '缺少项目编号')
        _require(row['id'] not in result, label + '重复项目编号')
        result[row['id']] = row
    return result


def _verify_run(case, name, world, registration, ledger):
    """Recompute mechanical checks and score arithmetic from original records."""
    from .scoring import applicable, check
    from .freeze import completed_live_evidence

    run = case / 'runs' / name
    for filename in ('author_events.jsonl', 'state.json', 'run_config.json', 'summary.json', 'jury_record.json', 'model_io.jsonl', 'usage.json'):
        _require((run / filename).is_file(), f'{name}缺少{filename}')
    _require(not (run / 'transaction.json').exists(), name + '仍有未完成事务，先恢复再导出')
    _require(not (run / 'call_inflight.json').exists(), name + '仍有未结算的在途API调用')
    state = read_json(run / 'state.json')
    events = jsonl(run / 'author_events.jsonl')
    _require(events, name + '没有实际事件')
    # Do not let Store repair missing version files while exporting.
    for event in events:
        if event.get('kind') == 'artifact_written':
            meta = event['meta']
            filename = meta['filename']
            _require(Path(filename).name == filename, '工作文件路径越界')
            path = run / 'artifacts' / 'versions' / f'{meta["version"]:03d}' / filename
            _require(path.is_file(), name + '缺少不可变工作文件版本')
            _same(digest(path.read_bytes().decode('utf-8')), meta['sha256'], name + '工作文件版本哈希不符')
    for filename, item in state.get('artifacts', {}).items():
        path = run / 'artifacts' / filename
        _require(path.is_file(), name + '缺少工作文件当前版本')
        _same(path.read_bytes().decode('utf-8'), item['content'], name + '工作文件与状态不一致')
    store = Store(run, world.initial())
    store.verify()
    cfg = read_json(run / 'run_config.json')
    summary = read_json(run / 'summary.json')
    _same(cfg.get('mock'), False, name + '不是live运行')
    _same(cfg.get('fingerprint'), digest({k: cfg.get(k) for k in ('config', 'candidate', 'profile', 'mock')}), name + '配置指纹错误')
    _same(cfg.get('config', {}).get('content_sha256'), registration['content_sha256'], name + '运行不是当前冻结内容')
    _same(summary.get('config_fingerprint'), cfg['fingerprint'], name + 'summary与配置不一致')
    _same(summary.get('mock'), False, name + 'summary不是live')
    _same(state.get('status'), 'completed', name + '未完成')
    _same(summary.get('status'), 'completed', name + 'summary未完成')
    _same(summary.get('verdict'), state.get('verdict'), name + 'summary裁决不一致')
    _same(summary.get('event_count'), len(events), name + 'summary事件数不一致')
    expected_models = {'gpt': 'gpt-5.6-sol', 'seed': 'doubao-seed-evolving', 'environment': 'gpt-6.1-sol', 'grader_gpt': 'gpt-6-sol', 'grader_seed': 'doubao-seed-evolving'}
    _same(cfg.get('config', {}).get('models'), expected_models, name + '模型分配偏离教师要求')
    _same(cfg.get('config', {}).get('effort'), {'candidate': 'medium', 'environment': 'medium', 'judge_prosecutor': 'high', 'grader': 'high'}, name + 'effort偏离教师要求')
    expected_candidate = name.split('_')[0] if name in FORMAL_RUNS else None
    expected_profile = name.split('_')[1] if name in CALIBRATION_RUNS else None
    _same(cfg.get('candidate'), expected_candidate, name + '候选模型角色错误')
    _same(cfg.get('profile'), expected_profile, name + '校准角色错误')

    jury = read_json(run / 'jury_record.json')
    _same(jury.get('mock'), False, name + '陪审团记录不是live')
    _same(jury.get('verdict'), state['verdict'], name + '陪审团裁决不一致')
    _same(jury.get('votes'), state.get('jurors', []), name + '陪审团投票不一致')
    charge_ids = {x['charge_id'] for x in world.manifest['charges']}
    _same(set(state['verdict']), charge_ids, name + '裁决缺失或增加罪名')
    judicial = state.get('charge_dispositions', {})
    _require(set(judicial) <= charge_ids, name + '司法处置增加了未知罪名')
    for cid, disposition in judicial.items():
        _require(disposition.get('decision') == 'not_guilty' and disposition.get('source') == 'judgment_of_acquittal', name + '司法无罪处置格式错误')
        _same(state['verdict'][cid], 'not_guilty', name + '司法无罪被后续裁决覆盖')
    remaining = charge_ids - set(judicial)
    if remaining:
        jurors = state.get('jurors', [])
        _same(len(jurors), 6, name + '缺少六名独立陪审员投票')
        _same({j['juror'] for j in jurors}, set(range(1, 7)), name + '陪审员编号重复或缺失')
        for juror in jurors:
            _same(sorted(v['charge_id'] for v in juror['votes']), sorted(remaining), name + '陪审员对已司法无罪/遗漏罪名投票')
            _require(all(v['decision'] in ('guilty', 'not_guilty') for v in juror['votes']), name + '陪审员投票值错误')
        for cid in remaining:
            votes = [v['decision'] for j in jurors for v in j['votes'] if v['charge_id'] == cid]
            decision = votes[0] if len(set(votes)) == 1 else 'hung'
            _same(state['verdict'][cid], decision, name + '裁决未按一致票计算')
    else:
        _same(state.get('termination_reason'), 'judgment_of_acquittal', name + '全案司法无罪缺少终结理由')
        _same(state.get('jurors', []), [], name + '已全案无罪仍存在陪审团投票')

    score_dir = case / 'scores' / name
    rubric = read_json(case / 'rubric/rubric.json')
    criteria = {x['id']: x for x in rubric['criteria']}
    script = _rows_by_id(read_json(score_dir / 'script_checks.json'), name + '机械评分')
    expected_script = {}
    quality_ids = set()
    for item in criteria.values():
        if not applicable(item, events, state):
            expected_script[item['id']] = None
        elif item['method'] == 'script':
            expected_script[item['id']] = check(item, state, events, world)['score']
        else:
            quality_ids.add(item['id'])
    _same(set(script), set(expected_script), name + '机械/N-A项目覆盖不完整')
    for cid, score in expected_script.items():
        row = script[cid]
        _same(row.get('max_points'), criteria[cid]['max_points'], name + '机械项目分母错误')
        _same(row.get('status'), 'N/A' if score is None else 'scored', name + '机械/N-A状态错误')
        _same(row.get('score'), score, name + '机械分数与轨迹重算不一致')
    result = read_json(score_dir / 'score_summary.json', {})
    _same(result.get('status'), 'completed', name + '双评分未完成')
    _same(result.get('mock'), False, name + '评分不是live')
    _same(result.get('quality_pending'), 0, name + '仍有待评分项目')
    raw_denominator = sum(x['max_points'] for x in criteria.values())
    applicable_denominator = sum(x['max_points'] for cid, x in criteria.items() if cid not in script or script[cid]['status'] != 'N/A')
    mechanical_points = sum(x or 0 for x in expected_script.values())
    _same(result.get('raw_denominator'), raw_denominator, name + '原始分母错误')
    _same(result.get('applicable_denominator'), applicable_denominator, name + '适用分母错误')
    _same(result.get('mechanical_points'), mechanical_points, name + '机械总分错误')
    _same(set(result.get('models', {})), set(GRADERS), name + '缺少两位独立评分器')
    event_ids = {x['event_id'] for x in events}
    artifact_paths = {str(p.relative_to(run)) for p in (run / 'artifacts').rglob('*') if p.is_file()}
    grade_rows = {}
    percents = []
    for route in GRADERS:
        grade = read_json(score_dir / (route + '.json'), {})
        _same(grade.get('role'), route, name + '评分器角色错误')
        _same(grade.get('effort'), 'high', name + '评分器effort不是high')
        rows = _rows_by_id(grade.get('items'), name + route)
        _same(set(rows), quality_ids, name + '质量项目覆盖不完整')
        for row in rows.values():
            _require(type(row.get('score')) is int and row['score'] in (0, 1, 2), name + '质量分值错误')
            _require(isinstance(row.get('evidence'), list), name + '质量依据格式错误')
            _require(all(ref in event_ids or ref in artifact_paths for ref in row['evidence']), name + '质量依据不存在')
            _require(row['score'] == 0 or row['evidence'], name + '质量得分缺少依据')
        severe = grade.get('severe_errors', [])
        _require(isinstance(severe, list), name + '严重错误不是数组')
        keys = [(e.get('category'), e.get('event_id')) for e in severe]
        _same(len(keys), len(set(keys)), name + '严重错误重复扣分')
        policy = rubric['penalty_policy']
        _require(all(category in policy['severity_points'] and eid in event_ids for category, eid in keys), name + '严重错误缺少合法类别或实际事件')
        penalty = min(policy['cap'], sum(policy['severity_points'][c] for c, _ in keys))
        points = mechanical_points + sum(r['score'] for r in rows.values())
        expected = {'points_before_penalty': points, 'penalty': penalty, 'points': max(0, points - penalty), 'percent': round(max(0, points - penalty) / applicable_denominator * 100, 2)}
        _same(result['models'][route], expected, name + '双评分汇总算术错误')
        grade_rows[route] = rows
        percents.append(expected['percent'])
    agreement = round(sum(grade_rows[GRADERS[0]][cid]['score'] == grade_rows[GRADERS[1]][cid]['score'] for cid in quality_ids) / len(quality_ids), 4) if quality_ids else None
    _same(result.get('agreement'), agreement, name + '双评分一致率错误')
    _same(result.get('mean_percent'), round(sum(percents) / 2, 2), name + '双评分均值错误')
    expected_disagreements = {cid for cid in quality_ids if grade_rows[GRADERS[0]][cid]['score'] != grade_rows[GRADERS[1]][cid]['score']}
    _same({x['id'] for x in result.get('disagreements', [])}, expected_disagreements, name + '评分分歧漏列')

    run_usage = [x for x in ledger if x.get('run_id') == name]
    _require(run_usage, name + '没有API用量记录')
    for row in run_usage:
        for key in ('total_tokens', 'reserved_unknown_tokens'):
            value = row.get(key, 0)
            _require(type(value) is int and value >= 0, name + '用量必须是非负整数')
    run_tokens = sum(x.get('total_tokens', 0) for x in run_usage)
    reserved = sum(x.get('reserved_unknown_tokens', 0) for x in run_usage)
    _require(run_tokens + reserved <= 30_000_000, name + '单场预算超限')
    usage_file = read_json(run / 'usage.json', {})
    _same(usage_file.get('run_tokens'), run_tokens, name + 'usage.json与ledger用量不一致')
    _same(usage_file.get('run_reserved_unknown', 0), reserved, name + 'usage.json与ledger未知用量预留不一致')
    io = jsonl(run / 'model_io.jsonl')
    _require(any(x.get('accepted') for x in io), name + '没有已接受模型响应记录')
    for route in GRADERS:
        _require(any(x.get('route') == route and x.get('model') == expected_models[route] and x.get('ok') and x.get('effort') == 'high' for x in run_usage), name + '缺少指定模型评分API用量记录')
    if expected_candidate:
        _require(any(x.get('route') == expected_candidate and x.get('model') == expected_models[expected_candidate] and x.get('effort') == 'medium' and x.get('ok') for x in run_usage), name + '缺少指定候选API用量记录')
    _require(any(x.get('route') == 'environment' and x.get('model') == expected_models['environment'] and x.get('ok') for x in run_usage), name + '缺少指定真实环境API用量记录')
    completed_live_evidence(case, name, profile=expected_profile, candidate=expected_candidate)
    return {'run': name, 'status': 'verified', 'events': len(events), 'verdict': state['verdict'], 'score_mean_percent': result['mean_percent'], 'quality_applicable': len(quality_ids)}


def validate_case(case, *, require_final=True):
    from .engine import World
    from .freeze import verify_formal, verify_preregistered

    case = Path(case).resolve()
    _require((case / 'world/manifest.json').is_file(), '找不到题目world/manifest.json')
    if not require_final:
        return {'case': case.name, 'status': 'partial_not_final', 'warning': '未进行最终实验资格验收；可能含离线、未完成或未评分记录。'}
    registration = verify_preregistered(case)
    frozen = verify_formal(case)
    _require(frozen.get('calibration', {}).get('passed') is True, '缺少通过的真实校准记录')
    world = World(case / 'world')
    ledger = jsonl(case / 'usage_ledger.jsonl')
    runs = [_verify_run(case, name, world, registration, ledger) for name in CALIBRATION_RUNS + FORMAL_RUNS]
    gate = read_json(case / 'scores/calibration_gate.json', {})
    _require(gate.get('passed') is True and gate.get('content_sha256') == registration['content_sha256'], '校准闸门与当前冻结不一致')
    ref = read_json(case / 'scores/calibration_reference_r1/score_summary.json')
    weak = read_json(case / 'scores/calibration_weak_r1/score_summary.json')
    _require(all(ref['models'][r]['percent'] >= 85 and weak['models'][r]['percent'] <= 40 for r in GRADERS), '真实校准分数未达到预注册阈值')
    rank = {'guilty': 0, 'hung': 1, 'not_guilty': 2}
    rv, wv = runs[0]['verdict'], runs[1]['verdict']
    differences = [rank[rv[c]] - rank[wv[c]] for c in rv]
    _require(all(x >= 0 for x in differences) and any(x > 0 for x in differences), '真实校准结局未体现区分度')
    _require(sum(x.get('total_tokens', 0) + x.get('reserved_unknown_tokens', 0) for x in ledger) <= 200_000_000, '本题API预算超限')
    return {'case': case.name, 'status': 'verified_final_records', 'content_sha256': registration['content_sha256'], 'runs': runs, 'scope': '验证冻结、原始轨迹、机械重算、双评分结构/算术与预算；不替代人工法律复核或服务商调用真实性证明。'}


def _safe_files(folder, root):
    if not folder.exists():
        return
    for path in sorted(folder.rglob('*')):
        relative = path.relative_to(root)
        if any(part in SKIP_NAMES or part.startswith('.') for part in relative.parts):
            continue
        if path.is_symlink():
            raise EvidenceError('交付路径不得包含符号链接：' + str(relative))
        if path.is_file() and path.suffix not in ('.pyc', '.tmp', '.lock', '.zip') and path.name not in SKIP_FILES:
            _require(path.resolve().is_relative_to(root), '交付路径超出项目目录')
            yield path


def _payloads(root, selected):
    paths = []
    for folder in ROOT_DIRS:
        paths += list(_safe_files(root / folder, root))
    for name in ROOT_FILES:
        if (root / name).is_file():
            paths.append(root / name)
    paths += list(root.glob('*.sh'))
    templates = []
    for case_name in CASE_NAMES:
        case = root / case_name
        if not case.exists():
            continue
        if case_name in selected:
            paths += list(_safe_files(case, root))
        else:
            templates.append(case_name)
            for folder in STATIC_CASE_DIRS:
                paths += [p for p in _safe_files(case / folder, root) if p.name != 'freeze.json']
    payloads = {}
    for path in sorted(set(paths)):
        _require(not path.is_symlink() and path.resolve().is_relative_to(root), '交付路径包含符号链接或越界')
        payloads[str(path.relative_to(root))] = path.read_bytes()
    for case_name in templates:
        payloads[case_name + '/README.md'] = ('# 依赖模板\n\n本目录仅提供共享离线测试所需的静态案件模板，不包含本次所选题的真实实验结果，也未作为本交付的最终结果验收。运行前请阅读根README。\n').encode()
        payloads[case_name + '/进度.md'] = '# 依赖模板：未纳入本次结果交付\n'.encode()
        payloads[case_name + '/usage_ledger.jsonl'] = b''
    return payloads, templates


def _scan_secrets(payloads):
    actual_keys = [os.environ[k].encode() for k in ('PACKY_API_KEY', 'ARK_API_KEY') if os.environ.get(k)]
    pattern = re.compile(rb'(?<![A-Za-z0-9_])(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|ark-[A-Za-z0-9_-]{24,})(?![A-Za-z0-9_])')
    for name, data in payloads.items():
        if any(key in data for key in actual_keys) or pattern.search(data):
            raise EvidenceError('交付文件疑似包含API密钥，停止导出：' + name)


def _export(root, selected, outdir, allow_partial):
    root = Path(root).resolve()
    outdir = Path(outdir).resolve()
    _require(selected and set(selected) <= set(CASE_NAMES), '导出题目不合法')
    # The CLI owns the selected case pipeline lock(s), including when a pipeline
    # exports immediately after completion. Do not acquire them recursively here.
    validations = [validate_case(root / name, require_final=not allow_partial) for name in selected]
    payloads, templates = _payloads(root, selected)
    status = 'partial_not_final' if allow_partial else 'verified_final_records'
    label = 'PARTIAL_未完成验收' if allow_partial else 'FINAL_结果记录已验收'
    delivery = ['# 实验交付', '', '状态：' + label, '', '结果题目：' + ', '.join(selected), '依赖模板（无结果）：' + (', '.join(templates) or '无'), '', '完整保留共享代码与配置。解压后进入legal_agent_3round，先读README，再执行bash setup.sh。', '各题RESULTS.md和scores/summary.md为人读结果，runs包含原始轨迹、工作文件及版本、评分依据和回放。', '未完成材料不得标作正式实验；此验收核对记录与评分一致性，不保证外部服务永不出错或替代人工法律审核。', 'DELIVERY_MANIFEST.json列明每个文件的SHA256。API密钥只从运行环境读取，本包不得包含密钥。', '']
    payloads['DELIVERY.md'] = '\n'.join(delivery).encode()
    _scan_secrets(payloads)
    manifest = {'format': 'teacher-delivery-v2', 'created_at': now(), 'status': status, 'selected_cases': selected, 'template_only_cases': templates, 'validations': validations, 'files': {name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in sorted(payloads.items())}}
    payloads['DELIVERY_MANIFEST.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    outdir.mkdir(parents=True, exist_ok=True)
    base = selected[0] if len(selected) == 1 else '005_006'
    tag = 'partial' if allow_partial else 'final'
    filename = f'{base}_teacher_delivery_{tag}_{now().replace(":", "").replace("-", "")}.zip'
    output = outdir / filename
    if output.exists():
        filename = output.stem + '_' + digest(manifest)[:8] + '.zip'
        output = outdir / filename
    fd, temporary = tempfile.mkstemp(prefix='.delivery_', suffix='.zip', dir=outdir)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, data in sorted(payloads.items()):
                info = zipfile.ZipInfo('legal_agent_3round/' + name)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100755 if name.endswith('.sh') else 0o100644) << 16
                archive.writestr(info, data)
        with zipfile.ZipFile(temporary) as archive:
            _require(archive.testzip() is None, '交付ZIP CRC失败')
            for name, data in payloads.items():
                _same(archive.read('legal_agent_3round/' + name), data, '交付ZIP内容校验失败')
        os.replace(temporary, output)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
    return {'path': str(output), 'bytes': output.stat().st_size, 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'status': status, 'selected_cases': selected, 'template_only_cases': templates, 'files': len(payloads), 'validations': validations}


def export_case(case, outdir, *, allow_partial=False):
    case = Path(case).resolve()
    return _export(case.parent, [case.name], outdir, allow_partial)


def export_project(root, outdir, *, allow_partial=False):
    return _export(Path(root), list(CASE_NAMES), outdir, allow_partial)
