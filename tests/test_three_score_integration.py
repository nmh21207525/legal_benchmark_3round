"""Synthetic scores over the actual 005/006 offline court fixtures.

All approval flags and criteria in this test are disposable test fixtures.
Nothing is written to shipped rubrics, no legal score is asserted, and socket
connections are forbidden throughout the test.
"""
from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
import backends
backends.bootstrap()
import runner
import scoring as s
import three_scoring as ds
from three_assets import ThreeRoundCaseData, load_assets
from three_demo import create_backend
from three_engine import ThreeRoundEngine


class SyntheticScorer:
    def __init__(self, calls):
        self.recorded = calls
        self.calls, self.stats = [], {'paid_api_calls': 0}

    def decide(self, **request):
        self.recorded.append(deepcopy(request))
        self.calls.append({'status': 'completed', 'model': 'offline_synthetic_scorer'})
        events = request['context']['events']
        eid = next(e['event_id'] for e in events
                   if e.get('actor') == 'R_DEFENSE' and e.get('kind') == 'tool_call')
        artifacts = request['context']['verified_artifacts']
        aid = next(a['artifact_id'] for a in artifacts
                   if a.get('actor') == 'R_DEFENSE' and a.get('is_latest_version'))
        return {'protocol': ds.PROTOCOL, 'case_id': request['context']['case_id'],
            'points': [{'point_id': p['point_id'], 'score': index % 3,
                'reason': '仅用于离线集成测试的合成分数，不代表任何律师模型的法律能力。',
                'event_ids': [eid], 'artifact_ids': [aid], 'observation_limit': ''}
                for index, p in enumerate(request['context']['points'])]}

    def close(self):
        pass


def synthetic_draft(asset):
    source = next(sid for sid in asset['source_map'] if sid != 'ENV-TASK')
    return {'case_id': asset['case_id'], 'version': 'SYNTHETIC-TEST-ONLY',
        'approved': True, 'development_run_ids': ['SYNTHETIC-OFFLINE-TEST-ONLY'],
        'point_count': 4, 'maximum_points': 8,
        'points': [{'point_id': asset['case_id'] + '-SYNTHETIC-' + str(index),
            'scope': scope, 'title': '仅供合成离线测试', 'domain': 'synthetic',
            'criterion': '检查来源、真实产物和三轮记录能否进入评分请求，不是法律标准。',
            'score_anchors': {'0': '合成零分锚点', '1': '合成一分锚点', '2': '合成二分锚点'},
            'weight': 1, 'source_ids': [source],
            'artifact_requirements': ['case_timeline.csv'] if scope == 'artifact' else []}
            for index, scope in enumerate(('pretrial', 'investigation', 'artifact', 'trial'))]}


