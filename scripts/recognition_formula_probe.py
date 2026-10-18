"""Bounded local formula experiment on explicit original PDF regions.

Run in tools/formula-venv. Outputs are unverified candidates, never study steps.
No screenshots, network inference, automatic model download or tablet actions.
"""
import argparse
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import time


def sha256(path):
    with Path(path).open('rb') as source:return hashlib.file_digest(source,'sha256').hexdigest()


def model_assets(directory):
    directory=Path(directory)
    manifest=json.loads((directory/'manifest.json').read_bytes())
    expected={'image_resizer.onnx','encoder.onnx','decoder.onnx','tokenizer.json'}
    entries=manifest.get('assets',[])
    if len(entries)!=4 or {a['name'] for a in entries}!=expected:raise ValueError('Unexpected formula assets')
    for entry in entries:
        path=directory/entry['name']
        if path.stat().st_size!=entry['size'] or sha256(path)!=entry['sha256']:
            raise ValueError('Formula model integrity mismatch')
    return manifest


class FormulaEngine:
    def __init__(self,directory,max_tokens=128,seconds=20):
        if type(max_tokens) is not int or not 8<=max_tokens<=512:raise ValueError('Invalid token budget')
        if not math.isfinite(seconds) or not 1<=seconds<=60:raise ValueError('Invalid time budget')
        self.assets=model_assets(directory);self.directory=Path(directory)
        self.max_tokens=max_tokens;self.seconds=seconds;self.deadline=0
        import cv2
        import numpy as np
        import rapid_latex_ocr.main as main
        import rapid_latex_ocr.models as models
        from rapid_latex_ocr.utils_load import OrtInferSession
        from onnxruntime import SessionOptions
        owner=self
        class LimitedSession(OrtInferSession):
            def _init_sess_opt(self):
                self.sess_opt=SessionOptions();self.sess_opt.log_severity_level=3
                self.sess_opt.intra_op_num_threads=2;self.sess_opt.inter_op_num_threads=1
                self.sess_opt.add_session_config_entry('session.intra_op.allow_spinning','0')
                self.sess_opt.add_session_config_entry('session.inter_op.allow_spinning','0')
                self.sess_opt.enable_cpu_mem_arena=False
            def __call__(self,inputs):
                if time.monotonic()>owner.deadline:raise TimeoutError('Formula inference deadline')
                result=super().__call__(inputs)
                if time.monotonic()>owner.deadline:raise TimeoutError('Formula inference deadline')
                if any(not np.isfinite(r).all() for r in result):raise ValueError('Nonfinite formula output')
                return result
        # Only this standalone process owns model construction. No package edits.
        previous=(main.OrtInferSession,models.OrtInferSession)
        started=time.perf_counter();cv2.setNumThreads(2)
        try:
            main.OrtInferSession=models.OrtInferSession=LimitedSession
            self.engine=main.LaTeXOCR(**{arg:str(self.directory/name) for arg,name in (
                ('image_resizer_path','image_resizer.onnx'),('encoder_path','encoder.onnx'),
                ('decoder_path','decoder.onnx'),('tokenizer_json','tokenizer.json'))})
        finally:main.OrtInferSession,models.OrtInferSession=previous
        self.engine.encoder_decoder.max_seq_len=max_tokens
        self.init_ms=(time.perf_counter()-started)*1000
        self.identity={'model':'RapidLaTeXOCR-v0.0.0','manifest':self.assets,
            'versions':{p:importlib.metadata.version(p) for p in
                        ('rapid-latex-ocr','onnxruntime','numpy','tokenizers','Pillow','PyMuPDF')},
            'config_sha256':sha256(main.DEFAULT_CONFIG),'max_tokens':max_tokens,
            'seconds_budget':seconds,'threads':2,'spinning':False,'provider':'CPUExecutionProvider'}

    def recognize(self,image):
        import numpy as np
        if (image.dtype!=np.uint8 or image.ndim!=3 or image.shape[2]!=3 or
            image.shape[0]*image.shape[1]>2_000_000 or min(image.shape[:2])<8):
            raise ValueError('Invalid or oversized formula input')
        if int(image.max())-int(image.min())<8:raise ValueError('Blank formula region')
        started=time.perf_counter();self.deadline=time.monotonic()+self.seconds
        np.random.seed(0)
        prepared=self.engine.loop_image_resizer(self.engine.load_img(image))
        pre_ms=(time.perf_counter()-started)*1000;decode_start=time.perf_counter()
        tokens=self.engine.encoder_decoder(prepared,temperature=self.engine.temperature)
        decode_ms=(time.perf_counter()-decode_start)*1000
        complete=bool(np.any(tokens==self.engine.encoder_decoder.eos_token))
        result=self.engine.post_process(self.engine.tokenizer.token2str(tokens)[0])
        return {'latex_candidate':result,'status':'candidate' if complete else 'token_budget_exhausted',
            'eos_observed':complete,'generated_tokens':int(tokens.size),'accuracy_verified':False,
            'timing_ms':{'preprocess':pre_ms,'encoder_decoder':decode_ms,
                         'recognition_total':(time.perf_counter()-started)*1000}}


