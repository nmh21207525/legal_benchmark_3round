"""Content-addressed preregistration and evidence-backed live calibration gates."""
from pathlib import Path
import hashlib, math
from .store import *


def content_manifest(case):
    case=Path(case);root=case.parent;files=[]
    for folder in ('world','legal_corpus','rubric','engine'):
        for p in (case/folder).rglob('*'):
            if not p.is_file() or '__pycache__' in p.parts or p.suffix=='.pyc':continue
            if folder=='rubric' and p.name not in ('rubric.json','checks.py','预注册说明.md'):continue
            files.append(p)
    files += list((root/'shared_engine').glob('*.py'))+list((root/'tools').glob('*.py'))+list(root.glob('*.sh'))+[root/'config.json']
    files += [p for p in (root/'requirements.txt',root/'ENVIRONMENT.json') if p.is_file()]
    entries={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(files))}
    return {'files':entries,'content_sha256':digest(entries)}


def preregister(case):
    case=Path(case);current=content_manifest(case);p=case/'rubric/preregistration.json';old=read_json(p)
    formal=list((case/'runs').glob('gpt_r*'))+list((case/'runs').glob('seed_r*'))
    if old and old['content_sha256']!=current['content_sha256'] and formal:
        raise EvidenceError('已有正式运行，不可覆盖预注册；须新版本和全部重评')
    if old and old['content_sha256']==current['content_sha256']:return old
    current.update({'registered_at':now(),'status':'before_live_calibration','note':'仅哈希预注册；真实校准通过前不能正式运行。'})
    atomic(p,current);return current


def verify_preregistered(case):
    old=read_json(Path(case)/'rubric/preregistration.json');current=content_manifest(case)
    if not old or old['content_sha256']!=current['content_sha256']:
        raise EvidenceError('内容与预注册哈希不同；停止，需明确修复、重校准及重新冻结')
    return old


