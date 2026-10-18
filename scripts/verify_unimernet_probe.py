"""Verify output budget/termination semantics without loading model weights."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
import numpy as np
import torch
from recognition_unimernet_probe import UniMEREngine


class UniMERChecks(unittest.TestCase):
    def engine(self,tokens,expired=False):
        engine=UniMEREngine.__new__(UniMEREngine);engine.device='cpu';engine.seconds=30;engine.max_tokens=8
        def generate(**kwargs):
            self.assertIsNone(kwargs['forced_eos_token_id'])
            self.assertEqual(kwargs['max_new_tokens'],8);self.assertFalse(kwargs['do_sample'])
            self.assertEqual(kwargs['pixel_values'].shape,(1,3,32,32))
            if expired:
                with patch('recognition_unimernet_probe.time.monotonic',return_value=float('inf')):
                    kwargs['stopping_criteria'][0](None,None)
            return torch.tensor([tokens])
        engine.model=SimpleNamespace(model=SimpleNamespace(model=SimpleNamespace(generate=Mock(side_effect=generate))),
            tokenizer=SimpleNamespace(bos_token_id=0,eos_token_id=2,token2str=lambda _:['unverified']))
        engine.processor=lambda _:torch.zeros((1,32,32))
        return engine

    def test_real_eos_required_and_forced_eos_disabled(self):
        image=np.zeros((32,32,3),dtype=np.uint8);image[0,0]=255
        result=self.engine([0,3,4]).recognize(image)
        self.assertEqual(result['status'],'token_budget_exhausted');self.assertFalse(result['eos_observed'])
        result=self.engine([0,3,2]).recognize(image)
        self.assertEqual(result['status'],'candidate');self.assertFalse(result['accuracy_verified'])

    def test_expired_deadline_cannot_publish_success(self):
        image=np.zeros((32,32,3),dtype=np.uint8);image[0,0]=255
        self.assertEqual(self.engine([0,3,2],expired=True).recognize(image)['status'],'time_budget_exhausted')

    def test_blank_input_never_reaches_model(self):
        engine=self.engine([0,2])
        with self.assertRaises(ValueError):engine.recognize(np.ones((32,32,3),dtype=np.uint8))
        engine.model.model.model.generate.assert_not_called()

    def test_invalid_budget_rejects_before_weights(self):
        for tokens,seconds in ((True,30),(7,30),(513,30),(128,float('nan'))):
            with self.assertRaises(ValueError):UniMEREngine('absent',tokens,seconds)


if __name__=='__main__':unittest.main(verbosity=2)
