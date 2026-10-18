"""Human labels must match the displayed, immutable historical inputs."""
from copy import deepcopy
import hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

from recognition_review import build,evaluate,normalize_latex,write_review,refresh_review


def packet():
    manifest={'schema':1,'session_id':'s','model_reports':[{'sha256':'a'*64},{'sha256':'b'*64}],
        'samples':[{'id':'s:1','predictions':[
            {'status':'candidate','eos_observed':True,'latex_candidate':'$$ x + 1 $$'},
            {'status':'token_budget_exhausted','eos_observed':False,'latex_candidate':'x+1'}]}]}
    identity=hashlib.sha256(json.dumps(manifest,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return {'dataset_id':identity,'manifest':manifest,'samples':[]}


class ReviewChecks(unittest.TestCase):
    def test_annotation_with_no_latex_is_valid_and_not_a_formula_score(self):
        p=packet();labels={'schema':1,'dataset_id':p['dataset_id'],'session_id':'s',
            'annotations':[{'sample_id':'s:1','kind':'annotation','notes':'框选原题条件','latex':''}]}
        report=evaluate(p,labels)
        self.assertEqual(report['classes'],{'annotation':1})
        self.assertIsNone(report['scores'][0]['normalized_exact_match_rate'])

    def test_refresh_preserves_exact_packet_manifest_and_draft_key(self):
        import re
        p=packet()
        with tempfile.TemporaryDirectory() as temporary:
            destination=Path(temporary)/'review';write_review(p,destination)
            path=destination/'review.html';before=path.read_text(encoding='utf-8')
            manifest=(destination/'manifest.json').read_bytes()
            self.assertEqual(refresh_review(path),p['dataset_id'])
            after=path.read_text(encoding='utf-8')
            extract=lambda h:re.search(r'<script id="packet" type="application/json">(.*?)</script>',h,re.S)[1]
            self.assertEqual(extract(before),extract(after))
            self.assertEqual((destination/'manifest.json').read_bytes(),manifest)
            self.assertIn("storageKey='inkbridge-review:'+packet.dataset_id",after)
            self.assertIn("['annotation','题目标注",after)

    def test_unlabeled_or_class_only_has_no_formula_accuracy_claim(self):
        p=packet();labels={'schema':1,'dataset_id':p['dataset_id'],'session_id':'s','annotations':[]}
        report=evaluate(p,labels)
        self.assertIsNone(report['scores'][0]['normalized_exact_match_rate'])
        self.assertFalse(report['accuracy_benchmark_complete'])
        labels['annotations']=[{'sample_id':'s:1','kind':'complete_formula'}]
        report=evaluate(p,labels)
        self.assertIsNone(report['scores'][0]['normalized_exact_match_rate'])
        self.assertFalse(report['accuracy_benchmark_complete'])

    def test_only_explicit_complete_formula_transcription_is_scored(self):
        p=packet();labels={'schema':1,'dataset_id':p['dataset_id'],'session_id':'s',
            'annotations':[{'sample_id':'s:1','kind':'complete_formula','latex':'\\[x + 1\\]'}]}
        report=evaluate(p,labels)
        self.assertEqual([s['normalized_exact_match_rate'] for s in report['scores']],[1,0])
        labels['annotations'][0]['kind']='diagram'
        self.assertIsNone(evaluate(p,labels)['scores'][0]['normalized_exact_match_rate'])
        self.assertEqual(normalize_latex('x^{1}'),'x^{1}','do not invent semantic equivalence')

    def test_wrong_dataset_session_duplicate_unknown_or_oversized_label_rejected(self):
        p=packet();base={'schema':1,'dataset_id':p['dataset_id'],'session_id':'s',
            'annotations':[{'sample_id':'s:1','kind':'diagram'}]}
        variants=[dict(base,dataset_id='wrong'),dict(base,session_id='other'),
            dict(base,annotations=base['annotations']*2),
            dict(base,annotations=[{'sample_id':'s:2','kind':'diagram'}]),
            dict(base,annotations=[{'sample_id':'s:1','kind':'model_guessed'}]),
            dict(base,annotations=[{'sample_id':'s:1','kind':'diagram','latex':'x'*8193}])]
        for labels in variants:
            with self.assertRaises(ValueError):evaluate(p,labels)
        changed=deepcopy(p);changed['manifest']['samples'][0]['predictions'][0]['latex_candidate']='wrong'
        with self.assertRaises(ValueError):evaluate(changed,base)

    def test_embedded_untrusted_text_cannot_end_script_or_load_remote_assets(self):
        p=packet();p['samples']=[{'id':'</script><script>alert(1)</script>'}]
        with tempfile.TemporaryDirectory() as temporary:
            destination=Path(temporary)/'review';write_review(p,destination)
            html=(destination/'review.html').read_text(encoding='utf-8')
            self.assertNotIn('</script><script>alert(1)',html)
            self.assertIn("connect-src 'none'",html)
            self.assertNotIn('<script src=',html)
            self.assertIn('先看清写了什么',html)

    def test_changed_raw_or_model_input_rejected_before_publishing(self):
        import numpy as np
        group={'id':'s:1','session_id':'s','start_sequences':[1],'contact_count':1,
            'input_mode_candidate':'screen','start_us':0,'end_us':1000,'budget_rejection':None}
        evidence={'contacts':[{'selected_raw_records_sha256':'a'*64}]}
        report={'external_inference':False,'proposals':{'session_id':'s'},'results':[
            {'group':group,'input_evidence':evidence,'status':'historical_candidate_requires_review',
             'recognition':{'status':'candidate','eos_observed':True,'latex_candidate':'x'}}]}
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);raw=root/'raw.jsonl';raw.write_bytes(b'canonical\n')
            (root/'job.json').write_text(json.dumps({'session_id':'s','geometry_closed':True,'final_complete':True}))
            (root/'processing.json').write_text(json.dumps({'final_complete':True}))
            model=root/'model.json';model.write_text(json.dumps(report))
            with patch('recognition_review.render_screen_contacts',return_value=(np.zeros((32,32,3),dtype=np.uint8),evidence)):
                self.assertEqual(len(build(root,raw,[model])['samples']),1)
            changed=deepcopy(evidence);changed['contacts'][0]['selected_raw_records_sha256']='b'*64
            with patch('recognition_review.render_screen_contacts',return_value=(np.zeros((32,32,3),dtype=np.uint8),changed)):
                with self.assertRaises(ValueError):build(root,raw,[model])
            def mutate(*args):
                raw.write_bytes(b'changed\n');return np.zeros((32,32,3),dtype=np.uint8),evidence
            with patch('recognition_review.render_screen_contacts',side_effect=mutate):
                with self.assertRaises(ValueError):build(root,raw,[model])


if __name__=='__main__':unittest.main(verbosity=2)
