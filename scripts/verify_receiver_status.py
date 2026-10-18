"""Status polling must not scan/send a journal or hide user stop/restart."""
from collections import deque
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from ink_viewer import LiveState,start_viewer
from receiver_status import ReceiverStatus


class CountingDeque(deque):
    seen=0
    def __iter__(self):
        for value in super().__iter__():
            self.seen+=1
            yield value


class StatusChecks(unittest.TestCase):
    def test_status_avoids_event_iteration_and_event_page_stops_at_1000(self):
        state=LiveState(Path('synthetic.jsonl'))
        state.events=CountingDeque({'cursor':i,'point':{'session_id':'s'}} for i in range(1,50001))
        state.total=50000
        state.sessions={'s':{'session_id':'s','state':'recording'}}
        snapshot=state.snapshot(0,include_events=False)
        self.assertEqual(snapshot['events'],[]);self.assertEqual(state.events.seen,0)
        self.assertEqual(snapshot['next_cursor'],50000)
        self.assertEqual(len(state.snapshot(0)['events']),1000)
        self.assertEqual(state.events.seen,1000)

    def test_real_http_status_returns_fresh_stop_and_no_points(self):
        with tempfile.TemporaryDirectory() as folder:
            state=LiveState(Path(folder)/'raw.jsonl')
            state.sessions={'s':{'session_id':'s','state':'recording'}}
            state.append({'tool_type':1,'session_id':'s','seq':1})
            server=start_viewer(0,state)
            try:
                client=ReceiverStatus('http://127.0.0.1:'+str(server.server_address[1]))
                first=client.fetch();self.assertEqual(first['events'],[])
                with state.lock:state.sessions['s']['state']='completed'
                second=client.fetch()
                self.assertEqual(second['sessions'][0]['state'],'completed')
                with urlopen(client.base+'/events') as response:events=json.load(response)
                self.assertEqual(len(events['events']),1)
            finally:server.shutdown();server.server_close()

    def test_old_receiver_uses_latest_cursor_and_recovers_after_restart(self):
        client=ReceiverStatus()
        missing=HTTPError('status',404,'old receiver',None,None)
        with patch.object(client,'read',side_effect=[missing,{'points':50000,'sessions':[]},
            {'points':2,'sessions':[{'state':'completed'}]}, {'points':3,'sessions':[]}]) as read:
            client.fetch();client.fetch();client.fetch()
        self.assertEqual([c.args[0] for c in read.call_args_list],
                         ['/status','/events?after=0','/events?after=50000','/events?after=2'])

    def test_status_server_errors_are_not_silently_replaced_with_stale_results(self):
        client=ReceiverStatus()
        with patch.object(client,'read',side_effect=HTTPError('status',500,'failure',None,None)) as read:
            with self.assertRaises(HTTPError):client.fetch()
        self.assertEqual(read.call_count,1)

    def test_legacy_client_detects_receiver_upgrade(self):
        client=ReceiverStatus();client.legacy_until=10;client.cursor=7
        with patch('receiver_status.time.monotonic',return_value=11), \
             patch.object(client,'read',return_value={'points':8,'sessions':[]}) as read:
            client.fetch()
        read.assert_called_once_with('/status')


if __name__=='__main__':unittest.main(verbosity=2)
