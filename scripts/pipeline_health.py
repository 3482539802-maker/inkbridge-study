"""Bounded read-only health sample from durable numeric pipeline files."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import statistics
import time


def sample(directory,raw_directory):
    job=json.loads((directory/'job.json').read_bytes());session=job['session_id']
    metadata=json.loads((raw_directory/(session+'.session.json')).read_bytes())
    progress=json.loads((directory/'processing.json').read_bytes())
    with closing(sqlite3.connect((directory/'pipeline.sqlite').resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        count,valid=db.execute("""SELECT COUNT(*),SUM(json_extract(payload,'$.registration.valid')=1 OR
            (json_extract(payload,'$.observation.valid')=1 AND
             json_extract(payload,'$.registration.reason')='independently_registered_visible_pages' AND
             json_array_length(payload,'$.registration.page_candidates')>0)) FROM geometry""").fetchone()
        rows=[json.loads(r[0]) for r in db.execute('SELECT payload FROM geometry ORDER BY id DESC LIMIT 256')]
        first,last=db.execute('SELECT MIN(t),MAX(t) FROM raw_index').fetchone()
        tools,tools_valid=db.execute("SELECT COUNT(*),SUM(json_extract(payload,'$.valid')=1) FROM edit_observations").fetchone()
    groups={}
    for row in rows:
        for name in ('source_observation_ms','registration_ms','processing_ms'):
            if name in row:groups.setdefault(name,[]).append(row[name])
        for key,value in row.get('observation',{}).get('source_stage_ms',{}).items():
            groups.setdefault('source.'+key,[]).append(value)
    medians={key:round(statistics.median(values),3) for key,values in groups.items()}
    # Receiver metadata and numeric status are independently published. The
    # raw index can already be ahead of the last metadata receipt; clipping a
    # negative difference to zero falsely claims a synchronized queue sample.
    delta=metadata['byte_length']-progress['raw_indexed_bytes']
    return {'session_id':session,'sampled_at_pc':time.time(),'state':metadata['state'],
        'receiver_events':metadata['point_count'],'receiver_bytes':metadata['byte_length'],
        'raw_event_span_seconds':(last-first)/1e6 if first is not None else 0,
        'indexed_events':progress['raw_events'],'indexed_bytes':progress['raw_indexed_bytes'],
        'pending_index_bytes_candidate':delta if delta>=0 else None,
        'receiver_metadata_behind_index':delta<0,
        'receiver_metadata_minus_index_bytes':delta,
        'pen_points':progress['pen_points'],'waiting_pen_points':progress['waiting_pen_points'],
        'results':progress['results'],'provisional_tail_pen_points':progress['provisional_tail_pen_points'],
        'clock_history_complete':progress.get('clock_history_complete'),
        'processing_status_age_seconds':round(time.time()-progress['updated_at_pc'],3),
        'geometry_records':count,'registered_frames':valid or 0,
        'tool_observations':tools,'valid_tool_observations':tools_valid or 0,
        'recent_geometry_limit':256,'timing_recent_median_ms':medians,
        'overlapped_cleanup_frames_in_recent_window':sum(bool(r.get('observation',{}).get('cleanup_postflight_overlapped')) for r in rows),
        'final_complete':progress['final_complete'],'sample_is_atomic_or_device_byte_equality_proof':False,
        'tablet_ui_actions':[],'screenshots_acquired_by_audit':0}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--session-directory',type=Path,required=True)
    p.add_argument('--raw-directory',type=Path,default=Path('output/ink/sessions'))
    p.add_argument('--append',type=Path)
    args=p.parse_args();result=sample(args.session_directory,args.raw_directory)
    if args.append:
        args.append.parent.mkdir(parents=True,exist_ok=True)
        with args.append.open('a',encoding='utf-8') as out:out.write(json.dumps(result,ensure_ascii=False)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
