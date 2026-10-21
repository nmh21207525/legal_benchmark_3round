#!/usr/bin/env python3
"""Deterministic authored simulation assets; original sources remain immutable.
Never invent historical provenance. All additional documents are labelled authored.
"""
from pathlib import Path
import argparse, json, shutil, hashlib, re

SPECS={
'005':{
 'folder':'005_Williams','title':'People v. Mercer：迁居与登记义务','defendant':'Evan Mercer',
 'dates':['2007-07-23','2007-08-06','2007-08-16'],'cutoff':'2006-06-03',
 'charges':[{'charge_id':'C01','name':'故意违反旧版登记义务','law':'L_PEN_290_2006','elements':['登记义务基础','在辖区居住及五个工作日','实际知情','故意不履行']}],
 'aliases':{'Bobby Lee Williams':'Evan Mercer','Gloria Chadwick':'Mara Ellis','Todd Cregar':'Jonah Pierce'},
 'roles':{'R_DEFENDANT':('Evan Mercer','紧张、防御，先讲有利经过，面对具体已取得材料才承认不利内容。'),
 'R_INTAKE_AGENT':('Mara Ellis','自信、重视表格习惯；具体追问时承认自己没有录音。'),
 'R_ASSIGNED_AGENT':('Jonah Pierce','强硬，区分自己观察与被告报告，承认没有连续监视。'),
 'R_REGISTRAR':('Leah Stone','谨慎的登记记录保管人，只证明系统和自己经手部分。'),
 'R_RECEPTIONIST':('Nora Bell','简短回答，不能从无姓名笔记认定来访者身份。'),
 'R_COUSIN':('Celia Grant','愿意帮助被告，但未目睹登记窗口或旅馆事件。'),
 'R_MOTEL_CLERK':('Owen Reed','记性一般，按值班记录回答，拒绝猜测。'),
 'R_INVESTIGATOR':('Iris Lane','按具体委托调查，区分查到和无法核实。'),
 'R_EXPERT_CAREFUL':('Dr. Rowan Hale','谨慎的记录与日历分析专家，明确法律判断留给法院。'),
 'R_EXPERT_CHEAP':('Dana Ford','较少经验，仅能给一般性整理，不能补齐没收到的材料。')},
 'docs':[
 ('D01','起诉书与有限用途约定','initial',['charge'],['R_REGISTRAR'],[
 '模拟起诉书只列C01，指控2006年6月3日前后故意违反旧版Penal Code §290(a)(1)(A)。',
 '当事人可以约定存在登记义务基础；具体既往犯罪细节不是本次登记行为的证明。',
 '控方仍须就本次实际知情、法定期限及故意不履行提供证据。',
 '起诉书同时使用进入辖区、住所变化的表述；控方必须说明依赖的具体义务。',
 '本模拟不包含量刑、三振或未取得的上诉争点。']),
 ('D02','初次接收访谈的开示副本','initial',['notice','intake'],['R_INTAKE_AGENT'],[
 'Mara Ellis于5月25日进行接收访谈，表格Yes栏被打勾，PC 290被圈出。',
 '表上手写To register, 290 PC by 5-31-06；手写日期与实际工作日计算须分别核对。',
 'Ellis说自己向被告口头说明登记义务；开示副本没有逐字对话。',
 '副本右下角的收悉签名栏被复印裁切，不能据此断言原件没有签名。',
 '表上有5月26日会见指定假释官的安排。']),
 ('D03','指定假释官报告','initial',['residence','assigned'],['R_ASSIGNED_AGENT'],[
 'Jonah Pierce记录5月26日被告说前夜住在救助站。',
 'Pierce告知救助站距学校太近，安排被告与另一人入住县内汽车旅馆。',
 '报告要求向县警长登记，但没有逐字区分市内与县内迁址。',
 'Pierce的手写提醒也为5月31日前登记。',
 '报告记两次家访未见被告，正文未写准确家访日期。',
 'Pierce未在被告离开旅馆后与其取得联系。']),
 ('D04','初始住宿时间线摘要','initial',['timeline','residence'],['R_DEFENDANT','R_ASSIGNED_AGENT'],[
 '5月24日出狱后在朋友家过一夜，因朋友搬家不能久住。',
 '5月25日晚在救助站；5月26日晚住汽车旅馆。',
 '被告说5月27日搬到市内亲属家，直到6月3日被捕。',
 '朋友家、救助站、旅馆和亲属住处属于不同来源的住宿陈述。',
 '摘要不是任何居住地点的连续监控记录。']),
 ('D05','逮捕与登记查询摘要','initial',['registry','arrest'],['R_REGISTRAR','R_ASSIGNED_AGENT'],[
 '被告于6月3日被捕，查询摘要记未完成本次登记。',
 '摘要只引用县系统的结果，未附完整查询条件和别名搜索。',
 '摘要未记本次是否有未完成预约或窗口接触。',
 '未完成登记与从未尝试登记属于不同命题。',
 '无连续定位记录能证明被告在每一时点住在哪里。']),
 ('D06','登记告知格式与背景约定','initial',['notice','prior'],['R_INTAKE_AGENT','R_REGISTRAR'],[
 '本案存在依法登记义务基础，但该基础不证明这次故意违反。',
 '格式告知把五个工作日和向负责机构登记写在相邻两行。',
 '格式文本另列变更住所的要求，不能混同各项起算事件。',
 '该版本没有解释阵亡将士纪念日如何计算。',
 '每次出狱时的实际告知仍须以经办人的个人知识核实。']),
 ('D07','初次会见委托记录','initial',['client'],['R_DEFENDANT'],[
 'Mercer希望律师先核对期限、机构和接待记录，不同意编造登记凭证。',
 '他声称曾尝试登记，但起初不主动提供之前违反假释的细节。',
 '他声称旅馆发生威胁，尚无独立材料证实绑架。',
 '他理解是否作证由本人自愿选择，沉默不等于认罪。',
 '他同意律师寻求合法的第三方记录和具体询问。']),
 ('D08','2006年5月末公开日历','initial',['calendar','deadline'],['R_REGISTRAR'],[
 '5月24日是星期三，5月25日星期四，5月26日星期五。',
 '5月27日与28日是周末，5月29日是阵亡将士纪念日。',
 '5月30日至6月2日依次为星期二至星期五。',
 '6月3日星期六被捕；不得把被捕日当完整工作日。',
 '从出狱次日到被捕前共有六个完整的非周末、非该假日日期。',
 '日历本身不决定登记义务从何时起算。']),
 ('D09','控方初始开示清单','initial',['discovery'],['R_REGISTRAR'],[
 '初始提供D01至D09，其余材料不因文件存在就自动给律师。',
 '接收原件、家访底稿、查询日志可以提出具体开示请求。',
 '第三方旅馆、救助站和预约信息需要说明调查对象或合法调取程序。',
 '材料披露不等于采纳，申请法院使用时仍须说明用途。',
 '控方不得用未披露材料无提示突袭辩方。']),
 ('D10','接收表原件的模拟扫描转写','discovery',['intake','notice','signature'],['R_INTAKE_AGENT','R_DEFENDANT'],[
 '模拟原件的收悉栏有Mercer的签名，但没有要求其复述义务的记录。',
 'Ellis手写的5月31日日期位于制式五个工作日旁。',
 '签名时间只有5月25日，没有具体分钟。',
 '手写标注没有另列周末和5月29日假日。',
 'Ellis当日经手多人，对逐字措辞主要依赖通常做法回忆。',
 '签名支持收到表格，仍需区分是否理解本次具体义务。']),
 ('D11','指定假释官家访底稿','discovery',['home_visit','residence','assigned'],['R_ASSIGNED_AGENT'],[
 '补写底稿把首次家访标在5月30日下午，第二次标在6月1日上午。',
 '两次均发现指定房间无人、被告个人物品不在。',
 'Pierce未询问旅馆每一名工作人员，也未持续守候。',
 '旅馆登记册仍显示最初的安置，不等于被告持续实际居住。',
 'Pierce不能亲眼证明被告从5月27日起每晚住在哪。',
 '原判摘要未提供这两个准确日期；这里明确为模拟补写。']),
 ('D12','县登记系统查询日志','discovery',['registry','query','record'],['R_REGISTRAR'],[
 '查询覆盖Mercer的法定姓名和一个已知别名，截止6月3日无完成登记条目。',
 '该数据库主要存放办结登记，不收录每次未完成的接待。',
 'Stone能说明查询方法，不能亲眼证明窗口发生的每次交谈。',
 '县查询不自动覆盖市警察部门的纸面预约簿。',
 '登记缺失支持未办结，不能单独证明实际知情和故意。',
 '保管链由查询人员、导出日期和页码组成，申请人可核验。']),
 ('D13','市警察部门窗口接待记录','subpoena',['window','registration','appointment'],['R_RECEPTIONIST'],[
 '5月26日早间接待便条记一名未出示身份证明的人询问登记。',
 '该便条没有姓名和可用于唯一辨认的签字。',
 '便条写让来访者带证明并预约，未记已接受登记。',
 'Bell无法仅凭便条确认来访者就是Mercer。',
 '当日排队人员较多，Bell不能给出每次接待的逐字原话。',
 '该材料既非完成登记凭证，也非已证明完全没有接待。']),
 ('D14','窗口营业及预约规则','subpoena',['hours','appointment','window'],['R_RECEPTIONIST','R_REGISTRAR'],[
 '模拟窗口通常星期一至星期五提供服务，5月29日假日休息。',
 '一般要求预约和身份证明，但紧急接待可由值班主管决定。',
 '网页告示和柜台口头安排可能不完全一致。',
 '工作人员未受命向未预约者出具正式拒绝决定。',
 '营业日安排不能取代法律对工作日的解释。']),
 ('D15','预约簿与身份核验办法','subpoena',['appointment','identity'],['R_RECEPTIONIST','R_REGISTRAR'],[
 '预约簿没有以Mercer姓名登记的已确认预约。',
 '工作人员可先记电话号码而不录姓名，存在未完成预约便条。',
 '原始便条未保存完整号码，不能回溯确认通话对象。',
 '没有预约不证明本人没有到过前台。',
 '也不能把无姓名便条直接认定为被告曾依法完成登记。']),
 ('D16','救助站住宿与迁出记录','investigate',['shelter','residence','address'],['R_DEFENDANT','R_INVESTIGATOR'],[
 '模拟住宿记录确认5月25日一晚住宿，未承诺长期床位。',
 '负责人只核验登记日期，未就法律上的住所形成意见。',
 '学校距离问题来自假释官要求，不代表救助站发出刑事驱逐命令。',
 '移住由假释官协助安排，不等于免除登记义务。',
 '完整记录可以帮助区分短暂停留与持续住居的事实。']),
 ('D17','旅馆房卡与入住账册','investigate',['motel','room','residence'],['R_MOTEL_CLERK','R_DEFENDANT'],[
 '房间由假释安置安排预付一周，账册没有记录每晚本人出入。',
 '5月26日有初次交付钥匙记录。',
 '5月27日后无人向前台主动办理被告退房。',
 '房费支付到期不等于Mercer实际仍住在房间。',
 '房卡登记没有实名刷卡轨迹。',
 'Reed只能证明账册，不能证明每次门外发生的事件。']),
 ('D18','旅馆现场与工作人员访谈','investigate',['motel','threat','scene'],['R_MOTEL_CLERK','R_INVESTIGATOR'],[
 '走廊摄像系统当时没有工作，不存在可供调取的模拟录像。',
 '值班记录提到相邻房间争吵，但没有可靠辨认参与人。',
 'Reed未目睹Mercer所称绑架或逃脱。',
 '不应将争吵记录改写成已经证实绑架。',
 '也不能仅凭无人目击断定威胁不曾发生。',
 '现场有侧门，可以作为询问逃离经过的线索，而非结论。']),
 ('D19','亲属住所访谈','investigate',['cousin','residence','family'],['R_COUSIN','R_DEFENDANT'],[
 'Celia Grant记得Mercer在5月27日带行李到访，并连续留宿。',
 '她不知道他是否白天去过登记窗口。',
 '她没有见过旅馆所称绑架者。',
 '她说他显得害怕，但不能诊断原因或真假。',
 '她承认是近亲属，愿意帮助他，需接受偏向质证。',
 '她保留了5月末的家庭购物便条，不能精确证明每一晚。']),
 ('D20','交通路线与辖区图','investigate',['jurisdiction','route','address'],['R_INVESTIGATOR','R_COUSIN'],[
 '模拟地图将旅馆标为县内非市区，亲属住处标为市界内。',
 '朋友住处与救助站位于市界内。',
 '地图只是辖区事实，不能替法院决定迁址是否重启期限。',
 '县警长窗口与市警察窗口不是同一个地点。',
 '公交连接存在，但部分时段班次间隔长。']),
 ('D21','被告后续会见备忘录','client',['client','admission','knowledge'],['R_DEFENDANT'],[
 '面对有签名接收表，Mercer承认签过并知道自己需要登记。',
 '他仍说当时不明白从何时起算以及应该向哪个窗口办理。',
 '面对家访底稿，他承认离开旅馆后未主动联系Pierce。',
 '他曾因登记问题违反假释，但不愿在法庭谈既往细节。',
 '他同意律师准确限定其说法，不把争议事件说成已经证实。',
 '若选择作证，他愿接受合法交叉询问；律师不得替其编造记忆。']),
 ('D22','登记流程专家材料要求','expert',['expert','calendar','records'],['R_EXPERT_CAREFUL','R_EXPERT_CHEAP'],[
 '谨慎专家要求接收原件、登记查询、窗口规则及日历一起核对。',
 '仅收到起诉书时，只能列出需要核验的资料，不能给案件结论。',
 '专家可以解释记录制度和日期整理，不能代替法院解释罪名。',
 '已知登记义务和是否故意未履行必须分别分析。',
 '无姓名便条的身份关联存在限制，不能给肯定鉴定。']),
 ('D23','控方补充关于告知的说明','between1',['late','notice','signature'],['R_INTAKE_AGENT'],[
 '控方在第二轮开庭前补交接收表背面复印件。',
 '背面含制式知悉栏，但无当场复述或问答记录。',
 'Ellis承认没有独立记住每一句提醒。',
 '控方认为签名、口头告知和后续行为共同支持知情。',
 '辩方可请求合理准备时间、补充询问和限制使用，不能倒改第一轮封闭记录。']),
 ('D24','补充窗口来电便条','r2',['late','window','appointment'],['R_RECEPTIONIST'],[
 '补交便条记5月30日有人询问缺身份证是否能登记。',
 '姓名栏空白，电话号码只留后三位。',
 '未能证明来电者是Mercer。',
 '便条与原来无预约结论并不逻辑矛盾。',
 '取得新资料后可重访Mercer，比较其此前说法。']),
 ('D25','假释官补充询问记录','r3',['followup','contradiction','notice'],['R_ASSIGNED_AGENT'],[
 'Pierce第三轮补充承认曾把五日简写为5月31日，没有写工作日。',
 '他强调被告已有多次登记经历。',
 '他未核实市窗口是否有人要求身份证和预约。',
 '他不能把没有亲眼看见登记尝试说成明确没有尝试。',
 '其个人结论仍须与基础事实分开。']),
 ('D26','专家书面意见模板与资格','expert',['expert','qualifications'],['R_EXPERT_CAREFUL','R_EXPERT_CHEAP'],[
 'Rowan Hale有记录系统和日历整理经验，模拟费用2400元。',
 'Dana Ford仅做一般文书整理，模拟费用800元。',
 '两者意见都取决于实际收到的材料，而非全部隐藏世界。',
 'Hale会列出不同起算前提的日期，不保证任一法律前提成立。',
 'Ford会提示材料缺少，但不能证明任何登记机关实际拒绝过Mercer。']),
 ('D27','记录保管人庭审基础清单','discovery',['foundation','custodian','registry'],['R_REGISTRAR'],[
 'Stone任职时负责系统导出、姓名匹配和保管登记。',
 '她没有参与5月25日的假释告知。',
 '她能证明导出的范围，不保证其他机关没有独立纸面记录。',
 '业务记录用途须有相应基础，不能靠文件标题自动采纳。',
 '对当事人是否故意的结论超出其个人观察。']),
 ('D28','材料差异核对表','investigate',['contradiction','records'],['R_INVESTIGATOR'],[
 '接收表和假释报告都写5月31日，但法律问题仍是五个工作日。',
 '县系统未完成登记与市窗口存在无姓名便条可以同时成立。',
 '旅馆账册预付一周与人已搬走可以同时成立。',
 '亲属称连续留宿不能证明被告从未离开房屋。',
 '调查报告应分别标注支持、削弱和仍未知的内容。']),
 ('D29','原始控告事实与证据用途','discovery',['prior','stipulation','charge'],['R_REGISTRAR','R_INTAKE_AGENT'],[
 '原型起诉只涉及本案C01，不追加其他登记罪名。',
 '存在登记基础可用约定证明，非必要犯罪情节应避免展现给陪审团。',
 '实际知情可以由允许用途的历史事实间接支持，但不能用坏人所以有罪推断。',
 '被告作证选择与既往弹劾风险应事前私下讨论。',
 '本记录不授权控方评论被告保持沉默。']),
 ('D30','第二次调查范围与未找到事项','investigate',['followup','threat','uncertainty'],['R_INVESTIGATOR','R_MOTEL_CLERK'],[
 '调查员未找到能够独立确认绑架的实名目击者。',
 '未找到只意味着本次委托未取得证据，不表示事件必然不存在。',
 '旅馆工作人员对5月27日争吵的时间回忆不一致。',
 '没有依据把任何具体人确定为绑架者。',
 '进一步调查仍需具体对象与可执行请求，不接受要求调查员保证找到无罪证据。']),
 ('D31','第三轮双方争议整理','between2',['supplement','deadline','knowledge'],['R_INTAKE_AGENT','R_ASSIGNED_AGENT','R_REGISTRAR'],[
 '本轮保留第二轮已采纳记录，之前的用途限制继续有效。',
 '控方坚持被告知情且期限已过，辩方可分别讨论起算、机构和故意。',
 '法院允许针对补交资料追加具体问题，不自动重新审全部材料。',
 '未来发表的本案上诉判决不得作为当时的正确答案交给律师。',
 '陪审团必须分别考虑每一要件，不能以未作证补足证明缺口。']),
 ('D32','准备与交付工作委托','initial_hidden',['workspace','artifacts'],['R_INVESTIGATOR'],[
 '律师应形成事实时间线、证据登记表、庭前方案和询问提纲。',
 '每次新材料到来应保留旧版本并更新适用部分。',
 '把尚未取得的文件写成已经证实属于不当陈述。',
 '准备文件本身不是陪审团证据。',
 '提交法庭的主张必须可追溯到已经合法获得的材料或当庭记录。'])]
},
'006':{
 'folder':'006_Wilson','title':'People v. Arden：酒驾致伤与抽血证据','defendant':'Lucas Arden',
 'dates':['2000-09-25','2000-10-09','2000-10-16'],'cutoff':'2000-08-16',
 'charges':[{'charge_id':'C01','name':'酒精影响下驾驶致伤','law':'L_VEH_23153_2000','elements':['驾驶','受酒精影响','其他违法行为或注意义务','致伤因果关系']},{'charge_id':'C02','name':'血液酒精含量0.08%以上驾驶致伤','law':'L_VEH_23153_2000','elements':['驾驶','驾驶时浓度0.08%以上','其他违法行为或注意义务','致伤因果关系']}],
 'aliases':{'Donald Wilson':'Lucas Arden','Jerry Chu':'Martin Vale'},
 'roles':{'R_DEFENDANT':('Lucas Arden','对事故紧张，区分记得与推测，具体追问才说明不利事实。'),
 'R_OFFICER':('Martin Vale','强调现场经验，具体追问时区分亲见与后来得知。'),
 'R_NURSE':('Elena Shaw','谨慎，只能证明自己采样和护理的部分。'),
 'R_LAB_ANALYST':('Dr. Felix Rowan','专业但保守，区分检测结果与驾驶时推算。'),
 'R_CUSTODIAN':('Ruth Kim','依照维修和记录制度作答，不是碰撞目击者。'),
 'R_MEDICAL_EXPERT':('Dr. Ada Chen','评估医疗记录和非酒精解释，不替被告编造病因。'),
 'R_EXPERT_CAREFUL':('Dr. Adrian Wells','谨慎的毒理专家，必须收到时点、血样和设备材料。'),
 'R_EXPERT_CHEAP':('Morgan Lake','经验较少，只有资料足够时才能做有限分析。'),
 'R_INVESTIGATOR':('Vera West','调查具体对象，承认记录空白和不能确定之处。'),
 'R_RECONSTRUCTION':('Noah Price','只按现场测量解释，不能把事后资料说成目击。')},
 'docs':[
 ('D01','起诉书与两项要件','initial',['charge','elements'],['R_CUSTODIAN'],[
 '本模拟只审C01 Vehicle Code §23153(a)与C02 §23153(b)。',
 '两项都需要驾驶、另一个违法行为或注意义务违反及致伤因果关系。',
 'C01的受影响与C02的驾驶时浓度不得合并为同一要件。',
 '伤害存在并不自动证明两项罪全部构成。',
 '不执行量刑或原案特别指控裁决。']),
 ('D02','到场警员初始报告','initial',['officer','observations','scene'],['R_OFFICER'],[
 'Martin Vale在碰撞后到场，见Arden眼睛很红、湿润、充血。',
 'Vale没有亲眼看见碰撞全过程。',
 'Arden受伤被送往医院，现场未完成清醒度测试。',
 '报告写车辆越过双黄线并正面相撞，该部分含调查后整理。',
 '警员关于醉酒的结论必须与其直接观察分开。']),
 ('D03','PAS筛查与告知摘要','initial',['pas','advisement','consent'],['R_OFFICER','R_DEFENDANT'],[
 '在医院Vale进行眼部跟随测试，记录Arden未能追踪手指。',
 'Vale请求进行自愿PAS，并告知不能代替被捕后化学测试义务。',
 'Arden同意PAS；摘要记结果0.09%。',
 '筛查后Vale逮捕Arden。',
 '本摘要未附设备原始输出和维护原件。']),
 ('D04','采血与0.12%摘要','initial',['blood','nonconsent','lab'],['R_OFFICER','R_NURSE','R_LAB_ANALYST','R_DEFENDANT'],[
 '逮捕后Vale告知需要血样，Arden坚持不表示同意抽血。',
 'Arden允许工作人员操作与其表示同意是不同命题。',
 '医院工作人员采血，检测记酒精含量0.12%。',
 '原判未提供完整原始报告，补写文件须明确模拟性质。',
 '采血时浓度不自动等于驾驶时浓度。']),
 ('D05','排除申请的初始争点','initial',['suppression','exigency','warrant'],['R_DEFENDANT','R_OFFICER'],[
 '申请主张已有PAS后保存酒精证据的紧急需要已减弱。',
 '控方主张事故处理、医疗情况及酒精消散需结合实际情形考虑。',
 '未表示同意不能自动终结宪法审查。',
 '本轮法官不得使用M06里第二轮才披露的正式审判记载。',
 '第一轮裁定记录在裁定后封闭，后续材料不能倒灌。']),
 ('D06','初始护理摘要','initial',['injury','medical','hgn'],['R_NURSE','R_DEFENDANT'],[
 '护理摘要记被告头部碰撞、疼痛和眼部不适。',
 '这些表现可以提出其他解释，但不等于已经证明无酒精影响。',
 '护理人员未进行完整药物或眼震鉴定。',
 '检查时没有记录被告能否完全理解所有法律告知。',
 '急诊照护与刑事采样的目的需要分别询问。']),
 ('D07','初始现场图与伤害摘要','initial',['collision','causation','injury'],['R_RECONSTRUCTION','R_NURSE'],[
 '现场图显示双黄线和两车最终位置，不是碰撞录像。',
 '另一车驾驶员和乘客受到严重伤害。',
 '摘要不能代替两名伤者的完整医疗原件和因果分析。',
 '没有来自Vale的碰撞亲见证言。',
 '道路图仅给调查线索，不能自动排除其他原因。']),
 ('D08','初次会见记录','initial',['client','consent','testimony'],['R_DEFENDANT'],[
 'Arden愿意说明自己经历，但不愿被律师代写不记得的时间。',
 '他知道自己允许过PAS，也说没有同意后续抽血。',
 '他不能提供毒理专业解释或自行逆推浓度。',
 '他希望律师核查记录后再讨论是否作证。',
 '不得把伤后记忆缺口补写成肯定否认驾驶。']),
 ('D09','初始开示目录与保留事项','initial',['discovery','records'],['R_CUSTODIAN'],[
 '初始开示D01至D09。设备、采样、流转、时点文件需具体申请。',
 '医院第三方记录需要合法授权或传票程序。',
 '控方需在补充资料取得后履行后续披露。',
 '申请某份文件不等于该文件已被读过或采纳。',
 'M06专门保留到r2_examination的stage_release事件。']),
 ('D10','PAS设备维护记录','discovery',['pas','device','calibration'],['R_CUSTODIAN','R_OFFICER'],[
 '模拟维护表有设备序列号与月度检测记录。',
 '校验表最后一页的签名不完整，需要向保管人核实。',
 '存在维护空白不等于已证明设备失准。',
 '设备保管人未亲自操作医院的本次测试。',
 '证明可靠性应分别检查设备、操作和操作员基础。',
 '本模拟不提供驾驶时的确定性浓度逆推。']),
 ('D11','警员操作资质与培训记录','discovery',['officer','qualification','hgn'],['R_OFFICER','R_CUSTODIAN'],[
 'Vale有常规酒驾调查培训，眼部测试的专项培训记录不完整。',
 '他没有毒理学学位。',
 '培训记录不代表本次测试步骤必然正确。',
 '资格争议应限制具体意见范围，而非抹去全部现场观察。',
 'Vale需要承认急诊环境与路边标准测试不同。']),
 ('D12','医院采样护理原件的模拟转写','subpoena',['blood','draw','medical'],['R_NURSE'],[
 'Elena Shaw在医院采血，使用消毒程序并填写标签。',
 '标签含日期与样本号，精确时点有更正痕迹。',
 '她没有听见全部警员告知，不能判断是否存在法律同意。',
 '她记得被告未挣扎，不能因此推断其已表示同意。',
 '她未执行酒精浓度分析。',
 '记录中无证据证明采样造成实质健康风险。']),
 ('D13','血样流转与送检登记','discovery',['chain','blood','custody'],['R_NURSE','R_CUSTODIAN','R_LAB_ANALYST'],[
 '样本交接登记中有一次接收人缩写无法辨认。',
 '封条验收栏记封口完整。',
 '缩写空白可供质询，不等于已经证明样本混淆。',
 '样本号能与0.12%报告对应，但应由知情人建立基础。',
 '当日有多份样本进入同一批检测。',
 '无工作人员声称亲见从采血到分析的全部过程。']),
 ('D14','实验室报告与质控页','discovery',['lab','quality','blood'],['R_LAB_ANALYST','R_CUSTODIAN'],[
 '模拟报告给本案样本0.12%结果，与原型转述一致。',
 '报告另有同批质控值，不能只摘最终数字。',
 '实验室人员只能说明检测时样本浓度。',
 '在时间和吸收状态不清时，不给确定的驾驶时逆推值。',
 '原始仪器数据可要求审阅，不能说报告本身已回答所有可靠性问题。',
 '样本流转空白与仪器分析误差是不同问题。']),
 ('D15','急诊时间线核对页','subpoena',['timeline','blood','medical'],['R_NURSE','R_CUSTODIAN'],[
 '原判没有精确时点；本模拟以医院各表时间存在差异作为补写情境。',
 '接诊登记和采样表相差约一段时间，但其中一页时钟未同步。',
 '不能凭这两张表精确断言采血距驾驶一定不足三小时。',
 '也不能反向断言必然超过三小时。',
 '三小时推定的基础应先由提出者证明。',
 '记录留有电话校时备注，可申请进一步核对。']),
 ('D16','现场调查与道路测量','investigate',['scene','collision','road'],['R_RECONSTRUCTION','R_INVESTIGATOR'],[
 '调查员取得现场示意和双黄线位置测量。',
 '无录像显示两车接触前全部路径。',
 '测量不能改变原型关于越线碰撞的记载，但可核对论证范围。',
 '无实际材料支持突然出现第三车的说法。',
 '不得为了辩护自行创造机械故障。']),
 ('D17','急诊非酒精解释的专家资料','expert',['medical','hgn','alternative'],['R_MEDICAL_EXPERT'],[
 '头部碰撞和疼痛可能影响眼部表现，需要结合临床资料。',
 '专家没有足够依据认定本案眼部表现完全由伤情造成。',
 '非酒精解释与酒精影响可能共存。',
 '未收到护理原件时只能给一般性说明。',
 '专家不能把可能原因说成个案确定诊断。']),
 ('D18','PAS与血检证明用途说明','expert',['expert','pas','blood'],['R_EXPERT_CAREFUL','R_LAB_ANALYST'],[
 '0.09%筛查与0.12%血检测量对象和程序不同。',
 '两数字差异本身不能证明后来继续饮酒。',
 '不能从差异自动断言任何一个设备错误。',
 '受影响驾驶与驾驶时达到0.08%仍需分别分析。',
 '专家需要实际收到D10、D13、D14、D15才能给材料充分的个案意见。']),
 ('D19','被告具体告知经历备忘录','client',['client','advisement','nonconsent'],['R_DEFENDANT'],[
 '面对告知摘要，Arden承认警员说过PAS是自愿筛查。',
 '他坚持没有表示同意抽血，允许护理操作是另一回事。',
 '他对事故后部分过程回忆零碎，不保证任何精确分钟。',
 '他不愿律师编造饮酒数量或最后饮酒时刻。',
 '他理解沉默不能作为有罪推断，作证仍有交叉询问风险。']),
 ('D20','医院记录调取与未取得材料','subpoena',['warrant','records','timeline'],['R_CUSTODIAN','R_NURSE'],[
 '医院没有保存能够录下全部法律告知的录像。',
 '可取得的是护理、标签和交接记录，不包括警员所有判断。',
 '未发现搜查令记录，不等于已经证明所有无令状行为违法。',
 '时间记录不齐应影响主张强度，不能补造精确间隔。',
 '第三方材料由合法程序取得后仍需说明法庭用途。']),
 ('D21','实验室分析员资质与证言范围','discovery',['expert','lab','qualifications'],['R_LAB_ANALYST'],[
 'Felix Rowan具有血液酒精分析经验，能解释本次报告。',
 '他不是事故目击者，也没有参加医院PAS操作。',
 '他没有独立资料证明最后饮酒时间。',
 '他说0.12%报告不会自动建立驾驶时浓度。',
 '他可以说明可确认和不能确认的质控内容。']),
 ('D22','补充操作备注M06','r2',['M06','late','breath','trial_only'],['R_OFFICER','R_DEFENDANT'],[
 'M06：Vale记正式审判中曾称三次尝试获取合格呼气样本。',
 'M06：备注把嘴唇未封紧描述为影响样本的行为。',
 '这组记载只在r2_examination的stage_release:M06后开放。',
 'M06不能反向成为第一轮抽血排除裁定的依据。',
 '即使开放，也应区分警员的观察与是否故意妨碍的推断。',
 '本模拟不增加原判未提供的三次尝试的具体时刻。']),
 ('D23','控方迟交质控附件','between1',['late','quality','chain'],['R_LAB_ANALYST','R_CUSTODIAN'],[
 '控方在第二轮补交质控批次中一个此前遗漏的说明页。',
 '该页说明封条验收但未消除所有交接缩写空白。',
 '辩方可申请准备时间、补充询问或合适的使用限制。',
 '迟交本身不自动导致全部检测结果排除。',
 '追加意见应说明新材料改变了哪一项原分析。']),
 ('D24','补充校时访谈','investigate',['followup','clock','timeline'],['R_CUSTODIAN','R_INVESTIGATOR'],[
 '工作人员确认急诊墙钟与登记系统并非全时同步。',
 '没有记录能完全还原所有分钟差异。',
 '调查员不能据此声称采血一定超过三小时。',
 '可据此要求控方说明推定的时间基础。',
 '该访谈不改变0.12%作为样本检测记载这一事实。']),
 ('D25','第三轮实验室补充答复','r3',['supplement','expert','limits'],['R_LAB_ANALYST'],[
 'Rowan补充说明未做个案吸收状态计算。',
 '他拒绝在缺少饮酒时点时提供确定逆推结论。',
 '他仍确认报告样本的检测记载为0.12%。',
 '质控问题与驾驶影响问题应分别陈述。',
 '第二轮采纳和限制继续适用于第三轮。']),
 ('D26','独立专家选择与材料要求','expert',['expert','selection'],['R_EXPERT_CAREFUL','R_EXPERT_CHEAP'],[
 'Adrian Wells模拟费用2600元，要求设备、血样、质控和时间线材料。',
 'Morgan Lake模拟费用900元，仅能作基础资料整理。',
 '没有收到材料时两人都不得读取隐藏案卷。',
 '完整意见可能同时支持检测结果并指出证明范围限制。',
 '聘请专家本身不意味着其必然给辩方有利结论。']),
 ('D27','事故记录保管说明','discovery',['record','collision','foundation'],['R_CUSTODIAN','R_RECONSTRUCTION'],[
 '事故记录含后续调查整理，不应整体说成警员亲眼所见。',
 '保管人可证明记录生成过程但不能替目击者作证。',
 '来源、形成时间和用途需要分开说明。',
 '已采纳文件仍受法官限定用途约束。',
 '材料之间没有记载的内容不能由保管人凭空确认。']),
 ('D28','伤害与因果关系核查','subpoena',['injury','causation','medical'],['R_NURSE','R_MEDICAL_EXPERT'],[
 '本模拟保留另一车驾驶员和乘客严重受伤的原型记载。',
 '核查资料支持碰撞后接受治疗，但不引入量刑特别认定。',
 '医学专家不能仅因被告0.12%就替法院认定事故法律因果。',
 '辩方可承认客观伤害同时争议其他要件。',
 '否认一切伤害与已知原型记录不符。']),
 ('D29','指示会议的资料范围','r3',['instructions','cutoff'],['R_CUSTODIAN'],[
 '模拟审理在2000年，不能把2013年及以后的血液搜查规则当时适用。',
 'CALCRIM在2006年才启用，不作为2000年的既有指示。',
 '陪审团只看当前获准实体证据和法院指示。',
 '律师的开场、结案、假设性问题均不是独立证据。',
 '未作证和被删除内容不得用于补足控方证明。']),
 ('D30','第一轮封闭记录目录','discovery',['r1','record','suppression'],['R_OFFICER','R_CUSTODIAN'],[
 '第一轮可用的是当时已经出示的观察、PAS、采样和相应证言。',
 '第二轮M06在第一轮不可见，不能被角色提示或法官摘要提前带入。',
 '后续申请须说明是在请求重新开启还是仅为实体用途，不得伪装成原裁定依据。',
 '检察官可以反驳排除理由，但不得引用隐藏材料。',
 '裁定理由应明确采用与未采用的事实范围。']),
 ('D31','第二次调查综合与未知事项','investigate',['followup','uncertainty','records'],['R_INVESTIGATOR','R_RECONSTRUCTION'],[
 '本次调查取得设备、记录制度与现场线索，没有产生新的碰撞目击者。',
 '仍未知的饮酒时点、精确延迟和吸收状态要保持未知。',
 '没有材料支持第三人驾驶或后来饮酒的确定陈述。',
 '对查不到的资料应说明所查范围。',
 '合理处理不利结果同样属于有效调查。']),
 ('D32','委托工作文件范围','initial_hidden',['artifacts','workspace'],['R_INVESTIGATOR'],[
 '律师应产出时间线、证据登记、准备方案和询问提纲。',
 '每轮取得的新资料都应在更新稿里说明影响。',
 '证据登记应区分披露、已读、提交、采纳、排除和限定用途。',
 '准备笔记不自动进入陪审团证据。',
 '本模拟不要求律师取得世界里没有的原始数据。'])]
}}


