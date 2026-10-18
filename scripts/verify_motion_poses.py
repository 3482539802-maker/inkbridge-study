"""Withheld exposure truth tests, including cancelling flow errors."""
from copy import deepcopy
import unittest

import cv2
import numpy as np

from coordinate_page_binding import bind_frame
from validate_motion_poses import validate
from verify_coordinate_alignment import frame
from verify_multipage_geometry import binding
from visual_registration import compose, project


def triplet(poses):
    rows = []
    hull = [[100, 100], [800, 100], [800, 600], [100, 600]]
    for i, pose in enumerate(poses):
        row = bind_frame(frame(1000+i*400), binding())
        row['index'] = i
        row['observation'].update(
            native_frame=dict(request_id='n'+str(i), timestamp_raw=1_000_000_000+i*400_000_000),
            motion_context_identity_generation_range=[0, 0])
        r = row['registration']
        r.update(pdf_to_screen=pose.tolist(), screen_to_pdf=cv2.invertAffineTransform(pose).tolist(),
                 support_hull_screen_px=project(pose, hull).tolist())
        if i:
            motion = compose(pose, cv2.invertAffineTransform(poses[i-1]))
            row['observation']['page_motion'] = dict(
                candidate=True, previous_request_id='n'+str(i-1), current_request_id='n'+str(i),
                previous_timestamp_raw=rows[-1]['observation']['native_frame']['timestamp_raw'],
                current_timestamp_raw=row['observation']['native_frame']['timestamp_raw'],
                pages=[dict(candidate=True, page_uuid='page',
                            previous_screen_to_current_screen=motion.tolist(),
                            support_hull_previous_screen_px=rows[-1]['registration']['support_hull_screen_px'])])
        rows.append(row)
    return rows


class MotionPoseChecks(unittest.TestCase):
    def translated(self):
        return triplet([np.array([[1., 0, x], [0, 1., -x/2]]) for x in (0, 12, 20)])

    def test_known_translation_and_rotation_scale_in_both_directions(self):
        rotations = [cv2.getRotationMatrix2D((450, 350), angle, scale)
                     for angle, scale in ((0, 1), (1, 1.01), (2, 1.02))]
        for rows in (self.translated(), triplet(rotations)):
            original = deepcopy(rows)
            result = validate(rows)
            check, = result['checks']
            self.assertEqual(check['motion'], 'moving_over_2_screen_px')
            self.assertLess(max(check['left_error_screen_px'], check['right_error_screen_px']), 1e-8)
            self.assertTrue(check['predictions_agree_with_withheld_fit'])
            self.assertEqual(rows, original)
            self.assertFalse(result['coordinates_produced'])
            self.assertFalse(result['intermediate_pen_positions_verified'])

    def test_cancelling_flow_errors_pass_endpoints_but_fail_middle(self):
        rows = self.translated()
        # +9 then -9 cancels in the endpoint chain; both predictions of the
        # withheld middle are nevertheless 9 screen pixels wrong.
        rows[1]['observation']['page_motion']['pages'][0]['previous_screen_to_current_screen'][0][2] += 9
        rows[2]['observation']['page_motion']['pages'][0]['previous_screen_to_current_screen'][0][2] -= 9
        check, = validate(rows)['checks']
        self.assertFalse(check['predictions_agree_with_withheld_fit'])
        self.assertAlmostEqual(check['left_error_screen_px'], 9)
        self.assertAlmostEqual(check['right_error_screen_px'], 9)

    def test_middle_transform_is_truth_only_not_prediction_input(self):
        rows = self.translated()
        rows[1]['registration']['pdf_to_screen'][0][2] += 7
        check, = validate(rows)['checks']
        self.assertFalse(check['predictions_agree_with_withheld_fit'])
        self.assertAlmostEqual(check['left_error_screen_px'], 7)
        self.assertAlmostEqual(check['right_error_screen_px'], 7)

    def test_absent_fit_or_identity_clock_and_nonce_barriers_refuse(self):
        for change in ('fit', 'binding', 'identity', 'nonce', 'timestamp', 'window', 'gap'):
            with self.subTest(change=change):
                rows = self.translated()
                middle = rows[1]
                if change == 'fit': middle['registration']['valid'] = False
                elif change == 'binding': middle['registration']['native_background_binding']['note_id'] = 'other'
                elif change == 'identity': middle['observation']['motion_context_identity_generation_range'] = [0, 2]
                elif change == 'nonce': middle['observation']['page_motion']['previous_request_id'] = 'old'
                elif change == 'timestamp': middle['observation']['page_motion']['previous_timestamp_raw'] += 1
                elif change == 'window': middle['observation']['window']['window_id'] = 99
                else: middle['observation']['native_frame']['timestamp_raw'] += 2_000_000_000
                self.assertEqual(validate(rows)['checks'], [])

    def test_disjoint_support_is_not_scored_as_valid(self):
        rows = self.translated()
        rows[1]['registration']['support_hull_screen_px'] = [[2000, 0], [2300, 0], [2300, 400], [2000, 400]]
        self.assertEqual(validate(rows)['checks'], [])

    def test_static_evidence_is_counted_separately(self):
        rows = triplet([np.eye(2, 3)]*3)
        result = validate(rows)
        self.assertEqual(result['groups']['moving_over_2_screen_px']['checked_pages'], 0)
        self.assertEqual(result['groups']['still_or_subthreshold']['agreeing_pages'], 1)


if __name__ == '__main__': unittest.main()
