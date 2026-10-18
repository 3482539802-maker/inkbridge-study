"""Finite numeric observation of an existing user-started session; no images/UI."""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

from audit_frame_runtime import audit
from pipeline_health import sample


def observe(directory, raw_directory):
    health = sample(directory, raw_directory)
    runtime = audit(directory)
    with closing(sqlite3.connect((directory/'pipeline.sqlite').resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        db.execute('BEGIN')
        rows = [json.loads(p) for p, in db.execute('SELECT payload FROM geometry ORDER BY id DESC LIMIT 256')][::-1]
    changes = Counter()
    for row in rows:
        o = row.get('observation',{})
        a,b = o.get('context_before') or {}, o.get('context') or {}
        if o.get('reason') == 'context_boundary_failed_during_frame':
            if o.get('window_before') != o.get('window'): changes['window'] += 1
            if a.get('document_title_candidate') != b.get('document_title_candidate'): changes['document'] += 1
            aa,bb = a.get('page_layers',[]),b.get('page_layers',[])
            if [p.get('page_id') for p in aa] != [p.get('page_id') for p in bb]: changes['visible_page_ids'] += 1
            elif aa != bb: changes['page_bounds_or_metadata'] += 1
            elif a == b: changes['intermediate_context_transition_or_failure'] += 1
    return {'health':health,'runtime':runtime,'rejected_context_endpoint_differences':dict(changes)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--raw-directory',type=Path,default=Path('output/ink/sessions'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seconds',type=float,default=180)
    parser.add_argument('--interval',type=float,default=15)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300 or not 1 <= args.interval <= 30:
        parser.error('Finite observation <=300 seconds, interval 1..30 seconds')
    args.output.mkdir(parents=True,exist_ok=False)
    deadline = time.monotonic()+args.seconds
    with (args.output/'observations.jsonl').open('x',encoding='utf-8') as out:
        while True:
            try:
                report = observe(args.directory,args.raw_directory)
                h,r = report['health'],report['runtime']
                compact = {k:h[k] for k in ('state','receiver_events','pen_points','waiting_pen_points','results',
                    'geometry_records','registered_frames','clock_history_complete','final_complete')}
                compact.update(frame_interval_ms=r['requested_frame_callback_intervals_ms'].get('median'),
                    recent_frame_reasons=r['reasons'],context_changes=report['rejected_context_endpoint_differences'])
            except (OSError,ValueError,sqlite3.Error) as error:
                report = {'error_type':type(error).__name__,'error':str(error)[:200],'sampled_at_pc':time.time()}
                compact = report
            out.write(json.dumps(report,ensure_ascii=False)+'\n');out.flush()
            print(json.dumps(compact,ensure_ascii=False),flush=True)
            if report.get('health',{}).get('final_complete') or time.monotonic()>=deadline:
                break
            time.sleep(min(args.interval,max(0,deadline-time.monotonic())))


if __name__=='__main__':main()
