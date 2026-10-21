"""Responses API, truthful client identity, persistent request cache and token ledger."""
from __future__ import annotations
import json, os, time, uuid, urllib.request, urllib.error
from pathlib import Path
from .store import *

class APIError(RunError): pass
MODEL_DEFAULTS={'gpt':'gpt-5.6-sol','seed':'doubao-seed-evolving','environment':'gpt-6.1-sol','grader_gpt':'gpt-6-sol','grader_seed':'doubao-seed-evolving'}

def usage_fields(usage, reserved_upper_bound):
    """Validate telemetry without coercing bools, strings, nulls or negatives.

    A nonempty usage object is not proof that the response was fully accounted
    for. Preserve trustworthy lower bounds, and reserve the remainder of this
    request's conservative upper bound whenever required telemetry is missing.
    Optional cache/reasoning details may be absent, but malformed details make
    the whole telemetry record incomplete rather than quietly recording zero.
    """
    errors=[];missing_details=[]
    if not isinstance(usage,dict):
        errors.append('usage must be an object');usage={}
    def integer(value):return isinstance(value,int) and not isinstance(value,bool) and value>=0
    values={}
    for name in ('input_tokens','output_tokens','total_tokens'):
        value=usage.get(name)
        if not integer(value):errors.append(name+' missing or not a nonnegative integer');values[name]=None
        else:values[name]=value
    inp,out,total=(values[x] for x in ('input_tokens','output_tokens','total_tokens'))
    if all(x is not None for x in (inp,out,total)) and total!=inp+out:errors.append('total_tokens differs from input_tokens + output_tokens')
    details={}
    for parent,key,limit in (('input_tokens_details','cached_tokens',inp),('output_tokens_details','reasoning_tokens',out)):
        detail=usage.get(parent)
        if detail is None or isinstance(detail,dict) and key not in detail:
            details[key]=None;missing_details.append(key);continue
        value=detail.get(key) if isinstance(detail,dict) else None
        if not integer(value) or limit is not None and value>limit:
            details[key]=None;errors.append(parent+'.'+key+' malformed or greater than its token subtotal')
        else:details[key]=value
    lower_bound=max(total or 0,(inp or 0)+(out or 0));complete=not errors
    return {**values,**details,'total_tokens':lower_bound,'usage_complete':complete,'usage_unknown':not complete,
            'reserved_unknown_tokens':0 if complete else max(0,reserved_upper_bound-lower_bound),
            'usage_validation_errors':errors,'usage_optional_details_missing':missing_details,
            'usage_attribution':usage.get('attribution',{})}

def safe_log(value):
    if isinstance(value,str):return redact(value)
    if isinstance(value,list):return [safe_log(x) for x in value]
    if isinstance(value,dict):return {redact(k) if isinstance(k,str) else k:safe_log(v) for k,v in value.items()}
    return value

class Ledger:
    def __init__(self,case_root,run_dir,limits=None):
        self.case_root=Path(case_root);self.run_dir=Path(run_dir)
        self.limits=limits or {'run_tokens':30000000,'case_tokens':200000000,'context_tokens':150000}
    def totals(self):
        rows=jsonl(self.case_root/'usage_ledger.jsonl')
        recorded={r.get('call_id') for r in rows if r.get('call_id')}
        pending_paths=set((self.case_root/'runs').glob('*/call_inflight.json'))|{self.run_dir/'call_inflight.json'}
        pending=[read_json(p) for p in pending_paths if p.exists()]
        pending=[r for r in pending if r.get('call_id') not in recorded]
        return {'case_tokens':sum(r.get('total_tokens',0) for r in rows),
                'run_tokens':sum(r.get('total_tokens',0) for r in rows if r.get('run_id')==self.run_dir.name),
                'case_reserved_unknown':sum(r.get('reserved_unknown_tokens',0) for r in rows)+sum(r['reserved_unknown_tokens'] for r in pending),
                'run_reserved_unknown':sum(r.get('reserved_unknown_tokens',0) for r in rows if r.get('run_id')==self.run_dir.name)+sum(r['reserved_unknown_tokens'] for r in pending if r['run_id']==self.run_dir.name),
                'calls':sum(1 for r in rows if r.get('run_id')==self.run_dir.name),'unknown_usage_calls':sum(r.get('usage_unknown',False) for r in rows if r.get('run_id')==self.run_dir.name),
                'inflight_unknown_calls':sum(r['run_id']==self.run_dir.name for r in pending)}
    def check(self,input_bytes=0,max_output=0):
        t=self.totals()
        if input_bytes+max_output>self.limits['context_tokens']:raise BudgetError('输入保守UTF-8字节上界加预留输出超过150K上下文限额；需检索式取记录，不能截掉记录后评分')
        if t['run_tokens']+t['run_reserved_unknown']+input_bytes+max_output>self.limits['run_tokens']:raise BudgetError('单次运行token预算不足')
        if t['case_tokens']+t['case_reserved_unknown']+input_bytes+max_output>self.limits['case_tokens']:raise BudgetError('本题总token预算不足')
    def record(self,row):
        row=safe_log({**row,'run_id':self.run_dir.name,'ts':now()})
        with lock(self.case_root/'.ledger.lock'):append(self.case_root/'usage_ledger.jsonl',row)
        atomic(self.run_dir/'usage.json',self.totals())

