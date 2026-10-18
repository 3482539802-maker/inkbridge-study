"""Known-transform and failure tests for time/context-safe coordinate candidates."""
import json
from pathlib import Path
import tempfile
import unittest

from coordinate_alignment import align, assess_clock


def clock_sample(t):
    return dict(monotonic_before_us=t*1000,monotonic_after_us=t*1000+2,
                boottime_us=(t+900000)*1000,unix_ms=1700000000000+t+.001)


def clocks():
    return {'session_id':'synthetic','event_clock_verified':False,
        'anchors':[clock_sample(100),clock_sample(25000)],
        'observations':[{'seq':i+1,'action_time_us':t*1000-10000,'callback_clock':clock_sample(t)}
                        for i,t in enumerate((1000,6000,11000))]}


def frame(t):
    return {'observation':{'valid':True,'device_capture_wall_interval_ms':[1700000000000+t,1700000000000+t+100],
        'window':{'pid':9,'window_id':2,'display_id':0,'focused':True},
        'context':{'document_title_candidate':'test','active_tab_marker_unique':True,
                   'page_layers':[{'page_id':'page','unclipped_window_bounds':[0,0,1000,800]}]}},
        'registration':{'valid':True,'source_pdf_sha256':'a'*64,'pdf_page_1based':1,
            'screen_size':[1000,800],'heldout_median_error_screen_px':.2,
            'screen_to_pdf':[[.5,0,0],[0,.5,0]],'pdf_to_screen':[[2,0,0],[0,2,0]],
            'support_hull_screen_px':[[0,0],[1000,0],[1000,800],[0,800]]}}


def point(seq,t,tool=1,window=2):
    return dict(type='point',session_id='synthetic',seq=seq,action=2,pointer_id=0,
        x=200,y=400,action_time=t*1000,down_time=0,pressure=.5,tool_type=tool,window_id=window,display_id=0)


