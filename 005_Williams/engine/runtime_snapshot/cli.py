"""Serial pipeline: preflight, live calibration, frozen 2+2 trials, grade and export."""
from __future__ import annotations
import argparse,contextlib,copy,json,os,subprocess,sys,time
from pathlib import Path
from .store import *
from .engine import Engine,World,TEXT_SCHEMA
from .llm import Client,Ledger
from .freeze import *
from .scoring import grade
from .report import render_run,summarize

ROOT=Path(__file__).resolve().parents[1]

def case_path(value):
    if value in ('005','006'):return ROOT/('005_Williams' if value=='005' else '006_Wilson')
    p=Path(value).resolve()
    if not (p/'world/manifest.json').exists():raise EvidenceError('找不到题目目录')
    return p

def config(case):
    c=read_json(case.parent/'config.json')
    expected={'gpt':'gpt-5.6-sol','seed':'doubao-seed-evolving','environment':'gpt-6.1-sol','grader_gpt':'gpt-6-sol','grader_seed':'doubao-seed-evolving'}
    if c['models']!=expected or c['formal_replicates_per_candidate_per_case']!=2 or c['effort']!={'candidate':'medium','environment':'medium','judge_prosecutor':'high','grader':'high'} or c['limits']!={'run_tokens':30000000,'case_tokens':200000000,'context_tokens':150000}:
        raise EvidenceError('模型、effort、预算或重复次数偏离教师要求，拒绝启动')
    return c

def progress(case,message,status='working'):
    usage=jsonl(case/'usage_ledger.jsonl');known=sum(x.get('total_tokens',0) for x in usage);unknown=sum(bool(x.get('usage_unknown')) for x in usage);reserved=sum(x.get('reserved_unknown_tokens',0) for x in usage)
    text=f'# 当前进度\n\n更新时间：{now()}\n\n状态：{status}\n\n{message}\n\n已记录token：{known:,}；用量未知调用：{unknown}，保守预留{reserved:,} token。总额度200,000,000。未知用量不当作免费调用。\n\n真实结果见scores与RESULTS.md；离线记录mock=true。密钥只从进程环境读取。\n'
    atomic_text(case/'进度.md',text)

def ensure_not_stopped(case):
    if (case/'STOP_REQUESTED.json').exists():
        raise StopRequested('已安全停止；使用resume命令清除停止请求并继续，已完成步骤不会重跑')

def run_one(case,name,profile=None,candidate=None,mock=False):
    ensure_not_stopped(case);registration=verify_preregistered(case)
    if candidate:verify_formal(case)
    run=case/'runs'/safe_name(name);cfg=config(case);cfg['content_sha256']=registration['content_sha256']
    with lock(run/'.run.lock'):
        prior=read_json(run/'summary.json',{});score=read_json(case/'scores'/name/'score_summary.json',{})
        if not mock and prior.get('status')=='completed' and score.get('status')=='completed':
            completed_live_evidence(case,name,profile=profile,candidate=candidate)
            render_run(case,run);summarize(case);return prior,score
        engine=Engine(case,run,profile=profile,candidate=candidate,mock=mock,config=cfg)
        progress(case,f'运行{name}，模式'+('离线mock' if mock else '真实API')+'。异常保留checkpoint，不生成替代判决。')
        try:
            result=engine.run_all();ensure_not_stopped(case)
            scores=grade(case,run,live=not mock,config=cfg)
            render_run(case,run);summarize(case);return result,scores
        except BaseException as ex:
            engine.s=copy.deepcopy(engine.store.state)
            if engine.s['status']!='completed':
                engine.s['status']='paused';engine.commit([engine.ev('run_paused',redact(ex),audience=['R_ADMIN'])])
            engine.finish_outputs();render_run(case,run);summarize(case)
            progress(case,f'{name}暂停：{redact(ex)}。安全停止后用resume；网络故障修复后重复原命令。四次传输尝试耗尽用retry-batch记录恢复原因。','paused')
            raise

def offline(case):
    ensure_not_stopped(case);preregister(case)
    for p in ('reference','weak'):run_one(case,'calibration_'+p+'_offline',profile=p,mock=True)
    progress(case,'离线参考/弱路径已跑通。仅机械项有分；质量双评分、真实结局区分和正式模型结果未执行。下一步真实预检→真实校准→冻结→两模型各两次。','offline_verified_live_pending')
    for name in ('calibration_reference_offline','calibration_weak_offline'):
        disk=Store(case/'runs'/name,World(case/'world').initial())
        if disk.state['status']!='completed':raise EvidenceError('离线完成状态不一致，停止交付')

