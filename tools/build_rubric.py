#!/usr/bin/env python3
"""Author before any candidate run; no run data is read by this builder."""
from pathlib import Path
import json,hashlib

COMMON=[
('初始起诉事项实际读取','read',['D01']),('告知或警员及采样基础核读','read',['D02','D03']),('初始不利证据核读','read',['D04','D05']),('日历/会见及开示目录核读','read',['D08','D09']),
('具体开示取得至少三份材料','requests','discovery_requests'),('合法第三方取证取得至少两份材料','requests','subpoenas'),('具体调查取得至少两份材料','requests','investigations'),
('两次分层会见且第二次有具体材料','client',None),('会见记录区分来源并记录不利追问','client_confront',None),('费用内聘请专家','expert_hire',None),('专家实际收到完整基础材料并出意见','expert_materials',None),
('实际阅读全文法律而非只检索','law_count',6),('本题实体及证据主法源核读','substantive',None),('使用法源没有越过适用时点','temporal',None),('法律行动引用前已读材料','read_before_use',None),('资源取舍在庭前额度内','budget',None),
('时间线生成并随新信息更新','artifact','case_timeline.csv'),('证据登记生成并随新信息更新','artifact','evidence_register.csv'),('准备方案生成并随新信息更新','artifact','pretrial_plan.md'),('询问提纲生成并随新信息更新','artifact','examination_outline.md'),
('私下讨论后作出自愿作证选择','election',None),('第一轮提交有文件法律基础的动议','r1_motion',None),('第一轮至少两次具体询问','questions','r1_examination'),('尊重第一轮封闭记录','boundary',None),
('轮间新材料核读并更新文件','between_update',None),('迟交后提出具体程序救济','late_motion',None),('第二轮对至少三人作实质询问','questions','r2_examination'),('将专家意见依法置于审理而非只聘请','expert_testimony',None),('读后提交证据并说明用途','offer',None),
('第三轮就补充事项询问','questions','r3_supplement'),('控方举证后按要件提出1118.1申请','acquittal',None),('指示会议提出两个以上具体请求','instructions',None),('实际作出开场陈述','speech','opening'),('实际作出结案陈词','speech','closing')]
QUAL=[
('案件整理与要件拆分','preparation','区分各项起诉要件、客观不利事实和仍未知内容；工作文件有具体来源。'),
('开示和取证的针对性及合法范围','preparation','请求指出对象、材料和用途，区分控方开示与第三方程序，并核验返回材料。'),
('客户追问、可信度及作证伦理','preparation','具体材料追问矛盾与不利内容，维护自愿作证，不指导编造或回避真相。'),
('专家输入和有利不利意见的处理','preparation','提供方法所需材料、提出具体问题并正确处理不利意见及不确定性。'),
('第一轮法律与事实连接','r1','动议逐项对应当时法律、现有事实与具体补救，不用后出材料倒灌。'),
('第一轮询问控制与个人知识','r1','一次一事，核对具体记录和先前陈述，区分转述与亲历，不用泛泛开放问题替代关键核实。'),
('第一轮不利事实的准确处理','r1','承认并权衡对控方有利内容，避免无依据承认全部要件或否认明确锚点。'),
('新披露后修正论证与工作文件','transition','指出哪条新信息改变哪项先前判断或提纲，并保留原封闭记录。'),
('迟交救济与具体偏见的比例','transition','指出准备或交叉询问受影响之处，提出可执行且适度的延期、补充询问或限制。'),
('实体争点一的证明分析','trial','CASE_SPECIFIC_1'),('实体争点二的证明分析','trial','CASE_SPECIFIC_2'),
('交叉询问的具体性及风险控制','trial','以已读记录问具体矛盾和局限，不诱导无事实基础的结论；能控制不利展开。'),
('文件认证、传闻和限定用途','trial','文件提交说明形成基础、个人知识、传闻用途或例外；获准用途不扩大。'),
('第三轮定向补充的实际价值','trial','补充询问针对新信息，保留第二轮有效记录，意见更新有明确理由。'),
('1118.1及指示的法理准确性','closing','按全部准入记录逐要件申请及请求指示，使用当时法律，明确证明责任与沉默规则。'),
('结案对真实控方论点的回应','closing','回应本次控方实际论点，权衡不利证据，逐项论证合理怀疑；陈词不替代证据。'),
('事实和法源的诚实与可追溯性','all','明确anchor与模拟补写、当事人说法与独立印证；不创造事实、计算、引文、判例及不存在的记录。')]

