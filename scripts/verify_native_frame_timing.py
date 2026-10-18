"""Native frame times reject ambiguity, jumps, stale frames and invalid contexts."""
from copy import deepcopy
import unittest
from native_frame_timing import bind


def example():
    origin = 1700000000000000
    frames, anchors = [], []
    for t in (1000000, 6000000, 11000000):
        anchors.append(dict(monotonic_before_us=t-1000, monotonic_after_us=t-998,
                            unix_ms=(origin+t-999)/1000, boottime_us=t-999+800000000))
        frames.append({'observation': {'valid': True, 'native_frame': dict(timestamp_raw=t*1000,
            callback_monotonic_us=t+10000, callback_boottime_us=t+800010000,
            callback_wall_us=origin+t+10000)}, 'registration': {'valid': True}})
    anchors.append(dict(anchors[-1], monotonic_before_us=12000000, monotonic_after_us=12000002,
                        unix_ms=(origin+12000001)/1000, boottime_us=812000001))
    return frames, {'anchors': anchors}


class TimingChecks(unittest.TestCase):
    def test_independent_clock_candidate_preserves_context_veto(self):
        frames, clock = example()
        frames[1]['observation'].update(valid=False, reason='real_scroll')
        original = deepcopy(frames)
        output, calibration = bind(frames, clock)
        self.assertTrue(calibration['usable_candidate'])
        self.assertFalse(calibration['verified'])
        self.assertEqual(frames, original)
        self.assertFalse(output[1]['observation']['valid'])
        self.assertEqual(output[1]['observation']['reason'], 'real_scroll')
        lo, hi = output[1]['observation']['device_capture_wall_interval_ms']
        self.assertLess(hi-lo, 2.1)
        self.assertLess(lo, 1700000006000)
        self.assertGreater(hi, 1700000006000)
        # Request file I/O occurs before the boottime sample. A known bounded
        # delay must not be mistaken for a clock-domain change.
        frames[0]['observation']['native_frame'].update(callback_boottime_us=801016000,
                                                       copy_finished_monotonic_us=1020000)
        self.assertTrue(bind(frames,clock)[1]['usable_candidate'])

    def test_ambiguity_jumps_inadequate_span_and_outside_anchors(self):
        for mode in ('ambiguous', 'few_frames', 'few_anchors', 'wall_jump', 'boot_jump', 'mismatch'):
            frames, clock = example()
            if mode == 'ambiguous':
                for f in frames:
                    m = f['observation']['native_frame']
                    m['callback_boottime_us'] = m['callback_monotonic_us']
            if mode == 'few_frames': frames.pop()
            if mode == 'few_anchors': clock['anchors'] = clock['anchors'][:2]
            if mode == 'wall_jump': clock['anchors'][1]['unix_ms'] += 10
            if mode == 'boot_jump': clock['anchors'][1]['boottime_us'] += 10000
            if mode == 'mismatch': frames[0]['observation']['native_frame']['callback_wall_us'] += 10000
            output, calibration = bind(frames, clock)
            self.assertFalse(calibration['usable_candidate'], mode)
            self.assertTrue(all(f['observation']['device_capture_wall_interval_ms'] is None for f in output))
        frames, clock = example()
        clock['anchors'].pop()
        output, calibration = bind(frames, clock)
        self.assertTrue(calibration['usable_candidate'])
        self.assertIsNone(output[-1]['observation']['device_capture_wall_interval_ms'])


if __name__ == '__main__': unittest.main(verbosity=2)
