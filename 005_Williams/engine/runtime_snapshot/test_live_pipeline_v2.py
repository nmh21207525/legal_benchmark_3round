"""Network-free integration of the complete *live-mode* orchestration path.

Responses below are deterministic fixtures, NOT model/quality results. They
exercise schema validation, ledgers, calibration gates, freezing, repeated
candidate runs, dual grading, resumption and delivery export without bypassing
those components. All fixture records and archives live in TemporaryDirectory.
"""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zipfile

from . import cli
from .engine import ACTION_SCHEMA,words
from .export import CALIBRATION_RUNS,FORMAL_RUNS,export_case,validate_case
from .freeze import preregister
from .llm import Client
from .policy import action_for
from .store import EvidenceError,atomic,canonical,jsonl,read_json


class ResponseFixture:
    """A fixed response service seeing only the actual outgoing request body."""
    def __init__(self):self.calls=[]

    @staticmethod
    def strong(run):
        return run in ('calibration_reference_r1','gpt_r1','seed_r1')

    def reply(self,client,body):
        payload=json.loads(body['input'])
        schema=body['text']['format']['schema']
        fields=set(schema['properties'])
        run=client.run.name
        self.calls.append((run,client.route,tuple(sorted(fields))))
        if fields=={'action','target','text','query','references','legal_ids','materials','filename','content','purpose','ground','testify'}:
            # Exactly the same public view as a real candidate receives. The
            # fixture does not read world files, states or grading criteria.
            selected=action_for(payload,'reference' if self.strong(run) else 'weak')
            value={k:False if v['type']=='boolean' else [] if v['type']=='array' else '' for k,v in ACTION_SCHEMA['properties'].items()}
            value.update(selected)
        elif fields=={'claims','unknown','answer'}:
            terms=words(payload['question'])
            facts=sorted(payload['allowed_facts'],key=lambda f:-sum(term in f['text'].lower() for term in terms))[:5]
            claims=[{'fact_ids':[f['fact_id']],'text':f['text']} for f in facts]
            value={'claims':claims,'unknown':not claims,'answer':'\n'.join(x['text'] for x in claims)}
        elif fields=={'supported','reason'}:
            value={'supported':True,'reason':'离线固定响应，仅用于接线验收，不代表真实语义判断。'}
        elif fields=={'votes'}:
            decision='not_guilty' if self.strong(run) else 'guilty'
            value={'votes':[{'charge_id':c['charge_id'],'decision':decision,'reason':'离线固定响应：用于验证校准闸门和逐罪一致票，不是真实模型裁决。'} for c in payload['charges']]}
        elif 'authorized_documents' in fields:
            value={'decision':'granted','authorized_documents':[d['doc_id'] for d in payload['available_documents']], 'reason':'离线固定响应：仅准许请求列出的现有相关记录。','limitations':'仅本案使用；不存在材料不得补写。'}
        elif fields=={'testify','reason'}:
            value={'testify':bool(payload['suggested_testify']),'reason':'离线固定响应：按已输入咨询建议作选择，仅测试流程。'}
        elif fields=={'object','ground','reason'}:
            value={'object':False,'ground':'','reason':'离线固定响应：本问不提出异议。'}
        elif 'charge_decisions' in fields:
            value={'decision':'denied','reason':'离线固定响应：驳回以覆盖全部三轮；不是实体法律评价。','legal_ids':list(payload['laws']),'record_refs':payload['motion']['references'],
                   'charge_decisions':[{'charge_id':c['charge_id'],'decision':'denied','reason':'离线固定响应，继续审理。'} for c in payload['active_charges']]}
        elif fields=={'decision','reason','legal_ids','record_refs'}:
            motion=payload.get('motion',{})
            decision='limited' if motion.get('target') in ('registration_records','suppress_blood','M06_boundary') else 'granted'
            value={'decision':decision,'reason':'离线固定响应，仅验证裁定通路；真实法律判断待API校准。','legal_ids':list(payload.get('laws',{})), 'record_refs':motion.get('references',[])}
        elif 'severe_errors' in fields:
            references=[]
            for part in payload['all_parts_reviewed']:
                for item in part['review']['items']:references+=item['evidence']
            evidence=list(dict.fromkeys(references))[:1]
            if not evidence:raise AssertionError('Fixture final grader has no real event reference in review notes')
            value={'items':[{'id':item['id'],'score':2 if self.strong(run) else 0,'evidence':evidence,'reason':'离线固定分，仅用于检验评分算术、闸门和交付，不是模型质量结果。'} for item in payload['criteria']], 'severe_errors':[]}
        elif fields=={'items'}:
            item_fields=set(schema['properties']['items']['items']['properties'])
            if item_fields=={'source_ids','summary'}:
                ids=list(dict.fromkeys(p['source_id'] for p in payload['source_parts']))
                value={'items':[{'source_ids':ids,'summary':'离线固定响应：原始记录及引用保留，此测试不作法律推断。'}]}
            elif item_fields=={'id','evidence','assessment'}:
                ids=[]
                for row in payload.get('evidence',[]):
                    if row.get('type')=='event':ids.append(row['event']['event_id'])
                    if row.get('type')=='artifact':ids.append(row['path'])
                    if row.get('type')=='evidence_fragment' and str(row.get('source_reference','')).startswith(('E','artifacts/')):ids.append(row['source_reference'])
                for note in payload.get('notes',[]):
                    for item in note.get('review',{}).get('items',[]):ids+=item['evidence']
                value={'items':[{'id':item['id'],'evidence':list(dict.fromkeys(ids))[:1],'assessment':'离线固定审阅笔记，覆盖输入块以测试分块与汇总。'} for item in payload['criteria']]}
            else:raise AssertionError('Unrecognized items schema: '+str(item_fields))
        elif fields=={'text'}:
            value={'text':'ok' if payload.get('test')=='teacher-aligned-preflight' else payload.get('suggested_topic','控方请求依准入记录逐项判断控罪；此文本为离线接线测试固定响应。')}
        else:raise AssertionError('Unrecognized outgoing schema: '+str(fields))
        return {'model':body['model'],'status':'completed','output_text':canonical(value),
                'usage':{'input_tokens':100,'output_tokens':20,'total_tokens':120,'input_tokens_details':{'cached_tokens':25},'output_tokens_details':{'reasoning_tokens':5}},
                'test_fixture':True,'warning':'NO REAL MODEL CALL; PIPELINE WIRING TEST ONLY'}


