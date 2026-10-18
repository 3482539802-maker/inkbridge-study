"""Bounded overlap preserves failures, timestamps, input barriers and cleanup."""
from copy import deepcopy
import threading
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

from geometry_acquisition import GeometryAcquisition
from screen_frame_probe import NativeProbeFrames


class AcquisitionChecks(unittest.TestCase):
    def test_socket_frame_keeps_context_gate_and_avoids_file_payloads(self):
        window={'focused':True,'pid':1,'window_id':2,'display_id':0}
        context={'active_tab_marker_unique':True,'document_title_candidate':'note','page_layers':[]}
        with tempfile.TemporaryDirectory() as temporary:
            collector=NativeProbeFrames.__new__(NativeProbeFrames)
            collector.size=(640,480);collector.scratch=Path(temporary);collector.prefix=['fake','-t','tablet']
            collector.send_text=Mock()
            collector.window_context=Mock(side_effect=[(window,context),(dict(window,focused=False),None)])
            transport=collector._socket_transport=Mock(connection=object())
            transport.request.side_effect=lambda *args:(b'PACKET',time.monotonic())
            with patch('screen_frame_probe.decode_packet',return_value=(object(),{'timestamp_raw':1})), \
                 patch('screen_frame_probe.subprocess.run') as hdc:
                _,row=collector.capture('note')
            self.assertFalse(row['valid'],'a socket frame never bypasses fresh focus/page checks')
            self.assertEqual(row['frame_transport'],'loopback_socket')
            self.assertFalse(row['cleanup_postflight_overlapped'])
            collector.send_text.assert_not_called();hdc.assert_not_called()
            self.assertEqual(list(collector.scratch.iterdir()),[])

    def test_socket_failure_backs_off_and_existing_route_reused(self):
        collector=NativeProbeFrames.__new__(NativeProbeFrames)
        collector.prefix=['fake','-t','tablet']
        transport=collector._socket_transport=Mock(connection=None)
        transport.request.side_effect=ConnectionError('disconnected')
        with patch('screen_frame_probe.subprocess.run',return_value=SimpleNamespace(returncode=0,
            stdout=b'tablet    tcp:18769 tcp:18769    [Forward]\n')) as hdc:
            self.assertIsNone(collector.socket_packet('a'*32,None))
            self.assertIsNone(collector.socket_packet('b'*32,None))
        self.assertEqual(hdc.call_count,1,'do not recreate a valid route or retry each frame')
        transport.request.assert_called_once()
        transport.close.assert_called_once()

    def test_socket_corrupt_frame_rejected_without_publishing_or_file_retry(self):
        window={'focused':True,'pid':1,'window_id':2,'display_id':0}
        context={'active_tab_marker_unique':True,'document_title_candidate':'note','page_layers':[]}
        collector=NativeProbeFrames.__new__(NativeProbeFrames)
        collector.size=(640,480)
        collector.window_context=Mock(return_value=(window,context));collector.send_text=Mock()
        collector._socket_transport=Mock(connection=object())
        collector._socket_transport.request.return_value=(b'corrupt',time.monotonic())
        with self.assertRaises(ValueError):collector.capture('note')
        collector._socket_transport.close.assert_called_once()
        collector.send_text.assert_not_called()
        self.assertIsNone(collector._postflight_boundary)

    def test_rearm_during_capture_cannot_close_active_transport(self):
        collector=NativeProbeFrames.__new__(NativeProbeFrames)
        collector._socket_transport=Mock()
        with patch('screen_frame_probe.receiver',return_value=[{'state':'recording'}]):
            with self.assertRaises(ValueError):collector.arm(continuous=True)
        collector._socket_transport.close.assert_not_called()

    def check_cleanup_overlap(self, context_failure=False, cleanup_failure=False):
        clear_started, context_finished = threading.Event(), threading.Event()
        window={'focused':True,'pid':1,'window_id':2,'display_id':0}
        context={'active_tab_marker_unique':True,'document_title_candidate':'note','page_layers':[]}
        with tempfile.TemporaryDirectory() as temporary:
            collector=NativeProbeFrames.__new__(NativeProbeFrames)
            collector.size=(640,480);collector.scratch=Path(temporary);collector.prefix=['fake']
            collector._postflight_boundary=(time.monotonic(),window,context)
            def send(name,value):
                if value!='clear\n':return
                clear_started.set()
                self.assertTrue(context_finished.wait(2),'cleanup must overlap fresh context')
                if cleanup_failure:raise RuntimeError('cleanup failed')
            def check():
                self.assertTrue(clear_started.wait(2))
                context_finished.set()
                if context_failure:raise RuntimeError('context failed')
                return window,context
            collector.send_text=send;collector.window_context=check
            def transfer(argv,**kwargs):
                Path(argv[-1]).write_bytes(json.dumps({'request_id':'a'*32}).encode()+b'\nJPEG')
                return SimpleNamespace(returncode=0,stdout=b'OK')
            with patch('screen_frame_probe.uuid.uuid4',return_value=SimpleNamespace(hex='a'*32)), \
                 patch('screen_frame_probe.subprocess.run',side_effect=transfer), \
                 patch('screen_frame_probe.decode_packet',return_value=(object(),{'timestamp_raw':1})):
                if context_failure or cleanup_failure:
                    with self.assertRaises(RuntimeError):collector.capture('note')
                    self.assertIsNone(collector._postflight_boundary)
                else:
                    _,row=collector.capture('note')
                    self.assertTrue(row['valid'])
                    self.assertTrue(row['cleanup_postflight_overlapped'])
            self.assertTrue(context_finished.is_set())
            self.assertEqual(list(collector.scratch.iterdir()),[])

    def test_cleanup_and_context_overlap_and_join(self):
        self.check_cleanup_overlap()

    def test_context_failure_still_joins_cleanup(self):
        self.check_cleanup_overlap(context_failure=True)

    def test_cleanup_failure_rejects_frame_and_shared_boundary(self):
        self.check_cleanup_overlap(cleanup_failure=True)

    def test_overlaps_source_with_registration_without_unbounded_images(self):
        second_started = threading.Event()
        count = 0
        def capture(title):
            nonlocal count
            count += 1
            if count == 2: second_started.set()
            return object(), {'valid': True, 'ordinal': count}
        frames = Mock(capture=capture)
        token = [7]
        source = GeometryAcquisition(frames, 'note', lambda: token[0], 0, 100, 10)
        try:
            first = source.next()
            self.assertEqual(first.guard_token,7)
            self.assertTrue(second_started.wait(1), 'source must run while first image is registered')
            time.sleep(.02)
            self.assertEqual(count,2,'backpressure permits only one extra image')
            token[0] = 9
            second = source.next()
            third = source.next()
            self.assertEqual(third.guard_token,9,'each frame obtains its own current raw barrier token')
            self.assertLessEqual(first.started,first.acquired)
            self.assertLessEqual(second.started,second.acquired)
        finally: source.close()
        self.assertTrue(source.queue.empty())
        self.assertFalse(source.thread.is_alive())

    def test_invalid_observations_are_never_dropped_or_reused(self):
        frames = Mock()
        frames.capture.side_effect = [(object(),{'valid':True}), RuntimeError('timeout'),
                                     (object(),{'valid':True})]
        source = GeometryAcquisition(frames,'note',lambda: 1,0,3,10)
        try:
            rows=[source.next() for _ in range(3)]
            self.assertEqual([r.observation['valid'] for r in rows],[True,False,True])
            self.assertIsNone(rows[1].image)
            self.assertEqual(rows[1].observation['reason'],'context_or_acquisition_failed')
            self.assertIsNone(source.next())
        finally: source.close()

    def test_close_waits_for_inflight_source_finally_and_discards_queued_image(self):
        started, release, cleaned = threading.Event(), threading.Event(), threading.Event()
        def capture(title):
            started.set()
            try:
                release.wait(1)
                return object(), {'valid':True}
            finally: cleaned.set()
        source = GeometryAcquisition(Mock(capture=capture),'note',lambda: 1,0,10,10)
        self.assertTrue(started.wait(1))
        closing=threading.Thread(target=source.close)
        closing.start()
        release.set()
        closing.join(2)
        self.assertFalse(closing.is_alive())
        self.assertTrue(cleaned.is_set())
        self.assertTrue(source.queue.empty())

    def test_recent_boundary_shared_but_old_context_and_failure_cleared(self):
        collector=NativeProbeFrames.__new__(NativeProbeFrames)
        window={'focused':True,'pid':1,'window_id':2,'display_id':0}
        context={'active_tab_marker_unique':True,'document_title_candidate':'note','page_layers':[]}
        collector.window_context=Mock(return_value=(window,context))
        collector.send_text=Mock(side_effect=RuntimeError('send failed'))
        collector._postflight_boundary=(9.95,deepcopy(window),deepcopy(context))
        with patch('screen_frame_probe.time.monotonic',return_value=10):
            with self.assertRaises(RuntimeError):collector.capture('note')
        collector.window_context.assert_not_called()
        self.assertIsNone(collector._postflight_boundary,'a failed frame cannot lend its boundary onward')
        collector._postflight_boundary=(9.8,window,context)
        with patch('screen_frame_probe.time.monotonic',return_value=10):
            with self.assertRaises(RuntimeError):collector.capture('note')
        collector.window_context.assert_called_once()

    def test_each_pipelined_frame_checks_fresh_postflight_and_focus_change(self):
        window={'focused':True,'pid':1,'window_id':2,'display_id':0}
        context={'active_tab_marker_unique':True,'document_title_candidate':'note','page_layers':[]}
        with tempfile.TemporaryDirectory() as temporary:
            collector=NativeProbeFrames.__new__(NativeProbeFrames)
            collector.size=(640,480)
            collector.scratch=Path(temporary)
            collector.prefix=['fake-hdc']
            collector.send_text=Mock()
            collector.window_context=Mock(side_effect=[(window,context),(window,context),
                (dict(window,focused=False),None)])
            def transfer(argv,**kwargs):
                Path(argv[-1]).write_bytes(json.dumps({'request_id':'a'*32}).encode()+b'\nJPEG')
                return SimpleNamespace(returncode=0,stdout=b'OK')
            with patch('screen_frame_probe.uuid.uuid4',return_value=SimpleNamespace(hex='a'*32)), \
                 patch('screen_frame_probe.subprocess.run',side_effect=transfer), \
                 patch('screen_frame_probe.decode_packet',return_value=(object(),{'timestamp_raw':1})), \
                 patch('screen_frame_probe.time.monotonic',return_value=10):
                _,first=collector.capture('note')
                _,second=collector.capture('note')
            self.assertTrue(first['valid'])
            self.assertFalse(first['shared_preflight_boundary'])
            self.assertFalse(second['valid'],'fresh postflight must detect focus loss')
            self.assertTrue(second['shared_preflight_boundary'])
            self.assertEqual(collector.window_context.call_count,3,'two frames share only their common boundary')
            self.assertEqual(list(collector.scratch.iterdir()),[],'all local packets cleaned')


if __name__=='__main__':unittest.main(verbosity=2)
