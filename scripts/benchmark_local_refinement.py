"""Known-transform speed/accuracy comparison; no tablet I/O or retained images."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import time

import cv2
import numpy as np

from visual_registration import PDFRegistration, compose, project


def known_error(result,truth):
    if not result.get('valid'):
        return None
    hull=np.float32(result['support_hull_screen_px'])
    screen=np.array([[x,y] for x in range(200,2600,200) for y in range(250,1700,150)
                     if cv2.pointPolygonTest(hull,(float(x),float(y)),False)>=0])
    if len(screen)<6:
        return None
    pdf=project(cv2.invertAffineTransform(truth),screen)
    errors=np.linalg.norm(project(result['pdf_to_screen'],pdf)-screen,axis=1)
    return {'max_screen_px':float(max(errors)),'p90_screen_px':float(np.percentile(errors,90)), 'probes':len(screen)}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pdf',type=Path,default=Path('materials/cmc2025/nonmath-a.pdf'))
    p.add_argument('--repeats',type=int,default=3)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if not 1<=args.repeats<=5:p.error('repeats 1..5')
    args.output.mkdir(parents=True,exist_ok=False)
    matchers={name:PDFRegistration(args.pdf,[0,1],**kw) for name,kw in
              (('balanced',{}),('precision',{'precision':True}),('local',{'local_refinement':True}))}
    rows=[]
    try:
        raster,pdf_to_raster,_=matchers['precision'].reference_pixels(0)
        raster_to_pdf=cv2.invertAffineTransform(pdf_to_raster)
        for case,(sx,sy,tx,ty) in enumerate(((2.237,2.237,57.37,-152.63),(4.673,4.673,-197.19,-1221.43),(2.31,2.17,117.29,-233.13))):
            truth=np.array([[sx,0,tx],[0,sy,ty]])
            image=cv2.warpAffine(raster,compose(truth,raster_to_pdf),(2801,1840),borderValue=232)
            cv2.rectangle(image,(0,0),(2801,150),245,-1)
            cv2.putText(image,'A B C',(1000,550),cv2.FONT_HERSHEY_SCRIPT_SIMPLEX,2,20,4)
            for repeat in range(args.repeats):
                coarse=None
                for mode,matcher in matchers.items():
                    start=time.perf_counter()
                    result=matcher.match(image)
                    elapsed=(time.perf_counter()-start)*1000
                    if mode=='balanced':coarse=result
                    row={'case':case,'repeat':repeat,'mode':mode,'ms':elapsed,'valid':result['valid'],
                         'known_error':known_error(result,truth),'refinement':result.get('pixel_refinement')}
                    rows.append(row)
                    print(json.dumps(row),flush=True)
                if coarse and coarse['valid']:
                    supplied=deepcopy(coarse)
                    supplied['pdf_to_screen'][0][2]+=.8
                    supplied['pdf_to_screen'][1][2]-=.6
                    supplied['screen_to_pdf']=cv2.invertAffineTransform(np.array(supplied['pdf_to_screen'])).tolist()
                    start=time.perf_counter()
                    result=matchers['local'].refine_local(image,supplied)
                    elapsed=(time.perf_counter()-start)*1000
                    row={'case':case,'repeat':repeat,'mode':'supplied_coarse_local','ms':elapsed,'valid':result['valid'],
                         'coarse_error':known_error(supplied,truth), 'known_error':known_error(result,truth),
                         'refinement':result.get('pixel_refinement')}
                    rows.append(row)
                    print(json.dumps(row),flush=True)
    finally:
        for matcher in matchers.values():matcher.close()
    summary={}
    for mode in ('balanced','precision','local','supplied_coarse_local'):
        selected=[r for r in rows if r['mode']==mode]
        warm=[r['ms'] for r in selected if r['repeat']>0]
        summary[mode]={'valid':sum(r['valid'] for r in selected), 'cases':len(selected),
                       'accepted_refinements':sum(bool((r.get('refinement') or {}).get('accepted')) for r in selected),
                       'warm_median_ms':float(np.median(warm)) if warm else None,
                       'max_known_error_screen_px':max(r['known_error']['max_screen_px'] for r in selected if r['known_error'])}
    report={'summary':summary,'rows':rows,'evidence':'synthetic_known_transform_not_real_pen_accuracy',
            'source_acquisition_ms':None,'ocr_ms':None,'model_response_ms':None,'retained_images':0}
    (args.output/'comparison.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
