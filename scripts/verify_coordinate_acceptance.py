"""Acceptance cannot turn static frames or copied timestamps into pen proof."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from coordinate_acceptance import audit
from coordinate_alignment import align
from native_frame_timing import bind
from verify_coordinate_alignment import clock_sample, frame, point
from verify_native_frame_timing import example


def fixture():
    frames, state=example()
    # Real production gates require independent event-clock observations that
    # match the raw sequence; the native frame timestamp alone is insufficient.
    points=[point(1,1100),point(2,6100),point(3,7000),point(4,11100)]
    state.update(session_id='synthetic',observations=[
        dict(seq=p['seq'],action_time_us=p['action_time'],callback_clock=dict(
            clock_sample(p['action_time']/1000+1),boottime_us=p['action_time']+800001000))
        for p in (points[0],points[1],points[3])])
    frames.insert(2,deepcopy(frames[1]))
    for t,f in zip((1000,6000,8000,11000),frames):
        f['observation']['native_frame'].update(timestamp_raw=t*1000000,
            callback_monotonic_us=t*1000+10000,callback_boottime_us=t*1000+800010000,
            callback_wall_us=1700000000000000+t*1000+10000)
        f['observation'].update({k:v for k,v in frame(t)['observation'].items() if k!='device_capture_wall_interval_ms'})
        f['registration']=frame(t)['registration']
        f['index']=t
        f.update(source_observation_ms=500,registration_ms=100,processing_ms=600)
    frames,_=bind(frames,state)
    return points,state,frames


class AcceptanceChecks(unittest.TestCase):
    def exercise(self,points,state,frames,page_margins=False):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            raw,clock,geometry,locations=[root/name for name in ('raw.jsonl','clock.json','geometry.jsonl','points.jsonl')]
            raw.write_text(''.join(json.dumps(p)+'\n' for p in points))
            clock.write_text(json.dumps(state))
            geometry.write_text(''.join(json.dumps(f)+'\n' for f in frames))
            before=raw.read_bytes()
            align(raw,state,frames,locations,allow_page_margins=page_margins)
            report=audit(raw,clock,geometry,locations)
            self.assertEqual(raw.read_bytes(),before)
            return report

    def test_native_pen_evidence_and_accuracy_are_separate(self):
        points,state,frames=fixture()
        report=self.exercise(points,state,frames)
        self.assertTrue(report['native_backend_with_pen_exercised'])
        self.assertGreater(report['native_bracketed_candidate_pen_points'],0)
        self.assertFalse(report['native_backend_full_pen_coverage'])
        self.assertFalse(report['physical_mm_accuracy_verified'])
        self.assertIsNone(report['latency_ms']['recognition_ms'])
        self.assertEqual(report['latency_ms']['source_observation_ms']['median'],500)

    def test_static_frames_with_fingers_are_not_writing_acceptance(self):
        points,state,frames=fixture()
        for p in points:p['tool_type']=0
        report=self.exercise(points,state,frames)
        self.assertEqual(report['pen_points'],0)
        self.assertEqual(report['native_frames'],4)
        self.assertFalse(report['native_backend_with_pen_exercised'])
        self.assertFalse(report['native_backend_full_pen_coverage'])

    def test_acquisition_failures_are_separate_from_native_frame_speed(self):
        points,state,frames=fixture()
        frames.append({'index':12000,'observation':{'valid':False,'reason':'context_or_acquisition_failed'},
                       'registration':{'valid':False},'source_observation_ms':5500,'processing_ms':5600})
        report=self.exercise(points,state,frames)
        latency=report['latency_ms']
        self.assertEqual(latency['native_acquired_frames_ms']['source_observation_ms']['count'],4)
        self.assertEqual(latency['native_acquired_frames_ms']['source_observation_ms']['median'],500)
        self.assertEqual(latency['acquisition_failed_attempts_ms']['source_observation_ms']['median'],5500)

    def test_unverified_native_clock_does_not_pass_even_with_valid_flags(self):
        points,state,frames=fixture()
        for f in frames:f['observation']['native_frame']['callback_wall_us']+=10000
        report=self.exercise(points,state,frames)
        self.assertFalse(report['native_clock']['usable_candidate'])
        self.assertFalse(report['native_backend_with_pen_exercised'])

    def test_capture_cadence_includes_gaps_and_separates_pipeline_wait(self):
        points,state,frames=fixture()
        for f in frames:
            m=f['observation']['native_frame']
            m.update(copy_finished_monotonic_us=m['callback_monotonic_us']+1000,
                     encoded_monotonic_us=m['callback_monotonic_us']+3000)
            f['acquired_to_registration_wait_ms']=700
        frames.insert(1,{'index':2000,'observation':{'valid':False,'reason':'context_or_acquisition_failed'},
                         'registration':{'valid':False}})
        latency=self.exercise(points,state,frames)['latency_ms']
        self.assertEqual(latency['native_capture_interval_ms']['count'],3)
        self.assertEqual(latency['native_capture_interval_ms']['max'],5000)
        self.assertEqual(latency['native_capture_interval_ms']['median'],3000)
        self.assertTrue(latency['native_capture_clock_monotonic'])
        self.assertEqual(latency['native_copy_ms']['median'],1)
        self.assertEqual(latency['native_encode_ms']['median'],2)
        self.assertEqual(latency['acquired_to_registration_wait_ms']['median'],700)

    def test_reversed_capture_clock_cannot_report_positive_cadence(self):
        points,state,frames=fixture()
        frames[1]['observation']['native_frame']['callback_monotonic_us']=0
        latency=self.exercise(points,state,frames)['latency_ms']
        self.assertFalse(latency['native_capture_clock_monotonic'])
        self.assertEqual(latency['native_capture_interval_ms']['count'],0)

    def test_margin_coverage_and_after_observation_are_separate(self):
        points,state,frames=fixture()
        for f in frames:
            f['registration'].update(pdf_size_pt=[500,400],
                support_hull_screen_px=[[300,200],[900,200],[900,700],[300,700]],
                native_background_binding={'page_uuid':'page','source_pdf_sha256':'a'*64,
                    'pdf_page_1based':1,'source_hinote_sha256':'b'*64,'note_id':'note'})
        report=self.exercise(points,state,frames,page_margins=True)
        self.assertGreater(report['extrapolated_candidate_points'],0)
        self.assertEqual(report['candidate_points'],0)
        self.assertEqual(report['pen_points'],report['located_candidate_points']+report['unresolved_points'])
        self.assertNotIn('extrapolated_candidate',report['unresolved_reasons'])
        self.assertEqual(report['unresolved_after_last_observed_frame'],1)
        self.assertFalse(report['native_backend_full_pen_coverage'])


if __name__=='__main__':unittest.main(verbosity=2)
