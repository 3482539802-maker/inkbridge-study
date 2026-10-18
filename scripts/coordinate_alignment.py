"""Conservative offline raw/clock/geometry association, independent of OCR.

Outputs candidates, never native Notes objects or verified handwriting locations.
Raw journals remain untouched. No screenshots, device commands or cloud calls.
"""
import argparse
from bisect import bisect_right
from collections import Counter
import json
import math
from pathlib import Path

from ink_protocol import PEN_TOOLS, validate_point


def number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def sample_clock(clock):
    keys = ('monotonic_before_us', 'monotonic_after_us', 'boottime_us', 'unix_ms')
    if not all(number(clock.get(k)) for k in keys):
        raise ValueError('Invalid device clock sample')
    before, after = clock['monotonic_before_us'], clock['monotonic_after_us']
    if after < before or after-before > 20000:
        raise ValueError('Clock sampling uncertainty too large')
    return clock['unix_ms'] - (before+after)/2000, (after-before)/2000


def assess_clock(state):
    """Require multiple callback observations; do not trust a manually set boolean."""
    result = {'usable_candidate': False, 'identity': None, 'verified': False,
              'reason': 'insufficient_event_clock_observations'}
    if state.get('history_lost') or (state.get('history_merged') and not state.get('history_complete')):
        return dict(result, reason='incomplete_clock_history')
    limit = 100000 if state.get('history_merged') else 32
    observations = state.get('observations', [])
    if len(observations) < 3:
        return result
    if len(observations) > limit:
        raise ValueError('Clock observations exceed bounded sidecar format')
    anchors = state.get('anchors', [])
    if len(anchors) < 2 or len(anchors) > limit:
        return dict(result, reason='insufficient_or_oversized_clock_anchors')
    offsets = []
    uncertainty = []
    for c in anchors:
        offset, error = sample_clock(c)
        offsets.append(offset)
        uncertainty.append(error)
    if max(offsets)-min(offsets)>5:
        return dict(result, reason='device_wall_clock_jump_or_suspend')
    previous = None
    lags = []
    for o in observations:
        c = o['callback_clock']
        offset, error = sample_clock(c)
        t, seq = o['action_time_us'], o['seq']
        if not number(t) or not number(seq) or seq < 1:
            raise ValueError('Invalid event clock observation')
        if previous and (t <= previous[0] or seq <= previous[1]):
            return dict(result, reason='event_clock_or_sequence_not_monotonic')
        previous = t, seq
        lag = (c['monotonic_after_us']-t)/1000
        if not 0<=lag<=100 or abs(offset-offsets[0])>5:
            return dict(result, reason='event_clock_identity_or_callback_delay_ambiguous')
        lags.append(lag)
    if any(b['monotonic_before_us'] <= a['monotonic_after_us'] for a,b in zip(anchors,anchors[1:])):
        return dict(result, reason='clock_anchors_not_monotonic')
    low = anchors[0]['monotonic_before_us']
    high = anchors[-1]['monotonic_after_us']
    # Input's first action can precede the first native anchor by microseconds.
    # Validate EVERY observation above (offset/lag/order), and still verify all
    # of them against raw below. Only observations actually covered by anchors
    # count toward calibration. Never extend the usable action-time interval.
    covered = [o for o in observations if o['action_time_us'] >= low]
    if any(not o['action_time_us']<=o['callback_clock']['monotonic_after_us']<=high for o in observations):
        return dict(result, reason='clock_observation_outside_anchors')
    if len(covered)<3:
        return dict(result, reason='insufficient_covered_event_clock_observations')
    if covered[-1]['action_time_us']-covered[0]['action_time_us']<8e6:
        return dict(result, reason='event_clock_observation_span_too_short')
    scope = {}
    if len(covered)!=len(observations):
        early = observations[:len(observations)-len(covered)]
        scope = {'calibration_observation_scope':'actions_within_independent_anchor_interval',
                 'all_validated_observation_count':len(observations),
                 'pre_anchor_observation_count':len(early),
                 'pre_anchor_observation_seq_range':[early[0]['seq'],early[-1]['seq']],
                 'maximum_pre_anchor_action_lead_us':low-early[0]['action_time_us'],
                 'pre_anchor_actions_located':False}
    return dict(result, **scope, usable_candidate=True, identity='CLOCK_MONOTONIC_candidate',
        reason=None, offset_ms=(min(offsets)+max(offsets))/2,
        uncertainty_ms=(max(offsets)-min(offsets))/2+max(uncertainty)+1,
        action_time_range_us=[low,high], observation_count=len(covered),
        callback_lag_ms_range=[min(lags),max(lags)])


