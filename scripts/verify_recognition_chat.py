"""Offline current-chat exchange checks; all ink/answers here are synthetic."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

import verify_course_workflow as workflow
from course_tutor import recognize_request, add_answer_evidence
from course_workbook import save, sha256
from recognition_chat import prepare_review, import_review, publish_review, load


class ChatRecognitionChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = workflow.WorkflowTests('test_original_unchanged_and_answer_pixels_excluded')
        self.fixture.setUp()
        self.root = self.fixture.root
        events = [self.fixture.event(1, 2), self.fixture.event(3, 4, 20_000_000, 21_000_000),
                  self.fixture.event(5, 6, 40_000_000, 41_000_000)]
        report = self.fixture.report(events)
        self.fixture.store.enqueue(report, self.fixture.binding_path)
        self.request = Path(self.fixture.store.db.execute('SELECT request_path FROM tutor_jobs').fetchone()[0])
        self.value = load(self.request); steps = self.value['activity']['step_candidates']
        ink = self.request.parent/'test-ink.png'; Image.new('RGB', (160, 80), 'white').save(ink)
        self.recognized = {'schema': 'inkbridge-course-recognition-v1', 'request_id': self.value['id'],
            'activity_evidence_hash': self.value['evidence_hash'],
            'groups': [{'step_id': steps[0]['id'], 'evidence_ids': steps[0]['evidence_ids'],
                        'status': 'ink_available', 'provenance': {'contacts': ['synthetic']},
                        'image_sha256': sha256(ink)},
                       {'step_id': steps[1]['id'], 'evidence_ids': steps[1]['evidence_ids'],
                        'status': 'unavailable', 'reason': 'Unknown coordinate'}],
            'images': [{'step_id': steps[0]['id'], 'path': str(ink), 'sha256': sha256(ink),
                        'evidence_ids': steps[0]['evidence_ids']}],
            'deferred_step_ids': [steps[2]['id']], 'recognition_backend': 'current_chat',
            'final_visible_answer_verified': False, 'external_inference': False}
        self.packet = prepare_review(self.request, self.recognized, self.root/'review')
        self.response = load(self.packet.parent/'response-template.json')
        self.response['reviewer']['model'] = 'synthetic-test'
        group = self.response['groups'][0]
        group.update(status='partial', semantic_type='mixed', uncertainties=['One unclear symbol'],
                     elements=[{'kind': 'text', 'text': '设面：', 'latex': None, 'bbox_px': [0, 0, 50, 20],
                                'evidence_ids': group['evidence_ids'], 'uncertainties': []},
                               {'kind': 'formula', 'text': 'x²', 'latex': 'x^2', 'bbox_px': [0, 20, 60, 70],
                                'evidence_ids': group['evidence_ids'], 'uncertainties': []}])

    def tearDown(self): self.fixture.tearDown()

    def test_archived_review_is_displayed_without_reviving_completed_jobs(self):
        from course_knowledge import build_snapshot
        response = self.root/'archive-response.json'
        save(response, self.response)
        self.fixture.store.db.execute("UPDATE tutor_jobs SET status='superseded'")
        self.fixture.store.db.commit()
        before = self.fixture.store.db.execute('SELECT id,status FROM tutor_jobs').fetchall()
        course = {'id': 'math', 'manifest': str(self.fixture.package/'manifest.json'),
                  'archived_recognition_reviews': [{'packet': str(self.packet), 'packet_sha256': sha256(self.packet),
                      'response': str(response), 'response_sha256': sha256(response)}]}
        snapshot = build_snapshot(self.fixture.store, course)
        question = next(q for q in snapshot['questions'] if q['id'] == self.value['question']['id'])
        review = question['archived_recognition'][0]
        self.assertIn('设面：', review['recognition_html'])
        self.assertIn('公式', review['recognition_html'])
        self.assertIn('Unknown coordinate', review['recognition_html'])
        self.assertEqual(len(review['images']), 1)
        self.assertEqual(before, self.fixture.store.db.execute('SELECT id,status FROM tutor_jobs').fetchall())
        changed = deepcopy(self.response)
        changed['groups'][0]['elements'][0]['text'] = 'changed'
        save(response, changed)
        with self.assertRaises(ValueError): build_snapshot(self.fixture.store, course)

    def test_multi_region_display_escapes_text_and_keeps_unavailable_groups(self):
        from recognition_display import recognition_html
        group = deepcopy(self.response['groups'][0])
        group['elements'][0]['text'] = '<script>alert(1)</script>'
        html = recognition_html([group, self.recognized['groups'][1]])
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn('Unknown coordinate', html)

    def test_new_review_page_uses_relative_assets_for_http_and_file_reading(self):
        html = (self.packet.parent/'review.html').read_text(encoding='utf-8')
        self.assertNotIn('file:', html)
        self.assertIn('src="question-0.png"', html)
        self.assertIn('src="../learning/tutor/test-ink.png"', html)

    def run_import(self, response=None, suffix=''):
        path = self.root/f'response{suffix}.json'
        save(path, response or self.response)
        return import_review(self.packet, path, self.root/f'recognized{suffix}.json')

    def test_mixed_regions_preserve_source_and_immutable_queue_revision(self):
        before = sha256(self.request)
        result = self.run_import()
        self.assertEqual(result['groups'][0]['provenance'], self.recognized['groups'][0]['provenance'])
        self.assertIsNone(result['groups'][0]['latex_candidate'])
        self.assertEqual(result['deferred_step_ids'], self.recognized['deferred_step_ids'])
        self.assertFalse(result['final_visible_answer_verified'])
        new = add_answer_evidence(self.fixture.store, self.request, result)
        self.assertNotEqual(new, self.request); self.assertEqual(sha256(self.request), before)
        self.assertEqual(load(new)['recognition_status'], 'current_chat_candidates_not_verified_final_answer')
        self.assertFalse(load(new)['recognition_review_provenance']['accuracy_verified'])
        self.assertIn('recognition_review_provenance', load(new))

    def test_default_worker_never_loads_formula_model_even_with_legacy_directory(self):
        evidence = {'contacts': [], 'saved_or_current_state_verified': False}
        # Sequence numbers restart per session; assets must not collide when
        # a question spans multiple recording sessions.
        value = load(self.request)
        second = value['activity']['step_candidates'][1]
        second.update(session_id='test-second-session', first_seq=1,
                      id='test-second-session:1:2', evidence_ids=['test-second-session:1:2'])
        value['source']['session_directories']['test-second-session'] = str(self.fixture.session)
        save(self.request, value)
        with patch('recognition_ink_input.render_contacts', return_value=(np.zeros((20, 20, 3), dtype=np.uint8), evidence)), \
             patch.dict('sys.modules', {'recognition_unimernet_probe': None}):
            result = recognize_request(self.request, self.root/'worker.json', 'legacy/model')
        self.assertEqual(len(result['images']), 3)
        self.assertEqual(len({a['path'] for a in result['images']}), 3)
        self.assertEqual(result['recognition_backend'], 'current_chat')
        self.assertTrue(Path(result['chat_review']['path']).exists())
        self.assertTrue(all(g['latex_candidate'] is None for g in result['groups']))

    def test_local_baseline_requires_explicit_model(self):
        with self.assertRaises(ValueError):
            recognize_request(self.request, self.root/'worker.json', backend='local_formula')

    def test_input_revision_and_images_must_remain_intact(self):
        for mutation in ('packet', 'request', 'ink', 'question'):
            with self.subTest(mutation=mutation):
                packet = load(self.packet)
                target = (self.packet if mutation == 'packet' else self.request if mutation == 'request' else
                          Path(packet['ink_images'][0]['path']) if mutation == 'ink' else
                          Path(packet['question_images'][0]['path']))
                original = target.read_bytes()
                try:
                    if mutation == 'packet':
                        value = load(target); value['question_id'] = 'changed'; save(target, value)
                    elif mutation == 'request':
                        value = load(target); value['question']['label'] = 'changed'; save(target, value)
                    else: target.write_bytes(original+b'changed')
                    with self.assertRaises(ValueError): self.run_import(suffix=mutation)
                finally: target.write_bytes(original)

    def test_wrong_revision_duplicate_missing_or_invented_contacts_rejected(self):
        for mutation in ('revision', 'duplicate', 'missing', 'membership', 'region_ref'):
            response = deepcopy(self.response)
            if mutation == 'revision': response['packet_id'] = 'wrong'
            elif mutation == 'duplicate': response['groups'][1] = deepcopy(response['groups'][0])
            elif mutation == 'missing': response['groups'].pop()
            elif mutation == 'membership': response['groups'][0]['evidence_ids'] = ['invented']
            else: response['groups'][0]['elements'][0]['evidence_ids'] = ['invented']
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.run_import(response, suffix=mutation)

    def test_unavailable_and_deferred_groups_cannot_gain_fabricated_recognition(self):
        for index in (1, 2):
            response = deepcopy(self.response)
            response['groups'][index].update(status='readable', semantic_type='formula',
                                            elements=deepcopy(response['groups'][0]['elements']))
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.run_import(response, suffix=str(index))

    def test_region_bounds_nonformula_latex_and_readable_empty_rejected(self):
        for mutation in ('bounds', 'nan', 'coercion', 'empty'):
            response = deepcopy(self.response); group = response['groups'][0]
            if mutation == 'bounds': group['elements'][0]['bbox_px'] = [0, 0, 161, 20]
            elif mutation == 'nan': group['elements'][0]['bbox_px'] = [0, 0, float('inf'), 20]
            elif mutation == 'coercion': group['elements'][0]['latex'] = 'pretend formula'
            else: group.update(status='readable', elements=[])
            path = self.root/f'response-{mutation}.json'
            path.write_text(json.dumps(response), encoding='utf-8')
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                import_review(self.packet, path, self.root/f'bad-{mutation}.json')

    def test_results_never_overwrite_prior_recognition(self):
        self.run_import()
        with self.assertRaises(FileExistsError): self.run_import()

    def test_late_chat_review_cannot_revive_completed_or_superseded_request(self):
        result = self.run_import()
        for status in ('completed', 'superseded'):
            with self.fixture.store.db:
                self.fixture.store.db.execute('UPDATE tutor_jobs SET status=? WHERE id=?', (status, self.value['id']))
            before = list(self.fixture.store.db.execute('SELECT id,status FROM tutor_jobs'))
            self.assertEqual(add_answer_evidence(self.fixture.store, self.request, result), self.request)
            self.assertEqual(list(self.fixture.store.db.execute('SELECT id,status FROM tutor_jobs')), before)

    def test_review_advances_exact_prepared_child_and_never_newer_writing(self):
        prepared = deepcopy(self.recognized)
        prepared['chat_review'] = {'path': str(self.packet), 'sha256': sha256(self.packet),
                                  'status': 'awaiting_current_chat_recognition'}
        child = add_answer_evidence(self.fixture.store, self.request, prepared)
        result = self.run_import()
        reviewed = publish_review(self.fixture.store, self.packet, result)
        self.assertNotEqual(reviewed, child)
        self.assertEqual(load(reviewed)['recognition_status'], 'current_chat_candidates_not_verified_final_answer')
        self.assertEqual(load(reviewed)['recognition_review_provenance']['prepared_request_id'], load(child)['id'])
        before = list(self.fixture.store.db.execute('SELECT id,status FROM tutor_jobs'))
        self.assertEqual(publish_review(self.fixture.store, self.packet, result), self.request)
        self.assertEqual(list(self.fixture.store.db.execute('SELECT id,status FROM tutor_jobs')), before)

    def test_prepared_review_superseded_by_new_activity_is_not_republished(self):
        prepared = deepcopy(self.recognized)
        prepared['chat_review'] = {'path': str(self.packet), 'sha256': sha256(self.packet)}
        add_answer_evidence(self.fixture.store, self.request, prepared)
        result = self.run_import()
        report = self.fixture.report([self.fixture.event(7, 8, 60_000_000, 61_000_000)])
        self.fixture.store.enqueue(report, self.fixture.binding_path)
        before = list(self.fixture.store.db.execute('SELECT id,status FROM tutor_jobs'))
        self.assertEqual(publish_review(self.fixture.store, self.packet, result), self.request)
        self.assertEqual(list(self.fixture.store.db.execute('SELECT id,status FROM tutor_jobs')), before)


if __name__ == '__main__': unittest.main(verbosity=2)