def dump(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
def jl(p,rows):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in rows),encoding='utf-8')
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def author(project,original_root,teacher):
 if any(json.loads(p.read_text()).get('version','').startswith('teacher-three-round-v2') for p in Path(project).glob('00*_*/world/manifest.json')):
  raise SystemExit('这是v1历史世界生成器；拒绝覆盖已有v2世界及其法源修复。运行实验无需重新生成世界。')
 for num,spec in SPECS.items():
  case=project/spec['folder'];w=case/'world';(w/'documents').mkdir(parents=True,exist_ok=True);(w/'roles').mkdir(exist_ok=True)
  original=original_root/f'CA-PILOT-{num}'/f'CA-PILOT-{num}'
  ref=case/'source_reference'/'original';shutil.copytree(original,ref,dirs_exist_ok=True)
  old=json.loads((original/'authoring/world.json').read_text());facts=[];anchors=[]
  for n,f in enumerate(old['facts'],1):
   text=f['text']
   for a,b in spec['aliases'].items():text=text.replace(a,b)
   fid=f'A{n:03d}';anchors.append({'fact_id':fid,'original_fact_id':f['fact_id'],'text':text,'origin':'anchor','source_locators':f.get('source_locators',[]),'epistemic_status':f.get('epistemic_status'),'roles':['R_LAB_ANALYST' if r=='R_TOXICOLOGIST' else r for r in f.get('known_by_role_ids',[])],'min_round':2 if num=='006' and f['fact_id']=='F10' else 0,'disclosure_gate':'M06' if num=='006' and f['fact_id']=='F10' else None,'doc_ids':[]})
  anchor_docs={'006':{3:'D02',4:'D02',5:'D03',6:'D03',7:'D04',8:'D14',10:'D22'},'005':{2:'D02',4:'D03',5:'D03',7:'D11',10:'D05',11:'D13',12:'D06',16:'D06'}}
  for a in anchors:
   mapped=anchor_docs[num].get(int(a['fact_id'][1:]));a['doc_ids']=[mapped] if mapped else []
  facts+=anchors;docs=[]
  for did,title,release,topics,roles,paragraphs in spec['docs']:
   fids=[]
   for i,text in enumerate(paragraphs,1):
    fid=f'{did}-F{i:02d}';fids.append(fid)
    facts.append({'fact_id':fid,'text':text,'origin':'authored','source_locators':[],'epistemic_status':'authored_simulation_record','roles':roles,'min_round':1 if release=='between1' else 2 if release in ('r2','between2') else 3 if release=='r3' else 0,'doc_ids':[did], 'disclosure_gate':('M06' if num=='006' and did=='D22' else 'release:'+did) if release in ('between1','between2','r2','r3') else None})
   method='discovery' if release=='initial_hidden' else release
   doc={'doc_id':did,'title':title,'origin':'authored','notice':'人物均为化名；本文件为模拟补写，非原案真实卷宗。','release':method,'topics':topics,'roles':roles,'fact_ids':fids,'path':f'documents/{did}.md','min_round':1 if release=='between1' else 2 if release in ('r2','between2') else 3 if release=='r3' else 0,'gate':('M06' if num=='006' and did=='D22' else 'release:'+did) if release in ('between1','between2','r2','r3') else None}
   docs.append(doc)
   (w/doc['path']).write_text('# '+title+'\n\n> 模拟补写文书；不是原案原件。\n\n| 文书控制字段 | 记载 |\n|---|---|\n| 文件编号 | '+f'CA-PILOT-{num}/{did}'+' |\n| 形成或经办来源（化名角色） | '+', '.join(roles)+' |\n| 日期与时刻 | 以正文记载为准；未记载的时点不补填 |\n| 原件/副本及认证状态 | 模拟案卷副本；法庭用途须另行认证和裁定 |\n\n## 记载内容\n\n'+ '\n\n'.join(f'[{fid}] {t}' for fid,t in zip(fids,paragraphs))+'\n',encoding='utf-8')
  jl(w/'facts.jsonl',facts);dump(w/'documents_index.json',docs)
  for rid,(name,personality) in spec['roles'].items():
   known=[f['fact_id'] for f in facts if rid in f['roles']]
   hidden=[f for f in known if any(k in next(x for x in facts if x['fact_id']==f)['text'] for k in ('承认','签名','嘴唇','未主动联系'))]
   dump(w/'roles'/f'{rid}.json',{'role_id':rid,'display_name':name,'personality':personality,'fact_ids':known,'conditional_fact_ids':hidden,'reveal_rule':'普通问题只答相关已开放事实；具体材料追问时才展开条件事实。不得跨轮泄露。','source_notice':'化名与补写记忆不是历史人物事实。'})
  for rid,name,personality in [('R_DEFENSE','辩护律师','忠实于证据，选择自己的策略。'),('R_PROSECUTOR','控方律师','在职业伦理范围内争取定罪，回应辩方真实论点，不主动替辩方承认缺口。'),('R_JUDGE','法官','具体、可追溯裁定，执行证据后果，不读取rubric。')]+[(f'R_JUROR_{i}',f'陪审员{i}','独立依准入记录逐罪判断，不看隐藏世界或rubric。') for i in range(1,7)]:
   dump(w/'roles'/f'{rid}.json',{'role_id':rid,'display_name':name,'personality':personality,'fact_ids':[]})
  stages=make_stages(num,spec)
  dump(w/'manifest.json',{'case_id':f'CA-PILOT-{num}','title':spec['title'],'version':'teacher-three-round-v1','simulation_round_count':3,'simulation_dates':spec['dates'],'substantive_law_date':spec['cutoff'],'law_cutoff':spec['dates'][0], 'dates_status':'模拟庭期设计，非历史全案庭期复原','charges':spec['charges'],'initial_documents':[d['doc_id'] for d in docs if d['release']=='initial'],'budget':{'pretrial_hours':36,'max_actions':72,'money':3000},'limits':{'run_tokens':30000000,'case_tokens':200000000,'context_tokens':150000},'required_artifacts':['case_timeline.csv','evidence_register.csv','pretrial_plan.md','examination_outline.md'],'defendant':spec['defendant'],'experts':{'R_EXPERT_CAREFUL':{'fee':2400 if num=='005' else 2600,'required_documents':['D10','D12','D13','D14','D15'] if num=='005' else ['D10','D13','D14','D15']},'R_EXPERT_CHEAP':{'fee':800 if num=='005' else 900,'required_documents':['D02','D03']}},'counts':{'facts':len(facts),'anchors':len(anchors),'authored':len(facts)-len(anchors),'documents':len(docs)}})
  dump(w/'procedure.json',stages);dump(w/'timeline.json',[{'round':i+1,'date':d,'description':['庭前听证与第一轮裁定','实体举证及新材料','补充询问、指示与裁决'][i]} for i,d in enumerate(spec['dates'])])
  dump(w/'traps.json',make_traps(num));dump(w/'motion_rules.json',make_motion_rules(num))
  prov=['# 原文锚点与模拟补写对照','', '原始数据完整保留在 source_reference/original。原材料的“不补写”是旧摘要任务的范围；本版本按教师新规格扩展，新增文件全部标 authored。原文未提供某细节不等于历史上不存在；本模拟补写不是恢复历史事实。','']
  for a in anchors:prov.append(f"- {a['fact_id']} ← {a['original_fact_id']}：{', '.join(a['source_locators'])}；{a['epistemic_status']}")
  prov+=['','全部 D01—D32 为模拟补写文书，逐句 fact_id 见 facts.jsonl；其中重复的原型内容用于构造文书，但仍不冒充取得了历史原件。', '006的A010与D22全部事实有M06披露闸门；任何角色在闸门开放前均不能得到这些事实。']
  (w/'provenance.md').write_text('\n'.join(prov)+'\n',encoding='utf-8')
  (w/'task.md').write_text(make_task(num,spec),encoding='utf-8')
  build_corpus(case,original,teacher,spec)
  (case/'runs').mkdir(exist_ok=True);(case/'scores').mkdir(exist_ok=True);(case/'usage_ledger.jsonl').touch(exist_ok=True)

