"""User-started continuous geometry with concurrent durable numeric processing.

No UI operations. Only explicit --continuous enables renewable geometry leases;
raw capture still requires the user and OS authorization. Ctrl+C revokes geometry
and retains resumable numeric jobs. Temporary images never enter the job store.
"""
import argparse
import hashlib
from collections import deque
from copy import deepcopy
from html import escape
import json
from pathlib import Path
import subprocess
import threading
import time

from capture_clock_history import atomic_bytes,load_clock,pull_clock
from atomic_output import publish_status
from coordinate_stream import CoordinateStream,VERSION as COORDINATE_VERSION
from coordinate_page_binding import load_binding
from coordinate_layout_check import load_calibration
from geometry_acquisition import GeometryAcquisition
from geometry_guard import RawGuardPoller
from ink_protocol import validate_session_id
from native_frame_timing import bind
from observe_coordinate_session import capture_geometry,receiver
from screen_frame_probe import NativeProbeFrames
from visual_registration import PDFRegistration
from pipeline_documents import AdditionalDocuments,configuration_extends
from edit_events import VERSION as EDIT_VERSION,process_edits,publish_edits
from edit_observations import EditObservationWorker,SharedContextReader
from study_evidence import VERSION as STUDY_VERSION,publish_study
from audit_pipeline_session import VERSION as AUDIT_VERSION,audit as audit_final_pipeline
from clock_transfer import ClockTransferWorker

ROOT=Path(__file__).resolve().parents[1]


def write_json(path,value):atomic_bytes(path,json.dumps(value,ensure_ascii=False,indent=2).encode())


