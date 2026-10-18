"""Formula source/raster bounds, asset integrity and truncated decoder results."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
import pymupdf as fitz
from recognition_formula_probe import model_assets,region_image,FormulaEngine


class FormulaChecks(unittest.TestCase):
    def test_model_content_mismatch_and_unexpected_asset_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);entries=[]
            for name in ('image_resizer.onnx','encoder.onnx','decoder.onnx','tokenizer.json'):
                (root/name).write_bytes(b'fixture')
                entries.append({'name':name,'size':7,'sha256':hashlib.sha256(b'fixture').hexdigest()})
            manifest=root/'manifest.json';manifest.write_text(json.dumps({'assets':entries}))
            self.assertEqual(len(model_assets(root)['assets']),4)
            (root/'encoder.onnx').write_bytes(b'changed')
            with self.assertRaises(ValueError):model_assets(root)
            entries[0]['name']='../outside.onnx';manifest.write_text(json.dumps({'assets':entries}))
            with self.assertRaises(ValueError):model_assets(root)

    def test_raster_bounds_and_rotation_never_silently_clip(self):
        with fitz.open() as doc:
            page=doc.new_page(width=300,height=200)
            image,meta=region_image(page,[10,20,110,70])
            self.assertEqual(meta['pixel_origin'],[30,60]);self.assertEqual(image.shape,(150,300,3))
            for box in ([-1,0,100,100],[0,0,301,100],[0,0,0,100],[0,float('nan'),100,100]):
                with self.assertRaises(ValueError):region_image(page,box)
            page.set_rotation(90)
            with self.assertRaises(ValueError):region_image(page,[0,0,100,100])

    def engine(self,tokens):
        e=FormulaEngine.__new__(FormulaEngine);e.seconds=2
        decoder=Mock(return_value=np.array([tokens]));decoder.eos_token=2
        e.engine=SimpleNamespace(load_img=lambda x:x,loop_image_resizer=lambda x:x,
            encoder_decoder=decoder,temperature=.00001,
            tokenizer=SimpleNamespace(token2str=lambda t:['test']),post_process=lambda x:x)
        return e

    def test_no_eos_marks_truncation_not_a_complete_candidate(self):
        image=np.zeros((30,60,3),dtype=np.uint8);image[1,1]=255
        result=self.engine([3,4,5]).recognize(image)
        self.assertEqual(result['status'],'token_budget_exhausted');self.assertFalse(result['eos_observed'])
        result=self.engine([3,4,2]).recognize(image)
        self.assertEqual(result['status'],'candidate');self.assertFalse(result['accuracy_verified'])

    def test_blank_and_oversized_input_never_reaches_model(self):
        e=self.engine([2])
        for image in (np.full((30,60,3),255,dtype=np.uint8),np.zeros((1500,1500,3),dtype=np.uint8),
                      np.zeros((30,60),dtype=np.uint8)):
            with self.assertRaises(ValueError):e.recognize(image)
        e.engine.encoder_decoder.assert_not_called()

    def test_invalid_budgets_reject_before_any_model_access(self):
        for tokens,seconds in ((True,2),(7,2),(513,2),(128,float('nan')),(128,61)):
            with self.assertRaises(ValueError):FormulaEngine('absent',tokens,seconds)


if __name__=='__main__':unittest.main(verbosity=2)
