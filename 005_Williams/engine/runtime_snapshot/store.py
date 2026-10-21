"""Atomic checkpoints, recoverable WAL, immutable hash-chain events."""
from __future__ import annotations
import contextlib, copy, fcntl, hashlib, json, os, re, time, tempfile
from pathlib import Path

class RunError(RuntimeError): pass
class ProtocolError(RunError): pass
class BudgetError(RunError): pass
class EvidenceError(RunError): pass
class StopRequested(RunError): pass

def canonical(v): return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
def digest(v): return hashlib.sha256((v if isinstance(v,bytes) else canonical(v).encode())).hexdigest()
def now(): return time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
def read_json(p, default=None):
    p=Path(p)
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else copy.deepcopy(default)
def fsync_directory(p):
    fd=os.open(str(p),os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
    try:os.fsync(fd)
    finally:os.close(fd)

def atomic_text(p,text):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+p.name+'.',suffix='.tmp',dir=p.parent);tmp=Path(name)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            written=f.write(text)
            if written!=len(text):raise EvidenceError('原子写入长度不一致')
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,p);fsync_directory(p.parent)
    finally:
        if tmp.exists():tmp.unlink()

def atomic(p,v):atomic_text(p,json.dumps(v,ensure_ascii=False,indent=2))
def jsonl(p):
    p=Path(p)
    if not p.exists():return []
    return [json.loads(x) for x in p.read_text(encoding='utf-8').splitlines() if x.strip()]
def append(p,v):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('a',encoding='utf-8') as f:
        f.write(canonical(v)+'\n');f.flush();os.fsync(f.fileno())
def safe_name(name):
    if not isinstance(name,str) or not name or len(name)>160 or Path(name).name!=name or name in ('.','..') or '\\' in name or '\x00' in name:
        raise ProtocolError('文件名只能是单个安全文件名')
    return name

def redact(s):
    s=str(s)
    for key in ('PACKY_API_KEY','ARK_API_KEY'):
        value=os.environ.get(key)
        if value:s=s.replace(value,'[REDACTED]')
    return re.sub(r'(?:sk-[A-Za-z0-9_-]{12,}|ark-[A-Za-z0-9_-]{16,})','[REDACTED]',s)

@contextlib.contextmanager
def lock(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+') as f:
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RunError('该运行已由其他进程持有；不要重复启动')
        try:yield
        finally:fcntl.flock(f,fcntl.LOCK_UN)

class Store:
    def __init__(self,path,initial):
        self.path=Path(path);self.path.mkdir(parents=True,exist_ok=True)
        self.wal=self.path/'transaction.json'
        self.recover()
        self.events=jsonl(self.path/'author_events.jsonl');self.verify()
        self.state=read_json(self.path/'state.json',initial)
        if self.state.get('last_hash','0'*64)!=(self.events[-1]['hash'] if self.events else '0'*64):
            raise EvidenceError('checkpoint 与事件链不一致')
        for name,a in self.state.get('artifacts',{}).items():
            safe_name(name);latest=next((e for e in reversed(self.events) if e['kind']=='artifact_written' and e['meta'].get('filename')==name),None)
            if not latest or latest['meta']['version']!=a['version'] or latest['meta']['sha256']!=digest(a['content']):raise EvidenceError('工作文件状态与已提交事件不一致')
            version=self.path/'artifacts/versions'/f'{a["version"]:03d}'/name
            if version.exists() and version.read_bytes().decode('utf-8')!=a['content']:raise EvidenceError('不可变工作文件版本被改动')
            if not version.exists():atomic_text(version,a['content'])
            current=self.path/'artifacts'/name
            if not current.exists() or current.read_bytes().decode('utf-8')!=a['content']:atomic_text(current,a['content'])
    def verify(self):
        prev='0'*64
        for i,e in enumerate(self.events,1):
            payload={k:v for k,v in e.items() if k!='hash'}
            if e['seq']!=i or e['prev_hash']!=prev or digest(payload)!=e['hash']:raise EvidenceError('事件哈希链校验失败')
            prev=e['hash']
        return True
    def recover(self):
        if not self.wal.exists():return
        tx=read_json(self.wal);existing=jsonl(self.path/'author_events.jsonl')
        if 'journal' in tx:
            journal=tx['journal'];prev='0'*64
            for i,e in enumerate(journal,1):
                if e['seq']!=i or e['prev_hash']!=prev or digest({k:v for k,v in e.items() if k!='hash'})!=e['hash']:raise EvidenceError('事务中的完整事件链无效')
                prev=e['hash']
            if len(existing)>len(journal) and existing[:len(journal)]==journal:
                # A stale WAL may survive its directory unlink. Discard only
                # when a validated later checkpoint proves it was superseded.
                prev='0'*64
                for i,e in enumerate(existing,1):
                    if e['seq']!=i or e['prev_hash']!=prev or digest({k:v for k,v in e.items() if k!='hash'})!=e['hash']:raise EvidenceError('较新日志未通过完整链校验')
                    prev=e['hash']
                current=read_json(self.path/'state.json',{})
                if current.get('last_hash')!=prev:raise EvidenceError('较新日志没有对应检查点，不能丢弃旧事务')
                self.wal.unlink();fsync_directory(self.path);return
            if len(existing)>len(journal) or existing!=journal[:len(existing)]:raise EvidenceError('事务恢复遇到历史缺口或冲突；保留证据并暂停')
        else:
            # Read old WALs, but never append across a missing sequence range.
            journal=existing[:]
            for e in tx['events']:
                if e['seq']<=len(journal):
                    if journal[e['seq']-1]!=e:raise EvidenceError('事务恢复遇到冲突事件')
                else:
                    if e['seq']!=len(journal)+1:raise EvidenceError('事务恢复检测到事件序号缺口')
                    journal.append(e)
        # Replace the complete journal atomically. Its prefix is byte-equivalent
        # and remains immutable; a process interruption cannot leave a half append.
        p=self.path/'author_events.jsonl';atomic_text(p,''.join(canonical(e)+'\n' for e in journal))
        if jsonl(p)!=journal:raise EvidenceError('日志落盘回读不一致；保留事务并暂停')
        for a in tx.get('artifacts',[]):
            fn=self.path/a['path'];fn.parent.mkdir(parents=True,exist_ok=True)
            atomic_text(fn,a['text'])
        atomic(self.path/'state.json',tx['state']);self.wal.unlink();fsync_directory(self.path)
    def commit(self,state,events,artifacts=None):
        prev=self.events[-1]['hash'] if self.events else '0'*64;seq=len(self.events)
        rows=[]
        for r in events:
            seq+=1;e={'event_id':f'E{seq:06d}','seq':seq,'stage_id':state['stage_id'],'round':state['round'],
                       'kind':r['kind'],'actor':r.get('actor','trusted_orchestrator'),'text':r.get('text',''),
                       'references':r.get('references',[]),'audience':r.get('audience',['R_DEFENSE','R_PROSECUTOR','R_JUDGE']),
                       'meta':r.get('meta',{}),'ts':now(),'prev_hash':prev}
            e['hash']=digest(e);prev=e['hash'];rows.append(e)
        state['last_hash']=prev
        atomic(self.wal,{'state':state,'events':rows,'journal':self.events+rows,'artifacts':artifacts or []});self.recover()
        self.state=copy.deepcopy(state);self.events.extend(rows)
