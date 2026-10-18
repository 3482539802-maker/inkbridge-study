"""Bounded passive source comparison; no retained screenshots or tablet controls."""
import argparse
import json
import math
from pathlib import Path
import statistics
import subprocess
import time

from coordinate_page_binding import bind_frame, load_binding
from coordinate_alignment import affine
from visual_calibration import ROOT, TabletFrames
from visual_registration import PDFRegistration


def paired_geometry_checks(rows):
    paired=[]
    for offset in range(0,len(rows),3):
        group=rows[offset:offset+3]
        if len(group)!=3 or not all(r['registration']['valid'] for r in group):continue
        baseline=group[0]
        for other in group[1:]:
            a,b=baseline['registration'],other['registration']
            if (baseline['observation']['context']!=other['observation']['context'] or
                (a['source_pdf_sha256'],a['pdf_page_1based'])!=(b['source_pdf_sha256'],b['pdf_page_1based'])):continue
            differences=[math.dist(affine(b['pdf_to_screen'],*affine(a['screen_to_pdf'],*point)),point)
                         for point in a['support_hull_screen_px']]
            paired.append({'baseline':baseline['index'],'other':other['index'],'backend':other['backend'],
                           'max_transform_difference_on_baseline_support_screen_px':max(differences),
                           'real_pen_accuracy_verified':False})
    return paired


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target')
    p.add_argument('--pdf',type=Path)
    p.add_argument('--hinote',type=Path)
    p.add_argument('--title')
    p.add_argument('--saved',type=Path,help='Reanalyze existing numeric records without device access')
    p.add_argument('--count',type=int,default=3)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.saved:
        if args.saved.stat().st_size>12*1024*1024:p.error('Saved benchmark exceeds bound')
        report=json.loads(args.saved.read_bytes())
        report['paired_geometry_checks']=paired_geometry_checks(report['rows'])
        args.output.mkdir(parents=True,exist_ok=False)
        (args.output/'comparison.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report['paired_geometry_checks'],indent=2))
        return
    if not all((args.target,args.pdf,args.hinote,args.title)):p.error('Live comparison requires target, pdf, hinote and title')
    if not 1<=args.count<=5:p.error('count must be 1..5 per backend')
    binding=load_binding(args.hinote,args.pdf)
    if binding['title']!=args.title:p.error('Title differs from native export')
    args.output.mkdir(parents=True,exist_ok=False)
    registration=PDFRegistration(args.pdf,[0,1])
    rows=[]
    try:
        # Acquire one shared collector lock; backend switches use the same
        # fixed scratch path and identical focus/context/cleanup checks.
        collector=TabletFrames(ROOT/'output/visual-scratch',args.target)
        try:
            for index in range(args.count):
                for backend in ('uitest','snapshot_display','snapshot_display_jpeg'):
                    collector.backend=backend
                    start=time.monotonic()
                    try:
                        image,observation=collector.capture(args.title)
                    except (RuntimeError,ValueError,OSError,subprocess.TimeoutExpired) as error:
                        image=None
                        observation={'valid':False,'reason':'source_failed','diagnostic':str(error)[:300]}
                    captured=time.monotonic()
                    fit={'valid':False,'reason':observation.get('reason')}
                    try:
                        if image is not None and observation['valid']:
                            fit=registration.match(image)
                    finally:
                        del image
                    row=bind_frame({'index':len(rows),'observation':observation,'registration':fit},binding)
                    interval=observation.get('device_capture_wall_interval_ms')
                    row.update(source_ms=(captured-start)*1000,match_ms=(time.monotonic()-captured)*1000,
                               device_snapshot_interval_ms=interval[1]-interval[0] if interval else None,
                               backend=backend,retained_images=0)
                    rows.append(row)
                    print(json.dumps({k:row[k] for k in ('backend','source_ms','match_ms','device_snapshot_interval_ms')}|
                                     {'valid':row['registration']['valid'],'reason':row['registration'].get('reason')}),flush=True)
                    if not observation['valid']:
                        # A changed foreground/context ends the finite benchmark.
                        break
                if not rows[-1]['observation']['valid']:break
        finally:
            collector.close()
    finally:
        registration.close()
        summary={}
        for backend in ('uitest','snapshot_display','snapshot_display_jpeg'):
            usable=[r for r in rows if r['backend']==backend and r['registration']['valid']]
            summary[backend]={'attempts':sum(r['backend']==backend for r in rows),'valid':len(usable),
                              **{key:statistics.median(r[key] for r in usable) if usable else None
                                 for key in ('source_ms','match_ms','device_snapshot_interval_ms')}}
        paired=paired_geometry_checks(rows)
        (args.output/'comparison.json').write_text(json.dumps({'summary':summary,'rows':rows,
            'paired_geometry_checks':paired,
            'tablet_ui_actions':[],'retained_images':0,'real_pen_accuracy_verified':False},indent=2),encoding='utf-8')
        print(json.dumps(summary,indent=2),flush=True)


if __name__=='__main__':main()
