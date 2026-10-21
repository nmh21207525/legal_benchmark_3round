"""Targeted regressions for accounting, interrupted calls and cache integrity.

All transports are local stubs. No API credentials or real network are used.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from .llm import APIError,Client,Ledger,usage_fields
from .store import BudgetError,EvidenceError,StopRequested,atomic,canonical,jsonl,read_json

TEXT_SCHEMA={'type':'object','properties':{'text':{'type':'string'}},'required':['text'],'additionalProperties':False}

class TransportV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name);self.run=self.root/'runs'/'test'
    def tearDown(self):self.tmp.cleanup()
    def response(self,usage=None,**overrides):
        return {'model':'gpt-5.6-sol','output_text':'{"text":"ok"}','usage':{'input_tokens':100,'output_tokens':20,'total_tokens':120} if usage is None else usage,**overrides}
    def client(self,transport):return Client(self.root,self.run,'gpt',transport=transport,sleeper=lambda _:None)
    def test_partial_and_invalid_telemetry_keeps_conservative_budget(self):
        cases=[{},None,[],{'input_tokens':123},{'input_tokens_details':{'cached_tokens':1}}]
        for field in ('input_tokens','output_tokens','total_tokens'):
            for value in (True,-1,None,'100',1.5):
                cases.append({**{'input_tokens':100,'output_tokens':20,'total_tokens':120},field:value})
        cases.append({'input_tokens':100,'output_tokens':20,'total_tokens':119})
        for usage in cases:
            with self.subTest(usage=usage):
                result=usage_fields(usage,17000)
                self.assertFalse(result['usage_complete']);self.assertTrue(result['usage_unknown'])
                self.assertGreaterEqual(result['total_tokens']+result['reserved_unknown_tokens'],17000)
                self.assertTrue(result['usage_validation_errors'])
    def test_valid_core_usage_and_optional_details(self):
        result=usage_fields({'input_tokens':100,'output_tokens':20,'total_tokens':120},17000)
        self.assertTrue(result['usage_complete']);self.assertEqual(result['reserved_unknown_tokens'],0)
        self.assertIsNone(result['cached_tokens']);self.assertIsNone(result['reasoning_tokens'])
        full=usage_fields({'input_tokens':100,'output_tokens':20,'total_tokens':120,'input_tokens_details':{'cached_tokens':99},'output_tokens_details':{'reasoning_tokens':19}},17000)
        self.assertTrue(full['usage_complete']);self.assertEqual(full['cached_tokens'],99)
        for detail,key,bad in [('input_tokens_details','cached_tokens',101),('output_tokens_details','reasoning_tokens',21),('input_tokens_details','cached_tokens',True)]:
            result=usage_fields({'input_tokens':100,'output_tokens':20,'total_tokens':120,detail:{key:bad}},17000)
            self.assertFalse(result['usage_complete'])
    def test_preflight_rejects_partial_usage_without_poisoning_cache(self):
        seen=[]
        def transport(body):
            seen.append(body)
            return self.response({'input_tokens_details':{'cached_tokens':1}}) if len(seen)==1 else self.response()
        c=self.client(transport)
        with self.assertRaisesRegex(APIError,'usage'):c.call('explicit',{},TEXT_SCHEMA,'R_PREFLIGHT')
        self.assertFalse(list((self.run/'call_cache').glob('*.json')))
        row=jsonl(self.root/'usage_ledger.jsonl')[0]
        self.assertTrue(row['usage_unknown']);self.assertFalse(row['usage_complete']);self.assertGreater(row['reserved_unknown_tokens'],0)
        self.assertEqual(c.call('explicit',{},TEXT_SCHEMA,'R_PREFLIGHT'),{'text':'ok'})
        self.assertEqual(len(seen),2);self.assertTrue(jsonl(self.root/'usage_ledger.jsonl')[-1]['usage_complete'])
    def test_midrun_partial_usage_preserves_output_and_unknown_cost(self):
        seen=[]
        def transport(body):seen.append(body);return self.response({'input_tokens':100})
        c=self.client(transport)
        self.assertEqual(c.call('explicit',{},TEXT_SCHEMA),{'text':'ok'})
        self.assertEqual(c.call('explicit',{},TEXT_SCHEMA),{'text':'ok'})
        self.assertEqual(len(seen),1);self.assertGreater(c.ledger.totals()['run_reserved_unknown'],0)
    def test_keyboard_interrupt_is_accounted_before_reraising(self):
        def transport(body):raise KeyboardInterrupt()
        c=self.client(transport)
        with self.assertRaises(KeyboardInterrupt):c.call('explicit',{},TEXT_SCHEMA)
        rows=jsonl(self.root/'usage_ledger.jsonl')
        self.assertEqual(len(rows),1);self.assertTrue(rows[0]['interrupted']);self.assertTrue(rows[0]['usage_unknown'])
        self.assertFalse((self.run/'call_inflight.json').exists())
    def test_hard_interruption_recovery_is_durable_and_idempotent(self):
        c=self.client(lambda _:self.response())
        pending={'call_id':'test-pending','run_id':self.run.name,'request_hash':'request','route':'gpt','model':'gpt-5.6-sol','effort':'medium','role':'R_DEFENSE','reserved_unknown_tokens':17000}
        atomic(self.run/'call_inflight.json',pending)
        self.assertEqual(c.ledger.totals()['run_reserved_unknown'],17000)
        c.recover_inflight();c.recover_inflight()
        rows=jsonl(self.root/'usage_ledger.jsonl')
        self.assertEqual(len(rows),1);self.assertTrue(rows[0]['recovered_interruption'])
        self.assertEqual(c.ledger.totals()['run_reserved_unknown'],17000)
        self.assertEqual(len(jsonl(self.run/'call_attempts.jsonl')),1)
    def test_crash_after_ledger_write_does_not_double_account(self):
        c=self.client(lambda _:self.response())
        pending={'call_id':'settled-before-crash','run_id':self.run.name,'request_hash':'request','reserved_unknown_tokens':17000}
        atomic(self.run/'call_inflight.json',pending)
        c.ledger.record({**pending,'total_tokens':120,'reserved_unknown_tokens':0,'usage_unknown':False,'usage_complete':True})
        c.recover_inflight()
        self.assertEqual(len(jsonl(self.root/'usage_ledger.jsonl')),1)
        self.assertEqual(c.ledger.totals()['run_reserved_unknown'],0)
    def test_other_run_inflight_reservation_prevents_case_overspend(self):
        c=self.client(lambda _:self.response())
        atomic(self.root/'runs/other/call_inflight.json',{'call_id':'other','run_id':'other','reserved_unknown_tokens':99000})
        ledger=Ledger(self.root,self.run,{'run_tokens':100000,'case_tokens':100000,'context_tokens':150000})
        with self.assertRaises(BudgetError):ledger.check(1001,0)
    def test_nonobject_response_and_incomplete_status_are_not_accepted(self):
        c=self.client(lambda _:[])
        with self.assertRaises(APIError):c.call('non-object',{},TEXT_SCHEMA)
        self.assertTrue(jsonl(self.root/'usage_ledger.jsonl')[-1]['usage_unknown'])
        c=self.client(lambda _:self.response(status='incomplete'))
        with self.assertRaises(APIError):c.call('incomplete',{},TEXT_SCHEMA)
        self.assertFalse(list((self.run/'call_cache').glob('*.json')))
        self.assertFalse((self.run/'call_inflight.json').exists())
    def test_stop_before_transport_does_not_create_paid_call(self):
        seen=[];c=self.client(lambda b:seen.append(b))
        atomic(self.root/'STOP_REQUESTED.json',{'reason':'test'})
        with self.assertRaises(StopRequested):c.call('explicit',{},TEXT_SCHEMA)
        self.assertEqual(seen,[]);self.assertEqual(jsonl(self.root/'usage_ledger.jsonl'),[])
        self.assertFalse((self.run/'call_inflight.json').exists())
    def test_stop_between_retries_preserves_only_actual_attempt(self):
        seen=[]
        def transport(body):
            seen.append(body);atomic(self.root/'STOP_REQUESTED.json',{'reason':'test'})
            raise urllib.error.URLError('injected')
        c=self.client(transport)
        with self.assertRaises(StopRequested):c.call('explicit',{},TEXT_SCHEMA)
        self.assertEqual(len(seen),1);self.assertEqual(len(jsonl(self.root/'usage_ledger.jsonl')),1)
    def test_cache_content_tamper_is_rejected_without_new_call(self):
        seen=[]
        def transport(body):seen.append(body);return self.response()
        c=self.client(transport);c.call('explicit',{},TEXT_SCHEMA)
        path=next((self.run/'call_cache').glob('*.json'));entry=read_json(path);entry['parsed']['text']='modified';atomic(path,entry)
        with self.assertRaises(EvidenceError):c.call('explicit',{},TEXT_SCHEMA)
        self.assertEqual(len(seen),1)
    def test_cache_cannot_hide_missing_ledger(self):
        c=self.client(lambda _:self.response());c.call('explicit',{},TEXT_SCHEMA)
        (self.root/'usage_ledger.jsonl').unlink()
        with self.assertRaises(EvidenceError):c.call('explicit',{},TEXT_SCHEMA)
    def test_endpoint_change_cannot_reuse_old_cache(self):
        seen=[]
        def transport(body):seen.append(body);return self.response()
        c=self.client(transport)
        with patch.dict(os.environ,{'PACKY_BASE_URL':'https://first.invalid/v1'}):c.call('explicit',{},TEXT_SCHEMA)
        with patch.dict(os.environ,{'PACKY_BASE_URL':'https://second.invalid/v1'}):c.call('explicit',{},TEXT_SCHEMA)
        self.assertEqual(len(seen),2)
    def test_missing_key_has_no_request_or_unknown_charge(self):
        with patch.dict(os.environ,{},clear=True):
            c=Client(self.root,self.run,'gpt')
            with self.assertRaisesRegex(APIError,'未设置'):c.call('explicit',{},TEXT_SCHEMA)
        self.assertEqual(jsonl(self.root/'usage_ledger.jsonl'),[])
        self.assertFalse((self.run/'call_inflight.json').exists())
    def test_exact_credentials_are_redacted_from_io_and_cache(self):
        secret='testing-credential-not-a-real-secret'
        with patch.dict(os.environ,{'PACKY_API_KEY':secret}):
            c=self.client(lambda _:self.response(output_text=canonical({'text':secret})))
            self.assertEqual(c.call('explicit '+secret,{'accidental':secret,secret:'accidental key'},TEXT_SCHEMA),{'text':'[REDACTED]'})
        for path in self.root.rglob('*.json*'):
            self.assertNotIn(secret,path.read_text())

if __name__=='__main__':unittest.main(verbosity=2)