class RealAssetScoringIntegration(unittest.TestCase):
    def test_preflight_limits_do_not_mutate_trial_or_witness_schema(self):
        from court_agent.schema import action, action_schema, WITNESS_SCHEMA
        from court_agent.llm import _validate_value
        action_before = deepcopy(action_schema(['pass', 'closing_argument']))
        witness_before = deepcopy(WITNESS_SCHEMA)
        for _ in range(6):
            request = runner.preflight_request()
            self.assertEqual(request['schema']['properties']['text']['maxLength'], 500)
            self.assertEqual(request['schema']['properties']['references']['maxItems'], 0)
        self.assertEqual(action_schema(['pass', 'closing_argument']), action_before)
        self.assertEqual(WITNESS_SCHEMA, witness_before)
        self.assertTrue(_validate_value(action('pass'), action_before, action_before))
        long_action = action('closing_argument', text='庭审陈述' * 300,
                             references=['E00001', 'M01-P001'])
        self.assertTrue(_validate_value(long_action, action_before, action_before))
        witness = {'unknown': False, 'fact_ids': ['F01', 'F02']}
        self.assertTrue(_validate_value(witness, WITNESS_SCHEMA, WITNESS_SCHEMA))

    def test_actual_cases_offline_courts_freeze_and_direct_score_resume(self):
        shipped_before = {p: s.sha(p) for p in (ROOT / 'data').rglob('rubric.json')}
        assets = load_assets(ROOT / 'data')
        config = backends.normalize_config({
            'api': {'provider': 'packy'}, 'codex': {'executable': 'codex'},
            'max_lawyer_actions': 120, 'default_actions_per_role_per_stage': 3,
            'max_model_calls': 360,
        }, ROOT)
        # A real live launcher performs preflight before entering either court.
        # Keep this order even in an isolated test process to catch schema alias
        # mutations, not merely test-order interactions.
        runner.preflight_request()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), \
                patch('socket.socket.connect', side_effect=AssertionError('test_attempted_network')):
            out = Path(directory)
            for cid, original in assets.items():
                with self.subTest(case=cid):
                    asset = deepcopy(original)
                    with self.assertRaises(s.ScoringError) as caught:
                        ds.validate_rubric(asset)
                    self.assertEqual(caught.exception.code, 'rubric_pending')
                    case = ThreeRoundCaseData(asset['case_path'])
                    args = SimpleNamespace(out=out, project_root=ROOT, mock=True,
                                           claude_executable='unused-offline-test')
                    attempt = runner.run_case(args, config, asset, case, 'gpt', {}, '0' * 64,
                        engine_factory=ThreeRoundEngine, backend_factory=create_backend)
                    summary = s.read(attempt / 'summary.json')
                    self.assertEqual(summary['status'], 'completed_development_run')
                    self.assertEqual(summary['mode'], 'mock')
                    draft = out / ('SYNTHETIC-' + cid + '.json')
                    frozen = out / 'SYNTHETIC-rubrics' / (cid + '.json')
                    s.atomic_write(draft, synthetic_draft(asset))
                    asset['rubric'] = ds.freeze_rubric(asset, draft, frozen)
                    case_dir = attempt.parent.parent
                    request = ds.prepare_request(asset, case, case_dir, attempt, mock=True)
                    stages = {e['stage_id'] for e in request['context']['events']}
                    self.assertTrue({'pretrial_workspace', 'r1_motion', 'r2_examination',
                                     'r3_clarification', 'r3_instructions'} <= stages)
                    self.assertTrue(request['context']['verified_artifacts'])
                    self.assertFalse(any(e['actor'] == 'R_JURY' for e in request['context']['events']))
                    self.assertFalse(request['context']['policy']['previous_scores_loaded'])
                    self.assertNotIn('CA-PILOT-003', s.canonical(request))
                    self.assertNotIn('CA-PILOT-004', s.canonical(request))
                    seen = []
                    score = ds.score_case(asset, case, case_dir, attempt, config,
                        lambda *_: SyntheticScorer(seen), mock=True)
                    self.assertEqual(len(seen), 1)
                    self.assertEqual(score['status'], 'complete')
                    self.assertEqual((score['numerator'], score['denominator']), (3, 8))
                    self.assertTrue(score['mock_only'])
                    self.assertEqual(score['rubric_freeze_sha256'], asset['rubric']['freeze_sha256'])
                    before = ds._inventory(case_dir / 'direct_score')
                    def forbidden(*_):
                        raise AssertionError('cached_score_must_not_call_backend')
                    self.assertEqual(ds.score_case(asset, case, case_dir, attempt, config,
                        forbidden, mock=True), score)
                    self.assertEqual(ds._inventory(case_dir / 'direct_score'), before)
                    # Mock court records can never be accepted as live records.
                    with self.assertRaises(s.ScoringError) as caught:
                        ds.prepare_request(asset, case, case_dir, attempt)
                    self.assertEqual(caught.exception.code, 'court_not_complete')
        self.assertEqual({p: s.sha(p) for p in shipped_before}, shipped_before)
        for asset in load_assets(ROOT / 'data').values():
            self.assertFalse(asset['rubric']['points'])
            self.assertFalse(asset['rubric']['approved'])


if __name__ == '__main__':
    unittest.main()
