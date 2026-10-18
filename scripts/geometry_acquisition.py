"""One in-flight geometry frame, overlapped with CPU registration; no UI actions."""
from dataclasses import dataclass
import queue
import subprocess
import threading
import time
import itertools


@dataclass
class Acquisition:
    image: object
    observation: dict
    started: float
    acquired: float
    guard_token: int


def acquire(frames, title, token):
    started = time.monotonic()
    try:
        image, observation = frames.capture(title)
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        image = None
        observation = {'valid': False, 'reason': 'context_or_acquisition_failed',
                       'error_type': type(error).__name__}
    return Acquisition(image, observation, started, time.monotonic(), token)


class GeometryAcquisition:
    """Backpressure keeps all invalid observations; never drops a motion barrier.

    At most one image is being acquired/queued in addition to the image the
    consumer is registering. Stop waits for source cleanup before releasing the
    screenshot lock. No previous image is reused as a new observation.
    """
    def __init__(self, frames, title, token, interval, count, seconds):
        self.frames, self.title, self.token = frames, title, token
        self.interval, self.count = interval, count
        self.deadline = time.monotonic() + seconds if seconds is not None else float('inf')
        self.queue = queue.Queue(maxsize=1)
        self.slot = threading.Semaphore(1)
        self.stop_event = threading.Event()
        self.frames.cancel_event = self.stop_event
        self.done = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._run, name='geometry-acquisition', daemon=True)
        self.thread.start()

    def _run(self):
        try:
            for _ in (range(self.count) if self.count is not None else itertools.count()):
                while not self.stop_event.is_set() and time.monotonic() < self.deadline:
                    if self.slot.acquire(timeout=.05): break
                else: break
                if self.stop_event.is_set() or time.monotonic() >= self.deadline: break
                result = acquire(self.frames, self.title, self.token())
                if self.stop_event.is_set():
                    result.image = None
                    break
                self.queue.put_nowait(result)
                self.stop_event.wait(max(0, self.interval - (time.monotonic()-result.started)))
        except BaseException as error:
            self.error = error
        finally:
            self.done.set()

    def next(self,timeout=None):
        deadline=time.monotonic()+timeout if timeout is not None else float('inf')
        while True:
            try:
                result = self.queue.get(timeout=.05)
                self.slot.release()
                return result
            except queue.Empty:
                if self.done.is_set():
                    if self.error is not None: raise self.error
                    return None
                if time.monotonic()>=deadline:raise TimeoutError('Acquisition still running')

    def close(self):
        self.stop_event.set()
        # Source operations have bounded HDC timeouts. The lock must stay owned
        # until their finally blocks clear temporary requests and images.
        self.thread.join(timeout=60)
        if self.thread.is_alive(): raise RuntimeError('Geometry source did not finish bounded cleanup')
        while True:
            try: self.queue.get_nowait().image = None
            except queue.Empty: break