class NumericProcessor:
    """One background consumer; jobs and byte offsets survive process restart."""
    def __init__(self,root,prefix,raw_directory):
        self.root,self.prefix,self.raw_directory=Path(root),prefix,Path(raw_directory)
        self.stop=threading.Event();self.thread=None
        self.last_publish={};self.last_inputs={};self.waiting_clock_since={}
        self.clock_transfers=ClockTransferWorker(prefix,pull_clock)
    def start(self):
        self.clock_transfers.start()
        self.thread=threading.Thread(target=self.run,name='incremental-coordinates',daemon=True)
        self.thread.start()
    def run(self):
        while not self.stop.is_set():
            for path in sorted(self.root.glob('session-*/job.json')):
                if self.stop.is_set():break
                try:self.process(path)
                except Exception as error:
                    publish_status(path.parent/'processing.json',{'stage':'retry_pending','error_type':type(error).__name__,
                        'error':str(error)[:240],'raw_preserved':True,'updated_at_pc':time.time()})
            self.stop.wait(.25)
    def process(self,path):
        job=json.loads(path.read_bytes());session=validate_session_id(job['session_id'])
        if (job.get('final_complete') and job.get('edits_version')==EDIT_VERSION and
            job.get('study_version')==STUDY_VERSION and job.get('coordinate_version')==COORDINATE_VERSION and
            job.get('audit_version')==AUDIT_VERSION):return
        directory=path.parent;raw=self.raw_directory/(session+'.jsonl')
        store=CoordinateStream(directory,raw,session,True,job['page_margins'])
        try:
            store.ingest()
            metadata_path=raw.with_suffix('.session.json')
            meta=json.loads(metadata_path.read_bytes()) if metadata_path.exists() else {}
            ended=meta.get('state') in ('completed','interrupted')
            closed=job.get('geometry_closed',False)
            now=time.monotonic();clock_path=directory/'clock.json'
            self.clock_transfers.request(session,clock_path,active=meta.get('state')=='recording')
            clock_transfer=self.clock_transfers.snapshot(session)
            state=load_clock(clock_path) if clock_path.exists() else None
            raw_ready=(ended and meta.get('byte_length')==store.get('raw_offset',0)==raw.stat().st_size)
            clock_ready=bool(state and state.get('history_complete',True) and not state.get('history_lost'))
            # Wait for final native worker tail, not just the current ring.
            clock_final=bool(state and state.get('stopped'))
            final=raw_ready and closed and clock_ready and clock_final
            clock_problem=None
            if raw_ready and closed and not (clock_ready and clock_final):
                since=self.waiting_clock_since.setdefault(session,now)
                if clock_transfer.get('last_transfer_succeeded') is False:
                    clock_problem='clock_transfer_failed_retrying'
                elif now-since>=30:
                    clock_problem=('clock_history_incomplete' if state and not clock_ready else
                                   'clock_stop_tail_missing' if state else 'clock_not_received')
            else:self.waiting_clock_since.pop(session,None)
            progressed=False
            inputs=(store.get('raw_offset',0),store.db.execute('SELECT MAX(id) FROM geometry').fetchone()[0],
                    hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest() if state else None,final)
            changed=self.last_inputs.get(session)!=inputs
            if state and (changed or final or store.summary()['waiting_pen_points']>2048):
                for _ in range(4):
                    if self.stop.is_set():break
                    if not store.step(state,final=final):break
                    progressed=True
                if not final and not self.stop.is_set():
                    # Idle after a stroke must not wait forever for another raw
                    # event. This bounded tail is explicitly provisional and is
                    # revised when delayed raw/clock/geometry arrives.
                    store.step(state,preview=True)
                self.last_inputs[session]=inputs
            summary=store.summary()
            edits_done=False
            if state and (not final or summary['final_complete']):
                for _ in range(4):
                    if self.stop.is_set():break
                    edits_done=process_edits(store,state,final=final)
                    if edits_done:break
            if now-self.last_publish.get(session,-1e9)>=2 or summary['final_complete']:
                store.publish();self.last_publish[session]=now
                if state:publish_edits(store,final=final and edits_done)
                if state:publish_study(store,final=final and edits_done,
                                       saved_store=ROOT/'output/notes-state/saved-notes.sqlite',
                                       text_store=ROOT/'output/recognition/page-text.sqlite')
            done=final and summary['final_complete'] and edits_done
            if done:
                if summary['raw_events']!=meta.get('point_count'):
                    raise ValueError('Final raw count differs from receiver metadata')
                store.export()
                final_audit=audit_final_pipeline(directory,raw,metadata_path)
                write_json(directory/'final-audit.json',final_audit)
                # A single atomic manifest publishes the final receipt. All raw
                # and geometry are already durable; crashes retry idempotently.
            write_json(directory/'processing.json',dict(summary,
                stage='complete' if done else 'finalizing' if final else 'clock_unavailable' if clock_problem else 'waiting_final_clock' if ended and closed else 'processing',
                raw_upload_complete=raw_ready,clock_history_complete=clock_ready,clock_stop_tail_received=clock_final,
                geometry_closed=closed,raw_state=meta.get('state','awaiting_receiver'),
                updated_at_pc=time.time(),progressed=progressed,clock_transfer=clock_transfer,
                clock_problem=clock_problem))
            if done:
                job.update(final_complete=True,finalized_at_pc=time.time(),edits_version=EDIT_VERSION,
                           study_version=STUDY_VERSION,coordinate_version=COORDINATE_VERSION,audit_version=AUDIT_VERSION)
                write_json(path,job)
        finally:store.close()
    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=55)
        self.clock_transfers.close()
        if self.thread and self.thread.is_alive():raise RuntimeError('Numeric worker still stopping; durable jobs retained')


class SessionSource:
    """Acquisition alone owns renewals, so no frame/request file races."""
    def __init__(self,frames,session,context_reader=None):
        self.frames,self.session=frames,session
        self.context_reader=context_reader
        self.requests_since_renew=0
    @property
    def cancel_event(self):return getattr(self.frames,'cancel_event',None)
    @cancel_event.setter
    def cancel_event(self,value):self.frames.cancel_event=value
    def window_context(self):return (self.context_reader or self.frames).window_context()
    @property
    def context_diagnostics(self):return getattr(self.context_reader,'last_diagnostics',{})
    def prepare(self):
        active=next((s for s in receiver() if s['session_id']==self.session),None)
        if not active or active.get('state')!='recording':
            return False
        # Native also caps requests at 100 per lease. Higher cadence must
        # renew by count as well as time, before exhausting that budget.
        if time.monotonic()-self.frames._lease_sent>=20 or self.requests_since_renew>=60:
            self.frames.renew()
            self.requests_since_renew=0
        self.requests_since_renew+=1
        return True
    def capture_payload(self):
        if not self.prepare():
            return None,{'valid':False,'reason':'user_capture_not_active'},time.monotonic()
        return self.frames.capture_payload()
    def prepare_payload(self):
        # Lease transport may block. Complete it before the sampler selects
        # the context boundary used for the actual frame request.
        return self.prepare()
    def capture_prepared_payload(self):
        # The user may stop while a fresh context boundary is being read.
        active=next((s for s in receiver() if s['session_id']==self.session),None)
        if not active or active.get('state')!='recording':
            return None,{'valid':False,'reason':'user_capture_not_active'},time.monotonic()
        return self.frames.capture_payload()
    def capture(self,title):
        if not self.prepare():
            return None,{'valid':False,'reason':'user_capture_not_active'}
        return self.frames.capture(title)


