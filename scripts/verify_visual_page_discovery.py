"""Independent original fits, remapped page identities, conflict and timing gates."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock,patch
import numpy as np

from visual_page_discovery import VisualPageDiscovery


class DiscoveryChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'new-directory'/'pages.json'
        self.binding={'title':'generated','pages':{'old-id':1},'note_id':'old-note',
                      'source_pdf_sha256':'a'*64,'source_hinote_sha256':'b'*64}
        self.discovery=VisualPageDiscovery(self.binding,2,self.path)
        self.fit={'valid':True,'source_pdf_sha256':'a'*64,'pdf_page_1based':1,
                  'support_hull_screen_px':[[10,10],[180,10],[180,180],[10,180]],
                  'heldout_median_error_screen_px':.2,'heldout_inlier_p90_error_screen_px':.7}
        self.registration=Mock();self.registration.sha256='a'*64
        self.registration.match.side_effect=lambda *a,**k:deepcopy(self.fit)
        self.observation={'valid':True,'window':{'focused':True,'window_id':5},
             'context':{'document_title_candidate':'generated','active_tab_marker_unique':True,
                        'page_layers':[{'page_id':'new-id','unclipped_window_bounds':[0,0,200,200]}]}}

    def tearDown(self):self.temp.cleanup()

    def sample(self,t=1,observation=None):
        captured=SimpleNamespace(image=np.zeros((200,200),np.uint8),observation=deepcopy(observation or self.observation),acquired=t)
        result=self.discovery.process(captured,self.registration)
        self.assertIsNone(captured.image)
        return result

    def test_two_original_fits_bind_remapped_uuid_not_generated_id(self):
        self.assertFalse(self.sample()[1]['valid']);self.assertFalse(self.path.exists())
        o,r,_=self.sample(1.5)
        self.assertTrue(r['valid']);self.assertEqual(self.discovery.pages,{'new-id':1})
        self.assertEqual(self.registration.match.call_args.kwargs['pages'],[0,1])
        self.assertFalse(r['native_background_binding']['exported_page_uuid_verified'])
        self.assertFalse(r['native_background_binding']['live_note_id_verified'])
        self.assertEqual(o['context']['page_layers'][0]['page_id'],'new-id')
        self.assertTrue(self.path.exists())
        self.sample(2);self.assertEqual(self.registration.match.call_args.kwargs['pages'],[0])

    def test_same_frame_or_long_gap_does_not_confirm(self):
        for t in (1,1,20):self.assertFalse(self.sample(t)[1]['valid'])
        self.assertEqual(self.discovery.pages,{})

    def test_failed_or_ambiguous_original_fit_breaks_agreement(self):
        self.sample();self.fit['valid']=False;self.sample(1.5)
        self.fit['valid']=True;self.assertFalse(self.sample(2)[1]['valid'])
        self.assertEqual(self.discovery.pages,{})

    def test_wrong_title_focus_or_hash_never_fits(self):
        for mutation in ('title','focus','hash'):
            o=deepcopy(self.observation)
            if mutation=='title':o['context']['document_title_candidate']='other'
            if mutation=='focus':o['window']['focused']=False
            if mutation=='hash':self.registration.sha256='other'
            self.sample(1,o);self.registration.match.assert_not_called()

    def test_support_outside_page_cannot_bootstrap(self):
        self.fit['support_hull_screen_px'][0]=[-5,-5]
        self.sample();self.sample(1.5);self.assertEqual(self.discovery.pages,{})

    def test_overlapping_regions_never_fit(self):
        o=deepcopy(self.observation);o['context']['page_layers'].append({'page_id':'second','unclipped_window_bounds':[50,50,250,250]})
        self.sample(1,o);self.registration.match.assert_not_called()

    def test_conflicting_identity_is_blocked_and_unpublished(self):
        self.sample();self.sample(1.5);self.fit['pdf_page_1based']=2
        self.assertFalse(self.sample(2)[1]['valid'])
        self.assertEqual(self.discovery.pages,{})
        self.assertIn('new-id',self.discovery.blocked)

    def test_window_change_breaks_agreement(self):
        self.sample();o=deepcopy(self.observation);o['window']['window_id']=6
        self.assertFalse(self.sample(1.5,o)[1]['valid'])
        self.assertEqual(self.discovery.pages,{})

    def test_uncreated_output_directory_is_created_on_first_publish(self):
        self.assertFalse(self.path.parent.exists())
        self.sample();self.sample(1.5)
        self.assertTrue(self.path.is_file())

    def test_derived_write_failure_is_retried_without_stopping_capture(self):
        self.sample()
        with patch('visual_page_discovery.write_bounded_json',side_effect=PermissionError('injected')):
            _,result,_=self.sample(1.5)
            self.assertEqual(result['reason'],'visual_identity_storage_unavailable')
            self.assertTrue(self.discovery.dirty)
        _,result,_=self.sample(2)
        self.assertTrue(result['valid']);self.assertFalse(self.discovery.dirty);self.assertTrue(self.path.exists())


if __name__=='__main__':unittest.main(verbosity=2)