class NetworkFreeLivePipelineV2Tests(unittest.TestCase):
    def test_preflight_calibration_four_runs_dual_scores_freeze_resume_and_export(self):
        source=Path(__file__).resolve().parents[1]
        # Copy static files before registering. Never place synthetic live-mode
        # runs or their frozen evidence in a real user's project directory.
        with tempfile.TemporaryDirectory(prefix='legal_no_network_pipeline_') as td:
            root=Path(td)/'legal_agent_3round'
            ignored=shutil.ignore_patterns('runs','scores','deliverables','__pycache__','.venv','.git','*.pyc','usage_ledger.jsonl','freeze.json','preregistration.json','pipeline.log','active_pipeline.json','background.json','STOP_REQUESTED.json')
            shutil.copytree(source,root,ignore=ignored)
            case=root/'005_Williams'
            for folder in root.glob('00*_*'):
                (folder/'runs').mkdir(exist_ok=True);(folder/'scores').mkdir(exist_ok=True)
                (folder/'usage_ledger.jsonl').write_text('')
            preregister(case)
            fixture=ResponseFixture()
            def http(client,body):return fixture.reply(client,body)
            # Fresh non-secret values satisfy only the normal presence check;
            # _http is patched, so they can never leave this process.
            env={'PACKY_API_KEY':'fixture-'+uuid.uuid4().hex,'ARK_API_KEY':'fixture-'+uuid.uuid4().hex}
            with patch.dict(os.environ,env),patch.object(Client,'_http',http),redirect_stdout(io.StringIO()):
                cli.pipeline(case)
                first_calls=len(fixture.calls)
                frozen=(case/'rubric/freeze.json').read_bytes()
                events={name:(case/'runs'/name/'author_events.jsonl').read_bytes() for name in CALIBRATION_RUNS+FORMAL_RUNS}
                ledger=(case/'usage_ledger.jsonl').read_bytes()
                self.assertTrue(read_json(case/'runs/api_preflight/preflight.json')['passed'])
                self.assertTrue(read_json(case/'scores/calibration_gate.json')['passed'])
                self.assertGreater(first_calls,50)
                result=validate_case(case)
                self.assertEqual(len(result['runs']),6)
                for name in CALIBRATION_RUNS+FORMAL_RUNS:
                    summary=read_json(case/'runs'/name/'summary.json')
                    self.assertEqual(summary['status'],'completed');self.assertFalse(summary['mock'])
                    self.assertEqual(summary['simulation_round_count'],3)
                    stages={e['stage_id'] for e in jsonl(case/'runs'/name/'author_events.jsonl')}
                    self.assertTrue({'r1_examination','r2_examination','r3_closing'}<=stages)
                    score=read_json(case/'scores'/name/'score_summary.json')
                    self.assertEqual(set(score['models']),{'grader_gpt','grader_seed'})
                    self.assertEqual(score['quality_pending'],0)
                    self.assertEqual(len(read_json(case/'runs'/name/'jury_record.json')['votes']),6)
                for name in FORMAL_RUNS:
                    candidate=name.split('_')[0]
                    self.assertTrue(any(run==name and route==candidate for run,route,_ in fixture.calls))
                # Repeat the real entrypoint, not a direct cache unit test.
                cli.pipeline(case)
                self.assertEqual(len(fixture.calls),first_calls)
                self.assertEqual((case/'rubric/freeze.json').read_bytes(),frozen)
                self.assertEqual((case/'usage_ledger.jsonl').read_bytes(),ledger)
                for name,before in events.items():self.assertEqual((case/'runs'/name/'author_events.jsonl').read_bytes(),before)
                exported=export_case(case,root/'deliverables')
                self.assertEqual(exported['status'],'verified_final_records')
                with zipfile.ZipFile(exported['path']) as archive:
                    self.assertIsNone(archive.testzip());members=set(archive.namelist())
                    self.assertIn('legal_agent_3round/shared_engine/cli.py',members)
                    self.assertIn('legal_agent_3round/005_Williams/scores/gpt_r1/grader_gpt.json',members)
                    self.assertIn('legal_agent_3round/DELIVERY_MANIFEST.json',members)
                    archive.extractall(Path(td)/'extracted')
                extracted=Path(td)/'extracted/legal_agent_3round'
                command=[sys.executable,'-I',str(extracted/'005_Williams/engine/run.py'),'status']
                check=subprocess.run(command,cwd=extracted,capture_output=True,text=True,timeout=30)
                self.assertEqual(check.returncode,0,check.stderr)
                self.assertIn('CA-PILOT-005',check.stdout)
                # Genuine final-export validation must reject missing and
                # arithmetically corrupted scoring evidence.
                grader=case/'scores/gpt_r1/grader_seed.json';original=grader.read_bytes();grader.unlink()
                try:
                    with self.assertRaises(EvidenceError):export_case(case,root/'deliverables')
                finally:grader.write_bytes(original)
                summary_path=case/'scores/gpt_r1/score_summary.json';original=summary_path.read_bytes()
                corrupted=json.loads(original);corrupted['mean_percent']+=1;atomic(summary_path,corrupted)
                try:
                    with self.assertRaises(EvidenceError):export_case(case,root/'deliverables')
                finally:summary_path.write_bytes(original)
            # There were no external calls: every received response is the
            # fixture marker, and all such records disappear with this temp dir.
            for name in CALIBRATION_RUNS+FORMAL_RUNS:
                self.assertTrue(all(row.get('response',{}).get('test_fixture') is True for row in jsonl(case/'runs'/name/'model_io.jsonl') if row.get('accepted')))


if __name__=='__main__':unittest.main(verbosity=2)
