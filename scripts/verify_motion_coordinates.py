"""Known moving-page truth and raw/identity refusal cases; no device access."""
from copy import deepcopy
import unittest

from coordinate_page_binding import bind_frame
from coordinate_alignment import assess_clock, align
from motion_coordinates import MotionCoordinates
from verify_coordinate_alignment import frame, clocks, point
import verify_coordinate_alignment as alignment_tests
import verify_coordinate_stream as stream_tests
from verify_multipage_geometry import binding


def moving_frames():
    rows=[]
    for i,t in enumerate((11000,11400,11800)):
        f=bind_frame(frame(t),binding());f['index']=i
        f['observation'].update(independent_page_exposure_requested=True,
            motion_context_identity_generation_range=[0,0],
            native_frame=dict(request_id='nonce-'+str(i),timestamp_raw=t*1000000))
        f['registration'].update(pdf_to_screen=[[2,0,i*10],[0,2,0]],
            screen_to_pdf=[[.5,0,-i*5],[0,.5,0]],pdf_size_pt=[500,400],
            heldout_inlier_p90_error_screen_px=.2)
        if i:
            f['observation']['page_motion']=dict(candidate=True,
                previous_request_id='nonce-'+str(i-1),current_request_id='nonce-'+str(i),
                previous_timestamp_raw=(t-400)*1000000,current_timestamp_raw=t*1000000,
                pages=[dict(candidate=True,page_uuid='page',validation_p90_screen_px=.1,
                    previous_screen_to_current_screen=[[1,0,10],[0,1,0]],
                    support_hull_previous_screen_px=[[0,0],[1000,0],[1000,800],[0,800]])])
        rows.append(f)
    return rows