def dashboard(root,state):
    # Status pages are disposable. Their failure must not revoke the active
    # screen lease or interrupt acquisition; durable job writes still raise.
    try:
        _dashboard(root,state)
    except OSError as error:
        publish_status(root/'status.json',dict(state,updated_at_pc=time.time(),
            tablet_ui_actions=[],dashboard_error=str(error)[:240]))


def _dashboard(root,state):
    write_json(root/'status.json',dict(state,updated_at_pc=time.time(),tablet_ui_actions=[]))
    rows=[]
    for path in sorted(root.glob('session-*/job.json'),reverse=True)[:20]:
        job=json.loads(path.read_bytes());processing=path.parent/'processing.json'
        progress=json.loads(processing.read_bytes()) if processing.exists() else {}
        result=progress.get('results',{})
        rows.append('<tr><td><a href="'+path.parent.name+'/report.html">'+escape(job['session_id'][:8])+'</a> · <a href="'+path.parent.name+'/edits.html">编辑候选</a> · <a href="'+path.parent.name+'/study.html">学习过程</a></td>'+
            ''.join('<td>'+escape(str(v))+'</td>' for v in (
                progress.get('raw_state','等待记录'),progress.get('raw_upload_complete',False),
                progress.get('clock_history_complete',False),
                ('校时资料缺失，正在重试；原始记录已上传' if progress.get('stage')=='clock_unavailable' else progress.get('stage','等待处理')),
                progress.get('pen_points',0),result.get('candidate',0),result.get('extrapolated_candidate',0),
                result.get('motion_candidate',0),
                sum(n for k,n in result.items() if k not in ('candidate','extrapolated_candidate','motion_candidate')),progress.get('waiting_pen_points',0)))+'</tr>')
    labels=['会话','记录状态','上传完整','时钟完整','处理阶段','累计笔点','域内候选','外推候选','运动估计','已处理但位置未知','待处理']
    html='<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="3"><title>采集与处理状态</title><style>body{font:16px system-ui;margin:32px;background:#f3f6fa}td,th{padding:12px;border-bottom:1px solid #ccd}table{background:white;border-collapse:collapse}</style><h1>采集与处理状态</h1><p>'+escape(state.get('message',''))+'</p><p id="service-freshness">正在核对状态更新时间…</p><p>记录、上传、时钟与定位分别显示；坐标仍是候选，未知点保留。临时画面用后删除。</p><table><tr>'+''.join('<th>'+s+'</th>' for s in labels)+'</tr>'+''.join(rows)+'</table>'
    html+='<script>const updated='+json.dumps(time.time())+';function freshness(){const age=Math.max(0,Date.now()/1000-updated);const p=document.getElementById("service-freshness");p.textContent=age>45?"定位服务状态已超过45秒未更新，当前是否就绪需重新检查；上方是最后保存的状态。":"定位服务状态更新于 "+Math.round(age)+" 秒前";p.style.color=age>45?"#a32323":"#39465b";}freshness();setInterval(freshness,1000);</script>'
    atomic_bytes(root/'report.html',html.encode())


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--continuous',action='store_true',required=True,help='Explicit user-authorized ongoing debugging; no total frame/time cap')
    p.add_argument('--target',required=True);p.add_argument('--pdf',type=Path,required=True)
    p.add_argument('--hinote',type=Path,required=True);p.add_argument('--title',required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--layout-calibration',type=Path)
    p.add_argument('--pages',help='1-based comma-separated pages; default first up to 12 pages')
    p.add_argument('--interval',type=float,default=1.)
    p.add_argument('--serial-acquisition',action='store_true',help='Diagnostic fallback: one context-checked frame per cycle')
    p.add_argument('--no-motion-evidence',action='store_true',help='Disable optional numeric motion diagnostics')
    p.add_argument('--sample-interval',type=float,default=.2,help='Minimum native request interval, not a guaranteed frame rate')
    p.add_argument('--page-margins',action='store_true')
    p.add_argument('--local-refinement',action='store_true')
    p.add_argument('--native-reduction',type=int,choices=(1,2),default=2)
    p.add_argument('--additional-document',nargs=2,action='append',default=[],metavar=('PDF','HINOTE'),
                   help='Additional verified note; visible page UUIDs choose up to 12 original pages')
    p.add_argument('--discover-generated-title',action='append',default=[],help='Opt-in visual page identity discovery for a generated workbook (<=12 pages)')
    p.add_argument('--validate-only',action='store_true',help='Check original hashes and configuration compatibility without device access or capture')
    args=p.parse_args()
    if not .5<=args.interval<=10:p.error('Interval must be 0.5..10 seconds')
    if not .1<=args.sample_interval<=1:p.error('Sample interval must be 0.1..1 second')
    binding=load_binding(args.hinote,args.pdf)
    if binding['title']!=args.title:p.error('Title differs from verified document binding')
    additional=AdditionalDocuments(args.additional_document,args.title,args.local_refinement,
        args.discover_generated_title,args.output/'discovered-pages')
    capture_titles=(args.title,*additional.documents) if additional.documents else args.title
    layout=load_calibration(args.layout_calibration) if args.layout_calibration else None
    pages=[int(v)-1 for v in args.pages.split(',')] if args.pages else None
    args.output.mkdir(parents=True,exist_ok=True)
    config={'pdf_sha256':binding['source_pdf_sha256'] if 'source_pdf_sha256' in binding else None,
        'hinote_sha256':binding['source_hinote_sha256'],
        'pdf':str(args.pdf.resolve()),'hinote':str(args.hinote.resolve()),'title':args.title,
        'pages':pages,'page_margins':args.page_margins,'native_reduction':args.native_reduction,
        'local_refinement':args.local_refinement,'layout_calibration':str(args.layout_calibration.resolve()) if args.layout_calibration else None}
    config_path=args.output/'configuration.json'
    if additional.configuration:config['additional_documents']=additional.configuration
    if args.discover_generated_title:config['visual_discovery_titles']=args.discover_generated_title
    if config_path.exists() and not configuration_extends(json.loads(config_path.read_bytes()),config):
        p.error('Use a different output directory for changed configuration; only verified document additions may extend an existing pipeline')
    if args.validate_only:
        additional.close()
        print(json.dumps({'valid':True,'device_access':False,'tablet_ui_actions':[]}))
        return
    registration=PDFRegistration(args.pdf,pages,local_refinement=args.local_refinement)
    from page_motion import MotionAnchorChain
    motion_chain=MotionAnchorChain()
    frames=guard=producer=writer=processor=edit_worker=None;current=None
    try:
        frames=NativeProbeFrames(ROOT/'output/visual-scratch',args.target,(2800,1840),args.native_reduction)
        write_json(config_path,config)
        acquisition_config={'mode':'serial' if args.serial_acquisition else 'independent_payload_and_bracketed_context',
            'minimum_request_interval_seconds':args.interval if args.serial_acquisition else args.sample_interval,
            'maximum_inflight_or_queued_frames':1 if args.serial_acquisition else 3,
            'pixel_history_persisted':False,'achieved_frame_rate_verified':False}
        acquisition_config['tool_observation_mode']='independent_worker' if args.serial_acquisition else 'shared_timed_context_reader'
        acquisition_config['independent_page_exposure']=not args.serial_acquisition
        acquisition_config['motion_coordinate_version']=2
        acquisition_config['motion_coordinates']=('disabled' if args.serial_acquisition or args.no_motion_evidence
            else 'independent_exposures_with_checked_flow_interpolation')
        if not args.serial_acquisition and not args.no_motion_evidence:
            acquisition_config['motion_evidence']={'method':'opencv_pyramidal_lk_forward_backward_ransac_similarity',
                'coordinate_mapping_requires_independent_exposures':True,'temporary_gray_frame_ttl_seconds':1.5}
        write_json(args.output/'acquisition-configuration.json',acquisition_config)
        processor=NumericProcessor(args.output,frames.prefix,ROOT/'output/ink/sessions');processor.start()
        guard=RawGuardPoller(args.output/'geometry-status.json');guard.start()
        recent=deque(maxlen=32);next_status=0;armed=False;geometry_reason=None
        while True:
            try:sessions=receiver()
            except OSError:
                dashboard(args.output,{'stage':'receiver_unavailable','message':'接收器暂不可用；已保存任务保留，等待恢复。'})
                time.sleep(1);continue
            active=[s for s in sessions if s.get('state')=='recording']
            if len(active)>1:raise ValueError('Multiple active captures; refusing ambiguous geometry')
            if current:
                state=next((s for s in sessions if s['session_id']==current),None)
                metadata_path=ROOT/'output/ink/sessions'/f'{current}.session.json'
                if state is None and metadata_path.exists():state=json.loads(metadata_path.read_bytes())
                if state and state.get('state') in ('completed','interrupted'):
                    producer.close();producer=None
                    edit_worker.close();edit_worker=None
                    try:frames.revoke()
                    except (OSError,RuntimeError,subprocess.TimeoutExpired):
                        # A stopped user's raw/clock finalization must not
                        # depend on a transient route for lease cleanup.
                        geometry_reason='stopped_geometry_cleanup_retry_pending'
                    armed=False
                    job_path=args.output/('session-'+current)/'job.json'
                    job=json.loads(job_path.read_bytes());job['geometry_closed']=True;write_json(job_path,job)
                    writer.close();writer=None;current=None;recent.clear();motion_chain.reset()
                    continue
                try:captured=producer.next(timeout=.5)
                except TimeoutError:continue
                if captured is None:raise RuntimeError('Acquisition unexpectedly stopped')
                # Never publish a frame after the user has stopped the session.
                try:
                    check=next((s for s in receiver() if s['session_id']==current),None)
                except OSError:
                    captured.image=None
                    dashboard(args.output,{'stage':'receiver_unavailable','message':'接收器暂不可用；本帧作废，原始记录与数字任务保留，等待恢复。'})
                    continue
                if not check or check.get('state')!='recording':captured.image=None;continue
                started=time.monotonic()
                if additional.documents:
                    observation,result,acquired=additional.process(captured,frames,registration,args.title,layout,binding)
                else:
                    observation,result,acquired=capture_geometry(frames,registration,args.title,layout,binding,False,captured)
                geometry_reason=result.get('reason') or observation.get('reason')
                index=writer.db.execute('SELECT COALESCE(MAX(id)+1,0) FROM geometry').fetchone()[0]
                row={'index':index,'session_id_candidate':current,'observation':observation,'registration':result,
                    'source_observation_ms':round((acquired-captured.started)*1000,2),
                    'registration_queue_ms':round((started-captured.acquired)*1000,2),
                    'registration_ms':round((time.monotonic()-started)*1000,2),
                    'processing_ms':round((time.monotonic()-captured.started)*1000,2),'image_files_retained':0}
                if observation.get('page_motion') and recent:
                    from page_motion import check_visual_anchors
                    row['page_motion_anchor_check']=check_visual_anchors(recent[-1],row)
                if observation.get('page_motion'):
                    row['page_motion_chain_check']=motion_chain.observe(row)
                writer.add_geometry(row)
                recent.append(row)
                clock_path=writer.directory/'clock.json'
                if clock_path.exists():
                    try:timed,_=bind(list(recent),load_clock(clock_path));guard.frame(timed[-1],captured.guard_token)
                    except (OSError,ValueError):pass
            elif active:
                session=validate_session_id(active[0]['session_id']);directory=args.output/('session-'+session)
                job_path=directory/'job.json'
                if not armed:
                    if not job_path.exists():
                        dashboard(args.output,{'stage':'unarmed_capture','message':'当前采集开始前未启用几何通道。原始记录继续；本次无法补开高分辨率画面。'})
                        time.sleep(1);continue
                    job=json.loads(job_path.read_bytes())
                    if job.get('geometry_closed'):
                        dashboard(args.output,{'stage':'closed_geometry','message':'本次几何观察已关闭，原始记录继续。等待下一次用户采集。'})
                        time.sleep(1);continue
                    frames.attach_lease(job['lease_token']);frames._lease_sent=0
                directory.mkdir(exist_ok=True)
                job={'session_id':session,'lease_token':frames.lease_token,'page_margins':args.page_margins,
                     'geometry_closed':False,'final_complete':False,'acquisition':acquisition_config}
                write_json(job_path,job)
                writer=CoordinateStream(directory,ROOT/'output/ink/sessions'/f'{session}.jsonl',session,True,args.page_margins)
                # Restart creates an explicit invalid observation; never bridge
                # frames across an interval during which no observer was alive.
                index=writer.db.execute('SELECT COALESCE(MAX(id)+1,0) FROM geometry').fetchone()[0]
                writer.add_geometry({'index':index,'observation':{'valid':False,'reason':'observer_started_or_resumed'},
                                     'registration':{'valid':False,'reason':'observer_started_or_resumed'}})
                current=session;motion_chain.reset();frames.send_text('geometry-enable.txt','0 0 0\n')
                if args.serial_acquisition:
                    edit_worker=EditObservationWorker(directory,frames.prefix,session,
                        lambda sid:any(s['session_id']==sid and s.get('state')=='recording' for s in receiver()))
                    source=SessionSource(frames,session)
                    producer=GeometryAcquisition(source,capture_titles,guard.token,args.interval,None,None)
                else:
                    from geometry_sampling import BracketedGeometryAcquisition
                    from page_motion import PageMotionProbe
                    edit_worker=SharedContextReader(directory,frames.prefix,session,
                        lambda sid:any(s['session_id']==sid and s.get('state')=='recording' for s in receiver()))
                    source=SessionSource(frames,session,context_reader=edit_worker)
                    producer=BracketedGeometryAcquisition(source,capture_titles,guard.token,args.sample_interval,
                        motion_probe=None if args.no_motion_evidence else PageMotionProbe(),
                        independent_page_exposure=True)
            else:
                # Numeric jobs from a previous process can finalize after a
                # user stop even when the former observer died before closing.
                for path in args.output.glob('session-*/job.json'):
                    job=json.loads(path.read_bytes())
                    state=next((s for s in sessions if s['session_id']==job['session_id']),None)
                    meta=ROOT/'output/ink/sessions'/f'{job["session_id"]}.session.json'
                    if state is None and meta.exists():state=json.loads(meta.read_bytes())
                    if state and state.get('state') in ('completed','interrupted') and not job.get('geometry_closed'):
                        job['geometry_closed']=True;write_json(path,job)
                try:
                    if not armed:
                        frames.arm(continuous=True)
                        armed=True
                    elif time.monotonic()-frames._lease_sent>=20:frames.renew(arming=True)
                except (OSError,RuntimeError,subprocess.TimeoutExpired) as error:
                    # A disconnected idle tablet must not kill the supervisor.
                    # Retry the same route/lease; never open the app to recover.
                    dashboard(args.output,{'stage':'device_unavailable',
                        'error_type':type(error).__name__,
                        'message':'平板连接或几何准备暂不可用；保留数字任务，等待后台连接恢复。'})
                    time.sleep(1);continue
                time.sleep(.25)
            if time.monotonic()>=next_status:
                dashboard(args.output,{'stage':'observing' if current else 'waiting_user_capture','session_id':current,
                    'geometry_reason':geometry_reason if current else None,
                    'message':('当前笔记与已绑定材料不符或无法唯一确认；原始记录与上传继续，页面位置保留未知。'
                        if geometry_reason in ('different_or_ambiguous_document','unbound_document_or_page') else '正在并行获取画面与增量处理。')
                        if current else '已准备好；由你在采集页开始并确认系统授权，显示正在记录后回华为笔记。'})
                next_status=time.monotonic()+2
    except KeyboardInterrupt:pass
    except Exception as error:
        dashboard(args.output,{'stage':'service_failed','error_type':type(error).__name__,
            'message':'定位服务异常退出；原始记录与已保存任务保留，需恢复电脑定位服务。'})
        raise
    finally:
        try:
            if producer:producer.close()
            if edit_worker:edit_worker.close()
            if frames:frames.revoke()
        finally:
            if current:
                job_path=args.output/('session-'+current)/'job.json'
                job=json.loads(job_path.read_bytes());job['geometry_closed']=True;write_json(job_path,job)
            if writer:writer.close()
            if guard:guard.close()
            if processor:processor.close()
            if frames:frames.close()
            registration.close()
            additional.close()
            dashboard(args.output,{'stage':'service_stopped','message':'调试服务已停止，原始采集未被代为停止；数字任务保留，可重启继续处理。'})


if __name__=='__main__':main()
