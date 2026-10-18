"""Locate an InkBridge checkout across workspaces; no implicit device actions."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def config_path():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'inkbridge' / 'config.json'


def validate_root(value):
    root = Path(value).expanduser().resolve()
    required = ('AGENTS.md', 'docs/WORKFLOW.md', 'scripts/course_runtime.py', 'start-study.ps1')
    if not all((root / name).is_file() for name in required):
        raise ValueError('Not an InkBridge checkout: required runtime/docs files are missing.')
    return root


def locate():
    if os.environ.get('INKBRIDGE_HOME'):
        return validate_root(os.environ['INKBRIDGE_HOME']), 'environment'
    cfg = config_path()
    if cfg.is_file():
        return validate_root(json.loads(cfg.read_text(encoding='utf-8-sig'))['root']), 'local_config'
    checkout = Path(__file__).resolve().parents[3]
    try:
        return validate_root(checkout), 'skill_checkout'
    except ValueError:
        raise ValueError('InkBridge is not configured. Run bridge.py configure --root <checkout>.') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('locate')
    commands.add_parser('status')
    configure = commands.add_parser('configure')
    configure.add_argument('--root', required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'configure':
            root = validate_root(args.root)
            cfg = config_path()
            cfg.parent.mkdir(parents=True, exist_ok=True)
            temporary = cfg.with_suffix('.tmp')
            temporary.write_text(json.dumps({'root': str(root)}, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
            temporary.replace(cfg)
            print(json.dumps({'configured': True, 'root': str(root)}, ensure_ascii=False))
            return 0
        root, source = locate()
        if args.command == 'locate':
            print(json.dumps({'root': str(root), 'source': source}, ensure_ascii=False))
            return 0
        pwsh = shutil.which('pwsh')
        if not pwsh:
            raise ValueError('PowerShell 7 (pwsh) is required for the Windows runtime status check.')
        return subprocess.run([pwsh, '-NoProfile', '-File', str(root/'start-study.ps1'), '-Status'], cwd=root).returncode
    except (ValueError, KeyError, OSError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
