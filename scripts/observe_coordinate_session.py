"""One finite, passive raw/clock/geometry session test; no tablet UI actions."""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import subprocess
import time
from receiver_status import receiver

from ink_protocol import validate_session_id
from visual_calibration import TabletFrames, write_bounded_json
from visual_registration import PDFRegistration
from coordinate_alignment import align
from geometry_guard import RawGuardPoller
from coordinate_layout_check import check as check_layout, load_calibration
from coordinate_page_binding import bind_frame, load_binding
from coordinate_context_check import reconcile
from coordinate_live import update as update_live
from native_frame_timing import bind as bind_native_times
from capture_clock_history import pull_clock, load_clock, backfill_clock
from coordinate_acceptance import audit as audit_acceptance
from geometry_acquisition import acquire, GeometryAcquisition
from geometry_pixel_mapping import lift_registration

ROOT=Path(__file__).resolve().parents[1]


def close_live_coordinates(args,session,records):
    """A finite observer has no future frames even if raw recording continues."""
    if not args.live_coordinates or not session:return
    clock_path=args.output/'clock.json'
    raw=ROOT/'output/ink/sessions'/f'{session}.jsonl'
    if not raw.exists() or not clock_path.exists():return
    try:
        progress=update_live(raw,load_clock(clock_path),records,args.output/'live-coordinates',
            stable_runs=args.stable_runs,allow_page_margins=args.page_margins,finalized=True)
    except (OSError,ValueError) as error:
        progress={'status':'final_prefix_unavailable','error_type':type(error).__name__,
                  'observation_finalized':True,'pending_future_frame_is_not_a_failure':False}
    write_bounded_json(args.output/'coordinate-progress.json',progress)


