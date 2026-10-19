"""Versioned source-bound courtroom environment; no rubric or outcome inputs.

This module adapts only the two frozen pilot packages. It does not mutate their
files or the old court_agent implementation. Authored witness speech preserves
the attribution and uncertainty of each source assertion. Original provenance
stays in author-only manifests; courtroom actors receive scoped role facts only.

The public simulation stipulation bridges the archive's excerpt format. It is a
new task-design assumption, NOT a claim that the historical parties agreed to
this exact substitute. Credibility, hearsay purposes, relevance and verdicts
remain contested. Native model tools remain governed by the existing backend.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from court_agent.data import CaseData
from court_agent.engine import CourtEngine, COURT, LAWYERS, RunStopped
from court_agent.schema import BASE_PROMPT, LAWYER_PROMPT as CORE_LAWYER_PROMPT, WITNESS_SCHEMA, decision_schema

ENVIRONMENT_VERSION = "source-environment-v2.0.0"

WITNESS_MAPS = {'CA-PILOT-003': {'R_OFFICER': {'F01': {'expected_original_content': '2013年4月19日，B的女儿C拨打911后，内华达县治安官办公室警员D到达B的住宅；C允许D进入。',
                                        'spoken_content': '2013年4月19日，我到达B的住宅，C允许我进入。',
                                        'knowledge_boundary': '本人到场和获准进入；不扩展为亲耳听到911原话。',
                                        'source_file': 'authoring/sources/source22/opinions/2798364/paragraphs.json',
                                        'source_anchors': [{'locator': 'CL-O2798364-P0033',
                                                            'quote': 'responded on April 19, 2013 and was '
                                                                     'permitted to enter the home by the '
                                                                     'daughter of the'}]},
                                'F03': {'expected_original_content': 'D先询问B。B向D报告，为避开与A的争执，她把自己锁在卧室里。',
                                        'spoken_content': '我先询问B。B告诉我，她为避开与A的争执，把自己锁在卧室里。',
                                        'knowledge_boundary': '听B转述；不表示本人目睹争执或锁门。',
                                        'source_file': 'authoring/sources/source22/opinions/2798364/paragraphs.json',
                                        'source_anchors': [{'locator': 'CL-O2798364-P0034',
                                                            'quote': 'victim. Deputy Grotke first '
                                                                     'interviewed the victim, who reported '
                                                                     'that she had locked herself'},
                                                           {'locator': 'CL-O2798364-P0035',
                                                            'quote': 'in her bedroom to escape an argument '
                                                                     'with Moore. The victim also reported '
                                                                     'that she had'}]},
                                'F04': {'expected_original_content': 'B向D报告，她要求A离开她的住宅，A拒绝离开。该段是B的报告，未提供当时对话逐字稿。',
                                        'spoken_content': 'B告诉我，她要求A离开她的住宅，A拒绝离开。',
                                        'knowledge_boundary': '听B转述；不表示本人听见最初的离开要求或拒绝原话。',
                                        'source_file': 'authoring/sources/source22/opinions/2798364/paragraphs.json',
                                        'source_anchors': [{'locator': 'CL-O2798364-P0035',
                                                            'quote': 'in her bedroom to escape an argument '
                                                                     'with Moore. The victim also reported '
                                                                     'that she had'},
                                                           {'locator': 'CL-O2798364-P0036',
                                                            'quote': 'asked Moore to leave her home, which '
                                                                     'he refused.'}]},
                                'F05': {'expected_original_content': 'D随后在住宅后部露台找到A。',
                                        'spoken_content': '我随后在住宅后部露台找到A。',
                                        'knowledge_boundary': '本人现场观察；不补充停留时长、控制措施或离开机会。',
                                        'source_file': 'authoring/sources/source22/opinions/2798364/paragraphs.json',
                                        'source_anchors': [{'locator': 'CL-O2798364-P0037',
                                                            'quote': 'Deputy Grotke then found Moore on '
                                                                     'the back deck of the home. He '
                                                                     'admitted that he'}]},
                                'F06': {'expected_original_content': '判决记载A承认有人要求他离开；没有给出原话，也没有记载A另行承认使用具体药物。',
                                        'spoken_content': 'A向我承认，有人要求他离开。',
                                        'knowledge_boundary': 'A对本人所作陈述；不补充逐字原话，不转化为承认吸毒。',
                                        'source_file': 'authoring/sources/source22/opinions/2798364/paragraphs.json',
                                        'source_anchors': [{'locator': 'CL-O2798364-P0037',
                                                            'quote': 'Deputy Grotke then found Moore on '
                                                                     'the back deck of the home. He '
                                                                     'admitted that he'},
                                                           {'locator': 'CL-O2798364-P0038',
                                                            'quote': 'had been asked to leave and '
                                                                     'displayed objective symptoms of '
                                                                     'being under the influence, such'}]},
                                'F07': {'expected_original_content': 'D观察到A站立不稳、坐立不安和瞳孔收缩等表现。',
                                        'spoken_content': '我观察到A站立不稳、坐立不安和瞳孔收缩等表现。',
                                        'knowledge_boundary': '本人观察；症状本身不等于实验室确认了具体物质。',
                                        'source_file': 'authoring/sources/source22/opinions/2798364/paragraphs.json',
                                        'source_anchors': [{'locator': 'CL-O2798364-P0038',
                                                            'quote': 'had been asked to leave and '
                                                                     'displayed objective symptoms of '
                                                                     'being under the influence, such'},
                                                           {'locator': 'CL-O2798364-P0039',
                                                            'quote': 'as instability, fidgeting, and '
                                                                     'constricted pupils. Deputy Grotke '
                                                                     'conducted a drug abuse'}]},
                                'F08': {'expected_original_content': 'D进行了药物滥用评估，并判断A受中枢神经系统兴奋剂影响。此处是D的判断，不是实验室鉴定。',
                                        'spoken_content': '我进行了药物滥用评估，并判断A受中枢神经系统兴奋剂影响。',
                                        'knowledge_boundary': '本人的评估意见；不是实验室鉴定，不新增资历或检验结果。',
                                        'source_file': 'authoring/sources/source22/opinions/2798364/paragraphs.json',
                                        'source_anchors': [{'locator': 'CL-O2798364-P0039',
                                                            'quote': 'as instability, fidgeting, and '
                                                                     'constricted pupils. Deputy Grotke '
                                                                     'conducted a drug abuse'},
                                                           {'locator': 'CL-O2798364-P0040',
                                                            'quote': 'evaluation and concluded that Moore '
                                                                     'was under the influence of a central '
                                                                     'nervous system'},
                                                           {'locator': 'CL-O2798364-P0041',
                                                            'quote': 'stimulant (“CNS”). Grotke then '
                                                                     'arrested Moore for being under the '
                                                                     'influence in violation of'}]},
                                'F09': {'expected_original_content': 'D随后以受管制物质影响和非法滞留两项涉嫌违法行为逮捕A。',
                                        'spoken_content': '我随后以涉嫌受管制物质影响和非法滞留逮捕A。',
                                        'knowledge_boundary': '本人执法行为；逮捕不是有罪证明。',
                                        'source_file': 'authoring/sources/source22/opinions/2798364/paragraphs.json',
                                        'source_anchors': [{'locator': 'CL-O2798364-P0041',
                                                            'quote': 'stimulant (“CNS”). Grotke then '
                                                                     'arrested Moore for being under the '
                                                                     'influence in violation of'},
                                                           {'locator': 'CL-O2798364-P0042',
                                                            'quote': 'Section 11550(a), and for '
                                                                     'trespassing in violation of Penal '
                                                                     'Code section 602(o).'}]},
                                'F10': {'expected_original_content': '判决另记载D观察到：脉搏每分钟112次、水平凝视眼球震颤提前出现、瞳孔收缩，以及Romberg测试的计时失败。没有给出具体瞳孔尺寸、震颤角度或测试时长。',
                                        'spoken_content': '我观察到A的脉搏为每分钟112次、水平凝视眼球震颤提前出现、瞳孔收缩，以及Romberg测试计时失败。',
                                        'knowledge_boundary': '本人观察；不新增瞳孔尺寸、震颤角度、测试时长或病因。',
                                        'source_file': 'authoring/sources/source22/opinions/2798364/paragraphs.json',
                                        'source_anchors': [{'locator': 'CL-O2798364-P0086',
                                                            'quote': 'indicates any alternative '
                                                                     'explanation for the objective '
                                                                     'symptoms observed by Deputy Grotke: '
                                                                     'Pulse rate of 112 beats per minute; '
                                                                     'horizontal gaze nystagmus early '
                                                                     'onset; constricted pupils'},
                                                           {'locator': 'CL-O2798364-P0087',
                                                            'quote': 'and time-based failure of the '
                                                                     'Romberg test. See People v. Gregg '
                                                                     '(1968)'}]}},
                  'R_OCCUPANT': {'F02': {'expected_original_content': '原文把A描述为B关系疏远的男友；没有说明双方是否仍同住或A是否具有独立居住权。',
                                         'spoken_content': 'A是与我关系疏远的男友。',
                                         'knowledge_boundary': '本人关系；不推定同住、住宅权属或A没有居住权。',
                                         'source_file': 'authoring/sources/source22/opinions/2798364/paragraphs.json',
                                         'source_anchors': [{'locator': 'CL-O2798364-P0031',
                                                             'quote': 'Moore was arrested at the home of '
                                                                      'his estranged girlfriend after law '
                                                                      'enforcement'}]},
                                 'F03': {'expected_original_content': 'D先询问B。B向D报告，为避开与A的争执，她把自己锁在卧室里。',
                                         'spoken_content': '我向D报告，我为避开与A的争执，把自己锁在卧室里。',
                                         'knowledge_boundary': '重复本人报告的亲历内容；模拟陈述不是法庭对其真实性的认定。',
                                         'source_file': 'authoring/sources/source22/opinions/2798364/paragraphs.json',
                                         'source_anchors': [{'locator': 'CL-O2798364-P0034',
                                                             'quote': 'victim. Deputy Grotke first '
                                                                      'interviewed the victim, who '
                                                                      'reported that she had locked '
                                                                      'herself'},
                                                            {'locator': 'CL-O2798364-P0035',
                                                             'quote': 'in her bedroom to escape an '
                                                                      'argument with Moore. The victim '
                                                                      'also reported that she had'}]},
                                 'F04': {'expected_original_content': 'B向D报告，她要求A离开她的住宅，A拒绝离开。该段是B的报告，未提供当时对话逐字稿。',
                                         'spoken_content': '我向D报告，我要求A离开我的住宅，而A拒绝离开。',
                                         'knowledge_boundary': '重复本人报告的亲历内容；不新增对话原话、时间或居住权。',
                                         'source_file': 'authoring/sources/source22/opinions/2798364/paragraphs.json',
                                         'source_anchors': [{'locator': 'CL-O2798364-P0035',
                                                             'quote': 'in her bedroom to escape an '
                                                                      'argument with Moore. The victim '
                                                                      'also reported that she had'},
                                                            {'locator': 'CL-O2798364-P0036',
                                                             'quote': 'asked Moore to leave her home, '
                                                                      'which he refused.'}]}},
                  'R_DAUGHTER': {'F01': {'expected_original_content': '2013年4月19日，B的女儿C拨打911后，内华达县治安官办公室警员D到达B的住宅；C允许D进入。',
                                         'spoken_content': '我拨打了911，并允许到场的警员D进入B的住宅。',
                                         'knowledge_boundary': '本人报警和允许进入的行为；不新增报警原话或对争执的见闻。',
                                         'source_file': 'authoring/sources/source22/opinions/2798364/paragraphs.json',
                                         'source_anchors': [{'locator': 'CL-O2798364-P0032',
                                                             'quote': 'responded to a 911 call from the '
                                                                      'victim’s daughter. Nevada County '
                                                                      'Sheriff’s Deputy Grotke'},
                                                            {'locator': 'CL-O2798364-P0033',
                                                             'quote': 'responded on April 19, 2013 and was '
                                                                      'permitted to enter the home by the '
                                                                      'daughter of the'}]}},
                  'R_DEFENDANT': {'F02': {'expected_original_content': '原文把A描述为B关系疏远的男友；没有说明双方是否仍同住或A是否具有独立居住权。',
                                          'spoken_content': '我是B关系疏远的男友。',
                                          'knowledge_boundary': '本人关系；不推定同住、产权或独立居住权。',
                                          'source_file': 'authoring/sources/source22/opinions/2798364/paragraphs.json',
                                          'source_anchors': [{'locator': 'CL-O2798364-P0031',
                                                              'quote': 'Moore was arrested at the home of '
                                                                       'his estranged girlfriend after law '
                                                                       'enforcement'}]},
                                  'F05': {'expected_original_content': 'D随后在住宅后部露台找到A。',
                                          'spoken_content': 'D在住宅后部露台找到我。',
                                          'knowledge_boundary': '本人在场；不新增谈话、拘束或离开机会。',
                                          'source_file': 'authoring/sources/source22/opinions/2798364/paragraphs.json',
                                          'source_anchors': [{'locator': 'CL-O2798364-P0037',
                                                              'quote': 'Deputy Grotke then found Moore on '
                                                                       'the back deck of the home. He '
                                                                       'admitted that he'}]},
                                  'F06': {'expected_original_content': '判决记载A承认有人要求他离开；没有给出原话，也没有记载A另行承认使用具体药物。',
                                          'spoken_content': '我向D承认，有人要求我离开。',
                                          'knowledge_boundary': '本人陈述；不扩大为承认具体药物使用或法律责任。',
                                          'source_file': 'authoring/sources/source22/opinions/2798364/paragraphs.json',
                                          'source_anchors': [{'locator': 'CL-O2798364-P0037',
                                                              'quote': 'Deputy Grotke then found Moore on '
                                                                       'the back deck of the home. He '
                                                                       'admitted that he'},
                                                             {'locator': 'CL-O2798364-P0038',
                                                              'quote': 'had been asked to leave and '
                                                                       'displayed objective symptoms of '
                                                                       'being under the influence, such'}]},
                                  'F09': {'expected_original_content': 'D随后以受管制物质影响和非法滞留两项涉嫌违法行为逮捕A。',
                                          'spoken_content': 'D随后以涉嫌受管制物质影响和非法滞留逮捕我。',
                                          'knowledge_boundary': '本人经历；逮捕不等于承认犯罪。',
                                          'source_file': 'authoring/sources/source22/opinions/2798364/paragraphs.json',
                                          'source_anchors': [{'locator': 'CL-O2798364-P0041',
                                                              'quote': 'stimulant (“CNS”). Grotke then '
                                                                       'arrested Moore for being under the '
                                                                       'influence in violation of'},
                                                             {'locator': 'CL-O2798364-P0042',
                                                              'quote': 'Section 11550(a), and for '
                                                                       'trespassing in violation of Penal '
                                                                       'Code section 602(o).'}]}}},
 'CA-PILOT-004': {'R_COMPANION': {'F01': {'expected_original_content': '原判决概要记载，B与同行女性C坐在B的汽车内。原文称C为B的约会对象，没有提供她的姓名。',
                                          'spoken_content': '我是B的约会对象，当时与B坐在他的汽车内。',
                                          'knowledge_boundary': '本人在场关系；不扩展为对全部现场细节的完整目击。',
                                          'source_file': 'authoring/sources/source14/opinions/2253521/paragraphs.json',
                                          'source_anchors': [{'locator': 'CL-O2253521-P0014',
                                                              'quote': 'George Makary and his date were '
                                                                       'sitting in Makary’s car when three '
                                                                       'men approached them. One of the '
                                                                       'men brandished a knife and '
                                                                       'demanded Makary give up his money '
                                                                       'and his car. When Makary refused, '
                                                                       'he stabbed him.'}]},
                                  'F06': {'expected_original_content': '判决记载C不能把A辨认为刺伤者；她还认为A甚至不像刺伤者。不能把“不能辨认”改写成她已确定另一个人就是刺伤者。',
                                          'spoken_content': '我不能把A辨认为刺伤者，而且我认为A甚至不像刺伤者。',
                                          'knowledge_boundary': '本人辨认判断；不等于确认A绝不在场，也不等于认定邻居是刺伤者。',
                                          'source_file': 'authoring/sources/source14/opinions/2253521/paragraphs.json',
                                          'source_anchors': [{'locator': 'CL-O2253521-P0016',
                                                              'quote': 'Makary identified defendant as his '
                                                                       'assailant, but his date could not. '
                                                                       'In fact, she did not think '
                                                                       'defendant even resembled the '
                                                                       'stabber.'}]}}}}

COMMON_CONTRACT = """本题是来源约束的模拟，事实材料是用于本题的结构化案情摘要，不冒充警察原报告、录像原件或逐字笔录。
模拟双方仅认可：授权事实段落准确呈现所标明的来源陈述，以及这些摘要可作为本题对应证据的呈现载体；无需取得本题没有提供的原始文件来重新认证摘要载体。
这项模拟约定只解决载体可用性与来源归属，不承认陈述必然真实、不承认被告身份或主观意图、不自动完成任何犯罪要件，也不自动准入全部材料。
法官仍须根据具体用途审查相关性、传闻层次、个人知情范围、意见基础和实际提出的合法异议。已排除材料仍不能进入陪审团事实记录。
对来源明确归属于某人的亲历、报告或意见，可以由该模拟人物在同样知情边界内陈述；不得因为其制作来源是判决摘要，就把所有模拟人物都当成在转述判决。
未知细节必须保持未知；“本题未附完整笔录/录像/资历文件”本身不是已经证明的程序违法、伪造、从未存在或全部来源事实不可用。涉及未明的实质基础须提出具体问题，不得从归档缺件直接推定。
本题不要求复现历史裁决；任何裁决只依本轮获准证据及已交付法律指示作出。"""

CASE_CONTRACTS = {
    "CA-PILOT-003": """003的M01—M05是来源所述事件、他人报告和警员观察/意见的模拟呈现载体；其中他人报告仍须保持报告人及传闻层次，警员意见仍非毒理检测结果。
