#!/usr/bin/env python3
"""Build the runnable source edition. Real experiment ZIPs use CLI export/export-all."""
from pathlib import Path
import argparse,csv,hashlib,io,json,os,subprocess,sys,tempfile,zipfile
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from shared_engine.store import read_json,atomic,Store
from shared_engine.engine import World
from shared_engine.freeze import verify_preregistered
from shared_engine.export import _scan_secrets


def release_files():
    result=[]
    for p in sorted(ROOT.rglob('*')):
        rel=p.relative_to(ROOT)
        if any(part.startswith('.') or part in ('__pycache__','deliverables','node_modules') for part in rel.parts):continue
        if p.is_file() and p.suffix not in ('.pyc','.tmp','.lock','.zip') and p.name not in ('active_pipeline.json','background.json','STOP_REQUESTED.json','pipeline.log'):
            if p.is_symlink():raise ValueError('Source release cannot contain symlinks: '+str(rel))
            result.append(p)
    return result


def build(out):
    report=read_json(ROOT/'docs/verification_report.json',{})
    if not report.get('success'):raise ValueError('Complete offline test suite must pass before release')
    for case in sorted(ROOT.glob('00*_*')):
        verify_preregistered(case)
        for name in ('calibration_reference_offline','calibration_weak_offline'):
            run=case/'runs'/name;s=Store(run,World(case/'world').initial())
            if s.state['status']!='completed':raise ValueError('Offline sample incomplete')
        if list((case/'runs').glob('gpt_r*')) or list((case/'runs').glob('seed_r*')):
            raise ValueError('Use bash export_all.sh for real experiment results; this command builds the source edition')
    payloads={str(p.relative_to(ROOT)):p.read_bytes() for p in release_files() if p.name!='release_files_sha256.csv'}
    _scan_secrets(payloads)
    buf=io.StringIO(newline='');w=csv.writer(buf);w.writerow(['path','bytes','sha256'])
    for name,data in payloads.items():w.writerow([name,len(data),hashlib.sha256(data).hexdigest()])
    payloads['docs/release_files_sha256.csv']=buf.getvalue().encode('utf-8-sig')
    out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for name,data in payloads.items():
            info=zipfile.ZipInfo('legal_agent_3round/'+name);info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=(0o100755 if name.endswith('.sh') else 0o100644)<<16;z.writestr(info,data)
    with zipfile.ZipFile(out) as z:
        if z.testzip() is not None:raise ValueError('ZIP CRC failure')
        for name,data in payloads.items():
            if z.read('legal_agent_3round/'+name)!=data:raise ValueError('ZIP byte mismatch')
    return {'path':str(out.resolve()),'bytes':out.stat().st_size,'sha256':hashlib.sha256(out.read_bytes()).hexdigest(),'files':len(payloads),'edition':'runnable_source_with_offline_evidence','real_api_calls':0}


def verify_extracted(out):
    report={'checks':[],'real_api_calls':0,'success':False}
    with tempfile.TemporaryDirectory(prefix='legal_v2_extract_') as td:
        with zipfile.ZipFile(out) as z:z.extractall(td)
        root=Path(td)/'legal_agent_3round'
        cmds=[['bash','setup.sh'],['bash','run_005.sh','status'],['bash','run_006.sh','status'],['bash','run_005.sh','offline'],['bash','run_006.sh','offline'],['.venv/bin/python','-I','005_Williams/engine/run.py','status'],['.venv/bin/python','-I','006_Wilson/engine/run.py','status'],['bash','export_all.sh','--allow-partial']]
        for cmd in cmds:
            r=subprocess.run(cmd,cwd=root,capture_output=True,text=True,timeout=600)
            report['checks'].append({'command':cmd,'exit_code':r.returncode,'stdout_tail':r.stdout[-1600:],'stderr_tail':r.stderr[-2200:]})
            if r.returncode:raise RuntimeError(json.dumps(report,ensure_ascii=False,indent=2))
        # Read-only replay of a completed offline sample must not create legal events.
        for c in ('005_Williams','006_Wilson'):
            for name in ('calibration_reference_offline','calibration_weak_offline'):
                rel=Path(c)/'runs'/name/'author_events.jsonl'
                if (root/rel).read_bytes()!=(ROOT/rel).read_bytes():raise ValueError('Offline resume changed completed event records')
        denied=subprocess.run(['bash','export_all.sh'],cwd=root,capture_output=True,text=True,timeout=60)
        if denied.returncode==0:raise ValueError('Offline source incorrectly exported as final')
        report['checks'].append({'command':['bash','export_all.sh'],'expected':'refuse unfinished final export','exit_code':denied.returncode,'stderr_tail':denied.stderr[-800:]})
    report['success']=True;return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--verify-extracted',action='store_true');a=p.parse_args();result=build(a.out)
    if a.verify_extracted:
        r=verify_extracted(a.out);atomic(ROOT.parent/'v2_extraction_verification.json',r);result['extraction_verified']=True
    print(json.dumps(result,ensure_ascii=False,indent=2))
