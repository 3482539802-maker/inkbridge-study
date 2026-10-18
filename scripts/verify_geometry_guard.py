"""Race, expiry and interruption checks using known synthetic geometry."""
import unittest
import json
from pathlib import Path
import tempfile
import threading
from geometry_guard import GeometryGuard,RawGuardPoller
from verify_coordinate_alignment import frame,point


class GuardChecks(unittest.TestCase):
    def stable(self):
        g=GeometryGuard()
        g.point(point(1,10000))
        g.frame(frame(11000),g.generation,1)
        g.frame(frame(13000),g.generation,3)
        self.assertEqual(g.status(3)['geometry_readiness'],'stable_pair_candidate')
        return g

    def test_finger_invalidates_before_new_frame(self):
        g=self.stable(); generation=g.generation
        g.point(point(2,14000,tool=0))
        self.assertGreater(g.generation,generation)
        self.assertEqual(g.status(3.01)['geometry_readiness'],'unavailable')

    def test_inflight_frame_cannot_restore_pre_gesture_geometry(self):
        g=self.stable(); generation=g.generation
        g.point(point(2,14000,tool=0))
        g.frame(frame(15000),generation,4)
        self.assertEqual(g.status(4)['geometry_readiness'],'unavailable')
        g.point(dict(point(3,16000,tool=0),action=3))
        g.frame(frame(17000),g.generation,6)
        self.assertEqual(g.status(6)['geometry_readiness'],'unavailable')
        g.frame(frame(19000),g.generation,8)
        self.assertEqual(g.status(8)['geometry_readiness'],'stable_pair_candidate')
        self.assertFalse(g.status(8)['forward_point_mapping_allowed'])

    def test_expiry_requires_two_new_frames(self):
        g=self.stable()
        self.assertEqual(g.status(6)['reason'],'geometry_expired')
        g.frame(frame(15000),g.generation,6)
        self.assertEqual(g.status(6)['geometry_readiness'],'unavailable')

    def test_gap_wrong_window_and_disconnect_invalidate(self):
        for p in (point(4,14000),point(2,14000,window=3),dict(point(2,14000),session_id='other')):
            g=self.stable(); g.point(p)
            self.assertEqual(g.status(4)['geometry_readiness'],'unavailable')
        g=self.stable(); g.invalidate('receiver_disconnected')
        self.assertEqual(g.status(4)['geometry_readiness'],'unavailable')

    def test_change_or_failed_frame_cannot_keep_old_geometry(self):
        for patch in ('failed','changed'):
            g=self.stable(); row=frame(15000)
            if patch=='failed': row['observation']['valid']=False
            else: row['registration']['pdf_page_1based']=2
            g.frame(row,g.generation,4)
            self.assertEqual(g.status(4)['geometry_readiness'],'unavailable')

    def test_held_finger_prevents_even_two_later_frames(self):
        g=self.stable(); g.point(dict(point(2,14000,tool=0),action=1))
        g.frame(frame(15000),g.generation,4)
        g.frame(frame(17000),g.generation,6)
        self.assertEqual(g.status(6)['geometry_readiness'],'unavailable')
        g.point(dict(point(3,18000,tool=0),action=3))
        g.frame(frame(19000),g.generation,8)
        self.assertEqual(g.status(8)['geometry_readiness'],'unavailable')

    def test_raw_cursor_consumption_and_reset(self):
        poller=RawGuardPoller(None)
        base={'points':100,'next_cursor':100,'reset':False,'connected':True,'events':[]}
        poller.consume(base)
        self.assertEqual(poller.cursor,100)
        poller.guard=self.stable()
        poller.consume(dict(base,points=101,next_cursor=101,events=[{'point':point(2,14000,tool=0)}]))
        self.assertEqual(poller.guard.status(4)['geometry_readiness'],'unavailable')
        poller.consume(dict(base,reset=True))
        self.assertEqual(poller.guard.status(4)['reason'],'receiver_cursor_reset')

    def test_start_replays_held_contact_and_blocks_backlog(self):
        poller=RawGuardPoller(None)
        base={'points':2000,'next_cursor':1000,'reset':False,'connected':True,
              'events':[{'point':dict(point(1,10000,tool=0),action=1)}]}
        poller.consume(base)
        self.assertFalse(poller.caught_up)
        poller.frame(frame(11000),poller.token())
        self.assertEqual(poller.guard.reason,'receiver_backlog_not_drained')
        poller.consume(dict(base,next_cursor=2000,events=[]))
        self.assertTrue(poller.caught_up)
        poller.frame(frame(13000),poller.token())
        self.assertEqual(poller.guard.reason,'non_pen_contact_still_active')

    def test_poll_thread_logs_bounded_transitions_and_closes(self):
        calls=0
        emitted=threading.Event()
        def fetch(cursor):
            nonlocal calls
            calls+=1
            if calls>=3: emitted.set()
            return {'points':calls,'next_cursor':calls,'reset':False,'connected':True,
                    'events':[{'point':point(calls,10000+calls*10,tool=0)}]}
        with tempfile.TemporaryDirectory(prefix='inkbridge-guard-check-') as folder:
            output=Path(folder)/'status.json'
            poller=RawGuardPoller(output,fetch)
            poller.start()
            self.assertTrue(emitted.wait(2))
            poller.close()
            self.assertFalse(poller.thread.is_alive())
            status=json.loads(output.read_bytes())
            self.assertEqual(status['reason'],'finite_observer_stopped')
            self.assertGreaterEqual(status['poll_count'],2)
            self.assertFalse(status['forward_point_mapping_allowed'])
            self.assertTrue((output.parent/'guard-events.jsonl').exists())
            self.assertFalse(output.with_suffix('.tmp').exists())


if __name__=='__main__':unittest.main(verbosity=2)
