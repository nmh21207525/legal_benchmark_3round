#!/usr/bin/env python3
"""Reproducible, offline revision of legal evidence and charge-specific instructions.
Only legal_corpus/, world/manifest.json and world/jury_instructions.json are changed.
Existing raw snapshots are immutable; new web retrieval notes are explicitly not HTML.
"""
from __future__ import annotations
import hashlib
import html
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
REVIEWED = '2026-10-21'

def dump(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def sha(data: bytes):
    return hashlib.sha256(data).hexdigest()

def norm(s):
    return re.sub(r'\s+', ' ', s).strip()

def html_text(p: Path):
    # Historical chaptered pages use Windows-1252; current section pages use UTF-8.
    b = p.read_bytes()
    try:
        s = b.decode('utf-8')
    except UnicodeDecodeError:
        s = b.decode('windows-1252')
    return html.unescape(re.sub(r'<[^>]+>', ' ', s))

SOURCES = [
    dict(legal_id='L_ELLIS_1999', title='People v. Ellis (1999) 69 Cal.App.4th 1334 — historical CALJIC 12.60 elements',
         url='https://law.justia.com/cases/california/court-of-appeal/4th/69/1334.html', effective='1999-02-16',
         locator='69 Cal.App.4th 1336–1339; published discussion II and footnotes 4–5',
         quote='The jury was instructed pursuant to CALJIC No. 12.60',
         text='研究用摘要（非判决逐字全文）：Ellis 原判记载审判使用 CALJIC 12.60，列明受酒精影响下驾驶、同时实施违法行为以及该行为导致他人身体受伤。其第(b)项罪名用驾驶时血醇至少0.08%代替受影响要件。受影响要求驾驶能力因饮酒受损，达不到相同情况下通常谨慎的清醒者水平。原判要求就作为第二要件的具体违法行为给予适当法律定义；本案实例是超速，该实例不是006的既定事实。',
         tags=['case', 'CALJIC', '12.60', '16.831', '23153', 'elements', 'under_influence']),
    dict(legal_id='L_WEEMS_1997', title='People v. Weems (1997) 54 Cal.App.4th 854 — §23153 concurrent act and causation',
         url='https://law.justia.com/cases/california/court-of-appeal/4th/54/854.html', effective='1997-04-29',
         locator='54 Cal.App.4th 858–859, 862; footnotes 3–4',
         quote='ordinary negligence',
         text='研究用摘要（非判决逐字全文）：Weems 在2000年以前解释并引述 §23153(a)、(b)。两项罪均须证明驾驶时另有违法行为或未履行驾驶中的法定义务，并且该行为或不履行近因造成驾驶员以外的人受伤；单有酒驾不够。第(a)项是受影响驾驶，第(b)项是驾驶时血醇至少0.08%。注意义务要件可由普通过失成立，不必另证某一具体车法条款被违反。因果关系指向身体伤害，不限于引发碰撞本身。',
         tags=['case', '23153', 'negligence', 'causation', 'elements']),
    dict(legal_id='L_YORDAMLIS_1992', title='Yordamlis v. Zolin (1992) 11 Cal.App.4th 655 — three-hour premise and historical CALJIC 12.61.1',
         url='https://law.justia.com/cases/california/court-of-appeal/4th/11/655.html', effective='1992-12-09',
         locator='11 Cal.App.4th 660–663; footnotes 5 and 9',
         quote='CALJIC No. 12.61.1',
         text='研究用摘要（非判决逐字全文）：这是驾驶证行政暂停案件，不是006的刑事裁判。原判解释 §23152(b)、§23153(b) 三小时推论的时间前提，且脚注5明确援引 CALJIC 12.61.1（1990修订）。不能只以公务行为通常合法为由假定抽样发生在驾驶后三小时内。后测血醇并非自动等于驾驶时血醇。脚注9也未把缺少抽样时刻等同于检验结果当然没有任何证明力；仍须评价具体证据。',
         tags=['case', '23153', 'BAC', 'three_hours', 'CALJIC', '12.61.1'],
         scope='Administrative-license case; used only for the expressly discussed statutory timing premise and historical instruction reference, not a substitute criminal burden of proof.'),
    dict(legal_id='L_ENRIQUEZ_1996', title='People v. Enriquez (1996) 42 Cal.App.4th 661 — historical definition of under the influence',
         url='https://law.justia.com/cases/california/court-of-appeal/4th/42/661.html', effective='1996-02-07',
         locator='42 Cal.App.4th 665–666; CALJIC 12.65 and 16.831 (1992 rev.), discussion of 12.60',
         quote='CALJIC No. 16.831 (1992 rev.)',
         text='研究用摘要（非判决逐字全文）：Enriquez 要求在酒驾／药物影响驾驶案件中定义“受影响”，并讨论 CALJIC 12.60、12.65 与 16.831 的搭配。所需的是实际驾驶能力受损，无法保持相同情况下通常谨慎清醒者的驾驶标准；身体存在酒精或药物、或者药物一般可能影响驾驶，本身不能替代该要件。原案涉及药物，006的酒精情形另由 Ellis 的酒精指示支撑。',
         tags=['case', 'CALJIC', '12.60', '12.65', '16.831', 'under_influence']),
]



BARRETT = dict(
    legal_id='L_BARRETT_2000',
    title='People v. Superior Court (Barrett) (2000) 80 Cal.App.4th 1305 — third-party subpoenas, PC1326/1327',
    url='https://law.justia.com/cases/california/court-of-appeal/4th/80/1305.html',
    effective='2000-05-25', locator='80 Cal.App.4th 1315–1321; footnote7',
    quote='specific facts justifying discovery',
    text='研究用摘要（非判决逐字全文）：Barrett 原判区分控方／侦查团队掌握材料的§1054披露与第三方记录的§1326、§1327传票。签发传票不自动赋予查看记录的权利；第三方先向法院提交，法院判断申请人是否有权获得。申请人应提出支持相关性和需要的具体事实，第三方可主张特权或申请撤销，漫无边际的搜寻可被拒绝。法院可内庭审查特权及辩护理论。此摘要没有把现代修订版§1326全文冒充当时法条。',
    tags=['case','subpoena','third_party','1326','1327','good_cause','relevance','privilege'])


def add_sources(corpus_dir: Path, entries: list, sources=SOURCES):
    by_id = {e['legal_id']: e for e in entries}
    for src in sources:
        raw = dict(source_url=src['url'], source_title=src['title'],
                   source_kind='California Court of Appeal primary opinion, hosted by Justia',
                   source_publication_date=src['effective'], retrieved_at=REVIEWED,
                   retrieval_method='web-search-service open; primary opinion body inspected',
                   snapshot_kind='retrieval_note_with_short_excerpt_and_authored_summary_NOT_full_HTML',
                   locator=src['locator'], short_verbatim_excerpt=src['quote'],
                   authored_summary=src['text'],
                   scope=src.get('scope', 'Published opinion proposition only; no case facts are transferred into the synthetic trial.'))
        rel = f"raw/{src['legal_id']}_retrieval_note.json"
        dump(corpus_dir / rel, raw)
        entry = dict(legal_id=src['legal_id'], canonical_id=src['legal_id'], title=src['title'], text=src['text'],
                     url=src['url'], retrieved_at=REVIEWED, snapshot_path=rel,
                     snapshot_kind=raw['snapshot_kind'], sha256=sha((corpus_dir / rel).read_bytes()),
                     text_sha256=sha(src['text'].encode('utf-8')), effective=src['effective'],
                     effective_basis='date of contemporaneous published primary opinion',
                     source_publication_date=src['effective'], cutoff_ok=True, distractor=False,
                     verification='Primary opinion body inspected on 2026-10-21; selected proposition summarized, not a full CALJIC edition or full opinion reproduction.',
                     verification_status='verified_selected_historical_proposition',
                     origin='web_verified_primary_opinion_summary', tags=src['tags'], locator=src['locator'])
        by_id[src['legal_id']] = entry
    return list(by_id.values())


def instruction(i, charges, legal_ids, text, verification):
    return dict(id=i, charge_ids=charges, legal_ids=legal_ids, text=text,
                source_type='authored_Chinese_instruction_based_on_historical_primary_sources',
                verification=verification)


def revise(case: str):
    base = ROOT / case
    cd = base / 'legal_corpus'
    entries = [json.loads(line) for line in (cd / 'corpus.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    entries = add_sources(cd, entries, [BARRETT])
    manifest = json.loads((base / 'world/manifest.json').read_text(encoding='utf-8'))
    checks = [dict(rule='L_BARRETT_2000',status='pass',scope='contemporaneous primary opinion on third-party discovery; not full PC1326 edition',publication_date='2000-05-25')]
    by_id = {e['legal_id']: e for e in entries}
    for e in entries:
        # A supplied raw snapshot remains unchanged even when its old metadata is corrected.
        e.setdefault('canonical_id', e['legal_id'])
        e.setdefault('effective_basis', 'legacy availability date; not independently asserted as the original enactment date')
        if e['legal_id'].startswith('L_EVID_') and e['snapshot_path'].endswith('.html'):
            rawtext = norm(html_text(cd / e['snapshot_path']))
            if rawtext.endswith('(Enacted by Stats. 1965, Ch. 299.)'):
                e['effective'] = '1967-01-01'
                e['effective_basis'] = 'Evidence Code commencement; official section snapshot attributes unchanged section to Stats.1965,ch.299'
                e['verification'] = 'Official section snapshot carries the original 1965 enactment history; selected supplied text retained. This is not certification of every surrounding historical provision.'
            for date, footer in [('2005-01-01', 'Effective January 1, 2005.'), ('1997-01-01', 'Effective January 1, 1997.')]:
                if footer in rawtext:
                    e['effective'] = date
                    e['effective_basis'] = 'explicit effective date in official section-history footer'
        if e['legal_id'] == 'L_PEN_1096':
            assert 'Effective July 3, 1995.' in html_text(cd / e['snapshot_path'])
            e.update(effective='1995-07-03', effective_basis='explicit effective date in official section-history footer',
                     verification='Supplied official section history: Stats.1995,ch.46,sec.1,effective1995-07-03. Text and raw snapshot hashes preserved.')
        if e['legal_id'] in ['L_EVID_1101', 'L-EVID1101']:
            assert 'Effective January 1, 1997.' in html_text(cd / 'raw/EVID_1101.html')
            e.update(canonical_id='L_EVID_1101', effective='1997-01-01', cutoff_ok=True, distractor=False,
                     effective_basis='official EVID_1101.html history: Stats.1996,ch.261,sec.1',
                     verification='Corrected shared rule identity: the (a)-(b) core is the same 1997-effective rule in both IDs. A later retrieval or mirror date is not a future-law date. Original snapshots are retained unchanged.',
                     verification_status='official_history_and_duplicate_core_checked',
                     corroborating_snapshot='raw/EVID_1101.html')
        if e['legal_id'] in ['L-EVID720','L-EVID801','L-EVID352']:
            canonical = {'L-EVID720':'L_EVID_720','L-EVID801':'L_EVID_801','L-EVID352':'L_EVID_352'}[e['legal_id']]
            e['canonical_id'] = canonical
            e['verification'] = 'Supplied excerpt checked against the same section in the included official snapshot; alias preserves its own raw source. Original snapshot notes are provenance, not independent legal conclusions.'
            e['effective_basis'] = 'same historical section as the included official canonical source'
        if e['legal_id'] == 'L_SCHMERBER_1966':
            e.update(effective='1966-06-20', effective_basis='Supreme Court opinion date; selected majority excerpt only')
        e['cutoff_ok'] = e['effective'] <= manifest['law_cutoff']
        e['distractor'] = not e['cutoff_ok']
        e['cutoff_basis'] = 'legal rule availability compared with simulation trial cutoff; retrieval/publication of a later archival witness is separate'
    # Whitespace/typographic normalization is used only for comparison, never to rewrite source text.
    punctuation = str.maketrans({'’':"'", '‘':"'", '“':'"', '”':'"'})
    equivalent = lambda s: norm(s.translate(punctuation))
    a = equivalent(by_id['L_EVID_1101']['text'])
    b = equivalent(by_id['L-EVID1101']['text'])
    assert b in a, 'Section1101 alias core mismatch'
    for alias, canonical in [('L-EVID720','L_EVID_720'),('L-EVID801','L_EVID_801'),('L-EVID352','L_EVID_352')]:
        assert equivalent(by_id[alias]['text']) in equivalent(by_id[canonical]['text']), f'{alias}: duplicate core mismatch'
    checks.append(dict(rule='Evidence1101', status='pass', canonical_id='L_EVID_1101', alias_id='L-EVID1101',
                       effective='1997-01-01', check='Full alias (a)-(b) core occurs in the included official full section; official effective-date footer inspected.'))

    common = instruction('J_BURDEN', [c['charge_id'] for c in manifest['charges']], ['L_PEN_1096'],
        '被告推定无罪。控方须对每项指控的每一要件排除合理怀疑；被告无需证明无罪。对某项必要要件仍有合理怀疑，应就该项指控作无罪认定。只评价本庭依法准许且在用途范围内的证据。',
        'PEN1096 official snapshot; Chinese procedural adaptation, not a verbatim historical model instruction.')
    instructions = [common]
    limitations = ['以下中文指示是本实验的法官指示改写；没有冒称为官方中文版或完整历史指示册。',
                   '法源的抓取日期、后来的存档载体日期和被证明的历史规则有效日期分开记录。',
                   '历史法源中的原案人名、地点和具体事实不自动成为本模拟案件事实。']
    if case.startswith('005'):
        e = by_id['L_PEN_290_2006']
        raw = html_text(cd / e['snapshot_path'])
        section = raw[raw.index('SEC. 3.5.'):raw.index('SEC. 4.', raw.index('SEC. 3.5.'))]
        assert all(norm(p) in norm(section) for p in e['text'].splitlines())
        pp = base / 'source_reference/original/authoring/sources/source15/opinions/2256789/paragraphs.json'
        paragraphs = {p['source_paragraph_id']:p for p in json.loads(pp.read_text(encoding='utf-8'))}
        assert 'operative Jan. 1, 2006' in paragraphs['P0083']['text_verbatim']
        e.update(effective='2006-01-01', enacted_effective='2005-10-07',
                 effective_basis='operative date explicitly identified in original opinion P0083; selected text checked against chapter722 SEC3.5',
                 verification='All four supplied selected paragraphs match official Stats.2005,ch.722,SEC3.5 after whitespace normalization. Original opinion P0083 identifies this operative version for the offense. No assertion that the whole former section or every chaptering contingency was independently reconstructed.',
                 verification_status='selected_historical_text_exactly_matched', source_publication_date='2005-10-07',
                 source_locator='Stats.2005,ch.722,SEC3.5; original opinion CL-O2256789-P0083')
        checks.append(dict(rule=e['legal_id'], status='pass', exact_paragraphs=4, source_section='SEC.3.5', historical_locator='CL-O2256789-P0083', scope='selected excerpts only'))
        e = by_id['L_CALCRIM_1170_2006_EXCERPT']
        quoted = '\n'.join(paragraphs[p]['text_verbatim'] for p in ['P0043','P0044','P0045'])
        assert norm(quoted) == norm(e['text'])
        e.update(effective='2006-01-01', effective_basis='historical 2006 instruction version, quoted expressly as such in the original opinion; CALCRIM commenced2006-01-01',
                 source_version='CALCRIM1170(2006), quoted historical trial instruction',
                 source_publication_year=2009,
                 verification='Exact match to original opinion P0043–P0045, which expressly identifies CALCRIM1170(2006). Used as documentary evidence of the historical instruction, not as a 2009 precedent. Full 2006 CALCRIM volume not supplied or independently reconstructed.',
                 verification_status='historical_quoted_excerpt_exactly_matched',
                 source_locator='CL-O2256789-P0043–P0045',
                 corroborating_url='https://courts.ca.gov/criminal-jury-instructions-resource-center')
        checks.append(dict(rule=e['legal_id'],status='pass',exact_source_paragraphs=['P0043','P0044','P0045'],scope='quoted trial instruction only, not later holding'))
        instructions += [
            instruction('J_005_ELEMENTS',['C01'],['L_PEN_290_2006','L_CALCRIM_1170_2006_EXCERPT'],
                'C01：控方须证明：（1）被告因符合旧版§290的既往定罪而负有登记义务；（2）被告在指控辖区居住，触发来到该辖区或在其中变更住所后的五个工作日登记义务；（3）被告实际知道其依法应在居住地登记；（4）被告故意未在该期限内向相应警察局长或县警长登记。故意指自愿或有目的地不履行。实际知情、居住与起算时间均须据本案准许证据判断；不得以仅有行政登记记录代替全部要件。',
                '2006 CALCRIM1170 quoted elements and SEC3.5 selected statute; renamed fictional party and generalized jurisdiction, no original-case finding imported.'),
            instruction('J_005_RESIDENCE',['C01'],['L_PEN_290_2006'],
                '旧版§290将住所定义为经常居住的一个或多个地址，不以在那里度过多少天夜为限；旅馆、收容所及能够以街道地址定位的车辆等可属于住所。无住所者属于 transient。须依准许证据判定实际居住及适用义务，不能仅凭当事人自称或某标签决定。',
                'Selected old§290(a)(1)(C)(vii), exactly matched to official SEC3.5.'),
            instruction('J_005_PRIOR_USE',['C01'],['L_EVID_1101','L_EVID_355','L_CALCRIM_1170_2006_EXCERPT'],
                '被告既往定罪在本题可用于依法确定登记义务基础；其余用途服从法庭具体限用裁定。不得仅因曾犯罪便推断本次具有犯罪倾向或已违反登记义务。',
                'Evidence1101(a)-(b),355 and the historical instruction duty-basis element; authored limiting instruction.')]
        limitations.append('005没有把2009原型判决的实体裁判结论当作2007可用判例；只使用其中明确标注的2006旧条文版本和当时实际指示的引文。')
    else:
        entries = add_sources(cd, entries)
        by_id = {e['legal_id']:e for e in entries}
        e = by_id['L_VEH_23612_2000']
        raw = html_text(cd / e['snapshot_path'])
        assert all(norm(re.sub(r'^(?:\([a-zA-Z0-9]+\)\s*)+', '', p)) in norm(raw) for p in e['text'].splitlines())
        assert 'urgency statute' in norm(raw) and 'go into immediate effect' in norm(raw)
        e.update(effective='1999-10-10', effective_basis='conservative availability-by date: urgency act filed1999-10-10 (governor approved1999-10-08); not asserted as an adjudicated exact effective moment',
                 source_publication_date='1999-10-10', verification_status='selected_enacted_text_exactly_matched',
                 verification='All five selected paragraphs match the supplied SB1282(1999),ch.854 official chaptered text after removing repeated subsection labels. SEC3 is an urgency clause. Later amendments are not substituted; full surrounding historical code is not claimed.')
        checks.append(dict(rule=e['legal_id'],status='pass',exact_paragraphs=5,scope='selected enacted text only',availability_no_later_than='1999-10-10'))
        e = by_id['L_VEH_23153_2000']
        raw = html_text(cd / e['snapshot_path'])
        section = raw[raw.index('SEC. 4.'):raw.index('SEC. 5.',raw.index('SEC. 4.'))]
        assert norm(e['text']) in norm(section)
        assert 'Chapter 974 of the Statutes of 1992' in section
        e.update(title='Vehicle Code §23153(a),(b),(c) — historical core corroborated by pre-2000 opinions',
                 effective='1999-02-16', effective_basis='conservative verified availability-by date (Ellis); not enactment date. Weems1997 corroborates(a)-(c), Yordamlis1992 the three-hour premise.',
                 source_publication_date='2012-09-29',
                 verification='Selected core matches the official2012 SEC4 restatement explicitly attributed to Stats.1992,ch.974,sec.18. Pre-2000 primary opinions independently corroborate the offense elements (Weems1997, Ellis1999), ordinary-negligence rule (Weems), and three-hour premise (Yordamlis1992). The2014 changes in SEC5 are excluded. This verifies the used core, not the complete2000 statute edition or every intervening chapter.',
                 verification_status='selected_core_text_and_contemporaneous_propositions_verified',
                 corroborating_legal_ids=['L_WEEMS_1997','L_ELLIS_1999','L_YORDAMLIS_1992'],
                 source_locator='AB2552(2012),SEC4, former1992-amended section; excludes SEC5 operative2014 text')
        checks.append(dict(rule=e['legal_id'],status='pass',exact_core_match='official2012 SEC4 (1992-amended predecessor)',contemporaneous_support=e['corroborating_legal_ids'],scope='core(a)-(c) and conditional timing premise; not full historic edition'))
        instructions += [
            instruction('J_006_C01',['C01'],['L_VEH_23153_2000','L_ELLIS_1999','L_ENRIQUEZ_1996'],
                'C01须分别证明：被告驾驶车辆；驾驶时受酒精影响；同时另外实施违法行为或未履行驾驶中的法定义务；该行为或不履行近因造成他人身体受伤。“受影响”要求饮酒实际损害身心驾驶能力，以致不能达到同等情形下通常谨慎清醒者的驾驶标准；仅证明饮酒或检出酒精并不足够。',
                'Contemporaneous CALJIC12.60/16.831 reported in Ellis1999; impairment-definition requirement corroborated by Enriquez1996.'),
            instruction('J_006_C02',['C02'],['L_VEH_23153_2000','L_ELLIS_1999','L_WEEMS_1997'],
                'C02须分别证明：被告驾驶车辆；驾驶时血液酒精重量比达到0.08%或以上；同时另外实施违法行为或未履行驾驶中的法定义务；该行为或不履行近因造成他人身体受伤。检验时浓度与驾驶时浓度须区分。C01的受影响要件与C02的驾驶时浓度要件须分别评议，不能相互替代。',
                'Pre2000 statutory elements and Ellis footnotes4–5; not a post2006 CALCRIM instruction.'),
            instruction('J_006_ACT_CAUSATION',['C01','C02'],['L_VEH_23153_2000','L_WEEMS_1997','L_ELLIS_1999'],
                '两项指控均须在酒驾之外确定另一具体违法行为或驾驶义务的不履行，并证明它近因造成他人受伤；单有酒驾或发生碰撞不能自动补齐这两个要件。普通过失可以满足注意义务要求，不一定须另证违反某一具体车法条号。评价的是对伤害的因果关系，不能机械地只问是否引发碰撞。请依本案证据和法官对具体违法行为的解释判断，不得移植其他判例的超速或安全带事实。',
                'Weems selected holding and Ellis instruction-definition requirement; charge-neutral adaptation.'),
            instruction('J_006_BAC_TIMING',['C02'],['L_VEH_23153_2000','L_YORDAMLIS_1992','L_PEN_1096'],
                '只有在证据排除合理怀疑地证明化学检测在驾驶后三小时内且浓度达0.08%时，才可以据此推断驾驶时浓度至少0.08%；该推论并非必须作出。未证明时间前提时不得直接套用。仍须考虑全部准许证据，控方的证明责任不转移给被告。缺少某一时刻记录也不当然使其他血醇证据失去所有证明力。',
                'Historical statutory timing premise and CALJIC12.61.1 reference in Yordamlis; administrative decision used only within its stated scope; criminal burden retained under PEN1096.')]
        limitations += ['006指示以2000年前原判记载的CALJIC要件为依据，没有把2006年后CALCRIM当成2000年的指示。',
                        '§23153选用核心经官方后续重述逐字校验并由当时原判交叉核验；未取得完整2000法典版本或完整历史CALJIC卷册，故不声称整卷复原。']

    # Every declared legal availability flag is recomputed using the applicable trial cutoff.
    for e in entries:
        e['cutoff_ok'] = e['effective'] <= manifest['law_cutoff']
        e['distractor'] = not e['cutoff_ok']
        assert sha((cd / e['snapshot_path']).read_bytes()) == e['sha256'], e['legal_id'] + ': raw digest mismatch'
        assert sha(e['text'].encode('utf-8')) == e['text_sha256'], e['legal_id'] + ': text digest mismatch'
    manifest['version'] = 'teacher-three-round-v2-20261021'
    manifest['jury_instructions_file'] = 'jury_instructions.json'
    manifest['legal_review'] = dict(reviewed_at=REVIEWED, status='key_historical_core_and_elements_verified',
                                   verification_file='../legal_corpus/verification.json',
                                   scope='selected core texts and element instructions, not certification of complete historical code or real legal advice')
    manifest['counts']['legal_entries'] = len(entries)
    dump(base / 'world/manifest.json', manifest)
    dump(base / 'world/jury_instructions.json', dict(version='historical-charge-instructions-v2', case_id=manifest['case_id'],
         law_cutoff=manifest['law_cutoff'], substantive_law_date=manifest['substantive_law_date'], instructions=instructions,limitations=limitations))
    (cd / 'corpus.jsonl').write_text(''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in entries),encoding='utf-8')
    dump(cd / 'index.json', [{k:v for k,v in e.items() if k != 'text'} for e in entries])
    dump(cd / 'verification.json', dict(reviewed_at=REVIEWED, scope='key historical core and dates', entry_count=len(entries),
         checks=checks, cutoff_check=dict(cutoff=manifest['law_cutoff'], all_flags_match_dates=True,
         future_legal_ids=[e['legal_id'] for e in entries if not e['cutoff_ok']]),
         remaining_boundaries=limitations, hash_verification='All raw-file bytes and all corpus text SHA256 values checked. New web records are labelled retrieval notes, not full page snapshots.'))
    (cd / 'README.md').write_text(f'''# Legal corpus — {manifest['case_id']}\n\n本包共 {len(entries)} 条检索条目。`corpus.jsonl` 为正文，`index.json` 为同内容元数据，`raw/` 保留原始输入快照以及明确命名的新检索记录。`verification.json` 记录本次实际完成的核验范围。\n\n已核验所用关键历史核心条文和要件；不声明完整历史法典／CALJIC卷册复原。`effective` 用于可用性截止判断，其含义由 `effective_basis` 明示：可能是确切有效日期、判决日期，或保守的已证实可用日期，不应统一当作立法生效日。`retrieved_at` 是来源抓取日期，不是法律有效日期。\n\n`canonical_id` 标识同一规则的来源别名。特别是 `L-EVID1101` 与 `L_EVID_1101` 的相同(a)-(b)核心均在1997年已有效，不应因ID或网页日期不同把其中一个判为未来法。原始教师抽取记录里的旧元数据原样保留供追溯，运行采用 corpus/index 的修订字段。\n\n新加入的判例文件是原判网页的短摘录、检索定位及研究摘要，有真实URL、抓取日期和字节／正文SHA256；它们不是完整原判HTML。使用的是原判正文而非网站的自动摘要。中文陪审指示位于 `../world/jury_instructions.json`，逐项列明支持法源与改写范围。\n''', encoding='utf-8')
    return dict(case=case,legal_entries=len(entries),instructions=len(instructions),cutoff=manifest['law_cutoff'],future_ids=[e['legal_id'] for e in entries if not e['cutoff_ok']])

if __name__ == '__main__':
    print(json.dumps([revise('005_Williams'),revise('006_Wilson')],ensure_ascii=False,indent=2))
