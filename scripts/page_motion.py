"""Bounded per-page Lucas-Kanade motion evidence, independent of ink coordinates.

Uses OpenCV's forward/backward sparse-flow pattern and RANSAC similarity fit.
Coordinates require independent original exposures in addition to these
estimates. A single temporary gray frame expires automatically; only numeric
evidence leaves this object.
"""
from copy import deepcopy
import math
import threading
import time

import cv2
import numpy as np

from coordinate_page_binding import rectangle
from visual_registration import compose,project,resize_to_original

METHOD='opencv_pyramidal_lk_forward_backward_ransac_similarity'


def rejected(reason,**extra):
    return dict(candidate=False,reason=reason,**extra)


def estimate(a,b,points,region,screen_map):
    """Fit disjoint training/validation tracks; never reuse validation in RANSAC."""
    if points is None or len(points)<40:return rejected('insufficient_page_features')
    lk=dict(winSize=(21,21),maxLevel=3,criteria=(cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_COUNT,25,.01))
    p1,s1,_=cv2.calcOpticalFlowPyrLK(a,b,points,None,**lk)
    if p1 is None:return rejected('forward_tracking_failed')
    p0r,s0,_=cv2.calcOpticalFlowPyrLK(b,a,p1,None,**lk)
    if p0r is None:return rejected('backward_tracking_failed')
    p0=points.reshape(-1,2);p1=p1.reshape(-1,2);p0r=p0r.reshape(-1,2)
    unit=float(np.max(np.linalg.svd(screen_map[:,:2],compute_uv=False)))
    fb=np.linalg.norm(p0-p0r,axis=1)*unit
    q=project(screen_map,p1);x0,y0,x1,y1=region
    keep=(s1.reshape(-1)==1)&(s0.reshape(-1)==1)&np.isfinite(p1).all(axis=1)&np.isfinite(p0r).all(axis=1)&(fb<=1.)
    keep&=(q[:,0]>x0+12)&(q[:,0]<x1-12)&(q[:,1]>y0+12)&(q[:,1]<y1-12)
    original=np.flatnonzero(keep);p0=p0[keep];p1=p1[keep]
    if len(p0)<40 or len(p0)<len(points)*.55:
        return rejected('insufficient_bidirectional_tracks',features=len(points),bidirectional_tracks=len(p0))
    # Spatially distributed feature extraction plus a disjoint validation set.
    held=original%4==0
    if sum(held)<8 or sum(~held)<24:return rejected('insufficient_validation_tracks')
    matrix,mask=cv2.estimateAffinePartial2D(p0[~held],p1[~held],method=cv2.RANSAC,
        ransacReprojThreshold=2/unit,maxIters=2000,confidence=.995,refineIters=10)
    if matrix is None or not np.isfinite(matrix).all():return rejected('motion_fit_failed')
    scale=math.hypot(matrix[0,0],matrix[1,0]);angle=math.degrees(math.atan2(matrix[1,0],matrix[0,0]))
    if not .8<=scale<=1.25 or abs(angle)>1:return rejected('motion_outside_similarity_limits')
    err=np.linalg.norm(project(matrix,p0[held])-p1[held],axis=1)*unit
    inliers=(np.linalg.norm(project(matrix,p0)-p1,axis=1)*unit<=2)
    train=float(np.mean(mask));validation=float(np.mean(err<=2))
    if train<.75 or validation<.8 or np.median(err)>.8 or np.percentile(err,90)>2:
        return rejected('motion_validation_failed',training_inlier_fraction=train,validation_inlier_fraction=validation,
            validation_median_screen_px=float(np.median(err)),validation_p90_screen_px=float(np.percentile(err,90)),
            training_tracks=int(sum(~held)),validation_tracks=int(sum(held)))
    source=project(screen_map,p0[inliers]);target=project(screen_map,p1[inliers])
    hull=cv2.convexHull(np.float32(source)).reshape(-1,2)
    area=float(cv2.contourArea(hull));span=np.ptp(source,axis=0)
    if min(span)<100 or area<max(12000,.025*(x1-x0)*(y1-y0)):
        return rejected('motion_support_too_local')
    # Both source and destination must span the page: an overlay is not a page.
    if min(np.ptp(target,axis=0))<100:return rejected('motion_target_support_too_local')
    screen=compose(compose(screen_map,matrix),cv2.invertAffineTransform(screen_map))
    centre=[(x0+x1)/2,(y0+y1)/2];delta=project(screen,[centre])[0]-centre
    if np.linalg.norm(delta)>.3*max(x1-x0,y1-y0):return rejected('motion_displacement_too_large')
    return dict(candidate=True,reason=None,previous_screen_to_current_screen=screen.tolist(),
        scale_ratio=scale,rotation_degrees=angle,centre_displacement_screen_px=delta.tolist(),
        features=len(points),bidirectional_tracks=len(p0),training_tracks=int(sum(~held)),
        validation_tracks=int(sum(held)),validation_median_screen_px=float(np.median(err)),
        validation_p90_screen_px=float(np.percentile(err,90)),support_hull_previous_screen_px=hull.tolist(),
        support_area_previous_screen_px2=area,error_is_physical_accuracy_bound=False)


