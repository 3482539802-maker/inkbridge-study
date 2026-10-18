"""Large audit/recovery stays bounded and preserves duplicate ACK semantics."""
import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ink_receiver import receive_connection
from ink_sessions import SessionStore
from ink_viewer import LiveState
from raw_journal_audit import audit,same_bytes
from recover_device_session import replay,replay_stream
from verify_device_sync import verify
from verify_coordinate_alignment import point


def metadata(raw,count):
    return dict(session_id='synthetic',state='completed',started_at=1,ended_at=2,
                byte_length=len(raw),point_count=count,stop_reason='user_stop',
                dropped_count=0,loss_accounting_complete=True,synced=True)


class AuditChecks(unittest.TestCase):
    def test_final_receipt_and_empty_capture(self):
        for count in (0,20):
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);raw=b''.join((json.dumps(point(i+1,i*10))+'\n').encode() for i in range(count))
                a,b=root/'a.jsonl',root/'b.jsonl'
                a.write_bytes(raw);b.write_bytes(raw)
                for p in (a,b):p.with_suffix('.session.json').write_text(json.dumps(metadata(raw,count)))
                with patch.object(Path,'read_bytes',side_effect=AssertionError('whole-file read forbidden')):
                    result=verify(a,b,a.with_suffix('.session.json'),b.with_suffix('.session.json'))
                self.assertTrue(result['passed']);self.assertEqual(result['sha256'],hashlib.sha256(raw).hexdigest())
                self.assertEqual(result['point_count'],count)

    def test_boundary_sequence_session_and_byte_mismatch_are_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);a,b=root/'a',root/'b'
            records=[point(2,10),point(4,20),dict(point(4,30),session_id='foreign')]
            payload='\n'.join(json.dumps(p) for p in records).encode()
            a.write_bytes(payload);b.write_bytes(payload.replace(b'foreign',b'changed'))
            result=audit(a,'synthetic')
            self.assertEqual(result['sequence_gaps'],2)
            self.assertFalse(result['sequence_monotonic'])
            self.assertFalse(result['all_points_in_session'])
            self.assertFalse(result['complete_jsonl_boundary'])
            self.assertFalse(same_bytes(a,b))

    def test_malformed_and_oversized_record_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'raw'
            for value in (b'{bad}\n',b' '*8193):
                path.write_bytes(value)
                with self.assertRaises(ValueError):audit(path,'synthetic')

    def test_large_files_use_bounded_reads(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);a,b=root/'a',root/'b'
            with a.open('wb') as x,b.open('wb') as y:
                for i in range(6000):
                    line=(json.dumps(point(i+1,i*10))+'\n').encode();x.write(line);y.write(line)
            original=Path.open;largest=[0]
            class Bounded:
                def __init__(self,stream):self.stream=stream
                def __enter__(self):return self
                def __exit__(self,*args):self.stream.close()
                def read(self,n=-1):
                    if not 0<=n<=1024*1024:raise AssertionError('unbounded read')
                    largest[0]=max(largest[0],n);return self.stream.read(n)
                def readline(self,n=-1):
                    if not 0<n<=8193:raise AssertionError('unbounded record')
                    return self.stream.readline(n)
            def opened(path,*args,**kwargs):return Bounded(original(path,*args,**kwargs))
            with patch.object(Path,'open',opened):
                self.assertEqual(audit(a,'synthetic')['point_count'],6000)
                self.assertTrue(same_bytes(a,b))
            self.assertEqual(largest[0],1024*1024)

    def real_replay(self,changed=False):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            raw=b''.join((json.dumps(point(i+1,i*10))+'\n').encode() for i in range(600))
            path=root/'device.jsonl';path.write_bytes(raw);meta=metadata(raw,600)
            state=LiveState(root/'legacy.jsonl');store=SessionStore(root/'sessions',state)
            store.hello({'type':'session','protocol':2,'session_id':'synthetic','started_at':1})
            prefix=raw[:raw.find(b'\n',10000)+1]
            store.batch('synthetic',{'type':'batch','session_id':'synthetic','offset':0,'data':prefix.decode()})
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(5)
                errors=[]
                def serve():
                    try:
                        connection,_=listener.accept()
                        with connection:receive_connection(connection,io.StringIO(),state,time.monotonic()+10,store)
                    except Exception as error:errors.append(error)
                worker=threading.Thread(target=serve,daemon=True);worker.start()
                try:
                    if changed:
                        def fail():raise ValueError('source changed')
                        with path.open('rb') as source,self.assertRaises(ValueError):
                            replay_stream(source,len(raw),meta,listener.getsockname()[1],fail)
                    else:
                        with patch.object(Path,'read_bytes',side_effect=AssertionError('unbounded read')):
                            ack=replay(path,meta,listener.getsockname()[1])
                        self.assertTrue(ack['complete'])
                finally:worker.join(timeout=5)
                self.assertFalse(worker.is_alive());self.assertFalse(errors)
            self.assertTrue(same_bytes(path,root/'sessions/synthetic.jsonl'))
            result=json.loads((root/'sessions/synthetic.session.json').read_bytes())
            self.assertEqual(result['state'],'recording' if changed else 'completed')
            self.assertGreater(state.replays,0)

    def test_streamed_recovery_verifies_existing_prefix_and_completes(self):
        self.real_replay()

    def test_changed_source_never_sends_completion(self):
        self.real_replay(changed=True)


if __name__=='__main__':unittest.main(verbosity=2)
