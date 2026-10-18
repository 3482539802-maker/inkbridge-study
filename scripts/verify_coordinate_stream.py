"""Incremental results must retain production timing/contact gates across restart."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from coordinate_alignment import align
from coordinate_stream import CoordinateStream,MAX_WINDOW_BYTES
from verify_coordinate_acceptance import fixture
from verify_coordinate_alignment import point,clock_sample


class StreamChecks(unittest.TestCase):
    def compare(self,points,state,frames,limit=2):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=root/'raw.jsonl'
            raw.write_text(''.join(json.dumps(p)+'\n' for p in points))
            before=raw.read_bytes()
            stream=CoordinateStream(root/'stream',raw,'synthetic')
            for i,f in enumerate(frames):stream.add_geometry(dict(f,index=i))
            stream.ingest()
            stream.step(state,final=True,limit=limit)
            stream.close()
            stream=CoordinateStream(root/'stream',raw,'synthetic')
            while stream.step(state,final=True,limit=limit):pass
            stream.export();summary=stream.publish();stream.close()
            aligned=[dict(f,index=i) for i,f in enumerate(frames)]
            align(raw,state,aligned,root/'offline.jsonl',stable_runs=True)
            a=[json.loads(l) for l in (root/'offline.jsonl').read_text().splitlines()]
            b=[json.loads(l) for l in (root/'stream/points.jsonl').read_text().splitlines()]
            self.assertEqual([(p['seq'],p['status'],p['reason'],p['pdf_xy']) for p in a],
                             [(p['seq'],p['status'],p['reason'],p['pdf_xy']) for p in b])
            self.assertEqual(raw.read_bytes(),before)
            self.assertTrue(summary['final_complete'])
            return summary

    def test_chunk_restart_matches_offline(self):
        self.compare(*fixture())

    def test_five_fps_dense_geometry_splits_raw_chunks_without_skipping_barriers(self):
        from verify_coordinate_alignment import frame
        points=[point(i+1,1000+i*200) for i in range(191)]
        state={'session_id':'synthetic','anchors':[clock_sample(100),clock_sample(20000),clock_sample(42000)],
            'observations':[dict(seq=p['seq'],action_time_us=p['action_time'],
                callback_clock=clock_sample(p['action_time']/1000+1)) for p in (points[0],points[95],points[-1])]}
        frames=[frame(t) for t in range(500,41001,200)]
        for t,f in zip(range(500,41001,200),frames):
            f['observation']['native_frame']=dict(timestamp_raw=t*1000000,
                callback_monotonic_us=t*1000+10000,callback_boottime_us=t*1000+900010000,
                callback_wall_us=1700000000000000+t*1000+10000)
        frames[100]['observation'].update(valid=False,reason='context_changed')
        from native_frame_timing import bind
        frames,_=bind(frames,state)
        points[70].update(tool_type=0,action=1)
        points[100].update(tool_type=0,action=3)
        summary=self.compare(points,state,frames,limit=2048)
        self.assertGreater(summary['results'].get('candidate',0),0)
        self.assertGreater(summary['results'].get('no_bracketing_geometry',0),0)
        self.assertGreater(summary['results'].get('input_motion_or_context_barrier',0),0)

    def test_held_finger_before_window_cannot_be_forgotten(self):
        points,state,frames=fixture()
        points[0]['tool_type']=0;points[0]['action']=1
        summary=self.compare(points,state,frames,limit=1)
        self.assertEqual(summary['results'].get('candidate',0),0)

    def test_contact_omitted_from_raw_window_is_carried_from_index(self):
        from verify_coordinate_alignment import frame
        points,state,frames=fixture()
        points[0]['tool_type']=0;points[0]['action']=1
        points.append(point(5,30000))
        # This tests a held contact, not a boot-clock jump (the shared generic
        # clock_sample fixture uses a different boot offset from native frames).
        state['anchors'].append(dict(clock_sample(35000),boottime_us=835000001))
        for t in (28000,32000):
            f=frame(t)
            f['observation']['native_frame']=dict(timestamp_raw=t*1000000,
                callback_monotonic_us=t*1000+10000,callback_boottime_us=t*1000+800010000,
                callback_wall_us=1700000000000000+t*1000+10000)
            frames.append(f)
        # Bind the offline comparison's new frames exactly as production does.
        from native_frame_timing import bind
        frames,_=bind(frames,state)
        summary=self.compare(points,state,frames,limit=1)
        self.assertEqual(summary['results'].get('candidate',0),0)

    def test_clock_observation_mismatch_and_missing_raw_reject(self):
        for failure in ('mismatch','missing'):
            points,state,frames=fixture()
            state['observations'][0]['action_time_us']+=1
            if failure=='missing':state['observations'][-1]['seq']=99
            summary=self.compare(points,state,frames)
            self.assertEqual(summary['results'].get('candidate',0),0)

    def test_partial_tail_and_append_resume_are_not_reread(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=root/'raw.jsonl'
            points,state,frames=fixture()
            lines=[(json.dumps(p)+'\n').encode() for p in points]
            raw.write_bytes(lines[0]+lines[1][:35])
            stream=CoordinateStream(root/'stream',raw,'synthetic')
            self.assertEqual(stream.ingest(),len(lines[0]))
            self.assertEqual(stream.ingest(),0)
            with raw.open('ab') as out:out.write(lines[1][35:]+b''.join(lines[2:]))
            self.assertEqual(stream.ingest(),sum(map(len,lines[1:])))
            self.assertEqual(stream.summary()['raw_events'],4)
            stream.close()

    def test_idle_tail_preview_does_not_commit_future_raw_watermark(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=root/'raw.jsonl'
            points,state,frames=fixture()
            raw.write_text(''.join(json.dumps(p)+'\n' for p in points))
            stream=CoordinateStream(root/'stream',raw,'synthetic')
            for i,f in enumerate(frames):stream.add_geometry(dict(f,index=i))
            stream.ingest()
            self.assertTrue(stream.step(state,preview=True))
            self.assertEqual(stream.get('processed_seq',0),0)
            summary=stream.summary()
            self.assertGreater(summary['results'].get('candidate',0),0)
            self.assertEqual(summary['provisional_tail_pen_points'],4)
            self.assertFalse(summary['final_complete'])
            stream.close()

    def test_more_than_64_mib_uses_bounded_reads(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=root/'raw.jsonl'
            with raw.open('wb') as out:
                for i in range(9500):
                    out.write((json.dumps(dict(point(i+1,1000+i*5000),padding='x'*7200))+'\n').encode())
            self.assertGreater(raw.stat().st_size,64*1024*1024)
            stream=CoordinateStream(root/'stream',raw,'synthetic')
            reads=[]
            while n:=stream.ingest():reads.append(n)
            self.assertLessEqual(max(reads),1024*1024+8192)
            state={'session_id':'synthetic','anchors':[clock_sample(100),clock_sample(48_000_000)],
                'observations':[dict(seq=i+1,action_time_us=(1000+i*5000)*1000,
                    callback_clock=clock_sample(1001+i*5000)) for i in (0,1,2)]}
            while stream.step(state,final=True):
                self.assertLessEqual(stream.get('last_window_bytes'),MAX_WINDOW_BYTES)
            self.assertEqual(stream.summary()['processed_pen_points'],9500)
            stream.export();stream.close()

    def test_missing_geometry_advances_live_unknowns_across_long_session(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=root/'raw.jsonl'
            points=[point(i+1,1000+i*1000) for i in range(80)]
            raw.write_text(''.join(json.dumps(p)+'\n' for p in points))
            state={'session_id':'synthetic','anchors':[clock_sample(100),clock_sample(81000)],
                'observations':[dict(seq=i+1,action_time_us=(1000+i*1000)*1000,
                    callback_clock=clock_sample(1001+i*1000)) for i in (0,40,79)]}
            stream=CoordinateStream(root/'stream',raw,'synthetic');stream.ingest()
            try:
                while stream.step(state):pass
                stream.step(state,preview=True)
                summary=stream.summary()
                self.assertGreater(stream.get('processed_seq'),60)
                self.assertEqual(summary['processed_pen_points'],80)
                self.assertEqual(summary['results'].get('candidate',0),0)
                self.assertFalse(summary['final_complete'])
            finally:stream.close()

    def test_replaced_or_truncated_source_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=root/'raw.jsonl'
            raw.write_text(json.dumps(point(1,1000))+'\n')
            stream=CoordinateStream(root/'stream',raw,'synthetic');stream.ingest()
            raw.write_text('')
            with self.assertRaises(ValueError):stream.ingest()
            stream.close()


if __name__=='__main__':unittest.main(verbosity=2)
