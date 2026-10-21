"""Regression coverage for independently reproduced court/permission/context defects.
No network, no model keys; fixtures inject role decisions without changing real source files.
"""
import copy,json,tempfile,unittest
from pathlib import Path
from .engine import Engine,World,ACQUITTAL_SCHEMA,SUBPOENA_SCHEMA,RECORD_NOTE_SCHEMA,VERIFY_SCHEMA,TEXT_SCHEMA
from .store import ProtocolError,EvidenceError,canonical
from .context import size,VIEW_BYTES,PAGE_CHARS

ROOT=Path(__file__).resolve().parents[1]
class JudgeFixture:
    def __init__(self,grants=(),subpoena='granted'):
        self.grants=set(grants);self.subpoena=subpoena;self.calls=[]
    def call(self,instructions,payload,schema,role='',**kwargs):
        self.calls.append((role,copy.deepcopy(payload)))
        if schema==SUBPOENA_SCHEMA:
            docs=[x['doc_id'] for x in payload['available_documents']]
            return {'decision':self.subpoena,'authorized_documents':docs if self.subpoena=='granted' else [],'reason':'逐项审查相关性、最小范围和隐私后裁定。','limitations':'仅为本案诉讼使用。'}
        if schema==ACQUITTAL_SCHEMA:
            charges=payload['active_charges']
            return {'decision':'granted','reason':'测试固定逐罪处置。','legal_ids':payload['motion']['legal_ids'],'record_refs':payload['motion']['references'],
                    'charge_decisions':[{'charge_id':c['charge_id'],'decision':'granted' if c['charge_id'] in self.grants else 'denied','reason':'注入的逐罪裁定，用于检验状态变更。'} for c in charges]}
        if role.startswith('R_JUROR_'):
            assert not self.grants&{c['charge_id'] for c in payload['charges']},'Acquitted charge reached jury'
            return {'votes':[{'charge_id':c['charge_id'],'decision':'guilty','reason':'注入票仅用于验证剩余罪名。'} for c in payload['charges']]}
        if schema==RECORD_NOTE_SCHEMA:
            return {'items':[{'source_ids':[p['source_id']],'summary':p['content'][:80]} for p in payload['source_parts']]}
        if schema==VERIFY_SCHEMA:return {'supported':True,'reason':'测试片段逐字来源一致。'}
        if schema==TEXT_SCHEMA:return {'text':'ok'}
        raise AssertionError((role,schema))

