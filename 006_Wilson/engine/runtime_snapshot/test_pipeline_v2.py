"""Safety gates and operational controls; all responses are local fixtures."""
from pathlib import Path
import json,os,tempfile,unittest
from unittest.mock import patch
from . import cli
from .store import atomic,append,read_json,EvidenceError,StopRequested
from .freeze import preregister,verify_formal,verify_preregistered,calibrate_gate

class PipelineTestsV2(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.case=self.root/'005_Williams'
        for name in ('world','rubric','engine','legal_corpus','runs','scores'):(self.case/name).mkdir(parents=True,exist_ok=True)
        cfg=read_json(Path(__file__).resolve().parents[1]/'config.json');atomic(self.root/'config.json',cfg)
        atomic(self.case/'world/manifest.json',{'case_id':'CA-PILOT-005'})
    def tearDown(self):self.tmp.cleanup()
    def test_stop_prevents_any_new_run_directory(self):
        cli.request_stop(self.case)
        with self.assertRaises(StopRequested):cli.run_one(self.case,'gpt_r1',candidate='gpt')
        self.assertFalse((self.case/'runs/gpt_r1').exists())
    def test_forged_freeze_without_calibration_evidence_is_rejected(self):
        reg=preregister(self.case)
        atomic(self.case/'rubric/freeze.json',{**reg,'status':'live_calibration_passed','calibration':{'passed':True}})
        with self.assertRaisesRegex(EvidenceError,'证据'):verify_formal(self.case)
    def test_mock_scores_never_open_formal_gate(self):
        preregister(self.case)
        with self.assertRaises(EvidenceError):calibrate_gate(self.case,{'mock':True,'status':'completed'},{'mock':True,'status':'completed'})
        self.assertFalse((self.case/'rubric/freeze.json').exists())
    def test_empty_live_score_dictionaries_cannot_pass_gate(self):
        preregister(self.case)
        fake={'mock':False,'status':'completed','run_id':'invented','models':{}}
        with self.assertRaises(EvidenceError):calibrate_gate(self.case,fake,fake)
        self.assertFalse((self.case/'rubric/freeze.json').exists())
    def test_content_change_after_formal_attempt_cannot_re_register(self):
        preregister(self.case);(self.case/'runs/gpt_r1').mkdir()
        atomic(self.case/'world/changed.json',{'change':True})
        with self.assertRaises(EvidenceError):preregister(self.case)
    def test_environment_file_change_invalidates_preregistration(self):
        atomic(self.root/'ENVIRONMENT.json',{'python_minimum':'3.11'})
        (self.root/'requirements.txt').write_text('# standard library only\n')
        reg=preregister(self.case)
        self.assertIn('ENVIRONMENT.json',reg['files']);self.assertIn('requirements.txt',reg['files'])
        atomic(self.root/'ENVIRONMENT.json',{'python_minimum':'3.12'})
        with self.assertRaises(EvidenceError):verify_preregistered(self.case)
    def test_preflight_rejects_partial_usage_even_if_not_unknown(self):
        preregister(self.case);case=self.case
        class FakeClient:
            def __init__(self,case,run,route,cfg):self.route=route
            def call(self,*args,**kwargs):
                append(case/'usage_ledger.jsonl',{'run_id':'api_preflight','route':self.route,'ok':True,'usage_complete':self.route!='seed','usage_unknown':False,'total_tokens':1})
                return {'text':'ok'}
        with patch.dict(os.environ,{'PACKY_API_KEY':'fixture-not-a-real-key','ARK_API_KEY':'fixture-not-a-real-key'}),patch.object(cli,'Client',FakeClient):
            with self.assertRaisesRegex(EvidenceError,'usage'):cli.preflight(self.case)
        self.assertFalse((self.case/'runs/api_preflight/preflight.json').exists())
    def test_complete_usage_preflight_creates_check_record(self):
        reg=preregister(self.case);case=self.case
        class FakeClient:
            def __init__(self,case,run,route,cfg):self.route=route
            def call(self,*args,**kwargs):
                append(case/'usage_ledger.jsonl',{'run_id':'api_preflight','route':self.route,'ok':True,'usage_complete':True,'usage_unknown':False,'total_tokens':1})
                return {'text':'ok'}
        with patch.dict(os.environ,{'PACKY_API_KEY':'fixture-not-a-real-key','ARK_API_KEY':'fixture-not-a-real-key'}),patch.object(cli,'Client',FakeClient):cli.preflight(self.case)
        self.assertEqual(read_json(self.case/'runs/api_preflight/preflight.json')['content_sha256'],reg['content_sha256'])

if __name__=='__main__':unittest.main()
