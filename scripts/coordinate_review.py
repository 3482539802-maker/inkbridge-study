"""Read-only historical ink on hash-verified original PDF pages, not screenshots.

Creates a local context viewer alongside an existing immutable review packet.
Never fills unknown positions or replays editing commands into a saved note.
"""
import argparse
import base64
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re

from atomic_output import atomic_bytes
from coordinate_live import strokes_from_raw
from ink_protocol import PEN_TOOLS
from recognition_review import digest, read_json, refresh_review

LATEST='d9b6889a-eaf3-4284-a21d-6b526cbbcbbc'


def split_runs(points):
    """Break at unknown, other page/status, missing seq, and contact boundaries."""
    runs=[];run=None;last=None
    for p in points:
        if p.get('pdf_xy') is None:
            run=None;last=None;continue
        key=(p['source_pdf_sha256'],p['pdf_page_1based'],p['page_uuid'],p['status'])
        if run is None or key!=last[0] or p['seq']!=last[1]+1:
            run={'page_key':':'.join(map(str,key[:3])),'status':key[3],'xy':[],'sequences':[]}
            runs.append(run)
        run['xy'].append([round(v,4) for v in p['pdf_xy']]);run['sequences'].append(p['seq'])
        last=(key,p['seq'])
    return runs


def build_session(root,session,pdf_sources,pages):
    directory=root/'output/pipeline/cmc'/('session-'+session)
    audit=read_json(directory/'final-audit.json')
    job=read_json(directory/'job.json')
    raw=root/'output/ink/sessions'/(session+'.jsonl');export=directory/'points.jsonl'
    if not job.get('final_complete') or not job.get('geometry_closed'):
        raise ValueError('Viewer accepts only closed, finalized historical sessions')
    if raw.stat().st_size>16*1024*1024 or export.stat().st_size>128*1024*1024:
        raise ValueError('Historical viewer size budget exceeded')
    if (digest(raw)!=audit['raw_sha256'] or digest(export)!=audit['coordinate_export_sha256'] or
            audit['session_id']!=session or not audit['raw_index_coordinates_export_equal']):
        raise ValueError('Final raw/export evidence mismatch')
    raw_bytes=raw.read_bytes();records={}
    for line in raw_bytes.splitlines():
        p=json.loads(line)
        if p.get('type')=='point' and p['tool_type'] in PEN_TOOLS:records[p['seq']]=p
    locations={}
    with export.open(encoding='utf-8') as stream:
        for line in stream:
            c=json.loads(line);p=records.get(c['seq'])
            if (not p or c['seq'] in locations or c['session_id']!=session or p['session_id']!=session or
                    c['screen_xy']!=[p['x'],p['y']] or c['action_time_us']!=p['action_time'] or
                    any(c[k]!=p[k] for k in ('pointer_id','window_id','display_id')) or
                    c.get('provisional_live_revision',True)):
                raise ValueError('Coordinate/raw point mismatch or provisional evidence')
            xy=c.get('pdf_xy')
            if xy is not None:
                if c['status'] not in ('candidate','extrapolated_candidate','motion_candidate') or not all(math.isfinite(v) for v in xy):
                    raise ValueError('Invalid located candidate')
                sha=c['source_pdf_sha256'];number=c['pdf_page_1based']
                if sha not in pdf_sources:raise ValueError('No verified original for located point')
                key=f'{sha}:{number}:{c["page_uuid"]}'
                if key not in pages:
                    import fitz
                    with fitz.open(pdf_sources[sha]) as document:
                        if not 1<=number<=len(document):raise ValueError('Invalid original page')
                        page=document[number-1];size=[page.rect.width,page.rect.height]
                        if any(abs(a-b)>.01 for a,b in zip(size,c['pdf_size_pt'])):
                            raise ValueError('Original page geometry mismatch')
                        pix=page.get_pixmap(matrix=fitz.Matrix(1.7,1.7),alpha=False)
                        image='data:image/jpeg;base64,'+base64.b64encode(pix.tobytes('jpeg',jpg_quality=87)).decode()
                    pages[key]={'key':key,'pdf_page':number,'size':size,'image':image,
                        'pdf_sha256':sha,'page_uuid':c['page_uuid'],
                        'document':'数学竞赛解析教程 · 上册' if 'math-tutorial' in str(pdf_sources[sha]) else 'CMC 2025 试题'}
            elif c['status']!='unresolved':raise ValueError('Unknown point status is inconsistent')
            locations[c['seq']]=c
    if set(locations)!=set(records) or len(records)!=audit['pen_points']:
        raise ValueError('Missing or extra raw pen points')
    strokes=strokes_from_raw(raw_bytes,locations);contacts=[]
    events={e['start_seq']:e for line in (directory/'edit-events.jsonl').read_text(encoding='utf-8').splitlines()
            if (e:=json.loads(line)).get('tool_type') in PEN_TOOLS}
    for stroke in strokes:
        lo,hi=stroke['seq_range'];raw_points=[records[n] for n in range(lo,hi+1) if n in records and
            all(records[n][k]==stroke[k] for k in ('pointer_id','window_id','display_id'))]
        if len(raw_points)!=stroke['raw_points']:raise ValueError('Contact reconstruction mismatch')
        selected=[locations[p['seq']] for p in raw_points]
        event=events.get(lo)
        if not event or event['end_seq']!=hi or event.get('provisional',True):
            raise ValueError('Missing final tool classification for raw contact')
        contacts.append({'number':stroke['stroke_index']+1,'start_seq':lo,'end_seq':hi,
            'tool_kind':event['kind'],'tool_evidence':event.get('context',{}).get('evidence'),
            'writing_tool_candidate':event['kind']=='pen_contact_pen',
            'points':len(selected),'located':sum(c['pdf_xy'] is not None for c in selected),
            'reasons':dict(Counter(c['reason'] for c in selected if c.get('reason'))),
            'runs':split_runs(selected),'screen_xy':[[p['x'],p['y']] for p in raw_points],
            'unknown_screen_xy':[[p['x'],p['y']] for p,c in zip(raw_points,selected) if c['pdf_xy'] is None]})
    # Recheck immutable inputs after deriving the viewer.
    if digest(raw)!=audit['raw_sha256'] or digest(export)!=audit['coordinate_export_sha256']:
        raise ValueError('Historical evidence changed during generation')
    return {'id':session,'label':'最新记录 · d9b6' if session==LATEST else '正在核对的记录 · 9a63',
        'contacts':contacts,'raw_sha256':audit['raw_sha256'],'coordinate_sha256':audit['coordinate_export_sha256'],
        'points':len(records),'statuses':audit['coordinate_status_counts'],'reasons':audit['coordinate_reason_counts'],
        'groups':[]}


