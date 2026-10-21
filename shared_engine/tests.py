"""Offline regression tests; no network, credentials or candidate calls."""
from __future__ import annotations
import ast,copy,json,tempfile,unittest,urllib.error
from pathlib import Path
from .engine import *
from .llm import Client,Ledger,APIError,validate_schema
from .scoring import applicable,grade,chunks
from .freeze import preregister,verify_formal,content_manifest
from .policy import action_for

ROOT=Path(__file__).resolve().parents[1]

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.run=Path(self.tmp.name)/'run';self.case=ROOT/'006_Wilson'
        self.e=Engine(self.case,self.run,profile='reference')
    def tearDown(self):self.tmp.cleanup()
    def phase(self,sid):
        i=next(i for i,s in enumerate(self.e.world.stages) if s['stage_id']==sid)
        self.e.s.update({'stage_index':i,'stage_id':sid,'round':self.e.world.stages[i]['round'],'slot':0,'script':None,'pending_window':None,'pending_answer':None});self.e.commit([]);self.e.enter()
    def read(self,did):self.e.s['disclosed']=list(dict.fromkeys(self.e.s['disclosed']+[did]));self.e.commit([]);self.e.apply({'action':'read_document','target':did})
    def test_world_counts_initial_ratio_and_provenance(self):
        for c in ('005_Williams','006_Wilson'):
            w=World(ROOT/c/'world');self.assertTrue(150<=len(w.facts)<=300);self.assertTrue(20<=len(w.docs)<=40);self.assertLessEqual(len(w.manifest['initial_documents'])/len(w.docs),.3)
            for f in w.facts.values():
                self.assertIn(f['origin'],('anchor','authored'))
                if f['origin']=='anchor':self.assertTrue(f['source_locators'])
    def test_m06_blocked_all_roles_before_gate_even_round2(self):
        self.e.s['round']=2
        for rid in self.e.world.roles:
            facts=self.e.world.role_facts(rid,self.e.s,'请具体说明三次嘴唇情况',['D02'])
            self.assertFalse(any('三次' in f['text'] or '嘴唇' in f['text'] for f in facts),rid)
    def test_m06_exact_release(self):
        self.phase('r2_opening');self.assertNotIn('D22',self.e.s['disclosed'])
        self.phase('r2_examination');self.assertIn('M06',self.e.s['gates']);self.assertIn('D22',self.e.s['disclosed'])
        gate=next(e for e in self.e.store.events if e['kind']=='stage_release');self.assertEqual(gate['stage_id'],'r2_examination')
        self.read('D22');self.assertTrue(any(f['fact_id']=='A010' for f in self.e.world.role_facts('R_OFFICER',self.e.s,'具体嘴唇',['D22'])))
    def test_hidden_read_rejected_no_resource_or_slot(self):
        before=copy.deepcopy(self.e.store.state)
        with self.assertRaises(ProtocolError):self.e.apply({'action':'read_document','target':'D22'})
        self.assertEqual(self.e.store.state,before)
    def test_unknown_action_rejected_no_commit(self):
        with self.assertRaises(ProtocolError):self.e.apply({'action':'hack_world'})
        self.assertEqual(self.e.s['slot'],0);self.assertFalse(self.e.store.events)
    def test_budget_checked_before_read_side_effect(self):
        self.e.s['hours_used']=36;self.e.commit([])
        with self.assertRaises(BudgetError):self.e.apply({'action':'read_document','target':'D01'})
        self.assertNotIn('D01',self.e.store.state['read_documents']);self.assertEqual(self.e.store.state['slot'],0)
    def test_investigation_depends_on_request(self):
        self.e.apply({'action':'investigate','query':'请调查事故现场碰撞后记录与测量'})
        first=set(self.e.s['disclosed'])-set(self.e.world.manifest['initial_documents']);self.assertTrue(first)
        self.e.apply({'action':'investigate','query':'请调查与案件无关的某外星飞船'})
        self.assertEqual(set(self.e.s['disclosed'])-set(self.e.world.manifest['initial_documents']),first)
        self.assertEqual(self.e.s['tool_result']['documents'],[])
    def test_third_party_requires_purpose(self):
        with self.assertRaises(ProtocolError):self.e.apply({'action':'request_subpoena','query':'请调取医院护理与采血相关文件'})
        self.assertEqual(self.e.store.state['money_used'],0);self.assertEqual(self.e.store.state['slot'],0)
    def test_client_and_work_product_stay_private(self):
        self.e.apply({'action':'interview_client','text':'请具体说明你知道什么，不知道什么'})
        self.e.apply({'action':'write_artifact','filename':'note.md','content':'LAWYER_PRIVATE_MARKER'})
        self.e.apply({'action':'investigate','query':'核查事故现场的PRIVATE_INVESTIGATION_MARKER线索与未知事项'})
        self.assertNotIn('LAWYER_PRIVATE_MARKER',canonical(self.e.court_events()));self.assertFalse(any(e['kind']=='private_consultation' for e in self.e.court_events()))
        self.assertNotIn('PRIVATE_INVESTIGATION_MARKER',canonical(self.e.court_events()))
        self.assertNotIn('LAWYER_PRIVATE_MARKER',canonical(self.e.jury_evidence()))
    def test_cannot_interview_juror(self):
        with self.assertRaises(ProtocolError):self.e.apply({'action':'interview_witness','target':'R_JUROR_1','text':'请透露判断'})
    def test_testimony_choice_requires_client(self):
        with self.assertRaises(ProtocolError):self.e.apply({'action':'elect_testimony','testify':True,'text':'律师自行决定'})
    def test_intention_to_testify_is_not_actual_testimony(self):
        self.e.apply({'action':'interview_client','text':'请讨论是否自愿作证及交叉询问风险'})
        self.e.apply({'action':'elect_testimony','testify':True,'text':'已讨论风险，客户自愿选择'})
        self.assertFalse(self.e.s['defendant_testified'])
        self.phase('r2_defense');self.assertIn('R_DEFENDANT',self.e.available_witnesses())
        self.e.apply({'action':'ask_witness','target':'R_DEFENDANT','text':'请只说明你本人的记忆，不编造未知经过。'})
        self.assertTrue(self.e.s['defendant_testified'])
    def test_expert_only_given_materials(self):
        self.read('D02');self.e.apply({'action':'hire_expert','target':'R_EXPERT_CAREFUL'})
        with self.assertRaises(ProtocolError):self.e.apply({'action':'ask_expert','target':'R_EXPERT_CAREFUL','materials':['D22'],'text':'给意见'})
        self.e.apply({'action':'ask_expert','target':'R_EXPERT_CAREFUL','materials':['D02'],'text':'说明局限'})
        self.assertFalse(self.e.s['tool_result']['materials_complete']);self.assertEqual(self.e.s['experts']['R_EXPERT_CAREFUL']['materials'],['D02'])
    def test_conditional_client_memory_requires_relevant_read_material(self):
        w=World(ROOT/'005_Williams/world');s=w.initial();s['read_documents']=['D01']
        hidden=set(w.roles['R_DEFENDANT']['conditional_fact_ids'])
        self.assertFalse(hidden&{f['fact_id'] for f in w.role_facts('R_DEFENDANT',s,'请具体承认签名',['D01'])})
        s['read_documents']+=['D10','D21','D06']
        self.assertTrue(hidden&{f['fact_id'] for f in w.role_facts('R_DEFENDANT',s,'请具体承认签名',['D10','D21','D06'])})
    def test_fact_validator_blocks_invented_fact_id(self):
        class Fake:
            def call(self,*args,**kwargs):return {'claims':[{'fact_ids':['F_INVENTED'],'text':'凭空事实'}],'unknown':False,'answer':'凭空事实'}
        self.e.mock=False;self.e.clients['environment']=Fake()
        with self.assertRaises(EvidenceError):self.e.witness('R_OFFICER','请说明现场',[])
    def test_semantic_false_claim_is_blocked(self):
        fid=self.e.world.roles['R_OFFICER']['fact_ids'][0]
        class Fake:
            def call(self,inst,payload,schema,*args,**kw):
                if schema==VERIFY_SCHEMA:return {'supported':False,'reason':'新增事实'}
                return {'claims':[{'fact_ids':[fid],'text':'警员亲眼看到全部事故，精确延迟199分钟'}],'unknown':False,'answer':''}
        self.e.mock=False;self.e.clients['environment']=Fake()
        with self.assertRaises(EvidenceError):self.e.witness('R_OFFICER','具体现场',[])
    def test_fact_validator_gets_speaker_and_original_alias_mapping(self):
        fid=self.e.world.roles['R_OFFICER']['fact_ids'][0];seen=[]
        class Fake:
            def call(self,inst,payload,schema,*a,**kw):
                if schema==VERIFY_SCHEMA:seen.append(payload);return {'supported':True,'reason':'注入测试，只验证身份映射上下文'}
                return {'claims':[{'fact_ids':[fid],'text':'我只说明获授权事实，不新增细节。'}],'unknown':False,'answer':''}
        self.e.mock=False;self.e.clients['environment']=Fake();self.e.witness('R_OFFICER','具体说明',[])
        self.assertEqual(seen[0]['speaker'],self.e.world.roles['R_OFFICER']['display_name']);self.assertEqual(seen[0]['identity_aliases']['A'],'Lucas Arden')
    def test_regular_question_also_gets_free_window(self):
        self.phase('r1_examination');self.e.step();before=(self.e.s['slot'],self.e.s['hours_used'],self.e.s['actions_used'])
        self.assertIsNotNone(self.e.s['pending_window']);self.assertIn('window',self.e.view())
        self.e.apply({'action':'pass'},window=True);self.assertEqual(before,(self.e.s['slot'],self.e.s['hours_used'],self.e.s['actions_used']))
        self.e.step();self.assertIsNotNone(self.e.s['pending_window'])
    def test_wrong_objection_not_automatically_sustained(self):
        self.phase('r1_examination');self.e.run_script_item({'type':'prosecution','text':'控方须证明每一项要件。'})
        target=self.e.s['pending_window'];self.e.apply({'action':'object','target':target,'ground':'silence','text':'评论沉默'},window=True)
        self.assertEqual(self.e.s['objections'][-1]['decision'],'overruled');self.assertNotIn(target,self.e.s['stricken'])
    def test_future_statement_cannot_be_objected_to(self):
        self.phase('r1_examination');self.e.step()
        with self.assertRaises(ProtocolError):self.e.apply({'action':'object','target':'E999999','ground':'silence','text':'预先知道的未来台词'},window=True)
    def test_correct_objection_strikes_actual_text(self):
        self.phase('r2_examination');self.e.run_script_item({'type':'prosecution','text':'匿名来电者告诉我，被告肯定有罪。'})
        target=self.e.s['pending_window'];self.e.apply({'action':'move_to_strike','target':target,'ground':'hearsay','text':'匿名转述无个人知识基础，请删除'},window=True)
        self.assertIn(target,self.e.s['stricken'])
    def test_disclosed_not_read_not_offered_not_admitted(self):
        self.phase('r2_examination')
        with self.assertRaises(ProtocolError):self.e.apply({'action':'offer_exhibit','target':'D01','text':'认证','purpose':'实体'})
        self.read('D01');self.e.apply({'action':'offer_exhibit','target':'D01','text':'请认证并限用途','purpose':'实体'})
        self.assertFalse(self.e.jury_evidence());self.assertTrue(any(e['kind']=='exhibit_ruling' and e['meta']['decision']=='not_admitted' for e in self.e.store.events))
    def test_round2_evidence_persists_and_round1_does_not(self):
        self.phase('r1_examination');self.e.commit([self.e.ev('testimony','R1_ONLY',references=['D02-F01'],admitted=False)])
        self.phase('r2_examination');self.e.commit([self.e.ev('testimony','R2_PERSISTS',references=['D02-F01'],admitted=True)])
        self.phase('r3_supplement');text=canonical(self.e.jury_evidence());self.assertIn('R2_PERSISTS',text);self.assertNotIn('R1_ONLY',text)
    def test_excluded_anchor_and_authored_blood_claims_filtered_atomically(self):
        self.phase('r2_examination');self.e.s['excluded_documents']=['D14']
        self.e.commit([self.e.ev('testimony','mixture',references=['A008','D02-F01'],admitted=True,claim_units=[{'fact_ids':['A008'],'text':'BLOOD_BLOCK'},{'fact_ids':['D02-F01'],'text':'EYES_KEEP'}])])
        result=canonical(self.e.jury_evidence());self.assertNotIn('BLOOD_BLOCK',result);self.assertIn('EYES_KEEP',result)
    def test_limited_use_reaches_jury(self):
        self.phase('r2_examination');self.e.s['limited_documents']['D02']='只看观察，不能推断全案'
        self.e.commit([self.e.ev('testimony','观察',references=['D02-F01'],admitted=True)])
        self.assertIn('只看观察，不能推断全案',canonical(self.e.jury_evidence()))
    def test_late_relief_creates_real_preparation_and_returns(self):
        self.phase('r2_examination');self.e.apply({'action':'file_motion','target':'late_relief','text':'核查准备与交叉询问受影响','legal_ids':['L_EVID_352']})
        self.assertEqual(self.e.s['preparation_remaining'],3);before=self.e.s['slot'];self.e.apply({'action':'pass'})
        self.assertEqual(self.e.s['preparation_remaining'],0);self.assertEqual(self.e.s['slot'],before)
    def test_round_interval_has_new_disclosure(self):
        for c in ('005_Williams','006_Wilson'):
            w=World(ROOT/c/'world');stage=next(s for s in w.stages if s['stage_id']=='between_rounds_1');self.assertTrue(stage['release_documents'])
    def test_between_rounds_material_cannot_leak_in_first_hearing(self):
        for cname in ('005_Williams','006_Wilson'):
            w=World(ROOT/cname/'world');s=w.initial();s['round']=1
            late=set(w.docs['D23']['fact_ids'])
            for rid in w.roles:self.assertFalse(late&{f['fact_id'] for f in w.role_facts(rid,s,'具体说明',['D23'])})
            s['gates'].append(w.docs['D23']['gate']);self.assertTrue(w.active(w.facts[next(iter(late))],s))
    def test_1118_after_people_before_defense(self):
        ids=[s['stage_id'] for s in self.e.world.stages];self.assertLess(ids.index('r2_examination'),ids.index('r2_1118'));self.assertLess(ids.index('r2_1118'),ids.index('r2_defense'))
    def test_artifact_path_traversal_rejected(self):
        with self.assertRaises(ProtocolError):self.e.apply({'action':'write_artifact','filename':'../outside','content':'x'})
        self.assertFalse((Path(self.tmp.name)/'outside').exists())
    def test_resume_and_hash_tamper(self):
        self.read('D01');e=Engine(self.case,self.run,profile='reference');self.assertEqual(e.s['read_documents'],['D01']);self.assertTrue(e.store.verify())
        rows=jsonl(self.run/'author_events.jsonl');rows[0]['text']='tampered';(self.run/'author_events.jsonl').write_text('\n'.join(canonical(x) for x in rows)+'\n')
        with self.assertRaises(EvidenceError):Engine(self.case,self.run,profile='reference')
    def test_changed_config_cannot_resume(self):
        with self.assertRaises(EvidenceError):Engine(self.case,self.run,profile='weak')
    def test_wal_recovery(self):
        self.read('D01');state=read_json(self.run/'state.json');atomic(self.run/'transaction.json',{'state':state,'events':[],'artifacts':[{'path':'artifacts/recovered.md','text':'committed'}]})
        e=Engine(self.case,self.run,profile='reference');self.assertEqual((self.run/'artifacts/recovered.md').read_text(),'committed');self.assertTrue(e.store.verify())
    def test_wal_will_not_append_across_missing_events(self):
        self.read('D01');state=read_json(self.run/'state.json');events=jsonl(self.run/'author_events.jsonl')
        fake=copy.deepcopy(events[-1]);fake['seq']+=5
        atomic(self.run/'transaction.json',{'state':state,'events':[fake],'artifacts':[]})
        with self.assertRaises(EvidenceError):Engine(self.case,self.run,profile='reference')
        self.assertTrue((self.run/'transaction.json').exists())
    def test_atomic_journal_recovery_rejects_changed_prefix(self):
        self.read('D01');state=read_json(self.run/'state.json');events=jsonl(self.run/'author_events.jsonl')
        atomic(self.run/'transaction.json',{'state':state,'events':[],'journal':events,'artifacts':[]})
        changed=copy.deepcopy(events);changed[0]['text']='changed_prefix';(self.run/'author_events.jsonl').write_text(''.join(canonical(e)+'\n' for e in changed))
        with self.assertRaises(EvidenceError):Engine(self.case,self.run,profile='reference')
    def test_superseded_wal_does_not_roll_back_later_checkpoint(self):
        self.read('D01');old_state=read_json(self.run/'state.json');old_events=jsonl(self.run/'author_events.jsonl')
        self.read('D02');current=read_json(self.run/'state.json')
        atomic(self.run/'transaction.json',{'state':old_state,'events':[],'journal':old_events,'artifacts':[]})
        e=Engine(self.case,self.run,profile='reference');self.assertEqual(e.s,current);self.assertNotIn('transaction.json',[p.name for p in self.run.iterdir()])
    def test_committed_artifact_materialized_view_can_be_recovered(self):
        self.e.apply({'action':'write_artifact','filename':'note.md','content':'已提交的工作稿'})
        (self.run/'artifacts/note.md').unlink();e=Engine(self.case,self.run,profile='reference')
        self.assertEqual((self.run/'artifacts/note.md').read_text(),'已提交的工作稿');self.assertTrue(e.store.verify())
    def test_csv_crlf_is_preserved_during_integrity_check(self):
        text='source,status\r\nD01,read\r\n';self.e.apply({'action':'write_artifact','filename':'data.csv','content':text})
        e=Engine(self.case,self.run,profile='reference');self.assertEqual((self.run/'artifacts/data.csv').read_bytes(),text.encode());self.assertTrue(e.store.verify())
    def test_jury_failure_never_defaults_to_acquittal(self):
        class Failing:
            def call(self,*a,**kw):raise APIError('offline injected fault')
        self.phase('r3_verdict');self.e.mock=False;self.e.clients['environment']=Failing()
        with self.assertRaises(APIError):self.e.deliberate()
        self.assertIsNone(self.e.store.state['verdict']);self.assertNotEqual(self.e.store.state['status'],'completed')
    def test_inconsistent_jurors_produce_hung(self):
        class Votes:
            def call(self,inst,payload,*a,**kw):return {'votes':[{'charge_id':c['charge_id'],'decision':'guilty' if payload['juror']%2 else 'not_guilty','reason':'独立票'} for c in payload['charges']]}
        self.phase('r3_verdict');self.e.mock=False;self.e.clients['environment']=Votes();self.e.deliberate();self.assertTrue(all(v=='hung' for v in self.e.s['verdict'].values()));self.assertEqual(len(self.e.s['jurors']),6)
    def test_reference_policy_cannot_read_hidden_files(self):
        source=(ROOT/'shared_engine/policy.py').read_text();tree=ast.parse(source)
        self.assertFalse(any(isinstance(n,ast.Name) and n.id=='World' for n in ast.walk(tree)));self.assertNotIn('open(',source);self.assertNotIn('read_text(',source)
        self.assertFalse(any(isinstance(n,ast.Import) and any(a.name in ('os','pathlib') for a in n.names) for n in ast.walk(tree)))
    def test_opportunity_na_and_denominators(self):
        item={'opportunity':{'type':'event','trap_id':'T_GRIFFIN'}};self.assertFalse(applicable(item,[]));self.assertTrue(applicable(item,[{'meta':{'trap_id':'T_GRIFFIN'}}]))
    def test_offline_cannot_open_live_freeze(self):
        c=Path(self.tmp.name)/'unfrozen_case';(c/'world').mkdir(parents=True);(c.parent/'config.json').write_text('{}')
        preregister(c)
        with self.assertRaises(EvidenceError):verify_formal(c)
    def test_quality_grading_covers_tail_and_deduplicates_severe_errors(self):
        from unittest.mock import patch
        from . import scoring
        e=self.e;e.run_all();e.commit([e.ev('review_test_tail','TAIL_EVIDENCE_MARKER',audience=['R_DEFENSE'])]);e.finish_outputs()
        criteria=[i for i in read_json(self.case/'rubric/rubric.json')['criteria'] if i['method']=='dual_model'];seen=[]
        class Grader:
            def __init__(self,*a,**kw):pass
            def call(self,inst,payload,schema,*a,**kw):
                seen.append(canonical(payload))
                if schema==scoring.PART_SCHEMA:return {'items':[{'id':i['id'],'evidence':['E000001'],'assessment':'注入评分器，仅检验记录覆盖与数据流程'} for i in criteria]}
                error={'category':'fabrication','event_id':'E000001','reason':'注入测试，不能当实验结论'}
                return {'items':[{'id':i['id'],'score':2,'evidence':['E000001'],'reason':'注入测试'} for i in criteria],'severe_errors':[error,error.copy()]}
        with patch.object(scoring,'Client',Grader):result=scoring.quality_grade(e.world,self.run,criteria,'grader_gpt',{})
        self.assertTrue(any('TAIL_EVIDENCE_MARKER' in p for p in seen));self.assertEqual(len(result['items']),17);self.assertEqual(len(result['severe_errors']),1)
    def test_na_reduces_actual_grade_denominator(self):
        import shutil
        c=Path(self.tmp.name)/'na_case'
        for folder in ('world','legal_corpus','rubric'):shutil.copytree(self.case/folder,c/folder)
        e=Engine(c,c/'runs/na_case',profile='reference');original=e.ask_candidate
        def choose_testimony():
            action=original()
            if action['action']=='elect_testimony':action['testify']=True
            return action
        e.ask_candidate=choose_testimony;e.run_all();score=grade(c,e.run)
        self.assertEqual(score['raw_denominator'],120);self.assertEqual(score['applicable_denominator'],118)
        self.assertEqual(sum(i['status']=='N/A' for i in read_json(c/'scores/na_case/script_checks.json')),1)
    def test_full_offline_both_cases_strong_weak(self):
        for cname in ('005_Williams','006_Wilson'):
            scores={}
            for profile in ('reference','weak'):
                # Copy scores to temporary case sibling; no delivery runs mutated by tests.
                import shutil
                c=Path(self.tmp.name)/cname/profile;shutil.copytree(ROOT/cname/'world',c/'world');shutil.copytree(ROOT/cname/'legal_corpus',c/'legal_corpus');shutil.copytree(ROOT/cname/'rubric',c/'rubric')
                run=c/'runs'/profile;e=Engine(c,run,profile=profile);view_sizes=[];original_ask=e.ask_candidate
                def observed_ask():
                    view_sizes.append(len(canonical(e.view()).encode()));return original_ask()
                e.ask_candidate=observed_ask
                summary=e.run_all();s=grade(c,run);scores[profile]=s
                self.assertLess(max(view_sizes)+5000+32768,150000,'候选视图连同schema与输出预留过大')
                self.assertEqual(summary['simulation_round_count'],3);self.assertEqual(summary['status'],'completed');self.assertEqual(len(e.s['jurors']),6);self.assertLessEqual(e.s['actions_used'],72)
                if profile=='reference':
                    self.assertEqual(set(summary['artifacts']),set(e.world.manifest['required_artifacts']));self.assertGreaterEqual(s['mechanical_percent'],85)
                    for fn in e.world.manifest['required_artifacts']:self.assertGreaterEqual(e.s['artifacts'][fn]['version'],3)
                else:self.assertLessEqual(s['mechanical_percent'],40)
                self.assertEqual(s['quality_pending'],17);self.assertEqual(s['status'],'pending_dual_grading')
            self.assertGreater(scores['reference']['mechanical_points'],scores['weak']['mechanical_points'])
    def test_live_role_branches_with_injected_transport_no_network(self):
        sizes=[]
        class Roles:
            def call(self,inst,payload,schema,role='',effort=None):
                sizes.append(len(canonical(payload).encode())+len(inst.encode())+len(canonical(schema).encode()))
                if schema==PROS_OBJECTION_SCHEMA:return {'object':False,'ground':'','reason':''}
                if schema==CONSENT_SCHEMA:return {'testify':False,'reason':'注入测试的自愿决定。'}
                if schema==VERIFY_SCHEMA:return {'supported':True,'reason':'固定响应仅用于离线协议路径测试。'}
                if schema==SUBPOENA_SCHEMA:return {'decision':'granted','authorized_documents':[d['doc_id'] for d in payload['available_documents']],'reason':'固定响应：准许所列相关资料。','limitations':'仅限本案准备。'}
                if schema==ACQUITTAL_SCHEMA:return {'decision':'denied','reason':'固定响应：继续审理，以覆盖完整三轮。','legal_ids':[],'record_refs':[],'charge_decisions':[{'charge_id':c['charge_id'],'decision':'denied','reason':'完整流程测试'} for c in payload['active_charges']]}
                if schema==RECORD_NOTE_SCHEMA:return {'items':[{'source_ids':[part['source_id']],'summary':part['content'][:80]} for part in payload['source_parts']]}
                if schema==TEXT_SCHEMA:return {'text':'请说明你本人能够确认的观察及记录，不补入未知内容。'}
                if schema==WITNESS_SCHEMA:return {'claims':[{'text':f['text'],'fact_ids':[f['fact_id']]} for f in payload['allowed_facts'][:2]],'unknown':False,'answer':''}
                if schema==MOTION_SCHEMA:
                    if 'target' in payload:dec='granted' if payload['objection']['ground'] in classify(payload['target']['text']) else 'denied'
                    else:dec='limited'
                    return {'decision':dec,'reason':'基于当前记录的测试裁定；不新增事实。','legal_ids':[],'record_refs':[]}
                if schema==JUROR_SCHEMA:return {'votes':[{'charge_id':c['charge_id'],'decision':'not_guilty','reason':'注入角色分支测试'} for c in payload['charges']]}
                raise AssertionError('unexpected schema')
        for cname in ('005_Williams','006_Wilson'):
            e=Engine(ROOT/cname,Path(self.tmp.name)/cname/'role_test',profile='reference',mock=False,clients={'environment':Roles()})
            e.run_all();self.assertEqual(e.s['status'],'completed');self.assertFalse(e.mock);self.assertEqual(len(e.s['jurors']),6)
        self.assertLess(max(sizes)+16384,150000,'live环境角色输入过大；必须改为完整可检索记录，不能截掉评分材料')

class TransportTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.run=self.root/'run'
    def tearDown(self):self.tmp.cleanup()
    def response(self,value,model='gpt-5.6-sol'):
        return {'model':model,'output':[{'type':'message','content':[{'type':'output_text','text':canonical(value)}]}],'usage':{'input_tokens':100,'output_tokens':20,'total_tokens':120,'input_tokens_details':{'cached_tokens':25},'output_tokens_details':{'reasoning_tokens':5}}}
    def test_explicit_instructions_seed_output_store_and_alias(self):
        seen=[]
        def transport(body):seen.append(body);return self.response({'text':'ok'},'doubao-seed-evolving-latest-version')
        c=Client(self.root,self.run,'seed',transport=transport,sleeper=lambda t:None);self.assertEqual(c.call('explicit',{},TEXT_SCHEMA)['text'],'ok');self.assertEqual(seen[0]['max_output_tokens'],32768);self.assertFalse(seen[0]['store']);self.assertEqual(seen[0]['reasoning']['effort'],'medium')
        row=jsonl(self.root/'usage_ledger.jsonl')[0];self.assertEqual(row['cached_tokens'],25);self.assertEqual(row['reasoning_tokens'],5)
    def test_cached_success_not_reissued(self):
        seen=[]
        def transport(body):seen.append(body);return self.response({'text':'ok'})
        c=Client(self.root,self.run,'gpt',transport=transport);c.call('explicit',{},TEXT_SCHEMA);c.call('explicit',{},TEXT_SCHEMA);self.assertEqual(len(seen),1)
    def test_transient_retry_total_four_and_persistent(self):
        count=[]
        def fail(body):count.append(1);raise urllib.error.URLError('offline injected timeout')
        c=Client(self.root,self.run,'gpt',transport=fail,sleeper=lambda t:None)
        with self.assertRaises(APIError):c.call('explicit',{},TEXT_SCHEMA)
        self.assertEqual(len(count),4)
        with self.assertRaises(APIError):c.call('explicit',{},TEXT_SCHEMA)
        self.assertEqual(len(count),4);self.assertGreater(c.ledger.totals()['run_reserved_unknown'],0)
    def test_nonretry_auth_failure(self):
        count=[]
        def fail(body):count.append(1);raise urllib.error.HTTPError('url',401,'unauthorized',{},None)
        c=Client(self.root,self.run,'gpt',transport=fail,sleeper=lambda t:None)
        with self.assertRaises(APIError):c.call('explicit',{},TEXT_SCHEMA)
        self.assertEqual(len(count),1)
    def test_format_repair_once(self):
        count=[]
        def bad(body):count.append(1);return self.response({'wrong':'shape'})
        c=Client(self.root,self.run,'gpt',transport=bad,sleeper=lambda t:None)
        with self.assertRaises(ProtocolError):c.call('explicit',{},TEXT_SCHEMA)
        self.assertEqual(len(count),2)
    def test_wrong_model_identity_fails(self):
        c=Client(self.root,self.run,'gpt',transport=lambda b:self.response({'text':'ok'},'different-model'))
        with self.assertRaises(APIError):c.call('explicit',{},TEXT_SCHEMA)
    def test_no_instruction_fails_before_transport(self):
        c=Client(self.root,self.run,'gpt',transport=lambda b:None)
        with self.assertRaises(ProtocolError):c.call('',{},TEXT_SCHEMA)
    def test_grader_effort_high(self):
        seen=[]
        def transport(body):seen.append(body);return self.response({'text':'ok'},'gpt-6-sol')
        c=Client(self.root,self.run,'grader_gpt',transport=transport);c.call('explicit',{},TEXT_SCHEMA);self.assertEqual(seen[0]['reasoning']['effort'],'high')
    def test_context_includes_output_reserve(self):
        with self.assertRaises(BudgetError):Ledger(self.root,self.run).check(140000,32768)
    def test_schema_validation(self):
        with self.assertRaises(ProtocolError):validate_schema({'text':True},TEXT_SCHEMA)
    def test_scoring_chunks_cover_all_evidence(self):
        rows=[{'id':i,'text':'证据'*100} for i in range(100)];parts=chunks(rows,2000);self.assertGreater(len(parts),1);self.assertEqual([r['id'] for b in parts for r in b],list(range(100)))

if __name__=='__main__':unittest.main(verbosity=2)
