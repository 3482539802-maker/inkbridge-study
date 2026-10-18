"""Offline numeric diagnosis of tool contacts and unknown page coordinates."""
from collections import Counter
import json,math,sqlite3
from pathlib import Path
from capture_clock_history import load_clock
from coordinate_alignment import assess_clock,prepare_pairs
from native_frame_timing import bind
from coordinate_alignment import spatial_support,page_identity

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/test-runs/20261017-lasso-coordinate-001'


def inspect(session):
    folder=ROOT/'output/pipeline/cmc'/('session-'+session)
    db=sqlite3.connect((folder/'pipeline.sqlite').as_uri()+'?mode=ro',uri=True)
    state=load_clock(folder/'clock.json');clock=assess_clock(state)
    frames,calibration=bind([json.loads(p) for p, in db.execute('SELECT payload FROM geometry ORDER BY id')],state)
    raw=[json.loads(line) for line in (ROOT/'output/ink/sessions'/(session+'.jsonl')).read_bytes().splitlines()]
    points={p['seq']:p for p in raw}
    obs=[dict(json.loads(p),observation_id=i) for i,p in db.execute('SELECT id,payload FROM edit_observations ORDER BY id')]
    events=[json.loads(p) for p, in db.execute('SELECT payload FROM edit_events ORDER BY rowid')]
    coords={s:json.loads(p) for s,p in db.execute('SELECT seq,payload FROM coordinates ORDER BY seq')}
    contacts=[]
    for e in events:
        if not e.get('tool_type') in (1,3,4,5):continue
        lo,hi=e['start_seq'],e['end_seq'];cs=[c for n,c in coords.items() if lo<=n<=hi]
        unknown=[c for c in cs if c['pdf_xy'] is None]
        distance=math.dist(e['start_xy'],e['end_xy'])
        loop=distance<80 and e['path_length_px']>600
        if loop or unknown or e['kind']=='lasso_path_candidate':
            tlo=e['start_us']/1000+clock['offset_ms'];thi=e['end_us']/1000+clock['offset_ms']
            near=[{'id':o['observation_id'],'valid':o['valid'],'reason':o.get('reason'),
                  'span_relative_ms':[round(t-tlo,2) for t in o.get('device_wall_interval_ms',[])],
                  'editing':o.get('context',{}).get('editing'),
                  'pages':[p['page_id'] for p in o.get('context',{}).get('page_layers',[])]}
                  for o in obs if o.get('device_wall_interval_ms') and
                  tlo-5000<=o['device_wall_interval_ms'][0]<=thi+5000]
            nonpen=[p for p in raw if e['start_us']-4e6<=p['action_time']<=e['end_us']+4e6 and
                    (p['tool_type'] not in (1,3,4,5) or p['action']==0)]
            flat=[c['pdf_xy'] for c in cs if c['pdf_xy'] is not None]
            contacts.append({'start_seq':lo,'end_seq':hi,'kind':e['kind'],'context':e.get('context'),
                 'duration_ms':(e['end_us']-e['start_us'])/1000,'screen_bounds':e['bounds'],
                 'path_length_px':e['path_length_px'],'endpoint_distance_px':distance,'loop_candidate':loop,
                 'pdf_bounds':([min(p[0] for p in flat),min(p[1] for p in flat),max(p[0] for p in flat),max(p[1] for p in flat)] if flat else None),
                 'unknown':len(unknown),'reasons':dict(Counter(c['reason'] for c in unknown)),
                 'nearby_tools':near,'nearby_nonpen':nonpen,
                 'geometry':[{'index':f['index'],'valid':f.get('observation',{}).get('valid'),
                    'reason':f.get('observation',{}).get('reason'),'registration_reason':f.get('registration',{}).get('reason'),
                    'span_relative_ms':[round(t-tlo,2) for t in f.get('observation',{}).get('device_capture_wall_interval_ms',[])]}
                     for f in frames if f.get('observation',{}).get('device_capture_wall_interval_ms') and
                     tlo-5000<=f['observation']['device_capture_wall_interval_ms'][0]<=thi+5000]})
    report={'session_id':session,'clock':clock,'calibration':calibration,'event_counts':dict(Counter(e['kind'] for e in events)),
          'coordinates':dict(Counter(c['reason'] or c['status'] for c in coords.values())),
          'tool_counts':dict(Counter(o.get('context',{}).get('editing',{}).get('selected_tool') for o in obs)),
          'contacts':contacts}
    pairs=prepare_pairs(frames,True)
    detail=[]
    for reason in ('ambiguous_bracketing_context','input_motion_or_context_barrier'):
        sample=next((c for c in coords.values() if c['reason']==reason),None)
        if not sample:continue
        p=points[sample['seq']];t=p['action_time']/1000+clock['offset_ms'];error=clock['uncertainty_ms']
        detail.append({'reason':reason,'seq':p['seq'],'xy':[p['x'],p['y']],
             'pairs':[{'indices':[q['a']['index'],q['b']['index']],
                'page':q['a']['registration']['pdf_page_1based'],
                'support':[spatial_support(f,p,True) for f in [q['a'],q['b']]+q.get('intermediate_frames',[])],
                'nearby_nonpen':[{k:v for k,v in r.items() if k in ('seq','action','tool_type','x','y','pointer_id','down_time')}
                    for r in raw if q['start']-error<=r['action_time']/1000+clock['offset_ms']<=q['end']+error and r['tool_type'] not in (1,3,4,5)]}
                for q in pairs if q['gap_start']+error<t<q['gap_end']-error]})
    report['point_pair_detail']=detail
    db.close();OUT.mkdir(parents=True,exist_ok=True)
    (OUT/('diagnosis-'+session[:4]+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('clock','calibration','contacts')},ensure_ascii=False))
    print(json.dumps([ {k:c[k] for k in ('start_seq','end_seq','kind','duration_ms','loop_candidate','pdf_bounds','unknown','reasons')}
        for c in contacts],ensure_ascii=False))
    print(json.dumps(detail,ensure_ascii=False))


if __name__=='__main__':
    for session in ('9a639cf0-b182-4857-a2a9-dfb83687444b','d9b6889a-eaf3-4284-a21d-6b526cbbcbbc'):inspect(session)
