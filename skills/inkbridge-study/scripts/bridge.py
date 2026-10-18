"""Locate an InkBridge checkout across workspaces; no implicit device actions."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler


def config_path():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'inkbridge' / 'config.json'


def validate_root(value):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError('Toolbox root must be a nonempty filesystem path.')
    root = Path(value).expanduser().resolve()
    required = ('AGENTS.md', 'docs/WORKFLOW.md', 'scripts/course_runtime.py', 'start-study.ps1')
    if not all((root / name).is_file() for name in required):
        raise ValueError('Not an InkBridge checkout: required runtime/docs files are missing.')
    return root


def configure(root, *, replace=False, keep_existing=False):
    root=validate_root(root)
    cfg=config_path()
    if cfg.is_file() and not replace:
        previous=validate_root(json.loads(cfg.read_text(encoding='utf-8-sig'))['root'])
        if previous!=root:
            if keep_existing:
                return {'configured':False,'preserved_existing':True,'root':str(previous)}
            raise ValueError('Another toolbox is already configured. Use --replace only to deliberately switch it.')
        if previous==root:
            return {'configured':True,'root':str(root)}
    cfg.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=cfg.parent,
                                         prefix='config-',suffix='.tmp',delete=False) as stream:
            temporary=Path(stream.name)
            json.dump({'root':str(root)},stream,ensure_ascii=False,indent=2)
            stream.write('\n')
        temporary.replace(cfg)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
    return {'configured':True,'root':str(root)}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Local status must not redirect.')


def status(root):
    settings=json.loads((root/'output/learning/runtime.json').read_text(encoding='utf-8-sig'))
    port=settings.get('port',18771)
    if type(port) is not int or not 1<=port<=65535:
        raise ValueError('Invalid local runtime port.')
    # Bypass proxies and redirects; this endpoint is exclusively loopback.
    opener=build_opener(ProxyHandler({}),NoRedirect())
    with opener.open(f'http://127.0.0.1:{port}/status.json',timeout=5) as response:
        payload=response.read(1024*1024+1)
    if len(payload)>1024*1024:raise ValueError('Local status exceeds size limit.')
    value=json.loads(payload)
    if not isinstance(value,dict):raise ValueError('Malformed runtime status.')
    stamp=value.get('updated_at_pc')
    if type(stamp) not in (int,float) or not -5<=time.time()-stamp<=45:
        raise ValueError('Runtime status is stale or its clock is invalid; inspect existing services before any action.')
    capture=value.get('capture',{})
    if not isinstance(capture,dict):raise ValueError('Malformed capture status.')
    if settings.get('supervise_capture',True) and not capture:
        raise ValueError('Capture health is missing; recording state is unknown.')
    if capture:
        checked=capture.get('checked_at_pc')
        if (capture.get('attention_required') or type(checked) not in (int,float) or
            not -5<=time.time()-checked<=45 or capture.get('pipeline_status_fresh') is not True):
            raise ValueError('Capture health is stale, unavailable or requires attention; do not infer recording is idle.')
    return value


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
    configure_parser = commands.add_parser('configure')
    configure_parser.add_argument('--root', required=True, type=Path)
    policy=configure_parser.add_mutually_exclusive_group()
    policy.add_argument('--replace',action='store_true',help='Deliberately switch an already configured toolbox')
    policy.add_argument('--keep-existing',action='store_true',help='Preserve a valid existing toolbox during skill updates')
    args = parser.parse_args()
    try:
        if args.command == 'configure':
            print(json.dumps(configure(args.root,replace=args.replace,
                                       keep_existing=args.keep_existing),ensure_ascii=False))
            return 0
        root, source = locate()
        if args.command == 'locate':
            print(json.dumps({'root': str(root), 'source': source}, ensure_ascii=False))
            return 0
        print(json.dumps(status(root),ensure_ascii=False,indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
