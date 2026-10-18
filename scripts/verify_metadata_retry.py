"""A transient Windows metadata lock must never kill ingestion or fake an ACK."""
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch
from ink_sessions import publish_metadata,MetadataPublishError
from ink_receiver import receive_connection
from geometry_guard import RawGuardPoller


class MetadataChecks(unittest.TestCase):
    def test_transient_denials_retry_then_atomically_publish(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json';tmp=Path(folder)/'state.tmp'
            path.write_text('old');tmp.write_text('new');replace=os.replace
            calls=[]
            def blocked(a,b):
                calls.append(1)
                if len(calls)<3:raise PermissionError('reader sharing violation')
                return replace(a,b)
            with patch('ink_sessions.os.replace',side_effect=blocked),patch('ink_sessions.time.sleep'):
                publish_metadata(tmp,path)
            self.assertEqual(path.read_text(),'new');self.assertEqual(len(calls),3)

    def test_persistent_denial_keeps_previous_and_raises_bounded_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json';tmp=Path(folder)/'state.tmp'
            path.write_text('old');tmp.write_text('new')
            with patch('ink_sessions.os.replace',side_effect=PermissionError('busy')) as replace,patch('ink_sessions.time.sleep'):
                with self.assertRaises(MetadataPublishError):publish_metadata(tmp,path)
            self.assertEqual(replace.call_count,21)
            self.assertEqual(path.read_text(),'old');self.assertEqual(tmp.read_text(),'new')

    def test_failed_metadata_drops_connection_without_ack_or_server_exception(self):
        state=Mock(lock=threading.Lock(),connections=0,errors=0,truncated=0)
        connection=Mock();connection.recv.return_value=json.dumps({'type':'session'}).encode()+b'\n'
        sessions=Mock();sessions.hello.side_effect=MetadataPublishError('busy')
        receive_connection(connection,io.StringIO(),state,time.monotonic()+1,sessions)
        connection.sendall.assert_not_called()
        self.assertEqual(state.errors,1);self.assertFalse(state.connected)

    def test_guard_can_start_while_receiver_is_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            guard=RawGuardPoller(Path(folder)/'status.json',fetch=Mock(side_effect=OSError('offline')))
            try:
                guard.start()
                self.assertTrue(guard.thread.is_alive())
                self.assertFalse(guard.connected)
            finally:guard.close()


if __name__=='__main__':unittest.main(verbosity=2)
