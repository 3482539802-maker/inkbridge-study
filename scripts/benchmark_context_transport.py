"""Compare passive context HDC round trips; acquires zero screen frames."""
import argparse
import json
from pathlib import Path
import statistics
import time

from notes_diagnostics import notes_window
from visual_calibration import ROOT, TabletFrames


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target',required=True)
    p.add_argument('--count',type=int,default=3)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if not 1<=args.count<=5:p.error('Use 1..5 finite paired context observations')
    if args.output.exists():p.error('Output already exists')
    collector=TabletFrames(ROOT/'output/visual-scratch',args.target)
    rows=[]
    try:
        # Warm only the window-ID hint; no cached observation authorizes a read.
        collector.window_context()
        for i in range(args.count):
            pair=[]
            for mode in (('separate','batched') if i%2==0 else ('batched','separate')):
                started=time.monotonic()
                if mode=='batched':
                    window,context=collector.window_context()
                else:
                    window=notes_window(collector.command('hidumper -s WindowManagerService -a -a'))
                    context=collector.context(window) if window['focused'] else None
                elapsed=(time.monotonic()-started)*1000
                pair.append({'mode':mode,'elapsed_ms':elapsed,'focused':window['focused'],
                             'window':window,'context':context})
            consistent=pair[0]['window']==pair[1]['window'] and pair[0]['context']==pair[1]['context']
            rows.extend({'pair':i,'mode':r['mode'],'elapsed_ms':r['elapsed_ms'],
                         'focused':r['focused'],'paired_context_equal':consistent} for r in pair)
            if not consistent or not all(r['focused'] for r in pair):break
    finally:
        collector.close()
    medians={m:statistics.median(r['elapsed_ms'] for r in rows if r['mode']==m) for m in ('separate','batched')}
    report={'rows':rows,'median_context_read_ms':medians,
            'estimated_two_checks_saving_ms':2*(medians['separate']-medians['batched']),
            'estimate_is_full_frame_benchmark':False,'screenshots_acquired':0,'tablet_ui_actions':[]}
    with args.output.open('x',encoding='utf-8') as out:json.dump(report,out,indent=2)
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
