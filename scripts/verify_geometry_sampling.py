"""Faults, bounded pixels and prospective coordinate gaps; synthetic, no device."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from coordinate_alignment import align
from geometry_sampling import BracketedGeometryAcquisition
from screen_frame_probe import NativeProbeFrames
from verify_coordinate_alignment import frame, clocks, point


class Source:
    def __init__(self):
        f = frame(11000)
        self.window = f['observation']['window']
        self.context = f['observation']['context']
        self.count = self.reads = 0
        self.context_read = self.payload_read = None

    def window_context(self):
        self.reads += 1
        if self.context_read:
            self.context_read(self.reads)
        return deepcopy(self.window), deepcopy(self.context)

    def capture_payload(self):
        self.count += 1
        if self.payload_read:
            self.payload_read(self.count)
        t = 11000+self.count*250
        return object(), {'ordinal': self.count,
            'frame_request_started_at_pc_monotonic': time.monotonic(),
            'device_capture_wall_interval_ms': [1700000000000+t,1700000000000+t+10]}, time.monotonic()


def sampler(source, **kwargs):
    return BracketedGeometryAcquisition(source, 'test', lambda: 7, interval=.005, **kwargs)


class SamplingChecks(unittest.TestCase):
    def test_independent_exposure_keeps_moving_pixels_but_not_identity_failures(self):
        for change in ('bounds', 'page', 'focus', 'title'):
            source = Source()
            def context(n):
                if n != 2: return
                if change == 'bounds': source.context['page_layers'][0]['unclipped_window_bounds'][0] += 2
                elif change == 'page': source.context['page_layers'][0]['page_id'] = 'different'
                elif change == 'focus': source.window['focused'] = False
                else: source.context['document_title_candidate'] = 'different'
            source.context_read = context
            producer = sampler(source, independent_page_exposure=True)
            try:
                row = producer.next(timeout=2)
                self.assertFalse(row.observation['valid'])
                self.assertEqual(row.image is not None, change == 'bounds')
            finally: producer.close()

    def test_research_payload_is_opt_in_and_never_revives_geometry_or_identity_failure(self):
        from geometry_acquisition import Acquisition
        from geometry_sampling import Boundary, Pending
        for enabled, identity, retained in ((False, False, False), (True, False, True), (True, True, False)):
            with self.subTest(enabled=enabled, identity=identity):
                producer = BracketedGeometryAcquisition.__new__(BracketedGeometryAcquisition)
                producer.motion_validation = enabled
                import queue
                producer.queue = queue.Queue()
                f = frame(1000)
                before = Boundary(1, 1.1, f['observation']['window'], f['observation']['context'], 0, None, 0)
                after = Boundary(1.5, 1.6, before.window, before.context, 1, None, 1 if identity else 0)
                sample = Acquisition(object(), dict(payload_received=True, page_motion=dict(candidate=True),
                    frame_request_started_at_pc_monotonic=1.2), 1.2, 1.4, 0)
                producer._finish(Pending(sample, 1.4, before), after)
                self.assertFalse(sample.observation['valid'])
                self.assertEqual(sample.image is not None, retained)
                self.assertEqual(bool(sample.observation.get('motion_validation_payload_only')), retained)

    def test_motion_evidence_cannot_revive_failed_context_or_drop_valid_payload(self):
        for failure in ('motion','context'):
            with self.subTest(failure=failure):
                source=Source();probe=Mock()
                probe.observe.side_effect=RuntimeError('motion failed') if failure=='motion' else None
                probe.observe.return_value={'candidate':True,'coordinate_mapping_allowed':False}
                if failure=='context':
                    source.context_read=lambda n: source.window.update(focused=False) if n==2 else None
                producer=BracketedGeometryAcquisition(source,'test',lambda:7,.005,motion_probe=probe)
                try:
                    row=producer.next(timeout=2)
                    if failure=='motion':
                        self.assertTrue(row.observation['valid'])
                        self.assertIsNotNone(row.image)
                        self.assertEqual(row.observation['page_motion']['reason'],'motion_probe_failed')
                    else:
                        self.assertFalse(row.observation['valid'])
                        self.assertIsNone(row.image)
                        self.assertTrue(row.observation['page_motion']['candidate'])
                finally:producer.close()
                probe.close.assert_called_once()

    def test_slow_lease_renewal_refreshes_preflight_before_request(self):
        source = Source()
        renewed = threading.Event()
        original = source.capture_payload
        def prepare():
            time.sleep(.06)
            renewed.set()
            return True
        source.prepare_payload = prepare
        source.capture_prepared_payload = original
        source.capture_payload = Mock(side_effect=AssertionError('must use prepared capture'))
        producer = sampler(source)
        producer.maximum_preflight_age = .025
        try:
            row = producer.next(timeout=2)
            self.assertTrue(renewed.is_set())
            self.assertTrue(row.observation['valid'])
            self.assertLessEqual(row.observation['context_preflight_age_ms'], 25)
            self.assertGreaterEqual(source.reads, 3)
        finally:
            producer.close()

    def test_failed_or_stopped_preparation_never_requests_pixels(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                source = Source()
                def prepare():
                    if fails: raise OSError('lease unavailable')
                    return False
                source.prepare_payload = prepare
                source.capture_prepared_payload = Mock(side_effect=AssertionError('no request permitted'))
                producer = sampler(source)
                try:
                    row = producer.next(timeout=1)
                    self.assertFalse(row.observation['valid'])
                    self.assertIsNone(row.image)
                    self.assertEqual(row.observation['reason'], 'context_or_acquisition_failed' if fails else 'user_capture_not_active')
                    self.assertEqual(source.count, 0)
                finally:
                    producer.close()

    def test_slow_context_does_not_stop_sampling_and_each_frame_gets_a_later_postflight(self):
        source = Source()
        checking, release, three = threading.Event(), threading.Event(), threading.Event()
        def context(n):
            if n == 2:
                checking.set()
                if not release.wait(2):
                    raise RuntimeError('test did not release context')
        def payload(n):
            if n == 3:
                three.set()
        source.context_read, source.payload_read = context, payload
        producer = sampler(source)
        try:
            self.assertTrue(checking.wait(1))
            self.assertTrue(three.wait(1), 'native requests continue during the slow context read')
            time.sleep(.02)
            self.assertEqual(source.count, 3, 'backpressure bounds queued, pending and in-flight pixels together')
            self.assertTrue(producer.queue.empty(), 'no frame may escape without its postflight')
            release.set()
            rows = [producer.next(timeout=2) for _ in range(3)]
            self.assertEqual([r.observation['ordinal'] for r in rows], [1,2,3])
            self.assertTrue(all(r.observation['valid'] for r in rows))
            self.assertTrue(all(r.observation['context_postflight_started_after_payload'] for r in rows))
            self.assertTrue(all(r.guard_token == 7 for r in rows))
            # Frame 2/3 arrived during read 2 and must await read 3, even
            # though read 2 FINISHED after their arrival.
            self.assertGreater(rows[1].acquired, rows[0].acquired)
        finally:
            release.set()
            producer.close()

    def test_failed_context_poison_is_not_lost_for_payload_received_during_the_failed_read(self):
        source = Source()
        second = threading.Event()
        source.payload_read = lambda n: second.set() if n == 2 else None
        def context(n):
            if n == 2:
                if not second.wait(1):
                    raise RuntimeError('second payload did not overlap')
                raise OSError('context unavailable')
        source.context_read = context
        producer = sampler(source)
        try:
            samples = []
            while not any(r.observation.get('ordinal') == 2 for r in samples):
                samples.append(producer.next(timeout=2))
            by_id = {r.observation.get('ordinal'): r for r in samples}
            for i in (1,2):
                self.assertFalse(by_id[i].observation['valid'])
                self.assertIsNone(by_id[i].image)
            self.assertEqual(by_id[2].observation['reason'], 'context_boundary_failed_during_frame')
        finally:
            producer.close()

    def test_observed_page_change_and_return_cannot_be_hidden_between_equal_endpoints(self):
        source = Source()
        second = threading.Event()
        source.payload_read = lambda n: second.set() if n == 2 else None
        def context(n):
            if n == 2:
                if not second.wait(1): raise RuntimeError('second payload did not overlap')
                source.context['page_layers'][0]['page_id'] = 'other-page'
            elif n == 3:
                source.context['page_layers'][0]['page_id'] = 'page'
        source.context_read = context
        producer = sampler(source)
        try:
            rows = [producer.next(timeout=2) for _ in range(2)]
            self.assertFalse(rows[1].observation['valid'])
            self.assertEqual(rows[1].observation['context_before'], rows[1].observation['context'])
            self.assertEqual(rows[1].observation['reason'], 'context_boundary_failed_during_frame')
            generations=rows[1].observation['motion_context_identity_generation_range']
            self.assertGreater(generations[1],generations[0],'motion chains must also see an observed A-B-A excursion')
        finally:
            producer.close()

    def test_initial_unbound_document_never_requests_pixels_and_keeps_failure_row(self):
        source = Source()
        source.context['document_title_candidate'] = 'unlisted'
        producer = sampler(source)
        try:
            row = producer.next(timeout=1)
            self.assertEqual(row.observation['reason'], 'different_or_ambiguous_document')
            self.assertIsNone(row.image)
            self.assertEqual(source.count, 0)
        finally:
            producer.close()

    def test_context_changes_still_reject_pixels(self):
        for change in ('focus','window','title','page','bounds'):
            with self.subTest(change=change):
                source = Source()
                def context(n):
                    if n == 2:
                        if change == 'focus': source.window['focused'] = False
                        elif change == 'window': source.window['window_id'] += 1
                        elif change == 'title': source.context['document_title_candidate'] = 'other'
                        elif change == 'page': source.context['page_layers'][0]['page_id'] = 'other'
                        else: source.context['page_layers'][0]['unclipped_window_bounds'][0] += 1
                source.context_read = context
                producer = sampler(source)
                try:
                    row = producer.next(timeout=1)
                    self.assertFalse(row.observation['valid'])
                    self.assertIsNone(row.image)
                finally:
                    producer.close()

    def test_slow_or_stale_context_never_becomes_usable(self):
        source = Source()
        source.context_read = lambda n: time.sleep(.03) if n > 1 else None
        producer = sampler(source)
        producer.maximum_context_span = .01
        try:
            row = producer.next(timeout=1)
            self.assertFalse(row.observation['valid'])
            self.assertEqual(row.observation['reason'], 'context_bracket_too_wide')
        finally:
            producer.close()

    def test_lease_preparation_cannot_hide_age_of_preflight_at_actual_request(self):
        source = Source()
        source.payload_read = lambda n: time.sleep(.03)
        producer = sampler(source)
        producer.maximum_preflight_age = .01
        try:
            row = producer.next(timeout=1)
            self.assertFalse(row.observation['valid'])
            self.assertEqual(row.observation['reason'], 'stale_preflight_context')
        finally:
            producer.close()

    def test_payload_failure_is_retained_between_good_frames(self):
        source = Source()
        def payload(n):
            if n == 2: raise ValueError('corrupt packet')
        source.payload_read = payload
        producer = sampler(source)
        try:
            rows = [producer.next(timeout=1) for _ in range(3)]
            self.assertEqual([r.observation['valid'] for r in rows], [True,False,True])
            self.assertEqual(rows[1].observation['reason'], 'context_or_acquisition_failed')
            self.assertIsNone(rows[1].image)
        finally:
            producer.close()

    def test_continuous_sampling_crosses_100_without_an_image_backlog(self):
        producer = sampler(Source())
        try:
            for ordinal in range(1,126):
                row = producer.next(timeout=1)
                self.assertEqual(row.observation['ordinal'], ordinal)
                self.assertLessEqual(producer.queue.qsize(), producer.capacity)
                row.image = None
        finally:
            producer.close()
        self.assertFalse(any(t.is_alive() for t in producer.threads))

    def test_unexpected_worker_failure_is_reported_and_both_threads_stop(self):
        source = Source()
        def payload(n): raise TypeError('unexpected implementation failure')
        source.payload_read = payload
        producer = sampler(source)
        try:
            with self.assertRaises(TypeError): producer.next(timeout=1)
        finally:
            producer.close()

    def test_close_joins_inflight_cleanup_and_discards_all_pixels(self):
        source = Source()
        started, release, cleaned = threading.Event(), threading.Event(), threading.Event()
        def payload(n):
            started.set()
            try: release.wait(1)
            finally: cleaned.set()
        source.payload_read = payload
        producer = sampler(source)
        self.assertTrue(started.wait(1))
        closing = threading.Thread(target=producer.close)
        closing.start();release.set();closing.join(2)
        self.assertFalse(closing.is_alive())
        self.assertTrue(cleaned.is_set())
        self.assertTrue(producer.queue.empty())
        self.assertFalse(producer.pending)
        self.assertFalse(any(t.is_alive() for t in producer.threads))

    def test_native_file_payload_is_cleared_before_context_can_be_published(self):
        source = NativeProbeFrames.__new__(NativeProbeFrames)
        source.request_payload = Mock(return_value=(object(), {'frame_transport':'hdc_file'}, 1))
        source.send_text = Mock(side_effect=RuntimeError('clear failed'))
        with self.assertRaises(RuntimeError): source.capture_payload()
        source.send_text.assert_called_once_with('geometry-request.txt', 'clear\n')

    def test_dense_exposures_avoid_nearby_touch_veto_without_relaxing_any_alignment_gate(self):
        source = Source()
        producer = sampler(source)
        try:
            rows = [producer.next(timeout=1) for _ in range(6)]
        finally:
            producer.close()
        samples = [{'index':i,'observation':r.observation,'registration':frame(11000)['registration']}
                   for i,r in enumerate(rows)]
        prefix = [point(i+1,t-10) for i,t in enumerate((1000,6000,11000))]
        raw_points = prefix+[point(4,11900),dict(point(5,12080),tool_type=0,action=1),
                             dict(point(6,12090),tool_type=0,action=3)]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); raw = root/'raw.jsonl'
            original = ''.join(json.dumps(p)+'\n' for p in raw_points).encode()
            raw.write_bytes(original)
            for name,frames in [('sparse',[frame(11000),frame(13000)]),('dense',samples)]:
                path = root/(name+'.jsonl')
                align(raw,clocks(),frames,path,stable_runs=True)
                result = json.loads(path.read_text().splitlines()[-1])
                self.assertEqual(result['seq'],4)
                if name == 'sparse': self.assertEqual(result['reason'],'input_motion_or_context_barrier')
                else:
                    self.assertEqual(result['status'],'candidate')
                    self.assertEqual(result['pdf_xy'],[100,200])
            self.assertEqual(raw.read_bytes(),original)


if __name__ == '__main__': unittest.main(verbosity=2)
