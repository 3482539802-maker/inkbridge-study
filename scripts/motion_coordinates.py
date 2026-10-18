"""Explicit temporal estimates between independently located page exposures.

Sparse flow corroborates the two fits. Constant affine motion between exposures
is an assumption, not an accuracy guarantee. Never extrapolate before/after
available exposures, across identity gaps, or onto active non-pen contacts.
"""
from bisect import bisect_left, bisect_right
import math

import cv2
import numpy as np

from ink_protocol import PEN_TOOLS
from page_motion import bound_fits
from visual_registration import compose, project


def checked_flow(track):
    """Malformed persisted evidence must reject a pair, never stop raw processing."""
    try:
        hull = np.asarray(track.get('support_hull_previous_screen_px'), float)
        matrix = np.asarray(track.get('previous_screen_to_current_screen'), float)
        residual = float(track.get('validation_p90_screen_px', math.inf))
        if (hull.ndim != 2 or hull.shape[1] != 2 or not 3 <= len(hull) <= 1024 or
                matrix.shape != (2, 3) or not np.isfinite(hull).all() or
                not np.isfinite(matrix).all() or not 0 <= residual <= 2): return None
        scale = np.linalg.svd(matrix[:, :2], compute_uv=False)
        if min(scale) < .8 or max(scale) > 1.25 or np.linalg.det(matrix[:, :2]) <= 0: return None
        if abs(math.degrees(math.atan2(matrix[1, 0], matrix[0, 0]))) > 1: return None
        if cv2.contourArea(np.float32(hull)) < 12000: return None
        return hull
    except (ValueError, TypeError, OverflowError, np.linalg.LinAlgError, cv2.error):
        return None


