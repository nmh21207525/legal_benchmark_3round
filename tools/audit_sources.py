#!/usr/bin/env python3
"""Read every original ZIP entry; hash, parse and classify without executing it."""
from pathlib import Path
import argparse,ast,collections,csv,hashlib,io,json,re,subprocess,tempfile,zipfile

def entries(z,prefix=''):
 for info in z.infolist():
  if info.is_dir():continue
  data=z.read(info);name=prefix+info.filename
  yield name,data
  if Path(info.filename).suffix.lower()=='.zip':
   with zipfile.ZipFile(io.BytesIO(data)) as nested:yield from entries(nested,name+'::')

def audit_zip(path,out):
 rows=[];problems=[];counts=collections.Counter();out.mkdir(parents=True,exist_ok=True)
 with zipfile.ZipFile(path) as z:
  for name,data in entries(z):
   suffix=Path(name).suffix.lower();counts[suffix or '(none)']+=1
   row={'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'type':suffix,'inspection':'all_bytes_read','records':None,'details':None}
   try:
    if suffix in ('.json','.jsonl','.md','.txt','.py','.ts','.html','.csv','.sha256') or not suffix:
     try:text=data.decode('utf-8-sig')
     except UnicodeDecodeError:
      if suffix!='.html':raise
      # Legacy California chaptered bills are byte-preserved; decoding is only
      # for inspection. No round-trip changes to the source snapshot.
      text=data.decode('windows-1252');row['details']='legacy_html_windows_1252_decoded_for_inspection_only'
     if suffix=='.json':
      obj=json.loads(text);row.update(inspection='json_parsed',records=len(obj) if isinstance(obj,(dict,list)) else 1,details=','.join(list(obj)[:20]) if isinstance(obj,dict) else 'array')
     elif suffix=='.jsonl':
      records=[json.loads(l) for l in text.splitlines() if l.strip()];row.update(inspection='all_jsonl_records_parsed',records=len(records),details=';'.join(sorted({str(x.get('kind','')) for x in records if isinstance(x,dict)})))
     elif suffix=='.py':
      tree=ast.parse(text);row.update(inspection='all_python_ast_parsed_no_execution',records=len(text.splitlines()),details=','.join(n.name for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.ClassDef))))
     elif suffix=='.csv':row.update(inspection='all_csv_rows_parsed',records=len(list(csv.reader(io.StringIO(text)))))
     elif suffix=='.html':row.update(inspection='all_html_text_read',records=len(re.findall(r'<[^>]+>',text)),details='text_chars='+str(len(re.sub(r'<[^>]*>','',text))))
     else:row.update(inspection='all_text_read',records=len(text.splitlines()),details=';'.join(re.findall(r'^#{1,3} (.+)',text,re.M)[:12]))
    elif suffix=='.pdf':
     with tempfile.TemporaryDirectory() as td:
      p=Path(td)/'input.pdf';p.write_bytes(data);r=subprocess.run(['pdftotext','-layout',str(p),'-'],capture_output=True,check=True)
      row.update(inspection='all_pdf_pages_text_extracted',records=r.stdout.decode(errors='replace').count('\f'),details='extracted_text_sha256='+hashlib.sha256(r.stdout).hexdigest())
    elif suffix=='.zip':
     with zipfile.ZipFile(io.BytesIO(data)) as nested:row.update(inspection='nested_zip_all_bytes_crc_checked',records=len(nested.infolist()),details=str(nested.testzip()))
    else:row['inspection']='binary_all_bytes_hashed'
   except Exception as ex:row['inspection']='parse_issue_preserved';row['details']=type(ex).__name__;problems.append({'path':name,'error_type':type(ex).__name__})
   rows.append(row)
 with (out/'all_files.csv').open('w',newline='',encoding='utf-8-sig') as f:
  w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
 summary={'zip_name':path.name,'zip_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'files':len(rows),'uncompressed_bytes':sum(r['bytes'] for r in rows),'types':dict(counts),'parse_issues':problems,'inspection_note':'全部原ZIP文件读取、哈希并按类型解析；不把静态遍历说成逐一验证历史真实运行。关键规格、引擎、评分及版本问题另列标准对照。'}
 (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n');return summary

if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--teacher',type=Path,required=True);ap.add_argument('--original',type=Path,required=True);a=ap.parse_args();root=Path(__file__).resolve().parents[1]
 print(json.dumps(audit_zip(a.teacher,root/'teacher_reference/inventory'),ensure_ascii=False))
 print(json.dumps(audit_zip(a.original,root/'docs/original_005006_inventory'),ensure_ascii=False))
