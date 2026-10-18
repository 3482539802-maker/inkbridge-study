"""Single bounded clock-file transfer worker, independent of raw indexing.

Requests coalesce by session. Active captures take priority; failed old jobs
back off without blocking the numeric worker or being marked complete.
"""
from collections import OrderedDict
import threading
import time


class ClockTransferWorker:
    def __init__(self,prefix,fetch,capacity=64):
        if type(capacity) is not int or not 1<=capacity<=128:raise ValueError('Invalid clock queue capacity')
        self.prefix,self.fetch,self.capacity=prefix,fetch,capacity
        self.lock=threading.Lock();self.wake=threading.Event();self.stopping=threading.Event()
        self.pending=OrderedDict();self.results=OrderedDict();self.inflight=None;self.thread=None

    def start(self):
        if self.thread is not None:raise RuntimeError('Clock worker already started')
        self.thread=threading.Thread(target=self.run,name='clock-file-transfer',daemon=True)
        self.thread.start()

    def request(self,session,path,active):
        with self.lock:
            if self.stopping.is_set():return False
            if session==self.inflight:return False
            if session in self.pending:
                old=self.pending[session]
                if old[0]!=path:raise ValueError('Clock destination changed')
                self.pending[session]=(path,active)
                return True
            result=self.results.get(session,{})
            # Active sessions can retry in five seconds even if an earlier
            # inactive request had entered a long backoff.
            wait=min(5 if active else 60,5*2**min(result.get('consecutive_failures',0),4))
            if time.monotonic()-result.get('finished_monotonic',-1e9)<wait:return False
            if len(self.pending)>=self.capacity:
                if not active:return False
                victim=next((s for s,(_,urgent) in self.pending.items() if not urgent),None)
                if victim is None:return False
                del self.pending[victim]
            self.pending[session]=(path,active);self.wake.set();return True

    def snapshot(self,session):
        with self.lock:
            result=dict(self.results.get(session,{}));result.pop('finished_monotonic',None)
            return dict(result,in_flight=self.inflight==session,queued=session in self.pending)

    def run(self):
        while not self.stopping.is_set():
            with self.lock:
                session=next((s for s,(_,active) in self.pending.items() if active),next(iter(self.pending),None))
                if session is not None:
                    path,_=self.pending.pop(session);self.inflight=session
                self.wake.clear()
            if session is None:
                self.wake.wait(.25);continue
            started=time.monotonic();error=None
            try:ok=bool(self.fetch(self.prefix,session,path,max_backfill=4))
            except Exception as problem:ok=False;error=type(problem).__name__
            with self.lock:
                previous=self.results.pop(session,{})
                self.results[session]={'last_transfer_succeeded':ok,'last_error_type':error,
                    'consecutive_failures':0 if ok else previous.get('consecutive_failures',0)+1,
                    'attempts':previous.get('attempts',0)+1,'last_attempt_finished_pc':time.time(),
                    'last_transfer_seconds':time.monotonic()-started,'finished_monotonic':time.monotonic()}
                while len(self.results)>self.capacity*2:self.results.popitem(last=False)
                self.inflight=None

    def close(self):
        self.stopping.set();self.wake.set()
        with self.lock:self.pending.clear()
        if self.thread:self.thread.join(timeout=55)
        if self.thread and self.thread.is_alive():raise RuntimeError('Clock transfer still stopping; files retained')
