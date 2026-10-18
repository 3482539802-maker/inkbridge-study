"""Validate a selected native PDF page across harmless UI cache/tree changes.

Requires both original context observations. Never repairs old incomplete logs.
No rectangle is used to construct a coordinate transform.
"""
from copy import deepcopy
from coordinate_page_binding import bind_frame,rectangle


def reconcile(frame,binding):
    result=deepcopy(frame)
    o,r=result.get('observation',{}),result.get('registration',{})
    if o.get('valid'):return bind_frame(result,binding)
    if (o.get('reason')!='window_title_or_page_geometry_changed' or not r.get('valid') or
        not o.get('window_before') or o['window_before']!=o.get('window') or
        not o['window'].get('focused') or not o.get('context_before')):
        return result
    probes=[]
    for context in (o['context_before'],o.get('context',{})):
        probe=deepcopy(result)
        probe['observation'].update(valid=True,context=context,reason=None)
        probe=bind_frame(probe,binding)
        if not probe['registration'].get('valid'):return result
        probes.append(probe)
    before,after=[p['observation']['context'] for p in probes]
    if before['page_layers'][0]['page_id']!=after['page_layers'][0]['page_id']:return result
    a,b=[rectangle(c['page_layers'][0]) for c in (before,after)]
    disagreement=max(abs(x-y) for x,y in zip(a,b))
    if disagreement>.25:return result
    # Shared other visible pages must also remain stationary. A newly materialized
    # node outside the selected page is already bounded/nonoverlapping by bind_frame.
    others={p['page_id']:p for p in before.get('visible_page_layers',[])}
    for p in after.get('visible_page_layers',[]):
        if p['page_id'] in others:
            if max(abs(x-y) for x,y in zip(rectangle(others[p['page_id']]),rectangle(p)))>.25:
                return result
    result=probes[1]
    result['observation']['context_validation']={
        'method':'both_contexts_verified_against_same_native_background_and_visual_support',
        'original_observation_valid':False,'maximum_selected_bounds_disagreement_px':disagreement,
        'tolerance_px':.25,'rectangle_used_for_transform':False}
    return result
