"""Bounded passive tablet snapshots, PDF alignment and prompt temporary cleanup.

Original pen journals remain the source for handwriting. Screenshots only supply
periodic geometric observations; no automatic taps, scrolling, or app launches.
"""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
import time

import cv2
import numpy as np

from notes_diagnostics import extract_tree, notes_window, summarize_tree
from visual_registration import PDFRegistration

ROOT = Path(__file__).resolve().parents[1]


class TabletFrames:
    def __init__(self, scratch, target=None, backend='uitest'):
        if backend not in ('uitest','snapshot_display','snapshot_display_jpeg'):
            raise ValueError('Unsupported geometry snapshot backend')
        self.backend=backend
        self.scratch = Path(scratch).resolve()
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.prefix = [str(ROOT/'tools/hdc/hdc.exe')]
        if target:
            self.prefix += ['-t', target]
        else:
            listing = subprocess.run(self.prefix+['list','targets','-v'],capture_output=True,
                                      encoding='utf-8',timeout=10,check=True).stdout
            targets = [c[0] for l in listing.splitlines() if len(c:=l.split())>=3 and c[2]=='Connected']
            if len(targets)!=1: raise ValueError('Expected one connected tablet')
            self.prefix += ['-t',targets[0]]
        self.remote = '/data/local/tmp/inkbridge-visual-frame.png'
        # OS lock releases on process death; do not allow two collectors to race the remote scratch file.
        self.lock = (self.scratch/'collector.lock').open('a+b')
        self.lock.seek(0)
        try:
            if __import__('os').name == 'nt':
                import msvcrt
                if self.lock.read(1) == b'': self.lock.write(b'0'); self.lock.flush()
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.cleanup()
        except BaseException:
            self.lock.close()
            raise

    def command(self, command, timeout=12):
        p = subprocess.run(self.prefix+['shell',command],capture_output=True,encoding='utf-8',errors='replace',timeout=timeout)
        if p.returncode or '[Fail]' in p.stdout:
            raise RuntimeError('Tablet diagnostic failed: '+(p.stdout+p.stderr)[:200])
        return p.stdout

    def cleanup(self):
        # Only the collector's own fixed remote file and local prefix. Never touch user notes or raw journals.
        self.command('rm -f '+self.remote+' '+self.remote.removesuffix('.png')+'.jpeg')
        for p in self.scratch.glob('inkbridge-frame-*.png'):
            if p.is_symlink() or not p.is_file() or p.resolve().parent != self.scratch:
                raise ValueError('Unexpected scratch path')
            p.unlink()

    def close(self):
        try: self.cleanup()
        finally: self.lock.close()

    def context(self, window):
        raw = self.command(f'hidumper -s WindowManagerService -a "-w {window["window_id"]} -allInfo"')
        return self.parse_context(raw, window)

    @staticmethod
    def parse_context(raw, window):
        pid = re.search(r'^Pid:\s*(\d+)', raw, re.M)
        if pid is None or int(pid[1]) != window['pid']:
            raise ValueError('Notes diagnostic PID mismatch')
        extracted = extract_tree(raw)
        tree = summarize_tree(extracted)
        # Numeric geometry and the active title only; do not archive other tab titles/canvas diagnostics.
        from notes_edit_context import summarize_edit_tools
        return {**{key: tree[key] for key in ('document_title_candidate', 'active_tab_marker_unique', 'page_layers')},
                'editing':summarize_edit_tools(extracted)}

    def window_context(self):
        """Batch fresh window and cached-ID tree reads in one HDC round trip.

        The hint only selects a diagnostic target. Every read still checks the
        current focused window/PID, and a changed hint rejects this observation.
        No cached title, geometry or focus state authorizes an image.
        """
        hint = getattr(self, '_context_window_hint', None)
        if hint is None:
            window = notes_window(self.command('hidumper -s WindowManagerService -a -a'))
            self._context_window_hint = window
            return window, self.context(window) if window['focused'] else None
        marker = 'INKBRIDGE_CONTEXT_BOUNDARY_57dced7c'
        command = ('hidumper -s WindowManagerService -a -a\n'
                   f'echo {marker}\n'
                   f'hidumper -s WindowManagerService -a "-w {int(hint["window_id"])} -allInfo"')
        raw = self.command(command)
        if len(raw) > 2*1024*1024 or raw.count(marker) != 1:
            raise ValueError('Malformed batched context observation')
        window_text, context_text = raw.split(marker)
        window = notes_window(window_text)
        self._context_window_hint = window
        if not window['focused']:
            return window, None
        if any(window[k] != hint[k] for k in ('pid','window_id','display_id')):
            raise ValueError('Notes window changed during batched context observation')
        return window, self.parse_context(context_text, window)

    def capture(self, expected_title):
        before=notes_window(self.command('hidumper -s WindowManagerService -a -a'))
        if not before['focused']:
            return None, {'valid':False,'reason':'notes_not_focused','window':before}
        initial = self.context(before)
        if not initial['active_tab_marker_unique'] or initial['document_title_candidate'] != expected_title:
            return None, {'valid':False,'reason':'different_or_ambiguous_document','window':before}
        # Device date brackets bound exposure; they are not equated to PC packet arrival times.
        start = datetime.now(timezone.utc).isoformat()
        backend=getattr(self,'backend','uitest')
        remote=self.remote.removesuffix('.png')+'.jpeg' if backend=='snapshot_display_jpeg' else self.remote
        snapshot=('snapshot_display -i '+str(before['display_id'])+' -t '+('jpeg' if backend.endswith('_jpeg') else 'png')+' -f '+remote
                  if backend.startswith('snapshot_display') else 'uitest screenCap -p '+remote)
        command = 'date +%s%3N\n'+snapshot+'\ndate +%s%3N'
        with tempfile.NamedTemporaryFile(prefix='inkbridge-frame-',suffix='.png',dir=self.scratch,delete=False) as tmp:
            local=Path(tmp.name)
        try:
            result=self.command(command)
            device_times=[int(l) for l in result.splitlines() if l.isdigit() and len(l)==13]
            p=subprocess.run(self.prefix+['file','recv',remote,str(local)],capture_output=True,timeout=12)
            if p.returncode or b'[Fail]' in p.stdout or local.stat().st_size > 24*1024*1024:
                raise RuntimeError('Screenshot transfer failed or oversized; backend='+backend+'; '+result[:200].replace('\n',' '))
            # Reject unexpected dimensions before OpenCV allocates an unbounded decoded image.
            from PIL import Image
            with Image.open(local) as header:
                if header.width*header.height > 12000000: raise ValueError('Screenshot dimensions exceed bound')
            image=cv2.imdecode(np.fromfile(local,dtype=np.uint8),cv2.IMREAD_COLOR)
            if image is None: raise ValueError('Invalid screenshot')
        finally:
            try: local.unlink(missing_ok=True)
            finally: self.command('rm -f '+remote)
        tree=self.context(before)
        after=notes_window(self.command('hidumper -s WindowManagerService -a -a'))
        from notes_edit_context import geometry_context
        stable=before==after and after['focused'] and geometry_context(initial) == geometry_context(tree)
        return image, {'valid':bool(stable),'reason':None if stable else 'window_title_or_page_geometry_changed',
                       'pc_capture_started_at':start,'pc_capture_ended_at':datetime.now(timezone.utc).isoformat(),
                       'device_capture_wall_interval_ms':device_times if len(device_times)==2 else None,
                       'window_before':before,'context_before':initial,
                       'window':after,'context':tree,'atomic_observation':False,'snapshot_backend':backend}


