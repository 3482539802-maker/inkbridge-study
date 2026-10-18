"""Propose bounded whole-contact recognition inputs from a finalized ledger.

Temporal proximity is only a segmentation proposal, never proof of a complete
formula, a solution step, stationary canvas, or the current saved handwriting.
Keep partially located contacts in the proposal: dropping them can remove a
character stroke. Rendering still validates the selected raw records.
"""
import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import statistics


def spatial_quality(events):
    """Conservative screen-space fragmentation gate, not semantic clustering.

    Do not split a disconnected run and feed a potentially incomplete formula.
    A rejected run keeps every contact for a future page/layout-aware method.
    """
    if len(events)>64:return {'assessed':False,'reason':'contact_budget'}
    boxes=[e.get('bounds') for e in events]
    if not all(isinstance(b,list) and len(b)==4 and
               all(type(v) in (int,float) and math.isfinite(v) for v in b) and
               b[0]<=b[2] and b[1]<=b[3] for b in boxes):
        return {'assessed':False,'reason':'missing_valid_screen_bounds'}
    heights=[b[3]-b[1] for b in boxes if b[3]>b[1]]
    threshold=max(48.,min(160.,4*statistics.median(heights))) if heights else 48.
    remaining=set(range(len(boxes)));components=0
    while remaining:
        components+=1;queue=[remaining.pop()]
        while queue:
            a=boxes[queue.pop()]
            nearby=[j for j in remaining if math.hypot(max(0,a[0]-boxes[j][2],boxes[j][0]-a[2]),
                       max(0,a[1]-boxes[j][3],boxes[j][1]-a[3]))<=threshold]
            remaining.difference_update(nearby);queue.extend(nearby)
    return {'assessed':True,'screen_bbox_components':components,'gap_threshold_px':threshold,
            'spatially_disconnected':components>1,'canvas_stationarity_verified':False,
            'threshold_validated_for_formula_completeness':False}


def propose(events, session, gap_us=2_000_000):
    if type(gap_us) is not int or not 100_000 <= gap_us <= 8_000_000:
        raise ValueError('Invalid grouping gap')
    groups=[];current=[];left='ledger_start';previous=None

    def flush(right):
        nonlocal current,left
        if current:
            points=sum(e['points'] for e in current)
            duration=current[-1]['end_us']-current[0]['start_us']
            page_keys={(e.get('location_evidence',{}).get('page_id'),
                        e.get('location_evidence',{}).get('source_pdf_sha256'),
                        e.get('location_evidence',{}).get('pdf_page_1based')) for e in current}
            located=all(e.get('location_evidence',{}).get('scope')=='within_bound_page_candidate' for e in current)
            page_eligible=located and len(page_keys)==1 and None not in next(iter(page_keys))
            reason=('contact_budget' if len(current)>64 else 'point_budget' if points>16384 else
                    'duration_budget' if duration>30_000_000 else None)
            groups.append({'id':f"{session}:{current[0]['start_seq']}",'session_id':session,
                'start_sequences':[e['start_seq'] for e in current],
                'first_seq':current[0]['start_seq'],'last_seq':current[-1]['end_seq'],
                'contact_count':len(current),'point_count':points,
                'start_us':current[0]['start_us'],'end_us':current[-1]['end_us'],
                'left_boundary':left,'right_boundary':right,
                'page_incomplete_contacts':sum(e.get('location_evidence',{}).get('scope')!='within_bound_page_candidate' for e in current),
                'input_mode_candidate':'page' if page_eligible else 'screen',
                'spatial_quality':spatial_quality(current),
                'budget_rejection':reason,'raw_validation_required':True,
                'semantic_grouping_verified':False,'saved_or_current_state_verified':False})
        current=[];left=right

    for e in events:
        if e.get('session_id')!=session:raise ValueError('Ledger session mismatch')
        required=('start_seq','end_seq','start_us','end_us')
        if any(type(e.get(k)) is not int for k in required):raise ValueError('Invalid ledger interval')
        if e['end_seq']<e['start_seq'] or e['end_us']<e['start_us']:raise ValueError('Reversed ledger interval')
        if previous is not None and e['start_seq']<previous:raise ValueError('Ledger must be in sequence order')
        previous=e['start_seq']
        if e.get('kind')!='pen_contact_pen' or e.get('provisional',True):
            flush('unqualified_contact_or_input_boundary');continue
        if type(e.get('points')) is not int or e['points']<2:raise ValueError('Invalid complete contact size')
        if e.get('tool_type') not in (1,3,4,5):raise ValueError('Writing ledger has non-pen tool')
        if current:
            last=current[-1];reason=None
            if e['start_seq']!=last['end_seq']+1:reason='raw_sequence_boundary'
            elif any(e.get(k)!=last.get(k) for k in ('tool_type','pointer_id','window_id','display_id')):reason='input_context_change'
            elif e['start_us']<last['end_us']:reason='time_overlap_or_reversal'
            elif e['start_us']-last['end_us']>gap_us:reason='pause'
            else:
                # A positively observed page change is a boundary; missing
                # page coordinates alone must not discard a character stroke.
                a=last.get('location_evidence',{});b=e.get('location_evidence',{})
                for k in ('page_id','source_pdf_sha256','pdf_page_1based'):
                    if a.get(k) is not None and b.get(k) is not None and a[k]!=b[k]:reason='page_change';break
            if reason:flush(reason)
        current.append(e)
    flush('ledger_end')
    return groups


def from_directory(directory,gap_us=2_000_000):
    directory=Path(directory);job=json.loads((directory/'job.json').read_bytes())
    if not job.get('geometry_closed'):raise ValueError('Use a closed geometry session')
    progress=json.loads((directory/'processing.json').read_bytes())
    if not progress.get('final_complete'):raise ValueError('Final coordinates required before grouping')
    session=job['session_id']
    with closing(sqlite3.connect((directory/'pipeline.sqlite').resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        db.execute('BEGIN')
        # Whole-session numeric ledger, streamed in source order. Bound work
        # explicitly instead of silently selecting only a tail of a formula.
        rows=db.execute("SELECT payload FROM edit_events ORDER BY json_extract(payload,'$.start_seq'),rowid LIMIT 100001")
        def events():
            for count,(payload,) in enumerate(rows,1):
                if count>100000:raise ValueError('Ledger exceeds offline grouping budget')
                yield json.loads(payload)
        groups=propose(events(),session,gap_us)
    return {'schema':1,'session_id':session,'gap_us':gap_us,'groups':groups,
        'summary':{'groups':len(groups),'contacts':sum(g['contact_count'] for g in groups),
            'budget_rejected_groups':sum(g['budget_rejection'] is not None for g in groups),
            'partially_located_contacts_retained':sum(g['page_incomplete_contacts'] for g in groups)},
        'inference_run':False,'semantic_grouping_verified':False,'automatic_solution_steps':False,
        'screen_groups_require_stationarity_review':True,'raw_modified':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();report=from_directory(args.directory)
    with args.output.open('x',encoding='utf-8') as out:json.dump(report,out,ensure_ascii=False,indent=2)
    print(json.dumps(report['summary'],indent=2))


if __name__=='__main__':main()
