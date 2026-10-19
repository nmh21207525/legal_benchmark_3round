"""Pre-run criteria are explicit, portable, immutable and independent of scores."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import importlib.util
import io
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'runtime')]
import backends
backends.bootstrap()
import scoring as s
import three_scoring as ds

spec = importlib.util.spec_from_file_location('prerun_freeze_cli', ROOT / 'freeze_rubric.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def draft_fixture():
    points = []
    for number, scope in enumerate(('pretrial', 'investigation', 'artifact', 'trial'), 1):
        points.append({'point_id': f'CA-PILOT-005-TEST-{number:02d}', 'title': '合成检查项',
            'domain': 'synthetic', 'criterion': '仅验证评分协议，非正式法律评分标准。',
            'scope': scope, 'weight': 1, 'source_ids': ['SOURCE-1'],
            'score_anchors': {'0': '未展示。', '1': '部分展示。', '2': '完整展示。'},
            'artifact_requirements': ['synthetic.md'] if scope == 'artifact' else []})
    return {'case_id': 'CA-PILOT-005', 'version': 'synthetic-prerun-v1', 'approved': True,
            'point_count': 4, 'maximum_points': 8, 'points': points,
            'authoring_provenance': {'authored_by': 'Synthetic test fixture; no human review claimed',
                'authorization_basis': 'user_requested_pre_run_rubric',
                'review_status': 'not_independently_human_reviewed',
                'authored_at': '2026-10-19T00:00:00+00:00',
                'rubric_version': 'synthetic-prerun-v1'}}


class PreRunRubricTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='legal-prerun-rubric-')
        self.root = Path(self.temp.name)
        source = self.root / 'server-one/workspace/data/case'
        source.mkdir(parents=True)
        (source / 'case.md').write_text('合成来源。', encoding='utf-8')
        self.draft_value = draft_fixture()
        self.asset = {'case_id': 'CA-PILOT-005', 'case_path': source,
            'workflow_spec': {'task': '合成任务', 'files': [], 'services': []},
            'source_map': {'SOURCE-1': {'source_id': 'SOURCE-1', 'text': '合成来源'}},
            'source_audit': {'status': 'passed'}, 'rubric': self.draft_value}
        self.draft = self.root / 'draft.json'
        self.output = self.root / 'rubrics/CA-PILOT-005.json'
        s.atomic_write(self.draft, self.draft_value)

    def tearDown(self):
        self.temp.cleanup()

    def freeze(self, **kwargs):
        return ds.freeze_rubric(self.asset, self.draft, self.output,
                                basis='pre_run_authoring', **kwargs)

    def test_pre_run_freeze_preserves_anchors_without_claiming_trial_or_expert_review(self):
        frozen = self.freeze()
        self.assertEqual(frozen['freeze_basis'], 'pre_run_authoring')
        self.assertEqual(frozen['development_run_ids'], [])
        self.assertEqual(frozen['points'], self.draft_value['points'])
        self.assertEqual(frozen['authoring_provenance'], self.draft_value['authoring_provenance'])
        self.assertEqual(ds.validate_rubric({**self.asset, 'rubric': frozen}), frozen['points'])
        self.assertEqual(self.freeze(), frozen)

    def test_original_default_still_requires_development_run(self):
        with self.assertRaises(s.ScoringError) as caught:
            ds.freeze_rubric(self.asset, self.draft, self.output)
        self.assertEqual(caught.exception.code, 'rubric_development_run_required')
        self.assertFalse(self.output.exists())

    def test_pre_run_rejects_a_claimed_development_run(self):
        with self.assertRaises(s.ScoringError) as caught:
            self.freeze(development_run_ids=['pretended-run'])
        self.assertEqual(caught.exception.code, 'pre_run_rubric_must_not_claim_development_run')

    def test_authorship_authorization_review_and_date_must_be_explicit(self):
        for field in ('authored_by', 'authorization_basis', 'review_status', 'authored_at', 'rubric_version'):
            draft = deepcopy(self.draft_value)
            draft['authoring_provenance'].pop(field)
            s.atomic_write(self.draft, draft)
            with self.subTest(field=field), self.assertRaises(s.ScoringError):
                self.freeze()
        self.assertFalse(self.output.exists())

    def test_pre_run_does_not_override_absent_user_authorization(self):
        draft = deepcopy(self.draft_value); draft['approved'] = False
        s.atomic_write(self.draft, draft)
        with self.assertRaises(s.ScoringError) as caught:
            self.freeze()
        self.assertEqual(caught.exception.code, 'rubric_approval_required')

    def test_frozen_provenance_cannot_be_silently_rewritten(self):
        frozen = self.freeze()
        frozen['authoring_provenance']['authored_by'] = 'Changed author claim'
        with self.assertRaises(s.ScoringError) as caught:
            ds.validate_rubric({**self.asset, 'rubric': frozen})
        self.assertEqual(caught.exception.code, 'frozen_rubric_changed')

    def test_content_binding_survives_server_and_workspace_rename_but_detects_code_changes(self):
        frozen = self.freeze()
        relocated = self.root / 'another-server/legal_agent_3round'
        moved_source = relocated / 'data/renamed-case-directory'
        shutil.copytree(self.asset['case_path'], moved_source)
        shutil.copytree(ROOT / 'runtime', relocated / 'runtime',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        moved = {**self.asset, 'case_path': moved_source, 'rubric': frozen}
        with patch.object(ds, '__file__', str(relocated / 'runtime/three_scoring.py')):
            self.assertEqual(ds.rubric_binding(moved), frozen['input_binding'])
            self.assertEqual(ds.validate_rubric(moved), frozen['points'])
            (relocated / 'runtime/three_scoring.py').write_text('# altered runtime\n')
            with self.assertRaises(s.ScoringError) as caught:
                ds.validate_rubric(moved)
        self.assertEqual(caught.exception.code, 'frozen_rubric_inputs_changed')
        self.assertNotIn(str(self.root), s.canonical(frozen['input_binding']))

    def test_legacy_freeze_without_basis_field_remains_compatible(self):
        draft = deepcopy(self.draft_value); draft['development_run_ids'] = ['synthetic-development']
        s.atomic_write(self.draft, draft)
        frozen = ds.freeze_rubric(self.asset, self.draft, self.output)
        frozen.pop('freeze_basis')
        frozen.pop('freeze_sha256')
        frozen['freeze_sha256'] = s.obj_hash(frozen)
        self.assertEqual(ds.validate_rubric({**self.asset, 'rubric': frozen}), frozen['points'])

    def test_cli_pre_run_needs_no_development_output_and_makes_no_trial_claim(self):
        out = io.StringIO()
        with patch.object(cli, 'load_assets', return_value={self.asset['case_id']: self.asset}), \
                patch.object(cli, 'verified_development_run', side_effect=AssertionError('must not invent a run')), \
                redirect_stdout(out):
            self.assertEqual(cli.main(['--case-id', self.asset['case_id'], '--basis', 'pre_run_authoring',
                '--draft', str(self.draft), '--output', str(self.output)]), 0)
        self.assertEqual(s.read(self.output)['development_run_ids'], [])
        self.assertIn('"paid_model_calls": 0', out.getvalue())

    def test_cli_default_requires_development_and_pre_run_rejects_ambiguous_mixing(self):
        common = ['--case-id', self.asset['case_id'], '--draft', str(self.draft)]
        for arguments in (common, common + ['--basis', 'pre_run_authoring', '--development-out', '/unused']):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                cli.main(arguments)
            self.assertEqual(caught.exception.code, 2)

    def test_grading_policy_enters_request_without_authorship_or_approval_metadata(self):
        policy = {'alternative_paths': ['按锚点接受有依据的替代路线'],
                  'opportunity': {'pretrial_actions': 20}, 'fixed_denominator': 8}
        draft = deepcopy(self.draft_value)
        draft['grading_policy'] = policy
        draft['private_author_note'] = 'AUTHOR_NOTE_MUST_NOT_ENTER_SCORING'
        s.atomic_write(self.draft, draft)
        frozen = self.freeze()
        case = self.root / 'run'
        court = case / 'court_attempts/attempt-001'
        court.mkdir(parents=True)
        events = [{'event_id': 'E1', 'stage_id': 'pretrial_workspace', 'kind': 'speak',
                   'actor': 'R_DEFENSE', 'text': '合成庭前行动'}]
        s.atomic_write(court / 'summary.json', {'case_id': self.asset['case_id'],
            'current_stage': 'ended', 'mode': 'live', 'status': 'completed_development_run'})
        s.atomic_write(court / 'author_events.jsonl', ''.join(s.canonical(e) + '\n' for e in events))
        s.atomic_write(court / 'artifacts/manifest.json', {'version': 'workflow-artifacts-v3', 'artifacts': []})
        s.atomic_write(case / 'private_model_io.jsonl', '')
        s.atomic_write(case / 'action_attempts.jsonl', '')
        info = {'procedure': {'stages': [{'stage_id': 'pretrial_workspace'}]},
                'permissions': {'allowed': 'synthetic'}}
        request = ds.prepare_request({**self.asset, 'rubric': frozen}, info, case, court)
        self.assertEqual(request['context']['rubric_grading_policy'], policy)
        self.assertEqual(request['context']['policy']['score_values'], [0, 1, 2])
        self.assertTrue(request['context']['policy']['fixed_denominator_no_na'])
        text = s.canonical(request)
        self.assertNotIn('authoring_provenance', text)
        self.assertNotIn('AUTHOR_NOTE_MUST_NOT_ENTER_SCORING', text)
        self.assertNotIn(draft['authoring_provenance']['authored_by'], text)
        self.assertNotIn('"approved"', text)
        request['context']['rubric_grading_policy']['alternative_paths'].append('request-only')
        self.assertEqual(len(frozen['grading_policy']['alternative_paths']), 1)

    def test_grading_policy_accepts_only_legal_json_policy_structures(self):
        for policy in ({'nested': [True, None, 2, 0.5, 'text']}, ['rule'], 'rule'):
            with self.subTest(policy=policy):
                self.assertEqual(ds._rubric_grading_policy({'grading_policy': policy}), policy)
        for policy in (True, 12, 1.5, {'nonfinite': float('nan')}, {1: 'non-string key'},
                       {'tuple': ('not', 'json')}, {'object': object()}):
            with self.subTest(policy=str(policy)), self.assertRaises(s.ScoringError) as caught:
                ds._rubric_grading_policy({'grading_policy': policy})
            self.assertEqual(caught.exception.code, 'invalid_rubric_grading_policy')


if __name__ == '__main__':
    unittest.main()
