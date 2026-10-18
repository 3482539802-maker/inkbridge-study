"""Read-only acceptance report for actual pen/frame evidence and missing coverage.

No screenshots or device commands. Separates native backend exercise, coordinate
coverage, measured processing cost, and still-unmeasured physical accuracy.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
import math

from capture_clock_history import load_clock
from coordinate_alignment import assess_clock, journal, valid_frame, page_identity, affine, number
from coordinate_live import strokes_from_raw
from ink_protocol import PEN_TOOLS
from native_frame_timing import calibrate
from multipage_geometry import page_frames


def distribution(values):
    values = sorted(v for v in values if isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v))
    return {'count':len(values),'median':statistics.median(values) if values else None,
            'p95':values[min(len(values)-1,int((len(values)-1)*.95+.5))] if values else None,
            'max':max(values) if values else None}


def touch_episodes(points):
    active, episode, episodes = {}, None, []
    for p in points:
        if p['tool_type'] != 0:
            continue
        key = p['display_id'],p['window_id'],p['pointer_id']
        if episode is None:
            episode = {'start_us':p['action_time'],'end_us':p['action_time'],
                       'max_contacts':0,'seq_range':[p['seq'],p['seq']],
                       'window_id':p['window_id'],'display_id':p['display_id'],
                       'down_observed':p['action']==1,'ended':False}
        if p['action'] in (0,3):
            active.pop(key,None)
        else:
            active[key] = True
        episode.update(end_us=p['action_time'],max_contacts=max(episode['max_contacts'],len(active)))
        episode['seq_range'][1] = p['seq']
        if not active:
            episode['ended']=True
            episodes.append(episode)
            episode=None
    if episode:episodes.append(episode)
    return episodes


def audit(raw, clock_path, geometry, locations):
    raw, clock_path, geometry, locations = map(Path,(raw,clock_path,geometry,locations))
    signature = raw.stat().st_size,raw.stat().st_mtime_ns
    if max(raw.stat().st_size,locations.stat().st_size)>64*1024*1024 or geometry.stat().st_size>12*1024*1024:
        raise ValueError('Use a bounded session segment for acceptance')
    state = load_clock(clock_path)
    points = list(journal(raw,state['session_id']))
    payload = raw.read_bytes()
    frames = [json.loads(line) for line in geometry.read_text(encoding='utf-8').splitlines()]
    mapped = {}
    by_seq = {p['seq']:p for p in points}
    previous = 0
    for line in locations.read_text(encoding='utf-8').splitlines():
        p = json.loads(line)
        if p['seq']<=previous or p['seq'] not in by_seq or p['session_id']!=state['session_id']:
            raise ValueError('Coordinate output does not match raw session/order')
        if p.get('screen_xy') != [by_seq[p['seq']]['x'],by_seq[p['seq']]['y']]:
            raise ValueError('Coordinate output screen points differ from raw')
        mapped[p['seq']]=p
        previous=p['seq']
    assessment = assess_clock(state)
    integrity = (not points or points[0]['seq']==1) and all(b['seq']==a['seq']+1 and b['action_time']>=a['action_time'] for a,b in zip(points,points[1:]))
    integrity = integrity and all(o['seq'] in by_seq and by_seq[o['seq']]['action_time']==o['action_time_us']
                                  for o in state['observations'])
    pens = [p for p in points if p['tool_type'] in PEN_TOOLS and p['action']!=0]
    candidates = [mapped[p['seq']] for p in pens if mapped.get(p['seq'],{}).get('status')=='candidate']
    extrapolated = [mapped[p['seq']] for p in pens if mapped.get(p['seq'],{}).get('status')=='extrapolated_candidate']
    motion = [mapped[p['seq']] for p in pens if mapped.get(p['seq'],{}).get('status')=='motion_candidate']
    unresolved = [p for p in pens if mapped.get(p['seq'],{}).get('status') not in ('candidate','extrapolated_candidate','motion_candidate')]
    intervals = [f['observation']['device_capture_wall_interval_ms'] for f in frames
                 if isinstance(f.get('observation',{}).get('device_capture_wall_interval_ms'),list)
                 and len(f['observation']['device_capture_wall_interval_ms'])==2
                 and all(number(v) for v in f['observation']['device_capture_wall_interval_ms'])]
    last_frame = max((v[1] for v in intervals),default=None)
    after_observation = [p for p in unresolved if last_frame is not None and assessment['usable_candidate']
                         and p['action_time']/1000+assessment['offset_ms']-assessment['uncertainty_ms']>last_frame]
    native = {f['index']:f for f in frames if f.get('observation',{}).get('native_frame')}
    native_pages = {index:page_frames(f) for index,f in native.items()}
    native_clock = calibrate(frames,state)
    native_candidates = [p for p in candidates if len(ids:=p.get('evidence',{}).get('geometry_indices',[]))==2
                         and native_clock['usable_candidate']
                         and all(any(valid_frame(f) and f['observation'].get('timestamp_used_for_coordinates') and
                             f['registration']['source_pdf_sha256']==p.get('source_pdf_sha256') and
                             f['registration']['pdf_page_1based']==p.get('pdf_page_1based') and
                             f['observation']['context']['page_layers'][0]['page_id']==p.get('page_uuid')
                             for f in native_pages.get(i,[])) for i in ids)]
    episodes = touch_episodes(points)
    usable_frames = [child for f in frames for child in page_frames(f) if valid_frame(child)]
    for e in episodes:
        e.update(visual_motion_confirmed=False,pen_points_after_2s=0,native_candidates_after_2s=0)
        if not assessment['usable_candidate'] or not integrity or not e['ended'] or not e['down_observed']:continue
        lo,hi = [t/1000+assessment['offset_ms'] for t in (e['start_us'],e['end_us'])]
        e['wall_interval_candidate_ms']=[lo,hi]
        before = [f for f in usable_frames if lo-5000<=f['observation']['device_capture_wall_interval_ms'][1]<lo-assessment['uncertainty_ms']]
        after = [f for f in usable_frames if hi+assessment['uncertainty_ms']<f['observation']['device_capture_wall_interval_ms'][0]<=hi+5000]
        if before and after:
            nearest={}
            for f in before:
                key=page_identity(f)
                if key not in nearest or f['observation']['device_capture_wall_interval_ms'][1]>nearest[key]['observation']['device_capture_wall_interval_ms'][1]:
                    nearest[key]=f
            choices=[(nearest[page_identity(f)],f) for f in after if page_identity(f) in nearest]
            if choices:
                a,b=min(choices,key=lambda pair:pair[1]['observation']['device_capture_wall_interval_ms'][0]-pair[0]['observation']['device_capture_wall_interval_ms'][1])
                ra,rb=a['registration'],b['registration']
                ratio=(sum(v*v for row in rb['pdf_to_screen'] for v in row[:2])/
                       sum(v*v for row in ra['pdf_to_screen'] for v in row[:2]))**.5
                centre=[v/2 for v in ra['screen_size']]
                moved=affine(rb['pdf_to_screen'],*affine(ra['screen_to_pdf'],*centre))
                displacement=sum((x-y)**2 for x,y in zip(centre,moved))**.5
                e.update(scale_ratio=ratio,centre_displacement_px=displacement,
                         visual_motion_confirmed=abs(ratio-1)>.02 or displacement>20,
                         visual_motion_kind='scale' if abs(ratio-1)>.02 else 'translation' if displacement>20 else 'stationary')
        after_points = [p for p in pens if e['end_us']<p['action_time']<=e['end_us']+2000000
                        and (p['window_id'],p['display_id'])==(e['window_id'],e['display_id'])]
        native_seqs = {p['seq'] for p in native_candidates}
        e.update(pen_points_after_2s=len(after_points),
                 native_candidates_after_2s=sum(p['seq'] in native_seqs for p in after_points))
    strokes = strokes_from_raw(payload,mapped)
    latency = {k:distribution([f.get(k) for f in frames]) for k in
               ('source_observation_ms','registration_ms','receiver_and_clock_ms','processing_ms')}
    # Separate successfully acquired native buffers from failed attempts. A
    # timeout must not become a successful-frame speed benchmark.
    latency['native_acquired_frames_ms'] = {k:distribution([f.get(k) for f in native.values()]) for k in
        ('source_observation_ms','registration_ms','receiver_and_clock_ms','processing_ms')}
    latency['acquisition_failed_attempts_ms'] = {k:distribution([f.get(k) for f in frames
        if f.get('observation',{}).get('reason')=='context_or_acquisition_failed']) for k in
        ('source_observation_ms','processing_ms')}
    stages = sorted({k for f in frames for k in f.get('observation',{}).get('source_stage_ms',{})})
    latency['source_stages_ms']={k:distribution([f.get('observation',{}).get('source_stage_ms',{}).get(k) for f in frames]) for k in stages}
    # Frame service time includes queue waiting when acquisition overlaps CPU
    # work. Measure actual source cadence independently in the native clock;
    # intervals across failed requests remain real gaps rather than disappearing.
    metadata = [f['observation']['native_frame'] for f in native.values()]
    callbacks = [m.get('callback_monotonic_us') for m in metadata]
    increasing = (all(number(v) for v in callbacks) and
                  all(b > a for a,b in zip(callbacks,callbacks[1:])))
    latency['native_capture_interval_ms'] = distribution(
        [(b-a)/1000 for a,b in zip(callbacks,callbacks[1:])] if increasing else [])
    latency['native_capture_clock_monotonic'] = increasing if callbacks else None
    for name,start,end in (
            ('native_copy_ms','callback_monotonic_us','copy_finished_monotonic_us'),
            ('native_encode_ms','copy_finished_monotonic_us','encoded_monotonic_us')):
        latency[name] = distribution([(m[end]-m[start])/1000 for m in metadata
            if number(m.get(start)) and number(m.get(end)) and m[end]>=m[start]])
    latency['acquired_to_registration_wait_ms'] = distribution(
        [f.get('acquired_to_registration_wait_ms') for f in native.values()])
    latency['recognition_ms']=None
    latency['model_response_ms']=None
    if signature != (raw.stat().st_size,raw.stat().st_mtime_ns):
        raise ValueError('Raw changed during acceptance; use a completed journal or immutable prefix')
    return {'session_id':state['session_id'],'raw_sha256':hashlib.sha256(payload).hexdigest(),
        'raw_integrity_and_clock_observations_match':integrity,'clock':assessment,
        'clock_history':{k:state.get(k) for k in ('history_merged','history_complete','history_missing_segments','history_window_count')},
        'pen_points':len(pens),'candidate_points':len(candidates),
        'candidate_coverage':len(candidates)/len(pens) if pens else None,
        'extrapolated_candidate_points':len(extrapolated),
        'motion_estimated_points':len(motion),
        'located_candidate_points':len(candidates)+len(extrapolated)+len(motion),
        'located_candidate_coverage':(len(candidates)+len(extrapolated)+len(motion))/len(pens) if pens else None,
        'unresolved_points':len(unresolved),
        'unresolved_reasons':dict(Counter(mapped.get(p['seq'],{}).get('reason') or
            mapped.get(p['seq'],{}).get('status','missing_coordinate_record') for p in unresolved)),
        'last_observed_frame_wall_ms':last_frame,'unresolved_after_last_observed_frame':len(after_observation),
        'strokes':strokes,'complete_strokes':sum(s['location_complete'] for s in strokes),
        'complete_interpolated_strokes':sum(s['location_complete'] and not s['status_counts'].get('extrapolated_candidate') and not s['status_counts'].get('motion_candidate') for s in strokes),
        'complete_strokes_including_motion_estimates':sum(s['location_complete'] and bool(s['status_counts'].get('motion_candidate')) for s in strokes),
        'native_frames':len(native),'native_bracketed_candidate_pen_points':len(native_candidates),
        'native_clock':native_clock,
        'native_backend_with_pen_exercised':bool(native_candidates) and integrity and assessment['usable_candidate'] and native_clock['usable_candidate'],
        'native_backend_full_pen_coverage':bool(pens) and len(native_candidates)==len(pens) and integrity and assessment['usable_candidate'] and native_clock['usable_candidate'],
        'visual_translation_observed':any(e.get('visual_motion_kind')=='translation' for e in episodes),
        'visual_scaling_observed':any(e.get('visual_motion_kind')=='scale' for e in episodes),
        'touch_episodes':episodes,'latency_ms':latency,
        'physical_mm_accuracy_verified':False,'physical_accuracy_reason':'independent_pen_ground_truth_required',
        'raw_modified':False,'screenshots_acquired':0,'tablet_ui_actions':[]}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('raw','clock','geometry','points','output'):p.add_argument('--'+name,type=Path,required=True)
    args=p.parse_args()
    report=audit(args.raw,args.clock,args.geometry,args.points)
    with args.output.open('x',encoding='utf-8') as out:json.dump(report,out,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps({k:v for k,v in report.items() if k not in ('strokes','touch_episodes','latency_ms')},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
