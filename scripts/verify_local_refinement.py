"""Small-correction accuracy, independent validation and source isolation checks."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from local_geometry_refinement import refine_local_pixels
from visual_registration import PDFRegistration, compose, project

ROOT=Path(__file__).resolve().parents[1]


class LocalChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (ROOT/'materials/cmc2025/nonmath-a.pdf').is_file():
            raise unittest.SkipTest('Private source PDF not distributed; local-refinement fixture unavailable')
        cls.matcher=PDFRegistration(ROOT/'materials/cmc2025/nonmath-a.pdf',[0,1],local_refinement=True)
        cls.reference,cls.pdf_to_raster,_=cls.matcher.reference_pixels(0)
        cls.truth=np.array([[4.673,0,-197.19],[0,4.673,-1221.43]])
        cls.image=cv2.warpAffine(cls.reference,compose(cls.truth,cv2.invertAffineTransform(cls.pdf_to_raster)),(2801,1840),borderValue=232)
        cls.coarse=cls.matcher.match(cls.image)

    @classmethod
    def tearDownClass(cls):cls.matcher.close()

    def test_small_shift_corrected_without_feature_extraction_or_input_mutation(self):
        supplied=deepcopy(self.coarse)
        supplied['pdf_to_screen'][0][2]+=.8
        supplied['pdf_to_screen'][1][2]-=.6
        supplied['screen_to_pdf']=cv2.invertAffineTransform(np.array(supplied['pdf_to_screen'])).tolist()
        before=deepcopy(supplied)
        with patch.object(self.matcher,'reference',side_effect=AssertionError('SIFT path forbidden')):
            result=self.matcher.refine_local(self.image,supplied)
        self.assertEqual(supplied,before)
        self.assertTrue(result['pixel_refinement']['accepted'],result)
        probes=project(cv2.invertAffineTransform(self.truth),[[400,500],[1200,800],[2000,1400]])
        errors=np.linalg.norm(project(result['pdf_to_screen'],probes)-project(self.truth,probes),axis=1)
        self.assertLess(max(errors),.2)
        self.assertGreaterEqual(result['pixel_refinement']['heldout_patches'],4)

    def test_large_shift_and_blank_reference_cannot_be_accepted(self):
        rng=np.random.default_rng(13)
        a=cv2.GaussianBlur(rng.integers(0,256,(800,1100),np.uint8),(3,3),.8)
        hull=[[0,0],[1099,0],[1099,799],[0,799]]
        shifted=cv2.warpAffine(a,np.array([[1,0,7],[0,1,-3]],np.float32),(1100,800))
        self.assertIsNone(refine_local_pixels(a,shifted,np.eye(2,3),hull))
        self.assertIsNone(refine_local_pixels(np.full_like(a,255),a,np.eye(2,3),hull))

    def test_one_axis_texture_is_rejected(self):
        a=np.tile((np.sin(np.arange(1100)*.2)*100+120).astype(np.uint8),(800,1))
        self.assertIsNone(refine_local_pixels(a,a,np.eye(2,3),[[0,0],[1099,0],[1099,799],[0,799]]))

    def test_changed_pdf_page_size_or_broken_inverse_rejected(self):
        for mutate in (lambda r:r.update(source_pdf_sha256='f'*64),lambda r:r.update(pdf_page_1based=7),
                       lambda r:r.update(screen_size=[1,1]),lambda r:r['screen_to_pdf'][0].__setitem__(2,999)):
            supplied=deepcopy(self.coarse);mutate(supplied)
            self.assertFalse(self.matcher.refine_local(self.image,supplied)['valid'])

    def test_failed_coarse_fit_is_never_revived(self):
        supplied={'valid':False,'reason':'wrong_page'}
        self.assertEqual(self.matcher.refine_local(self.image,supplied),supplied)

    def test_reference_cache_reused_bounded_and_read_only(self):
        image,matrix,_=self.matcher.reference_pixels(0)
        with patch.object(self.matcher,'_reference_pixels',side_effect=AssertionError('redecode forbidden')):
            again=self.matcher.reference_pixels(0)
        self.assertIs(again[0],image)
        self.assertFalse(image.flags.writeable)
        self.assertFalse(matrix.flags.writeable)
        self.assertLessEqual(self.matcher.pixel_cache_bytes,32*1024*1024)

    def test_inconsistent_validation_blocks_reject_training_fit(self):
        rng=np.random.default_rng(19)
        a=cv2.GaussianBlur(rng.integers(0,256,(1000,1400),np.uint8),(3,3),.8)
        # Two real screen regions disagree; a single global correction is unsafe.
        b=cv2.warpAffine(a,np.array([[1,0,1.3],[0,1,.2]],np.float32),(1400,1000))
        other=cv2.warpAffine(a,np.array([[1,0,-1.3],[0,1,-.2]],np.float32),(1400,1000))
        b[500:]=other[500:]
        self.assertIsNone(refine_local_pixels(a,b,np.eye(2,3),[[0,0],[1399,0],[1399,999],[0,999]]))


if __name__=='__main__':unittest.main(verbosity=2)
