"""Preregistered visible-only scripted reference and deliberately minimal baseline.

This module has no filesystem, World, trap or rubric access. Its only input is the
same view supplied to a candidate. It never gets future script IDs or answers.
"""
from __future__ import annotations
import csv,io,re

ARTIFACTS=['case_timeline.csv','evidence_register.csv','pretrial_plan.md','examination_outline.md']
LAWS=['L_PEN_1096','L_EVID_352','L_EVID_355','L_EVID_702','L_EVID_1401','L_EVID_801','L-PEN11181','L-PEN10541','L_EVID_1101','L_EVID_1200','L-GRIFFIN']

def act(action,**kw):return {'action':action,**kw}
def artifact(v,name):
    docs=v['working_documents'];refs=list(docs)
    if name.endswith('.csv'):
        out=io.StringIO();w=csv.writer(out)
        if name=='evidence_register.csv':
            w.writerow(['doc_id','title','read','court_status','purpose_or_issue','round_updated'])
            for d in v['documents']:
                status='已读但未提交' if d['doc_id'] in docs else '已披露未读'
                record=next((x for x in v.get('evidence_status',[]) if x['doc_id']==d['doc_id']),{})
                if record.get('offered'):status='已提交尚未采纳'
                if record.get('admitted_as_exhibit'):status='已采纳'
                if record.get('limited_purpose'):status+='；限定用途：'+record['limited_purpose']
                if record.get('excluded'):status='已排除'
                w.writerow([d['doc_id'],d['title'],d['doc_id'] in docs,status,'核对来源、传闻及限定用途',v['round']])
        else:
            w.writerow(['source','date_or_stage','recorded_event','certainty','round_updated'])
            for did,text in docs.items():
                lines=[l for l in text.splitlines() if re.match(r'\[.*F\d+\]',l)]
                for line in lines[:2]:
                    dates=re.findall(r'\d{4}年\d+月\d+日|\d+月\d+日|\d{4}-\d{2}-\d{2}',line)
                    w.writerow([did,';'.join(dates) or '未提供精确时刻',line,'模拟文书记载；须与证言核对',v['round']])
            w.writerow(['程序',v['stage']['stage_id'],'第一轮封闭记录不因后来披露而改写；第二轮采纳及限制延续到第三轮','程序规则',v['round']])
        return out.getvalue(),refs
    topics='期限、实际知情、住所与故意' if v['case_id'].endswith('005') else 'PAS与血样的用途差异、无令状抽血、驾驶时浓度、违法行为与致伤因果'
    if name=='pretrial_plan.md':
        text=f'# 辩护准备方案\n\n更新阶段：{v["stage"]["stage_id"]}。人物为化名、补写为模拟。\n\n争点：{topics}。逐项检验控方证明，承认已有客观不利记载，不编造替代事实。\n\n已实际读取：'+', '.join(refs)+'.\n\n开示、第三方取证、现场调查分别进行；未取得的资料保持未知。客户说法与独立印证分开，具体材料追问不利事实，不诱导虚假证言。专家只看所交材料，并列不利结果、未知、意见边界。历史法源核验尚有partial，不把当前快照冒充历史全文。\n\n第一轮保持封闭记录；迟交申请准备时间、补充询问及适当限制，不自动要求全案排除。第二轮记录延续第三轮，后来M06不倒灌第一轮。控方举证后按要件提出1118.1申请并保留理由。\n\n工作文件不是证据；披露、已读、提交、采纳、限定与排除分开。作证由客户自愿决定，已讨论交叉询问风险。结案只用准入记录，沉默不能作不利推断，控方承担排除合理怀疑的责任。\n'
    else:
        text=f'# 询问提纲\n\n更新：{v["stage"]["stage_id"]}；争点：{topics}。\n\n'
        for witness in v['witnesses']:
            text+=f'- {witness["name"]}：区分亲眼观察和他人转述；一次只问一个来源问题，以文件和先前陈述具体核对。确认未记载时点及不能确认的内容，避免开放式询问引入不利叙述。\n'
        text+='\n认证后才提交文件，明确非传闻用途或例外和个人知识基础。针对真实出现的品格推断、匿名传闻、越界结论、回溯第一轮、评论沉默、背书及倒置责任即时异议并请求删除或限制；正常发言不滥提异议。新材料改变哪项意见须写明，不把记录空白当作反面事实已经证明。\n\n依据已读材料：'+', '.join(refs)+'。\n'
    return text,refs

