"""Finite, passive AVScreenCapture frame/time diagnostic; never drives tablet UI.

Requires the user to start/authorize the ordinary capture session. Retains numeric
observations only. Raw pen journals, receiver protocol and coordinate acceptance
are untouched until the new timestamp path has actual device evidence.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import io
import json
import math
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
from receiver_status import receiver

import cv2
import numpy as np
from PIL import Image

from notes_diagnostics import notes_window
from visual_calibration import TabletFrames, write_bounded_json
from visual_registration import PDFRegistration
from coordinate_page_binding import bind_frame, load_binding
from notes_edit_context import geometry_context
from geometry_socket import GeometrySocket

DIRECTORY = '/data/storage/el2/base/haps/entry/files/'
ROOT = Path(__file__).resolve().parents[1]


class DeviceClockUnavailable(RuntimeError):
    """A malformed transport clock reply is retryable, never calibration."""


def decode_packet(payload, request_id, expected_size):
    if len(payload) > 8 * 1024 * 1024 + 8192:
        raise ValueError('Oversized frame packet')
    boundary = payload.find(b'\n')
    if not 0 < boundary < 8192:
        raise ValueError('Missing bounded metadata header')
    metadata = json.loads(payload[:boundary])
    if metadata.get('schema') != 1 or metadata.get('request_id') != request_id:
        raise ValueError('Stale or unknown frame packet')
    if metadata.get('error'):
        raise ValueError('Native frame error: ' + str(metadata['error'])[:80])
    for name in ('timestamp_raw', 'callback_monotonic_us', 'callback_boottime_us',
                 'callback_wall_us', 'copy_finished_monotonic_us', 'encoded_monotonic_us',
                 'width', 'height', 'stride', 'native_format', 'jpeg_bytes'):
        value = metadata.get(name)
        if type(value) is not int or value < 0:
            raise ValueError('Invalid numeric metadata: ' + name)
    if [metadata['width'], metadata['height']] != list(expected_size):
        raise ValueError('Canvas resolution differs from requested screen size')
    telemetry=('video_callback_count','max_video_fps_requested','max_video_fps_result')
    if any(k in metadata for k in telemetry):
        if (any(type(metadata.get(k)) is not int for k in telemetry) or
            not 1<=metadata['video_callback_count']<2**64 or metadata['max_video_fps_requested']!=5 or
            not -1<=metadata['max_video_fps_result']<2**31):
            raise ValueError('Invalid video callback telemetry')
    reduction = metadata.get('pixel_reduction',1)
    if type(reduction) is not int or reduction not in (1,2) or any(s % reduction for s in expected_size):
        raise ValueError('Invalid deterministic pixel reduction')
    encoded_size=tuple(s//reduction for s in expected_size)
    if (metadata.get('encoded_width',encoded_size[0]),metadata.get('encoded_height',encoded_size[1])) != encoded_size:
        raise ValueError('Encoded dimensions disagree with pixel reduction')
    if not metadata['callback_monotonic_us'] <= metadata['copy_finished_monotonic_us'] <= metadata['encoded_monotonic_us']:
        raise ValueError('Nonmonotonic frame processing clocks')
    jpeg = payload[boundary + 1:]
    if len(jpeg) != metadata['jpeg_bytes']:
        raise ValueError('Truncated frame payload')
    with Image.open(io.BytesIO(jpeg)) as header:
        if header.format != 'JPEG' or header.size != encoded_size or header.width * header.height > 6000000:
            raise ValueError('Unexpected JPEG dimensions or encoding')
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError('Invalid JPEG frame')
    return image, metadata


def assess_timestamps(metadata):
    """Expose plausible unit/domain combinations; do not assert capture truth."""
    if not metadata:
        return {'status': 'no_frames', 'verified': False}
    candidates = []
    for unit, factor in [('ns', .001), ('us', 1.), ('ms', 1000.)]:
        for domain, field in [('monotonic', 'callback_monotonic_us'),
                              ('boottime', 'callback_boottime_us'), ('realtime', 'callback_wall_us')]:
            lags = [(m[field] - m['timestamp_raw'] * factor) / 1000 for m in metadata]
            if all(math.isfinite(lag) and -2 <= lag <= 500 for lag in lags):
                candidates.append({'unit': unit, 'domain': domain, 'callback_lag_ms_range': [min(lags), max(lags)]})
    return {'status': 'diagnostic_candidates_only', 'verified': False, 'candidates': candidates,
            'frames': len(metadata),
            'span_ms': (metadata[-1]['callback_monotonic_us'] - metadata[0]['callback_monotonic_us']) / 1000,
            'used_for_pen_page_coordinates': False,
            'note': 'Callback proximity alone does not prove frame exposure, clock domain, or pen accuracy.'}


class NativeProbeFrames(TabletFrames):
    def __init__(self, scratch, target, size, reduction=2):
        if reduction not in (1,2) or any(s % reduction for s in size):
            raise ValueError('Invalid deterministic pixel reduction')
        self.reduction = reduction
        self.size = tuple(size)
        super().__init__(scratch, target)

    def send_text(self, name, text):
        if name not in ('geometry-enable.txt', 'geometry-request.txt', 'geometry-lease.txt'):
            raise ValueError('Unexpected diagnostic filename')
        with tempfile.TemporaryDirectory(prefix='native-frame-request-', dir=self.scratch) as temporary:
            local = Path(temporary) / name
            local.write_text(text, encoding='ascii')
            result = subprocess.run(self.prefix + ['file', 'send', '-b', 'com.example.inkbridge',
                                    str(local), DIRECTORY + name], capture_output=True, timeout=10)
            if result.returncode or b'[Fail]' in result.stdout:
                raise RuntimeError('Cannot send frame request')

    def arm(self, continuous=False):
        if any(s.get('state') == 'recording' for s in receiver()):
            raise ValueError('A capture is already running; do not change its screen canvas')
        self.close_socket()
        wall = self.device_wall_ms()
        self.lease_token=uuid.uuid4().hex if continuous else None
        self._lease_sent=time.monotonic()
        token=' '+self.lease_token if self.lease_token else ''
        self.send_text('geometry-enable.txt', f'{self.size[0]} {self.size[1]} {int(wall) + (60000 if continuous else 300000)} {self.reduction}{token}\n')
        # Only callbacks from the authorized screen session can supply frames.
        # Old installed builds simply use the existing file backend.
        self.attach_lease(self.lease_token)
        self._socket_retry_after = 0.

    def attach_lease(self, token):
        # Restore only a known numeric job's geometry lease after PC restart;
        # no canvas/recording configuration or OS authorization is changed.
        self.close_socket()
        self._socket_transport = GeometrySocket(token) if token else None
        self.lease_token = token
        self._socket_retry_after = 0.

    def close_socket(self):
        transport = getattr(self, '_socket_transport', None)
        if transport is not None:
            transport.close()

    def close(self):
        self.close_socket()
        super().close()

    def socket_packet(self, nonce, cancelled):
        self._socket_fallback_reason = None
        self._socket_attempt_ms = 0.
        transport = getattr(self, '_socket_transport', None)
        if transport is None or time.monotonic() < getattr(self, '_socket_retry_after', 0):
            return None
        started = time.monotonic()
        try:
            if transport.connection is None:
                listing = subprocess.run(self.prefix + ['fport', 'ls'], capture_output=True, timeout=5)
                target = self.prefix[self.prefix.index('-t')+1]
                expected = [target, 'tcp:18769', 'tcp:18769', '[Forward]']
                present = any(line.split() == expected for line in listing.stdout.decode('utf-8',errors='replace').splitlines())
                route = listing if present else subprocess.run(self.prefix + ['fport', 'tcp:18769', 'tcp:18769'],
                                                               capture_output=True, timeout=5)
                # An existing route is normal after a reconnect; do not remove
                # a route or any listener to force ownership of the local port.
                if route.returncode or b'[Fail]' in route.stdout:
                    raise ConnectionError('Geometry forwarding unavailable')
            return transport.request(nonce, cancelled)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            transport.close()
            self._socket_retry_after = time.monotonic() + 30
            self._socket_fallback_reason = type(error).__name__
            return None
        finally:
            self._socket_attempt_ms = (time.monotonic()-started)*1000

    def renew(self,arming=False):
        if not getattr(self,'lease_token',None):raise ValueError('No session geometry lease')
        wall=self.device_wall_ms()
        self.send_text('geometry-lease.txt',f'{self.lease_token} {int(wall)+60000}\n')
        if arming:
            self.send_text('geometry-enable.txt',f'{self.size[0]} {self.size[1]} {int(wall)+60000} {self.reduction} {self.lease_token}\n')
        self._lease_sent=time.monotonic()

    def device_wall_ms(self):
        wall=self.command('date +%s%3N').strip()
        if len(wall)!=13 or not wall.isascii() or not wall.isdigit():
            raise DeviceClockUnavailable('Device clock reply unavailable; retry transport')
        return int(wall)

    def revoke(self):
        self.close_socket()
        if getattr(self,'lease_token',None):
            self.send_text('geometry-lease.txt',f'{self.lease_token} 0\n')
        self.send_text('geometry-request.txt','clear\n')
        self.send_text('geometry-enable.txt','0 0 0\n')

    def request_payload(self):
        """One nonce-bound frame, without claiming focus/document validation.

        Only an acquisition owner may call this. The caller must bracket it
        with context reads before publishing any usable geometry observation.
        """
        cancelled = getattr(self, 'cancel_event', None)
        if cancelled is not None and cancelled.is_set():
            raise RuntimeError('Finite frame observation ended')
        nonce = uuid.uuid4().hex
        preflight = time.monotonic()
        packet = self.socket_packet(nonce, cancelled)
        socket_used = packet is not None
        try:
            # A timed-out send may already have reached the device. Include it
            # in the cleanup scope so an ambiguous send still requests clear.
            if socket_used:
                payload, requested = packet
                decode_started = time.monotonic()
                image, metadata = decode_packet(payload, nonce, self.size)
                decode_ms = (time.monotonic()-decode_started)*1000
                transfer_ms = (decode_started-requested)*1000
                transfer_attempts = 1
                acquired = time.monotonic()
            else:
                self.send_text('geometry-request.txt', nonce + '\n')
                requested = time.monotonic()
            with tempfile.TemporaryDirectory(prefix='native-frame-', dir=self.scratch) as temporary:
                local = Path(temporary) / 'frame.bin'
                deadline = time.monotonic() + 5
                if not socket_used:
                    image = metadata = None
                    transfer_attempts = 0
                    transfer_ms = decode_ms = 0.
                while not socket_used and time.monotonic() < deadline and not (cancelled is not None and cancelled.is_set()):
                    transfer_started = time.monotonic()
                    transfer_attempts += 1
                    result = subprocess.run(self.prefix + ['file', 'recv', '-b', 'com.example.inkbridge',
                                            DIRECTORY + 'geometry-frame.bin', str(local)], capture_output=True, timeout=6)
                    transfer_ms += (time.monotonic()-transfer_started)*1000
                    if result.returncode == 0 and b'[Fail]' not in result.stdout and local.exists():
                        if local.stat().st_size > 8 * 1024 * 1024 + 8192:
                            raise ValueError('Oversized native frame')
                        payload = local.read_bytes()
                        boundary = payload.find(b'\n')
                        if 0 < boundary < 8192 and json.loads(payload[:boundary]).get('request_id') == nonce:
                            decode_started = time.monotonic()
                            image, metadata = decode_packet(payload, nonce, self.size)
                            decode_ms = (time.monotonic()-decode_started)*1000
                            break
                    time.sleep(.1)
                if metadata is None:
                    raise RuntimeError('No requested native frame; opt-in may have expired or session not active')
                acquired = time.monotonic()
        except BaseException:
            self.close_socket()
            # An ambiguous send/transfer must still clear the response.
            if not socket_used:self.send_text('geometry-request.txt', 'clear\n')
            raise
        return image, {'snapshot_backend': 'avscreen_probe', 'native_frame': metadata,
            'frame_request_started_at_pc_monotonic': preflight,
            'frame_transport': 'loopback_socket' if socket_used else 'hdc_file',
            'device_capture_wall_interval_ms': None, 'atomic_observation': False,
            'frame_socket_attempt_ms': self._socket_attempt_ms,
            'frame_socket_fallback_reason': self._socket_fallback_reason,
            'frame_transfer_attempts': transfer_attempts,
            'frame_transfer_total_ms': transfer_ms, 'frame_decode_ms': decode_ms,
            'source_stage_ms': {'request_send': (requested-preflight)*1000,
                'wait_transfer_decode': (acquired-requested)*1000},
            'timestamp_used_for_coordinates': False}, acquired

    def capture_payload(self):
        """Clean the file fallback even when no context read can complete."""
        image, observation, acquired = self.request_payload()
        try:
            if observation['frame_transport'] != 'loopback_socket':
                self.send_text('geometry-request.txt', 'clear\n')
            return image, observation, acquired
        except BaseException:
            image = None
            raise

    def capture(self, title):
        started = time.monotonic()
        boundary = getattr(self, '_postflight_boundary', None)
        self._postflight_boundary = None
        # Consecutive pipelined frames share only their fresh common boundary.
        age = started-boundary[0] if boundary else None
        reused = age is not None and 0 <= age <= .1
        before, initial = boundary[1:] if reused else self.window_context()
        if not before['focused']:
            return None, {'valid': False, 'reason': 'notes_not_focused'}
        expected_titles = (title,) if isinstance(title,str) else tuple(title)
        if not 1 <= len(expected_titles) <= 4 or not all(isinstance(t,str) and t for t in expected_titles):
            raise ValueError('Expected one to four explicitly bound document titles')
        if not initial['active_tab_marker_unique'] or initial['document_title_candidate'] not in expected_titles:
            return None, {'valid': False, 'reason': 'different_or_ambiguous_document',
                          'window':before,'context':initial}
        cancelled = getattr(self, 'cancel_event', None)
        preflight = time.monotonic()
        image, payload_observation, acquired = self.request_payload()
        socket_used = payload_observation['frame_transport'] == 'loopback_socket'
        # Cleanup and the fresh focus/page check are independent HDC operations.
        # Join cleanup even if context reading fails or observation is cancelled;
        # never publish a frame or start the next request before it completes.
        def clear_response():
            if not socket_used:self.send_text('geometry-request.txt', 'clear\n')
            return time.monotonic()
        postflight_started = time.monotonic()
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='frame-cleanup') as cleanup:
            cleared_future = cleanup.submit(clear_response)
            try:
                if cancelled is not None and cancelled.is_set():
                    raise RuntimeError('Finite frame observation ended')
                after, after_context = self.window_context()
                checked = time.monotonic()
            finally:
                cleared = cleared_future.result()
        if cancelled is not None and cancelled.is_set():
            raise RuntimeError('Finite frame observation ended')
        # Timestamp the actual context read, not the later cleanup completion.
        self._postflight_boundary = (checked, after, after_context)
        valid = before == after and after['focused'] and geometry_context(initial) == geometry_context(after_context)
        return image, {**payload_observation,
                       'valid': bool(valid), 'reason': None if valid else 'window_title_or_page_geometry_changed',
                       'window_before': before, 'context_before': initial, 'window': after, 'context': after_context,
                       'shared_preflight_boundary':reused,
                       'shared_preflight_boundary_age_ms':age*1000 if reused else None,
                       'device_capture_wall_interval_ms': None, 'atomic_observation': False,
                       'source_observation_ms': (time.monotonic() - started) * 1000,
                       'cleanup_postflight_overlapped': not socket_used,
                       'source_stage_ms': {**payload_observation['source_stage_ms'],
                           'preflight':(preflight-started)*1000,
                           'response_cleanup':(cleared-acquired)*1000,
                           'postflight':(checked-postflight_started)*1000},
                       'timestamp_used_for_coordinates': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--pdf', type=Path, required=True)
    parser.add_argument('--hinote', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--wait-seconds', type=int, default=120)
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--width', type=int, default=2800)
    parser.add_argument('--height', type=int, default=1840)
    parser.add_argument('--native-reduction',type=int,choices=(1,2),default=2)
    args = parser.parse_args()
    if not 1 <= args.wait_seconds <= 180 or not 10 <= args.seconds <= 90:
        parser.error('Finite wait <=180s and observation 10..90s required')
    if not (640 <= args.width <= 4096 and 480 <= args.height <= 4096 and args.width * args.height <= 6000000):
        parser.error('Invalid bounded screen size')
    binding = load_binding(args.hinote, args.pdf)
    if binding['title'] != args.title:
        parser.error('Title differs from verified note')
    args.output.mkdir(parents=True, exist_ok=False)
    registration = PDFRegistration(args.pdf, [0, 1], local_refinement=True)
    frames = None
    rows = []
    state = {'status': 'preparing', 'retained_images': 0, 'tablet_ui_actions': []}
    write_bounded_json(args.output / 'status.json', state)
    try:
        frames = NativeProbeFrames(ROOT / 'output/visual-scratch', args.target, (args.width, args.height),args.native_reduction)
        baseline = {s['session_id'] for s in receiver()}
        frames.arm()
        state.update(status='ready', maximum_wait_seconds=args.wait_seconds, maximum_observation_seconds=args.seconds)
        write_bounded_json(args.output / 'status.json', state)
        print('Ready; waiting for a user-authorized session; no tablet controls', flush=True)
        deadline = time.monotonic() + args.wait_seconds
        session = None
        while time.monotonic() < deadline:
            fresh = [s for s in receiver() if s['session_id'] not in baseline and s.get('state') == 'recording']
            if fresh:
                session = max(fresh, key=lambda s: s['started_at'])['session_id']
                break
            time.sleep(.25)
        if session is None:
            state.update(status='waiting_expired')
            return
        state.update(status='observing', session_id=session)
        write_bounded_json(args.output / 'status.json', state)
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline and len(rows) < 45:
            current = next((s for s in receiver() if s['session_id'] == session), None)
            if current and current.get('state') in ('completed', 'interrupted'):
                break
            started = time.monotonic()
            image = None
            try:
                image, observation = frames.capture(args.title)
                result = registration.match(image) if image is not None and observation['valid'] else {'valid': False}
                if result.get('valid'):
                    from geometry_pixel_mapping import lift_registration
                    result=lift_registration(result,observation['native_frame'],image.shape)
                frame = bind_frame({'observation': observation, 'registration': result}, binding) if result.get('valid') else {'observation': observation, 'registration': result}
                row = {'index': len(rows), **frame, 'retained_images': 0}
            except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
                row = {'index': len(rows), 'error': str(error)[:200], 'retained_images': 0}
            finally:
                del image
            row['processing_ms'] = (time.monotonic() - started) * 1000
            rows.append(row)
            with (args.output / 'geometry.jsonl').open('a', encoding='utf-8') as output:
                output.write(json.dumps(row, ensure_ascii=False) + '\n')
            metadata = [r['observation']['native_frame'] for r in rows if r.get('observation', {}).get('native_frame')]
            state.update(samples=len(rows), timestamp_assessment=assess_timestamps(metadata))
            write_bounded_json(args.output / 'status.json', state)
            print(json.dumps({'sample': len(rows), 'valid': row.get('registration', {}).get('valid', False), 'error': row.get('error'), 'ms': row['processing_ms']}), flush=True)
            time.sleep(max(0, 2 - (time.monotonic() - started)))
        state.update(status='completed', used_for_pen_page_coordinates=False)
    finally:
        if frames is not None:
            # Expire an unconsumed opt-in; no automatic application start.
            try:
                frames.send_text('geometry-enable.txt', '0 0 0\n')
            finally:
                frames.close()
        registration.close()
        write_bounded_json(args.output / 'status.json', state)


if __name__ == '__main__':
    main()