def preflight(case):
    ensure_not_stopped(case)
    for key in ('PACKY_API_KEY','ARK_API_KEY'):
        if not os.environ.get(key):raise EvidenceError(key+'未设置。按README隐式输入；不要把密钥发送到聊天或写入项目。')
    p=verify_preregistered(case);run=case/'runs/api_preflight';run.mkdir(exist_ok=True);cfg=config(case)
    for route in ('gpt','seed','environment','grader_gpt','grader_seed'):
        ensure_not_stopped(case)
        out=Client(case,run,route,cfg).call('接口身份、结构化输出和用量预检。只返回JSON {"text":"ok"}。',{'test':'teacher-aligned-preflight','content_sha256':p['content_sha256']},TEXT_SCHEMA,'R_PREFLIGHT')
        if out['text'].lower().strip()!='ok':raise EvidenceError('接口预检未通过')
    rows=jsonl(case/'usage_ledger.jsonl')
    for route in ('gpt','seed','environment','grader_gpt','grader_seed'):
        last=next((r for r in reversed(rows) if r['run_id']=='api_preflight' and r['route']==route and r['ok']),None)
        if not last or last.get('usage_complete') is not True or last.get('usage_unknown'):
            raise EvidenceError('成功回复未提供完整有效usage，暂停预检；请修复网关用量字段后重试，费用按未知用量保守预留')
    atomic(run/'preflight.json',{'passed':True,'time':now(),'content_sha256':p['content_sha256'],'models':cfg['models'],'effort':cfg['effort']})

def pipeline(case,only=None):
    with lock(case/'.pipeline.lock'):
        ensure_not_stopped(case);atomic(case/'active_pipeline.json',{'pid':os.getpid(),'started_at':now(),'state':'running'})
        try:
            verify_preregistered(case);preflight(case)
            _,reference=run_one(case,'calibration_reference_r1',profile='reference')
            _,weak=run_one(case,'calibration_weak_r1',profile='weak')
            calibrate_gate(case,reference,weak)
            for model in ([only] if only else ['gpt','seed']):
                for n in (1,2):run_one(case,f'{model}_r{n}',candidate=model)
            completed=[name for name in ('gpt_r1','gpt_r2','seed_r1','seed_r2') if read_json(case/'scores'/name/'score_summary.json',{}).get('status')=='completed']
            summarize(case)
            if len(completed)==4:
                from .export import export_case
                progress(case,'真实校准、正式4场及双评分完成；本题结果记录已齐备，交付清单及验收状态见DELIVERY_MANIFEST.json与RESULTS.md。','completed_ready_for_export')
                deliverable=export_case(case,case.parent/'deliverables')
                progress(case,'真实校准、正式4场及双评分完成。正式交付包已生成，详情见deliverables及本题RESULTS.md。','completed_exported')
                print(json.dumps(deliverable,ensure_ascii=False,indent=2))
            else:progress(case,f'真实校准及双评分完成；正式候选完成{len(completed)}/4场。','partial_formal_completed')
        except BaseException as ex:
            progress(case,'流水线暂停：'+redact(ex)+'。已完成步骤和原始日志保留；未完成或校准失败不会伪造成正式成功。','paused')
            raise
        finally:
            atomic(case/'active_pipeline.json',{'pid':os.getpid(),'ended_at':now(),'state':'inactive'})

def status(case):
    summaries=[]
    for p in sorted((case/'runs').glob('*/summary.json')):
        s=read_json(p);scores=read_json(case/'scores'/p.parent.name/'score_summary.json',{})
        summaries.append({'run':p.parent.name,'status':s['status'],'stage':s['stage'],'mock':s['mock'],'dual_grade':scores.get('status')})
    active=read_json(case/'active_pipeline.json',{});is_active=False
    if active.get('state')=='running':
        try:os.kill(active['pid'],0);is_active=True
        except (ProcessLookupError,PermissionError,KeyError):pass
    ledger=Ledger(case,case/'runs/api_preflight',config(case)['limits']).totals()
    return {'case_id':read_json(case/'world/manifest.json')['case_id'],'preregistered':bool(read_json(case/'rubric/preregistration.json')),'live_freeze':bool(read_json(case/'rubric/freeze.json')),'active':is_active,'stop_requested':(case/'STOP_REQUESTED.json').exists(),'runs':summaries,'usage':ledger,'results':str(case/'RESULTS.md')}