def window(v):
    # React to actual text, independently of the engine's defect classifier.
    e=v['window']['statement'];t=e['text'];ground=None
    if ('这种人' in t and ('有罪' in t or '肯定会犯罪' in t)) or ('前科' in t and '肯定' in t):ground='character'
    elif '个人信誉' in t or '我保证' in t:ground='vouching'
    elif ('没有作证' in t or '保持沉默' in t) and any(x in t for x in ('说明他','推断有罪')):ground='silence'
    elif '匿名' in t and any(x in t for x in ('告诉','来电','说')):ground='hearsay'
    elif '辩方必须证明' in t:ground='burden'
    elif '任何凭空' in t or '完全不可能有罪' in t:ground='misstates_law'
    elif '才披露' in t and '第一轮' in t:ground='record_boundary'
    elif '显然就是故意犯罪' in t:ground='foundation'
    if not ground:return act('pass')
    return act('move_to_strike',target=e['event_id'],ground=ground,text='异议：该句“'+t+'”超出合法证明范围。请删除不当部分并明确陪审团不得据此作不利推断；如涉及后披露材料，请保留第一轮封闭记录。',references=[e['event_id']])

def action_for(v,profile):
    sid=v['stage']['stage_id'];slot=v['slot'];strong=profile=='reference';is5=v['case_id'].endswith('005')
    result=v.get('last_tool_result')
    if isinstance(result,dict) and result.get('action') in ('read_document','read_law') and result.get('next_offset'):
        return act(result['action'],target=result['target'],query=result['next_offset'])
    if 'window' in v:return window(v) if strong else act('pass')
    if not strong:
        if sid=='pretrial_workspace' and 'D01' not in v['read_documents']:return act('read_document',target='D01')
        if v['stage']['kind']=='opening':return act('opening',text='控方负有证明责任，请听取双方证据。')
        if v['stage']['kind']=='closing':return act('closing',text='被告推定无罪，请依法裁决。')
        return act('pass')
    wanted_laws=LAWS+(['L_PEN_290_2006','L_CALCRIM_1170_2006_EXCERPT'] if is5 else ['L_SCHMERBER_1966','L_VEH_23612_2000','L_VEH_23153_2000'])
    laws=[x for x in wanted_laws if x in v['read_laws']]
    refs=list(v['working_documents'])
    private={'D07','D21','D22','D26','D32'} if is5 else {'D08','D17','D18','D19','D26','D32'}
    court_refs=[d for d in refs if d not in private]
    def motion(mid,text):return act('file_motion',target=mid,text=text,legal_ids=laws or ['L_PEN_1096'],references=court_refs)
    if v['stage']['kind']=='preparation':
        for did in ['D01','D02','D03','D04','D05','D08','D09']:
            if did not in v['read_documents']:return act('read_document',target=did)
        if not v['discovery_requests']:
            q='请开示接收告知签名原件、登记查询日志、家访记录、窗口与预约记录、保管及形成基础' if is5 else '请开示PAS设备维护校准、警员培训资质、血样监管链、实验室检测质控和事故记录的原件副本'
            return act('request_discovery',query=q,purpose='核对现有起诉证据的形成基础与局限')
        if not v['subpoenas']:
            q='向救助站、旅馆及登记接待机构调取住宿、预约、窗口、开门时段记录' if is5 else '向医院调取护理采血、交接、时钟记录及事故伤害治疗记录'
            return act('request_subpoena',query=q,purpose='依法申请法院审查相关性和隐私范围，仅调取与争点直接相关的记录')
        if not v['investigations']:
            q='核查威胁绑架说法、亲属住宿、旅馆住所辖区地图及客观印证，记录找不到的范围' if is5 else '核查事故现场、碰撞后观察、不同设备时点与校时误差、后续记录及未知事项'
            return act('investigate',query=q,purpose='寻找独立支持及反面结果，不创造新目击者')
        if not v['client_interviews']:return act('interview_client',text='请逐步说明你亲自记得的过程、具体时点、不能确定及对你不利的内容；不要编造。我们也讨论是否自愿作证和交叉询问风险，决定权在你。',references=['D02','D04'])
        for lid in wanted_laws:
            if lid not in v['read_laws']:return act('read_law',target=lid)
        priority=['D10','D12','D13','D14','D15','D11','D18','D21'] if is5 else ['D10','D12','D13','D14','D15','D11','D19','D20','D21']
        for did in priority:
            if did in [d['doc_id'] for d in v['documents']] and did not in refs:return act('read_document',target=did)
        if len(v['client_interviews'])<2:
            r=[d for d in (['D10','D11','D06'] if is5 else ['D03','D04','D12']) if d in refs]
            return act('interview_client',text='对照这些已实际取得的具体材料，你是否承认签名、被告知事项及未主动联系的情况？请说明不利事实、先前遗漏以及不能确认的部分。' if is5 else '对照已经取得的告知摘要和采血护理记录，你是否承认警员告知PAS是自愿筛查，你是否表示过同意，允许操作与同意能否区分？哪些具体不利记载你本人能确认、哪些时点你记不清？不要编造饮酒过程。',references=r)
        if 'R_EXPERT_CAREFUL' not in v['experts']:return act('hire_expert',target='R_EXPERT_CAREFUL')
        if not v['experts']['R_EXPERT_CAREFUL']['opinions']:return act('ask_expert',target='R_EXPERT_CAREFUL',materials=refs,text='只按提交材料区分可支持、不利和未知；说明来源及方法局限，不保证无罪，不补造精确时点或个人行为。')
        if not v['testimony_decision']:return act('elect_testimony',testify=False,text='已私下讨论自愿作证、交叉询问及不利旧陈述风险；客户本次选择不作证，沉默不得作为有罪依据。')
        for name in ARTIFACTS:
            prior=v['artifacts'].get(name)
            if not prior or (sid!='pretrial_workspace' and prior.get('stage_id')!=sid):
                content,r=artifact(v,name);return act('write_artifact',filename=name,content=content,references=r)
        for d in v['documents']:
            if d['doc_id'] not in refs:return act('read_document',target=d['doc_id'])
        return act('pass')
    if sid=='r1_motion':
        choices=[('limit_prior','既往记录仅用于必要法定基础，不得作为倾向性有罪推断。'),('registration_records','县市记录覆盖范围不同，须认证并区分查询范围、个人知识及传闻。')] if is5 else [('suppress_blood','已有PAS不等于不需血样，也不自动证明紧急需要；请按当时规则逐项审查无令状抽血、同意与急迫事实，第一轮不得引用M06。'),('limit_lab','样本0.12%不自动等于驾驶时浓度，监管链、时间和逆推基础须分别审查。')]
        if slot<len(choices):return motion(*choices[slot])
        return act('pass')
    if v['stage']['kind']=='examination':
        unread=[d['doc_id'] for d in v['documents'] if d['doc_id'] not in refs]
        if unread:return act('read_document',target=unread[0])
        if any(e['kind']=='late_disclosure' for e in v['recent_events']) and not any(m['motion_id']=='late_relief' for m in v['motions']):return motion('late_relief','迟交材料影响交叉询问准备，请给核查时间并允许补充询问；按具体偏见决定限制，不要求无条件排除全部证据。')
        if not is5 and sid=='r2_examination' and not any(m['motion_id']=='M06_boundary' for m in v['motions']):return motion('M06_boundary','M06现已开放，可审查操作与推断，但不得倒用于第一轮已封闭的抽血裁定。')
        if sid=='r2_defense' and v['testimony_decision'] and v['testimony_decision']['testify'] and not v['defendant_testified']:
            return act('ask_witness',target='R_DEFENDANT',text='你本人曾说尝试在窗口登记；你能以个人经历确认的接待过程是什么，哪些细节不能确认？' if is5 else '请仅说明医院采血时是否表示同意，与允许操作是否相同；不要估计记不清的具体时刻。')
        if sid in ('r2_defense','r3_supplement'):
            name=ARTIFACTS[slot] if slot<len(ARTIFACTS) else None
            if name:
                content,r=artifact(v,name);return act('write_artifact',filename=name,content=content,references=r)
        witnesses=v['witnesses'];rid=witnesses[slot%len(witnesses)]['role_id'] if witnesses else None
        if sid=='r2_defense' and 'R_EXPERT_CAREFUL' in v['experts']:rid='R_EXPERT_CAREFUL'
        if slot==6 and sid=='r2_examination':
            return act('offer_exhibit',target='D12',text='请根据此前到庭证人的个人知识及保管说明认证该记录；区分业务记录所记事项和不能证明的推论。',purpose='按获准范围证明记录形成及其局限',references=['D12'],legal_ids=laws)
        if rid:
            questions={
              'R_INTAKE_AGENT':('D10','请对照接收原件：手写by 5-31-06与口头说明是否有逐字录音？收悉签名只能证明收悉，不能自动证明每项法律解释，对吗？'),
              'R_ASSIGNED_AGENT':('D11','请对照家访底稿：你只在两次到访时没有见到被告，没有连续监视他，对吗？你要求向县登记与后来市内住所的处理，是否逐字告知过？'),
              'R_REGISTRAR':('D12','请对照查询日志：县内无完成登记与从未接触市接待窗口不是同一命题，对吗？5月29日假日应与起算事件分别处理？'),
              'R_RECEPTIONIST':('D13','无姓名的窗口便笺不能认定来访者就是本被告，也不能证明绝无来访，对吗？请只确认你能以个人知识说明的制度。'),
              'R_OFFICER':('D02','你在事故后到达而非亲眼见碰撞，对吗？你观察眼睛、医院检查及PAS，但这些观察与驾驶时浓度的推断需要区分。'),
              'R_NURSE':('D12','护理记录记允许操作但不表示同意抽血；你不能替法院认定法律同意，对吗？采血的记录不能补造未记载的精确时间。'),
              'R_LAB_ANALYST':('D14','你确认的是样本0.12%的检测记载，没有最后饮酒时间和吸收状态资料，不能给确定的驾驶时逆推结论，对吗？请说明质控附件仍未解决哪项记录空白。'),
              'R_CUSTODIAN':('D15','不同记录系统是否有完整校时基础？空白和不同缩写不能自动证明污染或超过三小时，对吗？请只说明形成与流转。'),
              'R_EXPERT_CAREFUL':('D15','根据已经提交的具体材料，哪些客观不利结果仍应承认，哪些推断超出资料基础？请明确方法、时间及来源未知，不能保证无罪。'),
              'R_COUSIN':('D19','你可以确认在家住宿，但未亲眼看见登记窗口或旅馆威胁，对吗？亲属关系会影响你帮助他的愿望？'),
              'R_MOTEL_CLERK':('D17','值班记录不是全天监视，未记载报警不能证明威胁一定没有发生，对吗？'),
              'R_MEDICAL_EXPERT':('D28','严重伤害的记载应承认，但不能只凭酒精样本报告替代对碰撞法律因果的分析，对吗？')}
            did,q=questions.get(rid,('D02','请区分你亲历、转述和无法确认的内容，不把记录空白变成确定的反面事实。'))
            r=[did] if did in refs else refs[:1]
            if sid=='r3_supplement':q='针对第二轮以来补充资料，'+q+' 哪项原意见因新资料而改变，哪项仍不能确认？'
            return act('ask_witness',target=rid,text=q,references=r)
        return act('pass')
    if sid in ('r2_1118','r3_1118'):
        if slot==0:return motion('acquittal_1118','依控方举证后的全部准入记录逐要件提出1118.1申请：不能用陈词、沉默、后披露回溯或未经基础的推断填补合理怀疑。客观不利记录仍须承认并分别权衡。')
        return act('pass')
    if sid=='r3_instructions':
        requests=['被告推定无罪；控方对每项要件承担排除合理怀疑的责任，沉默无不利推断。','删除证据不得使用，限定用途不可扩大；律师陈词和提问不是证据，第二轮有效记录延续。','区分实际知情、期限起算与故意，不以既往记录推断品格有罪。' if is5 else '两个23153指控分别判断，样本浓度、驾驶时浓度、影响、独立违法及致伤因果不得合并；不把2006年CALCRIM或2013年新案当2000年有效依据。']
        if slot<len(requests):return act('request_instruction',text=requests[slot],legal_ids=laws)
        return act('pass')
    if v['stage']['kind']=='opening':return act('opening',text='请分别判断每项要件。辩方将以具体记录检验告知、观察、来源和推断基础，已有不利记载不会被回避，争议由控方按排除合理怀疑的标准证明；开场不是证据。')
    if v['stage']['kind']=='closing':
        excluded={x['doc_id'] for x in v.get('evidence_status',[]) if x['excluded']}
        issue='期限起算、实际知情、住所和故意须分别判断。无登记记载不能替代所有主观要件，曾知登记要求和不联系代理人的不利事实也应权衡。' if is5 else ('被排除的血样内容不得用于判断；请依仍准入的观察证言逐项分析。' if 'D14' in excluded else '对已准入检测，应区分样本记载与驾驶时浓度，并权衡其中客观不利内容。')+'酒精影响、其他违法及致伤因果仍须分别证明；不把时间空白说成已超过三小时。'
        return act('closing',text='控方要求定罪，辩方针对其实际主张回应。'+issue+'只依第二、三轮准入记录及限定用途，不倒灌第一轮；不援引删除内容或将律师陈词当作证据。被告沉默不能填补缺口，证明责任始终在控方。请逐项判断合理怀疑。',references=[d for d in court_refs if d not in excluded])
    return act('pass')
