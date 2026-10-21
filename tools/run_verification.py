#!/usr/bin/env python3
from pathlib import Path
import json,platform,sys,time,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from shared_engine import tests

class Result(unittest.TextTestResult):
 def __init__(self,*a,**kw):super().__init__(*a,**kw);self.passed=[]
 def addSuccess(self,t):super().addSuccess(t);self.passed.append(t.id())

if __name__=='__main__':
 start=time.monotonic();result=unittest.TextTestRunner(verbosity=2,resultclass=Result).run(unittest.defaultTestLoader.discover(str(ROOT/'shared_engine'), pattern='test*.py', top_level_dir=str(ROOT)))
 report={'run_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'python':platform.python_version(),'api_calls':0,'tests_run':result.testsRun,'passed_count':len(result.passed),'passed':result.passed,'failures':[str(t) for t,_ in result.failures],'errors':[str(t) for t,_ in result.errors],'skipped':[str(t) for t,_ in result.skipped],'elapsed_seconds':round(time.monotonic()-start,3),'success':result.wasSuccessful(),'scope':'离线结构、权限、证据、评分分母与故障注入；不代表真实API校准已通过。'}
 (ROOT/'docs/verification_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');sys.exit(not result.wasSuccessful())
