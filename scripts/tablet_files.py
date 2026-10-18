"""Read tablet public documents directly over HDC, without opening InkBridge.

Tested on TGR-W10/API24: file://docs/storage/Users/currentUser maps to
/storage/media/100/local/files/Docs. Release application sandboxes remain private.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import tempfile
import time
from urllib.parse import unquote, urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]


def public_path(value, user=100):
    if user < 0:
        raise ValueError('User ID must be nonnegative')
    if any(c in value for c in ('\x00', '\r', '\n')):
        raise ValueError('Path contains control characters')
    base = f'/storage/media/{user}/local/files/Docs'
    if value.startswith('file:'):
        uri = urlsplit(value)
        prefix = '/storage/Users/currentUser'
        path = unquote(uri.path)
        if uri.scheme != 'file' or uri.netloc != 'docs' or uri.query or uri.fragment:
            raise ValueError('Expected file://docs/storage/Users/currentUser/... URI')
        if path != prefix and not path.startswith(prefix + '/'):
            raise ValueError('Only currentUser document URIs are supported')
        path = base + path[len(prefix):]
    elif value.startswith('/'):
        path = value
    else:
        path = base + '/' + value
    parts = path.split('/')
    if '\\' in path or '..' in parts or '.' in parts:
        raise ValueError('Use a normal document path without traversal')
    path = str(PurePosixPath(path))
    if path != base and not path.startswith(base + '/'):
        raise ValueError('Only the tablet public Docs tree is handled by this tool')
    return path


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


class Hdc:
    def __init__(self, executable, target=None):
        self.prefix = [str(executable)]
        if not target:
            listing = self.run(['list', 'targets', '-v'])
            connected = [parts[0] for line in listing.splitlines()
                         if len(parts := line.split()) >= 3 and parts[1] in ('USB', 'TCP')
                         and parts[2] == 'Connected']
            if len(connected) != 1:
                raise ValueError('Expected one connected device; specify --target')
            target = connected[0]
        self.target = target
        self.prefix += ['-t', target]

    def run(self, args, timeout=30):
        result = subprocess.run(self.prefix + args, capture_output=True, timeout=timeout)
        output = (result.stdout + result.stderr).decode('utf-8', errors='replace')
        # HDC may return exit code 0 even when a transfer fails.
        if result.returncode or '[Fail]' in output or output.startswith('ls:'):
            raise RuntimeError(output.strip() or f'HDC exit code {result.returncode}')
        return output

    def shell(self, words):
        return self.run(['shell', ' '.join(shlex.quote(word) for word in words)])

    def list(self, directory):
        names = self.shell(['ls', '-1', directory]).splitlines()
        # Do not interpret a listing as a shell command or expand names as globs.
        return [name for name in names if name and name not in ('.', '..')]

    def pull(self, remote, local, overwrite=False, expected_hash=None):
        local = Path(local).resolve()
        if local.exists() and not overwrite:
            raise FileExistsError(f'Local destination already exists: {local}; use --overwrite explicitly')
        local.parent.mkdir(parents=True, exist_ok=True)
        temporary = local.with_name(local.name + '.' + uuid.uuid4().hex + '.partial')
        started = time.monotonic()
        try:
            output = self.run(['file', 'recv', remote, str(temporary)], timeout=60)
            if 'FileTransfer finish' not in output or not temporary.is_file():
                raise RuntimeError('HDC did not confirm receiving a file')
            actual_hash = digest(temporary)
            if expected_hash and actual_hash.lower() != expected_hash.lower():
                raise ValueError('Received file SHA-256 differs from expected hash')
            # Recheck destination after transfer; avoid replacing another process's file.
            if overwrite:
                temporary.replace(local)
            else:
                with temporary.open('rb') as source, local.open('xb') as destination:
                    for chunk in iter(lambda: source.read(1024 * 1024), b''):
                        destination.write(chunk)
            return {'type': 'tablet_direct_file', 'target': self.target,
                    'remote_path': remote, 'local_path': str(local), 'bytes': local.stat().st_size,
                    'sha256': actual_hash, 'duration_ms': round((time.monotonic() - started) * 1000),
                    'received_at_pc': datetime.now(timezone.utc).isoformat(),
                    'transport': 'hdc-public-docs', 'inkbridge_required': False,
                    'limits': ['Read access to this public file does not grant access to private Notes databases.',
                               'A public export is a saved snapshot, not automatically updated while writing.']}
        finally:
            temporary.unlink(missing_ok=True)

    def push(self, local, remote):
        """Send an explicitly chosen file to Docs without launching an app.

        Native HDC file send can work where shell mkdir is denied. Never infer
        success from shell permissions, nor equate a public file with Notes import.
        Existing filenames are rejected; always receive back to verify bytes.
        """
        local = Path(local).resolve(strict=True)
        if not local.is_file() or local.stat().st_size > 512 * 1024 * 1024:
            raise ValueError('Expected a regular file no larger than 512 MiB')
        remote_path = PurePosixPath(remote)
        if remote_path.name in self.list(str(remote_path.parent)):
            raise FileExistsError('Tablet destination already exists; choose a new test filename')
        original_hash = digest(local)
        started = time.monotonic()
        output = self.run(['file', 'send', str(local), remote], timeout=120)
        if 'FileTransfer finish' not in output:
            raise RuntimeError('HDC did not confirm sending the file')
        with tempfile.TemporaryDirectory(prefix='inkbridge-send-') as temporary:
            received = self.pull(remote, Path(temporary)/'roundtrip.bin', expected_hash=original_hash)
        if digest(local) != original_hash:
            raise ValueError('Computer source changed during transfer')
        return {'type': 'tablet_public_file_send', 'target': self.target, 'local_path': str(local),
                'remote_path': remote, 'bytes': received['bytes'], 'sha256': original_hash,
                'roundtrip_verified': True, 'transport': 'hdc-public-docs', 'inkbridge_required': False,
                'tablet_ui_actions': [], 'notes_import_verified': False,
                'duration_ms': round((time.monotonic()-started)*1000),
                'sent_at_pc': datetime.now(timezone.utc).isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hdc', type=Path, default=ROOT / 'tools/hdc/hdc.exe')
    parser.add_argument('--target')
    parser.add_argument('--user', type=int, default=100)
    commands = parser.add_subparsers(dest='command', required=True)
    listing = commands.add_parser('list', help='List one public directory; no recursive scan')
    listing.add_argument('--directory', default='file://docs/storage/Users/currentUser')
    listing.add_argument('--contains', help='Show only names containing this literal text')
    pull = commands.add_parser('pull', help='Receive a file directly, without app/file picker')
    pull.add_argument('file', help='Public document relative path, system path or docs URI')
    pull.add_argument('--output', type=Path, required=True)
    pull.add_argument('--overwrite', action='store_true')
    pull.add_argument('--sha256', help='Optional expected file hash')
    pull.add_argument('--report', type=Path)
    push = commands.add_parser('push', help='Send a file to an existing public directory; verify by reading it back')
    push.add_argument('source', type=Path)
    push.add_argument('--destination', required=True, help='New public filename or docs URI; never overwrites')
    push.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        hdc = Hdc(args.hdc, args.target)
        if args.command == 'list':
            directory = public_path(args.directory, args.user)
            names = hdc.list(directory)
            result = {'target': hdc.target, 'directory': directory,
                      'entries': [name for name in names if args.contains is None or args.contains in name],
                      'inkbridge_required': False}
        elif args.command == 'pull':
            result = hdc.pull(public_path(args.file, args.user), args.output, args.overwrite, args.sha256)
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        else:
            result = hdc.push(args.source, public_path(args.destination, args.user))
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=True, indent=2))
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