M05披露权限和阶段不变；本约定不披露其内容。M06—M07是材料范围说明，不是证明未知事项实际不存在的证据。""",
    "CA-PILOT-004": """004保留B为记录角色，不新增B的实时作证。来源明确记载双方约定通过条件讯问取得B的证言，录像在庭审中播放。
本次模拟另行约定：在允许的记录呈现阶段，以获授权的M01—M03事实/辨认摘要和M06程序摘要代替未随题提供的完整录像与笔录；双方不以缺少该原件本身争议摘要载体的真实性或可取得性。此为模拟设计约定，不能声称历史双方同意了当前中文摘要或其全部法律用途。
约定不证明B的辨认正确，不补写完整交叉询问、宣誓、观察条件或协议范围；允许针对具体内容、可靠性与证明用途提出争议。
C只能陈述其已记载的在场关系和辨认判断；M05仅表示辩方曾提出替代行为人证据，不确认邻居实施犯罪，也不补造那份证据。M07是来源范围说明，不是新证言。""",
}

ACTION_INTERFACE_CONTRACT = """
动作接口补充（只澄清字段，不替你选择论证）：
object 的 target 填本次异议针对的 E 开头事件编号；references 只能填 legal_catalog 中的法律编号，不能放 M 材料编号、F事实编号或 E 事件编号。
对象事件及材料可在 text/details 中说明；details 用 [{"key":"字段名","value":"值"}] 表示，并满足 objection_catalog.required_fields。
file_motion 的 target 填被申请处理的材料段落或公开证言，references 只填法律编号。程序不会替换你填错的编号。
"""
LAWYER_PROMPT = CORE_LAWYER_PROMPT + ACTION_INTERFACE_CONTRACT


class SourceBoundCaseData(CaseData):
    """A view-only adapter, with explicit mappings locked to expected old facts."""
    def __init__(self, path):
        super().__init__(path)
        if self.case_id not in WITNESS_MAPS:
            raise ValueError("source_environment_unsupported_case")
        self._verify_expected_facts()

    def _verify_expected_facts(self):
        for rid, mappings in WITNESS_MAPS[self.case_id].items():
            if rid not in self.roles:
                raise ValueError("source_environment_missing_role:" + rid)
            points = {p["fact_id"]: p for p in self.roles[rid].get("knowledge_points", [])}
            if set(points) != set(mappings):
                raise ValueError("source_environment_fact_set_changed:" + rid)
            for fid, row in mappings.items():
                if points[fid]["content"] != row["expected_original_content"]:
                    raise ValueError("source_environment_fact_content_changed:" + rid + ":" + fid)

    def simulation_contract(self):
        return {"version": ENVIRONMENT_VERSION,
                "kind": "explicit_simulation_assumption_not_historical_stipulation",
                "common": COMMON_CONTRACT,
                "case_specific": CASE_CONTRACTS[self.case_id],
                "does_not_determine_verdict": True,
                "does_not_make_all_evidence_admissible": True}

    def public_brief(self):
        brief = super().public_brief()
        brief["simulation_contract"] = self.simulation_contract()
        return brief

    def role_card(self, role_id):
        card = super().role_card(role_id)
        mappings = WITNESS_MAPS[self.case_id].get(role_id, {})
        for point in card.get("knowledge_points", []):
            mapping = mappings.get(point["fact_id"])
            if mapping:
                point["content"] = mapping["spoken_content"]
                point["knowledge_basis"] = mapping["knowledge_boundary"]
        # Do not show original source locators, complete authoring files, verdicts
        # or other witnesses' knowledge merely because this adapter has mappings.
        return card

    def author_environment_manifest(self):
        return {"environment_version": ENVIRONMENT_VERSION,
                "case_id": self.case_id,
                "simulation_contract": self.simulation_contract(),
                "witness_mappings": deepcopy(WITNESS_MAPS[self.case_id]),
                "meaning": "authored paraphrases, not historical verbatim witness quotations",
                "case_files_modified": False,
                "historical_result_loaded_by_runtime": False}


def verify_source_mappings(full_source_case_path):
    """Offline authoring check. Never put this output into actor contexts.

    Quotes prove traceability only; the authored Chinese speech still requires
    semantic review. This function never edits source documents or actor cards.
    """
    path = Path(full_source_case_path).resolve()
    cid = json.loads((path / "manifest.json").read_text(encoding="utf-8"))["case_id"]
    if cid not in WITNESS_MAPS:
        raise ValueError("source_environment_unsupported_case")
    checked, hashes, cache = 0, {}, {}
    for mappings in WITNESS_MAPS[cid].values():
        for row in mappings.values():
            relative = row["source_file"]
            source = (path / relative).resolve()
            if not source.is_relative_to(path) or source != path / relative:
                raise ValueError("source_environment_source_path_escape")
            if relative not in cache:
                raw = source.read_bytes()
                hashes[relative] = hashlib.sha256(raw).hexdigest()
                cache[relative] = {p["locator"]: p["text_verbatim"] for p in json.loads(raw)}
            for anchor in row["source_anchors"]:
                paragraph = cache[relative].get(anchor["locator"])
                if paragraph is None or anchor["quote"] not in paragraph:
                    raise ValueError("source_environment_anchor_mismatch:" + anchor["locator"])
                checked += 1
    return {"case_id": cid, "checked_anchors": checked, "source_hashes": hashes,
            "verbatim_anchor_check": "passed", "semantic_authoring_validation": "AI_authored_not_expert_validated"}


def verify_bundled_source_mappings(package_path):
    """Verify all authored speech anchors against the source-only bundled input.

    ``package_path`` is the source_rubric_v2 directory containing ``sources/``.
    This read-only authoring check needs no original archive, model, or key.
    Declared source-file hashes are retained as provenance; this check verifies
    actual bundled bytes/quotes, not the availability of those original files.
    Its output belongs in runner audit records, never actor contexts.
    """
    root = Path(package_path).resolve()
    cases = []
    for cid, roles in WITNESS_MAPS.items():
        relative = Path("sources") / cid / "author_input.json"
        source = (root / relative).resolve()
        if not source.is_relative_to(root) or source != root / relative:
            raise ValueError("source_environment_bundle_path_escape")
        raw = source.read_bytes()
        document = json.loads(raw)
        if document.get("case_id") != cid:
            raise ValueError("source_environment_bundle_case_mismatch:" + cid)
        units = document.get("source_units")
        if not isinstance(units, list):
            raise ValueError("source_environment_bundle_units_missing:" + cid)
        indexed = {}
        for unit in units:
            source_id = unit.get("source_id")
            if not isinstance(source_id, str) or not source_id or source_id in indexed:
                raise ValueError("source_environment_bundle_duplicate_or_missing_id:" + cid)
            indexed[source_id] = unit
        checked = 0
        for mappings in roles.values():
            for mapping in mappings.values():
                for anchor in mapping["source_anchors"]:
                    source_id = anchor["locator"]
                    unit = indexed.get(source_id)
                    if (unit is None or unit.get("kind") != "original_opinion"
                            or unit.get("file") != mapping["source_file"]
                            or not isinstance(unit.get("text"), str)
                            or anchor["quote"] not in unit["text"]):
                        raise ValueError("source_environment_bundle_anchor_mismatch:" + source_id)
                    checked += 1
        cases.append({"case_id": cid, "checked_anchors": checked,
                      "author_input_relative_path": relative.as_posix(),
                      "author_input_sha256": hashlib.sha256(raw).hexdigest(),
                      "declared_source_files_sha256": document.get("source_files_sha256", {}),
                      "verbatim_anchor_check": "passed"})
    return {"environment_version": ENVIRONMENT_VERSION,
            "status": "passed", "cases": cases,
            "semantic_authoring_validation": "AI_authored_not_expert_validated"}


class _EnvironmentBackend:
    """Apply public protocol consistently, including final foundation closure."""
    def __init__(self, inner, case):
        self.inner, self.case = inner, case

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def decide(self, role_id, system, context, schema):
        if role_id in {*LAWYERS, "R_JUDGE"}:
            context = deepcopy(context)
            context["simulation_contract"] = self.case.simulation_contract()
            if role_id in LAWYERS:
                system += ACTION_INTERFACE_CONTRACT
                context["action_field_contract"] = {
                    "object": {"target": "current E event ID", "references": "legal IDs only"},
                    "file_motion": {"target": "authorized material paragraph or testimony", "references": "legal IDs only"}}
            else:
                system += "\n以下为本次模拟的公开程序约定。一般的摘要认证提示须结合该约定解释；只在约定明确的载体范围内免于重复认证，不免除内容、用途及实质法律审查。\n" + COMMON_CONTRACT + "\n" + CASE_CONTRACTS[self.case.case_id]
        return self.inner.decide(role_id, system, context, schema)


class SourceBoundCourtEngine(CourtEngine):
    def __init__(self, case, backend, config):
        if not isinstance(case, SourceBoundCaseData):
            raise TypeError("source_environment_requires_SourceBoundCaseData")
        super().__init__(case, _EnvironmentBackend(backend, case), config)
        self.protocol_version = ENVIRONMENT_VERSION

    def judge(self, task, packet, decisions):
        ctx = self.context("R_JUDGE", task)
        ctx.update(packet=packet, legal_sources=self.legal_texts(),
                   motion_rules=self.case.procedure.get("motion_rules", {}))
        prompt = BASE_PROMPT + """你是法官，依授权材料、法律和公开simulation_contract作出本题裁定。
