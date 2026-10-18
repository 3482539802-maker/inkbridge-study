"""Failure-path checks for temporary image cleanup and context rejection."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from visual_calibration import TabletFrames


class CollectorSafety(unittest.TestCase):
    def test_batched_context_refreshes_focus_pid_and_geometry_every_time(self):
        collector = TabletFrames.__new__(TabletFrames)
        window = {'focused':True,'window_id':123,'pid':456,'display_id':0}
        tree = {'document_title_candidate':'test','active_tab_marker_unique':True,'page_layers':[]}
        marker = 'INKBRIDGE_CONTEXT_BOUNDARY_57dced7c'
        collector.command = Mock(side_effect=['window','Pid: 456\nTREE',
            'window\n'+marker+'\nPid: 456\nTREE'])
        with patch('visual_calibration.notes_window',return_value=window), \
             patch('visual_calibration.extract_tree',return_value={}), \
             patch('visual_calibration.summarize_tree',return_value=tree):
            for _ in range(2):
                observed_window,context=collector.window_context()
                self.assertEqual(observed_window,window)
                self.assertEqual({k:v for k,v in context.items() if k!='editing'},tree)
                self.assertIsNone(context['editing']['selected_tool'])
        self.assertEqual(collector.command.call_count,3,'warm context uses one fresh HDC call instead of two')
        collector.command.return_value='window\n'+marker+'\nPid: 789\nTREE'
        collector.command.side_effect=None
        with patch('visual_calibration.notes_window',return_value=window):
            with self.assertRaisesRegex(ValueError,'PID mismatch'):collector.window_context()
        changed=dict(window,pid=789)
        with patch('visual_calibration.notes_window',return_value=changed):
            with self.assertRaisesRegex(ValueError,'window changed'):collector.window_context()
        collector.command.return_value='window\n'+marker+'\nprivate tree ignored'
        with patch('visual_calibration.notes_window',return_value=dict(changed,focused=False)):
            self.assertIsNone(collector.window_context()[1])

    def test_batch_failure_never_reuses_a_cached_context(self):
        collector = TabletFrames.__new__(TabletFrames)
        collector._context_window_hint={'focused':True,'window_id':123,'pid':456,'display_id':0}
        collector.command=Mock(return_value='truncated shell output without separator')
        with self.assertRaisesRegex(ValueError,'Malformed'):collector.window_context()

    def exercise(self, *, focus=True, title='test', changed=False, transfer_fail=False, corrupt=False,backend='uitest'):
        with tempfile.TemporaryDirectory(prefix='inkbridge-frame-test-') as folder:
            collector = TabletFrames.__new__(TabletFrames)
            collector.scratch = Path(folder)
            collector.remote = '/data/local/tmp/inkbridge-visual-frame.png'
            collector.prefix = ['fake-hdc']
            collector.backend = backend
            commands = []
            window = {'focused': focus, 'window_id': 123, 'pid': 456, 'display_id': 0}
            before = {'document_title_candidate': title, 'active_tab_marker_unique': True,
                      'page_layers': [{'page_id': 'abc', 'unclipped_window_bounds': [0, 0, 100, 200]}]}
            after = dict(before)
            if changed: after['page_layers'] = []

            def command(value):
                commands.append(value)
                return '1792150000000\n1792150000050' if value.startswith('date') else ''

            def receive(args, **kwargs):
                destination = Path(args[-1])
                if corrupt: destination.write_bytes(b'corrupt-image')
                else: destination.write_bytes(cv2.imencode('.png', np.zeros((100, 200, 3), np.uint8))[1].tobytes())
                return SimpleNamespace(returncode=int(transfer_fail), stdout=b'')

            collector.command = command
            collector.context = lambda _: before if sum(c.startswith('date') for c in commands) == 0 else after
            with patch('visual_calibration.notes_window', return_value=window), \
                 patch('visual_calibration.subprocess.run', side_effect=receive):
                if transfer_fail or corrupt:
                    with self.assertRaises(Exception): collector.capture('test')
                    result = None
                else:
                    _, result = collector.capture('test')
            self.assertEqual(list(Path(folder).glob('*.png')), [])
            if transfer_fail or corrupt: self.assertTrue(any(c.startswith('rm -f') for c in commands))
            return result, commands

    def test_stable_frame_and_exposure_interval(self):
        result, _ = self.exercise()
        self.assertTrue(result['valid'])
        self.assertEqual(result['device_capture_wall_interval_ms'], [1792150000000, 1792150000050])
        self.assertFalse(result['atomic_observation'])

    def test_other_foreground_does_not_capture(self):
        result, commands = self.exercise(focus=False)
        self.assertFalse(result['valid'])
        self.assertFalse(any(c.startswith('date') for c in commands))

    def test_other_title_does_not_capture(self):
        result, commands = self.exercise(title='other')
        self.assertFalse(result['valid'])
        self.assertFalse(any(c.startswith('date') for c in commands))

    def test_geometry_change_rejected(self):
        result, _ = self.exercise(changed=True)
        self.assertFalse(result['valid'])

    def test_transfer_failure_cleans_images(self): self.exercise(transfer_fail=True)
    def test_decode_failure_cleans_images(self): self.exercise(corrupt=True)

    def test_jpeg_backend_uses_explicit_type_and_cleans_its_own_remote(self):
        result, commands = self.exercise(backend='snapshot_display_jpeg')
        self.assertTrue(result['valid'])
        self.assertEqual(result['snapshot_backend'],'snapshot_display_jpeg')
        self.assertTrue(any('-t jpeg -f /data/local/tmp/inkbridge-visual-frame.jpeg' in c for c in commands))
        self.assertIn('rm -f /data/local/tmp/inkbridge-visual-frame.jpeg',commands)
        self.exercise(backend='snapshot_display_jpeg',transfer_fail=True)

    def test_native_png_backend_keeps_lossless_type_explicit(self):
        result, commands = self.exercise(backend='snapshot_display')
        self.assertTrue(result['valid'])
        self.assertTrue(any('-t png -f /data/local/tmp/inkbridge-visual-frame.png' in c for c in commands))


if __name__ == '__main__': unittest.main(verbosity=2)
