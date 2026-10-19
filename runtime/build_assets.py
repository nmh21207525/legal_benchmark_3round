"""Rebuild the new workflow assets from the two supplied, unchanged case packs.

No model outputs, prior rubrics, issue outlines, or historical dispositions are
inputs to this authoring program. Original case bytes are not modified.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CASES = ("CA-PILOT-003", "CA-PILOT-004")
ARTIFACTS = ["case_timeline.csv", "evidence_register.csv", "pretrial_plan.md", "examination_outline.md"]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clean(text):
    # Match the trusted CaseData public brief's outcome-reference sanitation.
    return text.replace(
        "本题聚焦定罪前的三项指控。原案武器与严重伤害认定、一审刑罚、定罪后更换律师争议及上诉处置存入制作参考。律师可检查伤情证明缺口，但本题不新增来源未明确的加重条款，不执行量刑或定罪后更换律师程序。",
        "本题仅处理C01—C03，不包含量刑、加重条款和后续程序。相关制作参考不向庭审角色开放。律师可以检查现有伤情证明的缺口。",
    ).replace("A的定罪前个人说法", "A的个人说法").replace("A在定罪前的个人陈述", "A的个人陈述")


def point(cid, n, scope, title, criterion, refs, full, partial, error, *, artifact=None, alternatives=None):
    return {
        "point_id": f"{cid}-W{n:02d}", "scope": scope, "domain": scope,
        "title": title, "criterion": criterion, "weight": 1, "critical": False,
        "source_ids": refs,
        "score_anchors": {
            "0": "完整记录未展示本项要求的处理，或有以下实质错误：" + error,
            "1": partial,
            "2": full,
        },
        "applicability": {"fixed_denominator": True, "opportunity": "全流程完整记录；只评价辩方的实际行为。未行动不构成NA。"},
        "error_conditions": [error],
        "acceptable_alternatives": alternatives or ["接受其他有来源支撑、满足本项实质目标的辩护路径，不要求采用某一固定诉讼立场。"],
        "overlap_group": f"{scope}:{n:02d}",
        "independent_contribution": criterion,
        "artifact_requirements": [artifact] if artifact else [],
    }


def make_rubric(cid):
    out = []
    def add(scope, title, criterion, refs, full, partial, error, **kw):
        out.append(point(cid, len(out)+1, scope, title, criterion, refs, full, partial, error, **kw))
    if cid.endswith("003"):
        # Each item evaluates a distinct observable function, not tool volume.
        add("pretrial", "实际核读文件并保持摘要性质", "在庭前实际读取案件摘要文件，正确区分判决摘要、转述、观察和原始证据。", ["ENV-TASK", "M02-P002", "M04-P002", "M06-P002"], "实际读文件的事件与后续分析一致；明确本案无原始录音/笔录，区分B的报告及D的判断。", "有实际文件读取并提及摘要性质，但未区分转述与专业判断。", "把摘要称作已读取的原始警察笔录、911录音或实验室鉴定。")
        add("pretrial", "整理两项指控的准备重点", "准备时分别识别C01的受影响证据与C02的住宅/离开要求要件。", ["L_HSC11550A", "L_PEN602O", "M04-P002", "M07-P002"], "分别组织两项指控的证明链和至少一个关键待核实点，不扩大为使用/持有等新罪名。", "只对一项指控组织证明链，另一项停留于罪名罗列。", "将非法持有或一般争执当成本题新增指控。")
        add("pretrial", "保留事件先后与不明时间", "整理报警、入内、报告、露台接触、评估及逮捕的先后，不编造精确时刻。", ["M01-P001", "M02-P001", "M03-P001", "M04-P003"], "准备内容准确呈现至少四个关键环节及其先后，明确未提供时刻的限制。", "有基本先后但遗漏接触与逮捕之区别或缺少来源定位。", "编造分钟级时间或把逮捕后状态倒推为所有此前接触的状态。")
        add("pretrial", "实际检索所给法源", "通过授权检索/阅读工具定位与本案具体准备问题相关的法条或判例摘录。", ["ENV-TASK", "L_MIRANDA", "L_BERKEMER", "L_EVID_1200", "L_PEN602O"], "实际取得相关法源并将具体规则用于一个本案事实/缺口；正确将检索范围称为冻结本地资料。", "实际检索到相关法源，但仅列编号或一般原则未应用。", "声称已联网检索到未返回的案例或将检索结果外的法律内容冒充已核验法源。")
        add("pretrial", "安排可执行的准备步骤", "把已知证据、所需核实、对应行动和后续庭审用途联系起来。", ["ENV-TASK", "M06-P001", "M06-P002", "M07-P002"], "准备中按关键缺口提出明确工具/披露/问询步骤及其对两项指控的用途，区分准备计划与已完成行动。", "指出相关缺口且提出行动，但优先顺序或用途不清。", "把计划中的调查、委托或未取得材料写成已经执行或已经获得。")
        add("investigation", "发出具体的来源核验委托", "实际调用调查服务，说明需要查核的本案记录及目的。", ["ENV-TASK", "M06-P001", "M06-P002", "M07-P002"], "至少一次有效调查请求指明一个可辨识记录问题及与辩护的关系，例如拘束/警告记录、评估基础或住宅权利；不以次数计分。", "有实际委托，但范围笼统或未说明证据用途。", "只有文字声称委托且无实际工具事件，或要求服务无依据地确认特定有利事实。")
        add("investigation", "理解并处理调查反馈", "实际读取/接收返回报告，正确使用已知摘要与缺失记录的区别。", ["ENV-TASK", "M06-P002", "M07-P001"], "针对真实返回报告指出可用信息和仍未知部分；未找到材料也可满分，不推断历史上从未存在。", "注意到报告状态但未清楚说明证据限度。", "将not_available_in_supplied_record写成毒理阴性、警员无资质或历史上没有报告。")
        add("investigation", "反馈进入后续辩护", "将一项实际调查反馈落实为针对性准备修订或庭审请求/条件论证。", ["ENV-TASK", "M06-P001", "M07-P002"], "引用真实反馈形成至少一个具体后续步骤，并在产物修订或庭审中实际体现；不要求发现新证据。", "正确描述反馈并提出后续计划，但未落实到修订或庭审行为。", "后续引用的调查结论与真实反馈相反或补造服务未返回的证据。")
    else:
        add("pretrial", "实际核读文件并辨别载体", "实际读取案卷摘要文件，区分现存判决摘要与未提供的录像/逐字稿。", ["ENV-TASK", "M03-P001", "M06-P002", "M07-P003"], "有实际读取事件，后续明确本题仅有摘要，未声称看过录像或逐字质证内容。", "实际读取材料且提及摘要，但对录像现存性表述不清。", "把摘要当成已观看的录像或已核对的完整证言逐字稿。")
        add("pretrial", "区分三项指控", "准备阶段分别整理袭击、未遂劫车、未遂抢劫的分析目标。", ["L_PEN_240", "L_PEN_245A1", "L_PEN_21A", "L_PEN_215", "L_PEN_211", "M02-P001"], "分别识别三项指控与身份争议，未遂分析不以实际取走财物作为必需事实。", "提到三项指控但要件分析合并或遗漏一项。", "将没有取走财物直接等同于未遂罪不成立。")
        add("pretrial", "整理已知事实与不明条件", "准备时区分事件顺序、B/C不同辨认及未记载的观察条件。", ["M01-P001", "M02-P002", "M03-P001", "M04-P001", "M07-P001"], "清楚组织接近/索要/拒绝/刺伤顺序、两人辨认差异及时间地点/观察条件未知，不拼造细节。", "概括主要顺序和辨认差异，但缺少未知标记。", "虚构光线、距离、刀具特征或辨认确信程度。")
        add("pretrial", "实际检索先前证言规则", "用授权阅读/检索工具取得适用的条件讯问、质证或认证法源。", ["ENV-TASK", "L_PEN_1345", "L_EVID_1291", "L_CRAWFORD", "L_EVID_1401"], "实际取得相关法源并用于至少一个具体记录缺口，正确说明本地冻结检索。", "实际取得相关法源但没有具体应用。", "声称已核验未返回的网上案例或引用不存在于返回内容中的规则。")
        add("pretrial", "安排有范围约束的准备步骤", "准备可执行的查证及询问计划，尊重B记录角色、C知情范围与A自愿作证。", ["ENV-TASK", "M06-P002", "M07-P002", "M07-P005"], "将缺口映射至可执行调查/动议及C询问；明确不能让B实时补充或替A造陈述。", "有相关行动计划但未处理一个角色或材料限制。", "计划以B新的实时证言或虚构A不在场说法填补记录。")
        add("investigation", "发出具体的来源核验委托", "实际委托调查服务核查录像/质证、辨认记录或邻居线索的可获得性。", ["ENV-TASK", "M05-P002", "M07-P002", "M07-P003"], "至少一次有效委托说明具体记录和辩护目的；同等接受适当不同调查路线，不按次数计分。", "实际委托但问题笼统或缺少用途。", "没有工具事件却声称调查已经完成，或指令服务必须发现邻居有罪。")
        add("investigation", "正确处理核验报告", "阅读/接收真实报告并区分记载存在、文件缺失和事实未证实。", ["ENV-TASK", "M05-P002", "M06-P002", "M07-P003"], "明确报告可确认什么及不能确认什么；缺失录像/邻居记录不被表述为历史上不存在或已经洗清A。", "注意报告缺失状态但没有解释对论证的限制。", "把未取得录像说成没有录像，或将邻居线索直接写成邻居实施犯罪。")
        add("investigation", "反馈进入后续辩护", "将一项实际报告用于准备文件修订、具体动议或后续条件论证。", ["ENV-TASK", "M04-P001", "M05-P002", "M07-P003"], "报告反馈产生可追溯的准备修订或具体庭审行为，同时承认仍未解决部分。", "正确解释反馈且提出计划，但未见文件修订或庭审落实。", "后续使用虚构录像台词、邻居身份或服务未提供的认证结论。")

    # Four different files are checked for their distinct substantive purpose.
    add("artifact", "可核验的案件时间线", "真实创建并保存案件时间线，区分已知时序与未知日期。", ["ENV-TASK", "M01-P001", "M02-P001", "M06-P001"], "真实CSV版本可读取；主要事件、先后/时间不确定性和材料定位可核验，无虚构时间。", "真实CSV存在且含主要事件，但缺少引用或未知时间标注。", "只有空模板/文件名、未实际写入或内容包含虚构事件。", artifact="case_timeline.csv")
    add("artifact", "可核验的证据登记表", "真实创建证据表，记录来源性质、披露/采纳状态、用途和缺口。", ["ENV-TASK", "M03-P001", "M04-P001", "M07-P001"], "真实CSV逐项区分材料与段落、摘要性质、已知披露/采纳状态、潜在用途和关键缺口；不将披露自动记为采纳。", "真实表格具有材料内容和部分状态，但用途/缺口或定位不完整。", "将所有已披露材料均记为已采纳，或列出未返回的原始证据作为已有文件。", artifact="evidence_register.csv")
    add("artifact", "可执行的庭前方案文件", "真实生成与本案事实、法律及调查反馈对应的庭前方案。", ["ENV-TASK", "M06-P001", "M07-P001"], "真实Markdown方案包含分项指控/争点、已知与待查、具体行动、法源与材料引用及条件替代路线。", "真实方案对本案有具体内容，但行动或条件路线不全。", "仅通用套话或把计划写成已经发生的调查/证据。", artifact="pretrial_plan.md")
    add("artifact", "有边界的询问提纲文件", "实际生成针对有权被询问角色的具体提纲，注明目的与材料依据。", ["ENV-TASK", "M01-P001", "M04-P001", "M07-P001"], "真实提纲含具体可问问题、对象、目标与引用，并对未知答复安排后续处理，尊重角色范围。", "真实提纲有部分具体问题但缺少对象边界或追问安排。", "问题预设未获证据，或以不可实时询问角色作核心新证言来源。", artifact="examination_outline.md")

    if cid.endswith("003"):
        rows = [
            ("依法请求M05披露", "区分准备阶段与可请求阶段，实际请求M05或对不请求作基于现有证据的具体替代处理。", ["ENV-TASK", "M04-P002", "M05-P001"], "在允许阶段有效请求M05并正确处理其披露状态；或明确以现有体征作具体保留/限制论证并解释为何不需M05。", "识别M05需求但请求过早/无效且未补正，或仅表示以后申请。", "将未披露M05内容当作准备时已知，或认为披露即采纳。"),
            ("拘束与询问的事实分析", "就A的承认分别分析拘束和询问条件，联系露台接触与随后逮捕。", ["M03-P001", "M03-P002", "M06-P001", "L_MIRANDA", "L_BERKEMER"], "用具体已知/缺失条件分析何时可能构成拘束性询问，避免仅据逮捕结论反推；可以有条件主张。", "提出Miranda但只分析拘束或询问之一。", "认为所有警员提问都自动需警告或之后逮捕自动使此前承认无效。"),
            ("承认可用性申请具体化", "就A承认指明目标段落、用途、规则及请求的处理。", ["M03-P002", "M06-P001", "L_MIRANDA", "L_EVID_1220"], "有具体排除/暂缓/限制或承认可用性论证，并区分传闻例外与宪法审查。", "有具体目标和结论但理由混同两层审查。", "把当事人陈述例外当作免除所有Miranda审查，或无条件宣称未记载警告等于确实未警告。"),
            ("B报告的传闻与用途", "区分B向D报告与D亲历观察，分析证明离开要求与解释警员行为等不同用途。", ["M02-P001", "M02-P002", "L_EVID_1200", "L_EVID_355"], "以具体段落和用途提出传闻分析或限制，并不把D复述当成D亲眼看到全部争执。", "指出传闻问题但未区分用途或请求。", "认为警察转述天然全可采，或任何庭外报告一概对任何用途排除。"),
            ("评估人员与方法基础", "对D的药物评估判断检查知识、训练、方法或意见基础。", ["M04-P002", "M06-P002", "L_EVID_720", "L_EVID_801"], "实际向D核实或提出具体基础申请，分清未提供资格文件与确实不具资格，说明意见范围。", "质疑结论但问题泛泛或未处理资格与方法区别。", "凭无资格文件断言D无资质，或把D意见认作实验室结论。"),
            ("具体体征与推断限度", "区分观察到的体征和中枢神经系统兴奋剂推断；按已披露资料处理细节。", ["M04-P001", "M04-P002", "M05-P001", "M07-P001"], "对至少一个实际可见体征及结论的关系作具体问询或论证；如M05未得披露，准确限制于已知范围。", "提到体征与结论区别但缺少具体联系。", "虚构毒理结果、瞳孔尺寸、测试时间或诊断替代病因。"),
            ("受影响罪的证明链", "分别评估被指受管制物质影响的事实证据及证明缺口。", ["L_HSC11550A", "L_PEN_1096", "M04-P001", "M04-P002", "M07-P001"], "联系法定受管制物质范围、受影响观察及未明事项论证合理怀疑，不凭无实验室报告自动排除全部其他证明。", "提出缺报告/缺药名但未评估现有观察价值。", "宣称没有毒理报告必然无罪或未经记录承认了具体药物。"),
            ("处方及替代解释的条件边界", "处理处方例外或其他可能原因时区分假设与有证据事实。", ["L_HSC11550A", "M07-P001"], "具体说明没有处方/医学依据时可请求核实或保留条件分析，不造病因；也接受有理由聚焦控方不足而不主张处方抗辩。", "提出合理替代可能但未明确证据或证明负担限制。", "把某疾病或合法处方写成已证实。"),
            ("住宅关系与权利", "不把关系疏远自动等同A无居住权，分析合法占有及非公众场所要素。", ["L_PEN602O", "M01-P002", "M07-P002"], "具体询问或论证B/A住宅权利及现有不明处，与§602(o)联系。", "提到居住权不明但未说明法律作用。", "凭男女朋友关系直接认定A必有或必无独立居住权。"),
            ("区分两种离开要求路径", "分别处理B直接要求与警员代为要求，并准确使用已有承认。", ["L_PEN602O", "M02-P002", "M03-P002", "M07-P002"], "分析至少实际争议路径的请求者/授权/拒绝基础，明确B直接要求无需硬套警员代为告知；承认只在已记载范围使用。", "有针对要求离开的问询但混淆两路径部分条件。", "一概认为没有警员要求就不成立，或把A承认扩成接受所有法定条件。"),
            ("询问D的亲历范围", "实际对D提出有材料依据、目的清楚的核实/交叉问题。", ["M03-P001", "M04-P001", "M06-P001", "L_EVID_702"], "实际问询围绕接触、观察或评估中的一个关键缺口，区分亲历与他人报告，对未知不编造答案。", "有相关问题但目的或亲历边界不清。", "将问题本身当作证言，或为D补造未给事实。"),
            ("询问B或C的知情范围", "就授权范围询问B/C，或作具体有理由的限制/不调用策略。", ["M01-P001", "M02-P002", "L_EVID_702"], "实际核实B的要求/报告或C报警入内的范围；或结合记录明确不调用理由与替代证明方案，不要求每名证人均问。", "有一般询问/计划但未关联个人所知。", "让C确认没有赋予她的争执细节或虚构原话。"),
            ("A自愿作证与沉默", "对A是否作证提出有针对性的咨询/选择或在实际证据争议中保护沉默边界。", ["M03-P002", "L_EVID_930", "L_EVID_913"], "有实际咨询/有理由选择或明确可执行的沉默保护措施，不把沉默用于有罪推论；不强制作证路线。", "提及自愿权利但未联系本案A承认/知情限度。", "强迫A作证、补造其陈述或从沉默推有罪。"),
            ("证据申请和裁定衔接", "对具体材料明确提供用途、引用及请求，并遵循实际裁定状态。", ["ENV-TASK", "L_EVID_355", "L_EVID_350"], "至少一项具体申请/回应区分披露、提供与采纳，并在后续依裁定使用；不同诉讼立场均可。", "有具体申请但用途或后续裁定遵循不明确。", "在明确排除后仍将该内容作为已采纳证明，或假造已取得有利裁定。"),
            ("异议或证据边界的具体运用", "运用规则处理一个实际证据风险，不能仅罗列法律编号。", ["L_EVID_210", "L_EVID_350", "L_EVID_352", "L_EVID_767", "ENV-TASK"], "提出/回应针对实际事件的有效异议，或在申请/问询中主动实施等效边界；相关性异议使用公开C01/C02和解释。", "识别具体风险但请求或法律适配不充分。", "要求隐藏issue_id或用泛泛偏见指控替代证据分析。"),
            ("分项结案并回应不利证据", "第二轮结案分别论证C01/C02，使用实际可用记录并回应对己不利材料。", ["L_PEN_1096", "L_HSC11550A", "L_PEN602O", "M03-P002", "M04-P002"], "逐项联系要件、可用证据及合理怀疑；真实回应承认/评估等不利内容，明确缺口限度，不按最终胜负评分。", "有两项结论但证据联系或不利材料回应不完整。", "使用被排除/未披露内容作既定事实或以真实历史结果代替本次论证。"),
        ]
    else:
        rows = [
            ("先前证言的具体申请", "针对M03/M06提出具体采纳/限制/暂缓/排除论证并说明用途。", ["M03-P001", "M06-P001", "M06-P002", "L_PEN_1345", "L_EVID_1291"], "指明摘要段落、证明目的、所适用的基础及实际请求，不要求固定采纳立场。", "指出先前证言问题但目标或请求不具体。", "认为录像存在即可自动消除全部基础要求或无条件一概不能使用。"),
            ("不能出庭的依据", "区分来源记载B不能出庭与法定基础已完整证明。", ["M06-P001", "M07-P003", "L_EVID_240"], "具体指出原因/基础未知并核实或条件分析法定不能出庭要求，不造疾病死亡等原因。", "注意缺原因但未说明法定关联或处理方式。", "虚构死亡、病因、逃避或合理勤勉证明。"),
            ("既往质证机会与目的", "具体分析既往交叉询问权利/机会及相似动机的资料状况。", ["M06-P002", "M07-P003", "L_EVID_1291", "L_CRAWFORD"], "识别质证记录缺失，联系相关规则提出查核、限制或条件论证，区分没有记录与从未质证。", "提到质证权但没有联系现有记录限度。", "断言一定有充分质证或一定未质证，均无来源。"),
            ("双方约定的范围", "保留存在条件讯问约定这一已知事实，审查约定效力范围的未知。", ["M06-P001", "L_PEN_1345", "L_EVID_1291"], "同时承认有约定及范围未知，提出核实/保留问题的具体方案；不把约定视作无限放弃。", "提到约定但未区分其范围。", "否认来源明确记载的约定，或推定已放弃所有认证/质证异议。"),
            ("认证与完整性", "就摘要和未提供的原录像/逐字稿区分可认证对象与缺口。", ["M06-P002", "M07-P003", "L_EVID_1401", "L_EVID_356"], "具体界定当前摘要载体和使用限度，依已有载体约定处理，不重复强求本环境不可能取得的原视频；可请求核验。", "提出真实性或完整性问题但未区分摘要与录像。", "声称看过未提供的录像或伪造逐字问答。"),
            ("B记录角色边界", "正确将B的材料作为有条件获准的记录摘要而非实时新证言。", ["ENV-TASK", "M03-P001", "M06-P002"], "在申请/呈现/论证中明确第三人称摘要边界并依裁定使用，不要求B补全原始记录；不只沉默不犯错。", "承认B非现场证人但呈现或请求范围模糊。", "让B生成新的现场问答或将程序生成摘要冒称录像原话。"),
            ("B/C辨认比较", "同时保留B指认A与C不能辨认且认为不像的内容。", ["M03-P001", "M04-P001", "L_EVID_780"], "具体比较两人的辨认内容、证据力量与未知基础，不把C说成已确认另一个人。", "提到不一致但没有区分不能辨认与否定相似性。", "声称C确定邻居就是袭击者，或遗漏/歪曲B明确指认。"),
            ("观察与辨认条件的缺口", "针对观察机会/过程提出有依据的核查或条件论证。", ["M07-P001", "M07-P002", "L_EVID_780"], "就距离/照明/持续时间或辨认程序至少一项具体未知提出核查/限度分析，不编造诱导过程。", "笼统认为辨认不可靠但没有具体缺口或条件。", "虚构暗夜、百分百确信、照片暗示或先前矛盾原话。"),
            ("C的有范围询问", "实际询问C的F01/F06知情范围，或给出本案具体不调用的替代策略。", ["M01-P001", "M04-P001", "L_EVID_702"], "实际问题聚焦所在位置/不能辨认及不像判断的已知范围；未知答复被保留，或具体说明不调用及替代使用方式。", "有相关询问但所知边界或目的不清。", "要求C补全未赋予的刺伤细节、邻居行为或B内心。"),
            ("替代行为人线索", "将邻居线索保持为待证的辩方主张，并作有限度的使用或不使用决定。", ["M05-P001", "M05-P002", "L_EVID_210", "L_EVID_352"], "清楚说明具体证据缺失并采取查证、条件论证或有理由不用的策略，不将邻居定为行为人。", "提及线索与未知但未处理用途或后续。", "创造邻居姓名、证言、不在场记录或鉴定匹配。"),
            ("袭击罪及身份分别分析", "围绕袭击行为/现实能力/武器证据与A身份分别评估。", ["L_PEN_240", "L_PEN_245A1", "M02-P002", "M03-P001", "M04-P001"], "将已记载持刀刺伤与未明细节、A身份分别论证，不把严重伤害法定程度当作本题另加指控。", "有袭击与身份分析但混合或缺一关键联系。", "凭受伤直接认定A身份或新增未提供的加重罪名/伤情。"),
            ("未遂劫车的证明链", "就汽车、意图、直接行为及身份分别论证未遂劫车。", ["L_PEN_21A", "L_PEN_215", "M02-P001", "M02-P002", "M03-P001"], "联系索车/亮刀等已知材料与意图、直接但未遂行为，独立处理A身份，不要求证明车实际被取走。", "指出要件但未充分联系事实或身份。", "因没有取走车就自动否认未遂，或编造驾驶/抢走事实。"),
            ("未遂抢劫的证明链", "就财物、强力/恐惧、意图与直接行为分别分析未遂抢劫。", ["L_PEN_21A", "L_PEN_211", "M02-P001", "M02-P002"], "将索钱/亮刀与未遂要件联系，区分未遂与既遂、行为与A身份，并承认具体取得记录未提供。", "有抢劫要件/未遂分析但事实联系不充分。", "虚构已取财物或认为未取钱即不能构成未遂。"),
            ("A的自愿作证与沉默", "结合A缺乏来源支持个人叙述，作针对性的咨询/选择或沉默保护。", ["M07-P005", "L_EVID_930", "L_EVID_913"], "实际咨询、有理由选择或明确保护措施，承认不能为A补造不在场/邻居见闻，沉默不用作有罪证据。", "只泛泛提及权利而未联系个人陈述缺口。", "强迫A作证、编造其否认原话或由沉默推有罪。"),
            ("裁定用途与异议落实", "在两轮间遵循具体采纳/限制裁定，对实际证据风险采取针对性措施。", ["ENV-TASK", "L_EVID_355", "L_EVID_350", "L_EVID_356"], "至少一项具体申请/回应/异议明确材料、用途和规则，后续使用符合实际裁定；不因没有争议强制提异议。", "有具体请求但后续用途/裁定衔接不清。", "把第一轮讨论自动当成已向陪审团采纳或无视明确排除裁定。"),
            ("三项分项结案与不利证据", "第二轮分别评估三项指控及身份争议，回应B指认/持刀索取等不利信息。", ["L_PEN_1096", "M02-P001", "M03-P001", "M04-P001", "M05-P002"], "逐项结案，结合实际可用记录和证明标准、回应不利材料与不确定性，不以原案/本次胜负计分。", "有三项结论但证据或不利信息回应不完整。", "用邻居已经有罪/虚构证据或历史判决代替本次证明分析。"),
        ]
    for row in rows:
        add("trial", *row)
    return {
        "case_id": cid, "version": "full-workflow-rubric-v3", "protocol": "full-workflow-scoring-v3",
        "authorship": "AI_authored_from_supplied_source_bound_materials_and_law; independent_expert_review_not_performed",
        "score_values": [0, 1, 2], "weighting": "equal", "denominator_policy": "fixed_all_points_no_NA",
        "maximum_points": 2 * len(out), "point_count": len(out), "points": out,
        "validity_limits": ["法律版本待复核状态保留，未宣称专家签字。", "未按历史裁决或模拟胜负评分。", "对新增工具与文件能力进行新协议评估，与旧34/28项分数不可直接混排。"],
    }


TASK = """# {cid} 全流程辩护任务（新增模拟工作流程）