def background(case,only=None):
    ensure_not_stopped(case);log=case/'pipeline.log';cmd=[sys.executable,'-m','shared_engine.cli','pipeline','--case',str(case)]
    if only:cmd+=['--only',only]
    with lock(case/'.launch.lock'):
        with lock(case/'.pipeline.lock'):pass
        with log.open('a') as f:p=subprocess.Popen(cmd,cwd=case.parent,stdin=subprocess.DEVNULL,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
        atomic(case/'background.json',{'pid':p.pid,'started_at':now(),'log':str(log)})
        for _ in range(30):
            a=read_json(case/'active_pipeline.json',{})
            if a.get('pid')==p.pid and a.get('state')=='running':break
            if p.poll() is not None:raise RunError('后台进程启动失败，查看本题pipeline.log')
            time.sleep(.05)
    print(f'后台PID={p.pid}；日志：{log}。用status检查状态；stop安全停止，resume继续。')

def request_stop(case):
    atomic(case/'STOP_REQUESTED.json',{'requested_at':now(),'reason':'用户命令安全停止'})
    print('停止请求已记录。当前API调用先完成并保存，下一安全点暂停；status查看状态。')

def resume(case,only=None):
    with lock(case/'.pipeline.lock'):
        marker=case/'STOP_REQUESTED.json'
        if marker.exists():
            append(case/'recovery_audit.jsonl',{'time':now(),'action':'resume','stop_request':read_json(marker)})
            marker.unlink();fsync_directory(case)
    background(case,only)

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command',choices=['offline','pipeline','preflight','status','background','stop','resume','retry-batch','grade','report','preregister','export','export-all','validate'])
    ap.add_argument('--case',required=True);ap.add_argument('--only',choices=['gpt','seed']);ap.add_argument('--run');ap.add_argument('--reason');ap.add_argument('--out',type=Path);ap.add_argument('--allow-partial',action='store_true')
    a=ap.parse_args(argv);case=case_path(a.case)
    if a.command=='status':print(json.dumps(status(case),ensure_ascii=False,indent=2));return
    if a.command=='stop':request_stop(case);return
    if a.command=='resume':resume(case,a.only);return
    if a.command=='background':background(case,a.only);return
    if a.command=='preregister':
        with lock(case/'.pipeline.lock'):print(preregister(case)['content_sha256'])
        return
    if a.command=='retry-batch':
        if not a.run or not a.reason:raise EvidenceError('retry-batch必须给--run和--reason，保留旧重试记录')
        run=case/'runs'/safe_name(a.run)
        with lock(case/'.pipeline.lock'),lock(run/'.run.lock'):
            p=run/'call_attempts.jsonl';archive=run/'retry_batches';archive.mkdir(exist_ok=True)
            count=len(list(archive.glob('*.jsonl')))+1
            if p.exists():p.replace(archive/f'{count:03d}.jsonl')
            append(run/'recovery_audit.jsonl',{'batch':count,'time':now(),'reason':a.reason})
        print('已保留旧尝试记录并建立人工恢复批次；重新执行原命令。有停止标记时用resume。');return
    if a.command in ('export','export-all','validate'):
        from .export import export_case,export_project,validate_case
        with contextlib.ExitStack() as stack:
            targets=sorted(ROOT.glob('00*_*')) if a.command=='export-all' else [case]
            for c in targets:stack.enter_context(lock(c/'.pipeline.lock'))
            if a.command=='validate':out=validate_case(case,require_final=not a.allow_partial)
            elif a.command=='export':out=export_case(case,a.out or ROOT/'deliverables',allow_partial=a.allow_partial)
            else:out=export_project(ROOT,a.out or ROOT/'deliverables',allow_partial=a.allow_partial)
            print(json.dumps(out,ensure_ascii=False,indent=2))
        return
    if a.command=='pipeline':pipeline(case,a.only)
    else:
        with lock(case/'.pipeline.lock'):
            if a.command=='offline':offline(case)
            elif a.command=='preflight':preflight(case)
            elif a.command=='grade':
                if not a.run:raise EvidenceError('需要--run')
                name=safe_name(a.run);run=case/'runs'/name
                if name.startswith(('gpt_r','seed_r')):verify_formal(case)
                registration=verify_preregistered(case);cfg=config(case);cfg['content_sha256']=registration['content_sha256']
                with lock(run/'.run.lock'):out=grade(case,run,live=True,config=cfg)
                render_run(case,run);summarize(case);print(json.dumps(out,ensure_ascii=False,indent=2))
            elif a.command=='report':
                for p in (case/'runs').glob('*/author_events.jsonl'):render_run(case,p.parent)
                summarize(case)

if __name__=='__main__':
    try:main()
    except (RunError,KeyboardInterrupt) as ex:
        print('暂停：'+redact(ex or '用户中断；已保存步骤可恢复'),file=sys.stderr);sys.exit(2)
