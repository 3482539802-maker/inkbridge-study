"""Bounded native sampling while slow, read-only context checks run separately.

Every frame needs a completed preflight BEFORE its request and a postflight
STARTED AFTER its receipt. A context read in flight is never its postflight.
This changes acquisition cadence only; coordinate/raw/clock gates are unchanged.
"""
from collections import deque
from dataclasses import dataclass
import queue
import subprocess
import threading
import time

from geometry_acquisition import Acquisition
from notes_edit_context import geometry_context


@dataclass
class Boundary:
    started: float
    finished: float
    window: dict
    context: dict | None
    failure: int
    reason: str | None
    identity_failure: int = 0
    diagnostics: dict | None = None


@dataclass
class Pending:
    sample: Acquisition
    received: float
    before: Boundary


ERRORS = (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired)


class BracketedGeometryAcquisition:
    """Three in-flight/queued frames plus the consumer, with backpressure.

    The payload owner alone renews leases and requests/clears native frames.
    The context owner alone reads window/tree state. No pixels are persisted.
    Failed context reads poison every overlapping frame, including one whose
    payload was in flight when the failure occurred. Invalid rows are retained.
    """
    capacity = 3
    maximum_preflight_age = .75
    maximum_context_span = 2.5

    def __init__(self, source, title, token, interval=.2, motion_probe=None, motion_validation=False,
                 independent_page_exposure=False):
        self.source, self.token = source, token
        self.titles = (title,) if isinstance(title, str) else tuple(title)
        if not 1 <= len(self.titles) <= 4 or not all(isinstance(t, str) and t for t in self.titles):
            raise ValueError('One to four bound document titles required')
        self.interval = interval
        self.motion_probe = motion_probe
        self.motion_validation = motion_validation
        self.independent_page_exposure = independent_page_exposure
        self.stop_event = threading.Event()
        self.source.cancel_event = self.stop_event
        self.condition = threading.Condition()
        self.queue = queue.Queue(maxsize=self.capacity)
        self.slots = threading.Semaphore(self.capacity)
        self.pending = deque()
        self.boundary = None
        self.failure = 0
        self.identity_failure = 0
        self.refresh = True
        self.inflight = False
        self.error = None
        self.done = threading.Event()
        self.threads = [threading.Thread(target=self._contexts, name='geometry-context', daemon=True),
                        threading.Thread(target=self._payloads, name='geometry-payload', daemon=True)]
        for thread in self.threads:
            thread.start()

    def _fail(self, error):
        self.error = error
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()

    def _context(self):
        started = time.monotonic()
        window, context, reason = {}, None, None
        try:
            window, context = self.source.window_context()
            if not window.get('focused'):
                reason = 'notes_not_focused'
            elif (not context or not context.get('active_tab_marker_unique') or
                  context.get('document_title_candidate') not in self.titles):
                reason = 'different_or_ambiguous_document'
        except ERRORS as error:
            reason = 'context_or_acquisition_failed'
        previous = self.boundary
        changed = previous is not None and (previous.window != window or
            geometry_context(previous.context) != geometry_context(context))
        # A known A -> B -> A transition is a barrier even if the final
        # endpoint agrees with the preflight of a frame still in flight.
        if reason or changed:
            self.failure += 1
        def identity(w,c):
            return (w,(c or {}).get('document_title_candidate'),(c or {}).get('active_tab_marker_unique'),
                sorted(str(p.get('page_id')) for p in (c or {}).get('page_layers',[])))
        if reason or (previous is not None and identity(previous.window,previous.context)!=identity(window,context)):
            self.identity_failure += 1
        profile=getattr(self.source,'context_diagnostics',{})
        return Boundary(started, time.monotonic(), window, context, self.failure, reason,self.identity_failure,
                        dict(profile) if isinstance(profile,dict) else {})

    def _finish(self, pending, after):
        sample, before = pending.sample, pending.before
        o = sample.observation
        span = after.finished-before.started
        request_started = o.get('frame_request_started_at_pc_monotonic', sample.started)
        preflight_age = request_started-before.finished
        valid = (o.get('payload_received', False) and before.reason is None and after.reason is None and
                 before.failure == after.failure and before.finished <= sample.started and
                 0 <= preflight_age <= self.maximum_preflight_age and
                 pending.received <= after.started and span <= self.maximum_context_span and
                 before.window == after.window and
                 geometry_context(before.context) == geometry_context(after.context))
        reason = o.get('reason') or after.reason or before.reason
        if not valid and not reason:
            reason = ('context_boundary_failed_during_frame' if before.failure != after.failure else
                      'stale_preflight_context' if preflight_age > self.maximum_preflight_age else
                      'context_bracket_too_wide' if span > self.maximum_context_span else
                      'window_title_or_page_geometry_changed')
        sample.observation = {**o, 'valid': bool(valid), 'reason': None if valid else reason,
            'window_before': before.window, 'context_before': before.context,
            'window': after.window, 'context': after.context,
            'acquisition_mode': 'independent_payload_and_bracketed_context',
            'maximum_inflight_or_queued_frames': self.capacity,
            'context_preflight_age_ms': preflight_age*1000,
            'context_bracket_ms': span*1000,
            'context_postflight_started_after_payload': pending.received <= after.started,
            'context_before_profile':before.diagnostics or {},
            'context_after_profile':after.diagnostics or {},
            'motion_context_identity_generation_range': [before.identity_failure,after.identity_failure],
            'source_observation_ms': (after.finished-sample.started)*1000,
            'source_stage_ms': {**o.get('source_stage_ms', {}),
                'postflight': (after.finished-after.started)*1000,
                'context_wait': (after.started-pending.received)*1000}}
        sample.acquired = after.finished
        if getattr(self, 'independent_page_exposure', False):
            sample.observation['independent_page_exposure_requested'] = True
        from page_exposure import eligible
        exposure = eligible(sample.observation) and sample.image is not None
        # Reuse this already-acquired payload only for a separate numeric
        # experiment. It remains invalid for coordinate/guard consumers.
        diagnostic = (self.motion_validation and sample.image is not None and
            reason == 'context_boundary_failed_during_frame' and
            before.reason is None and after.reason is None and
            before.identity_failure == after.identity_failure and
            0 <= preflight_age <= self.maximum_preflight_age and
            span <= self.maximum_context_span and pending.received <= after.started and
            bool(o.get('page_motion', {}).get('candidate')))
        if diagnostic:
            sample.observation['motion_validation_payload_only'] = True
        if not valid and not diagnostic and not exposure:
            sample.image = None
        self.queue.put_nowait(sample)

    def _contexts(self):
        try:
            while not self.stop_event.is_set():
                with self.condition:
                    while not self.stop_event.is_set() and not (self.refresh or self.pending):
                        self.condition.wait(.05)
                    if self.stop_event.is_set():
                        break
                    self.refresh = False
                after = self._context()
                with self.condition:
                    self.boundary = after
                    while self.pending and self.pending[0].received <= after.started:
                        self._finish(self.pending.popleft(), after)
                    # Retain an explicit gap while unfocused/disconnected even
                    # when no image request was eligible to start.
                    if after.reason and not self.pending and not self.inflight and self.slots.acquire(blocking=False):
                        self.queue.put_nowait(Acquisition(None,
                            {'valid': False, 'reason': after.reason, 'window': after.window,
                             'context': after.context, 'acquisition_mode': 'independent_payload_and_bracketed_context'},
                            after.started, after.finished, self.token()))
                    self.condition.notify_all()
                if after.reason:
                    self.stop_event.wait(.5)
                    with self.condition:
                        self.refresh = True
        except BaseException as error:
            self._fail(error)
        finally:
            self.done.set()

    def _payloads(self):
        try:
            while not self.stop_event.is_set():
                if not self.slots.acquire(timeout=.05):
                    continue
                prepare = getattr(self.source, 'prepare_payload', None)
                if prepare is not None:
                    # Slow lease renewal must not age the already selected
                    # preflight. Select/recheck a boundary only AFTER renewal.
                    prepared_at = time.monotonic()
                    try:
                        ready = prepare()
                        failure = {'valid': False, 'reason': 'user_capture_not_active'}
                    except ERRORS as error:
                        ready = False
                        failure = {'valid': False, 'reason': 'context_or_acquisition_failed',
                                   'error_type': type(error).__name__}
                    if not ready:
                        if self.motion_probe:self.motion_probe.reset()
                        self.queue.put_nowait(Acquisition(None, failure, prepared_at,
                            time.monotonic(), self.token()))
                        self.stop_event.wait(max(.05, self.interval))
                        continue
                with self.condition:
                    while not self.stop_event.is_set():
                        before = self.boundary
                        if (before and before.reason is None and
                            0 <= time.monotonic()-before.finished <= self.maximum_preflight_age):
                            break
                        self.refresh = True
                        self.condition.notify_all()
                        self.condition.wait(.05)
                    if self.stop_event.is_set():
                        self.slots.release()
                        break
                    self.inflight = True
                started, guard_token = time.monotonic(), self.token()
                try:
                    capture = self.source.capture_prepared_payload if prepare is not None else self.source.capture_payload
                    image, observation, received = capture()
                    observation = dict(observation, payload_received=image is not None)
                    if self.motion_probe:
                        try:
                            observation['page_motion'] = self.motion_probe.observe(image,
                                observation.get('native_frame',{}),before.window,before.context)
                        except Exception as error:
                            # An optional diagnostic must not lose a valid
                            # payload or turn an invalid frame into geometry.
                            self.motion_probe.reset()
                            observation['page_motion'] = {'candidate':False,'reason':'motion_probe_failed',
                                'coordinate_mapping_allowed':False,'error_type':type(error).__name__}
                except ERRORS as error:
                    if self.motion_probe:self.motion_probe.reset()
                    image, received = None, time.monotonic()
                    observation = {'valid': False, 'reason': 'context_or_acquisition_failed',
                                   'error_type': type(error).__name__}
                with self.condition:
                    self.inflight = False
                    if self.stop_event.is_set():
                        image = None
                        if self.motion_probe:self.motion_probe.reset()
                        self.slots.release()
                        break
                    self.pending.append(Pending(Acquisition(image, observation, started, received, guard_token),
                                                received, before))
                    image = None
                    self.condition.notify_all()
                self.stop_event.wait(max(0, self.interval-(time.monotonic()-started)))
        except BaseException as error:
            self._fail(error)

    def next(self, timeout=None):
        deadline = time.monotonic()+timeout if timeout is not None else float('inf')
        while True:
            try:
                sample = self.queue.get(timeout=.05)
                self.slots.release()
                return sample
            except queue.Empty:
                if self.done.is_set():
                    if self.error is not None:
                        raise self.error
                    return None
                if time.monotonic() >= deadline:
                    raise TimeoutError('Acquisition still running')

    def close(self):
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        deadline = time.monotonic()+55
        for thread in self.threads:
            thread.join(timeout=max(0, deadline-time.monotonic()))
        if any(thread.is_alive() for thread in self.threads):
            raise RuntimeError('Geometry source still stopping; keep screenshot lock owned')
        if self.motion_probe:self.motion_probe.close()
        with self.condition:
            for pending in self.pending:
                pending.sample.image = None
            self.pending.clear()
        while True:
            try:
                self.queue.get_nowait().image = None
            except queue.Empty:
                break
