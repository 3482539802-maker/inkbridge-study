"""Run offline unittest modules with their existing isolated Python environments.

No device access, model downloads, service restarts, installation or UI actions.
Standalone transport mains, CJS, PowerShell and Harmony build remain separate.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODEL_MODULES = {'verify_unimernet_probe', 'verify_texteller_probe'}


def worker(group):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for path in sorted((ROOT/'scripts').glob('verify_*.py')):
        if path.stem == 'verify_workspace':
            continue
        if (path.stem in MODEL_MODULES) != (group == 'recognition'):
            continue
        suite.addTests(loader.loadTestsFromName(path.stem))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--group', choices=('all', 'core', 'recognition'), default='all')
    parser.add_argument('--recognition-python', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', choices=('core', 'recognition'), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker)
    groups = ('core', 'recognition') if args.group == 'all' else (args.group,)
    model_python = args.recognition_python or ROOT/'tools/unimernet-venv'/(
        'Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    if 'recognition' in groups and not model_python.is_file():
        parser.error('Isolated recognition Python missing; provide --recognition-python or use --group core. No packages will be installed.')
    report_path = (args.output or ROOT/'output/checks'/f'workspace-{time.time_ns()}.json').resolve()
    if report_path.exists():
        parser.error('Use a new report path; existing reports are immutable.')
    report_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for group in groups:
        python = Path(sys.executable) if group == 'core' else model_python.resolve()
        log = report_path.with_name(report_path.stem+f'-{group}.log')
        start = time.monotonic()
        with log.open('x', encoding='utf-8') as stream:
            result = subprocess.run([str(python), '-X', 'utf8', str(Path(__file__).resolve()),
                '--worker', group], cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
        lines = log.read_text(encoding='utf-8', errors='replace').splitlines()
        summary = [line for line in lines if line.startswith(('Ran ', 'OK', 'FAILED', 'ERROR:', 'FAIL:'))]
        row = {'group': group, 'python': str(python), 'exit_code': result.returncode,
               'seconds': round(time.monotonic()-start, 3), 'log': str(log), 'summary': summary}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    passed = all(row['exit_code'] == 0 for row in rows)
    report_path.write_text(json.dumps({'passed': passed, 'groups': rows,
        'scope': 'offline_unittest_only', 'tablet_verified': False, 'tablet_ui_actions': []},
        ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'passed': passed, 'report': str(report_path)}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
