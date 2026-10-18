"""Register a moving page from its pixels after validating identity separately.

Context bounds select/veto features only. This establishes an exposure pose,
not a stable interval and not permission to ignore raw input barriers.
"""
from copy import deepcopy

from coordinate_page_binding import bind_frame, rectangle
from geometry_pixel_mapping import lift_registration
from multipage_geometry import match_pages
from page_motion import bound_fits


def eligible(o):
    a, b = o.get('context_before') or {}, o.get('context') or {}
    generation = o.get('motion_context_identity_generation_range')
    ids = lambda c: [p.get('page_id') for p in c.get('page_layers', [])]
    first, last = ids(a), ids(b)
    return bool(o.get('independent_page_exposure_requested') and not o.get('valid') and
        o.get('reason') == 'context_boundary_failed_during_frame' and
        generation and len(generation) == 2 and generation[0] == generation[1] and
        o.get('window_before') == o.get('window') and o.get('window', {}).get('focused') and
        a.get('active_tab_marker_unique') and b.get('active_tab_marker_unique') and
        a.get('document_title_candidate') == b.get('document_title_candidate') and
        1 <= len(first) <= 12 and len(set(first)) == len(first) and
        len(set(last)) == len(last) and set(first) == set(last) and None not in first and
        o.get('context_postflight_started_after_payload') and
        0 <= o.get('context_preflight_age_ms', -1) <= 750 and
        0 <= o.get('context_bracket_ms', -1) <= 2500)


def register(image, observation, registration, binding, layout_check=None):
    failure = dict(valid=False, reason=observation.get('reason'))
    if image is None or not binding or not eligible(observation):
        return observation, failure
    o = deepcopy(observation)
    before = {p['page_id']: p for p in o['context_before']['page_layers']}
    # Intersections are observed inside the same page in BOTH contexts.
    # No union across a neighboring page, nor inferred screen/PDF transform.
    for layer in o['context']['page_layers']:
        try:
            a, b = rectangle(before[layer['page_id']]), rectangle(layer)
        except ValueError:
            return observation, dict(failure, exposure_rejection='invalid_page_region')
        region = [max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])]
        if region[2]-region[0] < 100 or region[3]-region[1] < 100:
            return observation, dict(failure, exposure_rejection='insufficient_common_page_region')
        layer['unclipped_window_bounds'] = region
    o.update(valid=True, reason=None)
    def quality_check(frame):
        # Do not feed the cropped intersection to a model trained on full UI
        # bounds. Keep the existing veto on each actual observed endpoint.
        checks=[]
        for context in (observation['context_before'], observation['context']):
            f=deepcopy(frame)
            uuid=f['registration']['native_background_binding']['page_uuid']
            f['observation']['context']['page_layers']=[deepcopy(p) for p in context['page_layers'] if p['page_id']==uuid]
            checks.append(layout_check(f))
        contradictions=[c for c in checks if c['status']=='contradiction']
        return dict(contradictions[0] if contradictions else checks[-1], exposure_endpoint_checks=checks)
    if len(o['context']['page_layers']) > 1:
        fitted = match_pages(registration, image, o, binding, quality_check if layout_check else None)
    else:
        layer = o['context']['page_layers'][0]
        page = binding['pages'].get(layer['page_id'])
        if page is None: return observation, dict(failure, exposure_rejection='unbound_page')
        reduction = o.get('native_frame', {}).get('pixel_reduction', 1)
        offset = (reduction-1)/2
        region = [(v-offset)/reduction for v in rectangle(layer)]
        fitted = registration.match(image, pages=[page-1], screen_region=region)
        if o.get('native_frame'): fitted = lift_registration(fitted, o['native_frame'], image.shape)
        fitted = bind_frame(dict(observation=o, registration=fitted), binding)['registration']
        if fitted.get('valid') and layout_check:
            quality=quality_check(dict(observation=o, registration=fitted))
            fitted['layout_consistency']=quality
            if quality['status']=='contradiction': fitted.update(valid=False, reason=quality['reason'])
    if not bound_fits(dict(observation=o, registration=fitted)):
        return observation, dict(failure, exposure_rejection='independent_original_fit_failed')
    o['context_validation'] = dict(method='identity_bracketed_independent_page_exposure',
        original_observation_valid=False, original_reason=observation['reason'],
        page_stationary_verified=False, rectangle_used_for_transform=False,
        support_region='intersection_of_both_observed_page_rectangles')
    return o, fitted
