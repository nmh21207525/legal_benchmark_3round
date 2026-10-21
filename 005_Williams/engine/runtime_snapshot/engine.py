"""Data-driven three-round court with scoped role input and transactional effects.
The lawyer chooses actions. The orchestrator owns stage order, disclosure and effects.
"""
from __future__ import annotations
import copy, json, re
from pathlib import Path
from .store import *
from .llm import Client
from .context import preview,compact,page,coverage_complete,size,VIEW_BYTES,PAGE_CHARS

STR={'type':'string'};ARR={'type':'array','items':STR};BOOL={'type':'boolean'}
def obj(props):return {'type':'object','properties':props,'required':list(props),'additionalProperties':False}
ACTION_SCHEMA=obj({'action':STR,'target':STR,'text':STR,'query':STR,'references':ARR,'legal_ids':ARR,'materials':ARR,'filename':STR,'content':STR,'purpose':STR,'ground':STR,'testify':BOOL})
CLAIM_SCHEMA=obj({'fact_ids':ARR,'text':STR})
WITNESS_SCHEMA=obj({'claims':{'type':'array','items':CLAIM_SCHEMA},'unknown':BOOL,'answer':STR})
VERIFY_SCHEMA=obj({'supported':BOOL,'reason':STR})
JUROR_SCHEMA=obj({'votes':{'type':'array','items':obj({'charge_id':STR,'decision':{'type':'string','enum':['guilty','not_guilty']},'reason':STR})}})
MOTION_SCHEMA=obj({'decision':{'type':'string','enum':['granted','denied','limited']},'reason':STR,'legal_ids':ARR,'record_refs':ARR})
ACQUITTAL_SCHEMA=obj({**MOTION_SCHEMA['properties'],'charge_decisions':{'type':'array','items':obj({'charge_id':STR,'decision':{'type':'string','enum':['granted','denied']},'reason':STR})}})
SUBPOENA_SCHEMA=obj({'decision':{'type':'string','enum':['granted','denied','limited']},'authorized_documents':ARR,'reason':STR,'limitations':STR})
TEXT_SCHEMA=obj({'text':STR})
PROS_OBJECTION_SCHEMA=obj({'object':BOOL,'ground':STR,'reason':STR})
CONSENT_SCHEMA=obj({'testify':BOOL,'reason':STR})
RECORD_NOTE_SCHEMA=obj({'items':{'type':'array','items':obj({'source_ids':ARR,'summary':STR})}})
GROUNDS={'foundation':'证据基础或个人知识','character':'品格或前科','hearsay':'传闻','vouching':'控方为证人背书','silence':'评论被告沉默','misstates_law':'曲解法律','burden':'倒置证明责任','record_boundary':'跨阶段使用材料','leading':'主询问诱导','speculation':'臆测','relevance':'相关性或偏见'}
READ_ACTIONS=['list_files','read_document','search_case','search_record','read_record','read_artifact','search_law','read_law']
BASE_ACTIONS=['read_record','read_artifact','list_files','read_document','search_case','search_record','search_law','read_law','request_discovery','request_subpoena','investigate','interview_client','interview_witness','hire_expert','ask_expert','write_artifact','elect_testimony','file_motion','pass']
COST={'read_record':.25,'read_artifact':.25,'list_files':0,'read_document':.25,'search_case':.25,'search_record':.25,'search_law':.25,'read_law':.25,'request_discovery':.5,'request_subpoena':1,'investigate':1.5,'interview_client':1,'interview_witness':1,'hire_expert':1,'ask_expert':1,'write_artifact':.5,'file_motion':1,'elect_testimony':0,'ask_witness':0,'offer_exhibit':0,'object':0,'move_to_strike':0,'request_instruction':0,'opening':0,'closing':0,'pass':0}

class World:
    def __init__(self,root):
        self.root=Path(root);self.case=self.root.parent;self.manifest=read_json(self.root/'manifest.json')
        self.docs={d['doc_id']:d for d in read_json(self.root/'documents_index.json')}
        self.facts={f['fact_id']:f for f in jsonl(self.root/'facts.jsonl')}
        self.roles={r['role_id']:r for r in (read_json(p) for p in (self.root/'roles').glob('*.json'))}
        self.laws={l['legal_id']:l for l in jsonl(self.case/'legal_corpus/corpus.jsonl')}
        self.stages=read_json(self.root/'procedure.json');self.traps=read_json(self.root/'traps.json');self.motions={m['motion_id']:m for m in read_json(self.root/'motion_rules.json')}
        self.task=(self.root/'task.md').read_text(encoding='utf-8')
    def text(self,did):return (self.root/self.docs[did]['path']).read_text(encoding='utf-8')
    def active(self,f,state):
        return f.get('min_round',0)<=state['round'] and (not f.get('disclosure_gate') or f['disclosure_gate'] in state['gates'])
    def role_facts(self,rid,state,question='',references=None):
        r=self.roles[rid];read_refs=set(references or [])&set(state['read_documents'])
        result=[]
        for fid in r.get('fact_ids',[]):
            f=self.facts[fid]
            if not self.active(f,state):continue
            if fid in r.get('conditional_fact_ids',[]):
                confront=bool(read_refs&set(f.get('confrontation_docs',f.get('doc_ids',[]))))
                terms=f.get('question_terms',('具体','签名','是否','承认','承認','admit','signature','confront','嘴唇'))
                specific=any(k.lower() in question.lower() for k in terms)
                learned=rid=='R_DEFENDANT' and fid in state.get('client_confronted_fact_ids',[])
                if not (learned or confront and specific):continue
            result.append({'fact_id':fid,'text':f['text']})
        return result
    def initial(self):
        m=self.manifest
        return {'protocol':'teacher-three-round-v2','case_id':m['case_id'],'world_version':m['version'],'status':'running','stage_index':0,'stage_id':self.stages[0]['stage_id'],'round':0,
                'slot':0,'script_cursor':0,'script':None,'pending_window':None,'pending_answer':None,'pending_rebuttal':False,
                'disclosed':m['initial_documents'][:],'read_documents':[],'law_reads':[],'gates':[],'hours_used':0,'actions_used':0,'money_used':0,
                'client_interviews':[],'investigations':[],'discovery_requests':[],'subpoenas':[],'experts':{},'motions':[],'objections':[],'questions':[],
                'artifacts':{},'rulings':[],'stricken':[],'admonished':[],'excluded_documents':[],'limited_documents':{},'heard_roles':[],
                'read_coverage':{},'client_confronted_fact_ids':[],'reached_stages':[],'charge_dispositions':{},'termination_reason':None,'terminated_at_stage':None,
                'defendant_testified':False,'testimony_decision':None,'traps_fired':[],'closed_records':{},'opponent_pending':None,'preparation_remaining':0,'approved_instructions':[],'verdict':None,'jurors':[],'last_hash':'0'*64,'accepted_steps':0,'protocol_errors':0,'tool_result':None}