def make_stages(n,s):
 ids=[('pretrial_workspace',0,'preparation',44),('r1_motion',1,'motion',4),('r1_examination',1,'examination',7),('r1_ruling',1,'ruling',0),('between_rounds_1',1,'preparation',18),('r2_opening',2,'opening',1),('r2_examination',2,'examination',12),('r2_1118',2,'motion',2),('r2_defense',2,'examination',8),('r2_ruling',2,'ruling',0),('between_rounds_2',2,'preparation',14),('r3_supplement',3,'examination',10),('r3_1118',3,'motion',2),('r3_instructions',3,'instructions',4),('r3_closing',3,'closing',1),('r3_verdict',3,'jury',0)]
 witnesses=['R_INTAKE_AGENT','R_ASSIGNED_AGENT','R_REGISTRAR','R_RECEPTIONIST'] if n=='005' else ['R_OFFICER','R_NURSE','R_LAB_ANALYST','R_CUSTODIAN']
 out=[]
 for sid,r,k,slots in ids:
  out.append({'stage_id':sid,'round':r,'kind':k,'slots':slots,'date':s['dates'][max(0,r-1)],'witnesses':witnesses if r in (1,2,3) else [],'release_documents':[d[0] for d in s['docs'] if (sid=='r2_examination' and d[2]=='r2') or (sid=='r3_supplement' and d[2]=='r3') or (sid=='between_rounds_1' and d[2]=='between1') or (sid=='between_rounds_2' and d[2]=='between2')],'next_stage_id':None})
 for i,x in enumerate(out):x['next_stage_id']=out[i+1]['stage_id'] if i+1<len(out) else 'ended'
 return out

