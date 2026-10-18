"""Constant-memory audit/comparison of stopped canonical raw journals."""
from collections import Counter
import hashlib
import json
from pathlib import Path

from ink_protocol import PEN_TOOLS,validate_point


def signature(path):
    s=Path(path).stat()
    return s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns


def audit(path,session):
    path=Path(path);initial=signature(path)
    digest=hashlib.sha256();count=pen=length=gaps=0
    previous=None;monotonic=True;same_session=True;complete=True
    actions=Counter();windows=Counter();tools=Counter()
    with path.open('rb') as source:
        while line:=source.readline(8193):
            if len(line)>8192:raise ValueError('Oversized raw record')
            complete &= line.endswith(b'\n')
            p=validate_point(json.loads(line))
            digest.update(line);count+=1;length+=len(line)
            same_session &= p.get('session_id')==session
            seq=p['seq']
            if previous is None:gaps+=max(0,seq-1)
            else:
                gaps+=max(0,seq-previous-1);monotonic &= seq>previous
            previous=seq;tools[p['tool_type']]+=1
            if p['tool_type'] in PEN_TOOLS:
                pen+=1;actions[p['action']]+=1;windows[p['window_id']]+=1
                if len(windows)>10000:raise ValueError('Too many raw window identities')
    if signature(path)!=initial:raise ValueError('Raw changed during stopped-session audit')
    return dict(byte_length=length,point_count=count,pen_point_count=pen,
                pen_actions=dict(actions),pen_window_ids=dict(windows),tool_counts=dict(tools),
                sequence_gaps=gaps,sequence_monotonic=monotonic,all_points_in_session=same_session,
                complete_jsonl_boundary=complete,last_sequence=previous or 0,sha256=digest.hexdigest())


def same_bytes(first,second):
    first,second=Path(first),Path(second)
    before=(signature(first),signature(second));equal=before[0][2]==before[1][2]
    if equal:
        with first.open('rb') as a,second.open('rb') as b:
            while True:
                x,y=a.read(1024*1024),b.read(1024*1024)
                if x!=y:equal=False;break
                if not x:break
    if (signature(first),signature(second))!=before:raise ValueError('Raw changed during byte comparison')
    return equal
