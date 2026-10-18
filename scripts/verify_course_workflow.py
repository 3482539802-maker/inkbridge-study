"""Source preservation, identity, activity timing, queue revisions and supervision.

All native exports and contacts made here are synthetic test fixtures, never
real device evidence. Production originals and journals are opened read-only.
"""
from copy import deepcopy
import base64
from contextlib import closing
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

import fitz

from course_workbook import build, bind, propose, save, sha256, validate
from course_learning import LearningStore, attribute, verified_package
from course_runtime import make_server, pipeline_action, windows_args, find_processes, decode_inventory, ROOT
from course_tutor import add_answer_evidence, endpoint_call, run_one
from hinote_workbook import generate, confirm_import


def synthetic_export(pdf, path, pages=2, wrong_hash=False, duplicate=False):
    payload = pdf.read_bytes(); digest = sha256(pdf)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('files/background.pdf', payload)
        record = {'customNoteContent': {'id': 'test-note', 'noteTitle': 'Synthetic workbook',
                  'attachment': [{'id': 'background', 'filePath': 'background.pdf'}]},
                  'fileList': [{'name': 'background.pdf', 'hash': '0'*64 if wrong_hash else digest}]}
        archive.writestr('note.jhinote', gzip.compress(json.dumps(record).encode()))
        for i in range(pages):
            record = {'customNotePageContent': {'notesId': 'test-note', 'id': 'page-1' if duplicate else f'page-{i+1}',
                      'pageNumber': i+1, 'bkgAttachmentId': 'background', 'bkgAttachmentIndex': i}}
            archive.writestr(f'page-{i}.jhinote', gzip.compress(json.dumps(record).encode()))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ink-course-'); self.root = Path(self.temp.name)
        self.source = self.root/'source.pdf'
        with fitz.open() as pdf:
            page = pdf.new_page(width=400, height=500)
            page.insert_text((30, 35), '1. Integrate x squared.'); page.insert_text((30, 200), 'ANSWER: hidden reference')
            page = pdf.new_page(width=400, height=500)
            page.insert_text((30, 35), '2. Prove the inequality.')
            pdf.save(self.source)
        self.original_hash = sha256(self.source)
        self.plan = {'schema': 'inkbridge-question-plan-v1', 'course': {'id': 'math', 'title': 'Synthetic math'},
                     'source_pdf': str(self.source), 'source_pdf_sha256': self.original_hash, 'review': 'confirmed',
                     'questions': [{'id': 'q1', 'label': 'Question 1', 'parts': [{'page': 1, 'box': [20, 15, 380, 80]}], 'tags': ['integration'], 'review': 'confirmed'},
                                   {'id': 'q2', 'label': 'Question 2', 'parts': [{'page': 2, 'box': [20, 15, 380, 80]}], 'review': 'confirmed'}]}
        self.plan_path = self.root/'plan.json'; save(self.plan_path, self.plan)
        self.package = self.root/'package'; self.manifest = build(self.plan_path, self.package)
        self.native = self.root/'synthetic.hinote'; synthetic_export(self.package/'workbook.pdf', self.native)
        self.binding_path = self.root/'binding.json'
        self.binding = bind(self.package/'manifest.json', self.native, self.binding_path)
        self.store = LearningStore(self.root/'learning')
        self.session = self.root/'session'; self.session.mkdir()
        save(self.session/'job.json', {'session_id': 'test-session'})
        db = sqlite3.connect(self.session/'pipeline.sqlite')
        db.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE edit_events(id TEXT PRIMARY KEY,kind TEXT,payload TEXT);')
        db.execute('INSERT INTO meta VALUES (?,?)', ('edit_detector_version', '7')); db.commit(); db.close()

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def event(self, start=1, end=2, lo=0, hi=2_000_000, **kwargs):
        event = {'session_id': 'test-session', 'start_seq': start, 'end_seq': end,
                 'start_us': lo, 'end_us': hi, 'kind': 'pen_contact_pen', 'provisional': False,
                 'document_title': 'Synthetic workbook', 'page_id': 'page-1', 'points': 2,
                 'context': {'page_id': 'page-1', 'page_ids': ['page-1']},
                 'location_evidence': {'scope': 'within_bound_page_candidate', 'page_id': 'page-1',
                                       'source_pdf_sha256': self.manifest['workbook_sha256'], 'pdf_page_1based': 1}}
        event.update(kwargs); return event

    def add_events(self, events):
        with closing(sqlite3.connect(self.session/'pipeline.sqlite')) as db, db:
            for e in events:
                db.execute('INSERT INTO edit_events VALUES (?,?,?)', (str(e['start_seq']), e['kind'], json.dumps(e)))

    def report(self, events=None):
        if events: self.add_events(events)
        self.store.ingest(self.session, self.binding_path)
        return self.store.report(self.binding_path)

    def queue(self):
        report = self.report([self.event()]); self.store.enqueue(report, self.binding_path)
        return Path(self.store.db.execute('SELECT request_path FROM tutor_jobs').fetchone()[0])

    def response(self, request):
        return {'request_id': request['id'], 'evidence_hash': request['evidence_hash'],
                'observations': [{'text': 'Observed two seconds of writing contact.', 'evidence_ids': request['available_evidence_ids']}],
                'hints': [], 'next_practice': [], 'needs_clarification': [], 'correctness': 'not_assessed'}

    def test_original_unchanged_and_answer_pixels_excluded(self):
        self.assertEqual(sha256(self.source), self.original_hash)
        with fitz.open(self.package/'workbook.pdf') as pdf:
            self.assertEqual(len(pdf), 2)
            # Cropped pixels preserve visible formulas while excluding even
            # hidden full-source resources. Prompt OCR/search is not promised.
            self.assertNotIn('hidden', pdf[0].get_text())
            crop = fitz.Pixmap(pdf, pdf[0].get_images()[0][0])
            self.assertEqual((crop.width, crop.height), (1080, 195))
            self.assertTrue(any(v != 255 for v in crop.samples))
            pix = pdf[0].get_pixmap(clip=fitz.Rect(50, 400, 550, 780))
            self.assertEqual(set(pix.samples), {255})

    def test_source_to_workbook_mapping_has_no_guess(self):
        page = self.manifest['pages'][0]; a,b,c,d,e,f = page['source_to_workbook_affine']
        self.assertAlmostEqual(20*a+e, page['prompt_box_pdf_pt'][0])
        self.assertAlmostEqual(15*d+f, page['prompt_box_pdf_pt'][1])

    def test_changed_source_rejected(self):
        self.source.write_bytes(self.source.read_bytes()+b'changed')
        with self.assertRaises(ValueError): validate(self.plan, self.source)

    def test_unreviewed_plan_only_makes_draft(self):
        self.plan['review'] = 'needs_review'; save(self.plan_path, self.plan)
        with self.assertRaises(ValueError): build(self.plan_path, self.root/'unapproved')
        draft = build(self.plan_path, self.root/'draft', preview=True)
        self.assertFalse(draft['deliverable'])

    def test_plan_rejects_invalid_duplicate_and_outside_regions(self):
        for mode in ('duplicate', 'nan', 'page', 'outside'):
            plan = deepcopy(self.plan)
            if mode == 'duplicate': plan['questions'][1]['id'] = 'q1'
            elif mode == 'nan': plan['questions'][0]['parts'][0]['box'][0] = float('nan')
            elif mode == 'page': plan['questions'][0]['parts'][0]['page'] = 3
            else: plan['questions'][0]['parts'][0]['box'][2] = 999
            with self.subTest(mode=mode), self.assertRaises(ValueError): validate(plan, self.source)

    def test_automatic_proposals_are_not_approved(self):
        proposal = propose(self.source, 'math', 'Math')
        self.assertEqual(proposal['review'], 'needs_review')
        self.assertTrue(all(q['review'] == 'needs_review' for q in proposal['questions']))

    def test_real_pdf_hash_and_all_native_uuids_needed(self):
        self.assertEqual(self.binding['page_questions'], {'page-1': 'q1', 'page-2': 'q2'})
        for mode in ('hash', 'missing', 'duplicate'):
            path = self.root/(mode+'.hinote')
            synthetic_export(self.package/'workbook.pdf', path, pages=1 if mode == 'missing' else 2,
                             wrong_hash=mode == 'hash', duplicate=mode == 'duplicate')
            with self.subTest(mode=mode), self.assertRaises(ValueError): bind(self.package/'manifest.json', path, self.root/(mode+'.json'))

    def test_manifest_tamper_invalidates_binding(self):
        manifest = read(self.package/'manifest.json'); manifest['course']['title'] = 'changed'; save(self.package/'manifest.json', manifest)
        with self.assertRaises(ValueError): verified_package(self.binding_path)

    def test_missing_coordinates_can_have_separate_page_context_candidate(self):
        event = self.event(location_evidence={'scope': 'partial_or_unknown'})
        self.assertEqual(attribute(event, self.binding), ('q1', 'single_page_context_candidate'))

    def test_page_context_cannot_override_wrong_source_or_title_or_ambiguity(self):
        for field in ('source', 'title', 'ambiguous', 'unknown_uuid'):
            e = self.event(location_evidence={'scope': 'partial_or_unknown'})
            if field == 'source': e['location_evidence']['source_pdf_sha256'] = 'a'*64
            elif field == 'title': e['document_title'] = 'Other note'
            elif field == 'ambiguous': e['context']['page_ids'].append('page-2')
            else: e['page_id'] = 'other'
            with self.subTest(field=field): self.assertIsNone(attribute(e, self.binding)[0])

    def test_timing_uses_interval_union_and_not_pause_as_thought(self):
        events = [self.event(), self.event(3,4,1_000_000,3_000_000), self.event(5,6,20_000_000,21_000_000)]
        report = self.report(events); q = report['questions'][0]
        self.assertEqual(q['observed_writing_seconds'], 4)
        self.assertEqual(q['first_to_last_contact_span_seconds_by_session']['test-session'], 21)
        self.assertFalse(q['total_solving_time_known']); self.assertNotIn('thinking_seconds', q)
        self.assertEqual(q['intercontact_gaps_seconds'], [17])

    def test_step_boundaries_respect_interruption_and_eraser(self):
        events = [self.event(), self.event(3,4,3_000_000,4_000_000,kind='erase_candidate_mode_unknown'),
                  self.event(5,6,5_000_000,6_000_000)]
        q = self.report(events)['questions'][0]
        self.assertEqual(len(q['step_candidates']), 2)
        self.assertEqual(q['observed_writing_seconds'], 3)
        self.assertFalse(q['step_candidates'][0]['semantic_step_verified'])

    def test_bounded_ingestion_resumes_and_is_idempotent(self):
        self.add_events([self.event(i,i+1,i*1_000_000,(i+1)*1_000_000) for i in (1,3,5)])
        self.assertTrue(self.store.ingest(self.session, self.binding_path, limit=2)['may_have_more'])
        self.assertEqual(self.store.ingest(self.session, self.binding_path, limit=2)['processed_contacts'], 1)
        self.assertEqual(self.store.ingest(self.session, self.binding_path, limit=2)['processed_contacts'], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM contacts').fetchone()[0], 3)

    def test_final_ledger_reset_replaces_provisional_activity(self):
        self.report([self.event(provisional=True)])
        with closing(sqlite3.connect(self.session/'pipeline.sqlite')) as db, db:
            db.execute('DELETE FROM edit_events'); db.execute('INSERT INTO meta VALUES (?,?)', ('edits_final_clock', 'final'))
        self.add_events([self.event(10,11,10_000_000,11_000_000)])
        q = self.report()['questions'][0]
        self.assertEqual(q['evidence_ids'], ['test-session:10:11']); self.assertFalse(q['provisional'])

    def test_queue_deduplicates_identical_evidence(self):
        report = self.report([self.event()])
        self.assertEqual(self.store.enqueue(report, self.binding_path), 1)
        self.assertEqual(self.store.enqueue(report, self.binding_path), 0)

    def test_provisional_answer_not_queued_for_grading(self):
        report = self.report([self.event(provisional=True)])
        self.assertEqual(self.store.enqueue(report, self.binding_path), 0)

    def test_feedback_requires_existing_evidence_and_correct_revision(self):
        path = self.queue(); request = read(path)
        for mode in ('id', 'hash', 'cite', 'grade'):
            response = self.response(request)
            if mode == 'id': response['request_id'] = 'wrong'
            elif mode == 'hash': response['evidence_hash'] = 'wrong'
            elif mode == 'cite': response['observations'][0]['evidence_ids'] = ['invented']
            else: response['correctness'] = 'candidate_incorrect'
            with self.subTest(mode=mode), self.assertRaises(ValueError): self.store.accept_feedback(path, response, {'mode':'test'})

    def test_feedback_cannot_use_tampered_request(self):
        path = self.queue(); request = read(path); request['question']['label'] = 'tamper'; save(path, request)
        with self.assertRaises(ValueError): self.store.accept_feedback(path, self.response(request), {'mode': 'test'})

    def test_current_chat_feedback_persists_without_inventing_mastery(self):
        path = self.queue(); request = read(path)
        self.store.accept_feedback(path, self.response(request), {'mode': 'current_chat_local_review'})
        profile = self.store.profile('math')
        self.assertEqual(len(profile['feedback_history']), 1); self.assertIsNone(profile['mastery_estimate'])
        with self.assertRaises(ValueError): self.store.accept_feedback(path, self.response(request), {'mode': 'test'})

    def test_semantic_step_duration_recomputed_from_device_evidence(self):
        path = self.queue(); request = read(path); response = self.response(request)
        response['steps'] = [{'label':'Inspect conditions', 'evidence_ids':request['available_evidence_ids'],
                              'observed_writing_seconds':999}]
        result = self.store.accept_feedback(path,response,{'mode':'current_chat_local_review'})
        self.assertEqual(result['step_timings'][0]['observed_writing_seconds'],2)
        self.assertFalse(result['step_timings'][0]['thinking_time_known'])

    def test_semantic_step_with_invented_contact_is_rejected(self):
        path=self.queue();request=read(path);response=self.response(request)
        response['steps']=[{'label':'Fake','evidence_ids':['not-a-contact']}]
        with self.assertRaises(ValueError):self.store.accept_feedback(path,response,{'mode':'test'})

    def test_review_packet_renders_question_and_measured_timing(self):
        from course_review import publish_request,publish_feedback
        path=self.queue();page=publish_request(path)
        self.assertIn('复制辅导请求',page.read_text(encoding='utf-8'))
        self.assertTrue(list(path.parent.glob('*.question-0.png')))
        result=self.store.accept_feedback(path,self.response(read(path)),{'mode':'test'})
        feedback=path.parent/(result['request_id']+'.feedback.json')
        self.assertTrue(publish_feedback(feedback).exists())

    def test_runtime_end_to_end_local_activity_queue_feedback_and_profile(self):
        from course_runtime import Runtime
        self.add_events([self.event()])
        pipeline_root=self.root/'pipelines';pipeline_root.mkdir()
        # Directory names match the real pipeline's discovery convention.
        session_root=pipeline_root/'session-test-session';self.session.rename(session_root)
        config={'root':str(self.root),'supervise_capture':False,
                'capture':{'output':str(pipeline_root)},'recognition':{'enabled':False},
                'courses':[{'id':'math','manifest':str(self.package/'manifest.json'),'binding':str(self.binding_path)}]}
        config_path=self.root/'runtime.json';save(config_path,config)
        runtime=Runtime(config_path)
        try:
            runtime.tick()
            request_path=Path(runtime.store.db.execute('SELECT request_path FROM tutor_jobs').fetchone()[0])
            request=read(request_path)
            save(runtime.root/'tutor/incoming/local-feedback.json',self.response(request))
            runtime.tick()
            self.assertEqual(runtime.store.db.execute('SELECT status FROM tutor_jobs').fetchone()[0],'completed')
            self.assertEqual(len(read(runtime.root/'math-profile.json')['feedback_history']),1)
            self.assertTrue((runtime.root/'index.html').exists())
            runtime.tick()
            self.assertEqual(runtime.store.db.execute('SELECT COUNT(*) FROM assessments').fetchone()[0],1)
        finally:runtime.close()

    def test_new_activity_supersedes_pending_feedback(self):
        old_path = self.queue()
        report = self.report([self.event(3,4,3_000_000,4_000_000)])
        self.store.enqueue(report, self.binding_path)
        request = read(old_path)
        with self.assertRaises(ValueError): self.store.accept_feedback(old_path, self.response(request), {'mode': 'test'})

    def test_answer_evidence_creates_new_immutable_request(self):
        path = self.queue(); old_hash = sha256(path); request = read(path)
        new = add_answer_evidence(self.store, path, {'request_id': request['id'], 'activity_evidence_hash': request['evidence_hash'],
                                 'groups': [], 'images': [{'path': 'test', 'sha256': 'a'*64}], 'deferred_step_ids': []})
        self.assertNotEqual(new, path); self.assertEqual(sha256(path), old_hash)
        with self.assertRaises(ValueError): self.store.accept_feedback(path, self.response(request), {'mode': 'test'})

    def test_review_mode_never_makes_network_calls(self):
        self.queue()
        with patch('course_tutor.endpoint_call', side_effect=AssertionError('network')):
            self.assertEqual(run_one(self.store, {'mode':'review'})['status'], 'waiting_local_review')

    def test_external_api_disabled_without_explicit_endpoint_authorization(self):
        path = self.queue()
        with self.assertRaises(ValueError): endpoint_call(path, {'endpoint':'https://example.com/chat', 'model':'test'})
        with self.assertRaises(ValueError): endpoint_call(path, {'endpoint':'http://example.com/chat', 'model':'test', 'external_content_authorized':True})

    def test_supervisor_never_replaces_pipeline_during_recording(self):
        current = [{'args':['python','old'], 'pid':123}]; desired = ['python','new']
        self.assertEqual(pipeline_action(current,desired,{'sessions':[{'state':'recording'}]},'waiting_user_capture',True), 'wait_user_stop')
        self.assertEqual(pipeline_action(current,desired,None,'waiting_user_capture',True),'wait_receiver')
        self.assertEqual(pipeline_action(current,desired,{'sessions':[]},'waiting_user_capture',False),'wait_idle_evidence')
        self.assertEqual(pipeline_action(current,desired,{'sessions':[]},'observing',True),'wait_idle_evidence')
        self.assertEqual(pipeline_action(current,desired,{'sessions':[]},'waiting_user_capture',True),'reconfigure_idle')
        self.assertEqual(pipeline_action([],desired,{'sessions':[{'state':'recording'}]},'observing',False),'start_missing')

    def test_process_argument_parsing_keeps_unicode_spaces_and_exact_script(self):
        argv = windows_args('"C:\\Program Files\\Python\\python.exe" -X utf8 scripts/session_pipeline.py --title "数学 试卷"')
        self.assertEqual(argv[-1], '数学 试卷')
        self.assertEqual(len(find_processes([{'pid':1,'args':argv}], 'scripts/session_pipeline.py')),1)
        self.assertEqual(find_processes([{'pid':1,'args':['python','scripts/course_runtime.py']}], 'scripts/session_pipeline.py'),[])

    def test_process_inventory_encoding_failure_never_means_no_services(self):
        value = [{'ProcessId':123,'CommandLine':'C:/Users/中文/python.exe scripts/session_pipeline.py'}]
        payload = base64.b64encode(json.dumps(value,ensure_ascii=False).encode())
        self.assertEqual(decode_inventory(payload), value)
        for malformed in (None, b'', b'[]', b'not base64', base64.b64encode(b'[]'), base64.b64encode(b'\xff')):
            with self.subTest(payload=malformed), self.assertRaises(RuntimeError): decode_inventory(malformed)

    def test_local_server_blocks_rebinding_traversal_and_database_reads(self):
        root = self.root/'learning'; save(root/'status.json', {'ready':True})
        server = make_server(root,0); thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            self.assertTrue(json.loads(urlopen(base+'/status.json').read())['ready'])
            for path in ('/learning.sqlite', '/../source.pdf', '/'):
                with self.subTest(path=path), self.assertRaises(HTTPError): urlopen(base+path)
            with self.assertRaises(HTTPError): urlopen(Request(base+'/status.json',headers={'Host':'evil.example'}))
        finally: server.shutdown();server.server_close();thread.join(timeout=2)

    def generated(self):
        output, pending = self.package/'native.hinote', self.root/'pending.json'
        value = generate(self.package/'manifest.json', output, 'Native synthetic math', pending)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        window = {'pid': 123, 'window_id': 456, 'display_id': 0, 'focused': True}
        context = {'source':'local_hidumper_allInfo', 'current_document_valid':True,
                   'active_tab_marker_unique':True, 'window_identity_stable':True,
                   'foreground_at_both_checks':True, 'window_before':window, 'window_after':dict(window),
                   'document_title_candidate':value['native']['title'],
                   'page_layers':[{'page_id':next(iter(value['page_questions']))}],
                   'observation_interval':{'start_at_pc':now,'end_at_pc':now}}
        return output, pending, value, context

    def test_generated_native_preserves_pdf_and_does_not_claim_import(self):
        output, pending, value, _ = self.generated()
        self.assertFalse(value['notes_import_verified'])
        self.assertEqual(set(value['native']['pages'].values()), {1,2})
        with self.assertRaises(ValueError): verified_package(pending)
        self.assertEqual(verified_package(pending, allow_generated=True)[0], value)
        from inspect_hinote import inspect
        native = inspect(output)
        self.assertEqual(native['warnings'], [])
        self.assertTrue(all(a['verified'] for a in native['attachment_verification']))
        self.assertTrue(all(not p['attachment'] and not p['elements'] for p in native['pages']))
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read(native['embedded_pdfs'][0]['entry']), (self.package/'workbook.pdf').read_bytes())
            md = json.loads(gzip.decompress(archive.read('custom_md.jhinote')))['customMdContents']
            for info in md:
                self.assertEqual(len(info['fileMdStr']), 64)
            from PIL import Image
            import io
            for p in native['pages']:
                raw = native['raw_metadata'][p['source_entry']]['customNotePageContent']
                image = Image.open(io.BytesIO(archive.read('files/'+raw['thumbnail'].rsplit('/',1)[-1])))
                self.assertEqual(max(image.size),1080)

    def test_generated_import_confirms_only_exact_observed_uuids(self):
        _, pending, value, context = self.generated()
        output = self.root/'confirmed.json'
        result = confirm_import(pending, context, output)
        self.assertTrue(verified_package(output)[0]['notes_import_verified'])
        self.assertFalse(result['import_observation']['all_pages_observed'])
        self.assertEqual(result['page_questions'], value['page_questions'])
        self.assertFalse(read(pending)['notes_import_verified'])
        with self.assertRaises(FileExistsError): confirm_import(pending,context,output)

    def test_generated_import_rejects_title_only_remapped_or_stale_context(self):
        _, pending, _, context = self.generated()
        cases = [dict(context, page_layers=[{'page_id':'remapped'}]),
                 dict(context, document_title_candidate='same-name-other-note'),
                 dict(context, foreground_at_both_checks=False),
                 dict(context, page_layers=[]), dict(context, active_tab_marker_unique=False),
                 dict(context, window_after=dict(context['window_after'],pid=999)),
                 dict(context, observation_interval={'start_at_pc':'2000-01-01T00:00:00+00:00','end_at_pc':'2000-01-01T00:00:01+00:00'})]
        for i, candidate in enumerate(cases):
            with self.subTest(i=i), self.assertRaises(ValueError):
                confirm_import(pending,candidate,self.root/f'rejected-{i}.json')

    def test_native_package_mutation_rejects_pending_and_confirmed_binding(self):
        output, pending, _, context = self.generated()
        confirmed=self.root/'confirmed.json'; confirm_import(pending,context,confirmed)
        with output.open('ab') as stream: stream.write(b'changed')
        for path in (pending,confirmed):
            with self.subTest(path=path), self.assertRaises(ValueError): verified_package(path,allow_generated=True)

    def test_native_import_poll_never_adds_context_queries_while_recording(self):
        from course_runtime import Runtime
        _,pending,_,_=self.generated()
        runtime=object.__new__(Runtime);runtime.last_import_check=0
        courses=[{'native_import_binding':str(pending),'binding':str(self.root/'unbound.json')}]
        with patch('course_runtime.receiver_state',return_value={'sessions':[{'state':'recording'}]}), patch('notes_diagnostics.collect') as observe:
            runtime.discover_native_imports(courses)
            observe.assert_not_called()

    def test_late_recognition_never_revives_superseded_activity(self):
        request=self.queue(); old=read(request)
        self.add_events([self.event(3,4,lo=3_000_000,hi=4_000_000)])
        self.store.ingest(self.session,self.binding_path)
        self.store.enqueue(self.store.report(self.binding_path),self.binding_path)
        before=list(self.store.db.execute('SELECT id,status FROM tutor_jobs ORDER BY id'))
        recognized={'request_id':old['id'],'activity_evidence_hash':old['evidence_hash'],
                    'images':[{'path':'unused.png'}],'groups':[],'deferred_step_ids':[]}
        add_answer_evidence(self.store,request,recognized)
        self.assertEqual(list(self.store.db.execute('SELECT id,status FROM tutor_jobs ORDER BY id')),before)

    def test_outgoing_generated_archive_is_never_discovered_as_user_export(self):
        from course_runtime import Runtime
        output,pending,_,_=self.generated()
        runtime=object.__new__(Runtime);runtime.last_export_check=0;runtime.root=self.root
        courses=[{'native_import_binding':str(pending),'native_workbook':str(output),
                  'native_export_name':'native.hinote','binding':str(self.root/'unbound.json')}]
        with patch('tablet_files.Hdc') as hdc:
            runtime.discover_exports(courses,False)
            hdc.assert_not_called()

    def test_visual_identity_publishes_immutable_course_binding_revisions(self):
        from course_runtime import Runtime
        import hashlib
        _,pending,value,_=self.generated()
        root=self.root/'learning';pipeline=self.root/'pipeline'
        course={'id':'math','manifest':str(self.package/'manifest.json'),'binding':str(self.root/'unbound.json'),
                'native_import_binding':str(pending),'visual_identity_discovery':True}
        runtime=object.__new__(Runtime);runtime.root=root;runtime.config_path=self.root/'runtime.json'
        runtime.config={'capture':{'output':str(pipeline)},'courses':[course]}
        key=hashlib.sha256(value['native']['title'].encode()).hexdigest()[:16]
        path=pipeline/'discovered-pages'/(key+'.json')
        observation={'schema':'inkbridge-visual-page-binding-v1','title':value['native']['title'],
            'source_pdf_sha256':value['native']['source_pdf_sha256'],'source_hinote_sha256':value['native']['source_hinote_sha256'],
            'local_association_id':'visual-test','pages':{'actual-new-page':1},'blocked_page_ids':[],
            'evidence':{'actual-new-page':{'first':{'page':1,'time':1,'identity':['window']},
                                         'second':{'page':1,'time':1.5,'identity':['window']}}}}
        save(path,observation);runtime.adopt_visual_bindings([course])
        first=Path(course['binding']);first_hash=sha256(first)
        binding,_=verified_package(first)
        self.assertEqual(binding['page_questions'],{'actual-new-page':'q1'})
        self.assertFalse(binding['private_database_binding_verified'])
        self.assertEqual(binding['native']['pages'],{'actual-new-page':1})
        observation['pages']['actual-second']=2
        observation['evidence']['actual-second']={'first':{'page':2,'time':2,'identity':['window']},
                                                'second':{'page':2,'time':2.5,'identity':['window']}}
        save(path,observation);runtime.adopt_visual_bindings([course])
        self.assertNotEqual(course['binding'],str(first));self.assertEqual(sha256(first),first_hash)
        self.assertEqual(verified_package(course['binding'])[0]['page_questions']['actual-second'],'q2')
        observation['source_pdf_sha256']='wrong';save(path,observation)
        with self.assertRaises(ValueError):runtime.adopt_visual_bindings([course])

    def test_supervisor_rejects_bad_configuration_before_stopping_healthy_pipeline(self):
        from course_runtime import Supervisor
        output=self.root/'pipeline'
        save(output/'status.json',{'stage':'waiting_user_capture','updated_at_pc':time.time()})
        config={'capture':{'output':str(output)},'allow_idle_pipeline_reconfigure':True}
        processes=[{'pid':11,'args':['python','scripts/session_pipeline.py','--output',str(output)]},
                   {'pid':12,'args':['pwsh',str(ROOT/'watch-tablet.ps1')]}]
        supervisor=Supervisor(config,self.root/'learning')
        with patch('course_runtime.inventory',return_value=processes), patch('course_runtime.receiver_state',return_value={'sessions':[]}), \
             patch('course_runtime.desired_pipeline',return_value=['python','scripts/session_pipeline.py','changed']), \
             patch('course_runtime.validate_pipeline_start',return_value=False), patch('course_runtime.os.kill') as stop, \
             patch('course_runtime.start_background') as start:
            status=supervisor.tick([])
        self.assertEqual(status['pipeline_action'],'configuration_rejected')
        stop.assert_not_called();start.assert_not_called()


def read(path): return json.loads(Path(path).read_bytes())


if __name__ == '__main__': unittest.main(verbosity=2)