def write_bounded_json(path, value):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(path)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pdf',type=Path,required=True)
    p.add_argument('--title',required=True,help='Exact active Notes title for this PDF')
    p.add_argument('--pages',help='Comma-separated 1-based pages, up to 12')
    p.add_argument('--target')
    p.add_argument('--output',type=Path,required=True,help='New directory; numeric results only, no archived frames')
    p.add_argument('--interval',type=float,default=2.)
    p.add_argument('--count',type=int,default=30)
    p.add_argument('--precision',action='store_true')
    p.add_argument('--local-refinement',action='store_true')
    p.add_argument('--snapshot-backend',choices=('uitest','snapshot_display','snapshot_display_jpeg'),default='uitest')
    args=p.parse_args()
    if not math.isfinite(args.interval) or args.interval<.5 or not 1<=args.count<=1800:
        p.error('interval >= 0.5s and count 1..1800 required; actual rate limited by sequential processing')
    if args.precision and args.local_refinement:p.error('Choose one refinement mode')
    args.output.mkdir(parents=True,exist_ok=False)
    pages=[int(n)-1 for n in args.pages.split(',')] if args.pages else None
    registration=PDFRegistration(args.pdf,pages,precision=args.precision,local_refinement=args.local_refinement)
    frames=None
    metrics=[]
    try:
        frames=TabletFrames(ROOT/'output/visual-scratch',args.target,args.snapshot_backend)
        for i in range(args.count):
            t0=time.monotonic()
            frame,observation=frames.capture(args.title)
            acquired=time.monotonic()
            result={'valid':False,'reason':observation.get('reason')}
            if frame is not None and observation['valid']:
                if observation['context']['document_title_candidate']!=args.title:
                    result={'valid':False,'reason':'different_document'}
                else: result=registration.match(frame)
            del frame
            record={'index':i,'observation':observation,'registration':result,
                    'source_observation_ms':round((acquired-t0)*1000,2),
                    'registration_ms':round((time.monotonic()-acquired)*1000,2),
                    'model_response_ms':None,
                    'processing_ms':round((time.monotonic()-t0)*1000,2),'image_files_retained':0}
            write_bounded_json(args.output/'latest.json',record)
            # Retain compact geometry records only; rotate at 4 MiB, total <=8 MiB.
            history=args.output/'geometry.jsonl'
            if history.exists() and history.stat().st_size>=4*1024*1024:
                history.replace(args.output/'geometry.previous.jsonl')
            with history.open('a',encoding='utf-8') as f: f.write(json.dumps(record,ensure_ascii=False)+'\n')
            metrics.append({'valid':result['valid'],'ms':record['processing_ms'],'reason':result.get('reason')})
            print(json.dumps({'sample':i,**metrics[-1]}),flush=True)
            if i+1<args.count: time.sleep(max(0,args.interval-(time.monotonic()-t0)))
    finally:
        try:
            if frames is not None: frames.close()
        finally:
            registration.close()
            write_bounded_json(args.output/'run.json',{'samples':metrics,'tablet_ui_actions':[],
                'cloud_operations':[],'retained_images':0,'input_source':'original_pen_events',
                'visual_role':'periodic_geometry_only','requested_interval_s':args.interval,
                'raw_to_frame_time_alignment_verified':False})


if __name__=='__main__':main()
