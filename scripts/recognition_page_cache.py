"""Local, versioned original-page text cache; never reads tablet screenshots.

Use the existing isolated recognition environment. Formula/handwriting accuracy
and question attribution remain unverified. No external inference is used.
"""
import argparse
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import sqlite3
import time

VERSION=1


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
                                    separators=(',',':'),allow_nan=False).encode()).hexdigest()


def file_hash(path):
    with Path(path).open('rb') as source:return hashlib.file_digest(source,'sha256').hexdigest()


class TextCache:
    def __init__(self,path):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS page_text(
            key TEXT PRIMARY KEY,pdf_hash TEXT,page INTEGER,created REAL,
            payload TEXT NOT NULL,payload_hash TEXT NOT NULL)''')
        self.db.execute('CREATE INDEX IF NOT EXISTS original_page ON page_text(pdf_hash,page)')

    def close(self):self.db.close()

    def get(self,request):
        row=self.db.execute('SELECT payload,payload_hash FROM page_text WHERE key=?',(digest(request),)).fetchone()
        if not row:return None
        result=json.loads(row[0])
        if digest(result)!=row[1] or result['request']!=request:raise ValueError('Text cache integrity mismatch')
        return result

    def put(self,request,result):
        if result.get('request')!=request:raise ValueError('Text result source differs from request')
        payload=json.dumps(result,ensure_ascii=False,allow_nan=False)
        if len(payload.encode())>2*1024*1024:raise ValueError('Text result exceeds bound')
        with self.db:self.db.execute('INSERT OR IGNORE INTO page_text VALUES (?,?,?,?,?,?)',
            (digest(request),request['pdf_sha256'],request['page_1based'],time.time(),payload,digest(result)))
        return self.get(request)


class LocalText:
    def __init__(self):
        import rapidocr_onnxruntime as rapid
        base=Path(rapid.__file__).parent
        assets=sorted((base/'models').glob('*.onnx'))+[base/'config.yaml']
        if len(assets)!=4 or not all(p.is_file() for p in assets):raise ValueError('Expected bundled local OCR model/config files')
        self.identity={'name':'RapidOCR-local-default-CPU',
            'versions':{n:importlib.metadata.version(n) for n in
                        ('rapidocr-onnxruntime','onnxruntime','numpy','PyMuPDF')},
            'assets':{p.name:file_hash(p) for p in assets},'profile':'package_default',
            'runtime':{'intra_op_threads':2,'inter_op_threads':1,'allow_spinning':False,'opencv_threads':2}}
        self.factory=rapid.RapidOCR;self.engine=None;self.init_ms=0

    def recognize(self,image):
        if self.engine is None:
            import cv2
            import rapidocr_onnxruntime.utils as ort_utils
            started=time.perf_counter();original=ort_utils.SessionOptions
            def options():
                value=original();value.intra_op_num_threads=2;value.inter_op_num_threads=1
                value.add_session_config_entry('session.intra_op.allow_spinning','0')
                value.add_session_config_entry('session.inter_op.allow_spinning','0')
                return value
            # This CLI owns its OCR process. Restore the factory immediately;
            # do not change the separate research environment or model files.
            cv2.setNumThreads(2)
            try:
                ort_utils.SessionOptions=options;self.engine=self.factory()
            finally:ort_utils.SessionOptions=original
            self.init_ms=(time.perf_counter()-started)*1000
        started=time.perf_counter();rows,stages=self.engine(image)
        return rows or [],(time.perf_counter()-started)*1000,stages


def process_page(document,page_number,pdf_sha,provider,cache,region=None,source_guard=None):
    import pymupdf as fitz
    if type(page_number) is not int or not 1<=page_number<=len(document):raise ValueError('Invalid original page')
    if len(pdf_sha)!=64 or any(c not in '0123456789abcdef' for c in pdf_sha):raise ValueError('Invalid original hash')
    page=document[page_number-1]
    if page.rotation:raise ValueError('Rotated original pages need a verified coordinate convention')
    box=list(region) if region else list(page.rect)
    if (len(box)!=4 or not all(isinstance(x,(int,float)) and math.isfinite(x) for x in box) or
        not 0<=box[0]<box[2]<=page.rect.width or not 0<=box[1]<box[3]<=page.rect.height):
        raise ValueError('Invalid original-page region')
    width,height=box[2]-box[0],box[3]-box[1]
    scale=min(2.,math.sqrt(5_900_000/(width*height)),8000/width,8000/height)
    request={'schema':VERSION,'pdf_sha256':pdf_sha,'page_1based':page_number,
        'region_pdf_pt':box,'page_size_pt':[page.rect.width,page.rect.height],
        'renderer':{'name':'PyMuPDF','version':importlib.metadata.version('PyMuPDF'),
                    'scale':scale,'colorspace':'RGB','alpha':False},'provider':provider.identity}
    previous=cache.get(request)
    if previous:
        if source_guard:source_guard()
        return previous,True
    began=time.perf_counter()
    words=page.get_text('words',clip=fitz.Rect(box),sort=True)
    if len(words)>20000:raise ValueError('Original text extraction exceeds bound')
    spans=[];raster_ms=recognition_ms=0;stages=None
    if sum(len(w[4].strip()) for w in words)>=20:
        method='embedded_pdf_text_unverified'
        for w in words:
            spans.append({'text':w[4],'box_pdf_pt':[float(v) for v in w[:4]],'score':None})
    else:
        import cv2
        import numpy as np
        started=time.perf_counter()
        pix=page.get_pixmap(matrix=fitz.Matrix(scale,scale),clip=fitz.Rect(box),colorspace=fitz.csRGB,alpha=False)
        if pix.width*pix.height>6_000_000:raise ValueError('Original raster exceeds bound')
        image=cv2.cvtColor(np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width,3),cv2.COLOR_RGB2BGR)
        raster_ms=(time.perf_counter()-started)*1000
        rows,recognition_ms,stages=provider.recognize(image)
        if len(rows)>2000:raise ValueError('OCR result exceeds bound')
        method='local_printed_text_ocr_candidate'
        for polygon,text,score in rows:
            xy=[[float(p[0]),float(p[1])] for p in polygon]
            if len(xy)!=4 or not all(math.isfinite(v) for p in xy for v in p):raise ValueError('Invalid OCR polygon')
            coords=[[(x+pix.x)/scale,(y+pix.y)/scale] for x,y in xy]
            spans.append({'text':str(text),'score':float(score),'polygon_pdf_pt':coords,
                'box_pdf_pt':[min(p[0] for p in coords),min(p[1] for p in coords),
                              max(p[0] for p in coords),max(p[1] for p in coords)]})
        del image,pix
    result={'schema':VERSION,'request':request,'cache_key':digest(request),'method':method,'spans':spans,
        'timing_ms':{'raster':raster_ms,'recognition':recognition_ms,'model_init_process':provider.init_ms,
                     'page_total':(time.perf_counter()-began)*1000},'reported_model_stages_seconds':stages,
        'accuracy_verified':False,'formula_recognition_verified':False,'handwriting_input':False,
        'question_attribution_verified':False,'content_role':'untrusted_study_material',
        'external_inference':False,'raster_files_retained':0}
    if source_guard:source_guard()
    return cache.put(request,result),False


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pdf',type=Path,required=True);p.add_argument('--pages',required=True)
    p.add_argument('--store',type=Path,default=Path('output/recognition/page-text.sqlite'))
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--region',nargs=4,type=float)
    args=p.parse_args();pages=[int(v) for v in args.pages.split(',')]
    if not 1<=len(pages)<=12 or len(set(pages))!=len(pages):p.error('Select 1..12 distinct original pages')
    if args.output.exists():p.error('Choose a new report path')
    import pymupdf as fitz
    signature=(args.pdf.stat().st_size,args.pdf.stat().st_mtime_ns)
    def guard():
        if signature!=(args.pdf.stat().st_size,args.pdf.stat().st_mtime_ns):raise ValueError('Original changed during recognition')
    pdf_sha=file_hash(args.pdf);guard()
    provider=LocalText();cache=TextCache(args.store);reports=[]
    try:
        with fitz.open(args.pdf) as document:
            if any(not 1<=n<=len(document) for n in pages):raise ValueError('Page outside original document')
            for n in pages:
                started=time.perf_counter()
                result,hit=process_page(document,n,pdf_sha,provider,cache,args.region,guard)
                record={'page_1based':n,'cache_hit':hit,'request_ms':(time.perf_counter()-started)*1000,
                        'cache_key':result['cache_key'],'span_count':len(result['spans']),'method':result['method'],
                        'timing_ms':result['timing_ms']}
                reports.append(record);print(json.dumps(record),flush=True)
    finally:cache.close()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as out:json.dump({'pdf_sha256':pdf_sha,'pages':reports,
        'provider':provider.identity,'accuracy_verified':False,'external_inference':False},out,indent=2)


if __name__=='__main__':main()
