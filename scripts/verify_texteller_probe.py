"""Output termination and image bounds without loading model weights."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
import numpy as np
import torch
from recognition_texteller_probe import TexTellerEngine


class TexTellerChecks(unittest.TestCase):
    def engine(self,tokens,expired=False):
        e=TexTellerEngine.__new__(TexTellerEngine);e.seconds=15;e.max_tokens=8
        e.transform=lambda _:torch.zeros((1,32,80))
        e.tokenizer=SimpleNamespace(bos_token_id=0,eos_token_id=2,pad_token_id=1,
                                   batch_decode=lambda *a,**k:['unverified'])
        def generate(**kwargs):
            self.assertEqual(tuple(kwargs['pixel_values'].shape),(1,1,448,448))
            self.assertIsNone(kwargs['forced_eos_token_id'])
            self.assertEqual(kwargs['decoder_start_token_id'],0)
            if expired:
                with patch('recognition_texteller_probe.time.monotonic',return_value=float('inf')):
                    kwargs['stopping_criteria'][0](None,None)
            return torch.tensor([tokens])
        e.model=SimpleNamespace(generate=Mock(side_effect=generate),
                               config=SimpleNamespace(decoder=SimpleNamespace(decoder_start_token_id=2)))
        return e

    def test_generated_eos_required_and_timeout_wins(self):
        image=np.full((32,80,3),255,dtype=np.uint8);image[10:20,10:60]=0
        self.assertEqual(self.engine([0,4,5]).recognize(image)['status'],'token_budget_exhausted')
        result=self.engine([0,4,2]).recognize(image)
        self.assertEqual(result['status'],'candidate');self.assertFalse(result['accuracy_verified'])
        self.assertEqual(self.engine([0,4,2],True).recognize(image)['status'],'time_budget_exhausted')

    def test_blank_or_oversized_input_never_calls_model(self):
        engine=self.engine([2,4,2])
        for image in (np.zeros((32,32,3),dtype=np.uint8),np.zeros((2000,2000,3),dtype=np.uint8)):
            with self.assertRaises(ValueError):engine.recognize(image)
        engine.model.generate.assert_not_called()

    def test_invalid_budget_rejected_before_reading_assets(self):
        for tokens,seconds in ((True,15),(7,15),(513,15),(256,float('nan'))):
            with self.assertRaises(ValueError):TexTellerEngine('absent',tokens,seconds)


if __name__=='__main__':unittest.main(verbosity=2)
