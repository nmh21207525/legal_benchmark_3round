"""Split-server launch and automatic scoring integration, strictly offline.

Only the real 005/006 assets, engine, frozen rubrics, journals and exporter are
used. Role replies and scores are synthetic fixtures, never legal assessment or
live model results. Formal-mode files exist only in TemporaryDirectory and are
removed after the tests; network sockets are prohibited throughout execution.
"""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'runtime')]
spec = importlib.util.spec_from_file_location('split_formal_runner_for_tests', ROOT / 'run.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
import scoring as s
import three_scoring as ds
from reliability import ReliableBackend
from three_assets import ThreeRoundCaseData, load_assets
from three_demo import create_backend as create_synthetic_role_backend

COUNTS = {'CA-PILOT-005': 46, 'CA-PILOT-006': 48}
NO_NETWORK = 'SYNTHETIC_TEST_ATTEMPTED_NETWORK'


class SyntheticWholeRubricScorer:
    """Return varied fixture grades for every supplied item, not model grades."""
    def __init__(self, recorded):
        self.recorded = recorded
        self.calls = []
        self.stats = {'paid_api_calls': 0, 'synthetic_test_only': True}

    def decide(self, **request):
        self.recorded.append(deepcopy(request))
        self.calls.append({'status': 'completed', 'model': 'SYNTHETIC_OFFLINE_SCORER',
                           'transport': 'offline_fixture'})
        context = request['context']
        eid = next(event['event_id'] for event in context['events']
                   if event.get('actor') == 'R_DEFENSE' and event.get('kind') == 'tool_call')
        aid = next(artifact['artifact_id'] for artifact in context['verified_artifacts']
                   if artifact.get('actor') == 'R_DEFENSE' and artifact.get('is_latest_version'))
        return {'protocol': ds.PROTOCOL, 'case_id': context['case_id'], 'points': [
            {'point_id': point['point_id'], 'score': index % 3,
             'reason': '仅用于零网络集成测试的合成分数；不代表律师模型表现或法律判断。',
             'event_ids': [eid], 'artifact_ids': [aid], 'observation_limit': ''}
            for index, point in enumerate(context['points'])]}

    def close(self):
        pass


class SplitFormalLaunchTests(unittest.TestCase):
    def wrapper(self, case_number, *arguments):
        # The shell wrapper starts fresh Python processes. sitecustomize makes
        # the same network prohibition apply there, including interpreter checks.
        with tempfile.TemporaryDirectory(prefix='legal-launch-no-network-') as directory:
            guard = Path(directory) / 'sitecustomize.py'
            guard.write_text(
                'import socket\n'
                'def forbidden(*args, **kwargs):\n'
                f'    raise AssertionError({NO_NETWORK!r})\n'
                'socket.socket.connect = forbidden\n'
                'socket.socket.connect_ex = forbidden\n'
                'socket.create_connection = forbidden\n', encoding='utf-8')
            env = dict(os.environ)
            for key in ('ARK_API_KEY', 'PACKY_API_KEY', 'PACKY_DEEPSEEK_API_KEY', 'PACKY_OPUS_API_KEY'):
                env.pop(key, None)
            env['COURT_PYTHON'] = sys.executable
            env['PYTHONPATH'] = str(guard.parent)
            return subprocess.run(['bash', str(ROOT / f'run_{case_number}.sh'), *arguments],
                                  cwd=ROOT, env=env, text=True, capture_output=True, timeout=60)

    def test_both_wrappers_validate_one_formal_case_four_models_and_distinct_outputs(self):
        outputs = []
        for case_number in ('005', '006'):
            with self.subTest(case=case_number):
                result = self.wrapper(case_number, '--validate-only')
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report['status'], 'validated')
                self.assertEqual(report['mode'], 'formal')
                self.assertEqual(report['case_ids'], ['CA-PILOT-' + case_number])
                self.assertEqual(set(report['candidate_models']), {'seed', 'deepseek', 'opus', 'gpt'})
                self.assertEqual((report['planned_courts'], report['rounds']), (4, 3))
                self.assertEqual(report['rubric_status'], {'CA-PILOT-' + case_number: 'frozen'})
                self.assertTrue(report['pretrial'])
                self.assertTrue(report['tool_calls'])
                self.assertEqual(report['paid_model_calls'], 0)
                self.assertFalse(report['live_connectivity_tested'])
                output = ROOT / 'runs' / ('formal-all-' + case_number)
                self.assertEqual(report['out'], str(output))
                outputs.append(report['out'])
        self.assertNotEqual(*outputs)

    def test_wrappers_reject_case_and_mode_override_before_interpreter(self):
        for case_number in ('005', '006'):
            for arguments in (('--case-id', 'CA-PILOT-005'), ('--case-id=CA-PILOT-006',),
                              ('--mode', 'development'), ('--mode=development',)):
                with self.subTest(case=case_number, arguments=arguments):
                    result = self.wrapper(case_number, *arguments)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn('固定只运行' + case_number, result.stderr)
                    self.assertEqual(result.stdout, '')

    def test_experiment_budgets_and_model_mapping_cannot_drift_between_servers(self):
        fixed = {'max_lawyer_actions': 150, 'max_model_calls': 360,
                 'max_output_tokens': 16384, 'timeout_seconds': 300,
                 'default_actions_per_role_per_stage': 3, 'max_input_chars': 700000}
        original = app.read(ROOT / 'config.example.json')
        mutations = [(key, value + 1) for key, value in fixed.items()]
        mutations.extend([
            ('actions_per_role_per_stage', {'r1_motion': 99}),
            ('defense_models', {**original['defense_models'], 'gpt': 'gpt-6-sol'}),
        ])
        with tempfile.TemporaryDirectory(prefix='legal-fixed-config-') as directory:
            path = Path(directory) / 'config.json'
            for key, value in mutations:
                altered = deepcopy(original)
                altered[key] = value
                path.write_text(json.dumps(altered), encoding='utf-8')
                args = app.arguments(['--config', str(path)])
                with self.subTest(setting=key), self.assertRaises(app.RunError) as caught:
                    app.load_config(args)
                self.assertEqual(caught.exception.code, 'fixed_experiment_setting_changed')

    def test_default_config_is_workspace_local_without_probing_old_project(self):
        original_is_file = Path.is_file
        with tempfile.TemporaryDirectory(prefix='legal-config-portability-') as directory:
            workspace = Path(directory) / 'legal_agent_3round'
            workspace.mkdir()
            expected = workspace / 'config.example.json'
            expected.write_bytes((ROOT / 'config.example.json').read_bytes())
            def no_old_project_probe(path):
                if str(path).startswith('/home/nmh/legal_agent_codex_030/'):
                    raise AssertionError('old_project_must_not_be_an_implicit_dependency')
                return original_is_file(path)
            args = app.arguments([])
            with patch.object(app, 'ROOT', workspace), patch.object(Path, 'is_file', no_old_project_probe):
                config = app.load_config(args)
            self.assertEqual(args.config, expected)
            self.assertEqual(args.project_root, workspace)
            self.assertEqual(config['defense_models'], app.backends.DEFAULT_MODELS)

    def test_each_case_runs_real_three_round_flow_and_full_rubric_autoscores_then_reuses(self):
        # main() is deliberately exercised in formal mode to cover the actual
        # automatic scoring branch. Only its external model interfaces, key
        # acquisition and preflight are replaced. All outputs are disposable.
        rubric_hashes = {cid: s.sha(ROOT / 'rubrics' / (cid + '.json')) for cid in COUNTS}
        assets = load_assets(ROOT / 'data')
        cases = {cid: ThreeRoundCaseData(asset['case_path']) for cid, asset in assets.items()}
        court_backends, scorer_requests = [], []

        def fake_court_factory(config, keys, candidate, project_root=None,
                               claude_executable=None, *, attempt_ledger=None, **kwargs):
            self.assertIsNotNone(attempt_ledger)
            ledger = Path(attempt_ledger)
            cid = ledger.parent.name
            inner = create_synthetic_role_backend(config, candidate, cases[cid])
            court_backends.append((cid, inner))
            reliable = ReliableBackend(inner, ledger, app.backends.bootstrap().COMPARISON_POLICY)
            reliable.is_offline_fixture = True
            reliable.replay_cached_decision = inner.decide
            return reliable

        def fake_scorer_factory(config, keys, *, model=None, **kwargs):
            self.assertEqual(model, app.backends.SCORER_MODEL)
            return SyntheticWholeRubricScorer(scorer_requests)

        with tempfile.TemporaryDirectory(prefix='SYNTHETIC-ONLY-split-formal-') as directory, \
                patch('socket.socket.connect', side_effect=AssertionError(NO_NETWORK)), \
                patch('socket.socket.connect_ex', side_effect=AssertionError(NO_NETWORK)), \
                patch('socket.create_connection', side_effect=AssertionError(NO_NETWORK)), \
                patch.object(app.signal, 'signal'), redirect_stdout(io.StringIO()) as log, \
                redirect_stderr(io.StringIO()) as error_log:
            base = Path(directory)
            (base / 'SYNTHETIC_TEST_ONLY.txt').write_text(
                'Synthetic fixtures only. No paid models or legal scores. Deleted at test exit.\n')
            for cid, count in COUNTS.items():
                with self.subTest(case=cid):
                    out = base / cid
                    arguments = ['--mode', 'formal', '--case-id', cid, '--candidate', 'gpt', '--out', str(out)]
                    with patch.object(app, 'collect_keys', return_value={}), \
                            patch.object(app, 'preflight', return_value=None) as preflight, \
                            patch.object(app.backends, 'create_backend', side_effect=fake_court_factory), \
                            patch.object(app.backends, 'shared_backend', side_effect=fake_scorer_factory):
                        code = app.main(arguments)
                    self.assertEqual(code, 0, log.getvalue() + error_log.getvalue())
                    preflight.assert_called_once()
                    with (out / 'results.csv').open(encoding='utf-8-sig', newline='') as stream:
                        results = list(csv.DictReader(stream))
                    self.assertEqual(len(results), 1)
                    row = results[0]
                    self.assertEqual((row['case_id'], row['candidate'], row['status']), (cid, 'gpt', 'complete'))
                    self.assertEqual(int(row['denominator']), 2 * count)
                    self.assertEqual(int(row['numerator']), sum(index % 3 for index in range(count)))
                    self.assertEqual({p.name for p in (out / 'courts/gpt').iterdir()}, {cid})
                    attempt = out / row['court_dir']
                    summary = s.read(attempt / 'summary.json')
                    self.assertEqual(summary['simulation_round_count'], 3)
                    self.assertEqual(summary['current_stage'], 'ended')
                    self.assertTrue({'r1_motion', 'r2_examination', 'r3_instructions', 'r3_verdict'}
                                    <= set(summary['completed_stages']))
                    self.assertTrue(summary['verdict'])
                    score_dir = out / 'courts/gpt' / cid / 'direct_score'
                    score = s.read(score_dir / 'score.json')
                    rubric = s.read(ROOT / 'rubrics' / (cid + '.json'))
                    self.assertEqual(len(score['points']), count)
                    self.assertEqual({p['point_id'] for p in score['points']},
                                     {p['point_id'] for p in rubric['points']})
                    self.assertEqual(score['rubric_freeze_sha256'], rubric['freeze_sha256'])
                    self.assertFalse(score['expert_validated'])
                    self.assertTrue(all('合成分数' in p['reason'] for p in score['points']))
                    request = scorer_requests[-1]
                    self.assertEqual(request['context']['case_id'], cid)
                    self.assertEqual(len(request['context']['points']), count)
                    self.assertEqual(request['context']['rubric_grading_policy'], rubric['grading_policy'])
                    self.assertFalse(request['context']['policy']['previous_scores_loaded'])
                    self.assertFalse(any(e['actor'] == 'R_JURY' for e in request['context']['events']))
                    stages = {e['stage_id'] for e in request['context']['events']}
                    self.assertTrue({'pretrial_workspace', 'r1_motion', 'r2_examination',
                                     'r3_clarification', 'r3_instructions'} <= stages)
                    self.assertTrue(request['context']['verified_artifacts'])
                    status = s.read(out / 'delivery_status.json')
                    self.assertEqual(status['status'], 'complete')
                    self.assertEqual(status['complete_courts_and_scores'], 1)
                    self.assertEqual({p['case_id'] for p in status['cases']}, {cid})
                    self.assertTrue(status['teacher_packages_created'])
                    self.assertTrue((out / 'index.html').is_file())
                    self.assertTrue((out / 'report.html').is_file())
                    self.assertEqual({p.name for p in (out / '交付材料').iterdir() if p.is_dir()},
                                     {'第' + cid.rsplit('-', 1)[1] + '题'})
                    for package in status['packages']:
                        path = out / package['file']
                        self.assertEqual(s.sha(path), package['sha256'])
                    immutable_before = {p: s.sha(p) for p in (out / 'courts').rglob('*') if p.is_file()}
                    with patch.object(app, 'collect_keys', side_effect=AssertionError('resume_requested_keys')), \
                            patch.object(app, 'preflight', side_effect=AssertionError('resume_requested_preflight')), \
                            patch.object(app.backends, 'create_backend', side_effect=AssertionError('resume_requested_court')), \
                            patch.object(app.backends, 'shared_backend', side_effect=AssertionError('resume_requested_scorer')):
                        self.assertEqual(app.main(arguments + ['--resume']), 0, log.getvalue() + error_log.getvalue())
                    self.assertEqual({p: s.sha(p) for p in immutable_before}, immutable_before)
        self.assertEqual([cid for cid, _ in court_backends], list(COUNTS))
        self.assertTrue(all(inner.calls for _, inner in court_backends))
        self.assertEqual([request['context']['case_id'] for request in scorer_requests], list(COUNTS))
        self.assertEqual({cid: s.sha(ROOT / 'rubrics' / (cid + '.json')) for cid in COUNTS}, rubric_hashes)


if __name__ == '__main__':
    unittest.main()
