"""Long-session recovery, outage, conflict and raw identity checks without HDC."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from capture_clock_history import load_clock, merge_windows, pull_clock, history_directory, backfill_clock
from coordinate_alignment import align, assess_clock
from verify_coordinate_alignment import clock_sample, frame, point

SESSION = '5494f517-426f-4157-aa29-eadb39f41d39'


def windows(count=90):
    anchors, observations, result = [], [], []
    for index in range(count):
        t = 100 + index * 5001
        anchors.append(clock_sample(t))
        if index:
            observations.append(dict(seq=index, action_time_us=t*1000-1000, callback_clock=clock_sample(t)))
        state = dict(session_id=SESSION, history_format=1, history_segments=len(result),
                     anchors=deepcopy(anchors[-32:]), observations=deepcopy(observations[-32:]))
        if (index+1) % 16 == 0:
            result.append(state)
    latest = dict(state, history_segments=len(result))
    return result, latest


class HistoryChecks(unittest.TestCase):
    def test_stopped_two_hour_session_recovers_more_than_one_poll(self):
        segments,latest=windows(1441)
        def transfer(argv,**kwargs):
            remote,local=argv[-2:]
            state=latest if remote.endswith('.clock.json') else segments[int(remote.rsplit('-',1)[1].split('.')[0])]
            Path(local).write_text(json.dumps(state))
            return SimpleNamespace(returncode=0,stdout=b'OK')
        with tempfile.TemporaryDirectory() as folder, \
             patch('capture_clock_history.subprocess.run',side_effect=transfer),patch('builtins.print'):
            path=Path(folder)/'clock.json'
            self.assertFalse(backfill_clock([],SESSION,path,max_segments=4))
            first=(history_directory(path)/'0.json').read_bytes()
            self.assertTrue(backfill_clock([],SESSION,path,max_segments=128))
            merged=load_clock(path)
            self.assertEqual(len(merged['observations']),1440)
            self.assertEqual(len(merged['anchors']),1441)
            self.assertTrue(merged['history_complete'])
            self.assertEqual((history_directory(path)/'0.json').read_bytes(),first)

    def test_early_and_late_points_survive_rotation_and_raw_is_unchanged(self):
        segments, latest = windows()
        merged = merge_windows(segments+[latest])
        self.assertEqual(len(merged['anchors']),90)
        self.assertEqual(len(merged['observations']),89)
        self.assertTrue(assess_clock(merged)['usable_candidate'])
        points = [dict(point(o['seq'],o['action_time_us']/1000),session_id=SESSION)
                  for o in merged['observations']]
        frames = [frame(t) for t in (14500,15500,435000,436000)]
        with tempfile.TemporaryDirectory() as folder:
            raw, target = Path(folder)/'raw.jsonl', Path(folder)/'points.jsonl'
            raw.write_text(''.join(json.dumps(p)+'\n' for p in points))
            original = raw.read_bytes()
            report = align(raw, merged, frames, target)
            rows = [json.loads(line) for line in target.read_text().splitlines()]
            self.assertEqual(raw.read_bytes(),original)
            self.assertTrue(report['clock']['usable_candidate'])
            self.assertEqual(rows[2]['status'],'candidate')
            self.assertEqual(rows[86]['status'],'candidate')

    def test_missing_or_nonoverlapping_history_is_not_bridged(self):
        segments, latest = windows()
        merged = merge_windows([segments[0],latest])
        self.assertFalse(merged['history_complete'])
        self.assertEqual(assess_clock(merged)['reason'],'incomplete_clock_history')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'clock.json'
            path.write_text(json.dumps(latest))
            merged = load_clock(path)
            self.assertFalse(merged['history_complete'])
            self.assertEqual(len(merged['history_missing_segments']),len(segments))

    def test_conflicts_sessions_jumps_and_source_reordering_rejected(self):
        segments, latest = windows()
        bad = deepcopy(segments)
        bad[1]['observations'][0]['action_time_us'] += 1
        with self.assertRaises(ValueError):merge_windows(bad+[latest])
        bad = deepcopy(segments)
        bad[1]['session_id']='other'
        with self.assertRaises(ValueError):merge_windows(bad+[latest])
        bad = deepcopy(segments)
        bad[0]['anchors'].reverse()
        with self.assertRaises(ValueError):merge_windows(bad+[latest])
        latest = deepcopy(latest)
        for a in latest['anchors']:
            a['unix_ms'] += 1000
        with self.assertRaises(ValueError):merge_windows(segments+[latest])

    def test_bounded_backfill_and_pc_restart_skip_cached_immutable_files(self):
        segments, latest = windows()
        calls=[]
        def transfer(argv, **kwargs):
            remote, local = argv[-2:]
            calls.append(remote)
            state = latest if remote.endswith('.clock.json') else segments[int(remote.rsplit('-',1)[1].split('.')[0])]
            Path(local).write_text(json.dumps(state))
            return SimpleNamespace(returncode=0,stdout=b'OK')
        with tempfile.TemporaryDirectory() as folder, patch('capture_clock_history.subprocess.run',side_effect=transfer):
            path = Path(folder)/'clock.json'
            self.assertTrue(pull_clock([],SESSION,path,max_backfill=2))
            self.assertFalse(load_clock(path)['history_complete'])
            self.assertEqual(len(calls),3)
            first = (history_directory(path)/'0.json').read_bytes()
            self.assertTrue(pull_clock([],SESSION,path))
            self.assertTrue(load_clock(path)['history_complete'])
            self.assertEqual((history_directory(path)/'0.json').read_bytes(),first)
            calls.clear()
            self.assertTrue(pull_clock([],SESSION,path))
            self.assertEqual(len(calls),1,'a restarted collector reuses disk checkpoints')
            with patch('capture_clock_history.subprocess.run',return_value=SimpleNamespace(returncode=1,stdout=b'[Fail]')):
                before = path.read_bytes()
                self.assertFalse(pull_clock([],SESSION,path))
                self.assertEqual(path.read_bytes(),before)

    def test_legacy_checkpoints_remain_overlapping_without_one_file_per_poll(self):
        segments, latest=windows()
        states=[dict(w) for w in segments+[latest]]
        for s in states:
            s.pop('history_format');s.pop('history_segments')
        remote={'state':states[0]}
        def transfer(argv,**kwargs):
            Path(argv[-1]).write_text(json.dumps(remote['state']))
            return SimpleNamespace(returncode=0,stdout=b'OK')
        with tempfile.TemporaryDirectory() as folder, patch('capture_clock_history.subprocess.run',side_effect=transfer):
            path=Path(folder)/'clock.json'
            for s in states:
                remote['state']=s
                for _ in range(3):self.assertTrue(pull_clock([],SESSION,path))
            merged=load_clock(path)
            self.assertTrue(merged['history_complete'])
            self.assertEqual(len(merged['observations']),89)
            # One head cursor plus the five ~80s checkpoints; the latest tail
            # remains in clock.json and must not force a duplicate disk window.
            self.assertLessEqual(len(list(history_directory(path).glob('legacy-*.json'))),len(states)+1)


if __name__ == '__main__':unittest.main(verbosity=2)
