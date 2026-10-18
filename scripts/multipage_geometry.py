"""Independent visual fits for hash/UUID-bound simultaneous visible pages.

UI rectangles only select feature centres and veto support; they do not supply
transforms. No pages are chosen from handwriting/OCR or export ordering alone.
"""
from copy import deepcopy

from coordinate_page_binding import bind_frame, rectangle
from geometry_pixel_mapping import lift_registration


REASON='independently_registered_visible_pages'


def match_pages(registration, image, observation, binding, layout_check=None):
    context=observation.get('context') or {}
    layers=context.get('page_layers',[])
    if (not observation.get('valid') or not observation.get('window',{}).get('focused') or
        context.get('document_title_candidate')!=binding['title'] or
        not context.get('active_tab_marker_unique') or registration.sha256!=binding['source_pdf_sha256'] or
        not 2<=len(layers)<=12):
        return {'valid':False,'reason':'multipage_identity_or_context_unavailable'}
    ids=[p.get('page_id') for p in layers]
    pages=[binding['pages'].get(i) for i in ids]
    if (None in ids or len(set(ids))!=len(ids) or None in pages or len(set(pages))!=len(pages)):
        return {'valid':False,'reason':'multipage_background_binding_ambiguous'}
    try:
        regions=[rectangle(p) for p in layers]
        for i,r in enumerate(regions):
            for q in regions[i+1:]:
                if max(r[0],q[0])<min(r[2],q[2]) and max(r[1],q[1])<min(r[3],q[3]):
                    raise ValueError('Overlapping page rectangles')
    except ValueError:
        return {'valid':False,'reason':'multipage_rectangles_invalid_or_overlapping'}
    native=observation.get('native_frame')
    reduction=native.get('pixel_reduction',1) if native else 1
    if reduction not in (1,2):raise ValueError('Invalid native pixel reduction')
    offset=(reduction-1)/2
    candidates=[];rejected=[]
    for layer,page,region in zip(layers,pages,regions):
        # Native exact 2x2 pixel centres: screen=2*raster+0.5.
        raster_region=[(v-offset)/reduction for v in region]
        fitted=registration.match(image,pages=[page-1],screen_region=raster_region)
        if native:fitted=lift_registration(fitted,native,image.shape)
        bound=bind_frame({'observation':observation,'registration':fitted},binding)
        result=bound['registration']
        if result.get('valid') and layout_check is not None:
            quality=layout_check(bound)
            result['layout_consistency']=quality
            if quality['status']=='contradiction':result.update(valid=False,reason=quality['reason'])
        if result.get('valid'):
            candidates.append({'context':bound['observation']['context'],'registration':result})
        else:
            rejected.append({'page_uuid':layer['page_id'],'pdf_page_1based':page,'reason':result.get('reason')})
    return {'valid':False,'reason':REASON,'source_pdf_sha256':binding['source_pdf_sha256'],
        'page_candidates':candidates,'rejected_pages':rejected,
        'rectangle_used_for_transform':False,'physical_pen_accuracy_verified':False}


def page_frames(frame):
    """Expand numeric page candidates while preserving the parent time/barrier.

    Invalid focus/context cannot be revived. Each child inherits the parent's
    calibrated timestamp, raw index, acquisition diagnostics and window.
    """
    r=frame.get('registration',{});o=frame.get('observation',{})
    if r.get('reason')!=REASON:return [frame]
    candidates=r.get('page_candidates',[])
    if (r.get('valid') or not o.get('valid') or not o.get('window',{}).get('focused') or
            not isinstance(candidates,list) or not 1<=len(candidates)<=12):return []
    children=[];seen=set()
    for child in candidates:
        registration=child.get('registration',{});context=child.get('context',{})
        pages=context.get('page_layers',[]);binding=registration.get('native_background_binding',{})
        parent=o.get('context') or {}
        if (not registration.get('valid') or len(pages)!=1 or
            context.get('document_title_candidate')!=parent.get('document_title_candidate') or
            not context.get('active_tab_marker_unique') or
            context.get('visible_page_layers')!=parent.get('page_layers') or
            registration.get('source_pdf_sha256')!=r.get('source_pdf_sha256') or
            binding.get('source_pdf_sha256')!=r.get('source_pdf_sha256') or
            binding.get('page_uuid')!=pages[0].get('page_id') or
            binding.get('pdf_page_1based')!=registration.get('pdf_page_1based')):
            return []
        identity=(pages[0].get('page_id'),registration.get('pdf_page_1based'))
        if identity in seen:return []
        seen.add(identity)
        result=deepcopy(frame)
        result['registration']=deepcopy(registration)
        result['observation']['context']=deepcopy(context)
        result['simultaneous_page_candidate']=True
        children.append(result)
    return children


def rebind_pages(frame,binding,layout_check=None):
    """Revalidate saved independent fits against an explicitly supplied export."""
    result=deepcopy(frame)
    if result.get('registration',{}).get('reason')!=REASON:
        return bind_frame(result,binding)
    children=page_frames(result);candidates=[]
    for child in children:
        # Use the full original context, so the new binding must again select
        # this visual fit uniquely and veto support/overlapping rectangles.
        child['observation']['context']=deepcopy(result['observation']['context'])
        rebound=bind_frame(child,binding)
        if rebound['registration'].get('valid') and layout_check is not None:
            quality=layout_check(rebound)
            rebound['registration']['layout_consistency']=quality
            if quality['status']=='contradiction':rebound['registration'].update(valid=False,reason=quality['reason'])
        if rebound['registration'].get('valid'):
            candidates.append({'context':rebound['observation']['context'],'registration':rebound['registration']})
    result['registration']['page_candidates']=candidates
    result['registration']['source_pdf_sha256']=binding['source_pdf_sha256']
    result['registration']['rebound_against_explicit_export']=True
    return result
