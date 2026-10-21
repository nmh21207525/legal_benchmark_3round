#!/usr/bin/env python3
"""Read-only corpus, cutoff, aliases and historical instruction integration checks."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def verify(root=ROOT):
    results = []
    for case, expected in [('005_Williams',43),('006_Wilson',47)]:
        base = Path(root) / case
        cd = base / 'legal_corpus'
        rows = [json.loads(x) for x in (cd/'corpus.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()]
        idx = json.loads((cd/'index.json').read_text(encoding='utf-8'))
        manifest = json.loads((base/'world/manifest.json').read_text(encoding='utf-8'))
        instructions = json.loads((base/'world/jury_instructions.json').read_text(encoding='utf-8'))
        by_id = {x['legal_id']:x for x in rows}
        assert len(rows) == expected == len(by_id)
        assert idx == [{k:v for k,v in x.items() if k != 'text'} for x in rows]
        assert manifest['counts']['legal_entries'] == expected
        assert manifest['jury_instructions_file'] == 'jury_instructions.json'
        for row in rows:
            assert hashlib.sha256((cd/row['snapshot_path']).read_bytes()).hexdigest() == row['sha256'], row['legal_id']
            assert hashlib.sha256(row['text'].encode('utf-8')).hexdigest() == row['text_sha256'], row['legal_id']
            assert row['cutoff_ok'] == (row['effective'] <= manifest['law_cutoff']), row['legal_id']
            assert row['distractor'] == (not row['cutoff_ok']), row['legal_id']
            canonical = by_id[row['canonical_id']]
            assert row['cutoff_ok'] == canonical['cutoff_ok'], row['legal_id']
            assert row['effective'] == canonical['effective'], row['legal_id']
        assert by_id['L-EVID1101']['effective'] == '1997-01-01'
        assert by_id['L-EVID1101']['canonical_id'] == 'L_EVID_1101'
        charges = {x['charge_id'] for x in manifest['charges']}
        covered = set()
        assert len({x['id'] for x in instructions['instructions']}) == len(instructions['instructions'])
        for inst in instructions['instructions']:
            assert set(inst['charge_ids']) <= charges
            covered.update(inst['charge_ids'])
            assert inst['text'] and inst['verification'] and inst['source_type']
            for legal_id in inst['legal_ids']:
                assert legal_id in by_id and by_id[legal_id]['cutoff_ok'], legal_id
        assert covered == charges
        assert by_id['L_BARRETT_2000']['effective'] == '2000-05-25'
        if case.startswith('005'):
            assert by_id['L_PEN_290_2006']['effective'] == '2006-01-01'
            assert by_id['L_CALCRIM_1170_2006_EXCERPT']['effective'] == '2006-01-01'
        else:
            for legal_id in ['L_ELLIS_1999','L_WEEMS_1997','L_ENRIQUEZ_1996','L_YORDAMLIS_1992']:
                assert by_id[legal_id]['effective'] < '2000-01-01'
            assert by_id['L_VEH_23153_2000']['source_publication_date'] == '2012-09-29'
            assert by_id['L_VEH_23153_2000']['cutoff_ok']
        results.append(dict(case=case,entries=len(rows),charge_instructions=len(instructions['instructions']),
                            all_hashes_match=True,aliases_consistent=True,all_cutoffs_consistent=True))
    return results

if __name__ == '__main__':
    print(json.dumps(verify(),ensure_ascii=False,indent=2))
