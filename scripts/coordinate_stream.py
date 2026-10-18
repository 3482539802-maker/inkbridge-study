"""Durable incremental raw index and bounded coordinate windows.

Canonical JSONL stays append-only and untouched. SQLite stores offsets, numeric
geometry, results and processing checkpoints, never screenshots. One worker
owns this database; acquisition can append geometry through a separate WAL
connection. Every result still uses the production alignment safety gates.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

from capture_clock_history import atomic_bytes
from atomic_output import replace_file
from coordinate_alignment import align, assess_clock
from coordinate_live import render_report, strokes_from_raw
from ink_protocol import PEN_TOOLS, validate_point
from native_frame_timing import bind
from clock_epochs import partition_clock,epoch_for,frames_in_epoch,public_epochs

OVERLAP_US=7_000_000
MAX_WINDOW_BYTES=4*1024*1024
VERSION=6


class GeometryWindowTooDense(ValueError):
    """Retry a smaller raw chunk without discarding any geometry barriers."""


class CoordinateStream:
    def __init__(self, directory, raw, session, stable_runs=True, page_margins=False):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.raw=Path(raw);self.session=session
        self.stable_runs,self.page_margins=stable_runs,page_margins
        self.db=sqlite3.connect(self.directory/'pipeline.sqlite',timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        from edit_observations import initialize as initialize_edit_observations
        initialize_edit_observations(self.db)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS raw_index(seq INTEGER PRIMARY KEY,t REAL NOT NULL,
                start INTEGER NOT NULL,end INTEGER NOT NULL,tool INTEGER NOT NULL,
                action INTEGER NOT NULL,display INTEGER,win INTEGER,pointer INTEGER);
            CREATE INDEX IF NOT EXISTS raw_time ON raw_index(t);
            CREATE TABLE IF NOT EXISTS contacts(start_seq INTEGER PRIMARY KEY,end_seq INTEGER,
                t REAL,display INTEGER,win INTEGER,pointer INTEGER);
            CREATE INDEX IF NOT EXISTS open_contacts ON contacts(end_seq);
            CREATE TABLE IF NOT EXISTS geometry(id INTEGER PRIMARY KEY,t REAL,payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS geometry_time ON geometry(t);
            CREATE TABLE IF NOT EXISTS coordinates(seq INTEGER PRIMARY KEY,payload TEXT NOT NULL,status TEXT,reason TEXT);
            CREATE INDEX IF NOT EXISTS coordinate_status ON coordinates(status,reason);
            CREATE TABLE IF NOT EXISTS counters(key TEXT PRIMARY KEY,n INTEGER NOT NULL);
            CREATE TRIGGER IF NOT EXISTS raw_count_insert AFTER INSERT ON raw_index BEGIN
                INSERT INTO counters VALUES ('events',1) ON CONFLICT(key) DO UPDATE SET n=n+1;
                INSERT INTO counters VALUES ('pens',NEW.tool IN (1,3,4,5) AND NEW.action!=0)
                    ON CONFLICT(key) DO UPDATE SET n=n+(NEW.tool IN (1,3,4,5) AND NEW.action!=0);
            END;
            CREATE TRIGGER IF NOT EXISTS coordinate_count_insert AFTER INSERT ON coordinates BEGIN
                INSERT INTO counters VALUES ('result:'||COALESCE(NEW.reason,NEW.status),1)
                    ON CONFLICT(key) DO UPDATE SET n=n+1;
            END;
            CREATE TRIGGER IF NOT EXISTS coordinate_count_delete AFTER DELETE ON coordinates BEGIN
                UPDATE counters SET n=n-1 WHERE key='result:'||COALESCE(OLD.reason,OLD.status);
            END;
        ''')
        # REPLACE must fire delete triggers as well, including preview revisions.
        self.db.execute('PRAGMA recursive_triggers=ON')
        if not self.get('counters_initialized',False):
            with self.db:
                self.db.execute('DELETE FROM counters')
                events,pens=self.db.execute('SELECT COUNT(*),SUM(tool IN (1,3,4,5) AND action!=0) FROM raw_index').fetchone()
                self.db.executemany('INSERT INTO counters VALUES (?,?)',[('events',events),('pens',pens or 0)])
                self.db.execute("INSERT INTO counters SELECT 'result:'||COALESCE(reason,status),COUNT(*) FROM coordinates GROUP BY reason,status")
                self.set('counters_initialized',True)
        identity=[str(self.raw.resolve()),session,stable_runs,page_margins]
        if self.get('identity',identity)!=identity:raise ValueError('Pipeline source/configuration changed')
        with self.db:self.set('identity',identity)

    def close(self):self.db.close()
    def get(self,key,default=None):
        row=self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set(self,key,value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,json.dumps(value)))

    def add_geometry(self,frame):
        observation=frame.get('observation',{})
        native=observation.get('native_frame',{})
        stamp=native.get('timestamp_raw')
        # Native stamps are independently calibrated before mapping, not here.
        t=stamp/1000 if stamp is not None else None
        payload=json.dumps(frame,allow_nan=False)
        if len(payload)>128*1024:raise ValueError('Oversized numeric geometry')
        with self.db:
            old=self.db.execute('SELECT payload FROM geometry WHERE id=?',(frame['index'],)).fetchone()
            if old and old[0]!=payload:raise ValueError('Conflicting immutable geometry')
            self.db.execute('INSERT OR IGNORE INTO geometry VALUES (?,?,?)',(frame['index'],t,payload))

    def ingest(self,budget=1024*1024):
        """Read only new complete records, at most one byte budget per call."""
        offset=self.get('raw_offset',0)
        if not self.raw.exists():return 0
        stat=self.raw.stat()
        identity=[stat.st_dev,stat.st_ino]
        if stat.st_size<offset or self.get('file_identity',identity)!=identity:
            raise ValueError('Canonical raw truncated or replaced')
        last=self.db.execute('SELECT seq,t,start,end FROM raw_index ORDER BY seq DESC LIMIT 1').fetchone()
        consumed=0
        with self.raw.open('rb') as source,self.db:
            if last:
                source.seek(last[2]);tail=source.read(last[3]-last[2])
                if hashlib.sha256(tail).hexdigest()!=self.get('tail_sha256'):
                    raise ValueError('Canonical acknowledged tail changed')
            source.seek(offset)
            previous=(last[0],last[1]) if last else None
            while consumed<budget:
                start=source.tell();line=source.readline(8193)
                if len(line)>8192:raise ValueError('Oversized raw record')
                if not line or not line.endswith(b'\n'):break
                p=validate_point(json.loads(line))
                if p.get('session_id')!=self.session:raise ValueError('Cross-session raw')
                seq,t=p['seq'],p['action_time']
                if previous and seq<=previous[0]:raise ValueError('Raw sequence reversal')
                if (previous and (seq!=previous[0]+1 or t<previous[1])) or (previous is None and seq!=1):
                    self.set('integrity_bad',True)
                end=source.tell()
                self.db.execute('INSERT INTO raw_index VALUES (?,?,?,?,?,?,?,?,?)',
                    (seq,t,start,end,p['tool_type'],p['action'],p['display_id'],p['window_id'],p['pointer_id']))
                if p['tool_type'] not in PEN_TOOLS:
                    key=p['display_id'],p['window_id'],p['pointer_id']
                    active=self.db.execute('SELECT start_seq FROM contacts WHERE end_seq IS NULL AND display=? AND win=? AND pointer=?',key).fetchone()
                    if not active:
                        self.db.execute('INSERT INTO contacts VALUES (?,NULL,?,?,?,?)',(seq,t,*key))
                    if p['action'] in (0,3):
                        self.db.execute('UPDATE contacts SET end_seq=? WHERE end_seq IS NULL AND display=? AND win=? AND pointer=?',(seq,*key))
                previous=seq,t;offset=end;consumed+=len(line)
                self.set('tail_sha256',hashlib.sha256(line).hexdigest())
            self.set('raw_offset',offset);self.set('file_identity',identity)
        return consumed

    def verify_clock(self,state,final=False):
        if state['session_id']!=self.session:raise ValueError('Clock session mismatch')
        assessment=assess_clock(state)
        observations={}
        for o in state.get('observations',[]):
            row=self.db.execute('SELECT t FROM raw_index WHERE seq=?',(o['seq'],)).fetchone()
            if row is None:
                if not final:return assessment,None  # ACK may trail the native clock.
                assessment=dict(assessment,usable_candidate=False,reason='raw_sequence_clock_or_observation_mismatch')
                continue
            if row[0]!=o['action_time_us']:
                assessment=dict(assessment,usable_candidate=False,reason='raw_sequence_clock_or_observation_mismatch')
            observations[o['seq']]=row[0]
        if self.get('integrity_bad',False):
            assessment=dict(assessment,usable_candidate=False,reason='raw_sequence_clock_or_observation_mismatch')
        return assessment,observations

    def frames_for(self,low,high):
        first=self.db.execute('SELECT id FROM geometry WHERE t>=? ORDER BY id LIMIT 1',(low,)).fetchone()
        last=self.db.execute('SELECT id FROM geometry WHERE t<=? ORDER BY id DESC LIMIT 1',(high,)).fetchone()
        if not first or not last:return []
        # Include surrounding invalid observations as barriers and enough real
        # timestamps for independent native-clock calibration at both ends.
        lower=max(0,min(first[0],last[0])-16);upper=max(first[0],last[0])+16
        rows=self.db.execute('SELECT payload FROM geometry WHERE id BETWEEN ? AND ? ORDER BY id LIMIT 161',(lower,upper)).fetchall()
        if len(rows)>160:raise GeometryWindowTooDense('Geometry window too dense')
        return [json.loads(row[0]) for row in rows]

    def step(self,state,final=False,limit=2048,preview=False):
        """Commit one bounded target chunk with raw/geometry overlap on both sides."""
        assessment,observations=self.verify_clock(state,final)
        if observations is None:return False
        usable=assessment['usable_candidate']
        epochs=(partition_clock(state) if final and not self.get('integrity_bad',False) and
                assessment.get('reason')!='raw_sequence_clock_or_observation_mismatch' else None)
        # Finalization is a single streaming recomputation with final clocks;
        # live results are provisional. An unusable clock revokes old results.
        final_key=hashlib.sha256(json.dumps([VERSION,state],sort_keys=True).encode()).hexdigest() if final else None
        with self.db:
            if final and self.get('final_clock')!=final_key:
                self.db.execute('DELETE FROM coordinates');self.set('processed_seq',0)
                self.set('final_clock',final_key);self.set('final_complete',False)
            if not usable and self.get('last_usable',False):
                self.db.execute('DELETE FROM coordinates');self.set('processed_seq',0)
            self.set('last_usable',usable);self.set('clock_assessment',assessment)
            if epochs:self.set('clock_assessment',dict(assessment,final_epochs=public_epochs(epochs),
                epoch_recovery_scope='final_replay_only',whole_session_single_clock_usable=False))
        if not usable and not final:return False
        last_raw=self.db.execute('SELECT MAX(t) FROM raw_index').fetchone()[0]
        if last_raw is None:
            if final:
                with self.db:self.set('final_complete',True)
            return False
        # Missing/rejected geometry must produce unknown locations rather than
        # stall the numeric cursor forever (e.g. a different Notes document).
        # Live results remain revisable; final processing uses all saved frames.
        watermark=last_raw if final or preview else min(last_raw,
            assessment.get('action_time_range_us',[-1,-1])[1])-OVERLAP_US
        rows=self.db.execute('SELECT seq,t FROM raw_index WHERE seq>? AND t<=? ORDER BY seq LIMIT ?',
                            (self.get('processed_seq',0),watermark,limit)).fetchall()
        if not rows:
            if final:
                with self.db:self.set('final_complete',True)
            return False
        # A sparse day can have hours between adjacent points. Never turn that
        # gap into a huge geometry query or lose responsiveness during replay.
        if not self.get('integrity_bad',False):
            rows=[r for r in rows if r[1]<=rows[0][1]+20_000_000]
        epoch=None;mapping_state=state;mapping_usable=usable
        if epochs:
            epoch=epoch_for(epochs,rows[0][1])
            if epoch:
                rows=[r for r in rows if r[1]<=epoch['end_us']]
                mapping_state=epoch['state'];mapping_usable=epoch['assessment']['usable_candidate']
            else:
                following=next((e['start_us'] for e in epochs if e['start_us']>rows[0][1]),float('inf'))
                rows=[r for r in rows if r[1]<following]
                mapping_state=dict(state,anchors=[],observations=[]);mapping_usable=False
        low,high=min(r[1] for r in rows)-OVERLAP_US,max(r[1] for r in rows)+OVERLAP_US
        bounds=self.db.execute('SELECT MIN(seq),MAX(seq),MIN(start),MAX(end) FROM raw_index WHERE t BETWEEN ? AND ?',(low,high)).fetchone()
        first,last,start,end=bounds
        if end-start>MAX_WINDOW_BYTES:
            if limit>1:return self.step(state,final, max(1,limit//2),preview)
            raise ValueError('Raw time window exceeds bounded memory; timestamps/rate require investigation')
        previous=self.db.execute('SELECT seq,t FROM raw_index WHERE seq<? ORDER BY seq DESC LIMIT 1',(first,)).fetchone()
        contact_boundary=first
        if epoch:
            epoch_first=self.db.execute('SELECT MIN(seq) FROM raw_index WHERE t>=?',(epoch['start_us'],)).fetchone()[0]
            contact_boundary=max(first,epoch_first or first)
        contacts=[([d,w,p],t) for t,d,w,p in self.db.execute(
            'SELECT t,display,win,pointer FROM contacts WHERE start_seq<? AND (end_seq IS NULL OR end_seq>=?)',
            (contact_boundary,contact_boundary))]
        context={'previous':previous,'contacts':contacts,'observations':observations,
                 'integrity_bad':not mapping_usable or self.get('integrity_bad',False)}
        try:
            frames=self.frames_for(low,high)
        except GeometryWindowTooDense:
            if limit>1:return self.step(state,final,max(1,limit//2),preview)
            raise
        if epoch:frames=frames_in_epoch(frames,epoch)
        if any(f.get('observation',{}).get('native_frame') for f in frames):frames,_=bind(frames,mapping_state)
        with self.raw.open('rb') as source:
            source.seek(start);payload=source.read(end-start)
        if len(payload)!=end-start:raise ValueError('Raw changed while reading indexed window')
        with tempfile.TemporaryDirectory(prefix='window-',dir=self.directory) as folder:
            raw=Path(folder)/'raw.jsonl';out=Path(folder)/'points.jsonl'
            raw.write_bytes(payload)
            align(raw,mapping_state,frames,out,self.stable_runs,self.page_margins,prefix_context=context)
            with out.open(encoding='utf-8') as results,self.db:
                for line in results:
                    p=json.loads(line)
                    if rows[0][0]<=p['seq']<=rows[-1][0]:
                        p['provisional_live_revision']=not final
                        if epochs:
                            p['clock_epoch']=epoch['index'] if epoch else None
                            p['clock_epoch_range_us']=[epoch['start_us'],epoch['end_us']] if epoch else None
                            if not epoch:p['reason']='clock_epoch_transition_or_insufficient_evidence'
                        line=json.dumps(p,allow_nan=False)
                        self.db.execute('INSERT OR REPLACE INTO coordinates VALUES (?,?,?,?)',
                            (p['seq'],line.strip(),p['status'],p['reason']))
                if not preview:self.set('processed_seq',rows[-1][0])
                self.set('last_window_bytes',end-start)
        return True

    def summary(self):
        counters=dict(self.db.execute('SELECT key,n FROM counters'))
        counts={k.removeprefix('result:'):n for k,n in counters.items() if k.startswith('result:') and n>0}
        events,pens=counters.get('events',0),counters.get('pens',0)
        pending=self.db.execute('SELECT COUNT(*) FROM coordinates WHERE seq>?',(self.get('processed_seq',0),)).fetchone()[0]
        return dict(session_id=self.session,raw_indexed_bytes=self.get('raw_offset',0),
            raw_bytes=self.raw.stat().st_size if self.raw.exists() else 0,raw_events=events,pen_points=pens or 0,
            processed_seq=self.get('processed_seq',0),processed_pen_points=sum(counts.values()),results=counts,
            waiting_pen_points=(pens or 0)-sum(counts.values()),clock=self.get('clock_assessment'),
            provisional_tail_pen_points=pending,
            final_complete=self.get('final_complete',False),last_window_bytes=self.get('last_window_bytes',0),
            maximum_window_bytes=MAX_WINDOW_BYTES,raw_modified=False,tablet_ui_actions=[])

    def publish(self):
        summary=self.summary()
        atomic_bytes(self.directory/'summary.json',json.dumps(summary,ensure_ascii=False,indent=2).encode())
        # UI shows a bounded recent view; full history remains queryable on disk.
        records=self.db.execute('SELECT seq,start,end FROM raw_index ORDER BY seq DESC LIMIT 2048').fetchall()
        if records:
            first=records[-1][0]
            with self.raw.open('rb') as source:
                source.seek(records[-1][1]);payload=source.read(records[0][2]-records[-1][1])
            locations={seq:json.loads(text) for seq,text in self.db.execute('SELECT seq,payload FROM coordinates WHERE seq>=?',(first,))}
            strokes=strokes_from_raw(payload,locations)
            html=render_report(strokes,locations,finalized=summary['final_complete'])
            html=html.replace('<h1>逐笔页面坐标</h1>',
                '<h1>最近笔迹（最多2048个原始事件）</h1><p>完整历史保存在磁盘数据库；此窗口不代表整次覆盖率。</p>'+
                f'<p>累计笔点：{summary["pen_points"]}；已处理：{summary["processed_pen_points"]}；等待处理：{summary["waiting_pen_points"]}。</p>')
            atomic_bytes(self.directory/'report.html',html.encode())
        return summary

    def export(self):
        """Stream full final coordinates, without reading all history into RAM."""
        if not self.get('final_complete',False):raise ValueError('Final processing incomplete')
        import os
        target=self.directory/'points.jsonl';temporary=target.with_suffix('.jsonl.tmp')
        with temporary.open('w',encoding='utf-8',newline='\n') as out:
            for (payload,) in self.db.execute('SELECT payload FROM coordinates ORDER BY seq'):
                out.write(payload+'\n')
            out.flush();os.fsync(out.fileno())
        replace_file(temporary,target)