def attach_groups(session,packet,raw):
    """Link original review identities to contacts; refuse changed source records."""
    if session['id']!=packet['manifest']['session_id'] or digest(raw)!=packet['manifest']['raw_sha256']:
        raise ValueError('Review/raw session mismatch')
    rows={p['seq']:(p,line) for line in raw.read_bytes().splitlines(keepends=True) if
          (p:=json.loads(line)).get('type')=='point'}
    contacts={c['start_seq']:c for c in session['contacts']}
    for sample in packet['samples']:
        starts=[]
        for evidence in sample['source']['contacts']:
            lo,hi=evidence['start_seq'],evidence['end_seq'];contact=contacts.get(lo)
            if not contact or contact['end_seq']!=hi:raise ValueError('Review contact boundary mismatch')
            selected=[(p,line) for n,(p,line) in rows.items() if lo<=n<=hi and p['tool_type'] in PEN_TOOLS]
            if (len(selected)!=evidence['point_count'] or
                    hashlib.sha256(b''.join(line for p,line in selected)).hexdigest()!=evidence['selected_raw_records_sha256']):
                raise ValueError('Review contact source changed')
            starts.append(lo)
        session['groups'].append({'id':sample['id'],'number':sample['number'],'starts':starts,
            'predictions':sample['predictions'],'input_mode':sample['input_mode']})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review',type=Path,default=Path('output/recognition/review-9a63/review.html'))
    args=parser.parse_args();root=Path(__file__).resolve().parents[1]
    review=args.review.resolve();html=review.read_text(encoding='utf-8')
    match=re.search(r'<script id="packet" type="application/json">(.*?)</script>',html,re.S)
    if not match:raise ValueError('Missing review packet')
    packet=json.loads(match[1]);config=read_json(root/'output/pipeline/cmc/configuration.json')
    sources={}
    for entry in [config]+config.get('additional_documents',[]):
        path=Path(entry['pdf']);sha=digest(path)
        if sha!=entry['pdf_sha256']:raise ValueError('Original PDF hash changed')
        sources[sha]=path
    pages={};sessions=[build_session(root,s,sources,pages) for s in (packet['manifest']['session_id'],LATEST)]
    attach_groups(sessions[0],packet,root/'output/ink/sessions'/(sessions[0]['id']+'.jsonl'))
    payload={'schema':1,'dataset_id':packet['dataset_id'],'sessions':sessions,'pages':list(pages.values()),
        'saved_state_verified':False,'screenshots_acquired':0,'tablet_actions':[],'raw_modified':False}
    template=Path(__file__).with_suffix('.html').read_text(encoding='utf-8')
    encoded=json.dumps(payload,ensure_ascii=False,allow_nan=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    destination=review.parent/'locations';destination.mkdir(exist_ok=True)
    atomic_bytes(destination/'index.html',template.replace('__CONTEXT_PACKET__',encoded).encode('utf-8'))
    # The manifest and embedded input packet are unchanged; localStorage uses the same dataset key.
    refreshed=refresh_review(review)
    if refreshed!=packet['dataset_id']:raise ValueError('Draft identity changed')
    report={k:v for k,v in payload.items() if k not in ('sessions','pages')}
    report['sessions']=[{k:v for k,v in s.items() if k not in ('contacts','groups')} for s in sessions]
    report['pages']=[{k:v for k,v in p.items() if k!='image'} for p in pages.values()]
    atomic_bytes(destination/'provenance.json',json.dumps(report,ensure_ascii=False,indent=2).encode())
    print(json.dumps({'output':str(destination/'index.html'),'dataset_id':refreshed,
        'sessions':[{k:s[k] for k in ('id','points','statuses')} for s in sessions],
        'pages':len(pages)},ensure_ascii=False))


if __name__=='__main__':main()
