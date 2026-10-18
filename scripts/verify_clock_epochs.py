"""Good final ranges survive a clock jump without bridging the uncertain gap."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from clock_epochs import partition_clock,frames_in_epoch,epoch_for
from coordinate_alignment import assess_clock
from coordinate_stream import CoordinateStream
from verify_coordinate_acceptance import fixture
from verify_coordinate_alignment import point


def jumped(wall_ms=-60000,boot_us=0):
    points,state,frames=fixture()
    later_points,later,later_frames=deepcopy((points,state,frames))
    shift=20_000_000
    for p in later_points:
        p['seq']+=5;p['action_time']+=shift
    def move(c):
        c['monotonic_before_us']+=shift;c['monotonic_after_us']+=shift
        c['boottime_us']+=shift+boot_us;c['unix_ms']+=shift/1000+wall_ms
    for a in later['anchors']:move(a)
    for o in later['observations']:
        o['seq']+=5;o['action_time_us']+=shift;move(o['callback_clock'])
    for f in later_frames:
        m=f['observation']['native_frame']
        m['timestamp_raw']+=shift*1000;m['callback_monotonic_us']+=shift
        m['callback_boottime_us']+=shift+boot_us;m['callback_wall_us']+=shift+wall_ms*1000
    state['anchors']+=later['anchors'];state['observations']+=later['observations']
    return points+[point(5,16000)]+later_points,state,frames+later_frames


class EpochChecks(unittest.TestCase):
    def replay(self,points,state,frames,limit=2):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=root/'raw.jsonl'
            raw.write_text(''.join(json.dumps(p)+'\n' for p in points));original=raw.read_bytes()
            stream=CoordinateStream(root/'derived',raw,'synthetic')
            try:
                for i,f in enumerate(frames):stream.add_geometry(dict(f,index=i))
                stream.ingest();stream.step(state,final=True,limit=limit)
            finally:stream.close()
            stream=CoordinateStream(root/'derived',raw,'synthetic')
            try:
                for _ in range(100):
                    if not stream.step(state,final=True,limit=limit):break
                self.assertTrue(stream.summary()['final_complete'])
                rows={seq:json.loads(p) for seq,p in stream.db.execute('SELECT seq,payload FROM coordinates')}
                self.assertEqual(len(rows),sum(p['tool_type']==1 for p in points))
                self.assertEqual(raw.read_bytes(),original)
                return rows,stream.summary()
            finally:stream.close()

    def test_wall_reversal_preserves_both_sides_but_not_transition_after_restart(self):
        points,state,frames=jumped()
        original=deepcopy(state)
        self.assertFalse(assess_clock(state)['usable_candidate'])
        rows,summary=self.replay(points,state,frames)
        self.assertEqual(rows[3]['status'],'candidate')
        self.assertEqual(rows[8]['status'],'candidate')
        self.assertEqual(rows[3]['clock_epoch'],0);self.assertEqual(rows[8]['clock_epoch'],1)
        self.assertEqual(rows[5]['reason'],'clock_epoch_transition_or_insufficient_evidence')
        self.assertEqual(state,original)
        self.assertEqual(len(summary['clock']['final_epochs']),2)

    def test_suspend_boot_offset_also_splits_even_when_wall_offset_is_stable(self):
        points,state,frames=jumped(wall_ms=0,boot_us=60_000_000)
        rows,_=self.replay(points,state,frames)
        self.assertEqual(rows[3]['status'],'candidate');self.assertEqual(rows[8]['status'],'candidate')

    def test_delayed_callback_invalidates_its_epoch_without_rescuing_it(self):
        points,state,frames=jumped()
        state['observations'][3]['callback_clock']['monotonic_after_us']+=150_000
        # A malformed wide sample must reject, not be silently dropped.
        with self.assertRaises(ValueError):partition_clock(state)
        points,state,frames=jumped()
        c=state['observations'][3]['callback_clock']
        for k in ('monotonic_before_us','monotonic_after_us','boottime_us'):c[k]+=150_000
        c['unix_ms']+=150
        rows,_=self.replay(points,state,frames)
        self.assertEqual(rows[3]['status'],'candidate')
        self.assertEqual(rows[8]['status'],'unresolved')

    def test_missing_history_raw_mismatch_and_gap_are_never_rescued(self):
        for mode in ('history','mismatch','gap'):
            points,state,frames=jumped()
            if mode=='history':state.update(history_merged=True,history_complete=False)
            elif mode=='mismatch':state['observations'][0]['action_time_us']+=1
            else:points=[p for p in points if p['seq']!=5]
            rows,_=self.replay(points,state,frames)
            self.assertFalse(any(p['status']=='candidate' for p in rows.values()),mode)

    def test_held_finger_across_epoch_gap_stays_barrier(self):
        points,state,frames=jumped()
        points[4].update(tool_type=0,action=1)
        rows,_=self.replay(points,state,frames)
        self.assertEqual(rows[3]['status'],'candidate')
        self.assertEqual(rows[8]['reason'],'input_motion_or_context_barrier')

    def test_frames_across_epoch_boundary_and_legacy_frames_are_barriers(self):
        _,state,frames=jumped()
        epochs=partition_clock(state)
        frame=deepcopy(frames[1]);frame['observation']['native_frame']['copy_finished_monotonic_us']=25_000_000
        legacy={'observation':{'valid':True,'device_capture_wall_interval_ms':[1,2]}}
        selected=frames_in_epoch([frames[1],frame,frames[-1],legacy],epochs[0])
        self.assertEqual([f['observation']['valid'] for f in selected],[True,False,False,False])
        self.assertIsNone(epoch_for(epochs,11_000_000,22_000_000))

    def test_stable_history_has_no_partition_and_time_reset_is_rejected(self):
        _,state,_=fixture();self.assertIsNone(partition_clock(state))
        _,state,_=jumped();state['anchors'][4].update(monotonic_before_us=0,monotonic_after_us=2)
        self.assertIsNone(partition_clock(state))


if __name__=='__main__':unittest.main(verbosity=2)
