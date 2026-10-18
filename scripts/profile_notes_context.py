"""Finite read-only context timing; keeps sizes/times, never tree contents."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import time

from notes_diagnostics import notes_window, extract_tree, summarize_tree


def profile(target, count=3):
    prefix=[str(Path(__file__).resolve().parents[1]/'tools/hdc/hdc.exe'),'-t',target,'shell']
    marker='INKBRIDGE_TIMING_8b67'
    # Only fixed diagnostic options, no changes to Notes or recording state.
    listing=subprocess.run(prefix+['hidumper -s WindowManagerService -a -a'],
        capture_output=True,encoding='utf-8',errors='replace',timeout=12,check=True)
    window=notes_window(listing.stdout);rows=[]
    for _ in range(count):
        command=('date +%s%3N\n'
            'hidumper -s WindowManagerService -a -a\n'
            f'echo {marker}\ndate +%s%3N\n'
            f'hidumper -s WindowManagerService -a "-w {window["window_id"]} -allInfo"\n'
            f'echo {marker}\ndate +%s%3N')
        started=time.monotonic()
        reply=subprocess.run(prefix+[command],capture_output=True,encoding='utf-8',errors='replace',timeout=12,check=True)
        elapsed=(time.monotonic()-started)*1000
        parts=reply.stdout.split(marker)
        if len(parts)!=3:raise ValueError('Timing boundaries missing')
        stamps=[re.findall(r'^\d{13}$',p,re.M) for p in parts]
        if any(len(s)!=1 for s in stamps):raise ValueError('Ambiguous device timing')
        a,b,c=[int(s[0]) for s in stamps]
        fresh=notes_window(parts[0]);stable=all(fresh[k]==window[k] for k in ('pid','window_id','display_id'))
        parsed=time.monotonic();tree=extract_tree(parts[1]);summary=summarize_tree(tree)
        parse_ms=(time.monotonic()-parsed)*1000
        rows.append(dict(elapsed_ms=elapsed,device_windows_ms=b-a,device_tree_ms=c-b,
            pc_parse_ms=parse_ms,reply_utf8_bytes=len(reply.stdout.encode('utf-8')),
            focused=fresh['focused'],window_identity_stable=stable,
            page_layers=len(summary['page_layers']),stderr_bytes=len(reply.stderr.encode('utf-8'))))
        if not stable:break
    return dict(rows=rows,tree_contents_persisted=False,screenshots_acquired=0,tablet_actions=[])


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--target',required=True)
    p.add_argument('--count',type=int,default=3);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if not 1<=args.count<=3:p.error('Use 1..3 finite reads')
    if args.output.exists():p.error('Output already exists')
    result=profile(args.target,args.count)
    with args.output.open('x',encoding='utf-8') as f:json.dump(result,f,indent=2)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