def make_traps(n):
 return [
 {'trap_id':'T_FOUNDATION','phase':'r1_examination','ground':'foundation','text':'请直接告诉法庭，被告显然就是故意犯罪，对不对？'},
 {'trap_id':'T_CHARACTER','phase':'r2_examination','ground':'character','text':'他以前就有犯罪记录，这种人这次也肯定会犯罪。'},
 {'trap_id':'T_HEARSAY','phase':'r2_examination','ground':'hearsay','text':'匿名来电者告诉我，被告肯定有罪。'},
 {'trap_id':'T_VOUCHING','phase':'r3_closing','ground':'vouching','text':'我以检察官的个人信誉担保，我们的证人绝不会说谎。'},
 {'trap_id':'T_GRIFFIN','phase':'r3_closing','ground':'silence','text':'被告本可以上证人席解释，但他没有作证，说明他没有清白解释。','unless_defendant_testified':True},
 {'trap_id':'T_RD','phase':'r3_instructions','ground':'misstates_law','text':'合理怀疑就是任何凭空想象的疑问，只有完全不可能有罪才可以无罪。'},
 {'trap_id':'T_BURDEN','phase':'r3_closing','ground':'burden','text':'辩方必须证明被告清白，否则各位就应当定罪。'},
 {'trap_id':'T_RETRO','phase':'r2_examination','ground':'record_boundary','text':'现在才披露的材料也证明第一轮法官当时就应该知道这些内容。'}]

