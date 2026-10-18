"""Bounded local OCR timing on an original PDF; no tablet or external inference.

Keeps raster inputs in memory. This is text OCR, not formula recognition or a
validated live Notes integration. Thread settings apply only to this process.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import statistics
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3, choices=range(2, 11))
    parser.add_argument('--profile', choices=('baseline', 'no-upscale'), default='baseline')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists; choose a new result path')
    import cv2
    import pymupdf as fitz
    import numpy as np
    import onnxruntime as ort
    from rapidocr_onnxruntime import RapidOCR
    import rapidocr_onnxruntime.utils as ort_utils

    started = time.perf_counter()
    with fitz.open(args.pdf) as doc:
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        image = cv2.cvtColor(np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3), cv2.COLOR_RGB2BGR)
    raster_ms = (time.perf_counter() - started) * 1000
    h, w = image.shape[:2]
    # Explicit fixed upper-page region, not an automatically established question.
    region = [int(w * .08), int(h * .18), int(w * .92), int(h * .38)]
    x0, y0, x1, y1 = region
    inputs = [('full_page', image), ('fixed_region', image[y0:y1, x0:x1].copy())]
    report = {'scope': 'local printed text OCR only; no formula accuracy or live end-to-end claim',
              'pdf_sha256': hashlib.sha256(args.pdf.read_bytes()).hexdigest(),
              'page_1based': 1, 'raster_ms': raster_ms, 'region_pixels': region,
              'python': platform.python_version(), 'versions': {},
              'profile': args.profile,
              'available_providers': ort.get_available_providers(), 'runs': []}
    for name in ['rapidocr-onnxruntime', 'onnxruntime', 'numpy', 'PyMuPDF']:
        report['versions'][name] = importlib.metadata.version(name)
    original_options = ort_utils.SessionOptions
    try:
        for threads in ((0, 4) if args.profile == 'baseline' else (0,)):
            def options():
                value = original_options()
                value.intra_op_num_threads = threads
                value.inter_op_num_threads = 1
                return value
            ort_utils.SessionOptions = options
            t0 = time.perf_counter()
            engine = (RapidOCR() if args.profile == 'baseline' else
                      RapidOCR(det_model_path=None, det_limit_type='max', det_limit_side_len=2048))
            init_ms = (time.perf_counter() - t0) * 1000
            print(json.dumps({'threads': threads, 'init_ms': init_ms}), flush=True)
            for label, pixels in inputs:
                trials = []
                for index in range(args.repeats + 1):
                    t0 = time.perf_counter()
                    result, stages = engine(pixels)
                    elapsed = (time.perf_counter() - t0) * 1000
                    trials.append({'elapsed_ms': elapsed, 'reported_stages_seconds': stages,
                                   'boxes': len(result or [])})
                warm = [x['elapsed_ms'] for x in trials[1:]]
                record = {'threads': threads, 'input': label, 'size': list(pixels.shape[:2]),
                          'model_init_ms': init_ms, 'first_call_ms': trials[0]['elapsed_ms'],
                          'warm_median_ms': statistics.median(warm), 'warm_max_ms': max(warm),
                          'trials': trials,
                          'recognized_text': [{'text': r[1], 'score': float(r[2])} for r in (result or [])],
                          'accuracy_verified': False}
                report['runs'].append(record)
                print(json.dumps({k: record[k] for k in ('threads', 'input', 'first_call_ms', 'warm_median_ms', 'warm_max_ms')}), flush=True)
            del engine
    finally:
        ort_utils.SessionOptions = original_options
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as destination:
        json.dump(report, destination, ensure_ascii=False, indent=2)
    print(str(args.output.resolve()))


if __name__ == '__main__':
    main()
