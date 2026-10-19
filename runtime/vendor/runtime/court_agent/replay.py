"""Local, static review page. No CDNs, external URLs or JavaScript execution."""
from html import escape
import json
from pathlib import Path


def write_replay(out, engine):
    rows = []
    for e in engine.events:
        details = escape(json.dumps(e["meta"], ensure_ascii=False, indent=2))
        rows.append(f"<article><small>{escape(e['event_id'])} · {escape(e['stage_id'])} · "
                    f"{escape(e['actor'])}</small><h3>{escape(e['kind'])}</h3>"
                    f"<p>{escape(e['text'])}</p><div class='tags'>可见角色："
                    f"{escape(', '.join(e['audience']))}</div>"
                    f"<details><summary>引用与状态</summary><pre>{escape(str(e['references']))}\n"
                    f"{details}</pre></details></article>")
    html = """<!doctype html><html lang='zh-CN'><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<meta http-equiv='Content-Security-Policy' content="default-src 'none'; style-src 'unsafe-inline'">
<title>庭审运行回放</title><style>
body{font:16px/1.65 system-ui,sans-serif;background:#f3f5f8;color:#1e293b;margin:0;padding:32px}
main{max-width:1000px;margin:auto}h1{margin-bottom:8px}small,.tags{color:#64748b}
article{background:white;margin:16px 0;padding:20px;border:1px solid #d8e0ea;border-radius:10px}
h3{margin:4px 0 10px}p,pre{white-space:pre-wrap;overflow-wrap:anywhere}pre{font-size:12px}
.notice{background:#fff2ca;padding:16px;border-radius:10px}summary{cursor:pointer}
</style><main>"""
    html += f"<h1>{escape(engine.case.case_id)} · 庭审运行回放</h1>"
    html += f"<p>状态：{escape(engine.status)} ｜ 模式：{escape(engine.config.get('mode','live'))}</p>"
    html += ("<p class='notice'>作者调试视图，包含律师私下咨询和工具记录，不能直接提供给庭审角色。"
             "Mock 结果仅验证流程；模拟裁决不是标准答案。本页不计算正式 benchmark 分数。</p>")
    html += "".join(rows) + "</main></html>"
    (Path(out) / "replay.html").write_text(html, encoding="utf-8")