def affine(matrix, x, y):
    if (not isinstance(matrix,list) or len(matrix)!=2 or
        any(not isinstance(r,list) or len(r)!=3 or not all(number(n) for n in r) for r in matrix)):
        raise ValueError('Invalid affine matrix')
    return [r[0]*x+r[1]*y+r[2] for r in matrix]


def inside_polygon(hull,x,y):
    if len(hull)<3 or any(len(p)!=2 or not all(number(v) for v in p) for p in hull):
        return False
    # Registration emits a convex hull. No extrapolation allowance here.
    signs=[]
    for a,b in zip(hull,hull[1:]+hull[:1]):
        cross=(b[0]-a[0])*(y-a[1])-(b[1]-a[1])*(x-a[0])
        if abs(cross)>1e-6: signs.append(cross>0)
    return bool(signs) and (all(signs) or not any(signs))


def spatial_support(frame,point,allow_page_margins=False,*,verified_run_margin=False):
    """Separate in-hull interpolation from bounded, explicitly provisional margins.

    Rectangles select/veto a page; only the visual affine supplies coordinates.
    A margin estimate is never promoted to an ordinary candidate.
    """
    r=frame['registration']
    hull=r['support_hull_screen_px']
    x,y=point['x'],point['y']
    if inside_polygon(hull,x,y):return {'kind':'interpolation','allowed':True,'distance_px':0.}
    rejected={'kind':'outside_support','allowed':False,'reason':'outside_feature_support'}
    binding=r.get('native_background_binding',{})
    c=frame['observation']['context']
    layers=c.get('page_layers',[])
    if (len(layers)!=1 or binding.get('page_uuid')!=layers[0].get('page_id') or
        binding.get('source_pdf_sha256')!=r['source_pdf_sha256'] or
        binding.get('pdf_page_1based')!=r['pdf_page_1based'] or
        not binding.get('source_hinote_sha256') or not binding.get('note_id')):
        return rejected
    rect=layers[0].get('unclipped_window_bounds')
    size=r.get('pdf_size_pt')
    screen=r.get('screen_size')
    if (not isinstance(rect,list) or len(rect)!=4 or not all(number(v) for v in rect) or
        not isinstance(size,list) or len(size)!=2 or not all(number(v) and v>0 for v in size) or
        not isinstance(screen,list) or len(screen)!=2 or not all(number(v) and v>0 for v in screen)):
        return rejected
    pdf=affine(r['screen_to_pdf'],x,y)
    if (not rect[0]<=x<=rect[2] or not rect[1]<=y<=rect[3] or
        not all(0<=p<=extent for p,extent in zip(pdf,size))):
        return dict(rejected,reason='outside_bound_page',
                    native_page_rectangle_px=rect,pdf_size_pt=size,
                    native_page_rectangle_used_for_transform=False)
    if not allow_page_margins or not 0<=x<=screen[0] or not 0<=y<=screen[1]:return rejected
    spans=[max(p[i] for p in hull)-min(p[i] for p in hull) for i in (0,1)]
    limit=(1. if verified_run_margin else .5)*min(spans)
    if limit<50:return rejected
    distances=[]
    for a,b in zip(hull,hull[1:]+hull[:1]):
        dx,dy=b[0]-a[0],b[1]-a[1]
        denominator=dx*dx+dy*dy
        fraction=max(0,min(1,((x-a[0])*dx+(y-a[1])*dy)/denominator)) if denominator else 0
        distances.append(math.hypot(x-a[0]-fraction*dx,y-a[1]-fraction*dy))
    distance=min(distances)
    if distance>limit:return rejected
    if verified_run_margin:
        # An empirical affine extrapolation indicator, NOT an accuracy bound.
        # This branch is used only by a three-exposure stable run below.
        residual=r.get('heldout_inlier_p90_error_screen_px')
        amplified=residual*(1+distance/min(spans)) if number(residual) and residual>=0 else None
        if amplified is None or amplified>2:return rejected
        return {'kind':'verified_run_page_margin_extrapolation','allowed':True,
            'distance_px':distance,'maximum_distance_px':limit,
            'heldout_p90_amplified_indicator_px':amplified,'indicator_is_accuracy_bound':False,
            'native_page_rectangle_used_for_transform':False,'accuracy_verified':False}
    return {'kind':'bounded_page_margin_extrapolation','allowed':True,
            'distance_px':distance,'maximum_distance_px':limit,
            'native_page_rectangle_used_for_transform':False,'accuracy_verified':False}


