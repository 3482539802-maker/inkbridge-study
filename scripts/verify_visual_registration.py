"""Verify geometry against known transforms, altered handwriting and wrong pages."""
import json
from pathlib import Path
import tempfile
import time
import unittest

import cv2
import fitz
import numpy as np

from visual_registration import PDFRegistration, project, screen_point_to_pdf

ROOT=Path(__file__).resolve().parents[1]


class RegistrationSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='inkbridge-visual-tests-')
        cls.pdf=Path(cls.tmp.name)/'test.pdf'
        source=ROOT/'materials/cmc2025/nonmath-a.pdf'
        if source.exists():
            cls.pdf=source
        else:
            with fitz.open() as doc:
                for n in range(2):
                    p=doc.new_page(width=612,height=792)
                    # Cover the entire page with distinctive, deterministic detail.
                    # Sparse left-aligned text does not exercise a deeply zoomed crop.
                    rng=np.random.default_rng(2026+n)
                    for j in range(38):
                        for col in range(3):
                            value=int(rng.integers(100000,999999))
                            p.insert_text((24+col*190,28+j*19.5),
                                          f'Q{n+1}.{j+1}.{col} x^{j+1} + {value}',fontsize=8)
                    for _ in range(700):
                        x,y=rng.uniform([12,12],[600,780])
                        width,height=rng.uniform([2,2],[8,8])
                        p.draw_rect(fitz.Rect(x,y,x+width,y+height),
                                    color=(0,0,0),width=.5,
                                    fill=(float(rng.uniform(.2,.9)),)*3)
                doc.save(cls.pdf)
        cls.matcher=PDFRegistration(cls.pdf,[0,1])
        with fitz.open(cls.pdf) as doc:
            p=doc[0];pix=p.get_pixmap(matrix=fitz.Matrix(1.8,1.8))
            cls.reference=cv2.cvtColor(np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width,3),cv2.COLOR_RGB2BGR)
            cls.pdf_scale=[pix.width/p.rect.width,pix.height/p.rect.height]
        cls.evidence=[]

    @classmethod
    def tearDownClass(cls):
        cls.matcher.close()
        report=ROOT/'output/test-runs/20261016-visual-calibration-001/registration-verification.json'
        report.parent.mkdir(parents=True,exist_ok=True)
        report.write_text(json.dumps({'source':'known_transform_synthetic_screen_from_original_pdf',
            'real_tablet_localization_proof':False,'cases':cls.evidence},indent=2),encoding='utf-8')
        cls.tmp.cleanup()

    def scene(self,sx,sy,tx,ty,handwriting=False):
        matrix=np.array([[sx/self.pdf_scale[0],0,tx],[0,sy/self.pdf_scale[1],ty]],np.float64)
        image=cv2.warpAffine(self.reference,matrix,(1600,1100),borderValue=(232,232,232))
        cv2.rectangle(image,(0,0),(1600,75),(245,245,245),-1)
        if handwriting:
            for i in range(6):cv2.putText(image,'A B C  x^2 + y', (500,400+i*35),cv2.FONT_HERSHEY_SCRIPT_SIMPLEX, .7,(10,10,210),2)
        return image,np.array([[sx,0,tx],[0,sy,ty]])

    def check_geometry(self,name,values,handwriting=False):
        image,truth=self.scene(*values,handwriting)
        started=time.perf_counter()
        result=self.matcher.match(image)
        elapsed=(time.perf_counter()-started)*1000
        self.assertTrue(result['valid'],result)
        self.assertEqual(result['pdf_page_1based'],1)
        probes=np.array([[100,100],[300,400],[500,650]],np.float64)
        errors=np.linalg.norm(project(result['pdf_to_screen'],probes)-project(truth,probes),axis=1)
        self.assertLess(max(errors),3.)
        self.evidence.append({'case':name,'max_known_point_error_screen_px':float(max(errors)),
            'processing_ms':elapsed,'heldout_error_px':result['heldout_median_error_screen_px']})

    def test_fit_whole_page(self): self.check_geometry('whole_page',(1.2,1.2,350,95))
    def test_zoom_and_scroll_crop(self): self.check_geometry('zoom_crop',(2.25,2.25,-160,-520))
    def test_notes_anisotropic_page_background(self): self.check_geometry('different_page_ratio',(1.6,1.43,200,-80))
    def test_added_pen_marks(self): self.check_geometry('added_handwriting',(1.7,1.7,180,-180),True)

    def test_large_tablet_zoom_crop_with_pen_marks(self):
        # Recorded tablet viewport/bounds shape, generated from a known PDF.
        # Numeric truth only; this cannot stand in for real pen localization.
        truth=np.array([[4.7,0,-197],[0,4.7,-1221]],np.float64)
        raster_transform=truth.copy()
        raster_transform[:,0]/=self.pdf_scale[0]
        raster_transform[:,1]/=self.pdf_scale[1]
        image=cv2.warpAffine(self.reference,raster_transform,(2800,1840),borderValue=(232,232,232))
        cv2.rectangle(image,(0,0),(2800,150),(245,245,245),-1)
        cv2.putText(image,'A B C',(1000,550),cv2.FONT_HERSHEY_SCRIPT_SIMPLEX,2,(10,10,210),4)
        result=self.matcher.match(image)
        self.assertTrue(result['valid'],result)
        self.assertEqual(result['pdf_page_1based'],1)
        probes=np.array([[150,400],[300,500],[450,600]],np.float64)
        error=np.linalg.norm(project(result['pdf_to_screen'],probes)-project(truth,probes),axis=1)
        self.assertLess(max(error),3.)

    def test_unrelated_and_blank_rejected(self):
        for image in (np.full((1100,1600,3),255,np.uint8), np.random.default_rng(7).integers(0,256,(1100,1600,3),dtype=np.uint8)):
            self.assertFalse(self.matcher.match(image)['valid'])

    def test_ambiguous_duplicate_pages_rejected(self):
        duplicate=Path(self.tmp.name)/'duplicate.pdf'
        with fitz.open(self.pdf) as source,fitz.open() as dest:
            dest.insert_pdf(source,from_page=0,to_page=0);dest.insert_pdf(source,from_page=0,to_page=0)
            dest.save(duplicate)
        matcher=PDFRegistration(duplicate)
        try:
            result=matcher.match(self.scene(1.2,1.2,350,95)[0])
            self.assertFalse(result['valid']);self.assertEqual(result['reason'],'ambiguous_multiple_pdf_pages')
        finally:matcher.close()

    def test_unsupported_point_rejected(self):
        result=self.matcher.match(self.scene(1.2,1.2,350,95)[0])
        self.assertTrue(result['valid'])
        with self.assertRaises(ValueError):screen_point_to_pdf(result,0,0)


if __name__=='__main__':unittest.main(verbosity=2)
