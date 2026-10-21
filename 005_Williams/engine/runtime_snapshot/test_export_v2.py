"""Delivery export regressions. Temporary fixtures only; no API or live claims."""
from pathlib import Path
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from .export import export_case, export_project
from .store import EvidenceError


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'project'
        self.out = Path(self.tmp.name) / 'deliveries'
        self.root.mkdir()
        for name, content in {'README.md': '工程说明', 'config.json': '{}', 'requirements.txt': '', 'setup.sh': '#!/bin/sh\nexit 0\n', 'run_005.sh': '#!/bin/sh\nexit 0\n', 'run_006.sh': '#!/bin/sh\nexit 0\n', 'shared_engine/cli.py': '# fixture', 'shared_engine/engine.py': '# fixture', 'tools/helper.py': '# fixture'}.items():
            self.write(name, content)
        for case in ('005_Williams', '006_Wilson'):
            self.write(case + '/world/manifest.json', '{}')
            self.write(case + '/world/facts.jsonl', '')
            self.write(case + '/legal_corpus/corpus.jsonl', '')
            self.write(case + '/rubric/rubric.json', '{}')
            self.write(case + '/rubric/preregistration.json', '{}')
            self.write(case + '/engine/run.py', '# entry')
            self.write(case + '/README.md', '该题原说明，可能过时')
            self.write(case + '/进度.md', '未运行')
            self.write(case + '/usage_ledger.jsonl', '')
        self.write('005_Williams/runs/unfinished/state.json', '{"status":"paused"}')
        self.write('006_Wilson/runs/other_result/state.json', '{"status":"completed"}')
        self.write('006_Wilson/rubric/freeze.json', '{"status":"example"}')
        self.write('.venv/credential.txt', 'not included')
        self.write('005_Williams/background.json', '{"pid":123}')

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def test_partial_is_labelled_and_preserves_shared_runtime(self):
        result = export_case(self.root / '005_Williams', self.out, allow_partial=True)
        self.assertEqual(result['status'], 'partial_not_final')
        with zipfile.ZipFile(result['path']) as archive:
            names = set(archive.namelist())
            prefix = 'legal_agent_3round/'
            self.assertIn(prefix + 'shared_engine/cli.py', names)
            self.assertIn(prefix + 'config.json', names)
            self.assertIn(prefix + 'setup.sh', names)
            self.assertIn(prefix + '005_Williams/runs/unfinished/state.json', names)
            self.assertIn(prefix + '006_Wilson/world/manifest.json', names)
            self.assertNotIn(prefix + '006_Wilson/runs/other_result/state.json', names)
            self.assertNotIn(prefix + '006_Wilson/rubric/freeze.json', names)
            self.assertNotIn(prefix + '.venv/credential.txt', names)
            self.assertNotIn(prefix + '005_Williams/background.json', names)
            self.assertIn('依赖模板', archive.read(prefix + '006_Wilson/README.md').decode())
            manifest = json.loads(archive.read(prefix + 'DELIVERY_MANIFEST.json'))
            self.assertEqual(manifest['selected_cases'], ['005_Williams'])
            self.assertEqual(manifest['template_only_cases'], ['006_Wilson'])
            self.assertIn('PARTIAL', archive.read(prefix + 'DELIVERY.md').decode())
            for name, entry in manifest['files'].items():
                data = archive.read(prefix + name)
                self.assertEqual(len(data), entry['bytes'])
                self.assertEqual(hashlib.sha256(data).hexdigest(), entry['sha256'])
        self.assertEqual((self.root / '006_Wilson/README.md').read_text(), '该题原说明，可能过时')

    def test_project_partial_contains_both_selected_cases(self):
        result = export_project(self.root, self.out, allow_partial=True)
        self.assertEqual(result['selected_cases'], ['005_Williams', '006_Wilson'])
        self.assertEqual(result['template_only_cases'], [])
        with zipfile.ZipFile(result['path']) as archive:
            self.assertIn('legal_agent_3round/006_Wilson/runs/other_result/state.json', archive.namelist())

    def test_default_rejects_unvalidated_results(self):
        with self.assertRaises(EvidenceError):
            export_case(self.root / '005_Williams', self.out)
        self.assertFalse(list(self.out.glob('*.zip')))

    def test_export_rejects_symlink_even_inside_selected_tree(self):
        target = Path(self.tmp.name) / 'outside.txt'
        target.write_text('private')
        (self.root / 'shared_engine' / 'linked.py').symlink_to(target)
        with self.assertRaisesRegex(EvidenceError, '符号链接'):
            export_case(self.root / '005_Williams', self.out, allow_partial=True)

    def test_actual_environment_key_is_never_packaged(self):
        secret = 'test_actual_key_' + 'x' * 32
        self.write('005_Williams/pipeline.log', 'error contains ' + secret)
        with patch.dict(os.environ, {'PACKY_API_KEY': secret}):
            with self.assertRaisesRegex(EvidenceError, '密钥') as ex:
                export_case(self.root / '005_Williams', self.out, allow_partial=True)
        self.assertNotIn(secret, str(ex.exception))
        self.assertFalse(list(self.out.glob('*.zip')))

    def test_key_shaped_literal_is_rejected_without_environment(self):
        secret = 'sk-' + 'a' * 32
        self.write('005_Williams/pipeline.log', secret)
        with self.assertRaisesRegex(EvidenceError, '密钥'):
            export_project(self.root, self.out, allow_partial=True)


if __name__ == '__main__':
    unittest.main()
