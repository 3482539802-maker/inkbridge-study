"""Durable job finalization, clock-tail ordering and continued acquisition."""
import json
from pathlib import Path
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from coordinate_stream import CoordinateStream
from geometry_acquisition import GeometryAcquisition
from session_pipeline import NumericProcessor,SessionSource,write_json
from screen_frame_probe import NativeProbeFrames,DeviceClockUnavailable
from verify_coordinate_acceptance import fixture
import session_pipeline as pipeline


class PipelineChecks(unittest.TestCase):
    def test_shared_context_source_does_not_issue_duplicate_device_tree_read(self):
        frames=Mock();shared=Mock();shared.window_context.return_value=({'focused':True},{'page_layers':[]})
        source=SessionSource(frames,'synthetic',context_reader=shared)
        self.assertEqual(source.window_context(),shared.window_context.return_value)
        shared.window_context.assert_called_once();frames.window_context.assert_not_called()

    def test_prepared_capture_rechecks_user_stop_without_renewing_again(self):
        frames=Mock(_lease_sent=10**12)
        source=SessionSource(frames,'synthetic')
        with patch.object(pipeline,'receiver',side_effect=[
                [{'session_id':'synthetic','state':'recording'}],
                [{'session_id':'synthetic','state':'completed'}]]):
            self.assertTrue(source.prepare_payload())
            image,observation,_=source.capture_prepared_payload()
        self.assertIsNone(image)
        self.assertEqual(observation['reason'],'user_capture_not_active')
        frames.capture_payload.assert_not_called()
        frames.renew.assert_not_called()

    def test_idle_device_outage_retries_without_revoking_or_launching(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);output=root/'service'
            frames=Mock(prefix=['fake'],lease_token='a'*32,_lease_sent=0)
            frames.arm.side_effect=[RuntimeError('HDC offline'),None]
            frames.renew.side_effect=[subprocess.TimeoutExpired('hdc',1),DeviceClockUnavailable('bad transport date'),KeyboardInterrupt]
            argv=['pipeline','--continuous','--target','test','--pdf',str(root/'input.pdf'),
                  '--hinote',str(root/'input.hinote'),'--title','test','--output',str(output)]
            with patch('sys.argv',argv),patch.object(pipeline,'ROOT',root), \
                 patch.object(pipeline,'load_binding',return_value={'title':'test','source_hinote_sha256':'b'*64}), \
                 patch.object(pipeline,'PDFRegistration'),patch.object(pipeline,'NativeProbeFrames',return_value=frames), \
                 patch.object(pipeline,'RawGuardPoller'),patch.object(pipeline,'NumericProcessor'), \
                 patch.object(pipeline,'receiver',return_value=[]),patch.object(pipeline.time,'sleep'), \
                 patch.object(pipeline,'dashboard') as views:
                pipeline.main()
            self.assertEqual(frames.arm.call_count,2)
            self.assertEqual(frames.renew.call_count,3)
            frames.revoke.assert_called_once() # Only final shutdown, never on an outage.
            self.assertEqual(sum(c.args[1]['stage']=='device_unavailable' for c in views.call_args_list),3)

    def test_invalid_transport_clock_does_not_send_or_advance_lease(self):
        frames=NativeProbeFrames.__new__(NativeProbeFrames)
        frames.size=(2800,1840);frames.reduction=2;frames.lease_token='a'*32;frames._lease_sent=123
        frames.command=Mock(return_value='[Fail] connection closed');frames.send_text=Mock();frames.close_socket=Mock()
        with self.assertRaises(DeviceClockUnavailable):frames.renew(arming=True)
        frames.send_text.assert_not_called();self.assertEqual(frames._lease_sent,123)
        with patch('screen_frame_probe.receiver',return_value=[]):
            with self.assertRaises(DeviceClockUnavailable):frames.arm(continuous=True)
        frames.send_text.assert_not_called();self.assertEqual(frames._lease_sent,123)

    def test_saved_dashboard_marks_stale_readiness_and_does_not_depend_on_server_callbacks(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            pipeline.dashboard(root,{'stage':'waiting_user_capture','message':'已就绪'})
            html=(root/'report.html').read_text(encoding='utf-8')
            self.assertIn('Date.now()/1000-updated',html)
            self.assertIn('age>45',html)
            self.assertIn('当前是否就绪需重新检查',html)

    def test_supervisor_keeps_session_past_100_frames_and_closes_job(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);output=root/'service';counter={'frames':0,'armed':0,'receiver_failures':0}
            frames=Mock(prefix=['fake']);frames.lease_token='a'*32
            frames.revoke.side_effect=[RuntimeError('route lost after user stop'),None]
            def arm(**kwargs):counter['armed']+=1
            frames.arm.side_effect=arm;frames._lease_sent=10**12
            def sessions():
                if counter['armed']==0:return []
                if counter['armed']>=2:raise KeyboardInterrupt
                if counter['frames']==1 and counter['receiver_failures']==0:
                    counter['receiver_failures']+=1
                    raise OSError('temporary receiver outage during frame completion')
                return [{'session_id':'synthetic','state':'recording' if counter['frames']<105 else 'completed'}]
            def next_frame(**kwargs):
                counter['frames']+=1
                return SimpleNamespace(image=None,started=0,acquired=1,guard_token=0)
            producer=Mock();producer.next.side_effect=next_frame
            argv=['pipeline','--continuous','--target','test','--pdf',str(root/'input.pdf'),
                  '--hinote',str(root/'input.hinote'),'--title','test','--output',str(output)]
            with patch('sys.argv',argv),patch.object(pipeline,'ROOT',root), \
                 patch.object(pipeline,'load_binding',return_value={'title':'test','source_pdf_sha256':'a'*64,'source_hinote_sha256':'b'*64}), \
                 patch.object(pipeline,'PDFRegistration'),patch.object(pipeline,'NativeProbeFrames',return_value=frames), \
                 patch.object(pipeline,'RawGuardPoller'),patch.object(pipeline,'NumericProcessor'), \
                 patch.object(pipeline,'EditObservationWorker'), \
                 patch.object(pipeline,'receiver',side_effect=sessions), \
                 patch('geometry_sampling.BracketedGeometryAcquisition',return_value=producer), \
                 patch.object(pipeline,'capture_geometry',return_value=({'valid':False},{'valid':False},1)), \
                 patch.object(pipeline.time,'sleep'):
                pipeline.main()
            job=json.loads((output/'session-synthetic/job.json').read_bytes())
            self.assertTrue(job['geometry_closed'])
            self.assertEqual(counter['receiver_failures'],1)
            store=CoordinateStream(output/'session-synthetic',root/'output/ink/sessions/synthetic.jsonl','synthetic')
            self.assertGreater(store.db.execute('SELECT COUNT(*) FROM geometry').fetchone()[0],100)
            store.close();producer.close.assert_called_once()

    def test_finalizer_waits_for_raw_ack_and_native_stop_tail_then_resumes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw_dir=root/'raw';raw_dir.mkdir()
            directory=root/'session-synthetic';directory.mkdir()
            points,clock,frames=fixture()
            raw=raw_dir/'synthetic.jsonl';raw.write_text(''.join(json.dumps(p)+'\n' for p in points))
            path=directory/'job.json'
            write_json(path,{'session_id':'synthetic','page_margins':False,'geometry_closed':True})
            metadata={'session_id':'synthetic','state':'completed','byte_length':raw.stat().st_size,'point_count':len(points)}
            write_json(raw.with_suffix('.session.json'),metadata)
            store=CoordinateStream(directory,raw,'synthetic')
            for i,f in enumerate(frames):store.add_geometry(dict(f,index=i))
            store.close()
            write_json(directory/'clock.json',clock)
            processor=NumericProcessor(root,[],raw_dir)
            with patch('session_pipeline.pull_clock',return_value=True):
                processor.process(path)
                progress=json.loads((directory/'processing.json').read_bytes())
                self.assertEqual(progress['stage'],'waiting_final_clock')
                self.assertFalse((directory/'points.jsonl').exists())
                clock['stopped']=True;write_json(directory/'clock.json',clock)
                # Also exercise recovery with no previously created ledger.
                check=CoordinateStream(directory,raw,'synthetic')
                with check.db:check.db.execute('DROP TABLE edit_events')
                check.close()
                # Final clock can arrive before the bounded coordinate backlog
                # is done. Do not freeze edits against incomplete locations.
                with patch.object(CoordinateStream,'step',return_value=True), \
                     patch.object(pipeline,'process_edits') as edits:
                    processor.process(path)
                    edits.assert_not_called()
                    self.assertEqual(json.loads((directory/'processing.json').read_bytes())['stage'],'finalizing')
                # New supervisor demonstrates restart from the durable index.
                processor=NumericProcessor(root,[],raw_dir)
                for _ in range(3):processor.process(path)
            self.assertTrue(json.loads(path.read_bytes())['final_complete'])
            self.assertTrue(json.loads((directory/'final-audit.json').read_bytes())['raw_index_coordinates_export_equal'])
            self.assertEqual(len((directory/'points.jsonl').read_text().splitlines()),len(points))
            self.assertEqual(json.loads((directory/'processing.json').read_bytes())['stage'],'complete')

    def test_source_never_requests_a_frame_after_user_stop(self):
        frames=Mock();source=SessionSource(frames,'s')
        with patch('session_pipeline.receiver',return_value=[{'session_id':'s','state':'completed'}]):
            image,observation=source.capture('note')
        self.assertIsNone(image);self.assertFalse(observation['valid'])
        frames.capture.assert_not_called();frames.renew.assert_not_called()
        with patch('session_pipeline.receiver',return_value=[{'session_id':'s','state':'completed'}]):
            image,observation,received=source.capture_payload()
        self.assertIsNone(image);self.assertEqual(observation['reason'],'user_capture_not_active')
        frames.capture_payload.assert_not_called();frames.renew.assert_not_called()

    def test_fast_sampler_renews_before_native_100_request_limit(self):
        frames=Mock(_lease_sent=10**12)
        source=SessionSource(frames,'s')
        with patch('session_pipeline.receiver',return_value=[{'session_id':'s','state':'recording'}]):
            for _ in range(60): source.capture_payload()
            frames.renew.assert_not_called()
            source.capture_payload()
        frames.renew.assert_called_once()
        self.assertEqual(source.requests_since_renew,1)

    def test_continuous_queue_crosses_100_without_unbounded_images(self):
        source=GeometryAcquisition(Mock(capture=lambda _: (object(),{'valid':True})), 'note',lambda:0,0,None,None)
        try:
            for _ in range(125):self.assertIsNotNone(source.next(timeout=1))
            self.assertLessEqual(source.queue.qsize(),1)
        finally:source.close()
        self.assertFalse(source.thread.is_alive())

    def test_lease_renewal_uses_same_session_token_and_finite_expiry(self):
        frames=NativeProbeFrames.__new__(NativeProbeFrames)
        frames.size=(2800,1840);frames.reduction=2
        frames.command=Mock(return_value='1700000000000')
        frames.send_text=Mock()
        with patch('screen_frame_probe.receiver',return_value=[]):frames.arm(continuous=True)
        token=frames.lease_token
        frames.renew(arming=True)
        calls=frames.send_text.call_args_list
        self.assertIn(token,calls[0].args[1])
        self.assertEqual(calls[1].args,('geometry-lease.txt',token+' 1700000060000\n'))
        self.assertIn(token,calls[2].args[1])
        frames.revoke()
        self.assertIn(('geometry-lease.txt',token+' 0\n'),[c.args for c in frames.send_text.call_args_list])


if __name__=='__main__':unittest.main(verbosity=2)
