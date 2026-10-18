"""Render explicit raw contacts for local recognition with provenance.

This is recorded input, not the saved/current note: edits are not replayed.
Reject unknown tools, partial coordinates and cross-page selections.
Live coordinate previews require a separate explicit opt-in and remain labelled.
"""
import hashlib
import json
import math
from pathlib import Path
import sqlite3


def render_contacts(directory,raw,session,start_sequences,scale=3.,allow_provisional=False,
                    allow_coordinate_preview=False):
    from PIL import Image,ImageDraw
    import numpy as np
    ids=list(start_sequences)
    if not 1<=len(ids)<=64 or len(set(ids))!=len(ids) or any(type(i) is not int or i<1 for i in ids):
        raise ValueError('Select 1..64 unique completed contacts')
    if not math.isfinite(scale) or not 1<=scale<=6:raise ValueError('Invalid ink raster scale')
    if type(allow_provisional) is not bool:raise ValueError('Invalid provisional input flag')
    if type(allow_coordinate_preview) is not bool or (allow_coordinate_preview and not allow_provisional):
        raise ValueError('Coordinate preview requires explicit live contact input')
    raw=Path(raw)
    def signature():
        stat=raw.stat();return stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns
    initial=signature();paths=[];sources=[];identity=None;total=0;preview_used=False
    db=sqlite3.connect((Path(directory)/'pipeline.sqlite').resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
    try:
        db.execute('BEGIN')
        with raw.open('rb') as source:
            for start in ids:
                events=db.execute("SELECT payload FROM edit_events WHERE json_extract(payload,'$.start_seq')=?",(start,)).fetchall()
                if len(events)!=1:raise ValueError('Missing or ambiguous contact')
                event=json.loads(events[0][0]);location=event.get('location_evidence',{})
                if allow_provisional and (event.get('provisional',True) or allow_coordinate_preview):
                    from edit_spatial_evidence import contact_location
                    from types import SimpleNamespace
                    # Read-only derived contact evidence. Coordinate preview is
                    # a separate opt-in; the canonical edit ledger stays intact.
                    location=contact_location(SimpleNamespace(db=db,session=session),event,
                                              allow_coordinate_preview=allow_coordinate_preview)
                if (event.get('session_id')!=session or (event.get('provisional',True) and not allow_provisional) or event['kind']!='pen_contact_pen' or
                    location.get('scope')!='within_bound_page_candidate'):
                    raise ValueError('Contact lacks final writing-tool/page evidence')
                key=(location.get('page_id'),location.get('source_pdf_sha256'),location.get('pdf_page_1based'))
                if None in key or (identity is not None and key!=identity):raise ValueError('Ink selection spans unknown/different pages')
                identity=key
                rows=db.execute('''SELECT r.seq,r.start,r.end,c.payload FROM raw_index r
                    LEFT JOIN coordinates c ON c.seq=r.seq WHERE r.seq BETWEEN ? AND ?
                    AND r.tool=? AND r.pointer=? AND r.win=? AND r.display=? ORDER BY r.seq LIMIT 8193''',
                    (start,event['end_seq'],event['tool_type'],event['pointer_id'],event['window_id'],event['display_id'])).fetchall()
                if (not rows or len(rows)!=event['points'] or len(rows)>8192 or rows[0][0]!=start or
                    rows[-1][0]!=event['end_seq']):raise ValueError('Incomplete ink contact')
                total+=len(rows)
                if total>16384:raise ValueError('Ink point budget exceeded')
                points=[];digest=hashlib.sha256();coordinate_digest=hashlib.sha256()
                for seq,lo,hi,payload in rows:
                    if payload is None or not 0<hi-lo<=8192:raise ValueError('Missing bounded coordinate/raw record')
                    source.seek(lo);line=source.read(hi-lo);p=json.loads(line);c=json.loads(payload)
                    if (p.get('session_id')!=session or p['seq']!=seq or c.get('session_id')!=session or
                        c.get('seq')!=seq or c.get('screen_xy')!=[p['x'],p['y']] or
                        (p.get('tool_type'),p.get('pointer_id'),p.get('window_id'),p.get('display_id'))!=
                        (event['tool_type'],event['pointer_id'],event['window_id'],event['display_id']) or
                        p.get('action') not in (1,2,3) or c.get('action_time_us')!=p.get('action_time') or
                        (seq==start and p['action']!=1) or (seq==event['end_seq'] and p['action']!=3) or
                        (c.get('provisional_live_revision',True) and not allow_coordinate_preview) or c.get('status') not in ('candidate','extrapolated_candidate') or
                        (c.get('page_uuid'),c.get('source_pdf_sha256'),c.get('pdf_page_1based'))!=identity):
                        raise ValueError('Ink coordinate/raw identity mismatch')
                    xy=c.get('pdf_xy')
                    if not isinstance(xy,list) or len(xy)!=2 or not all(type(v) in (int,float) and math.isfinite(v) for v in xy):
                        raise ValueError('Invalid ink coordinate')
                    preview_used=preview_used or c.get('provisional_live_revision',True)
                    points.append(xy);digest.update(line);coordinate_digest.update((payload+'\n').encode())
                paths.append(points);sources.append({'start_seq':start,'end_seq':event['end_seq'],
                    'point_count':len(rows),'selected_raw_records_sha256':digest.hexdigest(),
                    'selected_coordinate_records_sha256':coordinate_digest.hexdigest()})
    finally:db.close()
    # Live journals only append. Validate every selected byte range above and
    # reject identity changes/truncation; an unrelated appended contact is safe.
    after=signature()
    if (not allow_provisional and after!=initial) or (allow_provisional and (after[:2]!=initial[:2] or after[2]<initial[2])):
        raise ValueError('Raw changed during ink rendering')
    flat=[p for path in paths for p in path]
    box=[min(p[0] for p in flat),min(p[1] for p in flat),max(p[0] for p in flat),max(p[1] for p in flat)]
    width=max(32,math.ceil((box[2]-box[0])*scale)+24);height=max(32,math.ceil((box[3]-box[1])*scale)+24)
    if width*height>2_000_000 or max(width,height)>4096:raise ValueError('Ink region exceeds raster budget')
    image=Image.new('RGB',(width,height),'white');draw=ImageDraw.Draw(image)
    for path in paths:
        xy=[((p[0]-box[0])*scale+12,(p[1]-box[1])*scale+12) for p in path]
        if len(xy)==1:draw.ellipse((xy[0][0]-1,xy[0][1]-1,xy[0][0]+1,xy[0][1]+1),fill='black')
        else:draw.line(xy,fill='black',width=max(1,round(scale*.7)),joint='curve')
    return np.asarray(image),{'schema':1,'session_id':session,'contacts':sources,
        'page_uuid':identity[0],'source_pdf_sha256':identity[1],'pdf_page_1based':identity[2],
        'bounds_pdf_pt':box,'scale':scale,'padding_pixels':12,'raster_size':[width,height],
        'source':'live_raw_pen_contacts_with_candidate_page_coordinates' if allow_provisional else 'historical_raw_pen_contacts_with_candidate_page_coordinates',
        'saved_or_current_state_verified':False,'edit_operations_applied':False,
        'provisional_contact_input_allowed':allow_provisional,
        'coordinate_preview_input_allowed':allow_coordinate_preview,
        'coordinate_preview_used':preview_used,
        'semantic_grouping_verified':False,'pressure_rendering':False,'external_inference':False}


def render_screen_contacts(directory,raw,session,start_sequences):
    """Explicit short historical screen-space selection, with no page claim.

    Allows a whole raw contact whose page location is partially unknown. Every
    intervening record must belong to the selected pen contacts; finger, rubber,
    missing sequences, window changes and longer spans reject the selection.
    Absence of a recorded gesture does not prove the canvas remained stationary.
    """
    from PIL import Image,ImageDraw
    import numpy as np
    from ink_protocol import validate_point,PEN_TOOLS
    ids=sorted(start_sequences)
    if not 1<=len(ids)<=64 or len(set(ids))!=len(ids) or any(type(i) is not int or i<1 for i in ids):
        raise ValueError('Select 1..64 unique screen contacts')
    raw=Path(raw);initial=(raw.stat().st_size,raw.stat().st_mtime_ns)
    db=sqlite3.connect((Path(directory)/'pipeline.sqlite').resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
    events=[];paths=[];sources=[];identity=None;total=0;first_time=last_time=None;previous_seq=None
    try:
        db.execute('BEGIN')
        for start in ids:
            rows=db.execute("SELECT payload FROM edit_events WHERE json_extract(payload,'$.start_seq')=?",(start,)).fetchall()
            if len(rows)!=1:raise ValueError('Missing or ambiguous screen contact')
            e=json.loads(rows[0][0])
            if e.get('session_id')!=session or e.get('provisional',True) or e.get('kind')!='pen_contact_pen':
                raise ValueError('Screen input lacks final pen-tool evidence')
            if e['tool_type'] not in PEN_TOOLS:raise ValueError('Not a pen input')
            key=(e['tool_type'],e['pointer_id'],e['window_id'],e['display_id'])
            if identity is not None and key!=identity:raise ValueError('Screen input context changed')
            if events and e['start_seq']!=events[-1]['end_seq']+1:
                raise ValueError('Unselected or interrupted input between screen contacts')
            identity=key;events.append(e)
        with raw.open('rb') as source:
            for e in events:
                rows=db.execute('SELECT seq,start,end FROM raw_index WHERE seq BETWEEN ? AND ? ORDER BY seq LIMIT 8193',
                                (e['start_seq'],e['end_seq'])).fetchall()
                if not rows or len(rows)!=e['points'] or len(rows)>8192:raise ValueError('Incomplete screen contact')
                total+=len(rows)
                if total>16384:raise ValueError('Screen input point budget exceeded')
                path=[];digest=hashlib.sha256()
                for seq,lo,hi in rows:
                    if not 0<hi-lo<=8192:raise ValueError('Invalid raw record bound')
                    source.seek(lo);line=source.read(hi-lo);p=validate_point(json.loads(line))
                    if (p.get('session_id')!=session or p['seq']!=seq or
                        (p['tool_type'],p['pointer_id'],p['window_id'],p['display_id'])!=identity or
                        (previous_seq is not None and seq!=previous_seq+1) or p['action'] not in (1,2,3) or
                        p['action']!=(1 if seq==e['start_seq'] else 3 if seq==e['end_seq'] else 2)):
                        raise ValueError('Screen raw identity, continuity or contact boundary mismatch')
                    stamp=p['action_time']
                    if last_time is not None and stamp<last_time:raise ValueError('Screen raw time reversal')
                    if first_time is None:first_time=stamp
                    if stamp-first_time>30_000_000:raise ValueError('Screen selection exceeds 30 seconds')
                    last_time=stamp;previous_seq=seq
                    path.append([p['x'],p['y']]);digest.update(line)
                if rows[0][0]!=e['start_seq'] or rows[-1][0]!=e['end_seq']:raise ValueError('Screen contact boundary missing')
                paths.append(path);sources.append({'start_seq':e['start_seq'],'end_seq':e['end_seq'],
                    'point_count':len(rows),'selected_raw_records_sha256':digest.hexdigest()})
    finally:db.close()
    if (raw.stat().st_size,raw.stat().st_mtime_ns)!=initial:raise ValueError('Raw changed during screen input rendering')
    points=[p for path in paths for p in path]
    box=[min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)]
    size=(max(32,math.ceil(box[2]-box[0])+24),max(32,math.ceil(box[3]-box[1])+24))
    if size[0]*size[1]>2_000_000 or max(size)>4096:raise ValueError('Screen input raster budget exceeded')
    image=Image.new('RGB',size,'white');draw=ImageDraw.Draw(image)
    for path in paths:draw.line([(x-box[0]+12,y-box[1]+12) for x,y in path],fill='black',width=2,joint='curve')
    return np.asarray(image),{'schema':1,'session_id':session,'contacts':sources,
        'source':'explicit_historical_raw_screen_contacts','bounds_screen_px':box,'raster_size':list(size),
        'tool_type':identity[0],'pointer_id':identity[1],'window_id':identity[2],'display_id':identity[3],
        'action_time_range_us':[first_time,last_time],'selected_records_consecutive':True,
        'screen_to_page_transform_applied':False,'canvas_stationarity_verified':False,
        'page_identity_verified':False,'saved_or_current_state_verified':False,
        'edit_operations_applied':False,'semantic_grouping_verified':False,'external_inference':False}
