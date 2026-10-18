"""Replay closed sessions into isolated SQLite snapshots; never edit raw inputs."""
from collections import Counter
import argparse,hashlib,json,sqlite3
from pathlib import Path
from coordinate_stream import CoordinateStream
from capture_clock_history import load_clock
from edit_events import process_edits,publish_edits
from atomic_output import atomic_bytes


def run(root,output):
    output.mkdir(parents=True,exist_ok=False);reports=[]
    for source in sorted((root/'output/pipeline/cmc').glob('session-*/job.json')):
        job=json.loads(source.read_bytes())
        if not job.get('final_complete') or not job.get('geometry_closed'):continue
        sid=job['session_id'];raw=root/'output/ink/sessions'/(sid+'.jsonl')
        raw_hash=hashlib.sha256(raw.read_bytes()).hexdigest()
        target=output/('session-'+sid);target.mkdir()
        original=sqlite3.connect(source.with_name('pipeline.sqlite').as_uri()+'?mode=ro',uri=True)
        old={seq:json.loads(p) for seq,p in original.execute('SELECT seq,payload FROM coordinates')}
        old_events={json.loads(p)['start_seq']:json.loads(p) for p, in original.execute('SELECT payload FROM edit_events')}
        dest=sqlite3.connect(target/'pipeline.sqlite');original.backup(dest);dest.close();original.close()
        store=CoordinateStream(target,raw,sid,True,job['page_margins'])
        state=load_clock(source.with_name('clock.json'))
        while store.step(state,final=True):pass
        while not process_edits(store,state,final=True):pass
        new={seq:json.loads(p) for seq,p in store.db.execute('SELECT seq,payload FROM coordinates')}
        changes=[{'seq':n,'old':old[n],'new':c} for n,c in new.items() if old[n]!=c]
        lost=[n for n,c in old.items() if c.get('pdf_xy') is not None and (new[n].get('pdf_xy') is None or
              any(new[n].get(k)!=c.get(k) for k in ('pdf_xy','source_pdf_sha256','pdf_page_1based','page_uuid','status')))]
        if lost:raise ValueError('Existing located points changed: '+str(lost[:10]))
        events={json.loads(p)['start_seq']:json.loads(p) for p, in store.db.execute('SELECT payload FROM edit_events')}
        if set(events)!=set(old_events) or set(old)!=set(new):raise ValueError('Lost raw contacts/points')
        if hashlib.sha256(raw.read_bytes()).hexdigest()!=raw_hash:raise ValueError('Raw modified')
        store.export();store.publish();publish_edits(store,final=True);store.close()
        changed_tools=[{'start_seq':n,'old':old_events[n]['kind'],'new':e['kind'],'context':e.get('context')}
                       for n,e in events.items() if e['kind']!=old_events[n]['kind']]
        report={'session_id':sid,'raw_sha256':raw_hash,'old_results':dict(Counter(c['reason'] or c['status'] for c in old.values())),
            'new_results':dict(Counter(c['reason'] or c['status'] for c in new.values())),
            'changed_points':len(changes),'existing_locations_unchanged':not lost,'changed_tools':changed_tools,
            'point_count':len(new),'contact_events':len(events),'raw_modified':False}
        atomic_bytes(target/'changes.json',json.dumps(changes,ensure_ascii=False,indent=2).encode())
        reports.append(report);print(json.dumps(report,ensure_ascii=False),flush=True)
    atomic_bytes(output/'verification.json',json.dumps(reports,ensure_ascii=False,indent=2).encode())


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    run(Path(__file__).resolve().parents[1],args.output)