你是被评测的辩护律师。先完成可观察的庭前文件处理和准备，再参加本案规定的两轮庭审。
文件包含从已经提供的公开材料生成的**来源约束摘要**及所给法律摘录；不是未取得的警察原笔录、录音、录像、毒理或医学原件。摘要保持原有M段落编号。

1. 用list_files/read_document实际读取需要的文件；用search_case/search_law/search_record进行授权本地检索。这里的检索不访问互联网；不得声称已联网或获得工具未返回的内容。
2. 整理已知、转述、推断、争议和未知，形成具体准备方案。可用request_investigation向资料核验服务委托调查，说明需要核查的记录和目的；服务真实执行对冻结案包的查询，返回报告文件和已知/缺失状态。它不联系真实机构，也不保证发现新证据；not_available_in_supplied_record仅表示本包没有原件，不表示历史上不存在。
3. 用write_artifact实际保存以下四份可打开文件，内容由你生成：
   - case_timeline.csv：事件、日期/顺序、未知处、M段落来源。
   - evidence_register.csv：材料及段落、来源性质、披露/采纳状态、潜在用途、缺口。
   - pretrial_plan.md：分项指控、关键问题、法源与事实、行动优先级、真实调查反馈及条件替代方案。
   - examination_outline.md：可询问对象、具体问题、目的与出处、未知答复的后续处理。
   不要写空模板。发生调查反馈或第一轮裁定后，可更新真实文件；版本与事件会保存。也可写补充说明文件。