def verified_run_support(pair,point,allow_page_margins):
    """Extra margin evidence requires three stable exposures and stricter drift.

    Never overrides input barriers, page bounds, binding, or failed observations.
    Ordinary two-frame support remains the first choice and is unchanged.
    """
    middle=pair.get('intermediate_frames',[])
    if (not allow_page_margins or pair['blocked'] or len(middle)!=1 or pair['drift_px']>.5):return None
    frames=[pair['a'],pair['b'],*middle]
    for frame in frames:
        r=frame['registration'];binding=r.get('native_background_binding',{})
        layers=frame['observation']['context'].get('page_layers',[])
        if (len(layers)!=1 or binding.get('page_uuid')!=layers[0].get('page_id') or
                binding.get('source_pdf_sha256')!=r['source_pdf_sha256'] or
                binding.get('pdf_page_1based')!=r['pdf_page_1based'] or
                not binding.get('source_hinote_sha256') or not binding.get('note_id')):return None
    support=[spatial_support(f,point,True,verified_run_margin=True) for f in frames]
    return support if all(s['allowed'] for s in support) else None


def valid_frame(row):
    o,r=row.get('observation',{}),row.get('registration',{})
    interval=o.get('device_capture_wall_interval_ms')
    if not o.get('valid') or not r.get('valid') or not o.get('window',{}).get('focused'):
        return False
    if (not isinstance(interval,list) or len(interval)!=2 or not all(number(t) for t in interval)
            or not 0<=interval[1]-interval[0]<=1000):
        return False
    c=o.get('context',{})
    if any(not number(o.get('window',{}).get(k)) for k in ('pid','window_id','display_id')):
        return False
    if not c.get('active_tab_marker_unique') or not c.get('document_title_candidate') or len(c.get('page_layers',[]))!=1:
        return False
    if not c['page_layers'][0].get('page_id') or not isinstance(r.get('source_pdf_sha256'),str) or len(r['source_pdf_sha256'])!=64:
        return False
    if not isinstance(r.get('pdf_page_1based'),int) or r['pdf_page_1based']<1:
        return False
    if not number(r.get('heldout_median_error_screen_px')) or r['heldout_median_error_screen_px']>2:
        return False
    return True


def identity(frame):
    o,r=frame['observation'],frame['registration']
    w,c=o['window'],o['context']
    return (w.get('pid'),w.get('window_id'),w.get('display_id'),c['document_title_candidate'],
        c['page_layers'],c.get('visible_page_layers'),r['source_pdf_sha256'],r['pdf_page_1based'],r.get('screen_size'),
        r.get('pixel_geometry_version',1))


def page_identity(frame):
    o,r=frame['observation'],frame['registration']
    w,c=o['window'],o['context']
    return (w['pid'],w['window_id'],w['display_id'],c['document_title_candidate'],
            tuple(p['page_id'] for p in c['page_layers']),r['source_pdf_sha256'],
            r['pdf_page_1based'],tuple(r['screen_size']),r.get('pixel_geometry_version',1))


