"""Streaming final raw/index/coordinate/export audit, without a 64 MiB cap.

Checks derived provenance and completion, not physical coordinate accuracy.
No screenshots, tablet access, writes to the evidence database, or inference.
"""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from ink_protocol import PEN_TOOLS,validate_point
from raw_journal_audit import signature

VERSION=1


def read_record(source,limit):
    line=source.readline(limit+1)
    if not line:return None,None
    if len(line)>limit or not line.endswith(b'\n'):raise ValueError('Oversized or incomplete audit record')
    return line,json.loads(line)


def audit(directory,raw,metadata):
    directory,raw,metadata=map(Path,(directory,raw,metadata))
    export=directory/'points.jsonl'
    paths=(raw,metadata,export);before=[signature(p) for p in paths]
    meta=json.loads(metadata.read_bytes());session=meta['session_id']
    if meta.get('state') not in ('completed','interrupted'):raise ValueError('Audit needs stopped raw metadata')
    counts=Counter();results=Counter();points=events=length=0;previous_seq=previous_time=None
    gaps=0;time_monotonic=True;sequence_monotonic=True;digest=hashlib.sha256();export_hash=hashlib.sha256()
    with closing(sqlite3.connect((directory/'pipeline.sqlite').resolve().as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
        db.execute('PRAGMA temp_store=FILE');db.execute('BEGIN')
        get=lambda key:json.loads(row[0]) if (row:=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()) else None
        if not get('final_complete'):raise ValueError('Coordinate final replay incomplete')
        indexed=db.execute('''SELECT r.seq,r.t,r.start,r.end,r.tool,r.action,r.display,r.win,r.pointer,c.payload
            FROM raw_index r LEFT JOIN coordinates c ON r.seq=c.seq ORDER BY r.seq''')
        with closing(indexed),raw.open('rb') as source,export.open('rb') as output:
            for seq,t,start,end,tool,action,display,win,pointer,payload in indexed:
                line,p=read_record(source,8192)
                if p is None:raise ValueError('Raw shorter than index')
                p=validate_point(p)
                if ([seq,t,start,end,tool,action,display,win,pointer]!=
                    [p['seq'],p['action_time'],length,length+len(line),p['tool_type'],p['action'],p['display_id'],p['window_id'],p['pointer_id']] or
                    p.get('session_id')!=session):raise ValueError('Index differs from canonical raw')
                digest.update(line);length+=len(line);events+=1
                if previous_seq is None:gaps+=max(0,seq-1)
                else:
                    gaps+=max(0,seq-previous_seq-1);sequence_monotonic &= seq>previous_seq
                    time_monotonic &= t>=previous_time
                previous_seq,previous_time=seq,t
                if tool not in PEN_TOOLS or action==0:
                    if payload is not None:raise ValueError('Coordinate on non-pen/cancel record')
                    continue
                points+=1
                if payload is None:raise ValueError('Missing pen coordinate result')
                c=json.loads(payload);export_line,exported=read_record(output,128*1024)
                if exported!=c:raise ValueError('Coordinate export differs from database')
                export_hash.update(export_line)
                if (c.get('session_id')!=session or c.get('seq')!=seq or c.get('screen_xy')!=[p['x'],p['y']] or
                    c.get('action_time_us')!=t or c.get('provisional_live_revision',True)):
                    raise ValueError('Coordinate source identity or final revision mismatch')
                status=c.get('status');reason=c.get('reason')
                if status not in ('candidate','extrapolated_candidate','motion_candidate','unresolved'):raise ValueError('Unknown coordinate status')
                counts[status]+=1;results[reason or status]+=1
            if source.read(1) or output.read(1):raise ValueError('Unindexed raw or extra coordinate export')
        if db.execute('SELECT COUNT(*) FROM coordinates').fetchone()[0]!=points:
            raise ValueError('Orphan coordinate records')
        if get('raw_offset')!=length or meta.get('byte_length')!=length or meta.get('point_count')!=events:
            raise ValueError('Final byte/event counts differ')
        actual=dict(db.execute('SELECT key,n FROM counters WHERE n>0'))
        expected={'events':events,'pens':points,**{'result:'+k:n for k,n in results.items()}}
        if actual!={k:v for k,v in expected.items() if v>0}:raise ValueError('Derived counters differ')
        timing={}
        # SQLite orders numeric columns using bounded cache and disk temp storage;
        # never loads a day's frames to compute exact quantiles in Python.
        for field in ('source_observation_ms','registration_ms','processing_ms'):
            path='$.'+field
            query="SELECT CAST(json_extract(payload,?) AS REAL) AS v FROM geometry WHERE json_type(payload,?) IN ('real','integer') AND json_extract(payload,?)>=0"
            n,maximum=db.execute('SELECT COUNT(*),MAX(v) FROM ('+query+')',(path,path,path)).fetchone()
            def value(offset):
                return db.execute(query+' ORDER BY v LIMIT 1 OFFSET ?',(path,path,path,offset)).fetchone()[0]
            timing[field]={'count':n,'median':(value((n-1)//2)+value(n//2))/2 if n else None,
                           'p95':value(int((n-1)*.95+.5)) if n else None,'max':maximum}
        frames,registered=db.execute("""SELECT COUNT(*),SUM(json_extract(payload,'$.registration.valid')=1 OR
            (json_extract(payload,'$.observation.valid')=1 AND
             json_extract(payload,'$.registration.reason')='independently_registered_visible_pages' AND
             json_array_length(payload,'$.registration.page_candidates')>0)) FROM geometry""").fetchone()
    if [signature(p) for p in paths]!=before:raise ValueError('Stopped inputs changed during audit')
    return {'schema':VERSION,'session_id':session,'raw_sha256':digest.hexdigest(),'coordinate_export_sha256':export_hash.hexdigest(),
        'raw_events':events,'raw_bytes':length,'pen_points':points,'coordinate_status_counts':dict(counts),
        'coordinate_reason_counts':dict(results),'located_candidate_coverage':
        (counts['candidate']+counts['extrapolated_candidate']+counts['motion_candidate'])/points if points else None,
        'motion_estimated_points':counts['motion_candidate'],
        'zero_unknown_coordinates':points>0 and counts['unresolved']==0,
        'zero_unknown_with_local_raw_integrity':bool(points>0 and counts['unresolved']==0 and
            gaps==0 and sequence_monotonic and time_monotonic and meta.get('dropped_count')==0 and meta.get('state')=='completed'),
        'sequence_gaps':gaps,'sequence_monotonic':sequence_monotonic,'action_time_monotonic':time_monotonic,
        'raw_index_coordinates_export_equal':True,'derived_counters_equal':True,
        'geometry_records':frames,'registered_frames':registered or 0,'latency_ms':timing,
        'latency_scope':'all_geometry_attempts_not_end_to_end','device_byte_equality_checked':False,
        'native_dropped_count_from_metadata':meta.get('dropped_count'),
        'physical_mm_accuracy_verified':False,'screenshots_acquired':0,'tablet_actions':[],
        'raw_modified':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('directory','raw','metadata','output'):p.add_argument('--'+name,type=Path,required=True)
    args=p.parse_args()
    if args.output.exists():p.error('Choose a new audit output')
    result=audit(args.directory,args.raw,args.metadata)
    from atomic_output import atomic_bytes
    atomic_bytes(args.output,json.dumps(result,ensure_ascii=False,indent=2).encode())
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