class MovingCoordinatesChecks(unittest.TestCase):
    def test_three_exposures_cover_shared_time_boundary_but_not_failed_link_or_nonlinearity(self):
        _,rows=self.run_case([point(1,11450)])
        self.assertEqual(rows[0]['status'],'motion_candidate')
        self.assertEqual(rows[0]['pdf_xy'],[95,200])
        self.assertEqual(rows[0]['evidence']['middle_exposure_index'],1)
        for change in ('failed_link','nonlinear','finger','outer_start','outer_end'):
            frames=moving_frames();points=[point(1,11450)]
            if change=='failed_link':frames[1]['observation']['page_motion']['pages'][0]['candidate']=False
            elif change=='nonlinear':
                frames[1]['registration']['pdf_to_screen'][0][2]+=6
                frames[1]['registration']['screen_to_pdf'][0][2]-=3
                frames[1]['observation']['page_motion']['pages'][0]['previous_screen_to_current_screen'][0][2]+=6
                frames[2]['observation']['page_motion']['pages'][0]['previous_screen_to_current_screen'][0][2]-=6
            elif change=='finger':points=[dict(point(1,11449.5,tool=0),action=0),point(2,11450)]
            elif change=='outer_start':points=[point(1,11050)]
            else:points=[point(1,11850)]
            _,rows=self.run_case(points,frames)
            self.assertNotEqual(rows[-1]['status'],'motion_candidate',change)

    def test_different_exposure_uncertainties_do_not_duplicate_existing_pair(self):
        frames=moving_frames()
        # Outer right uncertainty is larger; left-side points remain in the
        # original pair, and must not become ambiguous with the joined bracket.
        frames[0]['observation']['device_capture_wall_interval_ms']=[1700000011049,1700000011051]
        frames[1]['observation']['device_capture_wall_interval_ms']=[1700000011449,1700000011451]
        _,rows=self.run_case([point(1,11440)],frames)
        self.assertEqual(rows[0]['status'],'motion_candidate')
        self.assertNotIn('middle_exposure_index',rows[0]['evidence'])

    def test_native_clock_chunks_restart_and_contact_release_match_offline(self):
        from native_frame_timing import bind
        points = [dict(point(i+1, t, tool=0), action=3) for i, t in enumerate((1000, 6000, 10000))]
        points += [dict(point(4, 11150, tool=0), action=1), dict(point(5, 11170, tool=0), action=0)]
        points += [point(6, 11250), point(7, 11650)]
        frames = [frame(500), frame(6000)] + moving_frames()
        for i, f in enumerate(frames):
            t = (500, 6000, 11000, 11400, 11800)[i]
            f['observation'].setdefault('native_frame', {}).update(timestamp_raw=t*1000000,
                callback_monotonic_us=t*1000+10000, callback_boottime_us=t*1000+900010000,
                callback_wall_us=1700000000000000+t*1000+10000)
        state = dict(session_id='synthetic', anchors=[alignment_tests.clock_sample(t) for t in (100,6000,13000)],
            observations=[dict(seq=p['seq'], action_time_us=p['action_time'],
                callback_clock=alignment_tests.clock_sample(p['action_time']/1000+1)) for p in points[:3]])
        frames, calibration = bind(frames, state)
        self.assertTrue(calibration['usable_candidate'])
        result = stream_tests.StreamChecks().compare(points, state, frames, limit=1)
        self.assertEqual(result['results'].get('motion_candidate'), 2)
        import json, tempfile
        from pathlib import Path
        from coordinate_stream import CoordinateStream
        from audit_pipeline_session import audit
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=root/'raw.jsonl';meta=root/'raw.session.json'
            raw.write_text(''.join(json.dumps(p)+'\n' for p in points))
            meta.write_text(json.dumps(dict(session_id='synthetic',state='completed',byte_length=raw.stat().st_size,
                point_count=len(points),dropped_count=0)))
            store=CoordinateStream(root/'derived',raw,'synthetic')
            try:
                store.ingest()
                for i,f in enumerate(frames):store.add_geometry(dict(f,index=i))
                while store.step(state,final=True):pass
                store.export()
            finally:store.close()
            report=audit(root/'derived',raw,meta)
            self.assertEqual(report['motion_estimated_points'],2)
            self.assertTrue(report['zero_unknown_with_local_raw_integrity'])
            self.assertFalse(report['physical_mm_accuracy_verified'])
            m=json.loads(meta.read_bytes());m['dropped_count']=1;meta.write_text(json.dumps(m))
            self.assertFalse(audit(root/'derived',raw,meta)['zero_unknown_with_local_raw_integrity'])
        held = deepcopy(points);held[4]['action'] = 2
        result = stream_tests.StreamChecks().compare(held, state, frames, limit=1)
        self.assertEqual(result['results'].get('motion_candidate', 0), 0)

    def test_real_feature_registration_and_flow_on_known_moving_original(self):
        """Full image->fit->flow->time->pen test; synthetic motion, not a tablet trial."""
        from pathlib import Path
        import cv2
        import fitz
        import numpy as np
        from page_exposure import register
        from page_motion import PageMotionProbe
        from visual_registration import PDFRegistration, project
        from verify_motion_exposure import payload
        pdf = Path(__file__).resolve().parents[1]/'materials/math-tutorial/upper-original.pdf'
        if not pdf.is_file():
            self.skipTest('Private textbook PDF not distributed; moving-original fixture unavailable')
        registration = PDFRegistration(pdf, [14], local_refinement=True)
        probe = PageMotionProbe()
        try:
            with fitz.open(pdf) as document:
                page = document[14]
                pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                original = cv2.cvtColor(np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width,3),cv2.COLOR_RGB2BGR)
                centre = [page.rect.width/2, page.rect.height/2]
            h,w = original.shape[:2]
            native_binding = dict(binding(), source_pdf_sha256=registration.sha256, pages={'page':15})
            frames = []
            for i,t in enumerate((11000,11400,11800)):
                captured,_ = payload();o = captured.observation
                o.update(independent_page_exposure_requested=True,
                    device_capture_wall_interval_ms=[1700000000000+t-1,1700000000000+t+1])
                o['context']['page_layers'][0]['unclipped_window_bounds']=[0,0,w,h]
                o['context_before']['page_layers'][0]['unclipped_window_bounds']=[0,-3,w,h-3]
                meta=dict(request_id='pixel-'+str(i),timestamp_raw=t*1000000,width=w,height=h,pixel_reduction=1)
                o['native_frame']=meta
                image=cv2.warpAffine(original,np.float64([[1,0,i*10],[0,1,0]]),(w,h),borderValue=(255,255,255))
                o['page_motion']=probe.observe(image,meta,o['window'],o['context'])
                observed,fit=register(image,o,registration,native_binding)
                self.assertTrue(observed['valid'],fit)
                self.assertTrue(fit['valid'],fit)
                frames.append(dict(index=i,observation=observed,registration=fit))
            points=[]
            for i,t in enumerate((11200,11300,11600,11700)):
                screen=project([[1.5,0,(t-11000)/400*10],[0,1.5,0]],[centre])[0]
                points.append(dict(point(i+1,t),x=float(screen[0]),y=float(screen[1])))
            summary,rows=self.run_case(points,frames)
            self.assertEqual(summary['results'].get('motion_candidate'),4)
            self.assertTrue(all(r['status']=='motion_candidate' for r in rows))
            for row in rows:self.assertLess(float(np.linalg.norm(np.array(row['pdf_xy'])-centre)),1.)
        finally:
            probe.close();registration.close()

    def test_malformed_flow_refuses_without_crashing(self):
        for value in (None, [], [[float('nan'), 1]]*3, [[0, 0], [1, 0], [1, 1]]):
            frames = moving_frames()
            frames[1]['observation']['page_motion']['pages'][0]['support_hull_previous_screen_px'] = value
            _, rows = self.run_case(frames=frames)
            self.assertEqual(rows[-1]['status'], 'unresolved')

    def run_case(self,points=None,frames=None):
        return alignment_tests.CoordinateSafety().run_alignment(points=points or [point(1,11250)],
            frames=moving_frames() if frames is None else frames,stable_runs=True)

    def test_known_motion_interpolation_outputs_correct_distinct_candidate(self):
        summary,rows=self.run_case()
        r=rows[0]
        self.assertEqual(r['status'],'motion_candidate')
        self.assertEqual(r['pdf_xy'],[97.5,200])
        self.assertEqual(summary['results']['motion_candidate'],1)
        self.assertFalse(r['evidence']['temporal_extrapolation'])
        self.assertFalse(r['error_indicator_is_accuracy_bound'])

    def test_release_does_not_poison_later_pen_but_held_finger_still_blocks(self):
        for action,expected in ((0,'motion_candidate'),(3,'motion_candidate'),(2,'unresolved')):
            with self.subTest(action=action):
                _,rows=self.run_case([dict(point(1,11150,tool=0),action=1),
                    dict(point(2,11170,tool=0),action=action),point(3,11250)])
                self.assertEqual(rows[-1]['status'],expected)

    def test_no_motion_before_or_after_exposures_and_cancel_uncertainty(self):
        for t in (10900,11050,11860,12000):
            with self.subTest(t=t):
                _,rows=self.run_case([point(1,t)])
                self.assertNotEqual(rows[-1]['status'],'motion_candidate')
        _,rows=self.run_case([dict(point(1,11249.5,tool=0),action=0),point(2,11250)])
        self.assertEqual(rows[-1]['status'],'unresolved')

    def test_nonce_time_identity_missing_fit_bad_flow_and_foreign_window_refuse(self):
        for change in ('nonce','timestamp','generation','fit','flow','window','legacy','diagnostic'):
            with self.subTest(change=change):
                frames=moving_frames();f=frames[1]
                if change=='nonce':f['observation']['page_motion']['previous_request_id']='bad'
                elif change=='timestamp':f['observation']['page_motion']['previous_timestamp_raw']+=1
                elif change=='generation':f['observation']['motion_context_identity_generation_range']=[0,2]
                elif change=='fit':f['registration']['valid']=False
                elif change=='flow':f['observation']['page_motion']['pages'][0]['previous_screen_to_current_screen'][0][2]+=10
                elif change=='window':f['observation']['window']['window_id']=50
                elif change=='legacy':
                    for row in frames:row['observation'].pop('independent_page_exposure_requested')
                else:
                    f['motion_exposure_validation']=dict(checked=True,exposure=deepcopy(f))
                    f['observation']['valid']=False
                _,rows=self.run_case(frames=frames)
                self.assertEqual(rows[-1]['status'],'unresolved')

    def test_foreign_raw_window_excursion_cannot_hide_between_ui_checks(self):
        _,rows=self.run_case([point(1,11170,window=77),point(2,11180),point(3,11250)])
        self.assertTrue(all(r['status']=='unresolved' for r in rows))

    def test_prefix_held_contact_and_missing_clock_refuse(self):
        state=assess_clock(clocks());raw=[point(1,11250)]
        mapper=MotionCoordinates(moving_frames(),state,raw,
            dict(contacts=[([0,2,0],10000000)]))
        self.assertIsNone(mapper.locate(raw[0]))
        mapper=MotionCoordinates(moving_frames(),dict(usable_candidate=False),raw)
        self.assertIsNone(mapper.locate(raw[0]))

    def test_supported_stable_results_are_not_replaced(self):
        frames=moving_frames()
        for f in frames:
            f['registration'].update(pdf_to_screen=[[2,0,0],[0,2,0]],screen_to_pdf=[[.5,0,0],[0,.5,0]])
        _,rows=self.run_case(frames=frames)
        self.assertEqual(rows[0]['status'],'candidate')
        self.assertEqual(rows[0]['pdf_xy'],[100,200])


if __name__=='__main__':unittest.main()
