#!/usr/bin/env python3
from pathlib import Path
import json,platform,shutil,sys
ROOT=Path(__file__).resolve().parents[1]

def build():
 if any(json.loads(p.read_text()).get('version','').startswith('teacher-three-round-v2') for p in ROOT.glob('00*_*/world/manifest.json')):
    raise SystemExit('这是v1历史生成器；v2文档已更新，拒绝覆盖。直接运行setup.sh与run_005.sh/run_006.sh。')
 for case in sorted(ROOT.glob('00*_*')):
    m=json.loads((case/'world/manifest.json').read_text());num=m['case_id'][-3:];counts=m['counts'];is5=num=='005'
    core='登记义务的基础、进入辖区与住所变化的起算、五个工作日、实际知情与故意。原型的不利事实——知道登记要求、未完成登记及未联系假释官——保留；声称尝试登记、旅馆威胁与独立印证分开。' if is5 else '医院观察、PAS 0.09%、不表示同意但允许抽血、样本0.12%、无令状抽血及两项23153指控。驾驶时浓度、酒精影响、违法行为或注意义务及致伤因果分别审查，不补造精确时刻和逆推参数。'
    gate='006的原始F10映射A010，连同D22全部实质内容均受M06闸门控制。即使round已经变成2，未到r2_examination的stage_release:M06也不能进入任何角色提示。后来披露不改写第一轮封闭记录。' if not is5 else '第二轮补充告知材料及第三轮代理人说明会改变准备范围。期限的手写提醒与法定工作日计算分开；机关查询的无登记结果不等于没有接触过窗口。'
    text=f'''# {m['case_id']}｜{m['title']}

人物为化名，新增案卷与细节为虚构模拟。历史题源保留在只读source_reference，候选只能使用受控视图；不把补写卷宗说成从真实案卷取得。

## 改了什么

原始{counts['anchors']}条锚点全部保留并带原文定位，新增{counts['authored']}条authored事实，共{counts['facts']}条、32份文书；初始只披露9份，占28.125%。每条事实有fact_id和来源类别，角色卡按个人知识、条件追问及轮次控制。世界另有表格、签名、查询、资质、监管链或第三方记录的缺口，既有有利线索，也有不能消除的不利材料。

本题核心是：{core}

世界中的补写细节预先写好。开示、第三方传票和现场调查不会临场创造新事实；请求具体且权限吻合才返回相应文件。过宽或无关请求可能没有结果。每次动作实际扣小时，庭前总额度36小时、72个动作、费用3000模拟单位。谨慎专家费用{'2400' if is5 else '2600'}，便宜专家费用{'800' if is5 else '900'}；专家仅能读律师实际交付的材料，意见可能不利。庭内异议窗口免费，非法动作不扣额度。

四份必交工作文件已在委托中明确：case_timeline.csv、evidence_register.csv、pretrial_plan.md、examination_outline.md；按新资料更新并保留版本，工作文件不会自动变成证据。

## 三轮及真实的新信息

第一轮：准备、证据动议、证人出庭与交叉询问、基于当时记录的最终裁定。轮间控方补交新材料，律师可以核读、调查和调整文件。

第二轮：控方及辩方开场、控方主询问/辩方交叉、控方举证后1118.1申请、辩方举证与控方交叉。每段控方发言、提问和回答都有统一实时窗口；候选看到实际台词，不知道未来剧本或陷阱编号。迟交救济准许后有真实补充准备动作，再回原举证阶段。

第三轮：定向补充证言、必要的申请更新、指示会议、控方结案→辩方结案→控方反驳→宣读实际准许指示→六名独立陪审员逐罪投票。第二轮准入记录和用途限制继续有效；程序听证记录不会自动进入陪审团。{gate}

披露、已读、已提交、已采纳、排除、限定用途分别记载。事实约束不只检查编号存在，还验证自然语言陈述是否由允许事实支持。陪审团使用逐句证据单元，排除血样时不会误删同一回答中合法的其他观察，也不会通过原始锚点绕过排除。

## 法律库、评分和冻结

本地42条法源，包含相关材料、非必要材料及明确标注的未来干扰项；每条有URL、检索日期、原始快照与文本哈希、时点和核验状态。检索后需实际阅读全文，不能以编号存在就算正确适用。

60项满分120：42项机械检查、17项双模型质量评分、1项结局。主评分gpt-6-sol high、副评分doubao-seed-evolving high；分数独立，报告均值、一致率和分歧。结局只占1.67%。N/A只由预注册机会条件触发，原始和适用分母同时报告；评分器失败保持pending，不能记N/A。严重错误按具体事件去重，另列扣分。

参考/弱路径只使用候选可见接口，不能访问World、文件系统、隐藏事实、rubric或未来剧本。代码与世界先记录预注册哈希，真实环境及双评分校准通过后冻结，再跑候选；候选输出后不得新增扣分项。任何修复须新版本、注明重评范围并重新校准，不可悄悄替换文件。

## 怎么运行

从项目根运行`bash setup.sh`、`bash run_{num}.sh offline`。密钥设置及后台启动的完整步骤见项目根README。默认`bash run_{num}.sh`启动本题流水线；`bash run_{num}.sh status`查询。

本题正式候选固定为GPT两次、Seed两次，每次三轮；另外的参考/弱路径校准不计入正式次数。实际运行代码是根shared_engine，engine/runtime_snapshot为完全相同的核对副本。`python3 -I engine/run.py pipeline`也可从本题目录启动。不要在有正式运行后直接执行tools重建世界或改配置。

## 当前结果与局限

已随包提供离线参考/弱路径、机械检查和HTML回放，结果见scores/summary.md。**未调用真实API：17项质量评分、真实校准、GPT/Seed正式分数、均值范围及双评分一致率均待运行。** 离线裁决按规则占位，只用于结构验证；不证明真实法律结局可区分，也不能开启正式实验。当前API token消耗0。

教师原包包含v1结果和未完成v2修复，当前引擎不是那些旧结果的完全对应版本；本包遵循规格与复核修复要求，不把旧缺陷照抄进新题。具体对应见docs/教师标准对照.md。

模拟日期{', '.join(m['simulation_dates'])}是设计日期，不冒充完整历史庭期。历史文本沿用已保存来源，部分仍为partial；{'旧§290完整合并版本及1170历史引文仍需法律复核。' if is5 else '§23153的2000完整历史版本链尚未独立确认；不得把2012重述说成2000原始快照。'}补写事实和法律适用尚无执业律师人工复核。真实环境的漂移、接口兼容和高分可达性须在运行端实际校准，失败会暂停并报告，不会伪造通过。
'''
    (case/'README.md').write_text(text,encoding='utf-8')
    if not (case/'进度.md').exists():(case/'进度.md').write_text('# 当前进度\n\n工程已实现，等待离线验证。真实API调用0。\n')
 (ROOT/'requirements.txt').write_text('# Python >=3.11；运行及离线测试仅标准库，无需第三方pip包。\n')
 (ROOT/'ENVIRONMENT.json').write_text(json.dumps({'python_minimum':'3.11','developed_python':sys.version.split()[0],'developed_platform':platform.system(),'third_party_runtime_dependencies':[],'api_protocol':'Responses / JSON schema strict / explicit instructions','max_concurrent_requests':1,'teacher_alignment':'模型、effort、接口、预算及数据协议对齐；教师未提供OS镜像或依赖锁，不宣称操作系统逐位相同。'},ensure_ascii=False,indent=2)+'\n')
if __name__=='__main__':build()
