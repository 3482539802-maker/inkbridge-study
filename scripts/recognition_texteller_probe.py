"""Data-only local TexTeller adapter for the same historical-ink experiments.

Preprocessing follows the public TexTeller constants/image pipeline reviewed at
9b88cec77bda735aa16f9fc7e4ccb4eb1500a8b2 (Apache-2.0). No package web server,
remote model code, automatic downloads, or external inference is used here.
"""
import importlib.metadata
import json
import math
import os
from pathlib import Path
import time

from recognition_formula_probe import sha256


class TexTellerEngine:
    def __init__(self,directory,max_tokens=256,seconds=15):
        if type(max_tokens) is not int or not 8<=max_tokens<=512:raise ValueError('Invalid token budget')
        if not math.isfinite(seconds) or not 1<=seconds<=60:raise ValueError('Invalid time budget')
        directory=Path(directory).resolve();manifest=json.loads((directory/'manifest.json').read_bytes())
        expected={'README.md','config.json','generation_config.json','model.safetensors','tokenizer.json',
                  'tokenizer_config.json','special_tokens_map.json','vocab.json','merges.txt','added_tokens.json'}
        if (manifest.get('repo')!='OleehyO/TexTeller' or len(manifest['assets'])!=len(expected) or
            {a['name'] for a in manifest['assets']}!=expected):raise ValueError('Unexpected TexTeller assets')
        for asset in manifest['assets']:
            path=directory/asset['name']
            if path.stat().st_size!=asset['size'] or sha256(path)!=asset['sha256']:
                raise ValueError('TexTeller model integrity mismatch')
        config=json.loads((directory/'config.json').read_bytes())
        if (config.get('model_type')!='vision-encoder-decoder' or config['encoder'].get('model_type')!='vit' or
            config['decoder'].get('model_type')!='trocr' or config['encoder'].get('num_channels')!=1 or
            config['encoder'].get('image_size')!=448 or config.get('auto_map')):
            raise ValueError('Unsupported local model architecture')
        os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
        os.environ['HF_HUB_DISABLE_TELEMETRY']='1';os.environ['TOKENIZERS_PARALLELISM']='false'
        import cv2
        import torch
        from torchvision.transforms import v2
        from transformers import RobertaTokenizerFast,VisionEncoderDecoderModel
        torch.set_num_threads(2);torch.set_num_interop_threads(1);cv2.setNumThreads(2)
        started=time.perf_counter()
        self.model,loading=VisionEncoderDecoderModel.from_pretrained(str(directory),local_files_only=True,
            use_safetensors=True,output_loading_info=True)
        if any(loading.get(k) for k in ('missing_keys','unexpected_keys','mismatched_keys','error_msgs')):
            raise ValueError('Model weights did not load exactly: '+str(loading))
        self.model.eval();self.tokenizer=RobertaTokenizerFast.from_pretrained(str(directory),local_files_only=True)
        self.transform=v2.Compose([v2.ToImage(),v2.ToDtype(torch.uint8,scale=True),v2.Grayscale(),
            v2.Resize(size=447,max_size=448,interpolation=v2.InterpolationMode.BICUBIC,antialias=True),
            v2.ToDtype(torch.float32,scale=True),v2.Normalize(mean=[.9545467],std=[.15394445])])
        self.seconds=seconds;self.max_tokens=max_tokens;self.init_ms=(time.perf_counter()-started)*1000
        self.identity={'model':'TexTeller','manifest':manifest,'device':'cpu','threads':2,
            'max_tokens':max_tokens,'time_budget_seconds':seconds,'image_size':[448,448],
            'safetensors_only':True,'strict_loading_checked':True,'forced_eos_disabled':True,
            'decoder_start':'tokenizer_bos_as_upstream_explicit_GenerationConfig',
            'preprocessor_source_commit':'9b88cec77bda735aa16f9fc7e4ccb4eb1500a8b2',
            'versions':{p:importlib.metadata.version(p) for p in ('torch','torchvision','transformers','numpy')}}

    def recognize(self,image):
        from collections import Counter
        import cv2
        import numpy as np
        import torch
        from torchvision.transforms import v2
        from transformers import StoppingCriteria,StoppingCriteriaList
        if (image.dtype!=np.uint8 or image.ndim!=3 or image.shape[2]!=3 or
            image.shape[0]*image.shape[1]>2_000_000 or min(image.shape[:2])<8 or
            int(image.max())-int(image.min())<8):raise ValueError('Blank, invalid or oversized formula input')
        started=time.perf_counter();deadline=time.monotonic()+self.seconds
        corners=[tuple(image[y,x]) for y,x in ((0,0),(0,-1),(-1,0),(-1,-1))]
        background=np.full_like(image,Counter(corners).most_common(1)[0][0])
        mask=cv2.cvtColor(cv2.absdiff(image,background),cv2.COLOR_RGB2GRAY)
        x,y,w,h=cv2.boundingRect((mask>15).astype(np.uint8))
        if not w or not h:raise ValueError('Blank formula after border crop')
        tensor=self.transform(image[y:y+h,x:x+w].copy())
        tensor=v2.functional.pad(tensor,[0,0,448-tensor.shape[2],448-tensor.shape[1]]).unsqueeze(0)
        pre_ms=(time.perf_counter()-started)*1000
        class Deadline(StoppingCriteria):
            expired=False
            def __call__(self,input_ids,scores,**kwargs):
                self.expired=time.monotonic()>deadline
                return self.expired
        stop=Deadline();decode_started=time.perf_counter()
        with torch.inference_mode():
            output=self.model.generate(pixel_values=tensor,max_new_tokens=self.max_tokens,do_sample=False,
                # Upstream supplies a fresh GenerationConfig with BOS and no
                # decoder_start_token_id, which falls back to BOS. The nested
                # TrOCR training config still says EOS=2; using it here causes
                # repetitive, malformed output and is not the upstream recipe.
                num_beams=1,use_cache=True,decoder_start_token_id=self.tokenizer.bos_token_id,
                bos_token_id=self.tokenizer.bos_token_id,eos_token_id=self.tokenizer.eos_token_id,
                pad_token_id=self.tokenizer.pad_token_id,forced_eos_token_id=None,
                stopping_criteria=StoppingCriteriaList([stop]))
        tokens=output[:,1:];eos=bool((tokens==self.tokenizer.eos_token_id).any())
        text=self.tokenizer.batch_decode(tokens,skip_special_tokens=True)[0]
        return {'latex_candidate':text,'status':'time_budget_exhausted' if stop.expired else
            'candidate' if eos else 'token_budget_exhausted','eos_observed':eos,
            'generated_tokens':int(tokens.numel()),'accuracy_verified':False,
            'timing_ms':{'preprocess':pre_ms,'encoder_decoder':(time.perf_counter()-decode_started)*1000,
                         'recognition_total':(time.perf_counter()-started)*1000}}
