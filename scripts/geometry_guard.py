"""Invalidate live geometry immediately on observed input/context barriers.

This guard reports readiness only; point mapping still requires a future frame
and device clock evidence in coordinate_alignment. It never maps forward.
"""
from coordinate_alignment import prepare_pairs, valid_frame
from ink_protocol import PEN_TOOLS, validate_point
import json
import threading
import time
from urllib.request import urlopen


class GeometryGuard:
    def __init__(self, ttl_seconds=2.5):
        self.ttl=ttl_seconds
        self.generation=0
        self.previous=None
        self.current=None
        self.expires=0
        self.last_seq=None
        self.session=None
        self.reason='awaiting_geometry'
        self.contacts=set()

    def invalidate(self,reason):
        self.generation+=1
        self.previous=self.current=None
        self.expires=0
        self.reason=reason

    def point(self,p):
        validate_point(p)
        if self.session!=p.get('session_id'):
            self.invalidate('session_changed')
            self.session=p.get('session_id')
            self.last_seq=None
            self.contacts.clear()
        if self.last_seq and (p['seq']!=self.last_seq[0]+1 or p['action_time']<self.last_seq[1]):
            self.invalidate('raw_gap_or_clock_discontinuity')
        self.last_seq=p['seq'],p['action_time']
        if p['tool_type'] not in PEN_TOOLS or p['action']==0:
            self.invalidate('finger_eraser_or_cancel')
            key=(p['display_id'],p['window_id'],p['pointer_id'])
            if p['action'] in (0,3): self.contacts.discard(key)
            elif p['tool_type'] not in PEN_TOOLS: self.contacts.add(key)
        elif self.current:
            window=self.current['observation']['window']
            if (p['window_id'],p['display_id'])!=(window['window_id'],window['display_id']):
                self.invalidate('input_window_changed')

    def frame(self,row,requested_generation,now):
        if self.contacts:
            self.reason='non_pen_contact_still_active'
            return
        if requested_generation!=self.generation:
            self.reason='frame_observed_before_input_barrier'
            return
        if not valid_frame(row):
            self.invalidate('invalid_frame_or_context')
            return
        if self.previous and prepare_pairs([self.previous,row]):
            self.current=row
            self.expires=now+self.ttl
            self.reason=None
        else:
            self.current=None
            self.reason='awaiting_second_stable_frame'
        self.previous=row

    def status(self,now):
        if self.current and now>=self.expires:
            self.invalidate('geometry_expired')
        return {'generation':self.generation,'geometry_readiness':'stable_pair_candidate' if self.current else 'unavailable',
                'reason':self.reason,'forward_point_mapping_allowed':False,
                'expires_at_pc_monotonic':self.expires if self.current else None,
                'latest_raw_seq':self.last_seq[0] if self.last_seq else None,
                'session_id':self.session}


class RawGuardPoller:
    """Independent raw polling keeps slow screenshot processing from delaying invalidation."""
    def __init__(self,output,fetch=None):
        self.output=output
        self.guard=GeometryGuard()
        self.lock=threading.Lock()
        self.stop_event=threading.Event()
        self.fetch=fetch or self.http
        self.cursor=None
        self.connected=None
        self.caught_up=False
        self.thread=None
        self.poll_count=0
        self.max_poll_ms=0
        self.transitions=0
        self.last_status=None

    @staticmethod
    def http(cursor):
        with urlopen(f'http://127.0.0.1:18766/events?after={cursor}',timeout=.75) as response:
            return json.load(response)

    def consume(self,snapshot):
        with self.lock:
            if self.cursor is None:
                self.cursor=0
            if snapshot['reset']: self.guard.invalidate('receiver_cursor_reset')
            # Replay available contact state instead of jumping to the live tail:
            # a finger may already be held when this observer starts.
            for event in snapshot['events']: self.guard.point(event['point'])
            self.cursor=snapshot['next_cursor']
            self.caught_up=self.cursor>=snapshot['points']
            if not snapshot['connected'] and self.connected is not False:
                self.guard.invalidate('receiver_disconnected')
            self.connected=snapshot['connected']

    def start(self):
        try:
            self.consume(self.fetch(0))
        except OSError:
            self.connected=False
            self.caught_up=False
            self.guard.invalidate('receiver_poll_failed')
        self.thread=threading.Thread(target=self.run,name='raw-geometry-guard',daemon=True)
        self.thread.start()

    def token(self):
        with self.lock: return self.guard.generation

    def frame(self,row,token):
        with self.lock:
            if not self.connected:
                self.guard.invalidate('receiver_disconnected')
            elif not self.caught_up:
                self.guard.invalidate('receiver_backlog_not_drained')
            else: self.guard.frame(row,token,time.monotonic())

    def run(self):
        while not self.stop_event.is_set():
            before=time.monotonic()
            try:
                self.consume(self.fetch(self.cursor or 0))
            except Exception:
                with self.lock:
                    self.connected=False
                    self.caught_up=False
                    self.guard.invalidate('receiver_poll_failed')
            with self.lock:
                status=self.guard.status(time.monotonic())
            self.poll_count+=1
            self.max_poll_ms=max(self.max_poll_ms,(time.monotonic()-before)*1000)
            status.update(poll_count=self.poll_count,max_poll_ms=round(self.max_poll_ms,2),
                          requested_poll_interval_ms=150,pc_observed_at_monotonic=time.monotonic(),
                          receiver_caught_up=self.caught_up)
            key=(status['generation'],status['geometry_readiness'],status['reason'])
            if key!=self.last_status:
                history=self.output.parent/'guard-events.jsonl'
                if history.exists() and history.stat().st_size>1024*1024:
                    history.replace(history.with_name('guard-events.previous.jsonl'))
                with history.open('a',encoding='utf-8') as out: out.write(json.dumps(status)+'\n')
                self.transitions+=1
                self.last_status=key
            temporary=self.output.with_suffix('.tmp')
            temporary.write_text(json.dumps(status,indent=2),encoding='utf-8')
            temporary.replace(self.output)
            self.stop_event.wait(max(0,.15-(time.monotonic()-before)))

    def close(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=2)
        with self.lock:
            self.guard.invalidate('finite_observer_stopped')
            status=self.guard.status(time.monotonic())
            status.update(poll_count=self.poll_count,max_poll_ms=round(self.max_poll_ms,2),
                          transitions=self.transitions,requested_poll_interval_ms=150)
        self.output.write_text(json.dumps(status,indent=2),encoding='utf-8')
