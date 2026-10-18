"""Read session state without retransferring old raw event pages on every poll."""
import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import urlopen


class ReceiverStatus:
    def __init__(self,base='http://127.0.0.1:18766'):
        self.base=base;self.legacy_until=0;self.cursor=0;self.lock=threading.Lock()

    def read(self,path):
        with urlopen(self.base+path,timeout=2) as response:
            payload=response.read(8*1024*1024+1)
        if len(payload)>8*1024*1024:raise ValueError('Receiver status exceeds bound')
        state=json.loads(payload)
        if type(state.get('points')) is not int or state['points']<0 or not isinstance(state.get('sessions'),list):
            raise ValueError('Invalid receiver status')
        return state

    def fetch(self):
        # No status cache: stop checks must always observe a new HTTP response.
        # The legacy cursor is shared by the acquisition and tool workers.
        with self.lock:
            if time.monotonic()>=self.legacy_until:
                try:
                    result=self.read('/status')
                    self.cursor=result['points']
                    return result
                except HTTPError as error:
                    if error.code!=404:raise
                    self.legacy_until=time.monotonic()+30
            result=self.read('/events?after='+str(self.cursor))
            self.cursor=result['points']
            return result


_client=ReceiverStatus()


def receiver():
    return _client.fetch()['sessions']
