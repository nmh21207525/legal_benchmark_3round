"""Bounded model views; complete source records remain available through read tools."""
from __future__ import annotations
import json

PAGE_CHARS = 4000
VIEW_BYTES = 90000

def preview(text, max_bytes=1200):
    text=str(text)
    data=text.encode('utf-8')
    if len(data)<=max_bytes:return text
    return data[:max_bytes].decode('utf-8','ignore')+'\n[预览截断；使用分页读取工具获取完整内容]'

def compact(value, string_bytes=1200, list_limit=8):
    if isinstance(value,str):return preview(value,string_bytes)
    if isinstance(value,list):return [compact(x,string_bytes,list_limit) for x in value[-list_limit:]]
    if isinstance(value,dict):return {k:compact(v,string_bytes,list_limit) for k,v in value.items()}
    return value

def page(text, offset, **metadata):
    if offset<0 or offset>len(text):raise ValueError('分页偏移超出内容范围')
    end=min(len(text),offset+PAGE_CHARS)
    return {**metadata,'text':text[offset:end],'offset':offset,'end_offset':end,'total_characters':len(text),'next_offset':f'offset:{end}' if end<len(text) else None,'page_complete':end==len(text)}

def coverage_complete(coverage, key, start, end, total):
    ranges=sorted(coverage.get(key,[])+[[start,end]])
    merged=[]
    for lo,hi in ranges:
        if merged and lo<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],hi)
        else:merged.append([lo,hi])
    coverage[key]=merged
    return bool(merged and merged[0][0]==0 and merged[0][1]>=total)

def size(value):return len(json.dumps(value,ensure_ascii=False,separators=(',',':'),sort_keys=True).encode())
