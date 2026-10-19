"""Offline tests for independent grades, warnings, and durable request budgets."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'runtime')]
import backends
backends.bootstrap()
import three_scoring as ds
import scoring as s


def request_fixture():
    points = [{'point_id': f'TEST-{n}', 'title': f'合成项{n}', 'scope': 'trial',
               'artifact_requirements': []} for n in range(3)]
    row = {'type': 'object', 'properties': {
        'point_id': {'type': 'string'}, 'score': {'type': 'integer', 'enum': [0, 1, 2]},
        'reason': {'type': 'string'}, 'event_ids': {'type': 'array', 'items': {'type': 'string'}},
        'artifact_ids': {'type': 'array', 'items': {'type': 'string'}},
        'observation_limit': {'type': 'string'}},
        'required': ['point_id', 'score', 'reason', 'event_ids', 'artifact_ids', 'observation_limit'],
        'additionalProperties': False}
    return {'role_id': 'R_DIRECT_RUBRIC_SCORER', 'system': ds.SYSTEM,
        'context': {'protocol': ds.PROTOCOL, 'case_id': 'TEST', 'points': points,
            'events': [{'event_id': 'E1', 'actor': 'R_DEFENSE', 'kind': 'speak'}],
            'verified_artifacts': [], 'rubric_version': 'fixture'},
        'schema': {'type': 'object', 'properties': {
            'protocol': {'type': 'string'}, 'case_id': {'type': 'string'},
            'points': {'type': 'array', 'items': row}},
            'required': ['protocol', 'case_id', 'points'], 'additionalProperties': False}}


def reply_fixture(request, *, bad_refs=False, blank_reason=False):
    return {'protocol': ds.PROTOCOL, 'case_id': 'TEST', 'points': [
        {'point_id': point['point_id'], 'score': n % 3,
         'reason': '' if blank_reason else '合成离线评分理由；验证数据流，不表示真实模型能力。',
         'event_ids': ['NO_SUCH_EVENT'] if bad_refs else ['E1'],
         'artifact_ids': ['NO_SUCH_ARTIFACT'] if bad_refs else [], 'observation_limit': ''}
        for n, point in enumerate(request['context']['points'])]}


class Backend:
    def __init__(self, seen, reply):
        self.seen, self.reply, self.calls, self.stats = seen, reply, [], {}
    def decide(self, **request):
        self.seen.append(deepcopy(request))
        self.calls.append({'status': 'completed'})
        return deepcopy(self.reply)
    def close(self):
        pass


class DirectScoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='legal-direct-scoring-')
        self.case = Path(self.temp.name) / 'case'
        self.court = self.case / 'court_attempts/attempt-001'
        self.court.mkdir(parents=True)
        (self.court / 'synthetic_court.txt').write_text('SYNTHETIC_COURT_ONLY')
        (self.case / 'old_score.json').write_text('OLD_SCORE_MUST_NOT_BE_LOADED')
        self.request = request_fixture()
        self.asset = {'case_id': 'TEST', 'rubric': {'points': self.request['context']['points']}}
    def tearDown(self):
        self.temp.cleanup()
    def score(self, factory):
        with patch.object(ds, 'prepare_request', return_value=deepcopy(self.request)), redirect_stdout(io.StringIO()):
            return ds.score_case(self.asset, {}, self.case, self.court, {}, factory)
    @staticmethod
    def forbidden(*_args):
        raise AssertionError('should not make a model call')

    def test_unknown_citations_warn_without_rejecting_or_changing_grades(self):
        raw = reply_fixture(self.request, bad_refs=True)
        before = deepcopy(raw)
        score = ds.validate_score(raw, self.request)
        self.assertEqual(raw, before)
        self.assertEqual(score['status'], 'complete')
        self.assertEqual(score['review_status'], 'needs_review')
        self.assertEqual((score['numerator'], score['denominator']), (3, 6))
        self.assertTrue(score['warnings'])
        self.assertEqual([row['score'] for row in score['points']], [0, 1, 2])
        self.assertEqual([row['reason'] for row in score['points']], [row['reason'] for row in raw['points']])

    def test_basic_format_errors_still_block_publication(self):
        for change in ('boolean_score', 'missing_item', 'duplicate_item', 'empty_reason', 'reference_type'):
            raw = reply_fixture(self.request)
            if change == 'boolean_score': raw['points'][0]['score'] = True
            if change == 'missing_item': raw['points'].pop()
            if change == 'duplicate_item': raw['points'][2] = deepcopy(raw['points'][1])
            if change == 'empty_reason': raw['points'][0]['reason'] = ' '
            if change == 'reference_type': raw['points'][0]['event_ids'] = 'E1'
            with self.subTest(change=change), self.assertRaises(s.ScoringError) as caught:
                ds.validate_score(raw, self.request)
            self.assertEqual(caught.exception.code, 'direct_score_format_invalid')

    def test_one_full_request_completes_with_warnings_and_resume_makes_no_calls(self):
        seen, configurations = [], []
        def factory(config, _attempt):
            configurations.append(config)
            return Backend(seen, reply_fixture(self.request, bad_refs=True))
        score = self.score(factory)
        self.assertEqual(seen, [self.request])
        self.assertEqual(score['review_status'], 'needs_review')
        self.assertEqual(configurations[0]['models']['environment'], 'gpt-6-sol')
        self.assertEqual(configurations[0]['timeout_seconds'], 300)
        self.assertEqual(configurations[0]['max_output_tokens'], 16384)
        self.assertEqual(configurations[0]['max_input_chars'], 1500000)
        self.assertNotIn('OLD_SCORE_MUST_NOT_BE_LOADED', s.canonical(seen))
        before = ds._inventory(self.case)
        self.assertEqual(self.score(self.forbidden), score)
        self.assertEqual(ds._inventory(self.case), before)

    def test_basic_format_retry_stays_within_new_eight_attempt_budget(self):
        seen = []
        with patch.object(ds.time, 'sleep'):
            with self.assertRaises(s.ScoringError) as caught:
                self.score(lambda *_: Backend(seen, reply_fixture(self.request, blank_reason=True)))
        self.assertEqual(caught.exception.code, 'direct_scoring_budget_exhausted')
        self.assertEqual(len(seen), 8)
        self.assertTrue(all(request == self.request for request in seen))
        before = ds._inventory(self.case)
        with self.assertRaises(s.ScoringError) as caught:
            self.score(self.forbidden)
        self.assertEqual(caught.exception.code, 'direct_scoring_budget_exhausted')
        self.assertEqual(ds._inventory(self.case), before)

    def test_known_reply_after_crash_is_replayed_without_resend(self):
        seen = []
        original = ds._Ledger.append
        def interrupted_finish(ledger, row):
            if row['kind'] == 'finish':
                raise KeyboardInterrupt('synthetic finish interruption')
            return original(ledger, row)
        with patch.object(ds._Ledger, 'append', interrupted_finish), self.assertRaises(KeyboardInterrupt):
            self.score(lambda *_: Backend(seen, reply_fixture(self.request)))
        self.assertEqual(len(seen), 1)
        score = self.score(self.forbidden)
        self.assertEqual(score['status'], 'complete')
        rows = s.read(self.case / 'direct_score/attempts.jsonl')
        self.assertEqual(len([row for row in rows if row['kind'] == 'reserve']), 1)
        self.assertEqual(len([row for row in rows if row['kind'] == 'finish']), 1)

    def test_unknown_reservation_blocks_even_an_earlier_saved_good_answer(self):
        self.score(lambda *_: Backend([], reply_fixture(self.request)))
        directory = self.case / 'direct_score'
        (directory / 'score_complete.json').unlink()
        (directory / 'score.json').unlink()
        ledger = ds._Ledger(directory, s.read(directory / 'input_binding.json'))
        ledger.append({'kind': 'reserve', 'attempt': 2, 'request_sha256': s.obj_hash(self.request)})
        attempt = directory / 'attempts/attempt-002'
        attempt.mkdir()
        s.immutable_json(attempt / 'request.json', self.request)
        with self.assertRaises(s.ScoringError) as caught:
            self.score(self.forbidden)
        self.assertEqual(caught.exception.code, 'direct_reserved_outcome_unknown')
        self.assertFalse((directory / 'score_complete.json').exists())

    def test_completed_score_detects_court_or_grade_changes(self):
        self.score(lambda *_: Backend([], reply_fixture(self.request)))
        (self.court / 'synthetic_court.txt').write_text('CHANGED')
        with self.assertRaises(s.ScoringError) as caught:
            self.score(self.forbidden)
        self.assertEqual(caught.exception.code, 'immutable_scoring_input_changed')


def draft_fixture():
    points = []
    for number, scope in enumerate(('pretrial', 'investigation', 'artifact', 'trial'), 1):
        points.append({'point_id': f'TEST-{number:02d}', 'title': '离线测试评分项',
            'domain': 'synthetic', 'criterion': '仅用于检查数据管道，不是正式法律标准。',
            'scope': scope, 'weight': 1, 'source_ids': ['SOURCE-1'],
            'score_anchors': {'0': '未展示。', '1': '部分展示。', '2': '完整展示。'},
            'artifact_requirements': ['合成报告'] if scope == 'artifact' else []})
    return {'case_id': 'TEST', 'version': 'synthetic-test-v1', 'approved': True,
            'point_count': 4, 'maximum_points': 8, 'points': points,
            'development_run_ids': ['synthetic-development-only']}


class FrozenRubricTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='three-round-rubric-')
        self.root = Path(self.temp.name)
        self.source = self.root / 'case_source'
        self.source.mkdir()
        (self.source / 'case.md').write_text('合成测试材料。', encoding='utf-8')
        self.asset = {'case_id': 'TEST', 'case_path': self.source,
            'workflow_spec': {'task': '合成任务', 'files': [], 'services': []},
            'source_map': {'SOURCE-1': {'source_id': 'SOURCE-1', 'text': '合成来源'}},
            'source_audit': {'status': 'passed'}, 'rubric': draft_fixture()}
        self.draft = self.root / 'draft.json'
        self.frozen = self.root / 'rubrics/TEST.json'
        s.atomic_write(self.draft, self.asset['rubric'])

    def tearDown(self):
        self.temp.cleanup()

    def freeze(self):
        value = ds.freeze_rubric(self.asset, self.draft, self.frozen)
        self.asset['rubric'] = value
        return value

    def test_empty_rubric_blocks_without_creating_criteria(self):
        self.asset['rubric'] = {'case_id': 'TEST', 'items': [], 'points': [],
                                'status': 'pending_development_trial'}
        with self.assertRaises(s.ScoringError) as caught:
            ds.validate_rubric(self.asset)
        self.assertEqual(caught.exception.code, 'rubric_pending')
        self.assertEqual(self.asset['rubric']['points'], [])
        self.assertFalse(self.frozen.exists())

    def test_freeze_requires_explicit_approval_and_development_run(self):
        for field, value, expected in (
                ('approved', False, 'rubric_approval_required'),
                ('development_run_ids', [], 'rubric_development_run_required')):
            draft = draft_fixture(); draft[field] = value
            s.atomic_write(self.draft, draft)
            with self.subTest(field=field), self.assertRaises(s.ScoringError) as caught:
                ds.freeze_rubric(self.asset, self.draft, self.frozen)
            self.assertEqual(caught.exception.code, expected)
        self.assertFalse(self.frozen.exists())

    def test_freeze_preserves_criteria_is_idempotent_and_detects_source_change(self):
        before = deepcopy(self.asset['rubric']['points'])
        frozen = self.freeze()
        self.assertEqual(frozen['points'], before)
        self.assertEqual(ds.validate_rubric(self.asset), before)
        self.assertEqual(ds.freeze_rubric(self.asset, self.draft, self.frozen), frozen)
        (self.source / 'case.md').write_text('改变了材料', encoding='utf-8')
        with self.assertRaises(s.ScoringError) as caught:
            ds.validate_rubric(self.asset)
        self.assertEqual(caught.exception.code, 'frozen_rubric_inputs_changed')

    def test_frozen_criterion_change_is_rejected(self):
        self.freeze()
        self.asset['rubric']['points'][0]['criterion'] = '事后改变标准'
        with self.assertRaises(s.ScoringError) as caught:
            ds.validate_rubric(self.asset)
        self.assertEqual(caught.exception.code, 'frozen_rubric_changed')

    def test_unapproved_unfrozen_draft_is_never_formally_scored(self):
        with self.assertRaises(s.ScoringError) as caught:
            ds.validate_rubric(self.asset)
        self.assertEqual(caught.exception.code, 'rubric_not_frozen')

    def test_full_request_contains_all_three_rounds_but_no_old_scores_or_verdict(self):
        self.freeze()
        case = self.root / 'run'
        court = case / 'court_attempts/attempt-001'
        court.mkdir(parents=True)
        events = [{'event_id': f'E{n}', 'stage_id': stage, 'kind': 'speak',
                   'actor': 'R_DEFENSE', 'text': f'{stage}合成陈述'}
                  for n, stage in enumerate(('pretrial_workspace', 'r1_motion',
                                            'r2_examination', 'r3_closing'), 1)]
        events.append({'event_id': 'E5', 'stage_id': 'r3_verdict', 'kind': 'verdict',
                       'actor': 'R_JURY', 'text': 'VERDICT_SHOULD_BE_WITHHELD'})
        s.atomic_write(court / 'summary.json', {'case_id': 'TEST', 'current_stage': 'ended',
            'mode': 'live', 'status': 'completed_development_run'})
        s.atomic_write(court / 'author_events.jsonl', ''.join(s.canonical(e) + '\n' for e in events))
        s.atomic_write(court / 'artifacts/manifest.json', {'version': 'workflow-artifacts-v3', 'artifacts': []})
        s.atomic_write(case / 'private_model_io.jsonl', '')
        s.atomic_write(case / 'action_attempts.jsonl', '')
        s.atomic_write(case / 'old_score.json', {'score': 'OLD_SCORE_MUST_NOT_BE_LOADED'})
        info = {'procedure': {'stages': [{'stage_id': e['stage_id']} for e in events]},
                'permissions': {'allowed': 'synthetic'}, 'old_scores': [99]}
        request = ds.prepare_request(self.asset, info, case, court)
        text = s.canonical(request)
        self.assertEqual(len(request['context']['points']), 4)
        self.assertIn('r3_closing', text)
        self.assertNotIn('VERDICT_SHOULD_BE_WITHHELD', text)
        self.assertNotIn('OLD_SCORE_MUST_NOT_BE_LOADED', text)
        self.assertFalse(request['context']['policy']['previous_scores_loaded'])
        self.assertEqual(request['context']['fixed_case_denominator'], 8)


if __name__ == '__main__':
    unittest.main()