def make_motion_rules(n):
 common=[{'motion_id':'limit_prior','title':'既往记录限定用途','legal_ids':['L_EVID_352','L_EVID_355','L_EVID_1101'],'requires_read':['D01'],'effect':'limit_prior'}, {'motion_id':'late_relief','title':'迟交材料准备及补充询问','legal_ids':['L_EVID_352','L_EVID_355'],'requires_late_disclosure':True,'effect':'late_relief'},{'motion_id':'acquittal_1118','title':'控方举证后无罪判决申请','legal_ids':['L_PEN_1096'],'effect':'preserve_motion'}]
 if n=='005':return common+[{'motion_id':'registration_records','title':'登记记录基础与范围','legal_ids':['L_EVID_702','L_EVID_1401'],'requires_read':['D12','D13'],'effect':'limit_registry'},{'motion_id':'deadline_instruction','title':'期限与实际知情指示','legal_ids':['L_PEN_290_2006','L_CALCRIM_1170_2006_EXCERPT'],'requires_read':['D08','D10'],'effect':'deadline_clarification'}]
 return common+[{'motion_id':'suppress_blood','title':'无令状抽血排除申请','legal_ids':['L_SCHMERBER_1966','L_VEH_23612_2000'],'requires_read':['D03','D04','D12'],'effect':'blood_ruling'},{'motion_id':'limit_lab','title':'浓度与驾驶时推断的基础限制','legal_ids':['L_EVID_720','L_EVID_801','L_EVID_1401'],'requires_read':['D13','D14','D15'],'effect':'limit_inference'},{'motion_id':'M06_boundary','title':'M06用途与第一轮封闭记录','legal_ids':['L_EVID_355'],'requires_gate':'M06','effect':'M06_boundary'}]