class PageMotionProbe:
    """Single owner, one gray frame <=960px, auto-erased after 1.5 seconds."""
    def __init__(self,ttl=1.5):
        self.ttl=ttl;self.previous=None;self.timer=None;self.generation=0
        self.lock=threading.RLock()

    def _erase(self,generation):
        with self.lock:
            if generation==self.generation:self.previous=None;self.timer=None

    def reset(self):
        with self.lock:
            self.generation+=1;self.previous=None
            if self.timer:self.timer.cancel();self.timer=None

    close=reset

    def observe(self,image,metadata,window,context):
        started=time.monotonic()
        result=dict(schema=1,method=METHOD,pages=[],coordinate_mapping_allowed=False,
            independent_clock_verified=False,page_roi_evidence='preflight_window_rectangles_provisional',
            images_persisted=0,maximum_gray_frame_age_seconds=self.ttl)
        try:
            with self.lock:result.update(self._observe(image,metadata,window,context))
        except (cv2.error,ValueError,TypeError,KeyError,OverflowError) as error:
            self.reset();result.update(candidate=False,reason='motion_input_or_algorithm_failure',error_type=type(error).__name__)
        result['processing_ms']=(time.monotonic()-started)*1000
        return result

    def _observe(self,image,m,window,c):
        if image is None or not isinstance(image,np.ndarray) or image.dtype!=np.uint8 or image.size>18_000_000:
            self.reset();return rejected('motion_image_unavailable')
        layers=(c or {}).get('page_layers',[]);ids=[p.get('page_id') for p in layers]
        if (not window.get('focused') or not c or not c.get('active_tab_marker_unique') or
            not c.get('document_title_candidate') or not 1<=len(ids)<=12 or
            any(not isinstance(i,str) or not i for i in ids) or len(set(ids))!=len(ids)):
            self.reset();return rejected('motion_context_unavailable')
        reduction=m.get('pixel_reduction',1);stamp=m.get('timestamp_raw');nonce=m.get('request_id')
        if (type(reduction) is not int or reduction not in (1,2) or type(stamp) is not int or stamp<=0 or
            not isinstance(nonce,str) or not nonce or m.get('width')!=image.shape[1]*reduction or
            m.get('height')!=image.shape[0]*reduction):
            self.reset();return rejected('motion_frame_metadata_unavailable')
        regions={p['page_id']:rectangle(p) for p in layers}
        for i,region in enumerate(regions.values()):
            for other in list(regions.values())[i+1:]:
                if max(region[0],other[0])<min(region[2],other[2]) and max(region[1],other[1])<min(region[3],other[3]):
                    self.reset();return rejected('motion_page_regions_overlap')
        gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY) if image.ndim==3 else image.copy()
        factor=min(1.,960/max(gray.shape));size=(max(1,round(gray.shape[1]*factor)),max(1,round(gray.shape[0]*factor)))
        gray=cv2.resize(gray,size,interpolation=cv2.INTER_AREA)
        screen_map=compose(np.array([[reduction,0,(reduction-1)/2],[0,reduction,(reduction-1)/2]],float),
            resize_to_original(gray.shape,image.shape))
        identity=(window.get('pid'),window.get('window_id'),window.get('display_id'),
                  c['document_title_candidate'],tuple(sorted(ids)),image.shape,reduction)
        old=self.previous
        self.reset()
        pages=[];reason='motion_reference_initialized'
        if old is not None:
            gap=(stamp-old['stamp'])/1e9
            if identity!=old['identity']:reason='motion_identity_changed'
            elif not 0<gap<=1.2:reason='motion_frame_gap_or_time_reversal'
            elif nonce==old['nonce']:reason='motion_request_reused'
            else:
                for uuid in ids:
                    fitted=estimate(old['gray'],gray,old['points'][uuid],regions[uuid],screen_map)
                    pages.append(dict(page_uuid=uuid,**fitted))
                reason=None
        # Sample a bounded grid to avoid features clustering around one word.
        inverse=cv2.invertAffineTransform(screen_map);features={}
        for uuid,region in regions.items():
            xy=project(inverse,[[region[0],region[1]],[region[2],region[3]]])
            maximum=[gray.shape[1]-1,gray.shape[0]-1]
            x0,y0=np.ceil(np.clip(xy[0]+5,[1,1],maximum)).astype(int)
            x1,y1=np.floor(np.clip(xy[1]-5,[1,1],maximum)).astype(int)
            points=[]
            if x1-x0>=40 and y1-y0>=40:
                # One corner pass per page, then bounded grid thinning. Doing
                # twelve full-image detector passes here dominated runtime.
                mask=np.zeros(gray.shape,np.uint8);mask[y0:y1,x0:x1]=255
                corners=cv2.goodFeaturesToTrack(gray,maxCorners=max(48,1200//len(ids)),qualityLevel=.03,
                    minDistance=7,mask=mask,blockSize=7)
                counts={};cap=max(4,40//len(ids))
                if corners is not None:
                    for point in corners.reshape(-1,2):
                        gx=min(3,int((point[0]-x0)*4/(x1-x0)))
                        gy=min(2,int((point[1]-y0)*3/(y1-y0)))
                        cell=gx,gy
                        if counts.get(cell,0)<cap:
                            points.append(point);counts[cell]=counts.get(cell,0)+1
            features[uuid]=np.float32(points).reshape(-1,1,2) if points else None
        self.previous=dict(gray=gray,stamp=stamp,nonce=nonce,identity=identity,points=features)
        generation=self.generation
        self.timer=threading.Timer(self.ttl,self._erase,args=(generation,));self.timer.daemon=True;self.timer.start()
        return dict(candidate=any(p['candidate'] for p in pages),reason=reason,pages=pages,
            previous_request_id=old['nonce'] if old else None,current_request_id=nonce,
            previous_timestamp_raw=old['stamp'] if old else None,current_timestamp_raw=stamp,
            retained_gray_bytes=int(gray.nbytes),maximum_gray_bytes=960*960,
            frame_gap_ms=(stamp-old['stamp'])/1e6 if old else None)


def bound_fits(row):
    """Only independently registered original pages with complete binding."""
    from multipage_geometry import page_frames
    if not row.get('observation',{}).get('valid'):return {}
    out={}
    for f in page_frames(row):
        r=f['registration'];binding=r.get('native_background_binding',{})
        layers=f['observation'].get('context',{}).get('page_layers',[])
        if (r.get('valid') and len(layers)==1 and binding.get('page_uuid')==layers[0].get('page_id') and
            binding.get('source_pdf_sha256')==r.get('source_pdf_sha256') and
            binding.get('pdf_page_1based')==r.get('pdf_page_1based') and binding.get('note_id') and
            binding.get('source_hinote_sha256')):
            out[(binding['page_uuid'],binding['note_id'],binding['source_pdf_sha256'],binding['pdf_page_1based'])]=r
    return out


def check_visual_anchors(previous,current):
    """Compare flow against two original-PDF fits; never feed results to align()."""
    hint=current.get('observation',{}).get('page_motion',{})
    result=dict(checked=False,coordinate_mapping_allowed=False,physical_accuracy_verified=False)
    a=previous.get('observation',{});b=current.get('observation',{})
    if (not a.get('valid') or not b.get('valid') or not hint.get('candidate') or
        a.get('native_frame',{}).get('request_id')!=hint.get('previous_request_id') or
        b.get('native_frame',{}).get('request_id')!=hint.get('current_request_id') or
        a.get('window')!=b.get('window')):
        return dict(result,reason='independent_adjacent_anchors_unavailable')
    old,new=bound_fits(previous),bound_fits(current);checks=[]
    for p in hint.get('pages',[]):
        if not p.get('candidate'):continue
        key=next((k for k in old if k[0]==p['page_uuid'] and k in new),None)
        if key is None:continue
        ra,rb=old[key],new[key]
        support=np.asarray(p['support_hull_previous_screen_px'],float)
        expected=compose(np.asarray(rb['pdf_to_screen']),np.asarray(ra['screen_to_pdf']))
        delta=np.linalg.norm(project(expected,support)-project(p['previous_screen_to_current_screen'],support),axis=1)
        checks.append(dict(page_uuid=p['page_uuid'],max_anchor_disagreement_screen_px=float(max(delta)),
            agrees_with_independent_fits=bool(max(delta)<=2),error_is_physical_accuracy_bound=False))
    return dict(result,checked=bool(checks),pages=checks,reason=None if checks else 'bound_page_anchors_unavailable')


class MotionAnchorChain:
    """Numeric keyframe/relocalization check across <=8 frames and <=2 seconds.

    This audits endpoint drift, not unobserved movement between exposures.
    It cannot produce a pen coordinate or remove a raw/contact barrier.
    """
    def __init__(self):self.reset()
    def reset(self):
        self.anchor=None;self.nonce=None;self.stamp=None;self.flows={};self.steps=0;self.path=[]
    def _anchor(self,row):
        self.reset()
        fits=bound_fits(row);o=row.get('observation',{});m=o.get('native_frame',{})
        generation=o.get('motion_context_identity_generation_range')
        if fits and m.get('request_id') and m.get('timestamp_raw') and generation and generation[0]==generation[1]:
            self.anchor=dict(index=row['index'],fits=deepcopy(fits),window=deepcopy(o['window']),
                generation=generation[0],stamp=m['timestamp_raw'],nonce=m['request_id'])
            self.nonce=m['request_id'];self.stamp=m['timestamp_raw']
            self.flows={k:np.eye(2,3) for k in fits};self.path=[row['index']]
    def observe(self,row):
        result=dict(checked=False,coordinate_mapping_allowed=False,physical_accuracy_verified=False,
            intermediate_pen_positions_verified=False)
        if self.anchor is None:
            self._anchor(row);return dict(result,reason='motion_chain_waiting_for_next_anchor')
        o=row.get('observation',{});hint=o.get('page_motion',{});m=o.get('native_frame',{})
        anchor=self.anchor;stamp=m.get('timestamp_raw',0)
        if (hint.get('previous_request_id')!=self.nonce or hint.get('current_request_id')!=m.get('request_id') or
            hint.get('previous_timestamp_raw')!=self.stamp or hint.get('current_timestamp_raw')!=stamp or
            not self.stamp<stamp<=self.stamp+1_200_000_000 or stamp-anchor['stamp']>2_000_000_000 or
            self.steps>=8 or o.get('window')!=anchor['window'] or
            o.get('motion_context_identity_generation_range')!=[anchor['generation'],anchor['generation']]):
            self._anchor(row);return dict(result,reason='motion_chain_identity_time_or_missing_frame_barrier')
        pages={p['page_uuid']:p for p in hint.get('pages',[]) if p.get('candidate')}
        self.flows={k:compose(np.asarray(pages[k[0]]['previous_screen_to_current_screen']),matrix)
            for k,matrix in self.flows.items() if k[0] in pages}
        self.steps+=1;self.path.append(row['index']);self.nonce=m['request_id'];self.stamp=stamp
        if not self.flows:
            self._anchor(row);return dict(result,reason='motion_chain_tracking_failed')
        fits=bound_fits(row);checks=[]
        for key,matrix in self.flows.items():
            if key not in fits:continue
            a,b=anchor['fits'][key],fits[key]
            support=np.asarray(a['support_hull_screen_px'],float)
            expected=compose(np.asarray(b['pdf_to_screen']),np.asarray(a['screen_to_pdf']))
            difference=np.linalg.norm(project(matrix,support)-project(expected,support),axis=1)
            checks.append(dict(page_uuid=key[0],max_anchor_disagreement_screen_px=float(max(difference)),
                agrees_with_independent_fits=bool(max(difference)<=2),error_is_physical_accuracy_bound=False))
        result.update(checked=bool(checks),reason=None if checks else 'motion_chain_waiting_for_relocalization',
            anchor_index=anchor['index'],current_index=row['index'],geometry_indices=list(self.path),
            steps=self.steps,span_ms=(stamp-anchor['stamp'])/1e6,pages=checks)
        if fits:self._anchor(row)
        return result
