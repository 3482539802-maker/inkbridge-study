"""Independent, timed Notes tool observations. No images or tablet UI actions."""
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time

from atomic_output import publish_status
from notes_diagnostics import notes_window
from visual_calibration import TabletFrames

MARKER='INKBRIDGE_EDIT_BOUNDARY_4df91'


def sampled_pages(context):
    """Visible page set, never a selection of the page receiving the pen."""
    pages=context.get('page_layers',[])
    ids=[p.get('page_id') for p in pages]
    if not 1<=len(ids)<=12 or any(not isinstance(p,str) or not p for p in ids):return None
    if len(set(ids))!=len(ids):return None
    return sorted(ids)


def usable_tool_sample(row):
    if row.get('valid'):return True
    # Schema 1 stored context only AFTER both matching, focused window checks
    # and tree PID validation. Its last gate rejected every multi-page view.
    # Recover that specific gate offline, retaining the original invalid row.
    context=row.get('context',{});pages=sampled_pages(context)
    span=row.get('device_wall_interval_ms')
    return bool(row.get('schema')==1 and row.get('reason')=='unstable_context' and
        row.get('atomic_observation') is False and row.get('screenshots')==0 and
        row.get('window',{}).get('focused') and context.get('active_tab_marker_unique') and
        pages and len(pages)>1 and isinstance(span,list) and len(span)==2 and
        all(isinstance(v,(int,float)) and not isinstance(v,bool) for v in span) and 0<=span[1]-span[0]<=1000)


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS edit_observations(
        id INTEGER PRIMARY KEY,lo REAL,hi REAL,payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS edit_observation_time ON edit_observations(lo);''')


def save(db,row):
    payload=json.dumps(row,ensure_ascii=False,allow_nan=False)
    if len(payload.encode('utf-8'))>48*1024:raise ValueError('Oversized tool observation')
    interval=row.get('device_wall_interval_ms',[None,None])
    with db:db.execute('INSERT INTO edit_observations(lo,hi,payload) VALUES (?,?,?)',(*interval,payload))


class ToolReader:
    def __init__(self,prefix):
        self.prefix=prefix;self.hint=None

    def command(self,command):
        p=subprocess.run(self.prefix+['shell',command],capture_output=True,encoding='utf-8',
                         errors='replace',timeout=6)
        if p.returncode or '[Fail]' in p.stdout:raise ValueError('Tool diagnostic unavailable')
        if len(p.stdout)>2*1024*1024:raise ValueError('Oversized tool diagnostic')
        return p.stdout

    def read(self):
        if self.hint is None:
            self.hint=notes_window(self.command('hidumper -s WindowManagerService -a -a'))
        # Device wall time encloses BOTH focus checks and the tree read. The PC
        # receive time is for health reporting only, never raw association.
        commands=['date +%s%N','hidumper -s WindowManagerService -a -a',
                  f'hidumper -s WindowManagerService -a "-w {int(self.hint["window_id"])} -allInfo"',
                  'hidumper -s WindowManagerService -a -a','date +%s%N']
        raw=self.command(('\necho '+MARKER+'\n').join(commands))
        return self.parse(raw)

    def parse(self,raw):
        parts=raw.split(MARKER)
        if len(parts)!=5:raise ValueError('Malformed tool time envelope')
        times=[p.strip() for p in (parts[0],parts[-1])]
        if any(re.fullmatch(r'\d{19}',t) is None for t in times):raise ValueError('Unsupported device date precision')
        lo,hi=[int(t)/1e6 for t in times]
        row={'schema':2,'device_wall_interval_ms':[lo,hi],'observed_at_pc':time.time(),
             'valid':False,'reason':'unstable_context','atomic_observation':False,'screenshots':0}
        if not 0<=hi-lo<=1000:return dict(row,reason='tool_observation_clock_or_duration')
        try:
            before=notes_window(parts[1]);after=notes_window(parts[3]);hint=self.hint
            self.hint=after
            if before!=after or not after['focused'] or (hint and before['window_id']!=hint['window_id']):return row
            context=TabletFrames.parse_context(parts[2],before)
            row.update(window=after,context=context)
            if not context['active_tab_marker_unique'] or not sampled_pages(context):return row
            row.update(valid=True,reason=None)
        except ValueError as error:row['reason']=str(error)[:120]
        return row


class SharedContextReader:
    """One diagnostic tree read serves geometry and timed tool evidence.

    The NEXT fresh window check closes the previous tree's tool envelope.
    This uses one window/tree query per boundary, never an extra tree poll.
    Geometry still receives the newly read context and its original full
    pre/post gates; the pending tool row never authorizes a geometry frame.
    """
    marker='INKBRIDGE_SHARED_CONTEXT_afd71'

    def __init__(self,directory,prefix,session,is_active=None):
        self.directory=Path(directory);self.reader=ToolReader(prefix);self.session=session
        self.db=None;self.closed=False;self.pending=None;self.is_active=is_active;self.last_diagnostics={}

    def read(self):
        if self.reader.hint is None:
            self.reader.hint=notes_window(self.reader.command('hidumper -s WindowManagerService -a -a'))
        command=('date +%s%N\nhidumper -s WindowManagerService -a -a\n'
            f'echo {self.marker}\ndate +%s%N\n'
            f'hidumper -s WindowManagerService -a "-w {int(self.reader.hint["window_id"])} -allInfo"\n'
            f'echo {self.marker}\ndate +%s%N')
        started=time.monotonic();raw=self.reader.command(command)
        transferred=time.monotonic();sample=self.parse(raw)
        self.last_diagnostics=dict(device_window_check_ms=sample['window_end']-sample['lo'],
            device_tree_ms=sample['hi']-sample['window_end'],
            transport_ms=(transferred-started)*1000,pc_parse_ms=(time.monotonic()-transferred)*1000,
            reply_utf8_bytes=len(raw.encode('utf-8')))
        return sample

    def parse(self,raw):
        parts=raw.split(self.marker)
        if len(parts)!=3:raise ValueError('Malformed shared context envelope')
        stamps=[re.findall(r'^\d{19}\s*$',part,re.M) for part in parts]
        if any(len(s)!=1 for s in stamps):raise ValueError('Ambiguous shared context clock')
        lo,window_end,hi=[int(s[0])/1e6 for s in stamps]
        if not lo<=window_end<=hi:raise ValueError('Shared context clock reversal')
        window=notes_window(parts[0]);hint=self.reader.hint;self.reader.hint=window
        if hint and any(hint[k]!=window[k] for k in ('window_id','pid','display_id')):
            raise ValueError('Notes window changed during shared context read')
        context=TabletFrames.parse_context(parts[1],window) if window['focused'] else None
        return dict(window=window,context=context,lo=lo,window_end=window_end,hi=hi)

    def _save(self,row):
        row.update(session_id=self.session,source='shared_geometry_context_read')
        save(self.db,row)

    def _finish_previous(self,current):
        previous=self.pending
        if previous is None:return
        span=[previous['lo'],current['window_end']]
        row=dict(schema=2,valid=False,reason='unstable_context',device_wall_interval_ms=span,
            observed_at_pc=time.time(),atomic_observation=False,screenshots=0,
            focus_after_source='next_fresh_context_window_check')
        context=previous['context'];window=previous['window']
        if not previous['hi']<=current['lo'] or not 0<=span[1]-span[0]<=1000:
            row['reason']='tool_observation_clock_or_duration'
        elif window==current['window'] and window['focused'] and context and context['active_tab_marker_unique'] and sampled_pages(context):
            row.update(window=window,context=context,valid=True,reason=None)
        self._save(row)
        def identity(sample):
            c=sample['context'] or {}
            edit=c.get('editing') or {}
            return (sample['window'],c.get('document_title_candidate'),c.get('active_tab_marker_unique'),
                sampled_pages(c),edit.get('selected_tool'),edit.get('eraser_mode'),edit.get('eraser_mode_style_candidate'))
        if identity(previous)!=identity(current):
            # The second tree is not reused as a tool sample, but an observed
            # pen->lasso->pen excursion MUST still poison the interval.
            self._save({'valid':False,'reason':'shared_context_observed_tool_or_identity_transition'})
        return row

    def window_context(self):
        if self.closed:raise RuntimeError('Shared context reader closed')
        if self.db is None:
            # Close is allowed from the supervisor AFTER the owner joins.
            self.db=sqlite3.connect(self.directory/'pipeline.sqlite',timeout=5,check_same_thread=False)
            initialize(self.db)
            self._save({'valid':False,'reason':'tool_observer_started'})
        started=time.monotonic()
        self.last_diagnostics={}
        try:
            if self.is_active and not self.is_active(self.session):raise RuntimeError('User capture stopped')
            current=self.read()
            if self.is_active and not self.is_active(self.session):raise RuntimeError('User capture stopped')
        except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired) as error:
            self.pending=None
            self._save({'valid':False,'reason':type(error).__name__+': '+str(error)[:120]})
            raise RuntimeError('Shared context unavailable') from error
        row=self._finish_previous(current)
        # Pair disjoint reads (0->1, 2->3, ...). Reusing read 1 as the
        # next tree would overlap tool envelopes; existing chronology checks
        # intentionally reject such overlap. No extra focus/tree call needed.
        self.pending=current if row is None else None
        publish_status(self.directory/'edit-observation-status.json',{
            'stage':'observing','last_valid':row['valid'] if row else False,
            'reason':row.get('reason') if row else 'waiting_next_focus_check',
            'sample_ms':round((time.monotonic()-started)*1000,2),
            'source':'shared_geometry_context_read','updated_at_pc':time.time(),'screenshots':0})
        return current['window'],current['context']

    def close(self):
        # The producer must be joined first, including failure cleanup.
        self.closed=True;self.pending=None
        if self.db is not None:
            self._save({'valid':False,'reason':'tool_observer_stopped'})
            self.db.close();self.db=None
        publish_status(self.directory/'edit-observation-status.json',{
            'stage':'stopped','source':'shared_geometry_context_read',
            'updated_at_pc':time.time(),'screenshots':0})


class EditObservationWorker:
    def __init__(self,directory,prefix,session,is_active,interval=2.):
        self.directory=Path(directory);self.reader=ToolReader(prefix);self.session=session
        self.is_active=is_active;self.interval=interval;self.stop=threading.Event()
        self.thread=threading.Thread(target=self.run,name='notes-tools',daemon=True)
        self.thread.start()

    def run(self):
        db=sqlite3.connect(self.directory/'pipeline.sqlite',timeout=5)
        initialize(db)
        failure=None
        try:
            # A durable barrier prevents matching across a worker restart.
            save(db,{'valid':False,'reason':'tool_observer_started','session_id':self.session})
            while not self.stop.is_set():
                started=time.monotonic()
                try:
                    if not self.is_active(self.session):break
                    row=self.reader.read()
                    if self.stop.is_set() or not self.is_active(self.session):break
                except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired) as error:
                    row={'valid':False,'reason':type(error).__name__+': '+str(error)[:120]}
                row.update(session_id=self.session)
                save(db,row)
                publish_status(self.directory/'edit-observation-status.json',{
                    'stage':'observing','last_valid':row['valid'],'reason':row.get('reason'),
                    'sample_ms':round((time.monotonic()-started)*1000,2),
                    'updated_at_pc':time.time(),'screenshots':0})
                self.stop.wait(max(0,self.interval-(time.monotonic()-started)))
        except Exception as error:
            failure=type(error).__name__+': '+str(error)[:160]
        finally:
            save(db,{'valid':False,'reason':'tool_observer_stopped','session_id':self.session})
            db.close()
            publish_status(self.directory/'edit-observation-status.json',{
                'stage':'failed' if failure else 'stopped','error':failure,
                'updated_at_pc':time.time(),'screenshots':0})

    def close(self):
        self.stop.set();self.thread.join(timeout=20)
        if self.thread.is_alive():raise RuntimeError('Tool observer did not finish stopping')


def contact_context(store,event,assessment):
    """Returns (channel_present, context). An invalid channel must not fall back."""
    if not store.db.execute('SELECT 1 FROM edit_observations LIMIT 1').fetchone():return False,None
    if not assessment.get('usable_candidate'):return True,None
    if event['end_us']-event['start_us']>20_000_000:return True,None
    low,high=assessment['action_time_range_us']
    if not low<=event['start_us']<=event['end_us']<=high:return True,None
    offset=assessment['offset_ms'];error=assessment['uncertainty_ms']
    start=event['start_us']/1000+offset;end=event['end_us']/1000+offset
    ends=store.db.execute('SELECT MIN(id),MAX(id) FROM edit_observations WHERE lo BETWEEN ? AND ?',
                          (start-8000,end+8000)).fetchone()
    if ends[0] is None:return True,None
    rows=store.db.execute('SELECT id,payload FROM edit_observations WHERE id BETWEEN ? AND ? ORDER BY id LIMIT 513',
                          (max(0,ends[0]-1),ends[1]+1)).fetchall()
    if len(rows)>512:return True,None
    items=[]
    for identity,payload in rows:
        row=json.loads(payload);context=row.get('context',{});window=row.get('window',{})
        span=row.get('device_wall_interval_ms');pages=sampled_pages(context)
        valid=(usable_tool_sample(row) and row.get('session_id')==store.session and span and
               low/1000+offset<=span[0]-error<=span[1]+error<=high/1000+offset and
               window.get('focused') and window.get('window_id')==event['window_id'] and
               window.get('display_id')==event['display_id'] and context.get('active_tab_marker_unique') and pages)
        edit=context.get('editing',{})
        item={'id':identity,'valid':bool(valid),'span':span}
        if valid:
            item.update(title=context['document_title_candidate'],page_ids=pages,
                page_id=pages[0] if len(pages)==1 else None,legacy_multipage_recovered=not row.get('valid'),
                tool=edit.get('selected_tool'),eraser_mode=edit.get('eraser_mode') or edit.get('eraser_mode_style_candidate'),
                eraser_mode_evidence='explicit_selected' if edit.get('eraser_mode') else edit.get('eraser_mode_style_evidence'),
                lasso_menu_available_actions=edit.get('lasso_menu_available_actions',[]))
        items.append(item)
    before=[i for i in items if i['valid'] and start-3000<=i['span'][1]+error<=start]
    after=[i for i in items if i['valid'] and end<=i['span'][0]-error<=end+3000]
    if not before or not after:return True,None
    a=before[-1];b=after[0];between=[i for i in items if a['id']<=i['id']<=b['id']]
    keys=('title','page_ids','tool')
    if any(not i['valid'] or any(i[k]!=a[k] for k in keys) for i in between):
        return True,transition_contact_context(store,event,items,start,end,error)
    if any(y['span'][0]-x['span'][1]>4000 or y['span'][0]<x['span'][1]
           for x,y in zip(between,between[1:])):return True,None
    modes={i['eraser_mode'] for i in between}
    # Losing a popup's mode marker must not erase evidence of the selected
    # eraser tool. Keep the operation type unknown when mode samples differ.
    mode=a['eraser_mode'] if len(modes)==1 else None
    mode_hint=mode_bracket_hint(items,start,end,error) if a['tool']=='eraser' and mode is None else None
    return True,{k:a[k] for k in (*keys,'page_id','lasso_menu_available_actions')} | {
        'page_assignment':'single_visible_page_candidate' if a['page_id'] else 'multiple_visible_pages_unassigned',
        'legacy_multipage_samples_recovered':any(i['legacy_multipage_recovered'] for i in between),
        'eraser_mode':mode,'eraser_mode_evidence':a['eraser_mode_evidence'] if mode else None,
        'eraser_mode_bracket_hint':mode_hint,
        'tool_observation_ids':[a['id'],b['id']],
        'evidence':'independent_device_timed_ui_samples_not_atomic_edit_subscription',
        'contact_within_sampled_page_bounds':None}


def transition_contact_context(store,event,items,start,end,error):
    """A sampled tool change can veto writing even without a before-tool bracket.

    Require a full read inside the contact plus a matching read after it, with
    no intervening invalid sample/context/other input. Only non-writing tools
    qualify. This is a candidate for the contact, not proof of the start tool.
    """
    inside=[i for i in items if i['valid'] and start<i['span'][0]-error and i['span'][1]+error<end]
    after=[i for i in items if i['valid'] and end<=i['span'][0]-error<=end+3000]
    before=[i for i in items if i['valid'] and start-3000<=i['span'][1]+error<=start]
    if not inside or not after or not before:return None
    first,last=inside[0],after[0];prior=before[-1]
    tool=first['tool']
    if tool not in ('lasso','eraser','laser') or prior['tool']==tool:return None
    between=[i for i in items if prior['id']<=i['id']<=last['id']]
    if any(not i['valid'] or any(i[k]!=first[k] for k in ('title','page_ids')) or
           (i['id']>=first['id'] and i['tool']!=tool) for i in between):return None
    if any(y['span'][0]-x['span'][1]>4000 or y['span'][0]<x['span'][1]
           for x,y in zip(between,between[1:])):return None
    bad=store.db.execute('''SELECT 1 FROM raw_index WHERE seq BETWEEN ? AND ?
        AND (tool NOT IN (1,3,4,5) OR action=0 OR win!=? OR display!=?) LIMIT 1''',
        (event['start_seq'],event['end_seq'],event['window_id'],event['display_id'])).fetchone()
    if bad:return None
    return {k:first[k] for k in ('title','page_ids','page_id','tool','lasso_menu_available_actions')} | {
        'eraser_mode':None,'eraser_mode_evidence':None,'eraser_mode_bracket_hint':None,
        'tool_observation_ids':[prior['id'],first['id'],last['id']],
        'evidence':'nonwriting_tool_observed_during_and_after_contact_transition',
        'tool_at_contact_start_verified':False,'full_contact_tool_verified':False,
        'page_assignment':'sampled_visible_pages_only','contact_within_sampled_page_bounds':None}


def mode_bracket_hint(items,start,end,error):
    """Nearby agreeing menu samples are a hint; never fill the observed mode."""
    before=[i for i in items if i['valid'] and i['tool']=='eraser' and i['eraser_mode'] and
            start-8000<=i['span'][1]+error<=start]
    after=[i for i in items if i['valid'] and i['tool']=='eraser' and i['eraser_mode'] and
           end<=i['span'][0]-error<=end+8000]
    if not before or not after:return None
    a=before[-1];b=after[0]
    if a['eraser_mode']!=b['eraser_mode'] or b['span'][1]-a['span'][0]>12000:return None
    between=[i for i in items if a['id']<=i['id']<=b['id']]
    if any(not i['valid'] or any(i[k]!=a[k] for k in ('tool','title','page_ids')) or
           i['eraser_mode'] not in (None,a['eraser_mode']) for i in between):return None
    if any(y['span'][0]-x['span'][1]>4000 or y['span'][0]<x['span'][1]
           for x,y in zip(between,between[1:])):return None
    return {'mode':a['eraser_mode'],'observation_ids':[a['id'],b['id']],
            'sample_evidence':[a['eraser_mode_evidence'],b['eraser_mode_evidence']],
            'status':'hint_only','unobserved_mode_switch_possible':True,
            'used_to_apply_erasure':False}