class MotionCoordinates:
    maximum_gap_ms = 1200

    def __init__(self, frames, clock, raw_points, prefix_context=None, allow_page_margins=False):
        from coordinate_alignment import valid_frame
        self.clock, self.margins = clock, allow_page_margins
        self.pairs = []
        if not clock.get('usable_candidate'): return
        for a, b in zip(frames, frames[1:]):
            oa, ob = a.get('observation', {}), b.get('observation', {})
            if not (oa.get('independent_page_exposure_requested') and ob.get('independent_page_exposure_requested')): continue
            ma, mb = oa.get('native_frame', {}), ob.get('native_frame', {})
            hint = ob.get('page_motion', {})
            generation = oa.get('motion_context_identity_generation_range')
            if (not generation or len(generation) != 2 or generation[0] != generation[1] or
                generation != ob.get('motion_context_identity_generation_range') or
                oa.get('window') != ob.get('window') or not ma.get('request_id') or
                hint.get('previous_request_id') != ma['request_id'] or
                hint.get('current_request_id') != mb.get('request_id') or
                hint.get('previous_timestamp_raw') != ma.get('timestamp_raw') or
                hint.get('current_timestamp_raw') != mb.get('timestamp_raw')): continue
            ia, ib = oa.get('device_capture_wall_interval_ms'), ob.get('device_capture_wall_interval_ms')
            if (not all(isinstance(v, (list, tuple)) and len(v) == 2 and
                    all(isinstance(x, (int, float)) and math.isfinite(x) for x in v) and v[0] <= v[1]
                    for v in (ia, ib)) or not ia[1] < ib[0] or ib[1]-ia[0] > self.maximum_gap_ms): continue
            left, right = bound_fits(a), bound_fits(b)
            pages = hint.get('pages', [])
            tracks = {p.get('page_uuid'): p for p in pages if p.get('candidate')}
            if len({p.get('page_uuid') for p in pages}) != len(pages): continue
            from multipage_geometry import page_frames
            children_a = {f['registration']['native_background_binding']['page_uuid']: f for f in page_frames(a)
                          if valid_frame(f) and f['registration'].get('native_background_binding')}
            children_b = {f['registration']['native_background_binding']['page_uuid']: f for f in page_frames(b)
                          if valid_frame(f) and f['registration'].get('native_background_binding')}
            for key in left.keys() & right.keys():
                uuid = key[0]
                if uuid not in tracks or uuid not in children_a or uuid not in children_b: continue
                ra, rb, track = left[key], right[key], tracks[uuid]
                if ra['native_background_binding'] != rb['native_background_binding']: continue
                if (ra.get('pixel_geometry_version', 1) != rb.get('pixel_geometry_version', 1) or
                        ra.get('screen_size') != rb.get('screen_size')): continue
                from coordinate_layout_check import describe
                try:
                    da, db = describe(children_a[uuid]), describe(children_b[uuid])
                    if da['identity'] != db['identity']: continue
                except (ValueError, TypeError, KeyError): continue
                hull = checked_flow(track)
                if hull is None: continue
                expected = compose(rb['pdf_to_screen'], ra['screen_to_pdf'])
                discrepancy = float(max(np.linalg.norm(project(expected, hull)-
                    project(track['previous_screen_to_current_screen'], hull), axis=1)))
                if not math.isfinite(discrepancy) or discrepancy > 2: continue
                self.pairs.append(dict(a=children_a[uuid], b=children_b[uuid], start=sum(ia)/2,
                    end=sum(ib)/2, uncertainty=max(ia[1]-ia[0], ib[1]-ib[0])/2,
                    flow=track, discrepancy=discrepancy))
        self.pairs.sort(key=lambda p: p['start'])
        self.starts = [p['start'] for p in self.pairs]
        # Closed intervals preserve held contacts, including a contact omitted
        # from an incremental raw window. CANCEL closes, never erases its past.
        active = {tuple(k): t/1000+clock['offset_ms'] for k, t in
                  (prefix_context or {}).get('contacts', [])}
        barriers = []
        windows = []
        for p in raw_points:
            t = p['action_time']/1000+clock['offset_ms']
            window = (p['window_id'],p['display_id'])
            if not windows or windows[-1][1] != window:windows.append((t,window))
            key = p['display_id'], p['window_id'], p['pointer_id']
            if p['tool_type'] not in PEN_TOOLS:
                begin = active.setdefault(key, t)
                if p['action'] in (0, 3):
                    barriers.append((begin, t)); active.pop(key)
            elif p['action'] == 0:
                barriers.append((t, t))
        barriers.extend((t, math.inf) for t in active.values())
        self.barriers = []
        for lo, hi in sorted(barriers):
            if self.barriers and lo <= self.barriers[-1][1]:
                self.barriers[-1][1] = max(hi, self.barriers[-1][1])
            else: self.barriers.append([lo, hi])
        self.barrier_starts = [p[0] for p in self.barriers]
        # A foreign input window anywhere in an exposure interval breaks its
        # identity, including A->B->A excursions between UI reads.
        for pair in self.pairs:
            w=pair['a']['observation']['window']; expected=(w['window_id'],w['display_id'])
            for index,(stamp,window) in enumerate(windows):
                end=windows[index+1][0] if index+1<len(windows) else math.inf
                if window!=expected and stamp<=pair['end'] and end>=pair['start']:
                    pair['foreign_window']=True;break
        self.boundary_pairs = []
        endings = {}
        for pair in self.pairs:
            key=(pair['a'].get('index'),pair['a']['registration']['native_background_binding']['page_uuid'])
            previous=endings.get(key)
            if previous is not None:
                joined=self.join_boundary(previous,pair)
                if joined:self.boundary_pairs.append(joined)
            endings[(pair['b'].get('index'),key[1])]=pair

    def join_boundary(self,left,right):
        """An independently fitted middle exposure validates a continuous bracket.

        This only covers the small uncertainty gap at their common exposure;
        it never bridges a failed flow link or extrapolates at outer endpoints.
        """
        if left.get('foreign_window') or right.get('foreign_window'):return None
        if right['end']-left['start']>self.maximum_gap_ms:return None
        a,m,b=left['a']['registration'],left['b']['registration'],right['b']['registration']
        if a['native_background_binding']!=b['native_background_binding']:return None
        forward=left['flow']['previous_screen_to_current_screen']
        # Both links must support the same material region. Express their
        # convex intersection in the left exposure's screen coordinates.
        second=project(cv2.invertAffineTransform(np.asarray(forward,float)),right['flow']['support_hull_previous_screen_px'])
        area,hull=cv2.intersectConvexConvex(np.float32(left['flow']['support_hull_previous_screen_px']),np.float32(second))
        if hull is None or area<12000:return None
        hull=hull.reshape(-1,2)
        alpha=(left['end']-left['start'])/(right['end']-left['start'])
        predicted=(1-alpha)*np.asarray(a['pdf_to_screen'])+alpha*np.asarray(b['pdf_to_screen'])
        pdf=project(a['screen_to_pdf'],hull)
        middle_error=float(max(np.linalg.norm(project(predicted,pdf)-project(m['pdf_to_screen'],pdf),axis=1)))
        matrix=compose(right['flow']['previous_screen_to_current_screen'],forward)
        discrepancy=float(max(np.linalg.norm(project(compose(b['pdf_to_screen'],a['screen_to_pdf']),hull)-project(matrix,hull),axis=1)))
        if not math.isfinite(middle_error+discrepancy) or max(middle_error,discrepancy)>2:return None
        track=dict(left['flow'],previous_screen_to_current_screen=matrix.tolist(),support_hull_previous_screen_px=hull.tolist())
        return dict(a=left['a'],b=right['b'],middle=left['b'],start=left['start'],end=right['end'],
            uncertainty=max(left['uncertainty'],right['uncertainty']),flow=track,discrepancy=discrepancy,
            middle_error=middle_error,boundary=left['end'],
            boundary_left_width=self.clock['uncertainty_ms']+left['uncertainty'],
            boundary_right_width=self.clock['uncertainty_ms']+right['uncertainty'])

    def locate(self, point):
        from coordinate_alignment import inside_polygon, spatial_support
        if not self.pairs or not self.clock['action_time_range_us'][0] <= point['action_time'] <= self.clock['action_time_range_us'][1]: return None
        t = point['action_time']/1000+self.clock['offset_ms']
        error = self.clock['uncertainty_ms']
        i = bisect_right(self.barrier_starts, t+error)-1
        if i >= 0 and self.barriers[i][1] >= t-error: return None
        candidates = []
        nearby=self.pairs[bisect_left(self.starts, t-self.maximum_gap_ms):bisect_right(self.starts, t)]
        nearby += [p for p in self.boundary_pairs if
            p['boundary']-p['boundary_left_width']<=t<=p['boundary']+p['boundary_right_width']]
        for pair in nearby:
            if pair.get('foreign_window'):continue
            if not pair['start']+error+pair['uncertainty'] < t < pair['end']-error-pair['uncertainty']: continue
            a, b = pair['a'], pair['b']
            w = a['observation']['window']
            if (point['window_id'], point['display_id']) != (w['window_id'], w['display_id']): continue
            ra, rb = a['registration'], b['registration']
            alpha = (t-pair['start'])/(pair['end']-pair['start'])
            matrix = (1-alpha)*np.asarray(ra['pdf_to_screen'])+alpha*np.asarray(rb['pdf_to_screen'])
            if not np.isfinite(matrix).all() or abs(np.linalg.det(matrix[:, :2])) < 1e-8: continue
            xy = project(cv2.invertAffineTransform(matrix), [[point['x'], point['y']]])[0]
            qa, qb = project(ra['pdf_to_screen'], [xy])[0], project(rb['pdf_to_screen'], [xy])[0]
            # An estimate only applies inside the region corroborated by flow.
            if not inside_polygon(pair['flow']['support_hull_previous_screen_px'], *qa): continue
            support = [spatial_support(f, dict(point, x=float(q[0]), y=float(q[1])), self.margins)
                       for f, q in ((a, qa), (b, qb))]
            if pair.get('middle'):
                middle=pair['middle'];qm=project(middle['registration']['pdf_to_screen'],[xy])[0]
                support.append(spatial_support(middle,dict(point,x=float(qm[0]),y=float(qm[1])),self.margins))
            if not all(s['allowed'] for s in support): continue
            speed = float(np.linalg.norm(qb-qa))/(pair['end']-pair['start'])
            registrations=[ra,rb]+([pair['middle']['registration']] if pair.get('middle') else [])
            residual = max(r.get('heldout_inlier_p90_error_screen_px', r['heldout_median_error_screen_px']) for r in registrations)
            indicator = residual+pair['discrepancy']+pair.get('middle_error',0)+speed*(error+pair['uncertainty'])
            if not math.isfinite(indicator) or indicator > 4: continue
            size = ra.get('pdf_size_pt')
            candidates.append(dict(status='motion_candidate', reason=None, pdf_xy=xy.tolist(),
                pdf_page_1based=ra['pdf_page_1based'], source_pdf_sha256=ra['source_pdf_sha256'],
                page_uuid=ra['native_background_binding']['page_uuid'], pdf_size_pt=size,
                pdf_normalized_xy=[float(xy[i]/size[i]) for i in (0, 1)] if size else None,
                native_notes_object_verified=False, device_wall_ms_candidate=t,
                clock_uncertainty_ms=error, bracket_device_wall_ms=[pair['start'], pair['end']],
                empirical_error_indicator_screen_px=indicator, error_indicator_is_accuracy_bound=False,
                unmeasured_error_sources=['nonlinear_motion_between_exposures', 'digitizer_to_rendered_ink', 'Notes_smoothing_and_persistence'],
                evidence=dict(method='independent_page_exposures_with_checked_flow_interpolation',
                    geometry_indices=[a.get('index'), b.get('index')], interpolation_fraction=alpha,
                    temporal_model='constant_affine_motion_between_exposures',
                    flow_disagreement_screen_px=pair['discrepancy'], spatial_support=support,
                    native_background_binding=[r['native_background_binding'] for r in (ra, rb)],
                    active_nonpen_contact_excluded=True, temporal_extrapolation=False,
                    page_margin_extrapolated=any(s['kind']!='interpolation' for s in support),
                    independent_of_ocr=True)))
            if pair.get('middle'):
                candidates[-1]['evidence'].update(middle_exposure_index=pair['middle'].get('index'),
                    withheld_middle_error_screen_px=pair['middle_error'])
        if len(candidates) != 1: return None
        return candidates[0]
