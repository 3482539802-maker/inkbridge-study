"""Passively save Notes UI geometry and per-test copies of new canonical journals."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import time

from ink_motion import analyze_points, page_motion
from notes_context import capture, walk

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir', type=Path, required=True)
    p.add_argument('--target')
    p.add_argument('--seconds', type=float, default=1800)
    p.add_argument('--interval', type=float, default=3)
    args = p.parse_args()
    if args.interval < 2 or args.seconds <= 0: p.error('interval >= 2 and seconds > 0 required')
    run = args.run_dir.resolve()
    run.mkdir(parents=True, exist_ok=True)
    manifest_path = run/'run.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_bytes())
    else:
        first_context = run/'context/before.json'
        first_time = (json.loads(first_context.read_bytes()).get('capture_started_at_pc')
                      if first_context.exists() else None)
        started_at_ms = int(datetime.fromisoformat(first_time).timestamp()*1000) if first_time else int(time.time()*1000)
        manifest = {'started_at_ms': started_at_ms, 'created_at': datetime.now(timezone.utc).isoformat(),
                    'test': 'CMC2025 pen/finger/page geometry', 'tablet_ui_actions': [],
                    'canonical_sessions': str(ROOT/'output/ink/sessions'),
                    'raw_clock_to_pc_calibrated': False}
        write_json(manifest_path, manifest)
    previous = None
    changed_files = {}
    deadline = time.monotonic()+args.seconds
    try:
        while time.monotonic() < deadline:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            destination = run/'context'/f'{stamp}.json'
            try:
                context = capture(ROOT/'tools/hdc/hdc.exe', args.target, destination, reveal_page=False)
                layout = json.loads(destination.with_suffix('.layout.json').read_bytes())
                roots = [n['attributes'] for n, _ in walk(layout) if n.get('attributes', {}).get('type') == 'root']
                if len(roots) == 1:
                    context['window_id'] = roots[0].get('hostWindowId')
                    context['display_id'] = roots[0].get('displayId')
                changes = page_motion(previous, context) if previous else []
                context['geometry_changes_since_previous'] = changes
                if previous:
                    context['previous_capture_started_at_pc'] = previous.get('capture_started_at_pc')
                write_json(destination, context)
                write_json(run/'context/latest.json', context)
                with (run/'context/history.jsonl').open('a', encoding='utf-8') as f:
                    f.write(json.dumps(context, ensure_ascii=False)+'\n')
                previous = context
            except Exception as error:
                previous = None
                write_json(destination, {'at': stamp, 'error': str(error), 'document_title': None})
            # Keep canonical ACK storage unchanged; copy only sessions created for this run.
            for meta in (ROOT/'output/ink/sessions').glob('*.session.json'):
                try:
                    info = json.loads(meta.read_bytes())
                    started = info.get('started_at')
                    if not isinstance(started, (int, float)) or started < manifest['started_at_ms']: continue
                    raw = meta.with_suffix('').with_suffix('.jsonl')
                    if not raw.exists(): continue
                    signature = (raw.stat().st_size, raw.stat().st_mtime_ns, meta.stat().st_mtime_ns)
                    if changed_files.get(raw.name) == signature: continue
                    local = run/'raw'/raw.name
                    local.parent.mkdir(exist_ok=True)
                    shutil.copyfile(raw, local)
                    shutil.copyfile(meta, local.parent/meta.name)
                    data = local.read_bytes()
                    complete = data[:data.rfind(b'\n')+1]
                    result = analyze_points(json.loads(l) for l in complete.splitlines() if l.strip())
                    result.update(data_source='real_device_session_copy', source=str(local),
                                  live_copy_not_final_sync_proof=True, incomplete_tail_bytes=len(data)-len(complete))
                    write_json(run/'analysis'/f'{raw.stem}-motion.json', result)
                    changed_files[raw.name] = signature
                except Exception as error:
                    write_json(run/'analysis/last-error.json', {'error': str(error), 'source': str(meta)})
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
