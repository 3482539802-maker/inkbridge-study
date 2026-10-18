"""Changing bounds need original-pixel fits; identity failures remain invalid."""
from copy import deepcopy
import unittest
from unittest.mock import Mock

import numpy as np

from page_exposure import eligible,register
from verify_motion_exposure import payload
from verify_multipage_geometry import binding


class ExposureChecks(unittest.TestCase):
    def test_layout_veto_uses_original_endpoint_rectangles_not_intersection(self):
        captured,fit=payload();o=captured.observation;o['independent_page_exposure_requested']=True
        registration=Mock(sha256='a'*64);registration.match.return_value=fit
        quality=Mock(return_value=dict(status='contradiction',reason='layout_conflict'))
        observed,result=register(object(),o,registration,binding(),quality)
        self.assertFalse(observed['valid']);self.assertFalse(result['valid'])
        for call,key in zip(quality.call_args_list,('context_before','context')):
            self.assertEqual(call.args[0]['observation']['context']['page_layers'],o[key]['page_layers'])

    def test_moving_bounds_are_located_using_pixels_not_rectangle_values(self):
        captured,fit=payload();o=captured.observation
        o['independent_page_exposure_requested']=True
        original=deepcopy(o)
        registration=Mock(sha256='a'*64);registration.match.return_value=fit
        observation,result=register(np.zeros((800,1000,3),np.uint8),o,registration,binding())
        self.assertTrue(observation['valid']);self.assertTrue(result['valid'])
        self.assertEqual(result['pdf_to_screen'],fit['pdf_to_screen'])
        self.assertEqual(o,original)
        self.assertFalse(observation['context_validation']['page_stationary_verified'])
        self.assertEqual(registration.match.call_args.kwargs['pages'],[0])

    def test_identity_excursion_bad_timing_focus_missing_page_and_legacy_refuse(self):
        for change in ('identity','time','focus','pages','legacy','missing_image'):
            with self.subTest(change=change):
                captured,_=payload();o=captured.observation
                o['independent_page_exposure_requested']=True
                if change=='identity':o['motion_context_identity_generation_range']=[0,2]
                elif change=='time':o['context_bracket_ms']=2501
                elif change=='focus':o['window']['focused']=False
                elif change=='pages':o['context']['page_layers'][0]['page_id']='other'
                elif change=='legacy':o.pop('independent_page_exposure_requested')
                registration=Mock()
                after,fit=register(None if change=='missing_image' else object(),o,registration,binding())
                self.assertFalse(after['valid']);self.assertFalse(fit['valid'])
                registration.match.assert_not_called()

    def test_failed_visual_match_cannot_authorize_context(self):
        captured,_=payload();o=captured.observation;o['independent_page_exposure_requested']=True
        registration=Mock(sha256='a'*64)
        registration.match.return_value=dict(valid=False,reason='no_features')
        after,fit=register(object(),o,registration,binding())
        self.assertFalse(after['valid']);self.assertFalse(fit['valid'])

    def test_intersection_support_rejects_neighbor_overlap(self):
        captured,fit=payload();o=captured.observation;o['independent_page_exposure_requested']=True
        o['context_before']['page_layers'][0]['unclipped_window_bounds'][0]=300
        registration=Mock(sha256='a'*64);registration.match.return_value=fit
        after,result=register(object(),o,registration,binding())
        self.assertFalse(after['valid']);self.assertFalse(result['valid'])


if __name__=='__main__':unittest.main()