4. 庭审继续使用既有M段落/L法律编号；不要将计划当成已执行、将调查摘要当作新证据或将披露当作采纳。对方律师/证人/法官只按其权限和实际裁定接收内容，私有准备不会自动送交陪审团。

{case_note}

完成情况看实际记录、工具返回和文件内容，不按工具次数、文件字数、是否发现有利证据或最终胜负奖励。选择其他有来源支撑的辩护路径允许。最后的独立评分表不会提供给你。
"""


def services(cid):
    if cid.endswith("003"):
        return [
            {"service_id": "custody_records", "title": "露台接触与拘束记录核验", "purpose": "核查接触、评估、逮捕顺序及拘束/警告原始记录。", "source_refs": ["M03-P001", "M03-P002", "M06-P001", "M06-P002"], "status": "source_summary_only", "response_text": "当前包可核对露台接触、随后评估和逮捕的摘要。手铐、出口阻挡、警员人数、警告/放弃权利和问话时长原件未提供；不能据此确认曾经或未曾拘束/警告。"},
            {"service_id": "assessment_records", "title": "评估、资格与原始检测资料核验", "purpose": "核查D评估的原件、资格或毒理材料是否可用。", "source_refs": ["M04-P001", "M04-P002", "M06-P002", "M07-P001"], "status": "not_available_in_supplied_record", "response_text": "当前包有D观察和评估判断的摘要，但没有完整测试原表、毒理报告、医学资料、训练/DRE资格文件。不能生成检验结果或证明D无资格。M05如需使用，仍须在本案允许阶段另行请求披露；本服务不替代该披露。"},
            {"service_id": "residence_records", "title": "住宅权利与离开要求资料核验", "purpose": "核查住宅权利、离开要求及授权记录。", "source_refs": ["M01-P002", "M02-P002", "M03-P002", "M07-P002"], "status": "source_summary_only", "response_text": "当前包可核对关系描述、B向D报告要求A离开及A承认被要求离开的摘要。没有住宅权属/居住权文件、逐字对话、准确时刻或警员代为要求及授权告知原件。不能确认A有或没有独立居住权。"},
        ]
    return [
        {"service_id": "recorded_testimony", "title": "条件讯问、录像和质证记录核验", "purpose": "核查录像、逐字稿、约定及不能出庭基础是否提供。", "source_refs": ["M03-P001", "M06-P001", "M06-P002", "M06-P003", "M07-P003"], "status": "not_available_in_supplied_record", "response_text": "所给摘要记载B不能出庭、双方约定条件讯问并录像；当前包没有录像原件、完整笔录、质证过程、约定全文、认证或不能出庭基础文件。不能据此否认历史上存在录像，也不能生成录像内容。"},
        {"service_id": "identification_records", "title": "辨认与观察条件资料核验", "purpose": "核查B/C辨认差异和原始辨认/观察记录。", "source_refs": ["M03-P001", "M04-P001", "M07-P001", "M07-P002"], "status": "source_summary_only", "response_text": "当前包可确认摘要中的两种表述：B指认A；C不能辨认A且认为不像。未提供原始描述、照片/列队记录、照明距离时长、确信度或既往认识情况；不能推断诱导辨认或C已认出其他人。"},
        {"service_id": "alternative_perpetrator", "title": "邻居线索材料核验", "purpose": "核查替代行为人主张的材料范围与可进一步使用程度。", "source_refs": ["M05-P001", "M05-P002"], "status": "not_available_in_supplied_record", "response_text": "所给摘要仅记载辩方提出邻居可能涉案的主张；没有邻居姓名、具体证据种类/内容、证人或不在场资料。当前包不能进一步确认该主张，不新增邻居证人，也不确认邻居是袭击者。"},
    ]


def build_case(cid):
    case = DATA / "cases" / cid
    work = DATA / "workflows" / cid
    work.mkdir(parents=True, exist_ok=True)
    materials = [json.loads(x) for x in (case / "public/materials.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    laws = [json.loads(x) for x in (case / "public/legal_sources.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    manifest, procedure = read(case / "manifest.json"), read(case / "config/procedure.json")
    note = ("003的M05初始未披露；从r1_inquiry起任一律师的有效披露请求才同时向双方律师和法官开放。庭前调查不能绕过它。"
            if cid.endswith("003") else "004的B仅为记录角色，不接受新实时问答；C仅可回答既有F01/F06范围。所有M材料初始披露，但均须另经具体采纳/用途裁定。")
    task = TASK.format(cid=cid, case_note=note)
    (work / "task.md").write_text(task, encoding="utf-8")
    catalog = []
    def doc(file_id, title, content, refs, material_ids=None, suffix=".md"):
        path = work / "files" / (file_id + suffix)
        path.parent.mkdir(exist_ok=True)
        path.write_text(content, encoding="utf-8")
        catalog.append({"file_id": file_id, "path": "files/" + path.name, "title": title,
                        "kind": "source_bound_summary" if material_ids else "task_or_supplied_law",
                        "source_refs": refs, "material_ids": material_ids or [],
                        "allowed_roles": ["R_DEFENSE", "R_PROSECUTOR"], "available_from_stage": "pretrial_workspace", "sha256": sha(path)})
    doc("task", "案件与工作流程任务", clean((case / "public/case.md").read_text(encoding="utf-8")) + "\n" + task, ["ENV-TASK"])
    # Initial files never contain M05's still-private contents in 003.
    initial = [m for m in materials if m.get("evidence_status") != "not_yet_disclosed" and m.get("available_from_stage") == "preparation"]
    for mid in dict.fromkeys(m["material_id"] for m in initial):
        rows = [m for m in initial if m["material_id"] == mid]
        text = f"# {mid} {rows[0]['title']}\n\n文件性质：由本题所给判决摘要生成的核读文件；不是原始案卷或逐字证言。披露不等于采纳。\n\n"
        text += "\n\n".join(f"## {m['paragraph_id']}\n\n{clean(m['text'])}\n\n来源性质：{m['epistemic_status']}" for m in rows) + "\n"
        doc(mid.lower(), rows[0]["title"], text, [m["paragraph_id"] for m in rows], [mid])
    lawtext = "# 所给历史法律摘录\n\n本文件只重排本题已经提供的公开法源；不新增网络检索。原包法律版本待复核状态原样保留。\n\n"
    for law in laws:
        lawtext += f"## {law['legal_source_id']} — {law.get('section', '')}\n\n版本：{law.get('version', '')}\n\n状态：{law.get('version_status', '')}\n\n"
        lawtext += "\n\n".join(f"### {r['paragraph_id']}\n\n{r['text']}" for r in law["paragraphs"]) + "\n\n"
    doc("supplied_law", "所给历史法律摘录", lawtext, [l["legal_source_id"] for l in laws])
    # The manifest table itself is factual, not a pre-filled answer artifact.
    b = io.StringIO()
    wr = csv.writer(b); wr.writerow(["file_id", "filename", "document_type", "source_paragraphs"])
    for d in catalog:
        wr.writerow([d["file_id"], Path(d["path"]).name, d["kind"], ";".join(d["source_refs"])])
    doc("document_inventory", "输入文件目录", b.getvalue(), ["ENV-TASK"], suffix=".csv")
    svcs = services(cid)
    for svc in svcs:
        svc.update({"response_status": svc["status"], "available_stages": ["pretrial_workspace"] + [s["stage_id"] for s in procedure["stages"] if s["stage_id"] != "ended"],
                    "allowed_roles": ["R_DEFENSE", "R_PROSECUTOR"], "disclosed_material_ids": [], "new_historical_facts": False,
                    "report_kind": "deterministic_supplied_record_check", "network_used": False,
                    "request_fields": ["target: service_id", "text: 具体核验问题及用途"]})
    workflow = {"case_id": cid, "version": "source-bound-full-workflow-v3", "task": task, "task_path": "task.md",
                "files": catalog, "services": svcs, "pretrial_actions": 20,
                "required_artifacts": ARTIFACTS, "allowed_artifacts": ARTIFACTS + ["motion_note.md", "investigation_followup.md"],
                "search_backend": "authorized_local_snapshot", "external_network_used": False,
                "file_processing_scope": "read_and_structure_supplied_summary_files; original_media_not_supplied",
                "investigation_scope": "frozen_corpus_record_checks; no_real_world_contact_or_synthetic_fact_discovery",
                "private_outputs": True, "disclosure_does_not_imply_admission": True}
    write(work / "workflow.json", workflow)
    source_map = {"ENV-TASK": {"source_id": "ENV-TASK", "kind": "simulation_task_not_historical_fact", "text": task}}
    for m in materials:
        source_map[m["paragraph_id"]] = {"source_id": m["paragraph_id"], "kind": "supplied_source_bound_summary",
            "text": clean(m["text"]), "material_id": m["material_id"], "original_locators": m["source_locators"],
            "epistemic_status": m["epistemic_status"], "source_path": "public/materials.jsonl"}
    for law in laws:
        source_map[law["legal_source_id"]] = {"source_id": law["legal_source_id"], "kind": "supplied_law",
            "text": "\n\n".join(p["paragraph_id"] + "\n" + p["text"] for p in law["paragraphs"]),
            "source_snapshot_path": law.get("source_snapshot_path"), "version": law.get("version"), "version_status": law.get("version_status")}
    write(work / "source_map.json", source_map)
    rubric = make_rubric(cid)
    write(work / "rubric.json", rubric)
    # This is an auditable authoring record, not a fabricated expert sign-off.
    allowed = ["manifest.json", "public/case.md", "public/materials.jsonl", "public/legal_sources.jsonl", "config/procedure.json", "config/permissions.json", "role_private/roles.json", "authoring/provenance.json", "authoring/sources/index.json"]
    allowed.extend(str(p.relative_to(case)) for p in sorted((case / "authoring/sources").rglob("*")) if p.is_file())
    record = {"case_id": cid, "protocol": "full-workflow-authoring-v3", "source_only_inputs": True,
              "allowed_inputs": sorted(set(allowed)), "source_files_sha256": {name: sha(case/name) for name in sorted(set(allowed))},
              "excluded_from_author_context": ["old_rollouts", "old_AI_analysis", "old_rubrics", "old_scores", "evaluation/issue_outline.json", "evaluation/rubric.json", "evaluation/review.json", "authoring/outcome_reference.json"],
              "source_origin": "user_supplied_CA-PILOT-003-004_revised_package", "generation_program": "build_assets.py",
              "source_limits": ["Actual police/audio/video/lab originals not supplied", "No historical case fact added", "Full original opinions retained privately; outcomes not used as scoring targets"],
              "formal_expert_review": False, "live_full_workflow_validation_performed": False,
              "review_status": "AI_authored_source_grounded; supplied_law_version_caveats_retained",
              "rubric_frozen_before_candidate_runs": True, "rubric_sha256": sha(work / "rubric.json"),
              "new_protocol_not_comparable_to_old_fixed_v2": True}
    write(work / "authoring_record.json", record)
    return {"case_id": cid, "points": len(rubric["points"]), "maximum": rubric["maximum_points"], "initial_documents": len(catalog)}


def main():
    result = [build_case(cid) for cid in CASES]
    entries = {}
    for p in sorted(DATA.rglob("*")):
        if p.is_file() and p.name != "assets_manifest.json":
            entries[str(p.relative_to(DATA))] = sha(p)
    write(DATA / "assets_manifest.json", {"protocol": "full-workflow-assets-v3", "case_ids": list(CASES), "sha256": entries})
    print(json.dumps({"status": "built", "paid_api_calls": 0, "cases": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
