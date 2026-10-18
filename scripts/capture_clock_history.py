"""Durable numeric clock windows; no images, raw edits or device UI operations.

The device retains immutable overlapping <=32-sample checkpoints. The small
latest sidecar remains backwards compatible. Missing/conflicting evidence is
rejected rather than interpolated across a disconnected interval.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from ink_protocol import validate_session_id
from atomic_output import atomic_bytes

MAX_SIDECAR = 128 * 1024
MAX_SEGMENTS = 4096
MAX_SAMPLES = 100000
DIRECTORY = '/data/storage/el2/base/haps/entry/files/'


def read_window(path, session=None):
    if path.stat().st_size > MAX_SIDECAR:
        raise ValueError('Oversized clock window')
    state = json.loads(path.read_bytes())
    if session is not None and state.get('session_id') != session:
        raise ValueError('Clock window session mismatch')
    for key in ('anchors', 'observations'):
        if not isinstance(state.get(key), list) or len(state[key]) > 32:
            raise ValueError('Invalid bounded clock window')
    return state


def history_directory(path):
    return path.parent / (path.stem + '-history')


def merge_windows(windows, complete=True):
    if not windows or len(windows) > MAX_SEGMENTS + 1:
        raise ValueError('Invalid clock window count')
    session = windows[0]['session_id']
    anchors, observations = {}, {}
    previous_high = None
    gaps = []
    for state in windows:
        if state.get('session_id') != session:
            raise ValueError('Cross-session clock history')
        if len(state.get('anchors', [])) > 32 or len(state.get('observations', [])) > 32:
            raise ValueError('Oversized source clock window')
        span = [a['monotonic_before_us'] for a in state['anchors']]
        if any(b <= a for a, b in zip(span, span[1:])):
            raise ValueError('Clock source window is not chronological')
        seqs = [o['seq'] for o in state['observations']]
        if any(b <= a for a, b in zip(seqs, seqs[1:])):
            raise ValueError('Clock source observations are not chronological')
        if span and previous_high is not None and min(span) > previous_high:
            gaps.append([previous_high, min(span)])
        if span:
            previous_high = max(previous_high or min(span), max(span))
        for key, target, field in [('anchors', anchors, 'monotonic_before_us'),
                                   ('observations', observations, 'seq')]:
            for row in state[key]:
                index = row[field]
                if index in target and target[index] != row:
                    raise ValueError('Conflicting clock history evidence')
                target[index] = row
        if max(len(anchors), len(observations)) > MAX_SAMPLES:
            raise ValueError('Clock history exceeds analysis bound; use a shorter session')
    result = dict(windows[-1])
    result.update(anchors=[anchors[k] for k in sorted(anchors)],
                  observations=[observations[k] for k in sorted(observations)],
                  history_merged=True, history_complete=complete and not gaps and not any(w.get('history_lost') for w in windows),
                  history_gaps_us=gaps, history_window_count=len(windows))
    return result


def load_clock(path):
    path = Path(path)
    latest = read_window(path)
    if latest.get('history_format') != 1:
        directory = history_directory(path)
        snapshots = [p for p in directory.glob('legacy-*.json') if p.name != 'legacy-head.json'] if directory.exists() else []
        if snapshots:
            if len(snapshots) > MAX_SEGMENTS:
                raise ValueError('Legacy clock history exceeds bounded snapshot count')
            windows = [read_window(p, latest['session_id']) for p in snapshots]
            windows.sort(key=lambda s: s['anchors'][-1]['monotonic_before_us'] if s['anchors'] else -1)
            return merge_windows(windows + [latest])
        return latest
    count = latest.get('history_segments')
    if type(count) is not int or not 0 <= count <= MAX_SEGMENTS:
        raise ValueError('Invalid clock checkpoint count')
    directory = history_directory(path)
    windows, missing = [], []
    for index in range(count):
        segment = directory / f'{index}.json'
        if not segment.exists():
            missing.append(index)
            continue
        state = read_window(segment, latest['session_id'])
        if state.get('history_segments') != index:
            raise ValueError('Clock checkpoint identity mismatch')
        windows.append(state)
    result = merge_windows(windows + [latest], complete=not missing)
    result['history_missing_segments'] = missing
    return result


def pull_clock(prefix, session, output, max_backfill=4):
    """Fetch latest and at most four uncached immutable checkpoints per poll.

    Cached checkpoints are never overwritten, and survive PC process restart.
    Failed/latest transfers leave the previous published clock usable. The
    consumer explicitly rejects an incomplete checkpoint chain.
    """
    validate_session_id(session)
    output = Path(output)
    with tempfile.TemporaryDirectory(prefix='inkbridge-clock-', dir=output.parent) as temporary:
        temporary = Path(temporary)

        def receive(name, path):
            try:
                response = subprocess.run(prefix + ['file', 'recv', '-b', 'com.example.inkbridge',
                    DIRECTORY + name, str(path)], capture_output=True, timeout=10)
                if response.returncode or b'[Fail]' in response.stdout or not path.exists():
                    return None
                return read_window(path, session)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                return None

        latest = temporary / 'latest.json'
        state = receive('ink-' + session + '.clock.json', latest)
        if state is None:
            return False
        if state.get('history_format') == 1:
            count = state.get('history_segments')
            if type(count) is not int or not 0 <= count <= MAX_SEGMENTS:
                return False
            directory = history_directory(output)
            directory.mkdir(exist_ok=True)
            pending = [i for i in range(count) if not (directory / f'{i}.json').exists()]
            for index in pending[:max_backfill]:
                local = temporary / f'{index}.json'
                window = receive(f'ink-{session}.clock-{index}.json', local)
                if window is None or window.get('history_segments') != index:
                    continue
                atomic_bytes(directory / f'{index}.json', local.read_bytes())
        # Also preserve legacy snapshots before their ring rotates. Numeric
        # snapshots use a content hash, so retries never replace old evidence.
        else:
            directory = history_directory(output)
            directory.mkdir(exist_ok=True)
            head = directory / 'legacy-head.json'
            previous = read_window(head, session) if head.exists() else None
            previous_high = previous['anchors'][-1]['monotonic_before_us'] if previous and previous['anchors'] else -1
            new_anchors = sum(a['monotonic_before_us'] > previous_high for a in state['anchors'])
            # Retain overlap while avoiding a new file on every 5s poll. The
            # head is just a checkpoint cursor; consumers read hash-named files.
            if previous is None or new_anchors >= 16:
                payload = latest.read_bytes()
                archived = directory / ('legacy-' + hashlib.sha256(payload).hexdigest() + '.json')
                if not archived.exists():
                    atomic_bytes(archived, payload)
                atomic_bytes(head, payload)
        atomic_bytes(output, latest.read_bytes())
        return True


def backfill_clock(prefix, session, output, max_segments=128):
    """Bounded recovery for a stopped session; no frame/source work to delay."""
    if type(max_segments) is not int or not 1 <= max_segments <= MAX_SEGMENTS:
        raise ValueError('Invalid finite clock recovery budget')
    remaining = max_segments
    while remaining > 0:
        budget = min(4,remaining)
        if not pull_clock(prefix,session,output,max_backfill=budget):
            return False
        state = load_clock(output)
        if state.get('history_complete',True):
            return True
        missing = state.get('history_missing_segments',[])
        print(json.dumps({'clock_recovery_missing_segments':len(missing),
                          'remaining_transfer_budget':remaining}),flush=True)
        if not missing:
            return False  # A real nonoverlap gap cannot be repaired by retries.
        remaining -= budget
    return False


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target',required=True)
    p.add_argument('--session',required=True)
    p.add_argument('--output',type=Path,required=True,help='Latest clock.json; keep adjacent history directory')
    p.add_argument('--max-segments',type=int,default=128,help='Finite recovery budget, 1..4096; retry safely to resume')
    args=p.parse_args()
    validate_session_id(args.session)
    if not 1<=args.max_segments<=MAX_SEGMENTS:p.error('Recovery budget must be 1..4096')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    if args.output.exists():read_window(args.output,args.session)
    prefix=[str(Path(__file__).resolve().parents[1]/'tools/hdc/hdc.exe'),'-t',args.target]
    complete=backfill_clock(prefix,args.session,args.output,args.max_segments)
    print(json.dumps({'recovery_complete':complete,'session_id':args.session,
                      'output':str(args.output.resolve()),'tablet_ui_actions':[]}),flush=True)
    if not complete:raise SystemExit(1)


if __name__=='__main__':main()
