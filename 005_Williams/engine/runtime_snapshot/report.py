"""Human-readable, complete research outputs derived only from recorded runs."""
from __future__ import annotations
import csv,html,json,re,statistics
from pathlib import Path
from .store import *

FORMAL_RUNS=('gpt_r1','gpt_r2','seed_r1','seed_r2')
RESULT_START='<!-- AUTO_RESULTS_START -->'
RESULT_END='<!-- AUTO_RESULTS_END -->'


def _cell(value):
    if value is None:return '待完成'
    return str(value).replace('|','\\|').replace('\n',' ')


def _csv(path,rows,fields=None):
    fields=fields or (list(rows[0]) if rows else [])
    with Path(path).open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for row in rows:
            safe={k:("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v) for k,v in row.items()}
            w.writerow(safe)


def render_run(case,run):
    case=Path(case);run=Path(run);events=jsonl(run/'author_events.jsonl');summary=read_json(run/'summary.json',{});score=read_json(case/'scores'/run.name/'score_summary.json',{})
    esc=lambda x:html.escape(str(x));rows=[]
    for e in events:
        label='私下/管理记录' if 'R_DEFENSE' in e['audience'] and len(e['audience'])==1 else '法庭记录'
        rows.append(f'<details data-role="{esc(e["actor"])}" data-stage="{esc(e["stage_id"])}"><summary>{esc(e["event_id"])} · 第{e["round"]}轮 · {esc(e["stage_id"])} · {esc(e["actor"])} · {esc(e["kind"])} · {label}</summary><pre>{esc(e["text"])}</pre><p>引用：{esc(e["references"])}</p><pre>{esc(json.dumps(e["meta"],ensure_ascii=False,indent=2))}</pre></details>')
    files=[]
    for p in sorted((run/'artifacts').rglob('*')):
        if not p.is_file():continue
        if p.is_symlink() or not p.resolve().is_relative_to(run.resolve()):raise EvidenceError('回放拒绝项目外工作文件')
        relative=str(p.relative_to(run));data=p.read_bytes()
        try:content=data.decode('utf-8')
        except UnicodeDecodeError:content='二进制文件；请在交付目录打开原文件。'
        files.append(f'<details><summary>{esc(relative)}</summary><p>SHA256：{esc(__import__("hashlib").sha256(data).hexdigest())}；{len(data)} bytes</p><pre>{esc(content)}</pre></details>')
    if not files:files=['<p>本运行没有候选实际创建的工作文件。没有事后代写。</p>']
    data=json.dumps({'summary':summary,'score':score},ensure_ascii=False,indent=2)
    roles=''.join(f'<option>{esc(x)}</option>' for x in sorted({e['actor'] for e in events}))
    stages=''.join(f'<option>{esc(x)}</option>' for x in dict.fromkeys(e['stage_id'] for e in events))
    text=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{esc(run.name)} 庭审回放</title><style>body{{max-width:1100px;margin:32px auto;padding:0 18px;font:16px/1.7 system-ui;background:#f7f8fa;color:#17212c}}details{{background:white;margin:8px 0;padding:12px;border:1px solid #dce1e7;border-radius:6px}}summary{{cursor:pointer;font-weight:600}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.7 ui-monospace,monospace}}h1{{font-size:25px}}input{{width:46%;padding:12px}}select{{padding:12px;max-width:24%}}[hidden]{{display:none!important}}</style></head><body><h1>{esc(case.name)} / {esc(run.name)}</h1><p>管理者完整回放，含私下会见、实际工作文件及全部版本。此页面不提供给候选或陪审团。mock表示离线结构演练，其裁决与分数不能替代真实实验。</p><details open><summary>完成状态与评分</summary><pre>{esc(data)}</pre></details><p><a href="#artifacts">查看实际工作文件及版本</a></p><input id="q" placeholder="搜索事件、引用或正文" oninput="filterEvents()"><select id="role" onchange="filterEvents()"><option value="">全部角色</option>{roles}</select><select id="stage" onchange="filterEvents()"><option value="">全部阶段</option>{stages}</select><main id="events">{''.join(rows)}</main><section id="artifacts"><h2>实际工作文件与版本</h2>{''.join(files)}</section><script>function filterEvents(){{const q=document.getElementById('q').value.toLowerCase(),r=document.getElementById('role').value,s=document.getElementById('stage').value;document.querySelectorAll('#events details').forEach(x=>x.hidden=!(x.textContent.toLowerCase().includes(q)&&(!r||x.dataset.role===r)&&(!s||x.dataset.stage===s)))}}</script></body></html>'''
    (run/'replay.html').write_text(text,encoding='utf-8')


def _legacy(case,replicates):
    source=read_json(case/'legacy_results.json',{})
    if not isinstance(source,dict):raise EvidenceError('legacy_results.json须为object，包含results列表')
    records=source.get('results',[])
    if not isinstance(records,list):raise EvidenceError('legacy_results.results须为列表')
    rows=[]
    for model in ('gpt','seed'):
        matches=[r for r in records if r.get('candidate')==model]
        for old in matches:
            value=old.get('percent')
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not 0<=value<=100 or not old.get('source'):
                raise EvidenceError('旧结果必须有0–100真实percent和可追溯source；不自动猜测分数')
        new=next(r for r in replicates if r['candidate']==model and r['metric']=='dual_grader_mean')
        rows.append({'candidate':model,'old_n':len(matches),'old_mean_percent':statistics.mean(x['percent'] for x in matches) if matches else None,'old_min_percent':min((x['percent'] for x in matches),default=None),'old_max_percent':max((x['percent'] for x in matches),default=None),'old_sources':canonical([x['source'] for x in matches]),'old_status':'provided' if matches else 'not_provided','new_n':new['n'],'new_mean_percent':new['mean'],'new_min_percent':new['min'],'new_max_percent':new['max'],'comparison_note':'新旧世界与rubric不同，描述表现，不将分差解释为同尺度能力变化。'})
    return rows


def summarize(case):
    case=Path(case);rows=[];disagreements=[];severe=[]
    for p in sorted((case/'runs').glob('*/summary.json')):
        s=read_json(p);run=p.parent.name;g=read_json(case/'scores'/run/'score_summary.json',{});u=read_json(p.parent/'usage.json',{})
        dg=g.get('models',{}).get('grader_gpt',{});ds=g.get('models',{}).get('grader_seed',{})
        row={'row_type':'run','run':run,'status':s['status'],'grading_status':g.get('status','not_started'),'mode':'offline_mock' if s.get('mock') else 'live','candidate':s.get('candidate') or s.get('profile'),'rounds':s.get('simulation_round_count',3),'termination_reason':s.get('termination_reason'),'mechanical_percent':g.get('mechanical_percent'),'gpt_grader_percent':dg.get('percent'),'seed_grader_percent':ds.get('percent'),'mean_percent':g.get('mean_percent'),'gpt_penalty':dg.get('penalty'),'seed_penalty':ds.get('penalty'),'agreement':g.get('agreement'),'agreement_scope':g.get('agreement_scope','待双评分'),'raw_denominator':g.get('raw_denominator'),'applicable_denominator':g.get('applicable_denominator'),'quality_pending':g.get('quality_pending'),'verdict':canonical(s.get('verdict')),'tokens':u.get('run_tokens',u.get('total_tokens',0)),'reserved_unknown_tokens':u.get('run_reserved_unknown',0),'unknown_usage_calls':u.get('unknown_usage_calls',0),'disagreements':canonical(g.get('disagreements',[]))}
        rows.append(row)
        for d in g.get('disagreements',[]):disagreements.append({'run':run,**d})
        for route in ('grader_gpt','grader_seed'):
            q=read_json(case/'scores'/run/(route+'.json'),{})
            severe += [{'run':run,'grader':route,**e} for e in q.get('severe_errors',[])]
    replicates=[]
    for candidate in ('gpt','seed'):
        selected=[r for r in rows if r['run'] in (candidate+'_r1',candidate+'_r2') and r['mode']=='live' and r['status']=='completed' and r['grading_status']=='completed']
        for metric,column in [('gpt_grader','gpt_grader_percent'),('seed_grader','seed_grader_percent'),('dual_grader_mean','mean_percent')]:
            vals=[r[column] for r in selected if r[column] is not None]
            replicates.append({'candidate':candidate,'metric':metric,'n':len(vals),'expected_n':2,'mean':round(statistics.mean(vals),2) if vals else None,'min':min(vals) if vals else None,'max':max(vals) if vals else None,'runs':','.join(r['run'] for r in selected),'complete':len(vals)==2})
        group=replicates[-1]
        for row in rows:
            if row['candidate']==candidate:row.update(formal_n=group['n'],formal_mean=group['mean'],formal_min=group['min'],formal_max=group['max'])
    for row in rows:
        for key in ('formal_n','formal_mean','formal_min','formal_max'):row.setdefault(key,None)
    legacy=_legacy(case,replicates)
    ledger=jsonl(case/'usage_ledger.jsonl');budget={'known_tokens':sum(r.get('total_tokens',0) for r in ledger),'reserved_unknown_tokens':sum(r.get('reserved_unknown_tokens',0) for r in ledger),'unknown_usage_calls':sum(bool(r.get('usage_unknown')) for r in ledger),'calls':len(ledger),'limit_tokens':200000000}
    budget['conservative_used_tokens']=budget['known_tokens']+budget['reserved_unknown_tokens']
    out=case/'scores';out.mkdir(exist_ok=True)
    _csv(out/'summary.csv',rows,fields=list(rows[0]) if rows else ['run','status','mode'])
    _csv(out/'replicates.csv',replicates);_csv(out/'disagreements.csv',disagreements,fields=['run','id','gpt','seed','gpt_reason','seed_reason'])
    _csv(out/'severe_errors.csv',severe,fields=['run','grader','category','event_id','reason']);_csv(out/'legacy_comparison.csv',legacy)
    atomic(out/'experiment_summary.json',{'runs':rows,'replicates':replicates,'disagreements':disagreements,'severe_errors':severe,'legacy_comparison':legacy,'budget':budget})
    md=['# 实验结果汇总','', '以下数值全部来自本目录实际记录。离线mock只检验机制，机械得分和占位裁决不能证明真实模型分数或法律结局区分度。两题合计8场正式候选运行，本表只汇总本题的4场。','', '| 运行 | 模式/状态 | GPT评分% | Seed评分% | 双评分均值% | 机械项% | 一致率 | 原始/适用分母 | 严重错误扣分GPT/Seed | token/未知预留 | 结局 |','|---|---|---:|---:|---:|---:|---:|---|---|---|---|']
    for r in rows:
        vals=[r['run'],r['mode']+'/'+r['status']+'/'+r['grading_status'],r['gpt_grader_percent'],r['seed_grader_percent'],r['mean_percent'],r['mechanical_percent'],r['agreement'],f"{r['raw_denominator']}/{r['applicable_denominator']}",f"{r['gpt_penalty']}/{r['seed_penalty']}",f"{r['tokens']}/{r['reserved_unknown_tokens']}",r['verdict']]
        md.append('| '+' | '.join(_cell(v) for v in vals)+' |')
    md+=['','## 正式重复运行均值与范围','', '| 候选 | 评分口径 | 已完成/要求 | 均值% | 最低% | 最高% |','|---|---|---:|---:|---:|---:|']
    for r in replicates:md.append('| '+' | '.join(_cell(v) for v in [r['candidate'],r['metric'],f"{r['n']}/2",r['mean'],r['min'],r['max']])+' |')
    md+=['','一致率分母仅含实际适用的双模型质量项；不是把共享机械项也算入一致率。依法提前终结时，未发生的后续机会按预注册规则N/A。','', '## 双评分分歧','', '| 运行 | 项目 | GPT | Seed | GPT依据 | Seed依据 |','|---|---|---:|---:|---|---|']
    for d in disagreements:md.append('| '+' | '.join(_cell(d[k]) for k in ['run','id','gpt','seed','gpt_reason','seed_reason'])+' |')
    if not disagreements:md+=['','暂无已记录分歧；若双评分尚未完成，这不代表两评分器一致。']
    md+=['','## 严重错误','', '| 运行 | 评分器 | 类别 | 事件 | 理由 |','|---|---|---|---|---|']
    for d in severe:md.append('| '+' | '.join(_cell(d[k]) for k in ['run','grader','category','event_id','reason'])+' |')
    if not severe:md+=['','暂无已记录严重错误；评分未完成时不能据此宣称没有严重错误。']
    md+=['','## 预算','',f"全部调用{budget['calls']}次；已知用量{budget['known_tokens']:,} token；未知用量调用{budget['unknown_usage_calls']}次，保守预留{budget['reserved_unknown_tokens']:,} token；合计保守用量{budget['conservative_used_tokens']:,}/200,000,000。包含预检、校准、正式和评分调用。",'', '## 新旧结果对照','', '| 候选 | 旧结果状态 | 旧次数 | 旧均值% | 新次数 | 新均值% | 旧结果来源 |','|---|---|---:|---:|---:|---:|---|']
    for d in legacy:md.append('| '+' | '.join(_cell(v) for v in [d['candidate'],d['old_status'],d['old_n'],d['old_mean_percent'],d['new_n'],d['new_mean_percent'],d['old_sources']])+' |')
    md+=['','旧工程005为46项/92分、006为48项/96分；本版每题60项/120分。新旧世界和rubric不同，仅描述各自条件下的表现，不把百分比差作同尺度能力变化。未提供旧运行结果时明确缺失，不使用003/004数字代替。可在本题legacy_results.json中提供results列表，每项含candidate（gpt或seed）、percent和source；填写真实可追溯数据后重新report。','', '## 工作文件与回放','', '每场runs/<run>/replay.html包含全部事件及实际artifacts文件正文和版本。文件缺失如实保留，不替候选补写。正式交付前运行export执行完整性、冻结与双评分验收。']
    text='\n'.join(md)+'\n';(out/'summary.md').write_text(text,encoding='utf-8');(case/'RESULTS.md').write_text(text,encoding='utf-8')
    complete=sum(r['run'] in FORMAL_RUNS and r['mode']=='live' and r['status']=='completed' and r['grading_status']=='completed' for r in rows)
    cal=sum(r['run'] in ('calibration_reference_r1','calibration_weak_r1') and r['mode']=='live' and r['grading_status']=='completed' for r in rows)
    status=f"本题当前已记录真实校准双评分 {cal}/2 场，正式候选完成并双评分 {complete}/4 场。已知 API 用量 {budget['known_tokens']:,} token，未知用量保守预留 {budget['reserved_unknown_tokens']:,} token。详细分数、均值范围、一致率、分歧与新旧对照见 [RESULTS.md](RESULTS.md)。冻结记录{'已生成' if (case/'rubric/freeze.json').exists() else '尚未生成'}；正式交付资格由 export 命令重新核验。"
    readme=case/'README.md'
    if readme.exists():
        current=readme.read_text(encoding='utf-8')
        block=RESULT_START+'\n'+status+'\n'+RESULT_END
        if RESULT_START in current:current=re.sub(re.escape(RESULT_START)+r'.*?'+re.escape(RESULT_END),lambda m:block,current,flags=re.S)
        else:
            # Replace only the original generated status paragraph, preserving
            # the case description and all historical/source limitations.
            current=re.sub(r'^已随包提供离线参考/弱路径[^\n]*\n?', '',current,flags=re.M)
            marker='## 当前结果与局限'
            current=current.replace(marker,marker+'\n\n'+block,1) if marker in current else current+'\n\n## 当前结果\n\n'+block+'\n'
        readme.write_text(current,encoding='utf-8')
    return rows
