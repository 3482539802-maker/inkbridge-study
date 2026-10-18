"""Bounded offline partial-path correspondence, never an erase transaction.

Position and point order must agree. No free shape fitting, missing-path deletion
inference or automatic edit replay. All distances use the caller's common page
coordinates; same-page/source checks belong to match_saved_ink's verified loader.
"""
import math

import numpy as np


def path_data(points):
    p=np.asarray(points,dtype=float)
    if p.ndim!=2 or p.shape[1]!=2 or not 2<=len(p)<=1024 or not np.isfinite(p).all():
        raise ValueError('Invalid or oversized fragment path')
    p=p[np.r_[True,np.linalg.norm(np.diff(p,axis=0),axis=1)>1e-8]]
    lengths=np.r_[0.,np.cumsum(np.linalg.norm(np.diff(p,axis=0),axis=1))]
    return p,lengths


def project(points,path,lengths):
    a=path[:-1];v=np.diff(path,axis=0)
    u=np.clip(np.sum((points[:,None,:]-a)*v,axis=2)/np.sum(v*v,axis=1),0,1)
    distances=np.linalg.norm(points[:,None,:]-(a+u[:,:,None]*v),axis=2)
    selected=distances.argmin(axis=1);row=np.arange(len(points))
    arcs=lengths[selected]+u[row,selected]*np.diff(lengths)[selected]
    return distances[row,selected],arcs


def fit_fragment(raw,saved,maximum_distance=.75):
    """Return interval evidence only for order-preserving bidirectional fits."""
    if not math.isfinite(maximum_distance) or not 0<maximum_distance<=2:
        raise ValueError('Invalid fragment distance bound')
    a,al=path_data(raw);b,bl=path_data(saved)
    if al[-1]<4 or bl[-1]<4:return None
    if np.any(b.min(axis=0)<a.min(axis=0)-maximum_distance) or np.any(b.max(axis=0)>a.max(axis=0)+maximum_distance):
        return None
    # Fixed sampling keeps the projection matrix below 128 x 1023 segments.
    samples=np.array([np.interp(np.linspace(0,bl[-1],128),bl,b[:,i]) for i in (0,1)]).T
    distance,arcs=project(samples,a,al)
    if max(distance)>maximum_distance:return None
    # Reversed and backtracking paths are not treated as a retained portion.
    if arcs[-1]<=arcs[0] or min(np.diff(arcs))<-.25:return None
    span=arcs[-1]-arcs[0]
    if span<4 or not .8<=bl[-1]/span<=1.2:return None
    inner=np.array([np.interp(np.linspace(arcs[0],arcs[-1],128),al,a[:,i]) for i in (0,1)]).T
    reverse,_=project(inner,b,bl)
    error=float(max(max(distance),max(reverse)))
    if error>maximum_distance:return None
    return {'raw_arc_interval_pdf_pt':[float(arcs[0]),float(arcs[-1])],
            'raw_path_length_pdf_pt':float(al[-1]),'retained_length_fraction_candidate':float(span/al[-1]),
            'bidirectional_max_distance_pdf_pt':error}


def match_fragments(raw,saved,translations=(),whole_pairs=(),maximum_distance=.75,uniqueness_margin=.5):
    if len(raw)>512 or len(saved)>512 or len(translations)>8:
        raise ValueError('Fragment path/hypothesis budget exceeded')
    if not math.isfinite(uniqueness_margin) or not .1<=uniqueness_margin<=2:
        raise ValueError('Invalid fragment uniqueness bound')
    if not math.isfinite(maximum_distance) or not 0<maximum_distance or maximum_distance+uniqueness_margin>2:
        raise ValueError('Invalid fragment distance bound')
    if len({r['start_seq'] for r in raw})!=len(raw) or len({s['index'] for s in saved})!=len(saved):
        raise ValueError('Duplicate fragment source identities')
    hypotheses=[{'name':'unchanged_position','delta':[0.,0.]}]
    for h in translations:
        delta=np.asarray(h['delta'],float)
        if delta.shape!=(2,) or not np.isfinite(delta).all() or type(h['before_seq']) is not int:
            raise ValueError('Invalid fragment translation hypothesis')
        hypotheses.append(h)
    # Validate once, even paths whose bounding boxes will not overlap.
    for p in raw+saved:path_data(p['samples'])
    known={p['saved_index'] for p in whole_pairs}
    whole_raw={p['raw_start_seq'] for p in whole_pairs if 'raw_start_seq' in p}
    found=[];rejected=[]
    for s in saved:
        if s['index'] in known:continue
        choices=[]
        for r in raw:
            for h in hypotheses:
                if 'before_seq' in h and r['end_seq']>=h['before_seq']:continue
                fit=fit_fragment(np.asarray(r['samples'])+h['delta'],s['samples'],maximum_distance+uniqueness_margin)
                if fit is not None:
                    choices.append((fit['bidirectional_max_distance_pdf_pt'],r,h,fit))
        choices.sort(key=lambda p:p[0])
        if not choices or choices[0][0]>maximum_distance:continue
        error,r,h,fit=choices[0]
        if r['start_seq'] in whole_raw:
            rejected.append({'saved_index':s['index'],'reason':'raw_already_has_whole_saved_match'});continue
        if len(choices)>1 and choices[1][0]-error<uniqueness_margin:
            rejected.append({'saved_index':s['index'],'reason':'competing_path_or_translation'});continue
        if fit['retained_length_fraction_candidate']>=.9:continue
        found.append({'raw_start_seq':r['start_seq'],'raw_end_seq':r['end_seq'],'saved_index':s['index'],
                      'hypothesis':h['name'],'translation_pdf_pt_candidate':h['delta'],**fit,
                      'native_identity_verified':False,'erase_operation_verified':False,'edits_applied':False})
    # Multiple disjoint pieces may come from one raw stroke. Overlapping pieces
    # compete; never select one merely because of export/iteration order.
    conflicts=set()
    for i,a in enumerate(found):
        for j,b in enumerate(found[:i]):
            if a['raw_start_seq']!=b['raw_start_seq']:continue
            x,y=a['raw_arc_interval_pdf_pt'],b['raw_arc_interval_pdf_pt']
            if a['hypothesis']!=b['hypothesis'] or min(x[1],y[1])-max(x[0],y[0])>.25:
                conflicts.update((i,j))
    rejected.extend({'saved_index':found[i]['saved_index'],'reason':'overlapping_or_inconsistent_saved_pieces'} for i in sorted(conflicts))
    return {'schema':1,'candidates':[p for i,p in enumerate(found) if i not in conflicts],
            'ambiguities':rejected,'maximum_distance_pdf_pt':maximum_distance,
            'uniqueness_margin_pdf_pt':uniqueness_margin,'minimum_path_length_pdf_pt':4.,
            'maximum_retained_fraction':.9,'native_identity_verified':False,'edits_applied':False,
            'limits':['Partial geometry matches do not establish eraser mode, operation time or object identity.',
                      'Absent intervals are not evidence of deletion; tiny/ambiguous paths remain unknown.',
                      'A saved path can also be a later independently drawn stroke at the same position.']}
