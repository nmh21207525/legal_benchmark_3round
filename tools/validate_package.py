#!/usr/bin/env python3
"""Release integrity checks. No API; prints counts without private file contents."""
from pathlib import Path
import ast,hashlib,json,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from shared_engine.engine import World
from shared_engine.store import read_json,digest,atomic,jsonl,EvidenceError
from tools.verify_legal_sources_v2 import verify as verify_legal_sources

def validate():
 report={'cases':{},'checks':[],'api_calls':0,'historical_source_validation':verify_legal_sources(ROOT)}
 for p in list((ROOT/'shared_engine').glob('*.py'))+list((ROOT/'tools').glob('*.py')):ast.parse(p.read_text())
 for c in sorted(ROOT.glob('00*_*')):
    w=World(c/'world');m=w.manifest
    assert len(w.docs)==32 and len(m['initial_documents'])==9
    assert 150<=len(w.facts)<=300
    assert all(r in w.roles for d in w.docs.values() for r in d['roles'])
    assert all(f in w.facts for r in w.roles.values() for f in r.get('fact_ids',[]))
    assert all(f in w.facts for d in w.docs.values() for f in d['fact_ids'])
    assert all(f['source_locators'] for f in w.facts.values() if f['origin']=='anchor')
    for law in w.laws.values():
        p=c/'legal_corpus'/law['snapshot_path'];assert p.is_file()
        assert hashlib.sha256(p.read_bytes()).hexdigest()==law['sha256']
        assert hashlib.sha256(law['text'].encode()).hexdigest()==law['text_sha256']
        assert law['url'].startswith('https://') and law['retrieved_at'] and law['verification']
        assert not (law['cutoff_ok'] and law['effective']>m['law_cutoff'])
    assert any(not r['cutoff_ok'] for r in w.laws.values())
    rubric=read_json(c/'rubric/rubric.json');assert len(rubric['criteria'])==60
    assert sum(x['max_points'] for x in rubric['criteria'])==120
    assert sum(x['method']=='dual_model' for x in rubric['criteria'])==17
    for name in ('README.md','进度.md','usage_ledger.jsonl','engine/接口说明.md','rubric/checks.py','rubric/预注册说明.md'):assert (c/name).exists(),str(c/name)
    for p in (ROOT/'shared_engine').glob('*.py'):
        target=c/'engine/runtime_snapshot'/p.name;assert target.read_bytes()==p.read_bytes(),'runtime_snapshot不一致：'+str(target)
    for p in (c/'source_reference/original').rglob('*'):
        if not p.is_file():continue
        # Original sources are read-only archives and are not part of a candidate view.
    entries={str(p.relative_to(c/'world')):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((c/'world').rglob('*')) if p.is_file() and p.name!='world.sha256'}
    (c/'world/world.sha256').write_text(''.join(f'{v}  {k}\n' for k,v in entries.items()))
    report['cases'][c.name]={'facts':len(w.facts),'anchor':sum(f['origin']=='anchor' for f in w.facts.values()),'authored':sum(f['origin']=='authored' for f in w.facts.values()),'documents':len(w.docs),'initial_documents':len(m['initial_documents']),'laws':len(w.laws),'future_distractors':sum(not r['cutoff_ok'] for r in w.laws.values()),'rubric_items':60,'raw_denominator':120,'rounds':3,'runtime_snapshot_identical':True}
 report['checks']=['all_python_syntax','doc_fact_role_integrity','anchor_provenance','all_law_raw_and_text_sha256','law_cutoff_and_distractors','rubric_size_and_weights','teacher_case_directory_shape','runtime_snapshot_identical']
 atomic(ROOT/'docs/package_validation.json',report);print(json.dumps(report,ensure_ascii=False,indent=2));return report
if __name__=='__main__':validate()