def capture_geometry(frames,registration,title,layout_calibration=None,page_binding=None,reconcile_context=False,captured=None):
    captured = captured or acquire(frames, title, 0)
    image,observation,acquired = captured.image,captured.observation,captured.acquired
    captured.image = None
    result={'valid':False,'reason':observation.get('reason')}
    try:
        from page_exposure import eligible, register
        if eligible(observation) and image is not None:
            quality=(lambda frame:check_layout(frame,layout_calibration)) if layout_calibration is not None else None
            return (*register(image,observation,registration,page_binding,quality),acquired)
        reconsider=(reconcile_context and page_binding is not None and
                    observation.get('reason')=='window_title_or_page_geometry_changed' and
                    observation.get('context_before') is not None)
        if image is not None and (observation['valid'] or reconsider):
            if (observation['valid'] and page_binding is not None and
                len((observation.get('context') or {}).get('page_layers',[]))>1):
                from multipage_geometry import match_pages
                quality_check=(lambda frame:check_layout(frame,layout_calibration)) if layout_calibration is not None else None
                result=match_pages(registration,image,observation,page_binding,quality_check)
                return observation,result,acquired
            result=registration.match(image)
            if observation.get('native_frame'):
                result=lift_registration(result,observation['native_frame'],image.shape)
            if page_binding is not None:
                frame={'observation':observation,'registration':result}
                bound=reconcile(frame,page_binding) if reconsider else bind_frame(frame,page_binding)
                observation,result=bound['observation'],bound['registration']
                if not observation.get('valid'):
                    result={**result,'valid':False,'reason':observation.get('reason')}
            if layout_calibration is not None and result.get('valid'):
                # A layout model can only veto a visual fit; it never provides
                # coordinates, revives an invalid fit or bypasses raw barriers.
                quality=check_layout({'observation':observation,'registration':result},layout_calibration)
                result={**result,'layout_consistency':quality}
                if quality['status']=='contradiction':
                    result.update(valid=False,reason=quality['reason'],visual_fit_valid_before_layout_check=True)
    finally:
        del image
    return observation,result,acquired


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target',required=True)
    p.add_argument('--pdf',type=Path,required=True)
    p.add_argument('--title',required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--seconds',type=int,default=180)
    p.add_argument('--interval',type=float,default=1.,help='Minimum sequential frame start interval, 0.5..10 seconds; actual rate limited by processing')
    p.add_argument('--max-frames',type=int,default=100,help='Finite 1..100 request budget, including failures')
    p.add_argument('--wait-seconds',type=int,default=180,
                   help='Separate finite wait for a new user-started capture; no waiting screenshots')
    p.add_argument('--session',help='Explicit current session, to resume a finite observer without restarting capture')
    p.add_argument('--attach-running',action='store_true',help='Observe a unique already-recording session at startup; never starts capture')
    p.add_argument('--precision',action='store_true',help='Full screen features and independently checked pixel refinement')
    p.add_argument('--local-refinement',action='store_true',help='Balanced features plus bounded local pixel correction')
    p.add_argument('--layout-calibration',type=Path,
                   help='Optional calibrated layout contradiction veto; never supplies coordinates')
    p.add_argument('--hinote',type=Path,help='Verified native background links for resolving multiple visible pages')
    p.add_argument('--snapshot-backend',choices=('uitest','snapshot_display','snapshot_display_jpeg','avscreen_probe'),default='uitest')
    p.add_argument('--reconcile-context',action='store_true',help='Verify both contexts using native hash/UUID binding; allow at most 0.25px UI rounding')
    p.add_argument('--stable-runs',action='store_true')
    p.add_argument('--page-margins',action='store_true',help='Distinct provisional coordinates outside feature support')
    p.add_argument('--live-coordinates',action='store_true',help='Update bounded provisional per-stroke PDF coordinates after each frame')
    p.add_argument('--serial-acquisition',action='store_true',help='Disable bounded source/registration overlap for diagnostics')
    p.add_argument('--native-reduction',type=int,choices=(1,2),default=2,help='Exact native box reduction, with pixel-centre transform to full-screen coordinates')
    args=p.parse_args()
    if not 10<=args.seconds<=300 or not 1<=args.wait_seconds<=300:
        p.error('Observation duration 10..300s and wait 1..300s required')
    if not math.isfinite(args.interval) or not .5<=args.interval<=10 or not 1<=args.max_frames<=100:
        p.error('Interval 0.5..10s and max frames 1..100 required')
    if args.precision and args.local_refinement:p.error('Choose one refinement mode')
    if (args.page_margins or args.reconcile_context) and not args.hinote:
        p.error('--hinote is required for page margins or context reconciliation')
    layout_calibration=load_calibration(args.layout_calibration) if args.layout_calibration else None
    page_binding=load_binding(args.hinote,args.pdf) if args.hinote else None
    if page_binding and page_binding['title']!=args.title:
        p.error('--title must match the verified native document')
    args.output.mkdir(parents=True,exist_ok=False)
    initial_sessions=receiver()
    baseline={s['session_id'] for s in initial_sessions}
    frames=None
    producer=None
    registration=PDFRegistration(args.pdf,[0,1],precision=args.precision,local_refinement=args.local_refinement)
    session=validate_session_id(args.session) if args.session else None
    if args.attach_running and session is None:
        active=[s for s in initial_sessions if s.get('state')=='recording']
        if len(active)>1:p.error('Multiple active sessions; specify --session')
        if active:session=validate_session_id(active[0]['session_id'])
    last_clock=-1e9
    records=[]
    start=time.monotonic()
    observation_started=start if session else None
    ended=False
    guard=RawGuardPoller(args.output/'geometry-status.json')
    write_bounded_json(args.output/'status.json',{'status':'ready','baseline_sessions':sorted(baseline),
        'started_at_pc':datetime.now(timezone.utc).isoformat(),'maximum_seconds':args.seconds,
        'maximum_wait_seconds':args.wait_seconds,'waiting_screenshots':False})
    try:
        guard.start()
        if args.snapshot_backend=='avscreen_probe':
            from screen_frame_probe import NativeProbeFrames
            # This size is verified only on this MatePad in landscape. The
            # packet validator rejects any changed orientation/resolution.
            frames=NativeProbeFrames(ROOT/'output/visual-scratch',args.target,(2800,1840),args.native_reduction)
            if session is None:frames.arm()
        else:
            frames=TabletFrames(ROOT/'output/visual-scratch',args.target,args.snapshot_backend)
        print('Ready; passive finite observer, no tablet controls',flush=True)
        while len(records)<args.max_frames and (observation_started is None or time.monotonic()-observation_started<args.seconds):
            t0=time.monotonic()
            current=receiver()
            fresh=[s for s in current if s['session_id'] not in baseline]
            if session is None and fresh:
                session=max(fresh,key=lambda s:s['started_at'])['session_id']
                observation_started=time.monotonic()
                print('New capture session '+session,flush=True)
            if session is None:
                if time.monotonic()-start>=args.wait_seconds: break
                time.sleep(.25)
                continue
            state=next((s for s in current if s['session_id']==session),None)
            if state and state.get('state') in ('completed','interrupted'):
                ended=True
                backfill_clock(frames.prefix,session,args.output/'clock.json')
                break
            if session and time.monotonic()-last_clock>=5:
                pull_clock(frames.prefix,session,args.output/'clock.json')
                last_clock=time.monotonic()
            # Clock transport is recorded separately from actual source work.
            capture_started=time.monotonic()
            token=guard.token()
            if args.snapshot_backend=='avscreen_probe' and not args.serial_acquisition:
                if producer is None:
                    producer=GeometryAcquisition(frames,args.title,guard.token,args.interval,args.max_frames,
                        max(.01,args.seconds-(time.monotonic()-observation_started)))
                captured=producer.next()
                if captured is None:break
            else:
                captured=acquire(frames,args.title,token)
            token=captured.guard_token
            registration_started=time.monotonic()
            observation,result,acquired=capture_geometry(frames,registration,args.title,layout_calibration,page_binding,
                args.reconcile_context,captured=captured)
            row={'index':len(records),'observation':observation,'registration':result,
                'session_id_candidate':session,'source_observation_ms':round((acquired-captured.started)*1000,2),
                'receiver_and_clock_ms':round((capture_started-t0)*1000,2),
                'registration_ms':round((time.monotonic()-registration_started)*1000,2),
                'acquired_to_registration_wait_ms':round(max(0,registration_started-acquired)*1000,2),
                'processing_ms':round((time.monotonic()-captured.started)*1000,2),
                'acquisition_pipelined':producer is not None,'model_response_ms':None,'image_files_retained':0}
            records.append(row)
            if args.snapshot_backend=='avscreen_probe' and (args.output/'clock.json').exists():
                records,calibration=bind_native_times(records,load_clock(args.output/'clock.json'))
                row=records[-1]
                write_bounded_json(args.output/'native-time-calibration.json',calibration)
            guard.frame(row,token)
            if args.snapshot_backend=='avscreen_probe':
                # Only numeric metadata; recalibrate earlier real frames when
                # enough independent clock anchors arrive. No new image or
                # old matrix is substituted for the observed frame.
                temporary=args.output/'geometry.jsonl.tmp'
                temporary.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records),encoding='utf-8')
                temporary.replace(args.output/'geometry.jsonl')
            else:
                with (args.output/'geometry.jsonl').open('a',encoding='utf-8') as out:
                    out.write(json.dumps(row,ensure_ascii=False)+'\n')
            write_bounded_json(args.output/'latest.json',row)
            raw=ROOT/'output/ink/sessions'/f'{session}.jsonl'
            clock_path=args.output/'clock.json'
            if args.live_coordinates and raw.exists() and clock_path.exists():
                try:
                    progress=update_live(raw,load_clock(clock_path),records,args.output/'live-coordinates',
                        stable_runs=args.stable_runs,allow_page_margins=args.page_margins)
                    write_bounded_json(args.output/'coordinate-progress.json',progress)
                except (OSError,ValueError) as error:
                    write_bounded_json(args.output/'coordinate-progress.json',
                        {'status':'awaiting_consistent_raw_clock_prefix','error_type':type(error).__name__})
            print(json.dumps({'sample':row['index'],'valid':result['valid'],'reason':result.get('reason'),
                              'session':session,'session_state':state.get('state') if state else None}),flush=True)
            time.sleep(max(0,args.interval-(time.monotonic()-t0)))
    finally:
        try:
            if producer is not None:producer.close()
            guard.close()
            if frames:
                try:
                    if args.snapshot_backend=='avscreen_probe':
                        frames.send_text('geometry-enable.txt','0 0 0\n')
                finally:
                    frames.close()
        finally:
            registration.close()
            write_bounded_json(args.output/'status.json',{'status':'completed' if ended else 'finite_observer_ended',
                'session_id':session,'samples':len(records),'retained_images':0,'tablet_ui_actions':[],
                'requested_interval_s':args.interval,'maximum_frames':args.max_frames,
                'geometry_observation_active':False,'raw_capture_stopped_by_observer':False,
                'final_session_processing_pending':bool(session) and not ended,
                'termination_reason':'session_ended' if ended else 'frame_budget_exhausted' if len(records)>=args.max_frames else
                    'waiting_expired' if session is None else 'duration_elapsed'})
            if not ended:close_live_coordinates(args,session,records)
    if ended and (args.output/'clock.json').exists():
        if args.snapshot_backend=='avscreen_probe':
            records,calibration=bind_native_times(records,load_clock(args.output/'clock.json'))
            write_bounded_json(args.output/'native-time-calibration.json',calibration)
            temporary=args.output/'geometry.jsonl.tmp'
            temporary.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records),encoding='utf-8')
            temporary.replace(args.output/'geometry.jsonl')
        summary=align(ROOT/'output/ink/sessions'/f'{session}.jsonl',
            load_clock(args.output/'clock.json'),records,args.output/'points.jsonl',
            stable_runs=args.stable_runs,allow_page_margins=args.page_margins)
        write_bounded_json(args.output/'alignment-summary.json',summary)
        if args.live_coordinates:
            progress=update_live(ROOT/'output/ink/sessions'/f'{session}.jsonl',
                load_clock(args.output/'clock.json'),records,args.output/'live-coordinates',
                stable_runs=args.stable_runs,allow_page_margins=args.page_margins,finalized=True)
            write_bounded_json(args.output/'coordinate-progress.json',progress)
        acceptance=audit_acceptance(ROOT/'output/ink/sessions'/f'{session}.jsonl',
            args.output/'clock.json',args.output/'geometry.jsonl',args.output/'points.jsonl')
        write_bounded_json(args.output/'acceptance.json',acceptance)
        print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
