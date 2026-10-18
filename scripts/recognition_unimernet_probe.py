"""Isolated local UniMERNet provider for explicit formula experiments."""
import importlib.metadata
import json
import math
import os
from pathlib import Path
import time

from recognition_formula_probe import sha256


class UniMEREngine:
    def __init__(self,directory,max_tokens=128,seconds=30):
        if type(max_tokens) is not int or not 8<=max_tokens<=512:raise ValueError('Invalid token budget')
        if not math.isfinite(seconds) or not 1<=seconds<=60:raise ValueError('Invalid time budget')
        directory=Path(directory).resolve();manifest=json.loads((directory/'manifest.json').read_bytes())
        variants={'wanderkid/unimernet_tiny':'tiny','wanderkid/unimernet_base':'base'}
        if manifest.get('repo') not in variants:raise ValueError('Unexpected UniMERNet repository')
        variant=variants[manifest['repo']];checkpoint_name='unimernet_'+variant+'.pth'
        expected={'config.json','configuration.json','preprocessor_config.json','tokenizer.json',
                  'tokenizer_config.json',checkpoint_name,'README.md'}
        if len(manifest['assets'])!=len(expected) or {a['name'] for a in manifest['assets']}!=expected:
            raise ValueError('Unexpected UniMERNet asset manifest')
        for a in manifest['assets']:
            path=directory/a['name']
            if path.stat().st_size!=a['size'] or sha256(path)!=a['sha256']:raise ValueError('UniMERNet model integrity mismatch')
        # Force all model/tokenizer reads to the verified local directory.
        os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
        os.environ['HF_HUB_DISABLE_TELEMETRY']='1';os.environ['TOKENIZERS_PARALLELISM']='false'
        os.environ['NO_ALBUMENTATIONS_UPDATE']='1'
        import torch
        import cv2
        from omegaconf import OmegaConf
        from unimernet.models.unimernet.unimernet import UniMERModel
        from unimernet.processors.formula_processor import FormulaImageEvalProcessor
        torch.set_num_threads(2);torch.set_num_interop_threads(1);cv2.setNumThreads(2)
        started=time.perf_counter()
        model=UniMERModel(model_name='unimernet',
            model_config=OmegaConf.create({'model_name':str(directory),'max_seq_len':max_tokens}),
            tokenizer_name='nougat',tokenizer_config=OmegaConf.create({'path':str(directory)}))
        checkpoint=torch.load(directory/checkpoint_name,map_location='cpu',weights_only=True)
        state=checkpoint['model'] if 'model' in checkpoint else checkpoint
        model.load_state_dict(state,strict=True);del checkpoint,state
        self.model=model.eval();self.device='cpu'
        config=json.loads((directory/'preprocessor_config.json').read_bytes())
        self.processor=FormulaImageEvalProcessor(config['size'])
        self.seconds=seconds;self.max_tokens=max_tokens
        self.init_ms=(time.perf_counter()-started)*1000
        self.identity={'model':'UniMERNet-'+variant,'manifest':manifest,'device':self.device,
            'versions':{p:importlib.metadata.version(p) for p in ('unimernet','torch','torchvision','transformers','numpy','albumentations')},
            'max_tokens':max_tokens,'time_budget_seconds':seconds,'threads':2,
            'image_size':config['size'],
            'weights_only_load':True,'strict_state_dict':True,'forced_eos_disabled':True}

    def recognize(self,image):
        import numpy as np
        import torch
        from PIL import Image
        from transformers import StoppingCriteria,StoppingCriteriaList
        if (image.dtype!=np.uint8 or image.ndim!=3 or image.shape[2]!=3 or
            image.shape[0]*image.shape[1]>2_000_000 or min(image.shape[:2])<8 or
            int(image.max())-int(image.min())<8):raise ValueError('Blank, invalid or oversized formula input')
        deadline=time.monotonic()+self.seconds
        class Deadline(StoppingCriteria):
            expired=False
            def __call__(self,input_ids,scores,**kwargs):
                self.expired=time.monotonic()>deadline
                return self.expired
        stop=Deadline();started=time.perf_counter()
        tensor=self.processor(Image.fromarray(image)).unsqueeze(0).to(self.device)
        if tensor.shape[1]==1:tensor=tensor.repeat(1,3,1,1)
        pre_ms=(time.perf_counter()-started)*1000;decode_start=time.perf_counter()
        with torch.inference_mode():
            # Upstream DonutEncoderDecoder.generate ignores kwargs; invoke
            # the same inner model so the deadline and EOS settings take effect.
            output=self.model.model.model.generate(pixel_values=tensor,max_new_tokens=self.max_tokens,
                decoder_start_token_id=self.model.tokenizer.bos_token_id,do_sample=False,
                forced_eos_token_id=None,stopping_criteria=StoppingCriteriaList([stop]))
        tokens=output[:,1:];decode_ms=(time.perf_counter()-decode_start)*1000
        eos=bool((tokens==self.model.tokenizer.eos_token_id).any())
        text=self.model.tokenizer.token2str(tokens)[0]
        return {'latex_candidate':text,'status':'time_budget_exhausted' if stop.expired else
            'candidate' if eos else 'token_budget_exhausted','eos_observed':eos,
            'generated_tokens':int(tokens.numel()),'accuracy_verified':False,
            'timing_ms':{'preprocess':pre_ms,'encoder_decoder':decode_ms,
                         'recognition_total':(time.perf_counter()-started)*1000}}