严格区分资料披露、内容可信性、证据准入与用途限制；第一轮记录不能自动成为第二轮证据。
摘要只是呈现载体，不能因它是摘要就当然采纳；也不能忽略本题明确的载体约定，仅凭未提供原件就全盘排除。
证人知情基础须按该人物实际选中陈述的归属和范围检查：听他人报告仍为他人报告，个人意见不是实验室结果。
只处理packet指定对象；admitted/limited_use须给出实际允许用途；有具体未解决基础可pending_foundation。
不得查看或推测历史裁决、隐藏答案或rubric；不得为了维持控方或辩方胜诉而倒推证据裁定。
reason为公开理由，legal_source_ids只用所给法源。实体法律判断不是评分标准答案。
"""
        result = self.backend.decide("R_JUDGE", prompt, ctx, decision_schema(decisions))
        if result.get("decision") not in decisions or not set(result.get("legal_source_ids", [])) <= self.legal_ids():
            raise RunStopped("invalid_judge_decision_or_citation")
        if not result.get("reason") or not result.get("legal_source_ids"):
            raise RunStopped("judge_ruling_requires_reasons_and_law")
        return result

    def witness_answer(self, witness, question, private=False):
        # Calling code enforces stage and voluntary defendant testimony. Keep a
        # second guard so direct callers cannot invent a live recorded witness.
        if self.case.roles[witness].get("role_type") == "recorded_witness":
            raise ValueError("recorded_witness_cannot_give_live_testimony")
        if private and witness != "R_DEFENDANT":
            raise ValueError("private_witness_not_authorized")
        event = super().witness_answer(witness, question, private=private)
        event["meta"]["rendering"] = "exact_authored_source_bound_speech_v2"
        event["meta"]["environment_version"] = ENVIRONMENT_VERSION
        mappings = WITNESS_MAPS[self.case.case_id].get(witness, {})
        event["meta"]["knowledge_boundaries"] = [
            {"fact_id": fid, "boundary": mappings[fid]["knowledge_boundary"]}
            for fid in event["references"] if fid in mappings]
        return event

    def summary(self):
        result = super().summary()
        result["environment_version"] = ENVIRONMENT_VERSION
        result["simulation_stipulation_active"] = True
        result["rubric_visible_to_court_roles"] = False
        result["limitations"] += ["source_excerpt_simulation_not_full_historical_record",
                                   "simulation_carrier_stipulation_changes_task_assumptions"]
        return result

    def save(self, out):
        super().save(out)
        manifest = self.case.author_environment_manifest()
        manifest["adapter_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        (Path(out) / "environment_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
