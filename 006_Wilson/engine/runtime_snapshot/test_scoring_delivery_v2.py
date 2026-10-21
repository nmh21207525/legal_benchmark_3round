"""Targeted audit regressions for legal disposition, timing and delivered evidence."""
from pathlib import Path
from types import SimpleNamespace
import copy,json,tempfile,unittest
from .engine import World
from .store import EvidenceError,canonical,atomic
from .scoring import applicable,check,chunks,validate_outcome
from .report import summarize,render_run

ROOT=Path(__file__).resolve().parents[1]

class ScoringDeliveryV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.dir=Path(self.temp.name)
        self.world=World(ROOT/'006_Wilson/world');self.state=self.world.initial()
    def tearDown(self):self.temp.cleanup()
    def event(self,kind,**kw):
        return {'event_id':kw.pop('event_id','E000001'),'stage_id':kw.pop('stage_id','r2_examination'),'round':2,'kind':kind,'actor':kw.pop('actor','R_DEFENSE'),'text':kw.pop('text',''),'references':kw.pop('references',[]),'audience':['R_DEFENSE','R_JUDGE'], 'meta':kw}
    def test_late_objection_cannot_claim_timely_full_credit(self):
        item={'id':'M36','check':'objection','argument':'T_CHARACTER','max_points':2}
        target=self.event('trap_statement',trap_id='T_CHARACTER')
        response=self.event('objection',event_id='E000009',references=['E000001'],action='move_to_strike',timely=False)
        self.state['objections']=[{'target_event_id':'E000001','ground':'character','decision':'sustained','window':True,'timely':False,'window_event_id':None}]
        self.assertEqual(check(item,self.state,[target,response],self.world)['score'],0)
        self.state['objections'][0].update(timely=True,window_event_id='E000001');response['meta']['timely']=True
        self.assertEqual(check(item,self.state,[target,response],self.world)['score'],2)
    def test_stage_na_only_after_lawful_terminal_acquittal(self):
        item={'opportunity':{'type':'stage','stage_ids':['r3_closing']}}
        self.assertTrue(applicable(item,[],self.state),'Not acting is not N/A')
        self.state.update(termination_reason='judgment_of_acquittal',reached_stages=['r2_1118'])
        self.assertFalse(applicable(item,[],self.state))
        self.state['reached_stages'].append('r3_closing');self.assertTrue(applicable(item,[],self.state))
    def judicial(self,all_charges=True):
        charges=self.world.manifest['charges'];ids=[c['charge_id'] for c in charges]
        selected=ids if all_charges else ids[:1]
        self.state.update(status='completed',verdict={c:'not_guilty' if c in selected else 'guilty' for c in ids},charge_dispositions={c:{'decision':'not_guilty','source':'judgment_of_acquittal','stage_id':'r2_1118'} for c in selected},termination_reason='judgment_of_acquittal' if all_charges else None)
        self.state['jurors']=[] if all_charges else [{'juror':i+1,'votes':[{'charge_id':c,'decision':'guilty'} for c in ids if c not in selected]} for i in range(6)]
    def test_judicial_acquittal_outcome_needs_no_fabricated_jurors(self):
        self.judicial();self.assertTrue(validate_outcome(self.state,self.world))
        item={'id':'O01','check':'outcome','max_points':2}
        self.assertEqual(check(item,self.state,[],self.world)['score'],2)
        self.state['verdict']['C01']='guilty'
        with self.assertRaises(EvidenceError):validate_outcome(self.state,self.world)
    def test_partial_acquittal_only_remaining_charge_goes_to_jury(self):
        self.judicial(False);self.assertTrue(validate_outcome(self.state,self.world))
        self.state['jurors'][0]['votes'].append({'charge_id':'C01','decision':'guilty'})
        with self.assertRaises(EvidenceError):validate_outcome(self.state,self.world)
    def test_artifact_updates_do_not_require_cancelled_round(self):
        item={'id':'M17','check':'artifact','argument':'timeline.csv','max_points':2,'update_stages':['pretrial_workspace','between_rounds_1','between_rounds_2']}
        events=[self.event('artifact_written',event_id=f'E{i+1:06d}',stage_id=s,filename='timeline.csv') for i,s in enumerate(['pretrial_workspace','between_rounds_1'])]
        self.state.update(termination_reason='judgment_of_acquittal',reached_stages=['pretrial_workspace','between_rounds_1','r2_1118'])
        self.assertEqual(check(item,self.state,events,self.world)['score'],2)
        self.state['termination_reason']=None;self.assertEqual(check(item,self.state,events,self.world)['score'],1)
    def test_long_evidence_fragments_preserve_every_character(self):
        row={'type':'artifact','path':'artifacts/versions/001/long.md','text':'证据“引文”\\\n'*15000}
        result=chunks([row],65000);fragments=[x for group in result for x in group]
        restored=''.join(x['json_fragment'] for x in fragments)
        self.assertEqual(restored,canonical(row))
        self.assertTrue(all(len(canonical(group).encode())<65010 for group in result))
    def test_generic_client_question_is_not_completed_confrontation(self):
        item={'id':'M09','check':'client_confront','max_points':2}
        self.state['client_interviews']=[{'question':'是否愿意介绍一下情况？','references':['D01']}]
        self.assertEqual(check(item,self.state,[],self.world)['score'],1)
        self.state['client_confronted_fact_ids']=['D19-F01']
        self.assertEqual(check(item,self.state,[],self.world)['score'],2)
    def test_duplicate_statute_ids_do_not_inflate_reading_count(self):
        item={'id':'M12','check':'law_count','argument':2,'max_points':2}
        self.state['law_reads']=['canonical','alias']
        world=SimpleNamespace(laws={'canonical':{'canonical_id':'same'},'alias':{'canonical_id':'same'}})
        self.assertEqual(check(item,self.state,[],world)['score'],1)
    def test_report_is_complete_and_does_not_invent_old_results(self):
        case=self.dir/'006_Wilson';case.mkdir();(case/'README.md').write_text('# 原说明\n\n## 当前结果与局限\n\n历史法源限制应保留。\n')
        (case/'usage_ledger.jsonl').write_text('')
        for n,pct in ((1,60),(2,80)):
            run=case/'runs'/f'gpt_r{n}';run.mkdir(parents=True);score=case/'scores'/run.name;score.mkdir(parents=True)
            atomic(run/'summary.json',{'status':'completed','mock':False,'candidate':'gpt','simulation_round_count':3,'verdict':{'C01':'hung'}})
            atomic(run/'usage.json',{'run_tokens':10,'run_reserved_unknown':0})
            atomic(score/'score_summary.json',{'status':'completed','models':{'grader_gpt':{'percent':pct-2,'penalty':2},'grader_seed':{'percent':pct+2,'penalty':0}},'mean_percent':pct,'agreement':.9,'raw_denominator':120,'applicable_denominator':118,'quality_pending':0,'disagreements':[{'id':'Q01','gpt':0,'seed':2,'gpt_reason':'具体反证','seed_reason':'具体支持'}]})
            atomic(score/'grader_gpt.json',{'severe_errors':[{'category':'fabrication','event_id':'E000001','reason':'注入测试'}]})
        summarize(case)
        report=(case/'scores/summary.md').read_text();data=json.loads((case/'scores/experiment_summary.json').read_text())
        self.assertIn('具体反证',report)
        means={r['metric']:r['mean'] for r in data['replicates'] if r['candidate']=='gpt'}
        self.assertEqual(means,{'gpt_grader':68,'seed_grader':72,'dual_grader_mean':70})
        self.assertTrue(all(x['old_status']=='not_provided' for x in data['legacy_comparison']))
        self.assertTrue((case/'scores/replicates.csv').exists());self.assertIn('历史法源限制应保留',(case/'README.md').read_text())
        self.assertIn('2/4',(case/'README.md').read_text());self.assertTrue((case/'RESULTS.md').exists())
    def test_replay_embeds_work_product_versions_and_escapes_html(self):
        case=self.dir/'case';run=case/'runs/r';(run/'artifacts/versions/001').mkdir(parents=True)
        (run/'author_events.jsonl').write_text('');atomic(run/'summary.json',{'mock':True})
        (run/'artifacts/versions/001/note.md').write_text('原始工作稿 <script>unsafe()</script>')
        render_run(case,run);text=(run/'replay.html').read_text()
        self.assertIn('原始工作稿 &lt;script&gt;',text);self.assertIn('artifacts/versions/001/note.md',text)
        self.assertNotIn('<script>unsafe()</script>',text)

if __name__=='__main__':unittest.main()