def compatible_context(a,b):
    if identity(a)==identity(b):return True
    ra,rb=a['registration'],b['registration']
    ba,bb=ra.get('native_background_binding'),rb.get('native_background_binding')
    if not ba or not bb or ba!=bb or page_identity(a)!=page_identity(b):return False
    from coordinate_page_binding import rectangle
    ca,cb=a['observation']['context'],b['observation']['context']
    try:
        rectangles=[rectangle(c['page_layers'][0]) for c in (ca,cb)]
        if max(abs(x-y) for x,y in zip(*rectangles))>.25:return False
        visible={p['page_id']:p for p in ca.get('visible_page_layers',ca['page_layers'])}
        for p in cb.get('visible_page_layers',cb['page_layers']):
            if p['page_id'] in visible and max(abs(x-y) for x,y in
                zip(rectangle(visible[p['page_id']]),rectangle(p)))>.25:return False
    except (ValueError,KeyError,TypeError):return False
    return True


def stable_pair(a,b,max_span_ms=4000):
    if not valid_frame(a) or not valid_frame(b) or not compatible_context(a,b):
        return None
    lo,hi=a['observation']['device_capture_wall_interval_ms'],b['observation']['device_capture_wall_interval_ms']
    if not lo[1]<hi[0] or hi[1]-lo[0]>max_span_ms:
        return None
    ra,rb=a['registration'],b['registration']
    size=ra['screen_size']
    if not isinstance(size,list) or len(size)!=2 or not all(number(n) and n>0 for n in size):
        return None
    drift=0
    for x,y in [(0,0),(size[0],0),(0,size[1]),tuple(size)]:
        pa,pb=affine(ra['screen_to_pdf'],x,y),affine(rb['screen_to_pdf'],x,y)
        if math.dist(affine(ra['pdf_to_screen'],*pa),[x,y])>1: return None
        if math.dist(affine(rb['pdf_to_screen'],*pb),[x,y])>1: return None
        drift=max(drift,math.dist(affine(ra['pdf_to_screen'],*pb),[x,y]),
                  math.dist(affine(rb['pdf_to_screen'],*pa),[x,y]))
    if drift>2: return None
    return {'a':a,'b':b,'start':lo[0],'end':hi[1],
            'gap_start':lo[1],'gap_end':hi[0],'blocked':False,'drift_px':drift}


def prepare_pairs(frames, stable_runs=False):
    from multipage_geometry import REASON, page_frames
    if not any(f.get('registration',{}).get('reason')==REASON for f in frames):
        return prepare_single_page_pairs(frames,stable_runs)
    if len(frames)>3600:raise ValueError('Use at most 3600 geometry observations per analysis')
    # Keep only contiguous runs for each identity. Missing/failed observations
    # close a run immediately, so a revisited page cannot bridge the gap.
    # Storage is bounded by observed children (12 per frame), not by every
    # distinct page multiplied by the entire geometry window.
    lanes={};pairs=[]
    for f in frames:
        children={page_identity(c):c for c in page_frames(f) if valid_frame(c)}
        if len(children)>12:raise ValueError('Too many simultaneous page candidates')
        for key in list(lanes):
            if key not in children:
                pairs.extend(prepare_single_page_pairs(lanes.pop(key),stable_runs))
        for key,child in children.items():lanes.setdefault(key,[]).append(child)
    for lane in lanes.values():
        pairs.extend(prepare_single_page_pairs(lane,stable_runs))
    return sorted(pairs,key=lambda p:(p['start'],p['end'],page_identity(p['a'])))


def prepare_single_page_pairs(frames, stable_runs=False):
    if len(frames)>3600:
        raise ValueError('Use at most 3600 geometry observations per analysis')
    pairs=[p for a,b in zip(frames,frames[1:]) if (p:=stable_pair(a,b))]
    if any(b['start']<=a['start'] for a,b in zip(pairs,pairs[1:])):
        raise ValueError('Geometry observations are not chronological')
    if stable_runs:
        # Only bridge one VERIFIED middle observation, never an invalid/context
        # transition. Outer exposures stay strictly outside the pen timestamp.
        # Every adjacent pair and the outer transform must pass the same 2px
        # test. Raw barriers below cover the ENTIRE three-frame interval.
        adjacent={(id(p['a']),id(p['b'])):p for p in pairs}
        extended=[]
        for a,middle,b in zip(frames,frames[1:],frames[2:]):
            left=adjacent.get((id(a),id(middle)))
            right=adjacent.get((id(middle),id(b)))
            if not left or not right: continue
            outer=stable_pair(a,b,max_span_ms=6000)
            if not outer: continue
            outer.update(drift_px=max(left['drift_px'],right['drift_px'],outer['drift_px']),
                         intermediate_frames=[middle])
            extended.append(outer)
        pairs.extend(extended)
        pairs.sort(key=lambda p:(p['start'],p['end']))
    return pairs