def region_image(page,box):
    import numpy as np
    import pymupdf as fitz
    if (page.rotation or len(box)!=4 or not all(type(v) in (int,float) and math.isfinite(v) for v in box)
        or not 0<=box[0]<box[2]<=page.rect.width or not 0<=box[1]<box[3]<=page.rect.height):
        raise ValueError('Invalid formula region or page rotation')
    scale=min(3.,math.sqrt(1_500_000/((box[2]-box[0])*(box[3]-box[1]))))
    pix=page.get_pixmap(matrix=fitz.Matrix(scale,scale),clip=fitz.Rect(box),colorspace=fitz.csRGB,alpha=False)
    image=np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width,3).copy()
    return image,{'scale':scale,'pixel_origin':[pix.x,pix.y],'pixel_size':[pix.width,pix.height]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf',type=Path,required=True)
    parser.add_argument('--regions',type=Path,required=True)
    parser.add_argument('--models',type=Path,default=Path('tools/formula-models/rapid-latex-ocr-v0.0.0'))
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():parser.error('Choose a new report path')
    spec=json.loads(args.regions.read_bytes());digest=sha256(args.pdf)
    if spec['pdf_sha256']!=digest:raise ValueError('Region source mismatch')
    regions=spec['regions']
    if not 1<=len(regions)<=12:raise ValueError('Use 1..12 explicit formula regions')
    engine=FormulaEngine(args.models);results=[]
    import pymupdf as fitz
    with fitz.open(args.pdf) as document:
        for region in regions:
            page=region['page_1based']
            if type(page) is not int or not 1<=page<=len(document):raise ValueError('Invalid formula page')
            started=time.perf_counter();image,raster=region_image(document[page-1],region['box_pdf_pt'])
            raster_ms=(time.perf_counter()-started)*1000
            try:result=engine.recognize(image)
            except (TimeoutError,ValueError,RuntimeError) as error:
                result={'status':'failed','error':type(error).__name__+': '+str(error)[:240]}
            finally:del image
            result.update(source_region=region,raster=raster,raster_ms=raster_ms)
            results.append(result);print(json.dumps(result,ensure_ascii=False),flush=True)
    if sha256(args.pdf)!=digest:raise ValueError('Original changed during recognition')
    from atomic_output import atomic_bytes
    report={'source_pdf_sha256':digest,'engine':engine.identity,'model_init_ms':engine.init_ms,
        'results':results,'accuracy_verified':False,'automatic_question_attribution':False,
        'external_inference':False,'raster_files_retained':0,'tablet_actions':[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    atomic_bytes(args.output,json.dumps(report,ensure_ascii=False,indent=2).encode())


if __name__=='__main__':main()
