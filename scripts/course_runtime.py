"""Local course workflow: service supervision, activity reports and tutor queue.

No tablet UI actions. Existing live services are adopted; an alive capture
pipeline is never replaced during recording. Only stopped/absent processes can
be recovered without a live-session restart. User import and consent stay user
actions. This service is an ordinary local process, not a boot-time guarantee.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import ctypes
from html import escape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit, unquote
from urllib.request import urlopen

from course_workbook import ROOT, bind, save, sha256
from course_learning import LearningStore, verified_package


def read_json(path): return json.loads(Path(path).read_bytes())


def windows_args(command):
    if os.name != 'nt':
        import shlex
        return shlex.split(command)
    shell32 = ctypes.windll.shell32
    shell32.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    count = ctypes.c_int()
    argv = shell32.CommandLineToArgvW(command, ctypes.byref(count))
    if not argv: raise ValueError('Cannot parse Windows process command')
    try: return [argv[i] for i in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


def inventory():
    if os.name != 'nt': return []
    # No owner filenames/content are enumerated; only relevant service commands.
    command = "$taskRows = @(Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe' OR Name='pwsh.exe'\" | Select-Object ProcessId,CommandLine); [Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($taskRows | ConvertTo-Json -Compress))))"
    result = subprocess.run(['pwsh', '-NoProfile', '-Command', command], cwd=ROOT,
                            capture_output=True, timeout=12,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode: raise RuntimeError('Cannot inspect service processes')
    data = decode_inventory(result.stdout)
    if isinstance(data, dict): data = [data]
    return [{'pid': v['ProcessId'], 'args': windows_args(v['CommandLine'])}
            for v in data if v.get('CommandLine')]


def decode_inventory(payload):
    # ASCII envelope prevents the host console codepage corrupting Chinese paths.
    # Inspection failure is never interpreted as "all services absent".
    if not isinstance(payload, bytes) or not payload.strip():
        raise RuntimeError('Empty process inventory response')
    try:
        rows = json.loads(base64.b64decode(payload.strip(), validate=True).decode('utf-8'))
    except (ValueError, UnicodeError) as error:
        raise RuntimeError('Invalid process inventory response') from error
    if not rows or not isinstance(rows, (list, dict)):
        raise RuntimeError('Empty or invalid process inventory')
    return rows


def find_processes(processes, script):
    wanted = (ROOT/script).resolve()
    result = []
    for process in processes:
        if any((ROOT/value).resolve() == wanted for value in process['args'][1:]
               if not value.startswith('-')):
            result.append(process)
    return result


def receiver_state():
    try:
        with urlopen('http://127.0.0.1:18766/status', timeout=2) as response:
            data = response.read(1024*1024+1)
        state = json.loads(data)
        if len(data) > 1024*1024 or not isinstance(state.get('sessions'), list): return None
        return state
    except (OSError, ValueError): return None


def pipeline_action(current, desired, capture_state, stage, fresh):
    """Pure decision: absence permits recovery; changing a live process is gated."""
    if len(current) > 1: return 'ambiguous_processes'
    if not current: return 'start_missing'
    if current[0]['args'][1:] == desired[1:]: return 'adopt_existing'
    if capture_state is None: return 'wait_receiver'
    if any(s.get('state') == 'recording' for s in capture_state['sessions']): return 'wait_user_stop'
    if not fresh or stage not in ('waiting_user_capture', 'device_unavailable'): return 'wait_idle_evidence'
    return 'reconfigure_idle'


def desired_pipeline(config, courses):
    settings = config['capture']
    target = read_json(ROOT/'output/device-connection.json').get('wireless_address') or settings.get('target')
    if not target: raise ValueError('No remembered tablet endpoint')
    args = [config.get('python', sys.executable), '-X', 'utf8', 'scripts/session_pipeline.py', '--continuous',
            '--target', target, '--pdf', settings['pdf'], '--hinote', settings['hinote'],
            '--title', settings['title'], '--output', settings['output'], '--local-refinement', '--page-margins']
    if settings.get('layout_calibration'): args += ['--layout-calibration', settings['layout_calibration']]
    extras = list(settings.get('additional_documents', []))
    for course in courses:
        path = course.get('binding')
        if not path or not Path(path).exists(): path = course.get('native_import_binding')
        if not path or not Path(path).exists(): continue
        binding, manifest = verified_package(path, allow_generated=True)
        pair = [manifest['workbook_pdf'], binding['hinote_path']]
        if pair not in extras: extras.append(pair)
    if len(extras) > 3: raise ValueError('Capture pipeline currently supports at most three additional documents')
    for pair in extras: args += ['--additional-document', *pair]
    for course in courses:
        if course.get('visual_identity_discovery') and course.get('native_import_binding'):
            binding,_=verified_package(course['native_import_binding'],allow_generated=True)
            args += ['--discover-generated-title',binding['native']['title']]
    return args


def start_background(args, log_root, name):
    log_root.mkdir(parents=True, exist_ok=True)
    stamp = str(time.time_ns())
    with (log_root/(name+'-'+stamp+'.stdout.log')).open('ab') as stdout, (log_root/(name+'-'+stamp+'.stderr.log')).open('ab') as stderr:
        return subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0)


def validate_pipeline_start(args):
    # A rejected output configuration must never cost an already healthy
    # service. This path loads local originals only, no HDC or screen access.
    result=subprocess.run([*args,'--validate-only'],cwd=ROOT,capture_output=True,timeout=30,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    return result.returncode==0


class Supervisor:
    def __init__(self, config, root):
        self.config, self.root = config, root
        self.last = 0; self.status = {}; self.children = []

    def tick(self, courses):
        if time.monotonic()-self.last < 15: return self.status
        self.last = time.monotonic()
        processes = inventory(); state = receiver_state()
        receivers = find_processes(processes, 'scripts/ink_receiver.py')
        if not state:
            if not receivers:
                command = [self.config.get('python', sys.executable), '-X', 'utf8', 'scripts/ink_receiver.py', '--seconds', '0']
                child = start_background(command, self.root/'logs', 'receiver'); self.children.append(child)
                receiver_status = 'recovering'
            else: receiver_status = 'process_alive_endpoint_unavailable'
        else: receiver_status = 'ready'
        pipelines = find_processes(processes, 'scripts/session_pipeline.py')
        # Separate output roots are not implicitly supervised or terminated.
        expected_output = Path(self.config['capture']['output']).resolve()
        pipelines = [p for p in pipelines if '--output' in p['args'] and
                     (ROOT/p['args'][p['args'].index('--output')+1]).resolve() == expected_output]
        status_path = expected_output/'status.json'
        status = read_json(status_path) if status_path.exists() else {}
        fresh = time.time()-status.get('updated_at_pc', 0) < 45
        desired = desired_pipeline(self.config, courses)
        action = pipeline_action(pipelines, desired, state, status.get('stage'), fresh)
        if action == 'start_missing' and receiver_status != 'ready': action = 'wait_receiver'
        if action == 'reconfigure_idle' and not self.config.get('allow_idle_pipeline_reconfigure', False): action = 'configuration_waiting'
        if action in ('start_missing', 'reconfigure_idle'):
            if not validate_pipeline_start(desired): action='configuration_rejected'
        if action in ('start_missing', 'reconfigure_idle'):
            if action == 'reconfigure_idle':
                # Recheck receiver immediately before touching this known idle PID.
                check = receiver_state()
                if check is None or any(s.get('state') == 'recording' for s in check['sessions']):
                    action = 'wait_user_stop'
                else:
                    os.kill(pipelines[0]['pid'], signal.SIGTERM)
            if action in ('start_missing', 'reconfigure_idle'):
                child = start_background(desired, self.root/'logs', 'pipeline'); self.children.append(child)
                action = 'starting_missing' if action == 'start_missing' else 'starting_reconfigured_idle'
        watchers = find_processes(processes, 'watch-tablet.ps1')
        if not watchers:
            child = start_background(['pwsh', '-NoProfile', '-File', str(ROOT/'watch-tablet.ps1')], self.root/'logs', 'connection')
            self.children.append(child)
        self.children = [p for p in self.children if p.poll() is None]
        self.status = {'receiver': receiver_status, 'pipeline_action': action,
                       'pipeline_stage': status.get('stage'), 'pipeline_status_fresh': fresh,
                       'recording': bool(state and any(s.get('state') == 'recording' for s in state['sessions'])),
                       'checked_at_pc': time.time(), 'tablet_ui_actions': []}
        return self.status


def publish_dashboard(root, courses, store, status):
    from course_knowledge import publish_knowledge
    knowledge_links = []
    for course in courses:
        try: knowledge_links.extend(publish_knowledge(store, [course]))
        except (OSError,ValueError,KeyError,TypeError,sqlite3.Error) as error:
            status.setdefault('knowledge_errors',{})[course['id']] = {'error_type':type(error).__name__, 'reason':str(error)[:240]}
            knowledge_links.append({'id':course['id'], 'url':'/knowledge/'+course['id']+'/index.html', 'stale':True,
                                    'available':(root/'knowledge'/course['id']/'index.html').exists()})
    cards = []
    for course in courses:
        try: manifest = read_json(course['manifest']); cid = manifest['course']['id']
        except (OSError,ValueError,KeyError,TypeError):
            cards.append('<section><p>课程材料暂时无法读取：'+escape(course['id'])+'。其他课程继续运行。</p></section>');continue
        binding_ready = bool(course.get('binding') and Path(course['binding']).exists())
        report_path = root/f'{cid}-activity.json'
        report = read_json(report_path) if report_path.exists() else {'questions': []}
        rows = []
        for q in report['questions']:
            rows.append('<tr>'+''.join('<td>'+escape(str(value))+'</td>' for value in
                        (q['id'], round(q['observed_writing_seconds'], 2), len(q['step_candidates']),
                         q['contact_kinds'].get('pen_contact_pen', 0),
                         '尚无记录' if not q['contact_kinds'] else '进行中' if q['provisional'] else '已处理'))+'</tr>')
        package = Path(course['manifest']).parent
        # A reusable library may reference materials in a different workspace.
        # Those files stay outside HTTP; original crops are copied to knowledge/assets.
        relative = package.relative_to(root).as_posix() if root in package.parents else None
        workbook_links = (f' <a href="/{relative}/preview.html">题目预览</a> · <a href="/{relative}/workbook.pdf">作答题册</a>'
                          if relative is not None else '')
        native_path = course.get('native_workbook')
        native_link = ''
        if native_path and Path(native_path).exists() and root in Path(native_path).resolve().parents:
            native_link = f' · <a href="/{Path(native_path).relative_to(root).as_posix()}">原生作答本 .hinote</a>'
        pending = course.get('native_import_binding')
        wait_text = ('原生作答本已生成，等待你导入并打开；后台自动核对页面身份，无需先导出。'
                     if pending and Path(pending).exists() else '等待你导入题册，再导出一次 .hinote 以建立页面绑定。')
        if course.get('visual_identity_discovery'):
            wait_text = '原生作答本已导入；开始记录后，用同期原题画面自动确认新页面身份，无需重复导入或预先导出。'
        knowledge = next(k for k in knowledge_links if k['id'] == cid)
        knowledge_url = knowledge['url']
        knowledge_message = ('<p>知识库本轮未更新，请检查课程材料或配置。'+
                             ('可继续阅读上次生成的版本。' if knowledge['available'] else '')+'</p>') if knowledge.get('stale') else ''
        cards.append(f'<section><h2>{escape(manifest["course"]["title"])}</h2>'+
                     f'<p><a class="knowledge-link" href="{escape(knowledge_url, quote=True)}">打开课程知识库：原题 · 笔迹 · 建议 · 解答 · 复练 →</a></p><p>'+
                     ('已核对笔记页面身份；后续每页仍按精确 UUID 匹配。' if binding_ready else wait_text)+
                     workbook_links+'</p>'+knowledge_message+
                     native_link+'<table><tr><th>题目</th><th>实际书写秒数</th><th>片段候选</th><th>笔接触</th><th>状态</th></tr>'+''.join(rows)+'</table></section>')
    tasks = []
    from course_review import publish_request, publish_feedback
    for key, course, question, state, request, result in store.db.execute(
            "SELECT id,course,question,status,request_path,result_path FROM tutor_jobs WHERE status!='superseded' ORDER BY created DESC LIMIT 50"):
        req = publish_request(request).relative_to(root).as_posix()
        detail = {'pending':'等待当前聊天辅导', 'completed':'反馈已入库', 'failed':'处理待重试', 'running':'正在处理'}.get(state, state)
        task = f'<li>{escape(course)} / {escape(question)}：{escape(detail)} · <a href="/{req}">证据包</a>'
        if result: task += ' · <a href="/'+publish_feedback(result).relative_to(root).as_posix()+'">反馈</a>'
        tasks.append(task+'</li>')
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="10"><title>本地学习工作台</title><style>body{font:16px/1.6 system-ui;max-width:1100px;margin:28px auto;padding:0 18px;background:#f4f6fa;color:#243249}section{background:white;padding:22px;margin:18px 0;border-radius:12px}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:8px;border-bottom:1px solid #dde3ed}a{color:#225ca1}</style><h1>本地学习工作台</h1>'''
    html += '<p id="freshness"></p><p>自行开始记录并确认系统授权后，照常使用华为笔记。书写时间按设备事件计算；停笔不直接算成思考，片段尚不是语义步骤。</p>'
    html += ''.join(cards)+'<section><h2>待辅导与反馈</h2><p>本机整理证据，由当前聊天处理；未配置外部模型调用。</p><ul>'+''.join(tasks or ['<li>还没有可归属到本题册的记录。</li>'])+'</ul></section>'
    html += '<script>const updated='+str(time.time())+';function freshness(){let age=Date.now()/1000-updated;document.getElementById("freshness").textContent=age>45?"工作台已超过45秒未更新，请检查本机服务。":"更新于 "+Math.round(age)+" 秒前"}freshness();setInterval(freshness,1000)</script></html>'
    from atomic_output import atomic_bytes
    atomic_bytes(root/'index.html', html.encode())
    save(root/'status.json', dict(status, updated_at_pc=time.time(), courses=len(courses), tablet_ui_actions=[]))


class Runtime:
    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve(); self.config = read_json(config_path)
        self.root = Path(self.config['root']).resolve(); self.root.mkdir(parents=True, exist_ok=True)
        self.store = LearningStore(self.root); self.supervisor = Supervisor(self.config, self.root)
        self.recognition = None; self.recognition_request = None; self.last_export_check = 0
        self.recognition_started = 0
        self.last_import_check = 0
        self.quiet_questions = {}
        # A crashed endpoint request must be retryable, without claiming exactly-once billing.
        with self.store.db:
            self.store.db.execute("UPDATE tutor_jobs SET status='failed',next_retry=?,error='worker_interrupted' WHERE status='running'", (time.time()+30,))

    def discover_exports(self, courses, recording):
        if recording or time.monotonic()-self.last_export_check < 30: return
        self.last_export_check = time.monotonic()
        waiting = [c for c in courses if c.get('native_export_name') and not Path(c['binding']).exists()
                   and (not c.get('native_import_binding') or c.get('allow_export_fallback') is True)]
        if not waiting: return
        from tablet_files import Hdc, public_path
        try:
            hdc = Hdc(ROOT/'tools/hdc/hdc.exe', read_json(ROOT/'output/device-connection.json').get('wireless_address'))
            names = hdc.list(public_path('file://docs/storage/Users/currentUser', 100))
            for course in waiting:
                name = course['native_export_name']
                if name not in names: continue
                destination = self.root/'imports'/(str(time.time_ns())+'.hinote'); destination.parent.mkdir(exist_ok=True)
                hdc.pull(public_path(name, 100), destination)
                if course.get('native_workbook') and sha256(destination) == sha256(course['native_workbook']):
                    raise ValueError('Outgoing generated archive cannot prove a user export or import')
                bind(course['manifest'], destination, course['binding'])
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            save(self.root/'export-check.json', {'status': 'waiting', 'error_type': type(error).__name__, 'checked_at_pc': time.time()})

    def discover_native_imports(self, courses):
        waiting = [c for c in courses if c.get('native_import_binding') and
                   Path(c['native_import_binding']).exists() and not Path(c['binding']).exists()]
        if not waiting or time.monotonic()-self.last_import_check < 30: return
        self.last_import_check = time.monotonic()
        # Do not add another expensive context reader to an active writing session.
        state = receiver_state()
        if state is None or any(s.get('state') == 'recording' for s in state['sessions']): return
        from hinote_workbook import confirm_import
        from notes_diagnostics import collect
        try:
            target = read_json(ROOT/'output/device-connection.json').get('wireless_address')
            with tempfile.TemporaryDirectory(prefix='inkbridge-import-') as temporary:
                context = collect(ROOT/'tools/hdc/hdc.exe', target, Path(temporary)/'context')
            # Keep only the successful minimal identity receipt, not full UI trees.
            for course in waiting:
                try: confirm_import(course['native_import_binding'], context, course['binding'])
                except (ValueError, KeyError): continue
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            save(self.root/'native-import-check.json', {'status': 'waiting_for_native_import',
                 'error_type': type(error).__name__, 'checked_at_pc': time.time(), 'tablet_ui_actions': []})

    def adopt_visual_bindings(self, courses):
        from course_learning import canonical_hash
        for course in courses:
            if not course.get('visual_identity_discovery'): continue
            pending,manifest=verified_package(course['native_import_binding'],allow_generated=True)
            import hashlib
            key=hashlib.sha256(pending['native']['title'].encode()).hexdigest()[:16]
            path=Path(self.config['capture']['output'])/'discovered-pages'/(key+'.json')
            if not path.exists(): continue
            value=read_json(path)
            if (value.get('schema')!='inkbridge-visual-page-binding-v1' or
                value.get('title')!=pending['native']['title'] or
                value.get('source_pdf_sha256')!=manifest['workbook_sha256'] or
                value.get('source_hinote_sha256')!=pending['native']['source_hinote_sha256']):
                raise ValueError('Visual page discovery source changed')
            pages=value['pages']
            if not pages and not value.get('blocked_page_ids'): continue
            available={p['workbook_page_1based']:p['question_id'] for p in manifest['pages']}
            if (len(pages)>len(available) or len(set(pages.values()))!=len(pages) or
                any(type(n) is not int or n not in available for n in pages.values())):
                raise ValueError('Ambiguous visual page assignment')
            for page,n in pages.items():
                evidence=value.get('evidence',{}).get(page,{})
                a,b=evidence.get('first',{}),evidence.get('second',{})
                if (page in value.get('blocked_page_ids',[]) or a.get('page')!=n or b.get('page')!=n or
                    a.get('identity')!=b.get('identity') or not .1 <= b.get('time',0)-a.get('time',0) <= 5):
                    raise ValueError('Two agreeing original observations required')
            result=dict(pending,notes_import_verified=True,binding_origin='visual_original_page_candidates',
                binding_basis='two_original_feature_fits_with_fresh_native_canvas_UUID',
                native=dict(pending['native'],pages=pages,note_id=value['local_association_id'],live_note_id_verified=False),
                page_questions={page:available[n] for page,n in pages.items()},
                visual_identity_evidence=value,private_database_binding_verified=False)
            revision=self.root/'bindings'/(course['id']+'-'+canonical_hash(result)[:24]+'.json')
            if not revision.exists(): save(revision,result)
            if course['binding'] != str(revision):
                course['binding']=str(revision)
                save(self.config_path,self.config)

    def recognition_tick(self):
        from course_tutor import add_answer_evidence
        if self.recognition is not None:
            if self.recognition.poll() is None:
                if time.monotonic()-self.recognition_started <= 300: return
                self.recognition.terminate()
                self.recognition.wait(timeout=5)
            request = self.recognition_request
            result = request.with_suffix('.recognition.json')
            if self.recognition.returncode == 0 and result.exists():
                recognized = read_json(result)
                add_answer_evidence(self.store, request, recognized)
                if not recognized.get('images'):
                    save(request.with_suffix('.recognition-outcome.json'),
                         {'status':'insufficient_ink_evidence', 'retry':'new_evidence_revision_required',
                          'request_id':read_json(request)['id']})
            else:
                save(request.with_suffix('.recognition-failed.json'), {'status': 'worker_failed', 'exit_code': self.recognition.returncode})
            self.recognition = None; self.recognition_request = None
        settings = self.config.get('recognition', {})
        if not settings.get('enabled', True): return
        rows = self.store.db.execute("SELECT request_path FROM tutor_jobs WHERE status='pending' ORDER BY created").fetchall()
        for (path,) in rows:
            request = Path(path); value = read_json(request)
            if value.get('recognition_status') != 'pending': continue
            if request.with_suffix('.recognition-outcome.json').exists(): continue
            result_path = request.with_suffix('.recognition.json')
            if result_path.exists():
                # Recover a completed worker result after a dashboard crash.
                recognized = read_json(result_path)
                add_answer_evidence(self.store, request, recognized)
                if not recognized.get('images'):
                    save(request.with_suffix('.recognition-outcome.json'),
                         {'status':'insufficient_ink_evidence', 'retry':'new_evidence_revision_required',
                          'request_id':value['id']})
                continue
            if request.with_suffix('.recognition-failed.json').exists(): continue
            command = [settings.get('python', sys.executable), '-X', 'utf8', 'scripts/course_tutor.py', '--request', str(request), '--output', str(request.with_suffix('.recognition.json'))]
            command += ['--backend', settings.get('backend', 'current_chat')]
            if settings.get('model_directory'): command += ['--model-directory', settings['model_directory']]
            self.recognition = start_background(command, self.root/'logs', 'recognition')
            self.recognition_request = request
            self.recognition_started = time.monotonic()
            break

    def tick(self):
        self.config = read_json(self.config_path); self.supervisor.config = self.config
        courses = self.config['courses']; status = {}
        if self.config.get('supervise_capture', True):
            try: status['capture'] = self.supervisor.tick(courses)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                status['capture'] = {'error_type': type(error).__name__, 'attention_required': True}
        self.discover_exports(courses, status.get('capture', {}).get('recording', False))
        self.discover_native_imports([c for c in courses if not c.get('visual_identity_discovery')])
        self.adopt_visual_bindings(courses)
        for course in courses:
            if not course.get('binding') or not Path(course['binding']).exists(): continue
            try:
                for path in sorted(Path(self.config['capture']['output']).glob('session-*/pipeline.sqlite')):
                    self.store.ingest(path.parent, course['binding'])
                report = self.store.report(course['binding'])
                ready = []
                from course_learning import canonical_hash
                for question in report['questions']:
                    key = (course['id'], question['id']); fingerprint = canonical_hash(question)
                    previous = self.quiet_questions.get(key)
                    if not previous or previous[0] != fingerprint:
                        self.quiet_questions[key] = (fingerprint, time.monotonic())
                        previous = self.quiet_questions[key]
                    # Avoid emitting every stroke as a tutoring request. A quiet
                    # provisional fragment is explicit and later superseded by final evidence.
                    if not question['provisional'] or time.monotonic()-previous[1] >= 15:
                        ready.append(question)
                queued = dict(report, questions=ready)
                self.store.enqueue(queued, course['binding'], include_provisional=True)
                self.store.profile(report['course']['id'])
            except (OSError, ValueError, sqlite3.Error) as error:
                status[course['id']] = {'error_type': type(error).__name__, 'status': 'waiting_for_valid_evidence'}
        self.recognition_tick()
        incoming = self.root/'tutor/incoming'; incoming.mkdir(parents=True, exist_ok=True)
        pending_inputs = [p for p in sorted(incoming.glob('*.json'))
                          if not p.name.endswith('.receipt.json') and not p.with_suffix('.receipt.json').exists()]
        for path in pending_inputs[:16]:
            receipt = path.with_suffix('.receipt.json')
            if path.name.endswith('.receipt.json') or receipt.exists(): continue
            try:
                response = read_json(path)
                if not isinstance(response,dict): raise ValueError('Feedback must be an object')
                job = self.store.db.execute('SELECT request_path,status FROM tutor_jobs WHERE id=?', (response.get('request_id'),)).fetchone()
                if not job: raise ValueError('Unknown local feedback request')
                if job[1] != 'completed':
                    result = self.store.accept_feedback(job[0], response, {'mode':'current_chat_local_review'})
                    self.store.profile(result['course']['id'])
                else:
                    # A different late file must not receive a false accepted receipt.
                    stored = self.store.db.execute('SELECT result_path FROM tutor_jobs WHERE request_path=?', (job[0],)).fetchone()
                    if read_json(stored[0])['response'] != response:
                        raise ValueError('Completed feedback differs from incoming payload')
                save(receipt, {'status':'accepted','feedback_sha256':sha256(path)})
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
                save(receipt, {'status':'rejected','error_type':type(error).__name__, 'reason':str(error)[:240]})
        from course_knowledge import process_incoming
        process_incoming(self.store, courses)
        publish_dashboard(self.root, courses, self.store, status)
        return status

    def close(self):
        # Capture/receiver processes intentionally survive closing the dashboard.
        self.store.close()


@contextmanager
def single_instance(root):
    """Kernel file lock releases on crash; an old PID file never blocks recovery."""
    path = Path(root)/'runtime.lock'; path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if not path.stat().st_size: stream.write(b'0'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            try: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error: raise RuntimeError('Learning runtime is already active') from error
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try: yield
        finally:
            stream.seek(0)
            if os.name == 'nt': msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(stream, fcntl.LOCK_UN)


def make_server(root, port, config_path=None):
    root = Path(root).resolve()
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs): super().__init__(*args, directory=str(root), **kwargs)
        def log_message(self, *args): pass
        def allowed_path(self):
            if self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
                self.send_error(403); return False
            path = (root/unquote(urlsplit(self.path).path).lstrip('/')).resolve()
            if (root not in (path, *path.parents) or path.suffix in ('.sqlite', '.lock', '.log')
                or path.name.endswith(('-wal', '-shm')) or path.name == 'runtime.json'):
                self.send_error(403); return False
            return True
        def do_GET(self):
            if not self.allowed_path(): return
            super().do_GET()
        def do_HEAD(self):
            if not self.allowed_path(): return
            super().do_HEAD()
        def do_POST(self):
            if not self.allowed_path(): return
            origins = (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}')
            if (self.headers.get('Origin') not in origins or self.headers.get('Content-Type') != 'application/json'
                or urlsplit(self.path).path != '/api/knowledge/action' or not config_path):
                self.send_error(403); return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 96000: raise ValueError('Invalid action size')
                self.connection.settimeout(5)
                data = json.loads(self.rfile.read(length))
                from course_knowledge import action
                config = read_json(config_path)
                store = LearningStore(root)
                try: event = action(store, config['courses'], data)
                finally: store.close()
                result = {'status':'saved', 'event':event}
                if event['kind'] == 'generate':
                    result.update(request_path=str(root/'knowledge/queue'/(event['id']+'.request.json')),
                                  incoming_path=str(root/'knowledge/incoming'/(event['id']+'.batch.json')))
                code = 200
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
                code = 400; result = {'error':str(error)[:240]}
            payload = json.dumps(result,ensure_ascii=False).encode()
            self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(payload)))
            self.end_headers(); self.wfile.write(payload)
        def list_directory(self, path):
            self.send_error(403); return None
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True); p.add_argument('--once', action='store_true')
    p.add_argument('--accept-feedback', type=Path); p.add_argument('--request', type=Path)
    args = p.parse_args(); config = read_json(args.config)
    if args.accept_feedback:
        store = LearningStore(config['root'])
        try:
            result = store.accept_feedback(args.request, read_json(args.accept_feedback), {'mode': 'current_chat_local_review'})
            store.profile(result['course']['id']); print(json.dumps({'request_id': result['request_id'], 'status': 'completed'}))
        finally: store.close()
        return
    with single_instance(config['root']):
        runtime = Runtime(args.config); server = None
        try:
            if args.once: print(json.dumps(runtime.tick(), ensure_ascii=False)); return
            server = make_server(runtime.root, config.get('port', 18771), args.config)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            while True:
                try: runtime.tick()
                except Exception as error:
                    save(runtime.root/'runtime-error.json', {'error_type': type(error).__name__, 'at_pc': time.time()})
                time.sleep(5)
        except KeyboardInterrupt: pass
        finally:
            if server: server.shutdown(); server.server_close()
            runtime.close()


if __name__ == '__main__': main()