def completed_live_evidence(case,name,*,profile=None,candidate=None):
    """Validate the actual persisted run, not a caller-supplied score dictionary."""
    from .engine import World
    case=Path(case);name=safe_name(name);run=case/'runs'/name;score_dir=case/'scores'/name
    reg=verify_preregistered(case);rc=read_json(run/'run_config.json',{});summary=read_json(run/'summary.json',{})
    if rc.get('mock') is not False or summary.get('mock') is not False:
        raise EvidenceError('校准和正式验收必须使用真实API运行，mock不能替代')
    if profile is not None and (rc.get('profile')!=profile or rc.get('candidate') is not None):
        raise EvidenceError('校准路径身份不符')
    if candidate is not None and (rc.get('candidate')!=candidate or rc.get('profile') is not None):
        raise EvidenceError('正式候选身份不符')
    cfg=rc.get('config',{})
    if cfg.get('content_sha256')!=reg['content_sha256']:
        raise EvidenceError('运行属于不同预注册内容，不能沿用评分')
    fingerprint=digest({k:rc.get(k) for k in ('config','candidate','profile','mock')})
    if rc.get('fingerprint')!=fingerprint or summary.get('config_fingerprint')!=fingerprint:
        raise EvidenceError('运行配置指纹不一致')
    world=World(case/'world');store=Store(run,world.initial());s=store.state
    if s.get('status')!='completed' or summary.get('status')!='completed' or s.get('verdict')!=summary.get('verdict'):
        raise EvidenceError('运行尚未完整完成或summary与状态不一致')
    charges={c['charge_id'] for c in world.manifest['charges']}
    if set(s.get('verdict') or {})!=charges or any(x not in ('guilty','not_guilty','hung') for x in s['verdict'].values()):
        raise EvidenceError('裁决缺少指控或含非法值')
    grade=read_json(score_dir/'score_summary.json',{})
    if grade.get('status')!='completed' or grade.get('mock') is not False or grade.get('quality_pending')!=0:
        raise EvidenceError('双评分尚未完成')
    if grade.get('run_id')!=name or grade.get('case_id')!=world.manifest['case_id']:
        raise EvidenceError('评分归属不一致')
    if set(grade.get('models',{}))!={'grader_gpt','grader_seed'}:
        raise EvidenceError('必须有两位指定评分器的完整结果')
    for route,g in grade['models'].items():
        value=g.get('percent')
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=100:
            raise EvidenceError('评分百分比非法')
        q=read_json(score_dir/(route+'.json'),{})
        if q.get('role')!=route or q.get('effort')!='high':raise EvidenceError('评分器身份/effort不符')
    files=[run/x for x in ('author_events.jsonl','state.json','summary.json','run_config.json','jury_record.json')]
    files += [score_dir/x for x in ('script_checks.json','grader_gpt.json','grader_seed.json','score_summary.json')]
    if any(not p.is_file() for p in files):raise EvidenceError('验收证据文件不完整')
    io=jsonl(run/'model_io.jsonl');usage=[r for r in jsonl(case/'usage_ledger.jsonl') if r.get('run_id')==name]
    if not io or not usage:raise EvidenceError('缺少真实API原始记录或用量记录')
    if (run/'call_inflight.json').exists():raise EvidenceError('仍有未结算在途调用，不得标为最终完成')
    hashes={str(p.relative_to(case)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    return {'state':s,'score':grade,'files':hashes,'sha256':digest(hashes)}


def calibrate_gate(case,reference,weak):
    case=Path(case);p=verify_preregistered(case)
    if any(x.get('mock') is not False or x.get('status')!='completed' for x in (reference,weak)):
        raise EvidenceError('真实校准尚未完成；离线结果不能开启正式运行')
    re=completed_live_evidence(case,reference['run_id'],profile='reference')
    we=completed_live_evidence(case,weak['run_id'],profile='weak')
    if reference!=re['score'] or weak!=we['score']:raise EvidenceError('传入评分与已保存双评分不一致')
    r=re['state'];w=we['state'];rank={'guilty':0,'hung':1,'not_guilty':2}
    diffs=[rank[r['verdict'][c]]-rank[w['verdict'][c]] for c in r['verdict']]
    success=all(v['percent']>=85 for v in reference['models'].values()) and all(v['percent']<=40 for v in weak['models'].values()) and all(d>=0 for d in diffs) and any(d>0 for d in diffs)
    evidence={**re['files'],**we['files']}
    result={'passed':success,'checked_at':now(),'content_sha256':p['content_sha256'],'reference_scores':reference['models'],'weak_scores':weak['models'],'reference_run':reference['run_id'],'weak_run':weak['run_id'],'reference_verdict':r['verdict'],'weak_verdict':w['verdict'],'evidence_sha256':digest(evidence),'evidence_files':evidence,'note':'两个评分器都满足强>=85、弱<=40，且结局严格区分才通过；这是预注册的保守验收阈值。'}
    old=read_json(case/'rubric/freeze.json',{})
    if old and old.get('content_sha256')==p['content_sha256'] and old.get('calibration',{}).get('evidence_sha256')==result['evidence_sha256'] and success:
        return old['calibration']
    if old and (list((case/'runs').glob('gpt_r*')) or list((case/'runs').glob('seed_r*'))):
        raise EvidenceError('正式运行后校准证据改变；不能覆写既有冻结')
    atomic(case/'scores/calibration_gate.json',result)
    if not success:raise EvidenceError('真实校准未达阈值，正式运行被阻止；保留全部证据，需冻结前修订和重新校准')
    atomic(case/'rubric/freeze.json',{**p,'status':'live_calibration_passed','frozen_at':now(),'calibration':result});return result


def verify_formal(case):
    case=Path(case);p=verify_preregistered(case);f=read_json(case/'rubric/freeze.json')
    if not f or f.get('status')!='live_calibration_passed' or f['content_sha256']!=p['content_sha256']:
        raise EvidenceError('未通过真实环境双评分校准并冻结，不得正式运行候选')
    c=f.get('calibration',{});files=c.get('evidence_files',{})
    if not c.get('passed') or not files or digest(files)!=c.get('evidence_sha256'):
        raise EvidenceError('冻结缺少可核验的真实校准证据')
    for name,expected in files.items():
        target=(case/name).resolve()
        if not target.is_relative_to(case.resolve()) or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest()!=expected:
            raise EvidenceError('冻结后的校准证据已改变：'+name)
    return f
