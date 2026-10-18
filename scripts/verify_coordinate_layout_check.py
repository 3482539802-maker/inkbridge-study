"""Known-pose, contradiction and source isolation tests for the layout veto."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from coordinate_layout_check import check, fit, load_calibration


def scene(x, y, scale=1., bad=False):
    rect = [x, y, x+1000*scale, y+1200*scale]
    sx = sy = scale if not bad else .3*scale
    tx, ty = (x+40*scale, y) if not bad else (x+700*scale, y+250*scale)
    m, inv = [[sx, 0, tx], [0, sy, ty]], [[1/sx, 0, -tx/sx], [0, 1/sy, -ty/sy]]
    hull = [[sx*u+tx, sy*v+ty] for u, v in ((100,100),(700,100),(700,800),(100,800))]
    return {'observation': {'valid': True, 'window': {'focused': True, 'display_id': 0},
            'context': {'active_tab_marker_unique': True, 'document_title_candidate': 'note',
                        'page_layers': [{'page_id': 'page', 'unclipped_window_bounds': rect}]}},
            'registration': {'valid': True, 'pdf_page_1based': 1, 'source_pdf_sha256': 'a'*64,
                             'pdf_to_screen': m, 'screen_to_pdf': inv,
                             'support_hull_screen_px': hull, 'screen_size': [2800,1840],
                             'heldout_median_error_screen_px': .2}}


def training():
    return [scene(0,-100,1.), scene(0,-400,1.), scene(-200,-700,1.5), scene(-350,-950,2.)]


class LayoutChecks(unittest.TestCase):
    def test_different_scroll_zoom_poses_and_unseen_pose(self):
        model = fit(training())
        self.assertEqual(len(model['models']), 1)
        result = check(scene(-100,-600,1.7), model)
        self.assertEqual(result['status'], 'consistent_candidate')
        self.assertLess(result['max_disagreement_screen_px'], 1e-8)
        self.assertFalse(result['point_mapping_allowed'])

    def test_thumbnail_with_tiny_feature_residual_is_rejected(self):
        frames = training()+[scene(0,-800,1.3,bad=True)]
        result = check(frames[-1], fit(frames))
        self.assertEqual(result['status'], 'contradiction')
        self.assertGreater(result['max_disagreement_screen_px'], 100)

    def test_repeated_stationary_frames_do_not_create_calibration(self):
        self.assertEqual(fit([scene(0,0)]*100)['models'], [])
        self.assertEqual(fit([scene(0,-i*100) for i in range(5)])['models'], [])

    def test_repeated_bad_pose_does_not_overwhelm_consensus(self):
        bad = scene(0,-800,1.3,bad=True)
        model = fit(training()+[bad]*100)
        self.assertEqual(model['models'][0]['calibration_poses'], 4)
        self.assertEqual(check(bad, model)['status'], 'contradiction')

    def test_changed_page_pdf_document_or_display_never_reuses_model(self):
        model = fit(training())
        for mutate in (lambda f:f['registration'].update(pdf_page_1based=2),
                       lambda f:f['registration'].update(source_pdf_sha256='b'*64),
                       lambda f:f['observation']['context'].update(document_title_candidate='other'),
                       lambda f:f['observation']['context']['page_layers'][0].update(page_id='other'),
                       lambda f:f['observation']['window'].update(display_id=1)):
            frame = scene(0,-800)
            mutate(frame)
            self.assertEqual(check(frame,model)['status'], 'unresolved')

    def test_invalid_frames_and_inverse_never_gain_validity(self):
        model = fit(training())
        values = [scene(0,-800) for _ in range(3)]
        values[0]['observation']['window']['focused'] = False
        values[1]['registration']['screen_to_pdf'][0][2] += 5
        values[2]['observation']['context']['page_layers'][0]['unclipped_window_bounds'] = [0,0,0,0]
        for frame in values:
            self.assertEqual(check(frame, model)['status'], 'unresolved')

    def test_insufficient_consensus_and_no_input_mutation(self):
        frames = training()
        saved = deepcopy(frames)
        fit(frames)
        self.assertEqual(frames,saved)
        for i in (0,1):
            frames[i] = scene(i*100,-i*100,1.+i,bad=True)
        self.assertEqual(fit(frames)['models'], [])

    def test_invalid_or_coordinate_granting_model_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'model.json'
            model = fit(training())
            path.write_text(json.dumps(model))
            self.assertEqual(load_calibration(path),model)
            model['models'][0]['point_mapping_allowed'] = True
            path.write_text(json.dumps(model))
            with self.assertRaises(ValueError): load_calibration(path)


if __name__ == '__main__':
    unittest.main(verbosity=2)
