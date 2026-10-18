"""Conservative frame time candidates tied to independent capture clock anchors.

Only supplies intervals for actual, timestamped frame packets. Does not create
page transforms, revive invalid UI/registration frames, or change raw journals.
"""
from copy import deepcopy
import math


def finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def calibrate(frames, clock):
    from screen_frame_probe import assess_timestamps
    metadata = [f['observation']['native_frame'] for f in frames
                if f.get('observation', {}).get('native_frame')]
    rejected = {'usable_candidate': False, 'verified': False}
    if clock.get('history_lost') or (clock.get('history_merged') and not clock.get('history_complete')):
        return {**rejected, 'reason': 'incomplete_clock_history'}
    diagnosis = assess_timestamps(metadata)
    candidates = diagnosis.get('candidates', [])
    if (len(metadata) < 3 or diagnosis.get('span_ms', 0) < 8000 or len(candidates) != 1 or
            candidates[0]['unit'] != 'ns' or candidates[0]['domain'] != 'monotonic'):
        return {**rejected, 'reason': 'frame_clock_insufficient_or_ambiguous', 'diagnosis': diagnosis}
    anchors = clock.get('anchors', [])
    if len(anchors) < 3:
        return {**rejected, 'reason': 'insufficient_independent_clock_anchors'}
    offsets = []
    boots = []
    monotonic = []
    for a in anchors:
        values = [a.get(k) for k in ('monotonic_before_us', 'monotonic_after_us', 'unix_ms', 'boottime_us')]
        if not all(finite(v) for v in values):
            return {**rejected, 'reason': 'invalid_clock_anchor'}
        before, after, wall, boot = values
        if not 0 <= after - before <= 1000:
            return {**rejected, 'reason': 'clock_anchor_too_wide'}
        monotonic.append(before)
        offsets.append([wall * 1000 - after, wall * 1000 - before])
        boots.append(boot - (before + after) / 2)
    if any(b <= a for a, b in zip(monotonic, monotonic[1:])) or monotonic[-1] - monotonic[0] < 8000000:
        return {**rejected, 'reason': 'independent_clock_span_or_order'}
    low, high = min(a[0] for a in offsets), max(a[1] for a in offsets)
    if high - low > 2000 or max(boots) - min(boots) > 2000:
        return {**rejected, 'reason': 'clock_jump_or_suspend'}
    # A 1ms pad on each side covers numeric clock precision and observed
    # anchor rounding; it is not a guaranteed physical exposure error bound.
    low, high = low - 1000, high + 1000
    for m in metadata:
        offset = m['callback_wall_us'] - m['callback_monotonic_us']
        boot = m['callback_boottime_us'] - m['callback_monotonic_us']
        # This native version samples boottime AFTER reading the request file,
        # but before copy_finished. Bound its actual sampling interval rather
        # than pretending it was simultaneous with callback_monotonic_us.
        boot_read_interval = m.get('copy_finished_monotonic_us',m['callback_monotonic_us'])-m['callback_monotonic_us']
        if (not finite(boot_read_interval) or boot_read_interval < 0 or
            not low - 2000 <= offset <= high + 2000 or
            not min(boots) - 1000 <= boot <= max(boots) + boot_read_interval + 1000):
            return {**rejected, 'reason': 'native_and_independent_clock_disagree'}
    return {'usable_candidate': True, 'verified': False, 'identity': 'CLOCK_MONOTONIC_ns_candidate',
            'offset_interval_us': [low, high], 'anchor_span_us': [monotonic[0], monotonic[-1]],
            'independent_anchor_count': len(anchors), 'frame_count': len(metadata),
            'reason': None, 'physical_exposure_or_pen_accuracy_verified': False}


def bind(frames, clock):
    calibration = calibrate(frames, clock)
    output = deepcopy(frames)
    for frame in output:
        obs = frame.get('observation', {})
        m = obs.get('native_frame')
        if m is None:
            continue
        obs.update(device_capture_wall_interval_ms=None, timestamp_used_for_coordinates=False,
                   native_time_calibration=calibration)
        if not calibration['usable_candidate']:
            continue
        stamp = m['timestamp_raw'] / 1000
        if not calibration['anchor_span_us'][0] <= stamp <= calibration['anchor_span_us'][1]:
            obs['native_time_rejection'] = 'frame_outside_independent_anchors'
            continue
        lo, hi = calibration['offset_interval_us']
        obs.update(device_capture_wall_interval_ms=[(stamp + lo) / 1000, (stamp + hi) / 1000],
                   timestamp_used_for_coordinates=True,
                   frame_time_method='native_buffer_timestamp_with_independent_clock_candidate')
        # Leave valid/reason/context/registration exactly as observed. Existing
        # page identity, stable pair and raw gesture barriers remain mandatory.
    return output, calibration
