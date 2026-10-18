"""Uncovered startup events cannot veto covered writing or erase held fingers."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from coordinate_alignment import align, assess_clock
from coordinate_stream import CoordinateStream
from verify_coordinate_alignment import clocks, clock_sample, frame, point


def fixture():
    state = clocks()
    state['anchors'][0] = clock_sample(990.012)
    state['observations'].append({'seq':5,'action_time_us':15990000,'callback_clock':clock_sample(16000)})
    points = [point(i+1,t-10) for i,t in enumerate((1000,6000,11000))]
    points += [point(4,12000),point(5,15990)]
    return points,state,[frame(11000),frame(13000)]


def run_alignment(points,state,frames):
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder); raw = root/'raw.jsonl'; out = root/'points.jsonl'
        original = ''.join(json.dumps(p)+'\n' for p in points).encode()
        raw.write_bytes(original)
        summary = align(raw,state,frames,out,stable_runs=True)
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        if raw.read_bytes()!=original: raise AssertionError('Raw changed')
        return summary,{row['seq']:row for row in rows}


class CoverageChecks(unittest.TestCase):
    def test_only_covered_observations_establish_calibration_without_extending_range(self):
        points,state,frames = fixture(); original = deepcopy(state)
        result = assess_clock(state)
        self.assertTrue(result['usable_candidate'])
        self.assertEqual(result['observation_count'],3)
        self.assertEqual(result['all_validated_observation_count'],4)
        self.assertEqual(result['pre_anchor_observation_seq_range'],[1,1])
        self.assertAlmostEqual(result['maximum_pre_anchor_action_lead_us'],12)
        self.assertEqual(result['action_time_range_us'][0],990012)
        self.assertEqual(state,original)
        _,rows = run_alignment(points,state,frames)
        self.assertIsNone(rows[1]['pdf_xy'])
        self.assertEqual(rows[1]['reason'],'outside_independent_clock_coverage')
        self.assertEqual(rows[4]['pdf_xy'],[100,200])

    def test_early_observation_does_not_supply_missing_count_or_span(self):
        _,state,_ = fixture()
        state['observations'].pop()
        self.assertEqual(assess_clock(state)['reason'],'insufficient_covered_event_clock_observations')
        state['observations'].insert(2,{'seq':3,'action_time_us':9990000,'callback_clock':clock_sample(10000)})
        state['observations'][-1]['seq'] = 4
        self.assertEqual(assess_clock(state)['reason'],'event_clock_observation_span_too_short')

    def test_early_bad_lag_offset_order_history_or_late_callback_is_not_discarded(self):
        for failure in ('lag','offset','order','history','late'):
            with self.subTest(failure=failure):
                _,state,_ = fixture()
                if failure == 'lag': state['observations'][0]['callback_clock'] = clock_sample(1200)
                elif failure == 'offset': state['observations'][0]['callback_clock']['unix_ms'] += 10
                elif failure == 'order': state['observations'][0]['seq'] = 10
                elif failure == 'history': state['history_lost'] = True
                else: state['anchors'][-1] = clock_sample(15000)
                self.assertFalse(assess_clock(state)['usable_candidate'])

    def test_early_raw_observation_must_still_exist_and_match(self):
        for failure in ('missing','mismatch'):
            with self.subTest(failure=failure):
                points,state,frames = fixture()
                if failure == 'missing': points = points[1:]
                else: points[0]['action_time'] += 1
                _,rows = run_alignment(points,state,frames)
                self.assertEqual(rows[4]['reason'],'raw_sequence_clock_or_observation_mismatch')

    def test_pre_anchor_finger_held_without_moves_still_blocks_covered_writing(self):
        points,state,frames = fixture()
        points[0].update(tool_type=0,action=1)
        _,rows = run_alignment(points,state,frames)
        self.assertEqual(rows[4]['reason'],'input_motion_or_context_barrier')

    def test_pre_anchor_release_removes_the_held_contact(self):
        points,state,frames = fixture()
        # The uncovered DOWN and UP bracket a closed contact. Both remain in
        # raw, but neither may become a page location or a held finger forever.
        state['anchors'][0] = clock_sample(6000)
        state['observations'].append({'seq':6,'action_time_us':20990000,'callback_clock':clock_sample(21000)})
        points.append(point(6,20990))
        points[0].update(tool_type=0,action=1)
        points[1].update(tool_type=0,action=3)
        _,rows = run_alignment(points,state,frames)
        self.assertEqual(rows[4]['status'],'candidate')

    def test_incremental_restart_and_offline_agree_with_an_early_held_contact(self):
        for held in (False,True):
            with self.subTest(held=held),tempfile.TemporaryDirectory() as folder:
                points,state,frames = fixture()
                if held: points[0].update(tool_type=0,action=1)
                state['anchors'].insert(1,clock_sample(18000))
                frames.append(frame(21000))
                for t,f in zip((11000,13000,21000),frames):
                    f['observation']['native_frame'] = dict(timestamp_raw=t*1000000,
                        callback_monotonic_us=t*1000+10000,
                        callback_boottime_us=t*1000+900010000,
                        callback_wall_us=1700000000000000+t*1000+10000)
                from native_frame_timing import bind
                frames,calibration = bind(frames,state)
                self.assertTrue(calibration['usable_candidate'])
                root = Path(folder); raw = root/'raw.jsonl'
                raw.write_text(''.join(json.dumps(p)+'\n' for p in points))
                stream = CoordinateStream(root/'stream',raw,'synthetic',True)
                for i,f in enumerate(frames): stream.add_geometry(dict(f,index=i))
                stream.ingest()
                # Live processing must advance even while the first raw event
                # remains uncalibrated, and restart retains full raw evidence.
                self.assertTrue(stream.step(state,limit=2))
                stream.close()
                stream = CoordinateStream(root/'stream',raw,'synthetic',True)
                while stream.step(state,final=True,limit=2): pass
                stream.export();stream.close()
                actual = {r['seq']:r for r in map(json.loads,(root/'stream/points.jsonl').read_text().splitlines())}
                _,expected = run_alignment(points,state,frames)
                for seq in expected:
                    self.assertEqual([actual[seq][k] for k in ('status','reason','pdf_xy')],
                                     [expected[seq][k] for k in ('status','reason','pdf_xy')])


if __name__=='__main__': unittest.main(verbosity=2)
