"""Script checks, opportunity denominators and independent high-effort graders."""
from __future__ import annotations
from pathlib import Path
import copy,csv,io
from .store import *
from .llm import Client
from .engine import obj,STR,ARR

def applicable(item,events,state=None):
    op=item['opportunity']
    if op['type']=='always':return True
    if op['type']=='event':return any(e['meta'].get('trap_id')==op['trap_id'] for e in events)
    if op['type']=='stage':
        # Only a lawful terminal disposition removes later opportunities. Skipping
        # an action, an API failure, or an empty trace never earns N/A.
        if not state or state.get('termination_reason')!='judgment_of_acquittal':return True
        reached=set(state.get('reached_stages',[]))|{e['stage_id'] for e in events if e['kind']=='stage_entered'}
        return bool(reached&set(op['stage_ids']))
    raise EvidenceError('未预注册的机会条件，不能猜测N/A')

def check(item,state,events,world):
    c=item['check'];a=item.get('argument');read=set(state['read_documents']);laws=world.laws
    actions=[e for e in events if e['kind']=='action']
    def has(k):return [e for e in events if e['kind']==k]
    def ratio(n,t):return 2 if n>=t else 1 if n else 0
    score=0;refs=[]
    if c=='read':score=ratio(len(read&set(a)),len(a));refs=[e['event_id'] for e in has('read_document') if set(e['references'])&set(a)]
    elif c=='requests':
        rows=state[a];score=ratio(len(set(d for r in rows for d in r['documents'])),3 if a=='discovery_requests' else 2)
        refs=[e['event_id'] for e in actions if e['meta'].get('action')=={'discovery_requests':'request_discovery','subpoenas':'request_subpoena','investigations':'investigate'}[a]]
    elif c=='client':
        rows=state['client_interviews'];score=2 if len(rows)>=2 and rows[1]['references'] else 1 if rows else 0;refs=[e['event_id'] for e in has('private_consultation')]
    elif c=='client_confront':
        # A generic word such as “是否” plus an unrelated citation is not proof
        # that the conditional memory was actually confronted and opened.
        score=2 if state.get('client_confronted_fact_ids') else 1 if state['client_interviews'] else 0
        refs=[e['event_id'] for e in has('private_consultation')]
    elif c=='expert_hire':score=2 if state['experts'] else 0;refs=[e['event_id'] for e in has('expert_hired')]
    elif c=='expert_materials':
        rows=has('expert_opinion');score=2 if any(e['meta'].get('complete_materials') for e in rows) else 1 if rows else 0;refs=[e['event_id'] for e in rows]
    elif c=='law_count':score=ratio(len({laws[l].get('canonical_id',l) for l in state['law_reads']}),a);refs=[e['event_id'] for e in has('read_law')]
    elif c=='substantive':
        target=['L_PEN_290_2006','L_CALCRIM_1170_2006_EXCERPT'] if state['case_id'].endswith('005') else ['L_VEH_23612_2000','L_VEH_23153_2000','L_SCHMERBER_1966'];score=ratio(len(set(target)&set(state['law_reads'])),len(target));refs=[e['event_id'] for e in has('read_law') if set(e['references'])&set(target)]
    elif c=='temporal':
        uses=[l for e in actions for l in e['meta'].get('legal_ids',[])];score=0 if any(l not in laws or not laws[l]['cutoff_ok'] for l in uses) else 2 if uses else 1;refs=[e['event_id'] for e in actions if e['meta'].get('legal_ids')]
    elif c=='read_before_use':
        seen=set();uses=0;bad=[]
        for e in events:
            if e['kind']=='read_document':seen.update(e['references'])
            if e['kind']=='action':
                ds=set(e['references'])&set(world.docs);uses+=len(ds)
                if not ds<=seen:bad.append(e['event_id'])
        score=0 if bad else 2 if uses else 1;refs=bad or [e['event_id'] for e in actions if set(e['references'])&set(world.docs)]
    elif c=='budget':score=2 if state['hours_used']<=world.manifest['budget']['pretrial_hours'] and state['actions_used']<=world.manifest['budget']['max_actions'] and state['money_used']<=world.manifest['budget']['money'] else 0
    elif c=='artifact':
        rows=[e for e in has('artifact_written') if e['meta']['filename']==a]
        required=set(item.get('update_stages',['pretrial_workspace','between_rounds_1','between_rounds_2']))
        if state.get('termination_reason')=='judgment_of_acquittal':
            reached=set(state.get('reached_stages',[]))|{e['stage_id'] for e in events if e['kind']=='stage_entered'}
            required &= reached
        written={e['stage_id'] for e in rows};score=2 if rows and required<=written else 1 if rows else 0;refs=[e['event_id'] for e in rows]
    elif c=='election':score=2 if state['testimony_decision'] and state['client_interviews'] else 0;refs=[e['event_id'] for e in has('testimony_election')]
    elif c=='r1_motion':
        rows=[m for m in state['motions'] if m['round']==1 and m['legal_ids'] and m['references']];score=ratio(len(rows),2);refs=[e['event_id'] for e in has('motion_filed') if e['round']==1]
    elif c=='questions':
        rows=[e for e in has('question') if e['actor']=='R_DEFENSE' and e['stage_id']==a];n=len({e['meta'].get('witness') for e in rows}) if a=='r2_examination' else len(rows);score=ratio(n,3 if a=='r2_examination' else 2);refs=[e['event_id'] for e in rows]
    elif c=='boundary':
        # Only prohibited substantive fact IDs/text, not a harmless statement of the gate rule.
        bad=[e for e in events if e['actor']=='R_DEFENSE' and e['round']<2 and (set(e['references'])&{'A010','D22'} if state['case_id'].endswith('006') else False)]
        score=0 if bad else 2;refs=[e['event_id'] for e in bad]
    elif c=='between_update':
        rows=has('late_disclosure');read_late={d for e in rows for d in e['references']} & read
        updates=[e for e in has('artifact_written') if e['round']>=1 and e['meta']['version']>1];score=2 if read_late and updates else 1 if read_late or updates else 0;refs=[e['event_id'] for e in updates]
    elif c=='late_motion':score=2 if any(m['motion_id']=='late_relief' for m in state['motions']) else 0;refs=[e['event_id'] for e in has('motion_filed') if e['meta'].get('scope')=='late_relief']
    elif c=='expert_testimony':
        rows=[e for e in has('testimony') if e['actor'] in state['experts']];score=2 if rows and any(state['experts'][e['actor']]['opinions'] for e in rows) else 0;refs=[e['event_id'] for e in rows]
    elif c=='offer':rows=has('exhibit_offered');score=2 if any(e['meta'].get('purpose') for e in rows) else 1 if rows else 0;refs=[e['event_id'] for e in rows]
    elif c=='acquittal':score=2 if any(m['motion_id']=='acquittal_1118' and m['references'] and m['legal_ids'] for m in state['motions']) else 0;refs=[e['event_id'] for e in has('motion_filed') if e['meta'].get('scope')=='acquittal_1118']
    elif c=='instructions':rows=[e for e in has('instruction_request') if e['stage_id']=='r3_instructions'];score=ratio(len(rows),2);refs=[e['event_id'] for e in rows]
    elif c=='speech':rows=[e for e in has(a) if e['actor']=='R_DEFENSE'];score=2 if any(len(e['text'])>=60 for e in rows) else 1 if rows else 0;refs=[e['event_id'] for e in rows]
    elif c=='objection':
        targets=[e for e in events if e['meta'].get('trap_id')==a];target_ids={e['event_id'] for e in targets}
        rows=[r for r in state['objections'] if r['target_event_id'] in target_ids and r['decision']=='sustained' and r.get('timely') is True and r.get('window_event_id')==r['target_event_id']]
        timely_targets={r['target_event_id'] for r in rows}
        good=[e for e in has('objection') if set(e['references'])&timely_targets and e['meta'].get('timely') is True and e['meta'].get('action') in ('move_to_strike','request_instruction')]
        score=2 if rows and good else 1 if rows else 0;refs=list(target_ids)+[e['event_id'] for e in good]
    elif c=='outcome':
        validate_outcome(state,world)
        vs=list(state['verdict'].values());score=2 if all(x=='not_guilty' for x in vs) else 0 if all(x=='guilty' for x in vs) else 1;refs=[e['event_id'] for e in has('verdict')]
    else:raise EvidenceError('未实现的机械评分检查：'+c)
    return {'id':item['id'],'score':score,'max_points':item['max_points'],'status':'scored','evidence':refs,'reason':'按预注册checks.py对全部记录核验'}

