"""Offline failures: cropped resources, multi-column/cross-page/diagram cuts.

Synthetic fixtures prove packaging contracts, not arbitrary automatic splitting
or device import. No production database, original or tablet is modified.
"""
from copy import deepcopy
import json
import importlib.util
from pathlib import Path
import tempfile
import unittest

import fitz

from course_workbook import audit, bind, build, propose, require_answer_free, save, sha256, validate
from hinote_workbook import generate


class MaterialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='materials-offline-')
        self.root = Path(self.temp.name)
        self.source = self.root/'source.pdf'
        with fitz.open() as doc:
            p = doc.new_page(width=500, height=600)
            p.insert_text((30, 40), '1. Left column question')
            p.insert_text((270, 40), '2. Right column question')
            p.draw_rect(fitz.Rect(40, 70, 160, 150), color=(0,0,1))
            p.insert_text((30, 300), 'ANSWER SECRET MUST NOT BE EMBEDDED')
            p = doc.new_page(width=500, height=600)
            p.insert_text((30, 40), 'Continuation, with diagram')
            p.draw_circle((100, 100), 30, color=(1,0,0))
            p.insert_text((30, 300), 'SOLUTION PRIVATE')
            doc.save(self.source)
        self.plan = {'schema':'inkbridge-question-plan-v1', 'course':{'id':'test','title':'Offline failure fixtures'},
            'source_pdf':str(self.source), 'source_pdf_sha256':sha256(self.source), 'review':'confirmed',
            'expected_question_ids':['q1','q2'],
            'excluded_regions':[{'page':1,'box':[20,270,480,330]}, {'page':2,'box':[20,270,480,330]}],
            'questions':[{'id':'q1','parts':[{'page':1,'box':[20,20,240,170]}, {'page':2,'box':[20,20,240,170]}],'review':'confirmed'},
                         {'id':'q2','parts':[{'page':1,'box':[260,20,480,170]}],'review':'confirmed'}]}
        self.path = self.root/'plan.json'
        save(self.path, self.plan)

    def tearDown(self):
        self.temp.cleanup()

    def package(self):
        return build(self.path, self.root/'workbook')

    def test_two_columns_cross_page_and_diagrams_readback_without_answer_resources(self):
        original = sha256(self.source)
        manifest = self.package()
        result = audit(self.root/'workbook/manifest.json')
        self.assertEqual([p['question_id'] for p in result['pages']], ['q1','q1','q2'])
        self.assertEqual(sha256(self.source), original)
        with fitz.open(manifest['workbook_pdf']) as doc:
            self.assertNotIn('SECRET', ''.join(p.get_text() for p in doc))
            for p in doc:
                image = fitz.Pixmap(doc, p.get_images()[0][0])
                self.assertLess(image.height, 600)  # Full 600pt source is never embedded.
            image = fitz.Pixmap(doc, doc[0].get_images()[0][0])
            self.assertTrue(any(v != 255 for v in image.samples))

    def test_declared_inventory_detects_missing_question(self):
        self.plan['questions'].pop()
        with self.assertRaisesRegex(ValueError, 'inventory'): validate(self.plan, self.source)

    def test_excluded_answer_and_reference_overlap_rejected(self):
        for kind in ('excluded_regions','reference_parts'):
            plan = deepcopy(self.plan)
            overlap = {'page':1,'box':[30,30,100,60]}
            if kind == 'excluded_regions': plan[kind].append(overlap)
            else: plan['questions'][1][kind] = [overlap]
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'intersects'): validate(plan, self.source)

    def test_answer_guide_cannot_enter_build_bind_or_native_paths(self):
        for value in ({'artifact_role':'worked_solutions'}, {'answer_content_included':True}, {'delivery_scope':'computer_only'}):
            plan = dict(self.plan, **value)
            with self.subTest(value=value), self.assertRaises(ValueError): validate(plan, self.source)
        manifest = self.package()
        manifest['artifact_role'] = 'worked_solutions'
        manifest['delivery_scope'] = 'computer_only'
        save(self.root/'guide.json', manifest)
        with self.assertRaises(ValueError): bind(self.root/'guide.json', self.root/'missing.hinote', self.root/'binding.json')
        with self.assertRaises(ValueError): generate(self.root/'guide.json', self.root/'native.hinote', 'Rejected guide', self.root/'pending.json')
        self.assertFalse((self.root/'native.hinote').exists())

    def test_legacy_answer_free_manifest_remains_supported(self):
        require_answer_free({'schema':'inkbridge-workbook-v1'})

    def test_question_suggestions_are_never_auto_confirmed_for_two_columns(self):
        candidate = propose(self.source, 'test', 'Unreviewed columns')
        self.assertEqual(candidate['review'], 'needs_review')
        self.assertTrue(all(q['review'] == 'needs_review' for q in candidate['questions']))
        save(self.root/'candidate.json',candidate)
        with self.assertRaises(ValueError): build(self.root/'candidate.json',self.root/'candidate-workbook')

    def test_audit_rejects_affine_tampering(self):
        manifest = self.package()
        manifest['pages'][0]['source_to_workbook_affine'][4] += 10
        save(self.root/'tampered.json',manifest)
        with self.assertRaisesRegex(ValueError,'affine'): audit(self.root/'tampered.json')

    def test_rebuild_does_not_overwrite_existing_package(self):
        self.package()
        with self.assertRaises(FileExistsError): self.package()

    def test_fractional_crops_keep_declared_affine_and_exact_embedded_crop(self):
        self.plan['questions'][0]['parts'][0]['box'] = [20.1,20.3,240.2,170.4]
        save(self.path,self.plan)
        self.package()
        self.assertEqual(audit(self.root/'workbook/manifest.json')['status'],'passed')

    def test_excluded_regions_use_same_bounds_validation_as_prompts(self):
        for box in ([20,270,999,330], [20,float('nan'),480,330]):
            plan=deepcopy(self.plan)
            plan['excluded_regions'][0]['box']=box
            with self.subTest(box=box),self.assertRaises(ValueError):validate(plan,self.source)

    @unittest.skipUnless(importlib.util.find_spec('latex2mathml'), 'Optional guide renderer not installed')
    def test_local_guide_is_complete_computer_only_and_not_binding_eligible(self):
        from course_solution_guide import build_guide
        solutions = self.solutions()
        path=self.root/'solutions.json';save(path,solutions)
        build_guide(self.path,path,self.root/'guide')
        manifest=json.loads((self.root/'guide/guide-manifest.json').read_bytes())
        self.assertEqual(manifest['delivery_scope'],'computer_only')
        self.assertTrue(manifest['answer_content_included'])
        self.assertFalse(manifest['notes_binding_eligible'])
        self.assertEqual(len(manifest['prompt_images']),3)
        with self.assertRaises(ValueError):require_answer_free(manifest)
        self.assertIn('<math', (self.root/'guide/guide.html').read_text(encoding='utf-8'))

    @unittest.skipUnless(importlib.util.find_spec('latex2mathml'), 'Optional guide renderer not installed')
    def test_guide_rejects_missing_duplicate_wrong_source_and_incomplete_steps(self):
        from course_solution_guide import build_guide
        for mode in ('missing','duplicate','wrong_source','no_steps'):
            solutions=self.solutions()
            if mode=='missing':solutions['questions'].pop()
            elif mode=='duplicate':solutions['questions'][1]['id']='q1'
            elif mode=='wrong_source':solutions['source_pdf_sha256']='wrong'
            else:solutions['questions'][0]['steps']=[]
            path=self.root/'solutions.json';save(path,solutions)
            with self.subTest(mode=mode),self.assertRaises(ValueError):build_guide(self.path,path,self.root/'guide')
            self.assertFalse((self.root/'guide').exists())

    def solutions(self):
        return {'schema':'inkbridge-worked-solutions-v1','source_pdf_sha256':self.plan['source_pdf_sha256'],
            'questions':[{'id':q['id'],'title':'Synthetic solution','approach':[{'text':'Use a known identity.'}],
                'steps':[{'title':'Calculate','blocks':[{'math':'1+1=2'}]}],
                'result':[{'math':'2'}],'pitfalls':[{'text':'Check the conditions.'}]} for q in self.plan['questions']]}


if __name__ == '__main__':
    unittest.main(verbosity=2)