class Engine:
    def __init__(self,case,run_dir,*,candidate=None,profile=None,mock=True,config=None,clients=None):
        self.world=World(Path(case)/'world');self.root=Path(case);self.run=Path(run_dir);self.mock=mock;self.profile=profile;self.candidate=candidate
        self.store=Store(self.run,self.world.initial());self.config=config or {};self.clients=clients or {}
        self.s=self.store.state
        if self.s['case_id']!=self.world.manifest['case_id']:raise EvidenceError('case_id与checkpoint不一致')
        self.config_fingerprint=digest({'config':self.config,'candidate':candidate,'profile':profile,'mock':mock})
        previous=read_json(self.run/'run_config.json')
        if previous and previous['fingerprint']!=self.config_fingerprint:raise EvidenceError('恢复时模型、模式或配置发生改变，须新建运行目录')
        atomic(self.run/'run_config.json',{'fingerprint':self.config_fingerprint,'candidate':candidate,'profile':profile,'mock':mock,'config':self.config})
        (self.run/'model_io.jsonl').touch(exist_ok=True)
    def client(self,route):
        if route not in self.clients:self.clients[route]=Client(self.root,self.run,route,self.config)
        return self.clients[route]
    def environment_call(self,instructions,payload,schema,role='',**kwargs):
        """Cover long public records in validated chunks, retaining immutable originals."""
        payload=copy.deepcopy(payload)
        if size(payload)>95000:
            for key in ('record','testimony','admitted_evidence'):
                if isinstance(payload.get(key),list) and size(payload[key])>24000:
                    payload[key]={'representation':'verified_record_notes','original_rows':len(payload[key]),'notes':self.record_notes(payload[key],instructions)}
        if size(payload)>105000:raise BudgetError('角色材料仍超过上下文安全上界；完整证据保留，不能截断或伪造裁定')
        return self.client('environment').call(instructions,payload,schema,role,**kwargs)
    def record_notes(self,rows,purpose,depth=0):
        pieces=[]
        for i,row in enumerate(rows):
            source_id=row.get('event_id') or (row.get('source_ids') or [f'row:{i}'])[0]
            source_ids=row.get('source_ids') or [source_id]
            encoded=canonical(row)
            for offset in range(0,len(encoded),4000):pieces.append({'source_id':source_id,'source_ids':source_ids,'part_offset':offset,'content':encoded[offset:offset+4000]})
        batches=[];batch=[]
        for piece in pieces:
            if batch and size(batch+[piece])>26000:batches.append(batch);batch=[]
            batch.append(piece)
        if batch:batches.append(batch)
        notes=[]
        for index,batch in enumerate(batches):
            allowed={sid for x in batch for sid in x['source_ids']}
            request={'purpose':preview(purpose,2500),'block':index+1,'blocks':len(batches),'source_parts':batch}
            for attempt in range(2):
                result=self.client('environment').call('将这一块正式记录压缩成依据明确的笔记。保留与purpose有关的双方有利和不利内容、否定、时间、裁定、准入/删除/限定用途和未知，不能补充事实或法律。每项给真实source_ids；整个输出必须覆盖本块每个来源编号，程序性或不相关记录可合并注明但不能无声遗漏。总输出尽量在2000汉字以内；来源文本可能是同一记录的分段，不把截断句补写成事实。',request,RECORD_NOTE_SCHEMA,'R_RECORD_REVIEW')
                ids=[r for item in result['items'] for r in item['source_ids']]
                if set(ids)!=allowed or any(not item['source_ids'] for item in result['items']) or size(result)>10000:
                    request['repair']='必须只引用本块source_ids，每项均有来源，并将总输出压缩至安全长度。';continue
                verdict=self.client('environment').call('核验notes中的每一事实和程序判断是否完全由source_parts支持；若丢失否定、删除/限定用途或补入新事实，supported=false。只是主题压缩且未歪曲允许true。',{'source_parts':batch,'notes':result},VERIFY_SCHEMA,'R_RECORD_VALIDATOR')
                if verdict['supported']:notes.extend(result['items']);break
                request['repair']=verdict['reason']
            else:raise EvidenceError('记录压缩两次未通过来源校验；保留原文并暂停')
        if size(notes)>55000:
            if depth>=2:raise BudgetError('经分层逐块核验的正式记录仍过长；保留全记录，不能静默删减')
            return self.record_notes(notes,purpose,depth+1)
        return notes
    @property
    def stage(self):return self.world.stages[self.s['stage_index']]
    def ev(self,kind,text='',actor='trusted_orchestrator',references=None,audience=None,**meta):
        return {'kind':kind,'actor':actor,'text':text,'references':references or [],'audience':audience or ['R_DEFENSE','R_PROSECUTOR','R_JUDGE'],'meta':meta}
    def commit(self,events,artifacts=None):
        self.s['accepted_steps']+=1;self.store.commit(self.s,events,artifacts);self.s=self.store.state
    def allowed(self):
        if self.s['status']=='completed':return []
        k=self.stage['kind']
        if self.s['pending_window']:actions=['object','move_to_strike','request_instruction','pass']
        elif self.s['preparation_remaining'] or k=='preparation':actions=BASE_ACTIONS
        elif k=='motion':actions=['file_motion','write_artifact','pass']
        elif k=='examination':actions=['ask_witness','offer_exhibit','object','move_to_strike','request_instruction','elect_testimony','file_motion','write_artifact','pass']
        elif k=='opening':actions=['opening']
        elif k=='instructions':actions=['request_instruction','file_motion','pass']
        elif k=='closing':actions=['closing']
        else:return []
        return list(dict.fromkeys(actions+READ_ACTIONS))
    def visible_events(self):
        out=[]
        for e in self.store.events:
            if 'R_DEFENSE' not in e['audience']:continue
            meta={k:v for k,v in e['meta'].items() if k in ('decision','ground','purpose','doc_id','filename','version','scope')}
            out.append({'event_id':e['event_id'],'stage_id':e['stage_id'],'round':e['round'],'kind':e['kind'],'actor':e['actor'],'text':e['text'],'references':e['references'],'meta':meta,
                        'stricken':e['event_id'] in self.s['stricken'],'admonished':e['event_id'] in self.s['admonished']})
        return out
    def court_events(self):
        ids={e['event_id'] for e in self.store.events if 'R_JUDGE' in e['audience']}
        return [e for e in self.visible_events() if e['event_id'] in ids]
    def view(self):
        s=self.s;latest=self.visible_events()
        def event_view(e):
            out=compact(e,1800,40);out['text_truncated']=len(out['text'])!=len(e['text']);return out
        v={'case_id':s['case_id'],'task':self.world.task,'stage':self.stage,'slot':s['slot'],'round':s['round'],'allowed_actions':self.allowed(),
         'budgets':{'hours_left':self.world.manifest['budget']['pretrial_hours']-s['hours_used'],'actions_left':self.world.manifest['budget']['max_actions']-s['actions_used'],'money_left':self.world.manifest['budget']['money']-s['money_used'],'rates':COST},
         'documents':[{'doc_id':d,'title':self.world.docs[d]['title'],'topics':self.world.docs[d]['topics']} for d in s['disclosed']],
         'read_documents':s['read_documents'],'read_laws':s['law_reads'],'working_documents':{d:preview(self.world.text(d),1000) for d in s['read_documents']},
         'artifacts':{k:{'version':a['version'],'content':preview(a['content'],1000),'round':a['round'],'stage_id':a.get('stage_id'),'read_via':'read_artifact','total_characters':len(a['content'])} for k,a in s['artifacts'].items()},
         'motions':[{k:compact(m.get(k),500,12) for k in ('motion_id','decision','reason','stage_id','round','references','legal_ids')} for m in s['motions']],
         'rulings':compact(s['rulings'][-4:],500,8),'investigations':compact(s['investigations'],500,6),'discovery_requests':compact(s['discovery_requests'],500,6),'subpoenas':compact(s['subpoenas'],500,6),
         'experts':{rid:{'materials':x['materials'],'opinions':[preview(t,1000) for t in x['opinions'][-2:]]} for rid,x in s['experts'].items()},
         'client_interviews':compact(s['client_interviews'][-2:],800,16),'testimony_decision':compact(s['testimony_decision'],800),'defendant_testified':s['defendant_testified'],
         'witnesses':[{'role_id':r,'name':self.world.roles[r]['display_name']} for r in self.available_witnesses()],
         'expert_options':[{'role_id':r,'fee':x['fee']} for r,x in self.world.manifest['experts'].items()],
         'objection_catalog':[{'ground':g,'label':l} for g,l in GROUNDS.items()],
         'evidence_status':[{'doc_id':d,'disclosed':True,'read':d in s['read_documents'],'offered':any(e['kind']=='exhibit_offered' and d in e['references'] for e in self.store.events),'admitted_as_exhibit':any(e['kind']=='exhibit' and d in e['references'] for e in self.store.events),'excluded':d in s['excluded_documents'],'limited_purpose':s['limited_documents'].get(d)} for d in s['disclosed']],
         'charge_dispositions':s.get('charge_dispositions',{}),
         'recent_events':[event_view(e) for e in latest[-12:]],'record_count':len(latest),
         'record_access':'所有庭审决策和异议窗口均可分页只读，不消耗实体庭审slot。search_record(query,target=offset:0)检索完整可见记录；read_record(target=事件ID,query=offset:0)读事件全文；read_document/read_law(target=编号,query=offset:0)读原文；read_artifact(target=文件名,query=offset:0)读完整工作文件；purpose=version:1可读历史版本。按next_offset继续。预览不等于全文，read_documents/read_laws只有全页读完才登记。完整来源一直保存在记录中。',
         # A page can contain four-byte Unicode characters; preserve the full
         # text the cursor/coverage certifies as read in the candidate's view.
         'last_tool_result':compact(s['tool_result'],4*PAGE_CHARS,16)}
        v['stage']={k:x for k,x in self.stage.items() if k not in ('release_documents',)}
        if s['preparation_remaining']:v['stage']={**v['stage'],'kind':'preparation','relief_actions_remaining':s['preparation_remaining']}
        last=next((e for e in reversed(latest) if e['kind']=='tool_result'),None)
        if last:v['last_tool_result_record_id']=last['event_id']
        if s['pending_window']:
            e=next(e for e in latest if e['event_id']==s['pending_window']);v['window']={'statement':event_view(e),'free':True}
        # Bounds are only on the view, never on persisted source/record text.
        if size(v)>VIEW_BYTES:
            v['working_documents']={d:'[全文已读；按read_document分页重读]' for d in s['read_documents']}
            for a in v['artifacts'].values():a['content']='[按read_artifact分页读取完整文件]'
            v['recent_events']=[event_view(e) for e in latest[-4:]]
            v['rulings']=compact(s['rulings'][-2:],300,6)
        if size(v)>VIEW_BYTES:raise BudgetError('候选元数据视图异常超过安全上界；完整数据保留，需检查可见对象数量')
        return v
    def available_witnesses(self):
        ids=list(self.stage.get('witnesses',[]))
        if self.s['stage_id']=='r2_defense':
            ids+=['R_COUSIN','R_MOTEL_CLERK'] if self.s['case_id'].endswith('005') else ['R_RECONSTRUCTION','R_MEDICAL_EXPERT']
        if self.s['defendant_testified'] or self.s['testimony_decision'] and self.s['testimony_decision']['testify']:ids+=['R_DEFENDANT']
        ids+=list(self.s['experts'])
        return list(dict.fromkeys(r for r in ids if r in self.world.roles))
    def ask_candidate(self):
        if self.profile:
            from .policy import action_for
            return action_for(self.view(),self.profile)
        schema=copy.deepcopy(ACTION_SCHEMA);schema['properties']['action']={'type':'string','enum':self.allowed()}
        instructions='视图里的文书和工作文件是有界预览；需要全文时用分页读取工具，按next_offset继续。庭审中的只读不耗实体slot。你是被测辩护律师。按委托和当前权限自主选择一个行动，返回严格JSON。只用已披露文件、已读资料及可见记录，不创造事实或法源。准备工作和法院提交应区分。窗口对所有控方发言统一开放，须自行判断是否需要反应。未使用字段填空字符串、空数组或false。'
        return self.client(self.candidate).call(instructions,self.view(),schema,'R_DEFENSE')
    def step(self):
        if self.s['status']=='completed':return
        self.s['status']='running'
        # stage transition hooks and opponent actions are individually checkpointed.
        if self.s['script'] is None:
            self.enter();return
        if self.s['pending_window']:
            a=self.ask_candidate();self.apply(a,window=True);return
        if self.s['pending_answer']:
            p=self.s['pending_answer'];self.s['pending_answer']=None
            if p.get('skip'):self.commit([self.ev('answer_blocked','问题异议成立，未要求证人回答。')]);return
            ans=self.witness(p['role'],p['question'],p.get('references',[]));self.record_answer(p['role'],ans,p.get('by','R_PROSECUTOR'));return
        if self.s['opponent_pending']:
            item=self.s['opponent_pending'];self.s['opponent_pending']=None;self.run_script_item(item);return
        if self.s['preparation_remaining']:
            self.apply(self.ask_candidate());return
        if self.s['script_cursor']<len(self.s['script']):
            item=self.s['script'][self.s['script_cursor']];self.s['script_cursor']+=1
            self.run_script_item(item);return
        if self.stage['kind']=='jury':self.deliberate();return
        if self.stage['kind']=='ruling':self.close_record();self.advance();return
        if self.s['slot']>=self.stage['slots']:
            if self.stage['kind']=='closing' and not self.s['pending_rebuttal']:
                self.s['pending_rebuttal']=True
                self.s['script']+=self.closing_items(rebuttal=True);self.commit([]);return
            self.advance();return
        a=self.ask_candidate();self.apply(a)
    def enter(self):
        self.s['reached_stages']=list(dict.fromkeys(self.s.get('reached_stages',[])+[self.s['stage_id']]))
        events=[self.ev('stage_entered',self.s['stage_id'])]
        for did in self.stage.get('release_documents',[]):
            if did not in self.s['disclosed']:
                self.s['disclosed'].append(did)
                gate=self.world.docs[did].get('gate')
                if gate and gate not in self.s['gates']:
                    self.s['gates'].append(gate);events.append(self.ev('stage_release','stage_release:'+gate,references=[did],scope=gate))
                events.append(self.ev('late_disclosure','控方补充披露 '+did+'《'+self.world.docs[did]['title']+'》。请自行核对并决定救济。',references=[did]))
        self.s['script']=self.script_for_stage();self.s['script_cursor']=0;self.s['pending_rebuttal']=False
        self.commit(events)
    def script_for_stage(self):
        sid=self.s['stage_id'];out=[]
        if sid=='r2_opening':return [{'type':'prosecution','text':'控方将依法证明各项要件，并请求在全部证据审理后定罪；辩方仍可质询证据。'}]
        if sid in ('r1_examination','r2_examination','r3_supplement'):
            witnesses=self.stage['witnesses'][:2] if sid=='r1_examination' else self.stage['witnesses']
            for rid in witnesses:
                if sid=='r3_supplement':q='请区分你能确认的内容、后来补交的记录以及仍不能确定的内容。'
                else:q='请说明你亲自参与的事项、依据和记录的局限；不把他人转述说成目击。'
                out.append({'type':'question','role':rid,'text':q})
            for trap in self.world.traps:
                if trap['phase']==sid:out.append({'type':'trap','trap':trap['trap_id'],'text':trap['text']})
        if sid=='r3_instructions':
            out=[{'type':'trap','trap':t['trap_id'],'text':t['text']} for t in self.world.traps if t['phase']==sid]
        if sid=='r3_closing':out=self.closing_items()
        return out
    def closing_items(self,rebuttal=False):
        if rebuttal:return [{'type':'rebuttal','text':''}]
        out=[{'type':'prosecution','text':'请根据已采纳证据分别分析各项要件。控方坚持已经排除合理怀疑，律师陈词本身不替代证据。'}]
        for t in self.world.traps:
            if t['phase']!='r3_closing':continue
            if t.get('unless_defendant_testified') and self.s['defendant_testified']:continue
            out.append({'type':'trap','trap':t['trap_id'],'text':t['text']})
        return out
    def run_script_item(self,item):
        typ=item['type']
        if typ=='question':
            if not self.mock:
                item=copy.deepcopy(item)
                item['text']=self.environment_call('你是争取定罪的检察官。根据该证人已开放的个人知识和真实法庭记录提出一个具体问题，为控方有利事实打基础或交叉质询实际辩方证言。不要中立地替辩方总结缺口，不引用尚未披露的事实，不创造事实。',{'suggested_topic':item['text'],'witness_facts':self.world.role_facts(item['role'],self.s),'record':self.court_events()[-18:],'charges':self.active_charges()},TEXT_SCHEMA,'R_PROSECUTOR',effort='high')['text']
            e=self.ev('question',item['text'],'R_PROSECUTOR',witness=item['role'],examination_mode='direct')
            self.s['pending_answer']={'role':item['role'],'question':item['text'],'references':[],'by':'R_PROSECUTOR'}
        elif typ=='rebuttal':
            defense=[e['text'] for e in self.store.events if e['kind']=='closing' and e['actor']=='R_DEFENSE']
            text='控方请求依已采纳记录判断，辩方提出的资料局限应与其他证据一并衡量。'
            if not self.mock:
                text=self.environment_call('你是以定罪为目标但遵守职业伦理的检察官。只回应辩方实际结案论点，以准入记录为基础；不创造事实，不评论沉默，不把自己陈词当证据。',{'defense':defense,'record':self.jury_evidence(),'charges':self.active_charges()},TEXT_SCHEMA,'R_PROSECUTOR',effort='high')['text']
            e=self.ev('closing_rebuttal',text,'R_PROSECUTOR')
        else:
            kind='trap_statement' if typ=='trap' else 'prosecution_statement'
            meta={}
            if typ=='trap':
                meta['trap_id']=item['trap'];self.s['traps_fired'].append({'trap_id':item['trap'],'seq':len(self.store.events)+1})
            if self.stage['kind']=='closing':kind='closing'
            e=self.ev(kind,item['text'],'R_PROSECUTOR',**meta)
        self.s['pending_window']=f'E{len(self.store.events)+1:06d}';self.commit([e])
    def record_answer(self,rid,ans,by):
        if rid=='R_DEFENDANT' and self.s['round']>=2:self.s['defendant_testified']=True
        events=[self.ev('testimony',ans,'{}'.format(rid),references=getattr(self,'answer_refs',[]),witness=rid,claim_units=getattr(self,'answer_units',[]))]
        self.s['heard_roles']=list(dict.fromkeys(self.s['heard_roles']+[rid]))
        # Hearing evidence remains in its own record. Jury receives only properly admitted rounds 2/3.
        if self.s['round']>=2:events[0]['meta'].update({'admitted':True,'purpose':'truth','source_type':'testimony'})
        self.s['pending_window']=f'E{len(self.store.events)+1:06d}' if by=='R_PROSECUTOR' else None
        self.commit(events)
    def witness(self,rid,question,references,facts_override=None):
        if rid in self.world.manifest['experts'] and rid not in self.s['experts']:raise ProtocolError('专家必须先聘请，且仅依据实际提交的材料回答')
        facts=self.world.role_facts(rid,self.s,question,references) if facts_override is None else facts_override
        if rid in self.s['experts'] and facts_override is None:
            facts=[{'fact_id':f,'text':self.world.facts[f]['text']} for d in self.s['experts'][rid]['materials'] for f in self.world.docs[d]['fact_ids'] if self.world.active(self.world.facts[f],self.s)]
        if not facts:self.answer_refs=[];self.answer_units=[];return '我对该问题没有获授权的个人知识，不能补充。'
        if self.mock:
            terms=words(question);ranked=sorted(facts,key=lambda f:-sum(t in f['text'].lower() for t in terms))
            selected=ranked[:5];self.answer_refs=[x['fact_id'] for x in selected];self.answer_units=[{'text':x['text'],'fact_ids':[x['fact_id']]} for x in selected];return '\n'.join(x['text'] for x in selected)
        role=self.world.roles[rid]
        inst='你扮演模拟案件角色。固定身份与personality，动态回答；只用下面允许的事实，不创造细节。每个事实性分句放入claims并列fact_ids；text须为原事实的保守复述。不能以泛泛不知道回避已知内容。未知部分明确说不知道。不要读隐藏材料或评分表。answer只是claims的串联，不得另加事实。'
        payload={'role':{'name':role['display_name'],'personality':role['personality']},'question':question,'allowed_facts':facts,'round':self.s['round']}
        allowed={f['fact_id']:f['text'] for f in facts}
        for repair in range(2):
            out=self.environment_call(inst,payload,WITNESS_SCHEMA,rid)
            bad=[]
            if not out['unknown'] and not out['claims']:bad.append('有实质回答却没有逐句依据')
            for c in out['claims']:
                if not c['fact_ids'] or any(fid not in allowed for fid in c['fact_ids']):bad.append('越界事实编号');continue
                basis=[allowed[fid] for fid in c['fact_ids']]
                if c['text'] not in basis:
                    aliases={'A':self.world.manifest['defendant']}
                    aliases.update({'B':self.world.roles['R_INTAKE_AGENT']['display_name'],'C':self.world.roles['R_ASSIGNED_AGENT']['display_name']} if self.s['case_id'].endswith('005') else {'B':self.world.roles['R_OFFICER']['display_name']})
                    verdict=self.environment_call('只判断claim是否由basis完整支持。允许按identity_aliases把原始A/B/C和化名对应，并将speaker自己的事实保守转换为第一人称；不得改变其他事实主体。不得依靠常识补入数字、人物、因果或时间。任一新增事实、不当确定化或否定均supported=false。',{'claim':c['text'],'basis':basis,'speaker':role['display_name'],'identity_aliases':aliases},VERIFY_SCHEMA,'R_FACT_VALIDATOR')
                    if not verdict['supported']:bad.append(verdict['reason'])
            if not bad:
                self.answer_refs=list(dict.fromkeys(f for c in out['claims'] for f in c['fact_ids']))
                self.answer_units=out['claims']
                return '\n'.join(c['text'] for c in out['claims']) or '我对这一部分没有获授权的个人知识。'
            payload['repair_errors']=bad
        raise EvidenceError('环境角色两次回答未通过事实约束，暂停而不是伪造未知回答')
    def apply(self,a,window=False):
        self.s=copy.deepcopy(self.store.state)
        action=a.get('action');text=a.get('text') or '';refs=a.get('references') or [];laws=a.get('legal_ids') or []
        window=bool(self.s['pending_window'])
        if action not in self.allowed():raise ProtocolError('当前阶段不允许该动作：'+str(action))
        for lid in laws:
            if lid not in self.world.laws:raise ProtocolError('未知法律编号：'+lid)
        known_events={e['event_id'] for e in self.visible_events()}
        known_facts={f for d in self.s['read_documents'] for f in self.world.docs[d]['fact_ids']}
        for e in self.visible_events():known_facts.update(e['references'])
        if any(r not in self.s['read_documents'] and r not in known_events and r not in known_facts for r in refs):raise ProtocolError('引用未读、未公开或未知材料')
        relief_before=self.s['preparation_remaining']
        pre=(self.stage['kind'] in ('preparation','motion') and self.s['round']<=2) or relief_before>0
        free_read=action in READ_ACTIONS and (window or self.stage['kind']!='preparation' and not relief_before)
        hours=COST.get(action,0) if pre and not window and not free_read else 0
        if action not in ('pass','list_files') and pre and not free_read:
            if self.s['hours_used']+hours>self.world.manifest['budget']['pretrial_hours'] or self.s['actions_used']>=self.world.manifest['budget']['max_actions']:raise BudgetError('庭前法律资源已用尽；可pass进入下一阶段，未发生动作副作用')
        events=[];artifacts=[];result=None;charge=pre and not window and not free_read and action not in ('pass','list_files')
        if window and action not in READ_ACTIONS:
            self.window_action(a,timely=True);return
        if action=='list_files':result=[{'doc_id':d,'title':self.world.docs[d]['title']} for d in self.s['disclosed']]
        elif action in ('read_document','read_law','read_artifact','read_record'):
            target=a.get('target') or ''
            cursor=a.get('query') or 'offset:0'
            if not re.fullmatch(r'offset:\d+',cursor):raise ProtocolError('分页读取query须为offset:数字或留空')
            offset=int(cursor.split(':')[1])
            if action=='read_document':
                if target not in self.s['disclosed']:raise ProtocolError('文件未披露')
                content=self.world.text(target)
            elif action=='read_law':
                if target not in self.world.laws:raise ProtocolError('未知法律编号')
                content=self.world.laws[target]['text']
            elif action=='read_artifact':
                if target not in self.s['artifacts']:raise ProtocolError('未知工作文件')
                version=a.get('purpose') or ''
                if version:
                    if not re.fullmatch(r'version:\d+',version):raise ProtocolError('历史工作文件版本purpose须为version:数字')
                    number=int(version.split(':')[1])
                    if not 1<=number<=self.s['artifacts'][target]['version']:raise ProtocolError('未知工作文件版本')
                    path=self.run/'artifacts/versions'/f'{number:03d}'/safe_name(target)
                    if not path.is_file():raise EvidenceError('工作文件历史版本缺失')
                    content=path.read_bytes().decode('utf-8')
                else:content=self.s['artifacts'][target]['content']
            else:
                record=next((e for e in self.visible_events() if e['event_id']==target),None)
                if not record:raise ProtocolError('未知或不可见事件')
                content=canonical(record)
            try:result=page(content,offset,action=action,target=target)
            except ValueError as ex:raise ProtocolError(str(ex)) from ex
            complete=coverage_complete(self.s.setdefault('read_coverage',{}),action+':'+target,offset,result['end_offset'],len(content))
            result['all_pages_read']=complete
            if action=='read_law':result['metadata']={k:v for k,v in self.world.laws[target].items() if k not in ('text','snapshot_path')}
            events.append(self.ev('page_read',action+' '+target,'R_DEFENSE',[target],audience=['R_DEFENSE'],scope=action,offset=offset,end_offset=result['end_offset']))
            key='read_documents' if action=='read_document' else 'law_reads' if action=='read_law' else None
            if key and complete and target not in self.s[key]:
                self.s[key].append(target);events.append(self.ev(action,'全文已实际读取 '+target,'R_DEFENSE',[target],audience=['R_DEFENSE']))
        elif action in ('search_case','search_record'):
            q=a.get('query') or text
            if action=='search_case':
                hits=[{'doc_id':d,'title':self.world.docs[d]['title'],'preview':self.world.text(d)[:350],'full_text_via':'read_document'} for d in self.s['disclosed']]
            else:hits=[{**compact(e,1000,30),'read_via':'read_record'} for e in self.visible_events()]
            terms=words(q);ranked=sorted(hits,key=lambda x:-sum(t in canonical(x).lower() for t in terms))
            cursor=a.get('target') or 'offset:0'
            if not re.fullmatch(r'offset:\d+',cursor):raise ProtocolError('检索分页target须为offset:数字，或留空')
            offset=int(cursor.split(':')[1]);result={'results':ranked[offset:offset+8],'total':len(ranked),'next_offset':f'offset:{offset+8}' if offset+8<len(ranked) else None,'query':q}
            events.append(self.ev('search_record' if action=='search_record' else 'search_case',q,'R_DEFENSE',audience=['R_DEFENSE']))
        elif action=='search_law':
            q=a.get('query') or text;terms=words(q)
            ranked=sorted(self.world.laws.values(),key=lambda x:-sum(t in (x['title']+' '+x['text']).lower() for t in terms))[:8]
            result=[{k:v for k,v in r.items() if k not in ('text','snapshot_path')} for r in ranked]
            events.append(self.ev('search_law',q,'R_DEFENSE',audience=['R_DEFENSE']))
        elif action in ('request_discovery','request_subpoena','investigate'):
            q=(a.get('query') or text).strip()
            if len(q)<8:raise ProtocolError('请说明具体对象、材料及核验目的')
            method={'request_discovery':'discovery','request_subpoena':'subpoena','investigate':'investigate'}[action]
            generic={'records','record','foundation','expert','followup','limits','uncertainty'}
            hits=[d for d,v in self.world.docs.items() if v['release']==method and v['min_round']<=self.s['round'] and topic_match(q,[t for t in v['topics'] if t not in generic]) and d not in self.s['disclosed']]
            subpoena_review=None
            if action=='request_subpoena':
                if not (a.get('purpose') or '').strip():raise ProtocolError('第三方取证须说明相关性、范围和隐私保护')
                subpoena_review=self.review_subpoena(a,hits)
                events.extend([self.ev('subpoena_application',q+'；目的：'+a['purpose'],'R_DEFENSE'),self.ev('subpoena_ruling',subpoena_review['reason'],'R_JUDGE',subpoena_review['authorized_documents'],decision=subpoena_review['decision'],limitations=subpoena_review['limitations'])])
                hits=subpoena_review['authorized_documents']
            self.s['disclosed']=list(dict.fromkeys(self.s['disclosed']+hits));result={'documents':hits,'reason':subpoena_review['reason'] if subpoena_review else '按具体请求取得已有文件' if hits else '未命中可取得的具体材料；请缩小对象、主题或核查权限。没有创造新事实。'}
            if subpoena_review:result['court_review']=subpoena_review
            key={'request_discovery':'discovery_requests','request_subpoena':'subpoenas','investigate':'investigations'}[action]
            self.s[key].append({'query':q,'documents':hits,'round':self.s['round'],'stage_id':self.s['stage_id'],**({'court_review':subpoena_review} if subpoena_review else {})})
            events.append(self.ev(action,q,'R_DEFENSE',refs,audience=['R_DEFENSE'] if action=='investigate' else None,scope=method));events.append(self.ev('disclosure',canonical(result),'R_DISCOVERY',hits,audience=['R_DEFENSE'],scope=method))
        elif action in ('interview_client','interview_witness','ask_witness'):
            rid='R_DEFENDANT' if action=='interview_client' else a.get('target')
            if rid not in self.world.roles:raise ProtocolError('未知角色')
            if rid in self.world.manifest['experts'] and rid not in self.s['experts']:raise ProtocolError('尚未聘请该专家，不可通过普通访谈绕过费用和材料权限')
            if action=='interview_witness' and (not self.world.roles[rid].get('fact_ids') or rid=='R_DEFENDANT'):raise ProtocolError('不可把法官、陪审员或当事人当作普通庭前证人访问')
            if action=='ask_witness' and rid not in self.available_witnesses():raise ProtocolError('该证人未在本阶段可询问名单')
            if not text.strip():raise ProtocolError('问题不能为空')
            if action=='ask_witness' and not self.mock:
                mode='direct' if rid in self.s['experts'] or rid=='R_DEFENDANT' or self.s['stage_id']=='r2_defense' else 'cross'
                objection=self.environment_call('你是争取定罪且恪守伦理的检察官。审查辩方刚提出的这个真实问题，必要时提出具体证据异议。不得为干扰而每问必反对；交叉中的诱导通常允许。只依据问题和公开庭审记录，不读律师私下材料。',{'question':text,'witness':rid,'examination_mode':mode,'record':self.court_events()[-14:]},PROS_OBJECTION_SCHEMA,'R_PROSECUTOR',effort='high')
                if objection['object']:
                    ruling=self.environment_call('你是中立法官。按真实问题、询问类型和理由审查控方异议。granted/limited时本问须重新提出，denied则证人回答。不要因为控方反对就准许。',{'question':text,'mode':mode,'objection':objection,'record':self.court_events()[-14:]},MOTION_SCHEMA,'R_JUDGE',effort='high')
                    events += [self.ev('question',text,'R_DEFENSE',refs,witness=rid,examination_mode=mode),self.ev('prosecution_objection',objection['reason'],'R_PROSECUTOR',ground=objection['ground']),self.ev('prosecution_objection_ruling',ruling['reason'],'R_JUDGE',decision=ruling['decision'])]
                    if ruling['decision']!='denied':
                        self.s['slot']+=1;self.s['tool_result']={'question_blocked':True,'ruling':ruling['reason']};self.commit(events);return
            ans=self.witness(rid,text,refs);frefs=getattr(self,'answer_refs',[])
            kind='private_consultation' if action=='interview_client' else 'interview' if action=='interview_witness' else 'testimony'
            audience=['R_DEFENSE'] if kind!='testimony' else None
            if not any(e['kind']=='question' for e in events):events.append(self.ev('question',text,'R_DEFENSE',refs,audience=audience,witness=rid,examination_mode='cross' if rid!='R_DEFENDANT' else 'direct'))
            events.append(self.ev(kind,ans,rid,frefs,audience=audience,witness=rid,admitted=self.s['round']>=2 and action=='ask_witness',purpose='truth',claim_units=getattr(self,'answer_units',[])))
            result={'answer':ans,'fact_ids':frefs}
            if action=='interview_client':
                self.s['client_interviews'].append({'question':text,'answer':ans,'references':refs,'round':self.s['round']})
                allowed={f['fact_id'] for f in self.world.role_facts('R_DEFENDANT',self.s,text,refs)}
                conditional=set(self.world.roles['R_DEFENDANT'].get('conditional_fact_ids',[]))
                self.s['client_confronted_fact_ids']=list(dict.fromkeys(self.s.get('client_confronted_fact_ids',[])+sorted(allowed&conditional)))
                ds=[]
                for did,doc in self.world.docs.items():
                    required=conditional&set(doc['fact_ids'])
                    if doc['release']=='client' and required and required<=set(self.s['client_confronted_fact_ids']) and self.world.active({'min_round':doc['min_round'],'disclosure_gate':doc.get('gate')},self.s) and did not in self.s['disclosed']:ds.append(did)
                if ds:
                    self.s['disclosed']=list(dict.fromkeys(self.s['disclosed']+ds));events.append(self.ev('disclosure','具体证据与追问条件已逐项满足，取得当事人补充说明。',references=ds,audience=['R_DEFENSE']))
            if action=='ask_witness':
                if rid=='R_DEFENDANT' and self.s['round']>=2:self.s['defendant_testified']=True
                self.s['heard_roles']=list(dict.fromkeys(self.s['heard_roles']+[rid]));self.s['questions'].append({'role':rid,'text':text,'references':refs,'round':self.s['round']})
                if self.s['stage_id']=='r2_defense' or rid in self.s['experts']:
                    self.s['opponent_pending']={'type':'question','role':rid,'text':'针对你刚才实际作出的回答，哪些内容是你亲自知道、哪些只是有限材料下的意见？请确认对控方有利的记录，不能无依据排除其证明力。'}
        elif action=='hire_expert':
            rid=a.get('target');option=self.world.manifest['experts'].get(rid)
            if not option:raise ProtocolError('请从公开专家选项选择')
            if rid in self.s['experts']:raise ProtocolError('已聘请该专家')
            if self.s['money_used']+option['fee']>self.world.manifest['budget']['money']:raise BudgetError('专家费用预算不足')
            self.s['money_used']+=option['fee'];self.s['experts'][rid]={'materials':[],'opinions':[]};events.append(self.ev('expert_hired',rid,'R_DEFENSE',scope='fee',fee=option['fee']))
            self.s['disclosed']=list(dict.fromkeys(self.s['disclosed']+[d for d,v in self.world.docs.items() if v['release']=='expert']))
        elif action=='ask_expert':
            rid=a.get('target');materials=a.get('materials') or []
            if rid not in self.s['experts']:raise ProtocolError('尚未聘请该专家')
            if not materials or any(d not in self.s['read_documents'] for d in materials):raise ProtocolError('只能向专家提交实际读过的材料')
            required=self.world.manifest['experts'][rid]['required_documents'];full=set(required)<=set(materials)
            if self.mock:opinion='已核对提交材料。'+('材料具备，须区分事实、推断与未知，不能证明全部控方要件，也不能无依据保证无罪。' if full else '材料不足，只能给一般意见，不能作个案确定结论。')
            else:
                allowed=[{'fact_id':f,'text':self.world.facts[f]['text']} for d in materials for f in self.world.docs[d]['fact_ids']]
                opinion=self.witness(rid,'专家材料审阅：只依据这些已交材料说明可支持和不能确认的内容；不得补入计算或数值。'+text,materials,facts_override=allowed)
            self.s['experts'][rid]['materials']=materials;self.s['experts'][rid]['opinions'].append(opinion)
            events.append(self.ev('expert_opinion',opinion,rid,materials,audience=['R_DEFENSE'],complete_materials=full));result={'opinion':opinion,'materials_complete':full}
        elif action=='offer_exhibit':
            did=a.get('target');purpose=a.get('purpose') or ''
            if did not in self.s['read_documents']:raise ProtocolError('不能提交未实际读过的文件')
            if not purpose.strip() or not text.strip():raise ProtocolError('请说明证据用途、认证、传闻例外及个人知识基础')
            basis=set(self.world.docs[did]['roles'])&set(self.s['heard_roles'])
            decision='excluded' if did in self.s['excluded_documents'] else 'admitted' if basis else 'not_admitted'
            reason='已有当庭证人提供来源基础，仍受用途限制。' if basis else '尚无相关证人到庭说明来源；保留申请，不进入陪审团。'
            if not self.mock and decision!='excluded':
                ruling=self.environment_call('你是中立法官。仅按提交文件、已到庭证言及所引法源，判断认证、传闻及用途。decision granted表示采纳，limited表示限定用途采纳，denied表示不采纳。不得因存在文件或证人就自动采纳。',{'offer':a,'document':self.world.text(did),'testimony':[e for e in self.visible_events() if e['kind']=='testimony'],'laws':{l:self.world.laws[l] for l in laws}},MOTION_SCHEMA,'R_JUDGE',effort='high')
                decision='admitted' if ruling['decision'] in ('granted','limited') else 'not_admitted';reason=ruling['reason']
                if ruling['decision']=='limited':self.s['limited_documents'][did]=reason
            events.append(self.ev('exhibit_offered',text,'R_DEFENSE',[did],purpose=purpose,decision=decision))
            events.append(self.ev('exhibit_ruling',reason,'R_JUDGE',[did],decision=decision))
            if decision=='admitted':events.append(self.ev('exhibit',self.world.text(did),'R_COURT',[did],admitted=True,purpose=purpose))
        elif action=='write_artifact':
            name=safe_name(a.get('filename',''));content=a.get('content') or ''
            if not content.strip():raise ProtocolError('文件内容为空')
            if len(name)>120:raise ProtocolError('工作文件名不得超过120字符')
            old=self.s['artifacts'].get(name);version=1+(old['version'] if old else 0)
            self.s['artifacts'][name]={'version':version,'content':content,'references':refs,'round':self.s['round'],'stage_id':self.s['stage_id'],'path':f'artifacts/{name}'}
            artifacts=[{'path':f'artifacts/{name}','text':content},{'path':f'artifacts/versions/{version:03d}/{name}','text':content}]
            events.append(self.ev('artifact_written',name,'R_DEFENSE',refs,audience=['R_DEFENSE'],filename=name,version=version,sha256=digest(content)))
        elif action=='elect_testimony':
            elected=bool(a.get('testify'))
            if not text.strip():raise ProtocolError('请记录与当事人讨论过的风险与选择理由')
            if not any('作证' in r['question'] or 'testify' in r['question'].lower() for r in self.s['client_interviews']):raise ProtocolError('须先私下讨论是否作证与风险，不能替当事人作选择')
            consent={'testify':elected,'reason':'离线模式按律师建议模拟自愿选择，仅为机制验证。'}
            if not self.mock:
                consent=self.environment_call('你扮演被告，自主决定是否作证。律师的建议不是命令；根据已经进行的私下咨询和你已开放的个人记忆选择。不得为了配合评分而选择，不编造经历；reason只说明选择及一般风险，不增加案件事实。',{'advice':text,'suggested_testify':elected,'consultations':self.s['client_interviews'],'known_facts':self.world.role_facts('R_DEFENDANT',self.s,'具体作证风险',refs)},CONSENT_SCHEMA,'R_DEFENDANT')
            elected=consent['testify'];self.s['testimony_decision']={'testify':elected,'reason':consent['reason'],'advice':text}
            events.append(self.ev('testimony_consultation',canonical(consent),'R_DEFENDANT',audience=['R_DEFENSE']))
            events.append(self.ev('testimony_election','当事人自愿选择'+('作证' if elected else '不作证')+'；具体咨询内容受私下范围保护。','R_DEFENSE',choice=elected));result=consent
        elif action=='file_motion':
            if self.s['stage_id'] in ('r2_1118','r3_1118'):mid='acquittal_1118'
            else:mid=a.get('target')
            rule=self.world.motions.get(mid)
            if not rule:raise ProtocolError('可用动议类型：'+','.join(self.world.motions))
            decision=self.motion(mid,a,rule)
            rec={'motion_id':mid,'text':text,'legal_ids':laws,'references':refs,'application':copy.deepcopy(a),'stage_id':self.s['stage_id'],'round':self.s['round'],**decision}
            self.s['motions'].append(rec);self.s['rulings'].append(rec)
            if mid=='acquittal_1118':self.apply_acquittal(decision,events)
            if decision['decision'] in ('granted','limited'):
                effect=rule['effect']
                if effect=='limit_prior':self.s['limited_documents']['D06' if self.s['case_id'].endswith('005') else 'D01']='只可用于法定基础或明确获准事项，不用于品格推断'
                if effect=='limit_inference':self.s['limited_documents']['D14']='样本检测记载，不自动等于驾驶时浓度'
                if effect=='late_relief':
                    self.s['preparation_remaining']=3;events.append(self.ev('preparation_relief','准许三个补充准备动作，庭前资源额度照常适用；完成后回到原举证阶段。','R_JUDGE'))
                if effect=='blood_ruling' and decision['decision']=='granted':self.s['excluded_documents']=list(dict.fromkeys(self.s['excluded_documents']+['D04','D12','D13','D14','D21','D23','D25']))
            events.append(self.ev('motion_filed',text,'R_DEFENSE',refs,scope=mid));events.append(self.ev('motion_ruling',decision['reason'],'R_JUDGE',decision['record_refs'],decision=decision['decision'],scope=mid));result=decision
        elif action in ('object','move_to_strike'):
            if not a.get('target'):raise ProtocolError('请指出实际事件ID')
            self.window_action(a,timely=False);return
        elif action=='request_instruction':
            if not text.strip():raise ProtocolError('请说明具体指示和依据')
            future=any(not self.world.laws[l].get('cutoff_ok') for l in laws)
            if future:events.append(self.ev('legal_error','请求引用了当时未生效的法律','R_DEFENSE',laws))
            events.append(self.ev('instruction_request',text,'R_DEFENSE',laws))
            decision={'decision':'denied' if future else 'granted','reason':'依当时法源与记录审查；未来法律不适用。','legal_ids':laws,'record_refs':[]}
            if not self.mock and not future:
                decision=self.environment_call('你是法官。按当时法律审查所请求的具体指示；错误的内容应denied，部分正确为limited且reason写出准许的指示全文。granted表示请求全文合法。不得因辩方请求就照单全收。',{'request':a,'laws':{l:self.world.laws[l] for l in laws},'charges':self.active_charges(),'record':self.jury_evidence()},MOTION_SCHEMA,'R_JUDGE',effort='high')
            if decision['decision']!='denied':self.s['approved_instructions'].append(text if decision['decision']=='granted' else decision['reason'])
            events.append(self.ev('instruction_ruling',decision['reason'],'R_JUDGE',laws,decision=decision['decision']))
        elif action in ('opening','closing'):
            if not text.strip():raise ProtocolError('陈述不能为空')
            events.append(self.ev(action,text,'R_DEFENSE',refs))
            # The opposing side receives the actual defense text for rebuttal; work notes remain private.
        elif action=='pass':
            if relief_before:self.s['preparation_remaining']=0
            else:self.s['slot']=self.stage['slots']
            charge=False;events.append(self.ev('pass','结束补充准备' if relief_before else '选择进入下一阶段','R_DEFENSE'))
        else:raise ProtocolError('动作未实现')
        if action!='pass' and not free_read:
            if relief_before:self.s['preparation_remaining']=max(0,relief_before-1)
            else:self.s['slot']+=1
        if charge:
            self.s['hours_used']+=hours;self.s['actions_used']+=1
        if result is not None:
            self.s['tool_result']=result;events.append(self.ev('tool_result',canonical(result),'trusted_tool',audience=['R_DEFENSE'],action=action))
        events.insert(0,self.ev('action',text or action,'R_DEFENSE',refs,audience=['R_DEFENSE'],action=action,target=a.get('target',''),query=a.get('query',''),purpose=a.get('purpose',''),legal_ids=laws))
        self.commit(events,artifacts)
    def review_subpoena(self,a,hits):
        purpose=a.get('purpose','').strip()
        relevant=bool(hits) and len(purpose)>=8 and not any(k in purpose.lower() for k in ('与案件无关','无关的目的','骚扰','harass','unrelated'))
        review={'decision':'granted' if relevant else 'denied','authorized_documents':hits[:] if relevant else [],'reason':'法院按请求的具体对象、争点相关性和最小范围审查；仅准许现有相关记录，其他私人材料不在命令内。' if relevant else '未证明具体相关性或范围不当，法院不准披露。','limitations':'仅限本案诉讼准备使用；未取得的其他私人资料不披露。'}
        if not self.mock:
            review=self.environment_call('你是审查第三方记录传票的中立法官。审查具体材料、案件争点、必要性、范围和隐私。purpose非空不等于合法；无关、骚扰或过宽请求须拒绝或限制。仅可在available_documents内授权，denied必须空数组；不能创造不存在文件。若申请信息不足，拒绝并说明如何具体化。',{'application':a,'charges':self.active_charges(),'available_documents':[{'doc_id':d,'title':self.world.docs[d]['title'],'topics':self.world.docs[d]['topics']} for d in hits],'legal_standard':'须具体、相关、必要并限定范围；法院审查与文件是否存在分开。','laws':{lid:law for lid,law in self.world.laws.items() if lid=='L_BARRETT_2000' and law.get('cutoff_ok')}},SUBPOENA_SCHEMA,'R_JUDGE',effort='high')
            authorized=review['authorized_documents']
            if len(authorized)!=len(set(authorized)) or not set(authorized)<=set(hits) or review['decision']=='denied' and authorized:raise EvidenceError('传票裁定授权未知/重复文件或拒绝却仍披露')
            # A purported court grant cannot override the fixed abuse/availability boundary.
            if not relevant:review={'decision':'denied','authorized_documents':[],'reason':'程序校验：未有可授权具体材料或明确无关/骚扰目的，申请不予签发。','limitations':'未披露第三方材料。'}
        return review
    def active_charges(self):
        disposed=self.s.get('charge_dispositions',{})
        return [c for c in self.world.manifest['charges'] if c['charge_id'] not in disposed]
    def apply_acquittal(self,decision,events):
        rows=decision.get('charge_decisions')
        active={c['charge_id'] for c in self.active_charges()}
        if rows is None:
            if decision['decision']=='limited':raise EvidenceError('部分无罪裁定必须列明逐罪处置')
            rows=[{'charge_id':c,'decision':decision['decision'],'reason':decision['reason']} for c in sorted(active)]
        if sorted(r['charge_id'] for r in rows)!=sorted(active) or any(r['decision'] not in ('granted','denied') for r in rows):raise EvidenceError('无罪裁定漏项、重复或引用非待审罪名')
        for row in rows:
            if row['decision']=='granted':
                self.s.setdefault('charge_dispositions',{})[row['charge_id']]={'decision':'not_guilty','source':'judgment_of_acquittal','stage_id':self.s['stage_id'],'round':self.s['round'],'reason':row['reason']}
                events.append(self.ev('charge_acquitted',row['reason'],'R_JUDGE',charge_id=row['charge_id'],decision='not_guilty',scope='acquittal_1118'))
        if not self.active_charges():
            self.s['verdict']={c['charge_id']:'not_guilty' for c in self.world.manifest['charges']}
            self.s['status']='completed';self.s['termination_reason']='judgment_of_acquittal';self.s['terminated_at_stage']=self.s['stage_id']
            self.s['pending_window']=None;self.s['pending_answer']=None;self.s['opponent_pending']=None
            events.append(self.ev('verdict',canonical(self.s['verdict']),'R_JUDGE',termination_reason='judgment_of_acquittal'))
    def motion(self,mid,a,rule):
        if mid=='acquittal_1118' and self.s['stage_id'] not in ('r2_1118','r3_1118'):return {'decision':'denied','reason':'控方举证尚未结束或不在法定申请阶段。','legal_ids':[],'record_refs':[]}
        missing=[d for d in rule.get('requires_read',[]) if d not in self.s['read_documents']]
        if missing:return {'decision':'denied','reason':'当前记录尚缺已读材料基础：'+','.join(missing),'legal_ids':[],'record_refs':[]}
        if rule.get('requires_gate') and rule['requires_gate'] not in self.s['gates']:return {'decision':'denied','reason':'材料尚未依法披露，不可使用','legal_ids':[],'record_refs':[]}
        if rule.get('requires_late_disclosure') and not any(e['kind']=='late_disclosure' for e in self.store.events):return {'decision':'denied','reason':'迟交事件尚未出现','legal_ids':[],'record_refs':[]}
        if not a.get('legal_ids'):return {'decision':'denied','reason':'缺少具体法律依据','legal_ids':[],'record_refs':[]}
        future=[l for l in a['legal_ids'] if not self.world.laws[l]['cutoff_ok']]
        if future:return {'decision':'denied','reason':'援引了晚于模拟时点的法律；不能作为当时依据','legal_ids':future,'record_refs':[]}
        if self.mock:
            dec='denied' if mid=='acquittal_1118' else 'limited' if mid in ('suppress_blood','registration_records','M06_boundary') else 'granted'
            return {'decision':dec,'reason':'离线占位：本申请驳回以覆盖后续完整流程，不代表真实法律判断。' if mid=='acquittal_1118' else '依已出示材料和具体法律限定用途并保留相关争议。','legal_ids':a['legal_ids'],'record_refs':a.get('references',[])}
        # Only the disclosed/read record is sent. No full world, outcome reference or rubric.
        payload={'motion':a,'rule_scope':rule['title'],'record':self.court_events(),'documents':{d:self.world.text(d) for d in a.get('references',[]) if d in self.s['read_documents']},'laws':{l:self.world.laws[l] for l in a['legal_ids']},'round':self.s['round'],'closed_r1_record':self.s['closed_records'].get('r1_ruling',[]),'active_charges':self.active_charges()}
        out=self.environment_call('你是中立模拟法官。根据实际记录与当时法源作granted/denied/limited裁定，说明法律和事实依据。M06等后来披露信息不得倒用于第一轮。正确引用编号不自动证明法律适用。无罪判决申请必须按每个active_charges列charge_decisions(granted为立即该罪无罪，denied为继续审理)，不是仅保留申请；记录不支持就驳回；不得按预期胜负作裁定。record_refs只可列出输入记录或已读材料。',payload,ACQUITTAL_SCHEMA if mid=='acquittal_1118' else MOTION_SCHEMA,'R_JUDGE',effort='high')
        if mid=='acquittal_1118':
            active={c['charge_id'] for c in self.active_charges()}
            rows=out.get('charge_decisions',[])
            if sorted(r['charge_id'] for r in rows)!=sorted(active):raise EvidenceError('无罪裁定必须且只能处理每个待审罪名一次')
            grants=sum(r['decision']=='granted' for r in rows)
            out['decision']='granted' if grants==len(rows) else 'limited' if grants else 'denied'
        allowed={e['event_id'] for e in self.visible_events()}|set(self.s['read_documents'])
        if any(x not in allowed for x in out['record_refs']) or any(l not in a['legal_ids'] for l in out['legal_ids']):raise EvidenceError('法官引用了不可见材料')
        return out
    def window_action(self,a,timely=None):
        target=a.get('target') or self.s['pending_window'];event=next((e for e in self.store.events if e['event_id']==target),None)
        if not event or 'R_DEFENSE' not in event['audience'] or 'R_JUDGE' not in event['audience']:raise ProtocolError('异议目标不是可见的既有法庭语句')
        if self.s['pending_window'] and target!=self.s['pending_window']:raise ProtocolError('实时窗口只能针对当前语句；其他异议请在正常动作中提出')
        in_window=bool(self.s['pending_window'] and target==self.s['pending_window'])
        timely=in_window if timely is None else bool(timely and in_window)
        distance=len(self.store.events)-next(i for i,e in enumerate(self.store.events) if e['event_id']==target)-1
        action=a.get('action');events=[]
        if action=='pass':events=[self.ev('window_pass','未提出异议','R_DEFENSE',[target])]
        else:
            ground=a.get('ground') or '';why=a.get('text') or ''
            if ground not in GROUNDS or not why.strip():raise ProtocolError('异议须给具体理由和法律类别')
            defect=classify(event['text'],event.get('meta',{}));correct=ground in defect
            if not self.mock:
                ruling=self.environment_call('你是中立法官，审查针对实际出现语句的具体异议及补救。仅按语句、询问类型、法源及当庭记录判断；不要按类别名称一律准许，也不要假定有窗口即有错误。granted或limited为准许，denied为驳回。说明删除、限制或重新提问的范围。',{'target':{k:v for k,v in event.items() if k not in ('meta','hash','prev_hash')},'examination_mode':event['meta'].get('examination_mode'),'objection':a,'timely':timely,'event_distance':distance,'record':self.court_events()[-12:],'laws':{l:self.world.laws[l] for l in self.s['law_reads']}},MOTION_SCHEMA,'R_JUDGE',effort='high')
                correct=ruling['decision'] in ('granted','limited')
            decision='sustained' if correct else 'overruled'
            self.s['objections'].append({'target_event_id':target,'ground':ground,'reason':why,'decision':decision,'stage_id':self.s['stage_id'],'round':self.s['round'],'window':timely,'timely':timely,'window_event_id':target if timely else None,'event_distance':distance})
            if correct:
                if timely and event['kind']=='question' and self.s['pending_answer']:self.s['pending_answer']['skip']=True
                if action=='move_to_strike' or event['kind'] in ('testimony','trap_statement'):self.s['stricken']=list(dict.fromkeys(self.s['stricken']+[target]))
                else:self.s['admonished']=list(dict.fromkeys(self.s['admonished']+[target]))
            ruling_text=ruling['reason'] if not self.mock else '理由与实际语句相符，准许并执行删除或限制。' if correct else '所给理由未能对应被指向的实际语句。'
            events=[self.ev('objection',why,'R_DEFENSE',[target],ground=ground,action=action,timely=timely,event_distance=distance),self.ev('objection_ruling',ruling_text,'R_JUDGE',[target],decision=decision,ground=ground)]
            if correct and action=='request_instruction':events.append(self.ev('curative_instruction','陪审团不得将被指出的不当陈述作为证据或不利推断。','R_JUDGE',[target]))
        if timely:self.s['pending_window']=None
        else:self.s['slot']+=1
        self.commit(events)
    def close_record(self):
        self.s['closed_records'][self.s['stage_id']]=[e['event_id'] for e in self.store.events if e['round']==self.s['round']]
        if self.s['stage_id']=='r1_ruling':
            events=[]
            for old in [m for m in self.s['motions'] if m['round']==1]:
                rule=self.world.motions[old['motion_id']]
                out=self.motion(old['motion_id'],old.get('application',old),rule);self.s['rulings'].append({**old,**out,'after_hearing':True})
                if rule['effect']=='blood_ruling':
                    blood=['D04','D12','D13','D14','D21','D23','D25']
                    self.s['excluded_documents']=[d for d in self.s['excluded_documents'] if d not in blood]
                    if out['decision']=='granted':self.s['excluded_documents']=list(dict.fromkeys(self.s['excluded_documents']+blood))
                events.append(self.ev('hearing_final_ruling',out['reason'],'R_JUDGE',out['record_refs'],decision=out['decision'],scope=old['motion_id']))
            self.commit(events)
            self.s['closed_records'][self.s['stage_id']]=[e['event_id'] for e in self.store.events if e['round']==self.s['round']]
    def advance(self):
        i=self.s['stage_index']+1
        if i>=len(self.world.stages):raise RunError('缺少终局裁决')
        stage=self.world.stages[i];self.s.update({'stage_index':i,'stage_id':stage['stage_id'],'round':stage['round'],'slot':0,'script':None,'script_cursor':0,'pending_window':None,'pending_answer':None})
        self.commit([])
    def jury_evidence(self):
        out=[]
        for e in self.store.events:
            if e['round']<2 or e['event_id'] in self.s['stricken']:continue
            admitted=e['kind']=='exhibit' or e['kind']=='testimony' and e['meta'].get('admitted')
            if not admitted:continue
            units=e['meta'].get('claim_units') or [{'text':e['text'],'fact_ids':e['references']}]
            for unit in units:
                refs=unit['fact_ids'];docs=set(r for r in refs if r in self.world.docs)
                for f in refs:
                    if f in self.world.facts:docs.update(self.world.facts[f].get('doc_ids',[]))
                if docs&set(self.s['excluded_documents']):continue
                purpose=[self.s['limited_documents'][d] for d in docs if d in self.s['limited_documents']]
                out.append({'event_id':e['event_id'],'text':unit['text'],'references':refs,'limitations':purpose,'source_type':e['kind']})
        return out
    def deliberate(self):
        evidence=self.jury_evidence();charges=self.active_charges()
        if not charges:raise EvidenceError('全部指控已经司法处置，不得再次提交陪审团')
        args=[{'actor':e['actor'],'text':e['text'],'admonished':e['event_id'] in self.s['admonished']} for e in self.store.events if e['kind'] in ('closing','closing_rebuttal') and e['event_id'] not in self.s['stricken']]
        instructions=['被告推定无罪，控方须对每项要件排除合理怀疑。','律师陈述、提问和准备文件不是证据；不得以被告沉默推断有罪。','删除内容不可使用；限定用途证据只能按限定范围使用。']
        instructions += [self.world.laws['L_PEN_1096']['text']]
        supplied=read_json(self.root/'world/jury_instructions.json',{})
        active_ids={c['charge_id'] for c in charges}
        instructions += [i['text'] for i in supplied.get('instructions',[]) if not i.get('charge_ids') or active_ids&set(i['charge_ids'])]
        instructions += self.s['approved_instructions']
        if not self.s['jurors']:
            self.commit([self.ev('jury_instruction','\n'.join(instructions),'R_JUDGE')]);return self.deliberate_votes(evidence,charges,args,instructions)
        return self.deliberate_votes(evidence,charges,args,instructions)
    def deliberate_votes(self,evidence,charges,args,instructions):
        for i in range(len(self.s['jurors']),6):
            if self.mock:
                # Only for explicitly labelled offline calibration; not a prediction.
                meaningful=len(self.s['motions'])+len(self.s['questions'])+sum(len(x['opinions']) for x in self.s['experts'].values())
                decision='not_guilty' if meaningful>=5 else 'guilty'
                vote={'votes':[{'charge_id':c['charge_id'],'decision':decision,'reason':'离线规则校准占位；不作为真实模型判决'} for c in charges]}
            else:
                payload={'juror':i+1,'charges':charges,'admitted_evidence':evidence,'instructions':instructions,'arguments_not_evidence':args,'limitations':self.s['limited_documents']}
                vote=self.environment_call('你是独立陪审员。只依据准入证据和法官指示逐项评议，不看隐藏事实、rubric或技术强度分。律师陈词不是证据，沉默不能作不利依据。每个charge_id必须且只能投一票。',payload,JUROR_SCHEMA,f'R_JUROR_{i+1}')
                if sorted(x['charge_id'] for x in vote['votes'])!=sorted(c['charge_id'] for c in charges):raise EvidenceError('陪审员漏投或重复指控；暂停恢复，不能默认无罪')
            self.s['jurors'].append({'juror':i+1,**vote});self.commit([self.ev('jury_vote',canonical(vote),f'R_JUROR_{i+1}',audience=['R_JUDGE'])])
        decisions={cid:row['decision'] for cid,row in self.s.get('charge_dispositions',{}).items()}
        for c in charges:
            choices=[v['decision'] for j in self.s['jurors'] for v in j['votes'] if v['charge_id']==c['charge_id']]
            decisions[c['charge_id']]='guilty' if all(x=='guilty' for x in choices) else 'not_guilty' if all(x=='not_guilty' for x in choices) else 'hung'
        self.s['verdict']=decisions;self.s['status']='completed';self.s['stage_id']='ended';self.s['round']=3;self.s['termination_reason']='jury_verdict'
        self.commit([self.ev('verdict',canonical(decisions),'R_JURY')])
        atomic(self.run/'jury_record.json',{'admitted_evidence':evidence,'instructions':instructions,'arguments_not_evidence':args,'votes':self.s['jurors'],'verdict':decisions,'mock':self.mock})
    def finish_outputs(self):
        self.store.verify()
        disk=Store(self.run,self.world.initial())
        if disk.state['last_hash']!=self.s['last_hash'] or disk.state['status']!=self.s['status'] or len(disk.events)!=len(self.store.events):raise EvidenceError('完成标记与持久化状态不一致，不能输出成功结果')
        if self.s['status']=='completed':
            judicial=self.s.get('termination_reason')=='judgment_of_acquittal'
            if not self.s['verdict'] or set(self.s['verdict'])!={c['charge_id'] for c in self.world.manifest['charges']}:raise EvidenceError('终局裁决未完整处理全部指控')
            if judicial:
                if self.active_charges() or self.s['jurors']:raise EvidenceError('司法终局与待审罪名/陪审票不一致')
            elif len(self.s['jurors'])!=6:raise EvidenceError('陪审终局缺少六票')
            instructions=next((e['text'].split('\n') for e in reversed(self.store.events) if e['kind']=='jury_instruction'),[])
            args=[{'actor':e['actor'],'text':e['text'],'admonished':e['event_id'] in self.s['admonished']} for e in self.store.events if e['kind'] in ('closing','closing_rebuttal') and e['event_id'] not in self.s['stricken']]
            atomic(self.run/'jury_record.json',{'admitted_evidence':self.jury_evidence(),'instructions':instructions,'arguments_not_evidence':args,'votes':self.s['jurors'],'verdict':self.s['verdict'],'judicial_dispositions':self.s.get('charge_dispositions',{}),'termination_reason':self.s.get('termination_reason'),'mock':self.mock})
        summary={'case_id':self.s['case_id'],'status':self.s['status'],'stage':self.s['stage_id'],'simulation_round_count':3,'candidate':self.candidate,'profile':self.profile,'mock':self.mock,
                 'verdict':self.s['verdict'],'termination_reason':self.s.get('termination_reason'),'terminated_at_stage':self.s.get('terminated_at_stage'),'completed_rounds':max([0]+[e['round'] for e in self.store.events]),'reached_stages':self.s.get('reached_stages',[]),'judicial_dispositions':self.s.get('charge_dispositions',{}),'hours_used':self.s['hours_used'],'actions':self.s['actions_used'],'money_used':self.s['money_used'],'event_count':len(self.store.events),'read_documents':self.s['read_documents'],'artifacts':list(self.s['artifacts']),'journal_ok':True,'config_fingerprint':self.config_fingerprint}
        atomic(self.run/'summary.json',summary)
        if not (self.run/'usage.json').exists():atomic(self.run/'usage.json',{'calls':0,'total_tokens':0,'mock':self.mock})
        return summary
    def run_all(self,max_steps=3000):
        for _ in range(max_steps):
            if self.s['status']=='completed':break
            if (self.root/'STOP_REQUESTED.json').exists():raise StopRequested('收到安全停止请求；当前事务已落盘，可恢复')
            try:self.step()
            except ProtocolError as ex:
                self.s=copy.deepcopy(self.store.state);self.s['protocol_errors']+=1
                self.s['tool_result']={'error':redact(ex),'budget_charged':False}
                self.commit([self.ev('action_rejected',redact(ex),audience=['R_DEFENSE'])])
                if self.s['protocol_errors']>=8:raise ProtocolError('累计协议错误达到上限；暂停并保留轨迹，不自动结束阶段') from ex
            except BudgetError as ex:
                if str(ex).startswith('庭前'):
                    self.s=copy.deepcopy(self.store.state);self.s['tool_result']={'error':str(ex),'budget_charged':False}
                    if self.s['preparation_remaining']:self.s['preparation_remaining']=0
                    else:self.s['slot']=self.stage['slots']
                    self.commit([self.ev('resource_exhausted',str(ex),audience=['R_DEFENSE'])]);continue
                raise
        if self.s['status']!='completed':raise RunError('安全循环上限达到；未伪造完成')
        return self.finish_outputs()