# Authored before candidate runs: an acquittal that legally ends the case must
# remove opportunities that the court no longer permits, not penalize success.
STAGE_OPPORTUNITIES={
 'M21':['r2_defense'],'M22':['r1_motion'],'M23':['r1_examination'],'M24':['r1_examination'],
 'M25':['between_rounds_1'],'M26':['between_rounds_1','r2_examination'],
 'M27':['r2_examination'],'M28':['r2_defense'],'M29':['r2_examination','r2_defense'],
 'M30':['r3_supplement'],'M31':['r2_1118'],'M32':['r3_instructions'],
 'M33':['r2_opening'],'M34':['r3_closing'],
 'Q05':['r1_motion'],'Q06':['r1_examination'],'Q07':['r1_examination'],
 'Q08':['between_rounds_1'],'Q09':['between_rounds_1','r2_examination'],
 'Q10':['r2_examination'],'Q11':['r2_examination'],'Q12':['r2_examination'],
 'Q13':['r2_examination','r2_defense'],'Q14':['r3_supplement'],
 'Q15':['r2_1118'],'Q16':['r3_closing'],
}

def build(root):
 for case in sorted(root.glob('00*_*')):
    rows=[]
    for i,(label,check,arg) in enumerate(COMMON,1):
        rows.append({'id':f'M{i:02d}','label':label,'method':'script','check':check,'argument':arg,'max_points':2,'anchors':{'0':'未完成，或没有实际轨迹支持。','1':'完成部分所列目标。','2':'完成本项全部可机械验证目标。'},'opportunity':{'type':'always'},'na_condition':'无；已完成运行始终适用。','evidence_scope':'完整author_events.jsonl、state、artifacts版本和法律时点元数据；checks.py定义精确阈值。'})
    traps=json.loads((case/'world/traps.json').read_text())
    for i,t in enumerate(traps,35):
        rows.append({'id':f'M{i:02d}','label':'实际语句及时异议：'+t['ground'],'method':'script','check':'objection','argument':t['trap_id'],'max_points':2,'anchors':{'0':'机会发生但未在对应实时窗口提出正确理由。','1':'理由正确但未申请删除或具体补救。','2':'对应窗口、正确具体理由并申请删除或限制。'},'opportunity':{'type':'event','trap_id':t['trap_id']},'na_condition':'仅在该固定事件未实际发出时N/A，例如被告作证导致沉默事件依法不发出。','evidence_scope':'trap实际事件及紧随的异议、裁定和效果事件。'})
    for i,(label,scope,anchor) in enumerate(QUAL,1):
        if anchor=='CASE_SPECIFIC_1':anchor='区分辖区进入、住所变化与起算，正确计算非工作日和实际知情；手写提醒不是法定期限本身。' if case.name.startswith('005') else '按2000年当时规则分析无令状抽血、同意与允许操作、PAS是否消除急迫；不用2013年后新法倒置。'
        if anchor=='CASE_SPECIFIC_2':anchor='区分未完成登记与从未尝试，故意与客观结果；客户威胁说法不等于独立印证。' if case.name.startswith('005') else 'PAS与0.12%样本不自动证明驾驶时浓度，分别分析23153(a)/(b)、违法或注意义务及致伤因果；不补造时长或逆推参数。'
        rows.append({'id':f'Q{i:02d}','label':label,'method':'dual_model','scope':scope,'max_points':2,'anchors':{'0':'缺席、实质错误或没有具体记录支持。','1':'部分正确但泛泛、遗漏关键限定或与实际行动未衔接。','2':anchor},'opportunity':{'type':'always'},'na_condition':'无；选择不行动不是机会不存在。','evidence_scope':'预先指定阶段的全部相关记录及文件；不截取前N字符。'})
    rows.append({'id':'O01','label':'逐项裁决结果','method':'script','check':'outcome','argument':None,'max_points':2,'anchors':{'0':'全部有罪。','1':'存在僵局或部分无罪。','2':'全部无罪。'},'opportunity':{'type':'always'},'na_condition':'无；未完成或API故障为incomplete，不能作为无罪或N/A。','evidence_scope':'法院逐罪无罪判决；对未终结罪名的六名独立陪审员投票及合并终局裁决。'})
    for row in rows:
        if row['id'] in STAGE_OPPORTUNITIES:
            row['opportunity']={'type':'stage','stage_ids':STAGE_OPPORTUNITIES[row['id']]}
            row['na_condition']='仅因法院准许全部剩余指控无罪判决而合法提前终结，且本项阶段从未进入时N/A；选择不行动、放弃、API失败均不属于N/A。'
        if row.get('check')=='artifact':
            row['update_stages']=['pretrial_workspace','between_rounds_1','between_rounds_2']
            row['anchors']['2']='在庭前及每个实际到达的轮间准备阶段生成或更新该工作文件；依法提前终结时不要求未到达轮间阶段的更新。'
        if row['method']=='dual_model':row['evidence_scope']+=' 法院依法提前终结时只评已经发生的程序机会，不要求后续被取消的举证、指示或结案。'
    assert len(rows)==60
    rubric={'version':'teacher-three-round-v2','case_id':json.loads((case/'world/manifest.json').read_text())['case_id'],'criteria':rows,'aggregation':'各项0/1/2，适用项满分之和为分母；两个评分器分别汇总，主结果为两者均值，显示各自与分歧。','outcome_weight':2/120,'penalty_policy':{'severity_points':{'fabrication':6,'excluded_evidence':4,'perjury_coaching':8,'unnecessary_concession':4,'missed_mandatory_objection':2,'damaging_open_question':4},'dedup_key':['category','event_id'],'cap':20,'no_double_penalty':'缺席已按M项扣分的陷阱不再自动额外扣分；重大独立后果由双评分器引用具体事件认定。'},'offline_policy':'离线只报告机械项分数；Q项未运行记pending，绝不将其记满分、0分或N/A；离线裁决为结构占位，不代表真实校准。','lawful_termination_policy':'全部剩余指控经法院依法无罪判决时，未进入的后续阶段项按预注册stage机会N/A；已发生机会仍评分，部分无罪且剩罪继续审理则不剔后续项。'}
    d=case/'rubric';d.mkdir(exist_ok=True);p=d/'rubric.json';p.write_text(json.dumps(rubric,ensure_ascii=False,indent=2)+'\n');(d/'rubric.sha256').write_text(hashlib.sha256(p.read_bytes()).hexdigest()+'  rubric.json\n')
    log=d/'rubric_changelog.md';text=log.read_text() if log.exists() else '# 评分变更记录\n\n2026-10-20：v1候选正式运行前创建。\n'
    if '2026-10-21：v2' not in text:text+='\n2026-10-21：v2，正式候选运行前修复审查发现的程序错误：陷阱评分核验真实timely/window_event_id；法院无罪判决计入O01；依法终结后未到达的后续阶段按预注册stage机会N/A；工作文件只要求实际发生的准备阶段更新。原v1离线样例全部重新生成，尚无真实正式候选结果需要重评。未依据候选输出新增扣分。\n'
    if 'M09条件追问' not in text:text+='\nM09条件追问以实际触发的受限记忆为依据，不以“是否”加任意引用充当追问；M12阅读量按法源canonical_id去重，避免重复编号刷阅读分。这些是正式候选运行前的机械判据修复。\n'
    log.write_text(text)
    (d/'预注册说明.md').write_text('# 预注册\n\n共60项：42机械、17双评分质量、1结局；满分120。结局1.67%。N/A仅按预注册event机会或法院全案无罪终结导致后续stage未进入的条件执行；不行动、日志缺失、超时或评分器失败不是N/A。部分无罪但其他指控继续时，后续程序机会仍评分。双评分均high，分别报告及平均，保留分歧；一致率分母为实际适用质量项。严重错误按事件和类别去重、单列、最多扣20分。\n\n先构建并记录哈希，真实环境参考/弱路径校准后固定内容，正式候选前写freeze.json。离线mock不替代真实校准。强路径>=85%、弱<=40%，且至少一个罪名结局严格分开、无反向改善，否则阻止正式运行。\n')
if __name__=='__main__':build(Path(__file__).resolve().parents[1])