def make_task(n,s):
 return f'''# {s['title']}｜辩护律师委托

你代理{s['defendant']}，处理本题公开起诉事项。人物为化名；这是以原案为锚点、补写并冻结的模拟案件，不是原案卷宗复原。

工作范围：读取授权案卷，核查材料，提出具体开示、合法第三方取证和调查请求，私下会见当事人，视需要聘请专家。在时间与费用约束下形成并更新 case_timeline.csv、evidence_register.csv、pretrial_plan.md、examination_outline.md。工作文件不是法庭证据。

三轮：第一轮审前证据听证；第二轮举证与询问；第三轮定向补充、控方举证后申请、指示、控方结案→辩方结案→控方反驳与裁决。轮间会有新材料，须自行决定如何核查、更新和使用。

只使用本地授权材料与法律库。可检索后全文阅读；法律有适用时间和核验状态，未来材料不等于当时有效法律。没有权限或不存在的内容不得凭空补造。

每段控方发言和问答都有一次不耗法律行动次数的反应窗口，动作可为异议、移请删除、请求指示或不采取行动。应针对实际出现的话、说明具体理由和补救；不要因为有窗口就假定该句有错。

被告是否作证须本人自愿选择。披露、阅读、提交、采纳、限定用途和排除分别记录。第一轮程序记录不自动进入陪审团；第二轮有效记录延续到第三轮。

{'M06只在r2_examination的stage_release:M06事件后开放给各角色；不能倒用于第一轮裁定。' if n=='006' else '期限起算、实际知情、住所和故意须分别论证；保留有争议的说法与来源局限。'}
'''

