"""Finite same-frame geometry comparison; no retained images or tablet UI actions."""
import argparse
import json
from pathlib import Path
import time

import numpy as np
from visual_calibration import TabletFrames,write_bounded_json
from visual_registration import PDFRegistration,project

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target',required=True)
    parser.add_argument('--pdf',type=Path,required=True)
    parser.add_argument('--title',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--count',type=int,default=3)
    parser.add_argument('--include-local',action='store_true',help='Compare bounded local correction on the same frame')
    parser.add_argument('--snapshot-backend',choices=('uitest','snapshot_display','snapshot_display_jpeg'),default='uitest')
    parser.add_argument('--wait-seconds',type=int,default=0,help='Finite wait for another collector to release its lock')
    args=parser.parse_args()
    if not 1<=args.count<=6 or not 0<=args.wait_seconds<=180:parser.error('count 1..6; wait 0..180s')
    args.output.mkdir(parents=True,exist_ok=False)
    frames=None
    matchers=[]
    report={'samples':[],'tablet_ui_actions':[],'retained_images':0,'real_pen_accuracy_verified':False}
    try:
        started=time.monotonic()
        while frames is None:
            try:frames=TabletFrames(ROOT/'output/visual-scratch',args.target,args.snapshot_backend)
            except PermissionError:
                if time.monotonic()-started>=args.wait_seconds:raise
                time.sleep(.5)
        matchers=[PDFRegistration(args.pdf,[0,1],precision=precision) for precision in (False,True)]
        if args.include_local:matchers.append(PDFRegistration(args.pdf,[0,1],local_refinement=True))
        for _ in range(args.count):
            start=time.monotonic()
            image,observation=frames.capture(args.title)
            acquired=time.monotonic()
            row={'observation':observation,'source_ms':(acquired-start)*1000,'registration':[]}
            try:
                if image is not None and observation['valid']:
                    for matcher in matchers:
                        before=time.monotonic()
                        result=matcher.match(image)
                        row['registration'].append({'mode':'local' if matcher.local_refinement else 'precision' if matcher.precision else 'balanced',
                            'ms':(time.monotonic()-before)*1000,'result':result})
            finally:del image
            report['samples'].append(row)
            print(json.dumps({'sample':len(report['samples']),
                'modes':[{'mode':r['mode'],'ms':round(r['ms'],2),'valid':r['result']['valid'],
                          'pixel_refinement':r['result'].get('pixel_refinement')} for r in row['registration']]}),flush=True)
    finally:
        try:
            if frames:frames.close()
        finally:
            for matcher in matchers:matcher.close()
            write_bounded_json(args.output/'comparison.json',report)


if __name__=='__main__':main()