class CoordinateSafety(unittest.TestCase):
    def margin_run(self):
        frames=[frame(t) for t in (11000,12000,13000)]
        for f in frames:
            r=f['registration']
            r.update(pdf_size_pt=[500,400],heldout_inlier_p90_error_screen_px=.5,
                support_hull_screen_px=[[0,0],[1000,0],[1000,200],[0,200]],
                native_background_binding={'page_uuid':'page','source_pdf_sha256':'a'*64,
                    'pdf_page_1based':1,'source_hinote_sha256':'b'*64,'note_id':'note'})
        return frames

    def test_three_exposure_margin_restores_only_explicit_extrapolated_candidate(self):
        p=dict(point(1,12500),y=350)
        for stable in (False,True):
            _,rows=self.run_alignment([p],frames=self.margin_run(),stable_runs=stable,allow_page_margins=True)
            self.assertEqual(rows[0]['status'],'extrapolated_candidate' if stable else 'unresolved')
            if stable:
                support=rows[0]['evidence']['spatial_support']
                self.assertEqual(len(support),3)
                self.assertTrue(all(s['kind']=='verified_run_page_margin_extrapolation' for s in support))
                self.assertFalse(support[0]['indicator_is_accuracy_bound'])

    def test_margin_run_still_rejects_raw_motion_page_exclusion_bad_binding_residual_or_drift(self):
        from copy import deepcopy
        p=dict(point(1,12500),y=350)
        for mutation in ('binding','binding_inside','residual','drift','page','missing'):
            frames=self.margin_run()
            if mutation=='binding':frames[1]['registration']['native_background_binding']['page_uuid']='wrong'
            if mutation=='binding_inside':
                frames[1]['registration']['native_background_binding']['page_uuid']='wrong'
                frames[1]['registration']['support_hull_screen_px']=[[0,0],[1000,0],[1000,800],[0,800]]
            if mutation=='residual':frames[1]['registration']['heldout_inlier_p90_error_screen_px']=1.5
            if mutation=='drift':
                frames[1]['registration']['screen_to_pdf'][0][2]=.4
                frames[1]['registration']['pdf_to_screen'][0][2]=-.8
            if mutation=='page':
                for f in frames:f['registration']['pdf_size_pt']=[500,150]
            if mutation=='missing':frames[1]['observation']['valid']=False
            _,rows=self.run_alignment([p],frames=frames,stable_runs=True,allow_page_margins=True)
            self.assertIsNone(rows[0]['pdf_xy'],mutation)
        _,rows=self.run_alignment([dict(point(1,12300,tool=0),action=1),dict(p,seq=2)],
            frames=self.margin_run(),stable_runs=True,allow_page_margins=True)
        self.assertIsNone(rows[-1]['pdf_xy'])

    def run_alignment(self,points=None,state=None,frames=None,stable_runs=False,allow_page_margins=False):
        with tempfile.TemporaryDirectory(prefix='coordinate-check-') as folder:
            root=Path(folder)
            raw=root/'raw.jsonl'
            prefix=[point(i+1,t-10) for i,t in enumerate((1000,6000,11000))]
            actual=prefix+[dict(p,seq=p['seq']+3) for p in (points or [point(1,12000)])]
            raw.write_text(''.join(json.dumps(p)+'\n' for p in actual))
            before=raw.read_bytes()
            summary=align(raw,state or clocks(),frames or [frame(11000),frame(13000)],root/'out.jsonl',
                          stable_runs=stable_runs,allow_page_margins=allow_page_margins)
            self.assertEqual(raw.read_bytes(),before)
            return summary,[r for line in (root/'out.jsonl').read_text().splitlines() if (r:=json.loads(line))['seq']>3]

    def test_known_scale_and_translation(self):
        frames=[frame(11000),frame(13000)]
        for f in frames:
            f['registration']['screen_to_pdf']=[[.5,0,-10],[0,.5,-20]]
            f['registration']['pdf_to_screen']=[[2,0,20],[0,2,40]]
        summary,rows=self.run_alignment(frames=frames)
        self.assertEqual(rows[0]['pdf_xy'],[90,180])
        self.assertEqual(rows[0]['status'],'candidate')
        self.assertFalse(rows[0]['native_notes_object_verified'])
        self.assertFalse(summary['clock']['verified'])

    def test_one_callback_is_insufficient_even_with_verified_flag(self):
        c=clocks(); c['observations']=c['observations'][:1]; c['event_clock_verified']=True
        _,rows=self.run_alignment(state=c)
        self.assertEqual(rows[0]['reason'],'insufficient_event_clock_observations')

    def test_boottime_mismatch_and_future_event_rejected(self):
        for delta in (900000000,-100000):
            c=clocks()
            for o in c['observations']: o['action_time_us']+=delta
            self.assertFalse(assess_clock(c)['usable_candidate'])

    def test_wall_clock_jump_rejected(self):
        c=clocks(); c['anchors'][1]['unix_ms']+=1000
        self.assertEqual(assess_clock(c)['reason'],'device_wall_clock_jump_or_suspend')

    def test_no_extrapolation_or_use_during_exposure(self):
        for t in (500,11050,14000,30000):
            _,rows=self.run_alignment([point(1,t)])
            self.assertIsNone(rows[0]['pdf_xy'])

    def test_finger_eraser_or_foreign_window_blocks_pair(self):
        for tool,window in ((0,2),(2,2),(1,3)):
            summary,rows=self.run_alignment([point(1,11500,tool,window),point(2,12000)])
            self.assertEqual(summary['blocked_pairs'],1)
            self.assertEqual(rows[-1]['reason'],'input_motion_or_context_barrier')

    def test_nonadjacent_frames_cannot_bridge_failed_observation(self):
        middle=frame(12000); middle['observation']['valid']=False
        _,rows=self.run_alignment(frames=[frame(11000),middle,frame(13000)])
        self.assertIsNone(rows[0]['pdf_xy'])

    def test_page_document_window_and_geometry_change_rejected(self):
        variants=[]
        for key,value in [('document_title_candidate','other'),('page_layers',[{'page_id':'other'}])]:
            b=frame(13000); b['observation']['context'][key]=value; variants.append(b)
        b=frame(13000); b['observation']['window']['window_id']=3; variants.append(b)
        b=frame(13000); b['registration']['pdf_page_1based']=2; variants.append(b)
        b=frame(13000); b['registration']['screen_to_pdf'][0][2]=10
        b['registration']['pdf_to_screen'][0][2]=-20; variants.append(b)
        for b in variants:
            _,rows=self.run_alignment(frames=[frame(11000),b])
            self.assertIsNone(rows[0]['pdf_xy'])

    def test_outside_support_rejected(self):
        frames=[frame(11000),frame(13000)]
        for f in frames: f['registration']['support_hull_screen_px']=[[0,0],[100,0],[100,100],[0,100]]
        _,rows=self.run_alignment(frames=frames)
        self.assertEqual(rows[0]['reason'],'outside_feature_support')

    def test_bad_session_does_not_emit_cross_session_point(self):
        c=clocks(); c['session_id']='other'
        with self.assertRaises(ValueError): self.run_alignment(state=c)

    def test_sequence_gap_and_cancel_block_mapping(self):
        for points in ([point(1,11500),point(3,12000)],
                       [dict(point(1,11500),action=0),point(2,12000)]):
            _,rows=self.run_alignment(points)
            self.assertIsNone(rows[-1]['pdf_xy'])

    def test_callback_evidence_must_match_raw_sequence(self):
        c=clocks(); c['observations'][1]['seq']=80
        _,rows=self.run_alignment(state=c)
        self.assertIsNone(rows[-1]['pdf_xy'])

    def test_held_finger_spanning_frames_remains_barrier(self):
        for tail in ([],[dict(point(3,14000,tool=0),action=3)]):
            points=[dict(point(1,10995,tool=0),action=1),point(2,12000)]+tail
            _,rows=self.run_alignment(points)
            self.assertEqual(rows[0]['reason'],'input_motion_or_context_barrier')

    def test_finger_at_uncertain_frame_boundary_blocks_pair(self):
        _,rows=self.run_alignment([point(1,10999.5,tool=0),point(2,12000)])
        self.assertEqual(rows[-1]['reason'],'input_motion_or_context_barrier')

    def test_symmetric_estimate_keeps_timing_and_error_provenance(self):
        frames=[frame(11000),frame(13000)]
        for f in frames:
            f['registration'].update(pdf_size_pt=[500,400],pixel_geometry_version=2)
        frames[1]['registration']['screen_to_pdf'][0][2]=-.2
        frames[1]['registration']['pdf_to_screen'][0][2]=.4
        _,rows=self.run_alignment(frames=frames)
        row=rows[0]
        self.assertEqual(row['pdf_xy'],[99.9,200])
        self.assertAlmostEqual(row['frame_disagreement_pdf_pt'],.2)
        self.assertEqual(row['action_time_us'],12000000)
        self.assertEqual(row['evidence']['geometry_version'],2)
        self.assertEqual(row['pdf_normalized_xy'],[99.9/500,.5])
        self.assertFalse(row['error_indicator_is_accuracy_bound'])

    def test_both_native_background_bindings_are_retained(self):
        frames=[frame(11000),frame(13000)]
        bindings=[{'page_uuid':'page','source_hinote_sha256':str(i)} for i in range(2)]
        for f,binding in zip(frames,bindings):
            f['registration']['native_background_binding']=binding
        _,rows=self.run_alignment(frames=frames)
        self.assertEqual(rows[0]['evidence']['native_background_binding'],bindings)

    def test_mixed_pixel_versions_and_broken_inverse_rejected(self):
        for mode in ('version','inverse'):
            frames=[frame(11000),frame(13000)]
            if mode=='version':frames[1]['registration']['pixel_geometry_version']=2
            else:frames[1]['registration']['pdf_to_screen'][0][0]=3
            _,rows=self.run_alignment(frames=frames)
            self.assertIsNone(rows[0]['pdf_xy'])

    def test_two_pixel_drift_limit_measures_actual_screen_displacement(self):
        frames=[frame(11000),frame(13000)]
        frames[1]['registration']['screen_to_pdf'][0][2]=-.9
        frames[1]['registration']['pdf_to_screen'][0][2]=1.8
        _,rows=self.run_alignment(frames=frames)
        self.assertEqual(rows[0]['status'],'candidate')
        frames[1]['registration']['screen_to_pdf'][0][2]=-1.1
        frames[1]['registration']['pdf_to_screen'][0][2]=2.2
        _,rows=self.run_alignment(frames=frames)
        self.assertIsNone(rows[0]['pdf_xy'])

    def test_stable_run_uses_outer_exposures_and_preserves_middle_evidence(self):
        frames=[frame(t) for t in (11000,12000,13000)]
        for i,f in enumerate(frames): f['index']=i
        _,baseline=self.run_alignment([point(1,12050)],frames=frames)
        self.assertIsNone(baseline[0]['pdf_xy'])
        _,rows=self.run_alignment([point(1,12050)],frames=frames,stable_runs=True)
        self.assertEqual(rows[0]['pdf_xy'],[100,200])
        self.assertTrue(rows[0]['evidence']['stable_run_bracketing'])
        self.assertEqual(rows[0]['evidence']['geometry_indices'],[0,2])
        self.assertEqual(rows[0]['evidence']['intermediate_verified_geometry_indices'],[1])

    def test_stable_run_never_bridges_failed_or_changed_middle(self):
        for failure in ('observation','registration','page','window'):
            frames=[frame(t) for t in (11000,12000,13000)]
            middle=frames[1]
            if failure in ('observation','registration'):middle[failure]['valid']=False
            elif failure=='page':middle['registration']['pdf_page_1based']=2
            else:middle['observation']['window']['window_id']=3
            _,rows=self.run_alignment([point(1,12050)],frames=frames,stable_runs=True)
            self.assertIsNone(rows[0]['pdf_xy'])

    def test_stable_run_full_span_finger_cancel_and_window_barriers(self):
        frames=[frame(t) for t in (11000,12000,13000)]
        for barrier in (point(1,11500,tool=0),point(1,11500,window=3),dict(point(1,11500),action=0)):
            _,rows=self.run_alignment([barrier,point(2,12050)],frames=frames,stable_runs=True)
            self.assertIsNone(rows[-1]['pdf_xy'])

    def test_stable_run_held_contact_and_clock_gap_block(self):
        frames=[frame(t) for t in (11000,12000,13000)]
        for points in ([dict(point(1,10995,tool=0),action=1),point(2,12050)],
                       [point(1,11500),point(3,12050)]):
            _,rows=self.run_alignment(points,frames=frames,stable_runs=True)
            self.assertIsNone(rows[-1]['pdf_xy'])

    def test_stable_run_requires_middle_support_and_outer_drift(self):
        for failure in ('support','drift'):
            frames=[frame(t) for t in (11000,12000,13000)]
            if failure=='support':
                frames[1]['registration']['support_hull_screen_px']=[[0,0],[100,0],[100,100],[0,100]]
            else:
                for i,f in enumerate(frames):
                    f['registration']['screen_to_pdf'][0][2]=-.6*i
                    f['registration']['pdf_to_screen'][0][2]=1.2*i
            _,rows=self.run_alignment([point(1,12050)],frames=frames,stable_runs=True)
            self.assertIsNone(rows[0]['pdf_xy'])

    def test_stable_run_six_second_bound_and_outer_exposure_not_bypassed(self):
        for times,t in (((11000,13900,16950),13950),((11000,13950,16950),14000),((11000,12000,13000),11050)):
            _,rows=self.run_alignment([point(1,t)],frames=[frame(v) for v in times],stable_runs=True)
            self.assertIsNone(rows[0]['pdf_xy'])

    def test_page_margin_is_separate_and_requires_verified_page_and_nearby_support(self):
        frames=[frame(11000),frame(13000)]
        for f in frames:
            f['registration'].update(pdf_size_pt=[500,400],
                support_hull_screen_px=[[300,200],[900,200],[900,700],[300,700]],
                native_background_binding={'page_uuid':'page','source_pdf_sha256':'a'*64,
                    'pdf_page_1based':1,'source_hinote_sha256':'b'*64,'note_id':'note'})
        _,rows=self.run_alignment(frames=frames)
        self.assertIsNone(rows[0]['pdf_xy'])
        _,rows=self.run_alignment(frames=frames,allow_page_margins=True)
        self.assertEqual(rows[0]['status'],'extrapolated_candidate')
        self.assertEqual(rows[0]['pdf_xy'],[100,200])
        for f in frames:f['registration'].pop('native_background_binding')
        _,rows=self.run_alignment(frames=frames,allow_page_margins=True)
        self.assertIsNone(rows[0]['pdf_xy'])

    def test_page_margin_does_not_cross_page_pdf_bounds_or_distance_limit(self):
        for failure in ('distance','pdf_bounds','native_rect','wrong_binding'):
            frames=[frame(11000),frame(13000)]
            for f in frames:
                f['registration'].update(pdf_size_pt=[500,400],
                    support_hull_screen_px=[[300,200],[900,200],[900,700],[300,700]],
                    native_background_binding={'page_uuid':'page','source_pdf_sha256':'a'*64,
                        'pdf_page_1based':1,'source_hinote_sha256':'b'*64,'note_id':'note'})
                if failure=='distance':f['registration']['support_hull_screen_px']=[[800,200],[950,200],[950,700],[800,700]]
                if failure=='pdf_bounds':f['registration']['pdf_size_pt']=[50,400]
                if failure=='native_rect':f['observation']['context']['page_layers'][0]['unclipped_window_bounds']=[250,0,1000,800]
                if failure=='wrong_binding':f['registration']['native_background_binding']['page_uuid']='other'
            _,rows=self.run_alignment(frames=frames,allow_page_margins=True)
            self.assertIsNone(rows[0]['pdf_xy'])
            self.assertEqual(rows[0]['reason'],'outside_bound_page' if failure in ('pdf_bounds','native_rect') else 'outside_feature_support')

    def test_page_exclusion_requires_both_brackets_and_preserves_raw(self):
        frames=[frame(11000),frame(13000)]
        for f in frames:
            f['registration'].update(pdf_size_pt=[500,400],
                support_hull_screen_px=[[300,200],[900,200],[900,700],[300,700]],
                native_background_binding={'page_uuid':'page','source_pdf_sha256':'a'*64,
                    'pdf_page_1based':1,'source_hinote_sha256':'b'*64,'note_id':'note'})
            f['observation']['context']['page_layers'][0]['unclipped_window_bounds']=[250,0,1000,800]
        _,rows=self.run_alignment(frames=frames)
        self.assertEqual(rows[0]['reason'],'outside_bound_page')
        self.assertIsNone(rows[0]['pdf_xy'])
        frames[1]['registration'].pop('native_background_binding')
        _,rows=self.run_alignment(frames=frames,allow_page_margins=True)
        self.assertEqual(rows[0]['reason'],'outside_feature_support')

    def test_native_bound_subpixel_ui_rounding_still_requires_stable_visual_fit(self):
        for displacement in (.17,.26):
            frames=[frame(11000),frame(13000)]
            for f in frames:f['registration']['native_background_binding']={'page_uuid':'page','note_id':'note'}
            rect=frames[1]['observation']['context']['page_layers'][0]['unclipped_window_bounds']
            rect[0]+=displacement;rect[2]+=displacement
            _,rows=self.run_alignment(frames=frames)
            self.assertEqual(rows[0]['status']=='candidate',displacement<=.25)


if __name__=='__main__':unittest.main(verbosity=2)