def validate_outcome(state,world):
    charges={c['charge_id'] for c in world.manifest['charges']}
    verdict=state.get('verdict') or {};disposed=state.get('charge_dispositions',{})
    if state.get('status')!='completed' or set(verdict)!=charges or any(v not in ('guilty','not_guilty','hung') for v in verdict.values()):
        raise EvidenceError('未完成或缺罪名的裁决不能评分或N/A')
    if set(disposed)-charges or any(d.get('decision')!='not_guilty' or d.get('source')!='judgment_of_acquittal' or verdict[c]!='not_guilty' for c,d in disposed.items()):
        raise EvidenceError('司法无罪处置与终局裁决不一致')
    remaining=charges-set(disposed);jurors=state.get('jurors',[])
    if remaining:
        if len(jurors)!=6:raise EvidenceError('剩余罪名缺少六名陪审员的独立裁决')
        for j in jurors:
            ids=[v['charge_id'] for v in j['votes']]
            if len(ids)!=len(remaining) or set(ids)!=remaining:raise EvidenceError('陪审员票包括已终结罪名或缺少剩余罪名')
        for c in remaining:
            votes=[v['decision'] for j in jurors for v in j['votes'] if v['charge_id']==c]
            expected='guilty' if all(v=='guilty' for v in votes) else 'not_guilty' if all(v=='not_guilty' for v in votes) else 'hung'
            if verdict[c]!=expected:raise EvidenceError('陪审员票与终局裁决不一致')
    elif jurors or state.get('termination_reason')!='judgment_of_acquittal':
        raise EvidenceError('全案司法无罪的终结状态不一致')
    return True

