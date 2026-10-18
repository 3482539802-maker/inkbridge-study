"""Finalize a finite observer after a later user stop, without acquiring images."""
import argparse
import json
from pathlib import Path

from capture_clock_history import load_clock, backfill_clock
from coordinate_alignment import align
from coordinate_acceptance import audit
from coordinate_live import update
from native_frame_timing import bind


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',type=Path,required=True)
    p.add_argument('--raw',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--stable-runs',action='store_true')
    p.add_argument('--page-margins',action='store_true',help='Bounded estimates only where existing frames carry verified native page bindings')
    p.add_argument('--target',help='Optionally recover final numeric clock history from this device; no images or UI actions')
    args=p.parse_args()
    metadata=json.loads(args.raw.with_suffix('.session.json').read_bytes())
    if metadata.get('state') not in ('completed','interrupted'):
        p.error('Wait for the user to stop capture; never finalize an active raw journal')
    if (args.run/'geometry.jsonl').stat().st_size>12*1024*1024:p.error('Oversized geometry')
    args.output.mkdir(parents=True,exist_ok=False)
    clock_path=args.run/'clock.json'
    if args.target:
        clock_path=args.output/'clock.json'
        prefix=[str(Path(__file__).resolve().parents[1]/'tools/hdc/hdc.exe'),'-t',args.target]
        if not backfill_clock(prefix,metadata['session_id'],clock_path):
            p.error('Final clock history recovery incomplete; raw is preserved')
    state=load_clock(clock_path)
    if state['session_id']!=metadata['session_id']:p.error('Clock and raw session mismatch')
    frames=[json.loads(line) for line in (args.run/'geometry.jsonl').read_text(encoding='utf-8').splitlines()]
    frames,calibration=bind(frames,state)
    geometry=args.output/'timed-geometry.jsonl'
    geometry.write_text(''.join(json.dumps(f)+'\n' for f in frames),encoding='utf-8')
    summary=align(args.raw,state,frames,args.output/'points.jsonl',stable_runs=args.stable_runs,allow_page_margins=args.page_margins)
    update(args.raw,state,frames,args.output/'live-coordinates',stable_runs=args.stable_runs,
           allow_page_margins=args.page_margins,finalized=True)
    acceptance=audit(args.raw,clock_path,geometry,args.output/'points.jsonl')
    for name,value in [('native-time-calibration.json',calibration),('alignment-summary.json',summary),
                       ('acceptance.json',acceptance)]:
        (args.output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in acceptance.items() if k not in ('strokes','touch_episodes')},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
