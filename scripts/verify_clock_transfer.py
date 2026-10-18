"""Blocked clock transport must not stall durable raw indexing."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

from clock_transfer import ClockTransferWorker
from coordinate_stream import CoordinateStream
from session_pipeline import NumericProcessor,write_json
from verify_coordinate_acceptance import fixture


class ClockTransferChecks(unittest.TestCase):
    def wait_finished(self,worker,session):
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            result=worker.snapshot(session)
            if result.get('attempts') and not result['in_flight']:return result
            threading.Event().wait(.005)
        self.fail('Clock transfer did not finish')

    def test_blocked_transfer_does_not_stall_indexing(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw_dir=root/'raw';raw_dir.mkdir();directory=root/'session-synthetic';directory.mkdir()
            points,_,_=fixture();raw=raw_dir/'synthetic.jsonl'
            raw.write_text(''.join(json.dumps(p)+'\n' for p in points))
            job=directory/'job.json';write_json(job,{'session_id':'synthetic','page_margins':False})
            write_json(raw.with_suffix('.session.json'),{'session_id':'synthetic','state':'recording'})
            entered=threading.Event();release=threading.Event()
            def fetch(*args,**kwargs):entered.set();release.wait(3);return False
            with patch('session_pipeline.pull_clock',side_effect=fetch):processor=NumericProcessor(root,[],raw_dir)
            processor.clock_transfers.start()
            try:
                processor.clock_transfers.request('other',directory/'other.json',False)
                self.assertTrue(entered.wait(1))
                processor.process(job)
                progress=json.loads((directory/'processing.json').read_bytes())
                self.assertEqual(progress['raw_events'],len(points))
                self.assertTrue(progress['clock_transfer']['queued'])
                self.assertFalse(progress['clock_stop_tail_received'])
            finally:release.set();processor.close()

    def test_active_priority_coalescing_and_bounded_queue(self):
        order=[];worker=ClockTransferWorker([],lambda prefix,s,*a,**kw:order.append(s) or True,capacity=2)
        worker.request('old',Path('old'),False);worker.request('older',Path('older'),False)
        self.assertFalse(worker.request('overflow',Path('overflow'),False))
        self.assertTrue(worker.request('active',Path('active'),True))
        for _ in range(20):worker.request('active',Path('active'),True)
        self.assertEqual(len(worker.pending),2)
        worker.start()
        try:
            self.wait_finished(worker,'active');self.wait_finished(worker,'older')
            self.assertEqual(order,['active','older'])
            self.assertFalse(worker.request('active',Path('active'),True))
        finally:worker.close()

    def test_transfer_exception_is_contained_and_next_job_runs(self):
        def fetch(prefix,s,*a,**kw):
            if s=='bad':raise OSError('unavailable')
            return True
        worker=ClockTransferWorker([],fetch)
        worker.request('bad',Path('bad'),False);worker.request('good',Path('good'),False);worker.start()
        try:
            bad=self.wait_finished(worker,'bad');good=self.wait_finished(worker,'good')
            self.assertEqual(bad['consecutive_failures'],1);self.assertEqual(bad['last_error_type'],'OSError')
            self.assertTrue(good['last_transfer_succeeded'])
            self.assertFalse(worker.request('bad',Path('bad'),False))
            with patch('clock_transfer.time.monotonic',return_value=time.monotonic()+6):
                self.assertFalse(worker.request('bad',Path('bad'),False))
                self.assertTrue(worker.request('bad',Path('bad'),True))
        finally:worker.close()

    def test_missing_clock_is_visible_and_recovery_finishes_without_new_recording(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw_dir=root/'raw';raw_dir.mkdir();directory=root/'session-synthetic';directory.mkdir()
            points,clock,frames=fixture();raw=raw_dir/'synthetic.jsonl'
            raw.write_text(''.join(json.dumps(p)+'\n' for p in points))
            job=directory/'job.json';write_json(job,{'session_id':'synthetic','page_margins':False,'geometry_closed':True})
            write_json(raw.with_suffix('.session.json'),{'session_id':'synthetic','state':'completed',
                'point_count':len(points),'byte_length':raw.stat().st_size})
            store=CoordinateStream(directory,raw,'synthetic')
            for i,f in enumerate(frames):store.add_geometry(dict(f,index=i))
            store.close()
            processor=NumericProcessor(root,[],raw_dir)
            processor.clock_transfers=Mock(snapshot=Mock(return_value={'last_transfer_succeeded':False}))
            processor.process(job)
            failed=json.loads((directory/'processing.json').read_bytes())
            self.assertEqual(failed['stage'],'clock_unavailable');self.assertTrue(failed['raw_upload_complete'])
            self.assertFalse(failed['final_complete']);self.assertFalse((directory/'points.jsonl').exists())
            clock['stopped']=True;write_json(directory/'clock.json',clock)
            processor.clock_transfers.snapshot.return_value={'last_transfer_succeeded':True}
            for _ in range(3):processor.process(job)
            self.assertTrue(json.loads(job.read_bytes())['final_complete'])
            self.assertTrue(json.loads((directory/'final-audit.json').read_bytes())['raw_index_coordinates_export_equal'])
            processor.close()


if __name__=='__main__':unittest.main(verbosity=2)