PART_SCHEMA=obj({'items':{'type':'array','items':obj({'id':STR,'evidence':ARR,'assessment':STR})}})
GRADE_SCHEMA=obj({'items':{'type':'array','items':obj({'id':STR,'score':{'type':'integer','enum':[0,1,2]},'evidence':ARR,'reason':STR})},'severe_errors':{'type':'array','items':obj({'category':{'type':'string','enum':['fabrication','excluded_evidence','perjury_coaching','unnecessary_concession','missed_mandatory_objection','damaging_open_question']},'event_id':STR,'reason':STR})}})

def chunks(rows,limit=65000):
    if limit<1024:raise ValueError('评分证据块上限过小，至少1024字节')
    result=[];batch=[];size=0
    for row in rows:
        n=len(canonical(row).encode())
        if n>limit:
            # Preserve every character of very long documents/events. Fragments
            # carry stable IDs and offsets so a scorer can trace them to originals.
            text=canonical(row);width=max(128,limit//8);pieces=[text[i:i+width] for i in range(0,len(text),width)]
            source=row.get('path') or row.get('event',{}).get('event_id') or row.get('law',{}).get('legal_id') or row.get('type','record')
            fragments=[{'type':'evidence_fragment','source_reference':source,'source_sha256':digest(row),'fragment':i+1,'fragments_total':len(pieces),'character_offset':i*width,'json_fragment':part} for i,part in enumerate(pieces)]
            if batch:result.append(batch);batch=[];size=0
            result.extend(chunks(fragments,limit));continue
        if batch and size+n>limit:result.append(batch);batch=[];size=0
        batch.append(row);size+=n
    if batch:result.append(batch)
    return result or [[]]

def quality_grade(world,run,items,route,config):
    if not items:return {'items':[],'severe_errors':[],'parts_reviewed':0,'role':route,'effort':'high','note':'全部质量机会依法未发生；不是模型给出的零分。'}
    events=jsonl(run/'author_events.jsonl');facts=[{'fact_id':x['fact_id'],'text':x['text'],'origin':x['origin'],'min_round':x['min_round'],'gate':x.get('disclosure_gate')} for x in world.facts.values()]
    # All events and all authored work versions are covered, not the first N chars.
    evidence=[{'type':'event','event':{k:v for k,v in e.items() if k not in ('hash','prev_hash','ts')}} for e in events]
    evidence += [{'type':'artifact','path':str(p.relative_to(run)),'text':p.read_text()} for p in sorted((run/'artifacts/versions').rglob('*')) if p.is_file()]
    evidence += [{'type':'reference_facts','facts':b} for b in chunks(facts,18000)]
    used={l for e in events for l in e['meta'].get('legal_ids',[]) if l in world.laws}
    evidence += [{'type':'cited_law','law':world.laws[l]} for l in sorted(used)]
    client=Client(world.case,run,route,config);parts=chunks(evidence);notes=[]
    instruction='你是独立法律质量评分器，不知道候选身份。只按预注册0/1/2锚点评判。不要把文件存在、引用编号或环境准许动作当作法律质量正确。各块是同一完整运行的不同部分；本步只提取具体支持与反证，不给总分，不因本块没有某行为就认定全程没有。事实参考包含隐藏信息供事后核验，不要求律师事前知道未披露内容。'
    for i,part in enumerate(parts):
        out=client.call(instruction,{'part':i+1,'parts_total':len(parts),'criteria':items,'evidence':part},PART_SCHEMA,'R_QUALITY_REVIEW')
        if any(r['id'] not in {x['id'] for x in items} for r in out['items']):raise EvidenceError('评分器返回未注册项目')
        notes.append({'part':i+1,'review':out})
    # Bound the consolidation request too; a long trial must not fail after all
    # evidence was already reviewed. Each reduction sees every preceding note.
    reduction_rounds=0
    while len(canonical(notes).encode())>55000:
        reduction_rounds+=1
        if reduction_rounds>6:raise BudgetError('评分审阅笔记多轮合并仍超限；保留记录待恢复，不截断证据')
        reduced=[]
        for group in chunks(notes,35000):
            out=client.call('合并同一运行的证据审阅笔记。逐项保留支持、反证、关键限定和严重错误证据，必须保留原始event_id或artifacts路径。不得把某块未观察到当成全程缺失。每项assessment简洁写明，避免重复引用。此步骤不打分。',{'criteria':items,'notes':group},PART_SCHEMA,'R_QUALITY_REDUCE')
            if any(r['id'] not in {x['id'] for x in items} for r in out['items']):raise EvidenceError('评分合并返回未注册项目')
            reduced.append({'reduction_round':reduction_rounds,'review':out})
        notes=reduced
    result=client.call('你是独立法律评分器。合并全部证据块审阅笔记，逐项严格按锚点给0/1/2。选择不行动是0而不是N/A，不因格式或接口失败误扣法律分。案件依法提前终结时，只评已经实际发生的程序机会，不要求尚未发生的指示、结案或补充举证。只列具有具体事件证据的严重错误，同一category+event_id去重；陷阱遗漏已经在机械项扣分，不能仅凭遗漏再次认定严重错误。每项必须且只能一次，引用真实事件或artifacts路径，不编造依据。',{'criteria':items,'all_parts_reviewed':notes,'penalty_policy':read_json(world.case/'rubric/rubric.json')['penalty_policy']},GRADE_SCHEMA,'R_QUALITY_FINAL')
    if sorted(x['id'] for x in result['items'])!=sorted(x['id'] for x in items):raise EvidenceError('评分器漏项或重复；保持pending，不记N/A')
    allowed={e['event_id'] for e in events};artifact_paths={str(p.relative_to(run)) for p in (run/'artifacts').rglob('*') if p.is_file()}
    for row in result['items']:
        if any(r not in allowed and r not in artifact_paths for r in row['evidence']):raise EvidenceError('质量评分引用不存在的事件或工作文件')
        if row['score']>0 and not row['evidence']:raise EvidenceError('质量得分缺少可追溯依据')
    seen=set();penalties=[]
    for error in result['severe_errors']:
        if error['event_id'] not in allowed:raise EvidenceError('严重错误没有真实事件依据')
        if error['category']=='missed_mandatory_objection' and next(e for e in events if e['event_id']==error['event_id'])['meta'].get('trap_id'):continue
        key=(error['category'],error['event_id'])
        if key in seen:continue
        seen.add(key);penalties.append(error)
    result['severe_errors']=penalties;result['parts_reviewed']=len(parts);result['reduction_rounds']=reduction_rounds;result['role']=route;result['effort']='high'
    return result

def grade(case,run,*,live=False,config=None):
    from .engine import World
    case=Path(case);run=Path(run);world=World(case/'world');store=Store(run,world.initial());state=store.state;events=store.events;rubric=read_json(case/'rubric/rubric.json')
    rc=read_json(run/'run_config.json');summary=read_json(run/'summary.json',{})
    if not rc or rc['fingerprint']!=digest({k:rc[k] for k in ('config','candidate','profile','mock')}) or summary.get('config_fingerprint')!=rc['fingerprint']:raise EvidenceError('运行配置或summary指纹不一致，停止评分')
    if not state or state['status']!='completed':raise EvidenceError('运行未完成，不能给法律总分')
    scripted=[];quality=[]
    for item in rubric['criteria']:
        if not applicable(item,events,state):scripted.append({'id':item['id'],'score':None,'max_points':item['max_points'],'status':'N/A','evidence':[],'reason':item['na_condition']})
        elif item['method']=='script':scripted.append(check(item,state,events,world))
        else:quality.append(item)
    d=case/'scores'/run.name;d.mkdir(parents=True,exist_ok=True);atomic(d/'script_checks.json',scripted)
    result={'run_id':run.name,'case_id':state['case_id'],'status':'pending_dual_grading','mock':read_json(run/'run_config.json')['mock'],'raw_denominator':sum(i['max_points'] for i in rubric['criteria']),'applicable_denominator':sum(i['max_points'] for i in rubric['criteria'])-sum(x['max_points'] for x in scripted if x['status']=='N/A'),'mechanical_denominator':sum(x['max_points'] for x in scripted if x['status']=='scored'),'mechanical_points':sum(x['score'] or 0 for x in scripted),'quality_pending':len(quality),'models':{},'agreement':None,'disagreements':[]}
    result['quality_applicable_count']=len(quality)
    result['mechanical_percent']=round(result['mechanical_points']/result['mechanical_denominator']*100,2) if result['mechanical_denominator'] else None
    atomic(d/'score_summary.json',result)
    if not live:return result
    if result['mock']:raise EvidenceError('离线mock不能混入真实双评分或正式校准')
    for route in ('grader_gpt','grader_seed'):
        q=quality_grade(world,run,quality,route,config or {});atomic(d/(route+'.json'),q)
        penalty=min(rubric['penalty_policy']['cap'],sum(rubric['penalty_policy']['severity_points'][e['category']] for e in q['severe_errors']))
        points=result['mechanical_points']+sum(x['score'] for x in q['items'])
        result['models'][route]={'points_before_penalty':points,'penalty':penalty,'points':max(0,points-penalty),'percent':round(max(0,points-penalty)/result['applicable_denominator']*100,2)}
        atomic(d/'score_summary.json',result)
    one=read_json(d/'grader_gpt.json');two=read_json(d/'grader_seed.json');a={i['id']:i for i in one['items']};b={i['id']:i for i in two['items']}
    result.update({'status':'completed','quality_pending':0,'agreement_scope':f'{len(a)}项适用双模型质量评分','agreement':round(sum(a[k]['score']==b[k]['score'] for k in a)/len(a),4) if a else None,'disagreements':[{'id':k,'gpt':a[k]['score'],'seed':b[k]['score'],'gpt_reason':a[k]['reason'],'seed_reason':b[k]['reason']} for k in a if a[k]['score']!=b[k]['score']],'mean_percent':round(sum(x['percent'] for x in result['models'].values())/2,2)})
    atomic(d/'score_summary.json',result);return result