def journal(path,session):
    with path.open('rb') as f:
        while line:=f.readline(8193):
            if len(line)>8192 or not line.endswith(b'\n'):
                raise ValueError('Incomplete or oversized raw record; analyze a completed journal')
            p=validate_point(json.loads(line))
            if p.get('session_id')!=session:
                raise ValueError('Clock sidecar session does not match raw journal')
            yield p


def point_time(p,clock):
    t=p['action_time']
    if not clock['action_time_range_us'][0]<=t<=clock['action_time_range_us'][1]:
        return None
    return t/1000+clock['offset_ms']


def align(raw,sidecar,frames,output,stable_runs=False,allow_page_margins=False,*,prefix_context=None):
    from multipage_geometry import page_frames
    # Internal incremental caller supplies a validated durable index of the
    # omitted prefix. It must check ALL clock observations against canonical
    # raw and carry held contacts across the window boundary. CLI never accepts
    # an unchecked user flag to bypass these gates.
    signature=(raw.stat().st_size,raw.stat().st_mtime_ns)
    clock=assess_clock(sidecar)
    pairs=prepare_pairs(frames,stable_runs=stable_runs)
    starts=[pair['start'] for pair in pairs]
    reasons=Counter()
    count=0
    if clock['usable_candidate']:
        # First streaming pass invalidates intervals crossed by any non-pen input,
        # gap, cancel or foreign window. Fingers never predict document displacement.
        raw_previous=prefix_context.get('previous') if prefix_context else None
        previous=(raw_previous[0],raw_previous[1]/1000+clock['offset_ms']) if raw_previous else None
        integrity_bad=bool(prefix_context and prefix_context.get('integrity_bad'))
        active_contacts={tuple(k):t/1000+clock['offset_ms'] for k,t in
                         prefix_context.get('contacts',[])} if prefix_context else {}
        expected={o['seq']:o['action_time_us'] for o in sidecar['observations']}
        verified=prefix_context.get('observations',{}) if prefix_context else {}
        found={seq for seq,t in verified.items() if expected.get(seq)==t}
        for p in journal(raw,sidecar['session_id']):
            if p['seq'] in expected:
                if p['action_time']!=expected[p['seq']]: integrity_bad=True
                found.add(p['seq'])
            if raw_previous and (p['seq']!=raw_previous[0]+1 or p['action_time']<raw_previous[1]):
                integrity_bad=True
            raw_previous=p['seq'],p['action_time']
            t=point_time(p,clock)
            if t is None:
                # A finger held before coverage starts can still move the
                # page later. Preserve that raw contact without assigning it
                # a calibrated time or any page coordinates.
                if p['action_time']<clock['action_time_range_us'][0] and p['tool_type'] not in PEN_TOOLS:
                    key=(p['display_id'],p['window_id'],p['pointer_id'])
                    if p['action'] in (0,3): active_contacts.pop(key,None)
                    else: active_contacts.setdefault(key,float('-inf'))
                continue
            error=clock['uncertainty_ms']
            key=(p['display_id'],p['window_id'],p['pointer_id'])
            # A held finger/eraser remains a barrier even when no MOVE event
            # arrives between two frames. Never infer release from silence.
            if p['tool_type'] not in PEN_TOOLS:
                active_contacts.setdefault(key,t)
                contact_start=active_contacts[key]
                for pair in pairs:
                    if contact_start-error<=pair['end'] and t+error>=pair['start']:
                        pair['blocked']=True
                if p['action'] in (0,3): active_contacts.pop(key,None)
            lo=min(t,previous[1]) if previous else t
            for pair in pairs[bisect_right(starts,lo-(6000 if stable_runs else 4000)):bisect_right(starts,max(t,previous[1] if previous else t))]:
                w=pair['a']['observation']['window']
                lo=min(t,previous[1]) if previous else t
                discontinuity=previous and (p['seq']!=previous[0]+1 or t<previous[1])
                overlap=lo-error<=pair['end'] and t+error>=pair['start']
                wrong_window=(p['window_id'],p['display_id'])!=(w['window_id'],w['display_id'])
                if overlap and (discontinuity or (pair['start']-error<=t<=pair['end']+error and
                    (p['tool_type'] not in PEN_TOOLS or p['action']==0 or wrong_window))):
                    pair['blocked']=True
            previous=p['seq'],t
        for contact_start in active_contacts.values():
            for pair in pairs:
                if pair['end']>=contact_start-clock['uncertainty_ms']: pair['blocked']=True
        if integrity_bad or len(found)!=len(expected):
            clock=dict(clock,usable_candidate=False,reason='raw_sequence_clock_or_observation_mismatch')
    if signature!=(raw.stat().st_size,raw.stat().st_mtime_ns):
        raise ValueError('Raw journal changed during analysis')
    motion=None
    if clock['usable_candidate'] and any(f.get('observation',{}).get('independent_page_exposure_requested') for f in frames):
        from motion_coordinates import MotionCoordinates
        motion=MotionCoordinates(frames,clock,journal(raw,sidecar['session_id']),prefix_context,allow_page_margins)
    def recover_motion(result,point):
        if (motion is not None and result.get('pdf_xy') is None and
            result.get('reason') in ('no_bracketing_geometry','input_motion_or_context_barrier')):
            candidate=motion.locate(point)
            if candidate:result.update(candidate)
    with output.open('x',encoding='utf-8') as out:
        for p in journal(raw,sidecar['session_id']):
            if p['tool_type'] not in PEN_TOOLS or p['action']==0:
                continue
            count+=1
            result={'session_id':p['session_id'],'seq':p['seq'],'screen_xy':[p['x'],p['y']],
                    'status':'unresolved','reason':clock['reason'],'pdf_xy':None,
                    'action_time_us':p['action_time'],'action':p['action'],
                    'pointer_id':p['pointer_id'],'window_id':p['window_id'],'display_id':p['display_id'],
                    'coordinate_schema_version':2}
            if clock['usable_candidate']:
                t=point_time(p,clock)
                error=clock['uncertainty_ms']
                candidates=[] if t is None else [pair for pair in pairs[bisect_right(starts,t-(6000 if stable_runs else 4000)):bisect_right(starts,t)]
                    if pair['gap_start']+error<t<pair['gap_end']-error]
                result['reason']='no_bracketing_geometry'
                if t is None and clock.get('pre_anchor_observation_count'):
                    result['reason']='outside_independent_clock_coverage'
                if candidates:
                    if all(pair['blocked'] for pair in candidates):
                        result['reason']='input_motion_or_context_barrier'
                        recover_motion(result,p)
                        reasons[result['reason'] or result['status']]+=1
                        out.write(json.dumps(result,allow_nan=False)+'\n')
                        continue
                    overrides={}
                    def support_for(pair):
                        return overrides.get(id(pair)) or [spatial_support(f,p,allow_page_margins) for f in
                            [pair['a'],pair['b']]+pair.get('intermediate_frames',[])]
                    # Only consider the extra margin evidence if ordinary
                    # unblocked support cannot already locate this point.
                    if not any(not q['blocked'] and all(s['allowed'] for s in support_for(q)) for q in candidates):
                        for q in candidates:
                            expanded=verified_run_support(q,p,allow_page_margins)
                            if expanded:overrides[id(q)]=expanded
                        if overrides:candidates=[q for q in candidates if id(q) in overrides]
                    # Two visible pages can both have independent stable fits.
                    # Select only unanimous spatial support in every exposure;
                    # a page's UI rectangle never constructs the transform.
                    if len({page_identity(p['a']) for p in candidates})>1:
                        supported=[pair for pair in candidates if all(s['allowed'] for s in support_for(pair))]
                        if supported:candidates=supported
                        else:
                            unblocked=[pair for pair in candidates if not pair['blocked']]
                            excluded=[]
                            for pair in unblocked:
                                support=support_for(pair)
                                if not all(s.get('reason')=='outside_bound_page' for s in support):break
                                a=pair['a']
                                excluded.append({'page_uuid':a['observation']['context']['page_layers'][0]['page_id'],
                                    'pdf_page_1based':a['registration']['pdf_page_1based'],
                                    'source_pdf_sha256':a['registration']['source_pdf_sha256'],
                                    'geometry_indices':[pair[k].get('index') for k in ('a','b')],
                                    'intermediate_verified_geometry_indices':[f.get('index') for f in pair.get('intermediate_frames',[])],
                                    'spatial_support':support})
                            else:
                                if unblocked and {page_identity(q['a']) for q in unblocked}=={page_identity(q['a']) for q in candidates}:
                                    result['reason']='outside_bound_page'
                                    result['evidence']={'all_bracketed_pages_excluded':True,'excluded_pages':excluded,
                                        'page_identity_selected':False,'native_notes_object_verified':False}
                                    reasons[result['reason']]+=1
                                    out.write(json.dumps(result,allow_nan=False)+'\n')
                                    continue
                    # Prefer the narrowest unblocked compatible interval. All
                    # candidates for a point must agree on page/window/context.
                    if len({page_identity(p['a']) for p in candidates})>1:
                        result['reason']='ambiguous_bracketing_context'
                        reasons[result['reason']]+=1
                        out.write(json.dumps(result,allow_nan=False)+'\n')
                        continue
                    pair=min(candidates,key=lambda p:(p['blocked'],p['end']-p['start']))
                    a,b=pair['a']['registration'],pair['b']['registration']
                    support=support_for(pair)
                    if pair['blocked']: result['reason']='input_motion_or_context_barrier'
                    elif not all(s['allowed'] for s in support):
                        # Classify only unanimous page exclusion. A disagreeing
                        # bracket remains unknown, never a UI/handwriting claim.
                        result['reason']=('outside_bound_page' if all(s.get('reason')=='outside_bound_page' for s in support)
                                          else 'outside_feature_support')
                        result['evidence']={'geometry_indices':[pair[k].get('index') for k in ('a','b')],
                            'spatial_support':support,'page_uuid':pair['a']['observation']['context']['page_layers'][0]['page_id'],
                            'pdf_page_1based':a['pdf_page_1based'],'source_pdf_sha256':a['source_pdf_sha256']}
                    else:
                        first=affine(a['screen_to_pdf'],p['x'],p['y'])
                        second=affine(b['screen_to_pdf'],p['x'],p['y'])
                        mean=[(x+y)/2 for x,y in zip(first,second)]
                        # Symmetric two-frame estimate reduces sensitivity to a
                        # single frame. Its spread is evidence, not a true bound.
                        residual=max(r.get('heldout_inlier_p90_error_screen_px',r['heldout_median_error_screen_px']) for r in (a,b))
                        observed_spread=math.dist(first,second)
                        inverse_scale=max(math.hypot(*r['screen_to_pdf'][0][:2])+math.hypot(*r['screen_to_pdf'][1][:2]) for r in (a,b))
                        indicator=(residual+.5+pair['drift_px']/2)*inverse_scale
                        extrapolated=any(s['kind']!='interpolation' for s in support)
                        result.update(status='extrapolated_candidate' if extrapolated else 'candidate',reason=None,pdf_xy=mean,
                            device_wall_ms_candidate=t,pdf_page_1based=a['pdf_page_1based'],
                            source_pdf_sha256=a['source_pdf_sha256'],page_uuid=pair['a']['observation']['context']['page_layers'][0]['page_id'],
                            bracket_device_wall_ms=[pair['start'],pair['end']],clock_uncertainty_ms=error,
                            native_notes_object_verified=False,
                            pdf_xy_from_brackets=[first,second],frame_disagreement_pdf_pt=observed_spread,
                            pdf_normalized_xy=[mean[i]/a['pdf_size_pt'][i] for i in (0,1)] if 'pdf_size_pt' in a else None,
                            pdf_size_pt=a.get('pdf_size_pt'),
                            empirical_error_indicator_pdf_pt=indicator,
                            error_indicator_is_accuracy_bound=False,
                            unmeasured_error_sources=['digitizer_to_rendered_ink','Notes_smoothing_and_persistence','unobserved_motion'],
                            evidence={'method':'raw_pen_with_two_bracketing_pdf_registrations',
                                'geometry_version':a.get('pixel_geometry_version',1),
                                'geometry_indices':[pair[k].get('index') for k in ('a','b')],
                                'intermediate_verified_geometry_indices':[f.get('index') for f in pair.get('intermediate_frames',[])],
                                'stable_run_bracketing':bool(pair.get('intermediate_frames')),
                                'spatial_support':support,'page_margin_extrapolated':extrapolated,
                                'exposure_intervals_ms':[pair[k]['observation']['device_capture_wall_interval_ms'] for k in ('a','b')],
                                'pixel_refinement':[r.get('pixel_refinement') for r in (a,b)],
                                'page_selection':pair['a']['observation']['context'].get('page_selection'),
                                  'native_background_binding':[r.get('native_background_binding') for r in (a,b)],
                                'independent_of_ocr':True})
            recover_motion(result,p)
            reasons[result['reason'] or result['status']]+=1
            out.write(json.dumps(result,allow_nan=False)+'\n')
    if signature!=(raw.stat().st_size,raw.stat().st_mtime_ns):
        output.unlink()
        raise ValueError('Raw journal changed during analysis; discarded derived output')
    return {'clock':clock,'pen_points':count,'results':dict(reasons),'geometry_pairs':len(pairs),
            'stable_runs_enabled':stable_runs,'stable_run_pairs':sum(bool(p.get('intermediate_frames')) for p in pairs),
            'page_margins_enabled':allow_page_margins,
            'blocked_pairs':sum(p['blocked'] for p in pairs),'raw_modified':False,
            'geometry_observations':len(frames),
            'valid_geometry_observations':sum(any(valid_frame(c) for c in page_frames(f)) for f in frames),
            'note':'Candidates use clock observations and stable bracketing frames; unobserved motion/undo/persistence remain unverified.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw',type=Path,required=True)
    parser.add_argument('--clock',type=Path,required=True)
    parser.add_argument('--geometry',type=Path,required=True)
    parser.add_argument('--hinote',type=Path,help='Optional hash-verified export to disambiguate multiple visible pages')
    parser.add_argument('--pdf',type=Path,help='Original PDF; required with --hinote for hash/page-count verification')
    parser.add_argument('--output',type=Path,required=True,help='New directory')
    parser.add_argument('--stable-runs',action='store_true',help='Allow validated three-frame stable runs, at most 6s; never bridge failed observations')
    parser.add_argument('--page-margins',action='store_true',help='Separate provisional page-margin coordinates, only with native hash/UUID binding')
    args=parser.parse_args()
    if args.page_margins and not args.hinote:parser.error('--hinote is required with --page-margins')
    if args.clock.stat().st_size>128*1024 or args.geometry.stat().st_size>12*1024*1024:
        parser.error('Sidecar or geometry exceeds bounded size')
    metadata=args.raw.with_suffix('.session.json')
    if metadata.exists() and json.loads(metadata.read_text(encoding='utf-8')).get('state') not in ('completed','interrupted'):
        parser.error('Analyze a stopped raw session; recording is still active')
    args.output.mkdir(parents=True,exist_ok=False)
    from capture_clock_history import load_clock
    sidecar=load_clock(args.clock)
    frames=[json.loads(line) for line in args.geometry.read_text(encoding='utf-8').splitlines()]
    if args.hinote:
        from coordinate_page_binding import load_binding
        from multipage_geometry import rebind_pages
        if not args.pdf:parser.error('--pdf is required with --hinote')
        binding=load_binding(args.hinote,args.pdf)
        frames=[rebind_pages(frame,binding) for frame in frames]
        (args.output/'bound-geometry.jsonl').write_bytes(('\n'.join(json.dumps(f,ensure_ascii=False) for f in frames)+'\n').encode('utf-8'))
    summary=align(args.raw,sidecar,frames,args.output/'points.jsonl',stable_runs=args.stable_runs,allow_page_margins=args.page_margins)
    summary['sources']={k:str(getattr(args,k).resolve()) for k in ('raw','clock','geometry')}
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