class EngineV2Tests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory(prefix='court_v2_test_')
    def tearDown(self):self.tmp.cleanup()
    def engine(self,case='006_Wilson',live=False,fixture=None):
        e=Engine(ROOT/case,Path(self.tmp.name)/case,profile='weak',mock=not live,clients={'environment':fixture} if fixture else {})
        e.enter();return e
    def stage(self,e,sid):
        i=next(i for i,x in enumerate(e.world.stages) if x['stage_id']==sid);st=e.world.stages[i]
        e.s.update(stage_index=i,stage_id=sid,round=st['round'],slot=0,script=None,script_cursor=0,pending_window=None,pending_answer=None)
        e.commit([]);e.enter()
    def acquire_read(self,e,did):
        e.s['disclosed']=list(dict.fromkeys(e.s['disclosed']+[did]));e.commit([])
        cursor='offset:0'
        while cursor:
            e.apply({'action':'read_document','target':did,'query':cursor});cursor=e.s['tool_result']['next_offset']
    def acquittal(self,e):
        self.stage(e,'r2_1118');self.acquire_read(e,'D01')
        e.apply({'action':'file_motion','target':'acquittal_1118','text':'请求依控方举证后的记录对全部待审罪名作无罪判决。','legal_ids':['L-PEN11181'],'references':['D01']})

    def test_all_acquittal_ends_case_without_jury_and_resumes(self):
        judge=JudgeFixture(('C01','C02'));e=self.engine(live=True,fixture=judge);self.acquittal(e)
        self.assertEqual(e.s['status'],'completed');self.assertEqual(e.s['termination_reason'],'judgment_of_acquittal')
        self.assertEqual(e.s['verdict'],{'C01':'not_guilty','C02':'not_guilty'});self.assertEqual(e.s['jurors'],[])
        summary=e.run_all();self.assertEqual(summary['completed_rounds'],2)
        restored=Engine(ROOT/'006_Wilson',e.run,profile='weak',mock=False,clients={'environment':judge});before=len(restored.store.events);restored.run_all()
        self.assertEqual(len(restored.store.events),before);self.assertFalse(any(r.startswith('R_JUROR') for r,_ in judge.calls))
        jury=json.loads((e.run/'jury_record.json').read_text());self.assertEqual(jury['votes'],[])

    def test_partial_acquittal_only_remaining_charge_goes_to_jury(self):
        judge=JudgeFixture(('C01',));e=self.engine(live=True,fixture=judge);self.acquittal(e)
        self.assertEqual(e.s['status'],'running');self.assertEqual([c['charge_id'] for c in e.active_charges()],['C02'])
        self.stage(e,'r3_verdict');e.deliberate();e.finish_outputs()
        self.assertEqual(e.s['verdict'],{'C01':'not_guilty','C02':'guilty'});self.assertEqual(len(e.s['jurors']),6)

    def test_mock_default_acquittal_is_denied_not_pretend_granted(self):
        e=self.engine();self.acquittal(e);self.assertEqual(e.s['motions'][-1]['decision'],'denied');self.assertEqual(len(e.active_charges()),2)

    def test_expert_cannot_be_accessed_through_ordinary_interview(self):
        e=self.engine()
        with self.assertRaises(ProtocolError):e.apply({'action':'interview_witness','target':'R_EXPERT_CAREFUL','text':'请解释本案血样。'})
        with self.assertRaises(ProtocolError):e.witness('R_EXPERT_CAREFUL','请解释血样',[])
        self.assertEqual(e.s['money_used'],0);self.assertEqual(e.s['experts'],{})

    def test_hired_expert_without_materials_has_no_hidden_memory(self):
        e=self.engine();e.apply({'action':'hire_expert','target':'R_EXPERT_CAREFUL'})
        e.apply({'action':'interview_witness','target':'R_EXPERT_CAREFUL','text':'本案血液具体结果是多少？'})
        self.assertEqual(e.s['tool_result']['fact_ids'],[]);self.assertNotIn('0.12',e.s['tool_result']['answer'])

    def test_client_generic_reference_does_not_release_hidden_memo(self):
        e=self.engine('005_Williams');self.acquire_read(e,'D01')
        e.apply({'action':'interview_client','text':'请介绍一下你的情况。','references':['D01']})
        self.assertNotIn('D21',e.s['disclosed'])
        with self.assertRaises(ProtocolError):e.apply({'action':'read_document','target':'D21'})

    def test_client_each_condition_must_be_confronted_before_full_memo(self):
        e=self.engine('005_Williams');self.acquire_read(e,'D10');self.acquire_read(e,'D11')
        e.apply({'action':'interview_client','text':'请对照签名接收表，具体说明你是否签过。','references':['D10']})
        self.assertNotIn('D21',e.s['disclosed'])
        e.apply({'action':'interview_client','text':'请对照家访底稿，具体说明离开后是否联系代理人。','references':['D11']})
        self.assertIn('D21',e.s['disclosed'])

    def test_late_objection_has_no_timeliness_credit(self):
        e=self.engine();self.stage(e,'r1_examination');e.run_script_item({'type':'trap','trap':'T_CHARACTER','text':'被告有前科，所以肯定有罪。'})
        target=e.s['pending_window'];e.apply({'action':'pass'},window=True)
        for _ in range(5):e.commit([e.ev('other_event','另一项庭审记录')])
        before=e.s['slot'];e.apply({'action':'move_to_strike','target':target,'ground':'character','text':'请求删除品格有罪推断。','references':[target]})
        row=e.s['objections'][-1];self.assertFalse(row['timely']);self.assertFalse(row['window']);self.assertIsNone(row['window_event_id']);self.assertEqual(e.s['slot'],before+1)
        from .scoring import check
        rubric=json.loads((e.root/'rubric/rubric.json').read_text());item=next(x for x in rubric['criteria'] if x.get('argument')=='T_CHARACTER')
        self.assertEqual(check(item,e.s,e.store.events,e.world)['score'],0)

    def test_read_in_window_preserves_real_opportunity_and_costs_no_slot(self):
        e=self.engine();self.stage(e,'r1_examination');e.run_script_item({'type':'trap','trap':'T_CHARACTER','text':'被告有前科，所以肯定有罪。'})
        target=e.s['pending_window'];before=(e.s['slot'],e.s['hours_used'],e.s['actions_used'])
        e.apply({'action':'read_record','target':target},window=True)
        self.assertEqual(e.s['pending_window'],target);self.assertEqual(before,(e.s['slot'],e.s['hours_used'],e.s['actions_used']))
        e.apply({'action':'move_to_strike','target':target,'ground':'character','text':'请求删除品格有罪推断。'},window=True)
        self.assertTrue(e.s['objections'][-1]['timely']);self.assertEqual(e.s['objections'][-1]['window_event_id'],target)

    def test_all_court_decision_stages_have_full_record_access(self):
        e=self.engine()
        for sid in ('r1_motion','r1_examination','r2_1118','r2_defense','r3_instructions','r3_closing'):
            self.stage(e,sid);before=e.s['slot'];e.apply({'action':'search_record','target':'offset:0','query':''})
            self.assertEqual(before,e.s['slot']);self.assertIn('read_artifact',e.allowed());self.assertIn('read_law',e.allowed())

    def test_paged_law_only_counts_read_when_all_pages_seen(self):
        e=self.engine();self.stage(e,'r3_instructions');lid='L_PEN_1096';text='法律原文甲。'*1800;e.world.laws[lid]['text']=text
        e.apply({'action':'read_law','target':lid});self.assertNotIn(lid,e.s['law_reads'])
        read=e.s['tool_result']['text'];offset=e.s['tool_result']['next_offset']
        while offset:
            e.apply({'action':'read_law','target':lid,'query':offset});read+=e.s['tool_result']['text'];offset=e.s['tool_result']['next_offset']
        self.assertEqual(read,text);self.assertIn(lid,e.s['law_reads']);self.assertEqual(e.s['slot'],0)
        self.assertEqual(sum(x['kind']=='read_law' for x in e.store.events),1)

    def test_large_artifact_keeps_view_bounded_and_pages_are_lossless(self):
        e=self.engine();self.stage(e,'r3_1118');text='证据事实法律分析。'*2500
        e.apply({'action':'write_artifact','filename':'long_memo.md','content':text});before=e.s['slot']
        self.assertLess(size(e.view()),VIEW_BYTES);self.assertEqual((e.run/'artifacts/long_memo.md').read_text(),text)
        offset='offset:0';read=''
        while offset:
            e.apply({'action':'read_artifact','target':'long_memo.md','query':offset});result=e.s['tool_result'];read+=result['text'];offset=result['next_offset']
            self.assertLess(size(e.view()),VIEW_BYTES)
        self.assertEqual(read,text);self.assertEqual(e.s['slot'],before)

    def test_four_byte_unicode_pages_reach_candidate_without_truncation(self):
        e=self.engine();self.stage(e,'r3_instructions');lid='L_PEN_1096';text='😀'*4001;e.world.laws[lid]['text']=text
        offset='offset:0';received=''
        while offset:
            e.apply({'action':'read_law','target':lid,'query':offset});result=e.view()['last_tool_result']
            self.assertEqual(result['text'],text[result['offset']:result['end_offset']])
            received+=result['text'];offset=result['next_offset']
        self.assertEqual(received,text);self.assertIn(lid,e.s['law_reads'])

    def test_prior_workfile_version_is_still_readable(self):
        e=self.engine();self.stage(e,'r3_1118')
        first='第一版记录。\r\n'*700
        e.apply({'action':'write_artifact','filename':'memo.md','content':first})
        e.apply({'action':'write_artifact','filename':'memo.md','content':'第二版'})
        offset='offset:0';read=''
        while offset:
            e.apply({'action':'read_artifact','target':'memo.md','purpose':'version:1','query':offset});read+=e.s['tool_result']['text'];offset=e.s['tool_result']['next_offset']
        self.assertEqual(read,first);self.assertEqual(e.s['artifacts']['memo.md']['content'],'第二版')

    def test_subpoena_court_denial_does_not_disclose(self):
        judge=JudgeFixture(subpoena='denied');e=self.engine(live=True,fixture=judge);before=set(e.s['disclosed'])
        e.apply({'action':'request_subpoena','query':'请向医院调取护理采血与交接记录。','purpose':'核对本案采血事实及同意程序并限定使用范围。'})
        self.assertEqual(set(e.s['disclosed']),before);self.assertEqual(judge.calls[-1][0],'R_JUDGE')
        self.assertTrue(any(x['kind']=='subpoena_ruling' and x['meta']['decision']=='denied' for x in e.store.events))

    def test_subpoena_explicit_unrelated_purpose_cannot_be_granted(self):
        judge=JudgeFixture();e=self.engine(live=True,fixture=judge);before=set(e.s['disclosed'])
        e.apply({'action':'request_subpoena','query':'请向医院调取护理采血与交接记录。','purpose':'与案件无关的目的'})
        self.assertEqual(set(e.s['disclosed']),before)

    def test_subpoena_unknown_document_from_judge_is_blocked(self):
        class BadJudge(JudgeFixture):
            def call(self,*args,**kwargs):return {'decision':'granted','authorized_documents':['D99'],'reason':'错误授权','limitations':''}
        e=self.engine(live=True,fixture=BadJudge());before=set(e.s['disclosed'])
        with self.assertRaises(EvidenceError):e.apply({'action':'request_subpoena','query':'请向医院调取护理采血与交接记录。','purpose':'核查具体采血时间与交接事实，并限定范围。'})
        self.assertEqual(set(e.s['disclosed']),before)

    def test_long_role_record_uses_all_chunks_and_validates_notes(self):
        judge=JudgeFixture();e=self.engine(live=True,fixture=judge)
        rows=[{'event_id':f'T{i}','text':('来源内容'+str(i))*1200} for i in range(20)]
        out=e.environment_call('根据完整记录核验。',{'record':rows},TEXT_SCHEMA,'R_JUDGE')
        self.assertEqual(out['text'],'ok')
        seen={p['source_id'] for role,payload in judge.calls if role=='R_RECORD_REVIEW' for p in payload['source_parts']}
        self.assertEqual(seen,{r['event_id'] for r in rows});self.assertTrue(any(role=='R_RECORD_VALIDATOR' for role,_ in judge.calls))
        self.assertLess(size(judge.calls[-1][1]),105000)

if __name__=='__main__':unittest.main()