def build_corpus(case,original,teacher,spec):
 corpus=case/'legal_corpus';raw=corpus/'raw';raw.mkdir(parents=True,exist_ok=True);rows=[]
 for l in [json.loads(x) for x in (original/'public/legal_sources.jsonl').read_text().splitlines() if x.strip()]:
  source=original/l['source_snapshot_path'];target=raw/source.name;shutil.copyfile(source,target)
  text='\n'.join(x['text'] for x in l['paragraphs'])
  row={'legal_id':l['legal_source_id'],'title':l.get('section',l['legal_source_id']),'text':text,'url':l.get('official_url'),'retrieved_at':'2026-10-02','snapshot_path':str(target.relative_to(corpus)),'sha256':sha(target),'text_sha256':hashlib.sha256(text.encode()).hexdigest(),'effective':l.get('applies_as_of',spec['cutoff']),'distractor':False,'cutoff_ok':True,'verification':l.get('version_status'),'origin':'user_supplied_source_snapshot','tags':[l.get('authority_type','law')]}
  if 'predecessor' in source.name:row['verification']='partial：从后来的官方重述恢复历史核心条文；完整2000版本链未独立确认。'
  rows.append(row)
 # 带原始文本的教师法源作为额外检索候选；不复制与新题无关的量刑/本案答案。
 tr=teacher/'003_Moore/legal_corpus'
 for t in [json.loads(x) for x in (tr/'corpus.jsonl').read_text().splitlines() if x.strip()]:
  if t['legal_id'] in ('L-PEN602O','L-HSC11550A','L-CALCRIM2932','L-CALCRIM2400','L-CALCRIM1703','L-SB602-2024'):continue
  if len(rows)>=42:break
  effective=str(t.get('effective') or t.get('year') or '9999')
  if len(effective)==4:effective+='-01-01'
  # 教师的2026指示文本即使编号早已存在，也不能冒充历史逐字版本。
  future=effective>spec['dates'][0] or (t['legal_id'].startswith('L-CALCRIM') and '2000' in spec['dates'][0])
  dst=raw/(t['legal_id']+'_teacher_extract.json');dump(dst,t)
  rows.append({'legal_id':t['legal_id'],'title':t['title'],'text':t.get('text',''),'url':t['url'],'retrieved_at':t.get('retrieved','2026-10-19'),'snapshot_path':str(dst.relative_to(corpus)),'sha256':sha(dst),'text_sha256':hashlib.sha256(t.get('text','').encode()).hexdigest(),'effective':effective,'distractor':future or bool(t.get('distractor')),'cutoff_ok':not future,'verification':'教师已保存的法源文本；历史逐字版本以该条notes为准。 '+t.get('notes',''),'origin':'teacher_supplied_corpus','tags':t.get('tags',[])})
 jl(corpus/'corpus.jsonl',rows);dump(corpus/'index.json',[{k:v for k,v in x.items() if k!='text'} for x in rows])
 (corpus/'README.md').write_text('# 本地法律库\n\n只检索冻结库，不联网。每条保存URL、抓取日期、原始快照和sha256、适用日期、干扰项及历史核验状态。partial不得写成已完成逐字历史核验。原案后出的判决只放在source_reference，绝不作为候选可检索答案。\n',encoding='utf-8')

if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--project',type=Path,required=True);ap.add_argument('--original',type=Path,required=True);ap.add_argument('--teacher',type=Path,required=True);a=ap.parse_args();author(a.project,a.original,a.teacher)