class Client:
    def __init__(self,case_root,run_dir,route,config=None,transport=None,sleeper=time.sleep):
        self.root=Path(case_root);self.run=Path(run_dir);self.route=route;self.config=config or {}
        self.model=self.config.get('models',{}).get(route,MODEL_DEFAULTS[route])
        self.provider='ark' if route in ('seed','grader_seed') else 'packy'
        self.effort='high' if route.startswith('grader') else 'medium'
        self.ledger=Ledger(self.root,self.run,self.config.get('limits'))
        self.transport=transport;self.sleep=sleeper
        (self.run/'call_cache').mkdir(parents=True,exist_ok=True)
    def endpoint(self):
        prefix='ARK' if self.provider=='ark' else 'PACKY'
        return os.environ.get(prefix+'_BASE_URL','https://ark.cn-beijing.volces.com/api/v3' if prefix=='ARK' else 'https://cf.api.fan/v1').rstrip('/')+'/responses'
    def stop_if_requested(self):
        if (self.root/'STOP_REQUESTED.json').exists():
            from .store import StopRequested
            raise StopRequested('收到安全停止请求；当前没有在途HTTP调用，已保留检查点和用量。')
    def recover_inflight(self):
        """An interrupted paid attempt must never disappear from the budget."""
        path=self.run/'call_inflight.json';pending=read_json(path)
        if not pending:return
        existing=[r for r in jsonl(self.root/'usage_ledger.jsonl') if r.get('call_id')==pending['call_id']]
        if not existing:
            self.ledger.record({**pending,'ok':False,'total_tokens':0,'usage_complete':False,'usage_unknown':True,'recovered_interruption':True,'latency_seconds':None})
            row={'request_hash':pending['request_hash'],'call_id':pending['call_id'],'kind':'transport_error','code':None,'detail':'恢复未结算的在途调用；是否计费未知，保留完整预留。','role':pending.get('role','')}
            append(self.run/'call_attempts.jsonl',row)
            append(self.run/'model_io.jsonl',{**row,'accepted':False})
        path.unlink();fsync_directory(self.run)
        atomic(self.run/'usage.json',self.ledger.totals())
    def settle(self,row):
        self.ledger.record(row)
        path=self.run/'call_inflight.json'
        if path.exists():
            if read_json(path)['call_id']!=row['call_id']:raise EvidenceError('在途调用标识冲突，保留预算证据并暂停')
            path.unlink();fsync_directory(self.run)
        atomic(self.run/'usage.json',self.ledger.totals())
    def call(self,instructions,payload,schema,role='',effort=None):
        if not instructions.strip():raise ProtocolError('instructions 必须显式提供')
        self.recover_inflight()
        effort=effort or self.effort
        body={'model':self.model,'instructions':instructions,'input':canonical(payload),'reasoning':{'effort':effort},
              'max_output_tokens':32768 if self.provider=='ark' else 16384,
              'text':{'format':{'type':'json_schema','name':'legal_response','schema':schema,'strict':True}}}
        if self.provider=='ark':body['store']=False
        endpoint=self.endpoint();request_id=digest({'route':self.route,'endpoint':endpoint,'body':body});cache=self.run/'call_cache'/f'{request_id}.json'
        saved=read_json(cache)
        if saved:
            allowed={self.model,'doubao-seed-evolving-latest-version'} if self.model=='doubao-seed-evolving' else {self.model}
            expected={'cache_version':2,'request_hash':request_id,'route':self.route,'model':self.model,'endpoint':endpoint}
            if any(saved.get(k)!=v for k,v in expected.items()) or saved.get('observed_model') not in allowed or saved.get('parsed_sha256')!=digest(saved.get('parsed')):raise EvidenceError('调用缓存身份、请求或内容校验失败，不能直接采用')
            validate_schema(saved['parsed'],schema)
            if not any(r.get('call_id')==saved.get('call_id') for r in jsonl(self.root/'usage_ledger.jsonl')):raise EvidenceError('缓存缺少对应用量记录，不能把已调用结果当作免费')
            if role!='R_PREFLIGHT' or saved.get('usage_complete') is True:return saved['parsed']
        attempts=[x for x in jsonl(self.run/'call_attempts.jsonl') if x.get('request_hash')==request_id]
        network_attempt=sum(x.get('kind')=='transport_error' for x in attempts)
        format_attempt=sum(x.get('kind')=='format_error' for x in attempts)
        if network_attempt>=4 or format_attempt>=2:raise APIError('该请求累计尝试耗尽；原日志保留，需明确的新恢复批次，不能悄悄重置额度')
        while True:
            self.stop_if_requested()
            if self.transport is None:
                prefix='ARK' if self.provider=='ark' else 'PACKY'
                if not os.environ.get(prefix+'_API_KEY'):raise APIError(prefix+'_API_KEY 未设置；尚未发送HTTP请求，不计调用费用')
            original=canonical(body).encode()
            start=time.monotonic();raw=None
            call_id=uuid.uuid4().hex
            pending={'call_id':call_id,'run_id':self.run.name,'request_hash':request_id,'route':self.route,'model':self.model,'effort':effort,'role':role,'reserved_unknown_tokens':len(original)+body['max_output_tokens'],'started_at':now()}
            with lock(self.root/'.budget_reservation.lock'):
                self.ledger.check(len(original),body['max_output_tokens'])
                atomic(self.run/'call_inflight.json',pending)
            try:
                raw=self.transport(body) if self.transport else self._http(body)
            except BaseException as ex:
                code=ex.code if isinstance(ex,urllib.error.HTTPError) else None
                retry=(code in (408,409,429,500,502,503,504)) if code is not None else isinstance(ex,(TimeoutError,urllib.error.URLError))
                # 401/403 为权限错误，不能当普通断流无限重试。
                row={'request_hash':request_id,'call_id':call_id,'kind':'transport_error','code':code,'detail':redact(ex),'role':role}
                append(self.run/'call_attempts.jsonl',row)
                append(self.run/'model_io.jsonl',safe_log({**row,'instructions':instructions,'input':payload,'accepted':False}))
                self.settle({**pending,'ok':False,'total_tokens':0,'usage_complete':False,'usage_unknown':True,'interrupted':not isinstance(ex,Exception),'latency_seconds':round(time.monotonic()-start,3)})
                if not isinstance(ex,Exception):raise
                network_attempt+=1
                if not retry or network_attempt>=4:raise APIError(f'接口调用失败：{code or type(ex).__name__}；未推进法律流程') from ex
                self.sleep(min(60,2**network_attempt*2));continue
            usage=usage_fields(raw.get('usage') if isinstance(raw,dict) else None,pending['reserved_unknown_tokens'])
            self.settle({**pending,**usage,'ok':True,'latency_seconds':round(time.monotonic()-start,3)})
            if not isinstance(raw,dict):
                append(self.run/'model_io.jsonl',{'request_hash':request_id,'call_id':call_id,'accepted':False,'error':'response_not_object'})
                raise APIError('API回复不是对象；未知用量已保守预留')
            observed=raw.get('model')
            allowed={self.model}
            if self.model=='doubao-seed-evolving':allowed.add('doubao-seed-evolving-latest-version')
            if observed not in allowed:
                append(self.run/'model_io.jsonl',safe_log({'request_hash':request_id,'call_id':call_id,'role':role,'instructions':instructions,'input':payload,'response':raw,'accepted':False,'error':'model_identity_mismatch'}))
                raise APIError('实际返回模型标识缺失或与配置不符')
            if raw.get('status') in ('failed','cancelled','incomplete') or raw.get('error'):
                append(self.run/'model_io.jsonl',safe_log({'request_hash':request_id,'call_id':call_id,'role':role,'response':raw,'accepted':False,'error':'response_not_completed'}))
                raise APIError('API回复失败或未完成；不得采用其中的部分输出')
            try:
                texts=[c.get('text','') for m in raw.get('output',[]) if m.get('type')=='message' for c in m.get('content',[]) if c.get('type')=='output_text']
                parsed=safe_log(json.loads(''.join(texts) or raw.get('output_text','')))
                validate_schema(parsed,schema)
            except Exception as ex:
                format_attempt+=1
                append(self.run/'call_attempts.jsonl',{'request_hash':request_id,'kind':'format_error','detail':redact(ex)})
                append(self.run/'model_io.jsonl',safe_log({'request_hash':request_id,'call_id':call_id,'instructions':instructions,'input':payload,'response':raw,'accepted':False,'error':'format_error'}))
                if format_attempt>=2:raise ProtocolError('结构化回复连续两次无效；法律行动未扣次数') from ex
                continue
            append(self.run/'call_attempts.jsonl',{'request_hash':request_id,'call_id':call_id,'kind':'success','usage_complete':usage['usage_complete']})
            append(self.run/'model_io.jsonl',safe_log({'request_hash':request_id,'call_id':call_id,'role':role,'model':self.model,'effort':effort,'instructions':instructions,'input':payload,'response':raw,'parsed':parsed,'accepted':True}))
            if role=='R_PREFLIGHT' and not usage['usage_complete']:raise APIError('接口预检回复的usage字段不完整或不一致；已保守预留用量，不能开始正式实验')
            atomic(cache,{'cache_version':2,'request_hash':request_id,'call_id':call_id,'route':self.route,'model':self.model,'observed_model':observed,'endpoint':endpoint,'usage_complete':usage['usage_complete'],'parsed_sha256':digest(parsed),'parsed':parsed});return parsed
    def _http(self,body):
        prefix='ARK' if self.provider=='ark' else 'PACKY'
        key=os.environ.get(prefix+'_API_KEY')
        if not key:raise APIError(prefix+'_API_KEY 未设置')
        req=urllib.request.Request(self.endpoint(),data=canonical(body).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+key,'User-Agent':'legal-benchmark/teacher-aligned-1.0'},method='POST')
        with urllib.request.urlopen(req,timeout=900 if prefix=='ARK' else 300) as resp:return json.load(resp)

def validate_schema(v,s):
    types=s.get('type');types=[types] if isinstance(types,str) else types
    ok={'object':isinstance(v,dict),'array':isinstance(v,list),'string':isinstance(v,str),'integer':isinstance(v,int) and not isinstance(v,bool),'number':isinstance(v,(int,float)) and not isinstance(v,bool),'boolean':isinstance(v,bool),'null':v is None}
    if types and not any(ok.get(t,False) for t in types):raise ProtocolError('schema类型不匹配')
    if 'enum' in s and v not in s['enum']:raise ProtocolError('schema枚举不匹配')
    if isinstance(v,dict):
        if any(k not in v for k in s.get('required',[])):raise ProtocolError('schema缺字段')
        if s.get('additionalProperties') is False and set(v)-set(s.get('properties',{})):raise ProtocolError('schema额外字段')
        for k,x in v.items():
            if k in s.get('properties',{}):validate_schema(x,s['properties'][k])
    if isinstance(v,list):
        for x in v:validate_schema(x,s.get('items',{}))
