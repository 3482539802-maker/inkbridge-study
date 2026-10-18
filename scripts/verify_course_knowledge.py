"""Knowledge library trust boundaries and the actual multi-round learning loop."""
from copy import deepcopy
import json
from pathlib import Path
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import verify_course_workflow as workflow
from course_knowledge import (action, accept_batch, accept_review, build_snapshot, context,
    events, items, knowledge_states, load_catalog, process_incoming, publish_knowledge, setup, equation_parts)
from course_workbook import save, sha256
from course_runtime import make_server, Runtime


class KnowledgeTests(unittest.TestCase):
    setUp = workflow.WorkflowTests.setUp
    tearDown = workflow.WorkflowTests.tearDown
    event = workflow.WorkflowTests.event
    add_events = workflow.WorkflowTests.add_events
    report = workflow.WorkflowTests.report
    queue = workflow.WorkflowTests.queue
    response = workflow.WorkflowTests.response

    def courses(self):
        return [{'id':'math', 'manifest':str(self.package/'manifest.json'), 'binding':str(self.binding_path)}]

    def generate(self, key='generation-1'):
        return action(self.store, self.courses(), {'id':key, 'kind':'generate', 'course_id':'math', 'question_id':'q1'})

    def batch(self, generation, ids=('p1','p2')):
        return {'schema':'inkbridge-practice-batch-v1', 'generation_id':generation['id'],
                'course_id':'math', 'context_hash':generation['context_hash'],
                'items':[{'id':key,'question_id':'q1', 'knowledge_ids':['unclassified'],
                          'title':'New exercise '+key,'level':'基础诊断','reason':'检验相同方法',
                          'prompt':[{'text':'Compute 2 + 2.'}], 'hints':['Add two pairs.'],
                          'solution':[{'math':'2+2=4'}]} for key in ids]}

    def attempt(self, key='a1', practice='p1'):
        return action(self.store, self.courses(), {'id':key,'course_id':'math','kind':'attempt',
                      'practice_id':practice,'answer':'2+2=4','self_report':'independent_correct'})

    def review(self, attempt, key='r1', outcome='independent_correct'):
        return {'schema':'inkbridge-practice-review-v1','id':key,'attempt_id':attempt['id'],
                'answer_sha256':attempt['answer_sha256'], 'outcome':outcome,
                'rationale':'已核对本次提交的算式与结果。', 'feedback':[{'text':'推导正确。'}]}

    def test_source_ink_feedback_reference_and_safe_html_are_together(self):
        path=self.queue(); req=workflow.read(path); response=self.response(req)
        response['observations'][0]['text']='Text </script><img src=x onerror=alert(1)>'
        self.store.accept_feedback(path,response,{'mode':'test'})
        reference={'source_pdf_sha256':self.original_hash, 'questions':[{'id':'q1',
            'approach':[{'text':'Reference <script>never execute</script>'}],
            'steps':[{'title':'Compute','blocks':[{'math':'2+2=4'}]}], 'result':[{'text':'4'}]}]}
        ref=self.root/'solutions.json';save(ref,reference)
        course=dict(self.courses()[0], solutions=str(ref),solutions_sha256=sha256(ref))
        publish_knowledge(self.store,[course])
        output=self.store.root/'knowledge/math/index.html';html=output.read_text(encoding='utf-8')
        self.assertIn('原题',html);self.assertIn('你的笔迹',html);self.assertIn('我的建议',html);self.assertIn('参考答案',html)
        self.assertNotIn('Text </script>',html);self.assertIn('\\u003c/script',html)
        snapshot=build_snapshot(self.store,course);q=snapshot['questions'][0]
        self.assertTrue(q['prompt_images']);self.assertIn('&lt;script&gt;',q['reference_html'])
        self.assertIsNone(snapshot['states']['unclassified']['mastery_percentage'])

    def test_catalog_rejects_missing_mapping_unknown_dependencies_and_cycles(self):
        catalog=load_catalog(self.courses()[0],self.manifest);path=self.root/'curriculum.json'
        for mode in ('missing','unknown','cycle'):
            bad=deepcopy(catalog)
            if mode=='missing':del bad['question_knowledge']['q2']
            elif mode=='unknown':bad['knowledge_points'][0]['prerequisites']=['fake']
            else:bad['knowledge_points'][0]['prerequisites']=['unclassified']
            save(path,bad)
            with self.subTest(mode=mode),self.assertRaises(ValueError):load_catalog(dict(self.courses()[0],curriculum=str(path)),self.manifest)

    def test_formula_wrap_preserves_grouped_fractions_and_all_math_tokens(self):
        formula=r'f(x)=x-\left(x-\frac{x^2}{2}+\frac{x^3}{3}\right)-\frac{x}{2}\sin x.'
        parts=equation_parts(formula)
        self.assertEqual(''.join(parts),formula)
        self.assertIn(r'-\left(x-\frac{x^2}{2}+\frac{x^3}{3}\right)',parts)
        self.assertGreater(len(parts),2)

    def test_duplicate_pending_generation_is_reused(self):
        first=self.generate();second=self.generate('generation-different-id')
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM practice_generations').fetchone()[0],1)

    def test_missing_json_projection_recovers_without_rewriting_existing_history(self):
        accept_batch(self.store,self.courses(),self.batch(self.generate()))
        attempt=self.attempt();path=self.store.root/'knowledge/history'/(attempt['id']+'.json');path.unlink()
        publish_knowledge(self.store,self.courses());self.assertEqual(workflow.read(path),attempt)
        save(path,{'tampered':True})
        with self.assertRaises(ValueError):publish_knowledge(self.store,self.courses())

    def test_browser_self_report_never_awards_mastery_and_duplicate_is_idempotent(self):
        g=self.generate();batch=self.batch(g);accept_batch(self.store,self.courses(),batch)
        a=self.attempt();self.assertEqual(self.attempt(),a)
        _,_,_,states,_=context(self.store,self.courses()[0],self.manifest)
        self.assertEqual(states['unclassified']['state'],'pending_review')
        data={'id':'a1','course_id':'math','kind':'attempt','practice_id':'p1','answer':'changed','self_report':'incorrect'}
        with self.assertRaises(ValueError):action(self.store,self.courses(),data)

    def test_complete_loop_preserves_history_and_only_spaced_different_questions_qualify(self):
        batch=self.batch(self.generate());accept_batch(self.store,self.courses(),batch)
        a1=self.attempt();r1=self.review(a1);accept_review(self.store,r1)
        self.assertTrue(accept_review(self.store,r1)['replayed'])
        self.assertEqual(context(self.store,self.courses()[0],self.manifest)[3]['unclassified']['state'],'independent')
        with workflow.patch('course_knowledge.time.time',return_value=time.time()+2*86400):a2=self.attempt('a2','p2')
        accept_review(self.store,self.review(a2,'r2'))
        s=context(self.store,self.courses()[0],self.manifest)[3]['unclassified']
        self.assertEqual(s['state'],'retained');self.assertEqual(s['review_count'],2)
        with workflow.patch('course_knowledge.time.time',return_value=time.time()+3*86400):a3=self.attempt('a3','p2')
        accept_review(self.store,self.review(a3,'r3','incorrect'))
        self.assertEqual(context(self.store,self.courses()[0],self.manifest)[3]['unclassified']['state'],'needs_work')
        self.assertEqual(len(events(self.store,'math')),6)
        self.assertEqual(self.generate('generation-next')['states']['unclassified']['state'],'needs_work')

    def test_same_day_or_same_exercise_does_not_establish_retention(self):
        accept_batch(self.store,self.courses(),self.batch(self.generate()))
        for i,practice in enumerate(('p1','p1','p2')):
            a=self.attempt('a'+str(i),practice);accept_review(self.store,self.review(a,'r'+str(i)))
        self.assertEqual(context(self.store,self.courses()[0],self.manifest)[3]['unclassified']['state'],'independent')

    def test_revealed_answers_and_unknown_answer_hash_cannot_award_independence(self):
        accept_batch(self.store,self.courses(),self.batch(self.generate()))
        action(self.store,self.courses(),{'id':'reveal','course_id':'math','kind':'reveal','practice_id':'p1','target':'solution'})
        a=self.attempt()
        with self.assertRaises(ValueError):accept_review(self.store,self.review(a))
        review=self.review(a,outcome='hinted_correct');review['answer_sha256']='tampered'
        with self.assertRaises(ValueError):accept_review(self.store,review)
        review=self.review(a,outcome='hinted_correct');accept_review(self.store,review)
        self.assertEqual(context(self.store,self.courses()[0],self.manifest)[3]['unclassified']['state'],'with_help')

    def test_generation_rejects_stale_context_wrong_parent_and_partial_batch(self):
        g=self.generate();bad=self.batch(g);bad['items'][1]['question_id']='q2'
        with self.assertRaises(ValueError):accept_batch(self.store,self.courses(),bad)
        self.assertEqual(len(items(self.store,'math')),0)
        accept_batch(self.store,self.courses(),self.batch(g))
        self.assertTrue(accept_batch(self.store,self.courses(),self.batch(g))['replayed'])
        g2=self.generate('generation-2');self.attempt()
        with self.assertRaises(ValueError):accept_batch(self.store,self.courses(),self.batch(g2,('p3',)))

    def test_processed_inbox_does_not_starve_new_inputs_and_restart_recovers(self):
        g=self.generate();incoming=self.store.root/'knowledge/incoming'
        for i in range(20):
            save(incoming/f'000-{i}.json',{});save(incoming/f'000-{i}.receipt.json',{'status':'accepted'})
        save(incoming/'new-batch.json',self.batch(g))
        process_incoming(self.store,self.courses());self.assertEqual(workflow.read(incoming/'new-batch.receipt.json')['status'],'accepted')
        save(incoming/'duplicate-batch.json',self.batch(g));process_incoming(self.store,self.courses())
        self.assertEqual(len(items(self.store,'math')),2)

    def test_course_inventory_excludes_other_document_sessions(self):
        self.add_events([self.event(),self.event(3,4,document_title='Other subject',page_id='other')])
        report=self.report()
        self.assertEqual(report['unattributed_events'],{})
        self.assertEqual(sum(report['scan_inventory']['excluded_events'].values()),1)

    def test_http_actions_require_same_origin_and_reject_browser_grades(self):
        config=self.root/'runtime.json';save(config,{'root':str(self.store.root),'courses':self.courses()})
        server=make_server(self.store.root,0,config);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        data={'id':'http-gen','kind':'generate','course_id':'math','question_id':'q1'}
        def post(payload,origin=base):
            return urlopen(Request(base+'/api/knowledge/action',data=json.dumps(payload).encode(),
                                  headers={'Content-Type':'application/json','Origin':origin}))
        try:
            with self.assertRaises(HTTPError):post(data,'http://evil.example')
            self.assertEqual(json.loads(post(data).read())['event']['kind'],'generate')
            with self.assertRaises(HTTPError):post(dict(data,id='review',kind='review'))
            for method in ('GET','HEAD'):
                with self.assertRaises(HTTPError):urlopen(Request(base+'/learning.sqlite',method=method))
        finally:server.shutdown();server.server_close();thread.join(timeout=2)

    def runtime(self):
        self.add_events([self.event()]);pipeline=self.root/'pipelines';pipeline.mkdir()
        self.session.rename(pipeline/'session-test-session')
        config=self.root/'runtime.json'
        save(config,{'root':str(self.store.root),'supervise_capture':False,
              'capture':{'output':str(pipeline)},'recognition':{'enabled':False},'courses':self.courses()})
        return Runtime(config)

    def test_feedback_inbox_recovers_after_more_than_sixteen_receipts_and_rejects_changed_late_feedback(self):
        runtime=self.runtime()
        try:
            runtime.tick();path=Path(runtime.store.db.execute('SELECT request_path FROM tutor_jobs').fetchone()[0])
            response=self.response(workflow.read(path));incoming=runtime.root/'tutor/incoming'
            for i in range(20):
                save(incoming/f'000-{i}.json',{});save(incoming/f'000-{i}.receipt.json',{'status':'accepted'})
            save(incoming/'new.json',response);runtime.tick()
            self.assertEqual(workflow.read(incoming/'new.receipt.json')['status'],'accepted')
            response['hints']=['Different feedback cannot replace the accepted response.']
            save(incoming/'newer.json',response);runtime.tick()
            self.assertEqual(workflow.read(incoming/'newer.receipt.json')['status'],'rejected')
        finally:runtime.close()

    def test_no_image_recognition_becomes_an_explicit_outcome_without_retry_loop(self):
        runtime=self.runtime()
        try:
            runtime.tick();path=Path(runtime.store.db.execute('SELECT request_path FROM tutor_jobs').fetchone()[0])
            request=workflow.read(path)
            save(path.with_suffix('.recognition.json'),{'request_id':request['id'],
                'activity_evidence_hash':request['evidence_hash'],'images':[],'groups':[],'deferred_step_ids':[]})
            runtime.config['recognition']['enabled']=True
            with workflow.patch('course_runtime.start_background',side_effect=AssertionError('must not relaunch')):
                runtime.recognition_tick();runtime.recognition_tick()
            self.assertEqual(workflow.read(path.with_suffix('.recognition-outcome.json'))['status'],'insufficient_ink_evidence')
        finally:runtime.close()

    def test_bad_catalog_retains_last_page_and_surfaces_runtime_error(self):
        runtime=self.runtime()
        try:
            runtime.tick();page=runtime.root/'knowledge/math/index.html';before=sha256(page)
            bad=self.root/'bad-catalog.json';save(bad,{'schema':'wrong'})
            config=workflow.read(runtime.config_path);config['courses'][0]['curriculum']=str(bad);save(runtime.config_path,config)
            runtime.tick()
            self.assertEqual(sha256(page),before)
            self.assertIn('math',workflow.read(runtime.root/'status.json')['knowledge_errors'])
        finally:runtime.close()


if __name__=='__main__':unittest.main(verbosity=2)
