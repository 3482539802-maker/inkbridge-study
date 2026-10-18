"""Recover one stopped device journal and replay it through the existing receiver.

Never opens an ability, modifies tablet files, or writes canonical journals directly.
The device's synced flag remains authoritative and is never changed by this tool.
"""
import argparse
import io
import json
from pathlib import Path
import socket
import subprocess

from ink_protocol import validate_session_id
from capture_clock_history import backfill_clock, load_clock
from raw_journal_audit import audit,same_bytes,signature

ROOT = Path(__file__).resolve().parents[1]


def replay(raw, meta, port):
    # Accept bytes for existing small callers, but the recovery CLI streams a
    # file and never loads a stopped day of ink into memory.
    path=raw if isinstance(raw,Path) else None
    initial=signature(path) if path else None
    with path.open('rb') if path else io.BytesIO(raw) as source:
        length=initial[2] if path else len(raw)
        if length!=meta['byte_length']:raise ValueError('Recovery raw length differs from metadata')
        def check_unchanged():
            if path and signature(path)!=initial:raise ValueError('Recovery raw changed during replay')
        return replay_stream(source,length,meta,port,check_unchanged)


def replay_stream(source,length,meta,port,check_unchanged):
    session = meta['session_id']
    with socket.create_connection(('127.0.0.1', port), timeout=5) as connection:
        connection.settimeout(20)
        with connection.makefile('rb') as reader:
            def exchange(message):
                connection.sendall((json.dumps(message) + '\n').encode())
                reply = json.loads(reader.readline(8193))
                if reply.get('type') != 'ack' or reply.get('session_id') != session:
                    raise ValueError('Receiver rejected recovery frame')
                return reply
            ack = exchange(dict(type='session', protocol=2, session_id=session,
                                started_at=meta['started_at']))
            offset = ack['offset']
            if type(offset) is not int or not 0 <= offset <= length:
                raise ValueError('Invalid receiver recovery offset')
            if offset:
                source.seek(offset-1)
                if source.read(1)!=b'\n':raise ValueError('Receiver offset is not a record boundary')
            # Replay the prefix too: duplicate ACKs must verify existing bytes,
            # rather than trusting an offset alone to prove byte equality.
            cursor = 0
            while cursor < length:
                limit = min(cursor + 32768, length, offset if cursor < offset else length)
                source.seek(cursor);block=source.read(limit-cursor)
                if len(block)!=limit-cursor:raise ValueError('Recovery raw truncated during replay')
                end = cursor+block.rfind(b'\n')+1
                if end <= cursor:
                    raise ValueError('Oversized or incomplete raw record')
                ack = exchange(dict(type='batch', session_id=session, offset=cursor,
                                    data=block[:end-cursor].decode('utf-8')))
                if ack.get('offset') != max(offset, end):
                    raise ValueError('Unexpected recovery ACK')
                offset = ack['offset']
                cursor = end
            check_unchanged()
            end_frame = {key: meta[key] for key in (
                'session_id', 'byte_length', 'state', 'ended_at', 'stop_reason',
                'point_count', 'dropped_count', 'loss_accounting_complete')}
            ack = exchange(dict(end_frame, type='session_end'))
            if ack.get('offset') != length or ack.get('complete') is not True:
                raise ValueError('Missing durable completion ACK')
            return ack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--replay', action='store_true')
    parser.add_argument('--port', type=int, default=18765)
    args = parser.parse_args()
    session = validate_session_id(args.session)
    args.output.mkdir(parents=True, exist_ok=False)
    prefix = [str(ROOT/'tools/hdc/hdc.exe'), '-t', args.target]
    for suffix in ('session.json', 'jsonl'):
        remote = f'/data/storage/el2/base/haps/entry/files/ink-{session}.{suffix}'
        result = subprocess.run(prefix + ['file', 'recv', '-b', 'com.example.inkbridge',
                                         remote, str(args.output/f'device.{suffix}')],
                                capture_output=True, timeout=30)
        if result.returncode or b'[Fail]' in result.stdout:
            raise RuntimeError(f'Device retrieval failed: {suffix}')
    meta = json.loads((args.output/'device.session.json').read_bytes())
    raw = args.output/'device.jsonl'
    checked=audit(raw,session)
    if (meta['session_id'] != session or meta['state'] not in ('completed', 'interrupted')
            or checked['byte_length'] != meta['byte_length'] or checked['point_count'] != meta['point_count']
            or not all(checked[k] for k in ('complete_jsonl_boundary','all_points_in_session','sequence_monotonic'))):
        raise ValueError('Stopped device session failed validation')
    report = dict(session_id=session, raw_sha256=checked['sha256'],
                  **{k:checked[k] for k in ('byte_length','point_count','pen_point_count','tool_counts','sequence_gaps')},
                  device_synced_at_retrieval=meta['synced'],
                  native_drops=meta['dropped_count'],
                  loss_accounting_complete=meta['loss_accounting_complete'],
                  synchronous_geometry_assessed=False, tablet_ui_actions=[])
    # Raw delivery and clock recovery are separate results. Never call the
    # latest 32-entry ring a complete recovery, or block raw replay on a clock
    # transfer failure. Checkpoints remain resumable via capture_clock_history.
    clock_path=args.output/'device.clock.json'
    try:
        report['clock_recovery_complete']=backfill_clock(prefix,session,clock_path)
        if clock_path.exists():
            clock=load_clock(clock_path)
            report['clock_history']={k:clock.get(k) for k in
                ('history_complete','history_window_count','history_missing_segments')}
    except (OSError,ValueError) as error:
        report.update(clock_recovery_complete=False,clock_recovery_error=str(error))
    if args.replay:
        try:
            report['recovery_ack'] = replay(raw, meta, args.port)
            pc = ROOT/'output/ink/sessions'/f'{session}.jsonl'
            report['canonical_raw_bytes_equal'] = same_bytes(pc,raw)
            if report['canonical_raw_bytes_equal']:
                # Keep a single raw copy, in the canonical durable store.
                (args.output/'device.jsonl').unlink()
                report['raw_path'] = str(pc.resolve())
        except (OSError, ValueError) as error:
            report['replay_error'] = str(error)
    (args.output/'recovery.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
