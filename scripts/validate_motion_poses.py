"""Hide real middle-frame PDF fits and validate flow-predicted exposure poses.

Only numeric records are read. This is an image-geometry cross-check, not a
pen-coordinate source, physical accuracy bound, or motion-between-frames proof.
"""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import cv2
import numpy as np

from audit_frame_runtime import quantiles
from page_motion import bound_fits
from visual_registration import compose, project


def include_diagnostic_exposures(rows):
    """Explicit research view; never write these rows into a coordinate store."""
    from copy import deepcopy
    out = deepcopy(rows)
    for row in out:
        diagnostic = row.get('motion_exposure_validation') or {}
        if not row.get('observation', {}).get('valid') and diagnostic.get('checked'):
            exposure = diagnostic['exposure']
            row['observation'] = deepcopy(exposure['observation'])
            row['registration'] = deepcopy(exposure['registration'])
    return out


def link(a,b):
    oa,ob=a['observation'],b['observation'];ma,mb=oa.get('native_frame',{}),ob.get('native_frame',{})
    hint=ob.get('page_motion',{});generation=oa.get('motion_context_identity_generation_range')
    if (not generation or generation[0]!=generation[1] or
        generation!=ob.get('motion_context_identity_generation_range') or oa.get('window')!=ob.get('window') or
        not ma.get('request_id') or hint.get('previous_request_id')!=ma['request_id'] or
        hint.get('current_request_id')!=mb.get('request_id') or
        hint.get('previous_timestamp_raw')!=ma.get('timestamp_raw') or
        hint.get('current_timestamp_raw')!=mb.get('timestamp_raw') or
        not 0<mb.get('timestamp_raw',0)-ma.get('timestamp_raw',0)<=1_200_000_000):return {}
    return {p['page_uuid']:p for p in hint.get('pages',[]) if p.get('candidate')}


def common_support(hulls):
    hull=cv2.convexHull(np.float32(hulls[0]))
    for other in hulls[1:]:
        area,intersection=cv2.intersectConvexConvex(hull,cv2.convexHull(np.float32(other)))
        if intersection is None or area<10000:return None
        hull=intersection
    return hull.reshape(-1,2).astype(float)


def validate(rows):
    results=[];skips=Counter()
    for a,m,b in zip(rows,rows[1:],rows[2:]):
        old,middle,new=bound_fits(a),bound_fits(m),bound_fits(b)
        left,right=link(a,m),link(m,b)
        if not left or not right:
            skips['identity_time_or_tracking_barrier']+=1;continue
        if b['observation']['native_frame']['timestamp_raw']-a['observation']['native_frame']['timestamp_raw']>2_000_000_000:
            skips['span_over_two_seconds']+=1;continue
        keys=set(old)&set(middle)&set(new)
        if not keys:skips['independent_midpoint_or_bound_endpoints_unavailable']+=1
        for key in keys:
            uuid=key[0]
            if uuid not in left or uuid not in right:
                skips['page_tracking_unavailable']+=1;continue
            fa,fm,fb=old[key],middle[key],new[key]
            l,r=left[uuid],right[uuid]
            lm=np.asarray(l['previous_screen_to_current_screen'],float)
            rm=np.asarray(r['previous_screen_to_current_screen'],float)
            # Support only where both flow links and all three independent
            # fits have observed features. Never score extrapolated regions.
            hull=common_support([project(lm,l['support_hull_previous_screen_px']),
                r['support_hull_previous_screen_px'],fm['support_hull_screen_px'],
                project(lm,fa['support_hull_screen_px']),
                project(cv2.invertAffineTransform(rm),fb['support_hull_screen_px'])])
            if hull is None:skips['insufficient_common_feature_support']+=1;continue
            # Neither prediction uses the withheld middle-frame transform.
            from_left=compose(lm,fa['pdf_to_screen'])
            from_right=compose(cv2.invertAffineTransform(rm),fb['pdf_to_screen'])
            reference=np.asarray(fm['pdf_to_screen'],float)
            pdf=project(fm['screen_to_pdf'],hull)
            truth=project(reference,pdf)
            errors=[float(np.max(np.linalg.norm(project(pred,pdf)-truth,axis=1)))
                    for pred in (from_left,from_right)]
            movements=[float(np.max(np.linalg.norm(project(matrix,support)-support,axis=1)))
                for matrix,support in ((lm,project(cv2.invertAffineTransform(lm),hull)),(rm,hull))]
            results.append(dict(geometry_indices=[a['index'],m['index'],b['index']],page_uuid=uuid,
                pdf_page_1based=key[3],motion='moving_over_2_screen_px' if max(movements)>2 else 'still_or_subthreshold',
                maximum_link_movement_screen_px=max(movements),
                left_error_screen_px=errors[0],right_error_screen_px=errors[1],
                predictions_agree_with_withheld_fit=bool(max(errors)<=2),
                common_support_area_screen_px2=float(cv2.contourArea(np.float32(hull)))))
    groups={}
    for label in ('moving_over_2_screen_px','still_or_subthreshold'):
        selected=[r for r in results if r['motion']==label]
        groups[label]=dict(checked_pages=len(selected),agreeing_pages=sum(r['predictions_agree_with_withheld_fit'] for r in selected),
            worst_direction_error_screen_px=quantiles([max(r['left_error_screen_px'],r['right_error_screen_px']) for r in selected]))
    return dict(schema=1,groups=groups,skips=dict(skips),checks=results,
        middle_fit_used_for_prediction=False,coordinates_produced=False,
        coordinate_mapping_allowed=False,intermediate_pen_positions_verified=False,
        physical_accuracy_verified=False,screenshots_acquired=0,tablet_actions=[])


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--diagnostic-exposures',action='store_true',
                   help='Research view only: include independent fits on invalid moving frames')
    args=p.parse_args()
    with closing(sqlite3.connect((args.directory/'pipeline.sqlite').resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        records=db.execute('SELECT payload FROM geometry ORDER BY id LIMIT 1025').fetchall()
    if len(records)>1024:p.error('Bounded validation supports at most 1024 geometry rows')
    rows=[json.loads(p) for p, in records]
    result=validate(include_diagnostic_exposures(rows) if args.diagnostic_exposures else rows)
    result['includes_diagnostic_exposures']=args.diagnostic_exposures
    with args.output.open('x',encoding='utf-8') as f:json.dump(result,f,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='checks'},indent=2))


if __name__=='__main__':main()
