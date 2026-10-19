"""Read-only status and explicit safe-stop selection regressions; no model calls."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('three_status_for_tests', ROOT / 'status.py')
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


class StatusTests(unittest.TestCase):
    def test_help_and_empty_workspace_do_not_crash_or_modify(self):
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'status.py'), '--help'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--stop', result.stdout)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(status, 'ROOT', root), self.assertRaises(SystemExit) as error:
                status.main([])
            self.assertIn('这个目录尚未启动实验', str(error.exception))
            self.assertEqual(list(root.iterdir()), [])

    def test_case_selection_zero_score_and_explicit_safe_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for cid in ('005', '006'):
                out = root / 'runs' / ('formal-all-' + cid)
                out.mkdir(parents=True)
                (out / 'matrix_manifest.json').write_text(json.dumps({'case_ids': ['CA-PILOT-' + cid]}))
                (out / 'results.csv').write_text('candidate,case_id,status,numerator,denominator,error_code\ngpt,CA-PILOT-' + cid + ',complete,0,100,\n')
            with patch.object(status, 'ROOT', root), patch('sys.stdout', new_callable=io.StringIO) as captured:
                self.assertEqual(status.main(['--case-id', '006']), 0)
                self.assertIn('0/100', captured.getvalue())
                self.assertNotIn('CA-PILOT-005', captured.getvalue())
            self.assertEqual(list(root.rglob('STOP_REQUESTED')), [])
            with patch.object(status, 'ROOT', root), patch('sys.stdout', new_callable=io.StringIO):
                status.main(['--case-id', '006', '--stop', '--tail', '0'])
            self.assertEqual(len(list(root.rglob('STOP_REQUESTED'))), 1)


if __name__ == '__main__':
    unittest.main()
