"""Synthetic fault tests for disk ACKs, duplicate replay and receiver restarts."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

from simulate_ink import demo_points
from render_ink import split_strokes

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        return server.getsockname()[1]


def launch(folder, port, viewer, attempt):
    process = subprocess.Popen([
        sys.executable, '-X', 'utf8', str(ROOT / 'scripts/ink_receiver.py'), '--seconds', '0',
        '--port', str(port), '--viewer-port', str(viewer), '--sessions-dir', str(folder / 'sessions'),
        '--output', str(folder / f'legacy-{attempt}.jsonl')],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with urlopen(f'http://127.0.0.1:{viewer}/events', timeout=.3):
                return process
        except OSError:
            if process.poll() is not None:
                raise AssertionError(process.communicate())
            time.sleep(.025)
    process.terminate()
    process.communicate(timeout=5)
    raise AssertionError('Session receiver did not become ready')


def send(connection, reader, frame):
    connection.sendall((json.dumps(frame) + '\n').encode())
    line = reader.readline()
    assert line, 'Receiver closed before acknowledgement'
    return json.loads(line)


def verify():
    port, viewer = free_port(), free_port()
    while port == viewer:
        viewer = free_port()
    with tempfile.TemporaryDirectory(prefix='ink-sessions-') as temporary:
        folder = Path(temporary)
        expected = [dict(point, session_id='synthetic-session-a') for point in demo_points()]
        raw = ''.join(json.dumps(point) + '\n' for point in expected)
        lines = raw.splitlines(keepends=True)
        first, rest = ''.join(lines[:30]), ''.join(lines[30:])
        hello = dict(type='session', protocol=2, session_id='synthetic-session-a', started_at=1234)
        batch = dict(type='batch', session_id=hello['session_id'], offset=0, data=first)
        process = launch(folder, port, viewer, 1)
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=3) as connection:
                with connection.makefile('rb') as reader:
                    assert send(connection, reader, hello)['offset'] == 0
                    assert send(connection, reader, batch)['offset'] == len(first)
                    # The same bytes must not append or count twice after an ACK is lost.
                    assert send(connection, reader, batch)['offset'] == len(first)
                    # Send a second batch and lose its ACK at the client boundary.
                    next_batch = dict(batch, offset=len(first), data=''.join(lines[30:50]))
                    connection.sendall((json.dumps(next_batch) + '\n').encode())
                    time.sleep(.1)
            journal = folder / 'sessions/synthetic-session-a.jsonl'
            deadline = time.monotonic() + 3
            prefix = ''.join(lines[:50])
            while journal.stat().st_size != len(prefix):
                if time.monotonic() > deadline:
                    raise AssertionError('Lost-ACK batch not persisted')
                time.sleep(.025)
            process.terminate()  # No graceful in-memory checkpoint is allowed to help.
            process.communicate(timeout=5)
            # Simulate a power-loss tail that was never ACKed.
            with journal.open('ab') as output:
                output.write(b'{"type":"point"')
            process = launch(folder, port, viewer, 2)
            with socket.create_connection(('127.0.0.1', port), timeout=3) as connection:
                with connection.makefile('rb') as reader:
                    assert send(connection, reader, hello)['offset'] == len(prefix)
                    assert journal.read_bytes() == prefix.encode()
                    assert send(connection, reader, batch)['offset'] == len(prefix)
                    offset = len(prefix)
                    # Real client caps batches at 32 KiB and keeps newline boundaries.
                    chunk = ''
                    for line in lines[50:]:
                        if len(chunk) + len(line) > 32768:
                            ack = send(connection, reader, dict(batch, offset=offset, data=chunk))
                            offset += len(chunk)
                            assert ack['offset'] == offset
                            chunk = ''
                        chunk += line
                    if chunk:
                        assert send(connection, reader, dict(batch, offset=offset, data=chunk))['offset'] == len(raw)
                    end = dict(type='session_end', session_id=hello['session_id'], byte_length=len(raw),
                               state='completed', ended_at=5678, stop_reason='user_stop',
                               point_count=len(expected), dropped_count=0)
                    assert send(connection, reader, end)['complete'] is True
                    # A second capture starts at seq=1 and has independent bytes/metadata.
                    second = dict(hello, session_id='synthetic-session-b')
                    assert send(connection, reader, second)['offset'] == 0
                    second_raw = json.dumps(dict(expected[0], session_id=second['session_id'])) + '\n'
                    assert send(connection, reader, dict(batch, session_id=second['session_id'], data=second_raw))['offset'] == len(second_raw)
                    # Native queue drops leave sequence holes. Byte ACKs must still advance.
                    third = dict(hello, session_id='synthetic-native-gap')
                    assert send(connection, reader, third)['offset'] == 0
                    gap_points = [dict(expected[0], session_id=third['session_id'], seq=1),
                                  dict(expected[1], session_id=third['session_id'], seq=3)]
                    gap_raw = ''.join(json.dumps(p) + '\n' for p in gap_points)
                    assert send(connection, reader, dict(batch, session_id=third['session_id'], data=gap_raw))['offset'] == len(gap_raw)
                    assert send(connection, reader, dict(end, session_id=third['session_id'], byte_length=len(gap_raw),
                                                         point_count=2, dropped_count=1))['complete'] is True
                    assert send(connection, reader, second)['offset'] == len(second_raw)
                    # A conflicting replay must fail without changing persisted raw data.
                    conflict = dict(batch, session_id=second['session_id'], data=second_raw.replace('"x":', '"x": 1, "unused":'))
                    assert send(connection, reader, conflict)['type'] == 'error'
            with urlopen(f'http://127.0.0.1:{viewer}/events') as response:
                state = json.load(response)
            assert state['points'] == len(expected) + 3, state
            assert state['sequence_gaps'] == 1 and state['sequence_reorders'] == 0, state
            assert state['replayed_batches'] == 1, state
            assert state['invalid_records'] == 1, state
            assert len(state['sessions']) == 3
            with urlopen(f'http://127.0.0.1:{viewer}/events?after=99999') as response:
                restarted_view = json.load(response)
            assert restarted_view['reset'] is True and restarted_view['next_cursor'] == len(expected) + 3
            with urlopen(f'http://127.0.0.1:{viewer}/events?session_id=synthetic-session-b') as response:
                selected = json.load(response)
            assert len(selected['events']) == 1 and selected['next_cursor'] == len(expected) + 3
            assert journal.read_bytes() == raw.encode(), 'Raw fields/order/bytes changed during replay'
            metadata = json.loads(journal.with_suffix('.session.json').read_text(encoding='utf-8'))
            assert metadata['state'] == 'completed' and metadata['point_count'] == len(expected)
            # A coincidentally consecutive seq cannot join geometry across sessions.
            sample = [dict(expected[0], seq=1, action=1),
                      dict(expected[1], seq=2, action=2, session_id='another-session')]
            assert len(split_strokes(sample)) == 2
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
    report = dict(data_source='synthetic-test', passed=True, tablet_verified=False,
                  received_points=len(expected) + 3,
                  checks=['durable byte ACK', 'lost ACK replay without duplicates',
                          'forced receiver restart', 'unacknowledged torn-tail repair',
                          'raw byte equality', 'multiple session isolation',
                          'conflicting replay rejection', 'session end metadata',
                          'no stroke across session boundaries', 'HTTP session selection',
                          'byte ACK advances across native sequence gaps',
                          'viewer cursor recovery after receiver restart'])
    destination = ROOT / 'output/tests/session-replay-report.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'PASS: {len(expected) + 3} synthetic points, durable sessions, replay and forced restart.')


if __name__ == '__main__':
    verify()