def words(t):
    t=t.lower();latin=re.findall(r'[a-z0-9_]+',t);cn=re.findall(r'[\u4e00-\u9fff]{2,}',t)
    return list(dict.fromkeys(latin+[x[i:i+2] for x in cn for i in range(len(x)-1)]))
def topic_match(q,topics):
    q=q.lower();aliases={'intake':['接收','签名','告知'],'notice':['告知','签名'],'registry':['登记','查询'],'window':['窗口','接待'],'appointment':['预约'],'home_visit':['家访'],'residence':['住所','住宿'],'shelter':['救助站'],'motel':['旅馆'],'threat':['威胁','绑架'],'cousin':['亲属'],'jurisdiction':['辖区','地图'],'records':['记录'],'pas':['pas','呼气','筛查'],'device':['设备','维护'],'calibration':['校准'],'qualification':['资质','培训'],'blood':['血样','采血','抽血'],'chain':['交接','监管链','流转'],'lab':['实验室','检测','质控'],'quality':['质控'],'timeline':['时间','时点','校时'],'medical':['医疗','护理'],'collision':['碰撞','事故'],'injury':['伤害'],'scene':['现场'],'followup':['补充','后续'],'foundation':['基础'],'custodian':['保管']}
    return any(t.lower() in q or any(x in q for x in aliases.get(t,[])) for t in topics)
