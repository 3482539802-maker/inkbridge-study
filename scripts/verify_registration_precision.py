"""Subpixel geometry checks against known transforms; no real pen accuracy claim."""
import json
from pathlib import Path
import tempfile
import time
import unittest
import cv2
import fitz
import numpy as np
from visual_registration import PDFRegistration,compose,project,resize_to_original,robust_refit,refine_pixels

ROOT=Path(__file__).resolve().parents[1]


class PrecisionChecks(unittest.TestCase):
    def test_resize_pixel_centres_and_independently_rounded_axes(self):
        m=resize_to_original((1051,1600),(1840,2801))
        self.assertAlmostEqual(project(m,[[0,0]])[0,0],(2801/1600-1)/2)
        self.assertAlmostEqual(project(m,[[1599.5,1050.5]])[0,1],1839.5)
        self.assertNotEqual(m[0,0],m[1,1])

    def test_training_refit_rejects_outliers_and_recovers_subpixel_transform(self):
        rng=np.random.default_rng(31)
        source=rng.uniform([0,0],[2200,1800],(240,2))
        truth=np.array([[1.3,.001,-23.37],[-.002,1.29,117.43]])
        target=project(truth,source)+rng.normal(0,.12,(240,2))
        target[:60]+=rng.uniform(30,300,(60,2))
        initial=truth+np.array([[0,0,.2],[0,0,-.2]])
        result=robust_refit(source,target,initial)
        probes=np.array([[0,0],[2200,0],[0,1800],[2200,1800],[1000,900]])
        self.assertLess(max(np.linalg.norm(project(result,probes)-project(truth,probes),axis=1)),.08)

    def test_pixel_refinement_cannot_authorize_blank_or_large_jump(self):
        rng=np.random.default_rng(3)
        reference=rng.integers(0,256,(800,1000),dtype=np.uint8)
        reference=cv2.GaussianBlur(reference,(5,5),1)
        hull=[[0,0],[999,0],[999,799],[0,799]]
        self.assertIsNone(refine_pixels(reference,np.full_like(reference,255),np.eye(2,3),hull))
        shifted=cv2.warpAffine(reference,np.array([[1,0,7],[0,1,0]],np.float32),(1000,800))
        self.assertIsNone(refine_pixels(reference,shifted,np.eye(2,3),hull))

    def test_precise_pdf_geometry_with_crop_noninteger_scale_and_pen(self):
        results=[]
        source=ROOT/'materials/cmc2025/nonmath-a.pdf'
        if not source.is_file():
            self.skipTest('Private source PDF not distributed; precision fixture unavailable')
        matchers=[PDFRegistration(source,[0,1],precision=v) for v in (False,True)]
        try:
            raster,pdf_to_raster,kind=matchers[1].reference_pixels(0)
            self.assertEqual(kind,'embedded_full_page_scan')
            raster_to_pdf=cv2.invertAffineTransform(pdf_to_raster)
            for values in ((2.237,2.237,57.37,-152.63),(4.673,4.673,-197.19,-1221.43),(2.31,2.17,117.29,-233.13)):
                sx,sy,tx,ty=values
                truth=np.array([[sx,0,tx],[0,sy,ty]])
                image=cv2.warpAffine(raster,compose(truth,raster_to_pdf),(2801,1840),borderValue=232)
                cv2.rectangle(image,(0,0),(2801,150),245,-1)
                cv2.putText(image,'A B C',(1000,550),cv2.FONT_HERSHEY_SCRIPT_SIMPLEX,2,20,4)
                for matcher in matchers:
                    start=time.perf_counter()
                    result=matcher.match(image)
                    elapsed=(time.perf_counter()-start)*1000
                    self.assertTrue(result['valid'],result)
                    hull=np.float32(result['support_hull_screen_px'])
                    screen=np.array([[x,y] for x in range(200,2600,200) for y in range(250,1700,150)
                                     if cv2.pointPolygonTest(hull,(float(x),float(y)),False)>=0])
                    self.assertGreater(len(screen),5)
                    pdf=project(cv2.invertAffineTransform(truth),screen)
                    errors=np.linalg.norm(project(result['pdf_to_screen'],pdf)-screen,axis=1)
                    print(json.dumps({'mode':result['registration_mode'],'max_error':float(max(errors)),
                        'pixel_refinement':result.get('pixel_refinement')}),flush=True)
                    self.assertLess(max(errors),.8 if matcher.precision else 1.5)
                    results.append({'mode':result['registration_mode'],'transform':values,
                        'known_point_max_error_screen_px':float(max(errors)),
                        'known_point_p90_error_screen_px':float(np.percentile(errors,90)),
                        'processing_ms':elapsed,'heldout_median_px':result['heldout_median_error_screen_px']})
        finally:
            for matcher in matchers:matcher.close()
        output=ROOT/'output/test-runs/20261016-precision-alignment-001'
        output.mkdir(exist_ok=True,parents=True)
        (output/'synthetic-precision.json').write_bytes(json.dumps({'real_pen_accuracy_proof':False,'cases':results},indent=2).encode())
        print(json.dumps(results,indent=2))


if __name__=='__main__':unittest.main(verbosity=2)
