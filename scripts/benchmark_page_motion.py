"""Known synthetic motions on local original pages; no tablet or image output."""
import argparse,hashlib,json
from pathlib import Path

import cv2
import fitz
import numpy as np

from page_motion import PageMotionProbe
from visual_registration import project
from verify_page_motion import context,metadata
from audit_frame_runtime import quantiles


def benchmark(pdf,pages):
    results=[];times=[]
    with fitz.open(pdf) as document:
        for page in pages:
            pix=document[page-1].get_pixmap(matrix=fitz.Matrix(1.5,1.5),alpha=False)
            image=cv2.cvtColor(np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width,3),cv2.COLOR_RGB2BGR)
            h,w=image.shape[:2];window,ctx=context(w*2,h*2)
            for name,scale,dx,dy,angle in [('still',1.,0,0,0),('pan',1.,11,-17,0),
                    ('zoom',1.025,-10,-8,0),('fast_pan',1.,60,0,0),('rotation_rejected',1.,0,0,4)]:
                matrix=cv2.getRotationMatrix2D((w/2,h/2),angle,scale);matrix[:,2]+=[dx,dy]
                changed=cv2.warpAffine(image,matrix,(w,h),borderValue=(255,255,255))
                cv2.line(changed,(w//3,h//3),(w//3+60,h//3+30),(20,20,20),3)
                probe=PageMotionProbe()
                try:
                    probe.observe(image,metadata(0,w*2,h*2,2),window,ctx)
                    out=probe.observe(changed,metadata(1,w*2,h*2,2),window,ctx)
                    times.append(out['processing_ms']);fitted=out['pages'][0]
                    row=dict(page_1based=page,case=name,candidate=fitted['candidate'],reason=fitted.get('reason'),
                        processing_ms=out['processing_ms'],validation_p90_screen_px=fitted.get('validation_p90_screen_px'))
                    if fitted['candidate']:
                        lift=np.array([[2,0,.5],[0,2,.5]],float)
                        from visual_registration import compose
                        expected=compose(compose(lift,matrix),cv2.invertAffineTransform(lift))
                        support=np.asarray(fitted['support_hull_previous_screen_px'])
                        row['known_transform_max_support_error_screen_px']=float(max(np.linalg.norm(
                            project(expected,support)-project(fitted['previous_screen_to_current_screen'],support),axis=1)))
                    if name in ('still','pan','zoom') and not fitted['candidate']:
                        raise ValueError('Baseline synthetic motion failed: '+str(row))
                    if fitted['candidate'] and row['known_transform_max_support_error_screen_px']>2:
                        raise ValueError('Wrong known transform accepted: '+str(row))
                    if name=='rotation_rejected' and fitted['candidate']:
                        raise ValueError('Unsupported rotation accepted')
                    results.append(row)
                finally:probe.close()
    return dict(source='synthetic_warps_of_local_original_not_tablet_writing',pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
        opencv_version=cv2.__version__,cases=results,processing_ms=quantiles(times),images_saved=0,
        physical_pen_accuracy_verified=False,real_motion_verified=False,coordinate_mapping_allowed=False)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pdf',type=Path,required=True)
    p.add_argument('--pages',type=int,nargs='+',required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    result=benchmark(args.pdf,args.pages);args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as f:json.dump(result,f,indent=2)
    print(json.dumps(result,indent=2))
