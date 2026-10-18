"""Independent PDF exposure checks for motion research; never authorize ink.

Only an already-acquired, otherwise rejected frame can enter this path.
Rectangle unions select features and veto support; their values never supply
the fitted transform. Results are stored outside the coordinate registration.
"""
from copy import deepcopy
import math
import time

import numpy as np

from coordinate_page_binding import bind_frame, rectangle
from geometry_pixel_mapping import lift_registration
from multipage_geometry import match_pages
from page_motion import bound_fits
from visual_registration import project


class MotionExposureValidation:
    maximum_attempts = 24

    def __init__(self): self.attempts = 0

    def inspect(self, captured, primary_registration, primary_title, primary_binding, additional):
        o = getattr(captured, 'observation', {})
        if not o.get('motion_validation_payload_only') or captured.image is None: return None
        result = dict(schema=1, checked=False, coordinate_mapping_allowed=False,
                      physical_accuracy_verified=False, image_files_retained=0)
        if self.attempts >= self.maximum_attempts:
            return dict(result, reason='session_validation_budget_exhausted')
        try:
            before, after = o.get('context_before') or {}, o.get('context') or {}
            generation = o.get('motion_context_identity_generation_range')
            if (o.get('valid') or o.get('reason') != 'context_boundary_failed_during_frame' or
                not generation or generation[0] != generation[1] or
                o.get('window_before') != o.get('window') or not o.get('window', {}).get('focused') or
                not before.get('active_tab_marker_unique') or not after.get('active_tab_marker_unique') or
                before.get('document_title_candidate') != after.get('document_title_candidate') or
                not o.get('context_postflight_started_after_payload') or
                not 0 <= o.get('context_preflight_age_ms', math.inf) <= 750 or
                not 0 <= o.get('context_bracket_ms', math.inf) <= 2500):
                return dict(result, reason='validation_identity_or_time_barrier')
            a = {p['page_id']: p for p in before.get('page_layers', [])}
            b = {p['page_id']: p for p in after.get('page_layers', [])}
            if (not 1 <= len(a) <= 12 or a.keys() != b.keys() or
                len(a) != len(before.get('page_layers', [])) or len(b) != len(after.get('page_layers', []))):
                return dict(result, reason='validation_page_identity_changed')
            movements = []
            for p in o.get('page_motion', {}).get('pages', []):
                if not p.get('candidate'): continue
                hull = np.asarray(p['support_hull_previous_screen_px'], float)
                movements.append(float(np.max(np.linalg.norm(
                    project(p['previous_screen_to_current_screen'], hull)-hull, axis=1))))
            if not movements or not .25 < max(movements) <= 300:
                return dict(result, reason='no_bounded_moving_page_candidate')
            context = deepcopy(after)
            layers = []
            for key in a:
                ra, rb = rectangle(a[key]), rectangle(b[key])
                layer = deepcopy(b[key])
                layer['unclipped_window_bounds'] = [min(ra[0], rb[0]), min(ra[1], rb[1]),
                                                     max(ra[2], rb[2]), max(ra[3], rb[3])]
                layers.append(layer)
            context['page_layers'] = layers
            if context['document_title_candidate'] == primary_title:
                registration, binding = primary_registration, primary_binding
            else:
                registration, binding = additional.select(context)
            if not binding: return dict(result, reason='validation_original_binding_unavailable')
            # Synthetic validity applies ONLY inside this isolated experiment.
            # The original observation and registration are never changed.
            experiment = deepcopy(o)
            experiment.update(valid=True, context=context, diagnostic_context_only=True)
            started = time.monotonic()
            self.attempts += 1
            if len(layers) > 1:
                fitted = match_pages(registration, captured.image, experiment, binding)
            else:
                page = binding['pages'].get(layers[0]['page_id'])
                if page is None: return dict(result, reason='validation_page_not_bound')
                fitted = registration.match(captured.image, pages=[page-1])
                if o.get('native_frame'):
                    fitted = lift_registration(fitted, o['native_frame'], captured.image.shape)
                fitted = bind_frame(dict(observation=experiment, registration=fitted), binding)['registration']
            row = dict(observation=experiment, registration=fitted)
            result.update(checked=bool(bound_fits(row)), exposure=row,
                          processing_ms=(time.monotonic()-started)*1000,
                          reason='independent_exposure_fit_only', attempt=self.attempts)
            return result
        except Exception as error:
            return dict(result, reason='exposure_validation_failed', error_type=type(error).__name__)
