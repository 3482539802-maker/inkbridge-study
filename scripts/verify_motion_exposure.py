"""Research fits cannot become coordinate authority; no device or images saved."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from motion_exposure import MotionExposureValidation
from multipage_geometry import page_frames
from validate_motion_poses import include_diagnostic_exposures, validate
from verify_coordinate_alignment import frame
import verify_motion_poses as pose_fixtures
from verify_multipage_geometry import binding


def payload():
    f = frame(1000)
    o = f['observation']
    o.update(valid=False, reason='context_boundary_failed_during_frame',
        motion_validation_payload_only=True, context_before=deepcopy(o['context']),
        window_before=deepcopy(o['window']), context_postflight_started_after_payload=True,
        context_preflight_age_ms=200, context_bracket_ms=1700,
        motion_context_identity_generation_range=[1, 1],
        page_motion=dict(candidate=True, pages=[dict(candidate=True, page_uuid='page',
            previous_screen_to_current_screen=[[1, 0, 3], [0, 1, 0]],
            support_hull_previous_screen_px=[[100, 100], [800, 100], [800, 600], [100, 600]])]))
    o['context_before']['page_layers'][0]['unclipped_window_bounds'][1] -= 3
    return SimpleNamespace(image=object(), observation=o), f['registration']


class ExposureChecks(unittest.TestCase):
    def test_independent_fit_is_separate_and_original_failure_preserved(self):
        captured, fit = payload()
        original = deepcopy(captured.observation)
        registration = Mock(sha256='a'*64)
        registration.match.return_value = fit
        result = MotionExposureValidation().inspect(captured, registration, 'test', binding(), Mock())
        self.assertTrue(result['checked'])
        self.assertEqual(captured.observation, original)
        self.assertFalse(result['coordinate_mapping_allowed'])
        row = dict(observation=captured.observation, registration=dict(valid=False),
                   motion_exposure_validation=result)
        self.assertFalse(page_frames(row)[0]['observation']['valid'])
        registration.match.assert_called_once_with(captured.image, pages=[0])

    def test_focus_identity_failure_old_context_and_wrong_page_never_fit(self):
        for mutation in ('identity', 'focus', 'window', 'duration', 'age', 'page', 'valid', 'postflight'):
            with self.subTest(mutation=mutation):
                captured, _ = payload(); o = captured.observation
                if mutation == 'identity': o['motion_context_identity_generation_range'] = [1, 3]
                elif mutation == 'focus': o['window']['focused'] = False
                elif mutation == 'window': o['window']['pid'] += 1
                elif mutation == 'duration': o['context_bracket_ms'] = 2501
                elif mutation == 'age': o['context_preflight_age_ms'] = 751
                elif mutation == 'page': o['context']['page_layers'][0]['page_id'] = 'other'
                elif mutation == 'valid': o['valid'] = True
                else: o['context_postflight_started_after_payload'] = False
                registration = Mock()
                result = MotionExposureValidation().inspect(captured, registration, 'test', binding(), Mock())
                self.assertFalse(result['checked'])
                registration.match.assert_not_called()

    def test_failure_and_budget_do_not_crash_or_authorize_coordinates(self):
        captured, _ = payload(); registration = Mock()
        registration.match.side_effect = RuntimeError('bad frame')
        probe = MotionExposureValidation(); probe.maximum_attempts = 1
        result = probe.inspect(captured, registration, 'test', binding(), Mock())
        self.assertEqual(result['reason'], 'exposure_validation_failed')
        result = probe.inspect(captured, registration, 'test', binding(), Mock())
        self.assertEqual(result['reason'], 'session_validation_budget_exhausted')
        self.assertEqual(registration.match.call_count, 1)
        self.assertFalse(captured.observation['valid'])

    def test_still_frame_does_not_spend_extra_registration_budget(self):
        captured, _ = payload()
        captured.observation['page_motion']['pages'][0]['previous_screen_to_current_screen'][0][2] = 0
        registration = Mock(); probe = MotionExposureValidation()
        result = probe.inspect(captured, registration, 'test', binding(), Mock())
        self.assertEqual(result['reason'], 'no_bounded_moving_page_candidate')
        self.assertEqual(probe.attempts, 0)
        registration.match.assert_not_called()

    def test_research_view_is_explicit_and_never_changes_input_rows(self):
        rows = pose_fixtures.MotionPoseChecks().translated()
        original_midpoint = deepcopy(rows[1])
        rows[1]['observation']['valid'] = False
        rows[1]['registration'] = dict(valid=False)
        rows[1]['motion_exposure_validation'] = dict(checked=True, exposure=original_midpoint,
                                                   coordinate_mapping_allowed=False)
        original = deepcopy(rows)
        self.assertEqual(validate(rows)['checks'], [])
        result = validate(include_diagnostic_exposures(rows))
        self.assertEqual(result['groups']['moving_over_2_screen_px']['agreeing_pages'], 1)
        self.assertFalse(result['coordinate_mapping_allowed'])
        self.assertEqual(rows, original)

    def test_coordinate_engine_ignores_successful_diagnostic_fit(self):
        from verify_coordinate_alignment import CoordinateSafety
        from coordinate_page_binding import bind_frame
        frames = [bind_frame(frame(t), binding()) for t in (11000, 13000)]
        for f in frames:
            exposure = deepcopy(f)
            f['observation']['valid'] = False
            f['registration'] = dict(valid=False, reason='context_boundary_failed_during_frame')
            f['motion_exposure_validation'] = dict(checked=True, exposure=exposure,
                                                   coordinate_mapping_allowed=False)
        _, rows = CoordinateSafety().run_alignment(frames=frames)
        self.assertTrue(all(r['pdf_xy'] is None for r in rows))

    def test_additional_document_uses_its_own_verified_original(self):
        captured, fit = payload()
        captured.observation['context']['document_title_candidate'] = 'tutorial'
        captured.observation['context_before']['document_title_candidate'] = 'tutorial'
        source_binding = dict(binding(), title='tutorial')
        primary = Mock(); registration = Mock()
        registration.match.return_value = fit
        additional = Mock()
        additional.select.return_value = registration, source_binding
        result = MotionExposureValidation().inspect(captured, primary, 'test', binding(), additional)
        self.assertTrue(result['checked'])
        primary.match.assert_not_called()
        additional.select.assert_called_once()

    def test_runtime_reports_extra_fit_cost_without_counting_as_coordinate_frame(self):
        import json
        from pathlib import Path
        import sqlite3
        import tempfile
        from audit_frame_runtime import audit
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with sqlite3.connect(path/'pipeline.sqlite') as db:
                db.execute('CREATE TABLE geometry(id INTEGER,payload TEXT)')
                db.execute('INSERT INTO geometry VALUES(0,?)', (json.dumps(dict(index=0,
                    observation=dict(valid=False, reason='context_boundary_failed_during_frame'),
                    motion_exposure_validation=dict(checked=True, processing_ms=250,
                        reason='independent_exposure_fit_only', coordinate_mapping_allowed=False))),))
            db.close()
            result = audit(path)
            self.assertEqual(result['reasons']['context_boundary_failed_during_frame'], 1)
            self.assertEqual(result['metrics']['page_motion.exposure_validation_ms']['median'], 250)
            self.assertEqual(result['page_motion_evidence']['exposure_validation']['independently_fitted_frames'], 1)


if __name__ == '__main__': unittest.main()
