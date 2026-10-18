"""Classify contact location from existing final coordinate evidence only."""
import json


def contact_location(store,event,allow_coordinate_preview=False):
    if event.get('tool_type') not in (1,3,4,5) or not event.get('points'):
        return {'scope':'unknown','reason':'no_pen_coordinate_channel'}
    rows=store.db.execute('''SELECT r.seq,c.payload FROM raw_index r
        LEFT JOIN coordinates c ON c.seq=r.seq
        WHERE r.seq BETWEEN ? AND ? AND r.tool=? AND r.pointer=? AND r.win=? AND r.display=?
        ORDER BY r.seq LIMIT 8193''',(event['start_seq'],event['end_seq'],event['tool_type'],
        event['pointer_id'],event['window_id'],event['display_id'])).fetchall()
    result={'scope':'unknown','coordinate_records':sum(p is not None for _,p in rows),
            'contact_points':event['points'],'method':'existing_final_coordinate_results',
            'page_position_verified':False}
    if len(rows)>8192:return dict(result,reason='contact_exceeds_spatial_budget')
    if (len(rows)!=event['points'] or not rows or rows[0][0]!=event['start_seq'] or
        rows[-1][0]!=event['end_seq'] or any(p is None for _,p in rows)):
        return dict(result,reason='incomplete_coordinate_records')
    points=[json.loads(p) for _,p in rows]
    if any(p.get('session_id')!=store.session or
           (p.get('provisional_live_revision',True) and not allow_coordinate_preview) for p in points):
        return dict(result,reason='not_final_same_session_coordinates')
    if allow_coordinate_preview:
        result['method']='existing_coordinate_preview_results'
        result['coordinate_preview']=any(p.get('provisional_live_revision',True) for p in points)
    if all(p.get('reason')=='outside_bound_page' for p in points):
        return dict(result,scope='outside_bound_page',reason=None)
    located=[p for p in points if p.get('status') in ('candidate','extrapolated_candidate') and p.get('pdf_xy')]
    if len(located)!=len(points):return dict(result,scope='partial_or_unknown',reason='some_points_unresolved')
    identities={(p.get('page_uuid'),p.get('source_pdf_sha256'),p.get('pdf_page_1based')) for p in points}
    if len(identities)!=1 or any(v is None for v in next(iter(identities))):
        return dict(result,reason='ambiguous_page_identity')
    page,source,number=next(iter(identities))
    context=event.get('context',{})
    if context.get('page_id') and context['page_id']!=page:return dict(result,reason='tool_coordinate_page_mismatch')
    if context.get('page_ids') and page not in context['page_ids']:
        return dict(result,reason='tool_coordinate_page_mismatch')
    return dict(result,scope='within_bound_page_candidate',reason=None,page_id=page,
        source_pdf_sha256=source,pdf_page_1based=number,
        extrapolated_points=sum(p['status']=='extrapolated_candidate' for p in points),
        start_pdf_candidate=points[0]['pdf_xy'],end_pdf_candidate=points[-1]['pdf_xy'])
