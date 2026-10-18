"""Read-only numeric motion/coordinate audit of one durable session.

Distinguishes still images from actual motion and links unresolved contacts to
nearby acquisition failures. No images, device requests or coordinate changes.
"""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import numpy as np

from page_motion import bound_fits
from visual_registration import compose, project


def displacement(matrix, support):
    points = np.asarray(support, float)
    return float(np.max(np.linalg.norm(project(matrix, points)-points, axis=1)))


def motion_class(distance):
    # A descriptive screen-space threshold, never an accuracy/acceptance gate.
    return 'moving_over_2_screen_px' if distance > 2 else 'still_or_subthreshold'


def geometry_summary(row):
    o, r = row.get('observation', {}), row.get('registration', {})
    return dict(index=row['index'], valid_context=o.get('valid', False),
        reason=o.get('reason') or r.get('reason'), bound_pages=len(bound_fits(row)),
        preflight_age_ms=o.get('context_preflight_age_ms'),
        context_bracket_ms=o.get('context_bracket_ms'),
        identity_generations=o.get('motion_context_identity_generation_range'),
        flow_candidate=o.get('page_motion', {}).get('candidate', False))


def inspect(directory):
    with closing(sqlite3.connect((Path(directory)/'pipeline.sqlite').resolve().as_uri()+'?mode=ro',
                                uri=True, timeout=2)) as db:
        db.execute('BEGIN')
        counts = dict(db.execute("SELECT COALESCE(reason,status),COUNT(*) FROM coordinates GROUP BY COALESCE(reason,status)"))
        records=db.execute('SELECT payload FROM geometry ORDER BY id LIMIT 1025').fetchall()
        if len(records)>1024:raise ValueError('Use bounded runtime audit for sessions over 1024 geometry rows')
        rows = [json.loads(p) for p, in records]
        unknown = db.execute("""SELECT c.seq,r.t,c.reason FROM coordinates c JOIN raw_index r USING(seq)
            WHERE json_extract(c.payload,'$.pdf_xy') IS NULL ORDER BY c.seq LIMIT 32769""").fetchall()
        if len(unknown)>32768:raise ValueError('Unknown-point diagnostic exceeds bounded sample')
        indexed = db.execute('SELECT COUNT(*),MIN(t),MAX(t) FROM raw_index').fetchone()
    by_id = {r['index']:r for r in rows}
    by_nonce = {r.get('observation',{}).get('native_frame',{}).get('request_id'):r for r in rows}
    evidence = Counter(); residuals = {}; moving_examples = []
    for row in rows:
        hint = row.get('observation', {}).get('page_motion', {})
        for page in hint.get('pages', []):
            if page.get('candidate'):
                distance = displacement(page['previous_screen_to_current_screen'], page['support_hull_previous_screen_px'])
                evidence['flow.'+motion_class(distance)] += 1
        for field, previous in (
            ('page_motion_anchor_check', by_nonce.get(hint.get('previous_request_id'))),
            ('page_motion_chain_check', by_id.get(row.get('page_motion_chain_check', {}).get('anchor_index')))):
            if previous is None:continue
            old, new = bound_fits(previous), bound_fits(row)
            for page in row.get(field, {}).get('pages', []):
                key = next((k for k in old if k[0] == page['page_uuid'] and k in new), None)
                if key is None:continue
                a, b = old[key], new[key]
                distance = displacement(compose(b['pdf_to_screen'], a['screen_to_pdf']), a['support_hull_screen_px'])
                label = field+'.'+motion_class(distance)
                evidence[label] += 1
                evidence[label+'.agreeing'] += bool(page['agrees_with_independent_fits'])
                residuals.setdefault(label, []).append(page['max_anchor_disagreement_screen_px'])
                if distance > 2 and field == 'page_motion_chain_check':
                    moving_examples.append(dict(indices=row[field]['geometry_indices'],
                        page_uuid=key[0], endpoint_motion_screen_px=distance,
                        disagreement_screen_px=page['max_anchor_disagreement_screen_px'],
                        agrees=page['agrees_with_independent_fits']))
    groups = []
    for seq, t, reason in unknown:
        if not groups or seq != groups[-1]['seq_range'][1]+1 or reason != groups[-1]['reason']:
            groups.append(dict(seq_range=[seq,seq], points=0, reason=reason, action_time_us=[t,t]))
        group = groups[-1];group['seq_range'][1] = seq;group['points'] += 1;group['action_time_us'][1] = t
    # Native timestamp and input time have independently assessed candidate
    # domains. Neighborhood is diagnostic only and cannot supply a coordinate.
    frames = [(r['observation']['native_frame']['timestamp_raw']/1000, r) for r in rows
              if r.get('observation', {}).get('native_frame')]
    for group in groups:
        lo, hi = group['action_time_us']
        group['nearby_geometry'] = [dict(geometry_summary(r),
            relative_ms=round((stamp-lo)/1000,3)) for stamp,r in frames if lo-2e6 <= stamp <= hi+2e6]
    from audit_frame_runtime import quantiles
    return dict(schema=1, indexed_events=indexed[0], indexed_action_span_us=list(indexed[1:]),
        coordinate_counts=counts, unknown_points=len(unknown), unknown_groups=groups,
        motion_counts=dict(evidence), independent_fit_residuals_screen_px={k:quantiles(v) for k,v in residuals.items()},
        moving_chain_examples=moving_examples, geometry_records=len(rows),
        acquisition_reasons=dict(Counter(r.get('observation',{}).get('reason') or
            r.get('registration',{}).get('reason') or 'valid' for r in rows)),
        interpretation='Still-image agreement does not validate movement. Endpoint agreement does not locate intermediate pen points.',
        physical_accuracy_verified=False, coordinates_modified=False,
        screenshots_acquired=0, tablet_actions=[])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();result=inspect(args.directory)
    with args.output.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='unknown_groups'},ensure_ascii=False,indent=2))
    print(json.dumps([{k:v for k,v in g.items() if k!='nearby_geometry'} for g in result['unknown_groups']],ensure_ascii=False))


if __name__=='__main__':main()