def classify(text,meta=None):
    low=text.lower();g=[]
    if re.search(r'这种人.{0,12}(犯罪|有罪)|前科.{0,18}(肯定|所以)|bad person|criminal.{0,10}therefore',low):g+=['character','relevance']
    if re.search(r'个人信誉|我.{0,8}(保证|担保)|personal.{0,12}(guarantee|belief)',low):g+=['vouching']
    if re.search(r'(没有作证|保持沉默|did not testify).{0,25}(说明|有罪|清白|guilt)',low):g+=['silence']
    if re.search(r'匿名.{0,10}(告诉|来电|说)|anonymous.{0,20}(told|said)',low):g+=['hearsay','foundation']
    if re.search(r'任何凭空|完全不可能有罪|any imaginary doubt',low):g+=['misstates_law']
    if re.search(r'辩方必须证明.{0,5}清白|prove.{0,8}innocen',low):g+=['burden','misstates_law']
    if re.search(r'才披露.{0,20}第一轮|倒.{0,8}第一轮|earlier hearing.{0,10}later',low):g+=['record_boundary','relevance']
    if re.search(r'显然就是故意犯罪|obviously.{0,10}guilty',low):g+=['foundation','speculation']
    if (meta or {}).get('examination_mode')=='direct' and re.search(r'对不对|对吗|isn.t it|correct\?',low):g+=['leading']
    return list(set(g))
