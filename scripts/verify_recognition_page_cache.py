"""Original/model/region identity, coordinate mapping, reuse and corruption."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz
from recognition_page_cache import TextCache,process_page
from recognition_context import original_text_refs


class Provider:
    identity={'model':'synthetic-test'}
    init_ms=0
    calls=0
    def recognize(self,image):
        self.calls+=1
        return [[[[0,0],[40,0],[40,20],[0,20]],'unverified',.8]],1.,[.001]


class CacheChecks(unittest.TestCase):
    def test_changing_original_never_publishes_misattributed_result(self):
        with tempfile.TemporaryDirectory() as folder,fitz.open() as doc:
            doc.new_page(width=200,height=100);cache=TextCache(Path(folder)/'cache.sqlite')
            def changed():raise ValueError('source changed')
            try:
                with self.assertRaises(ValueError):process_page(doc,1,'a'*64,Provider(),cache,source_guard=changed)
                self.assertEqual(cache.db.execute('SELECT COUNT(*) FROM page_text').fetchone()[0],0)
                for page in (0,-1,2,True):
                    with self.assertRaises(ValueError):process_page(doc,page,'a'*64,Provider(),cache)
            finally:cache.close()

    def test_roi_boxes_map_to_original_and_identical_request_reuses_result(self):
        with tempfile.TemporaryDirectory() as folder,fitz.open() as doc:
            doc.new_page(width=200,height=100);provider=Provider();cache=TextCache(Path(folder)/'cache.sqlite')
            try:
                result,hit=process_page(doc,1,'a'*64,provider,cache,[10,20,110,70])
                self.assertFalse(hit);self.assertEqual(result['spans'][0]['box_pdf_pt'],[10,20,30,30])
                again,hit=process_page(doc,1,'a'*64,provider,cache,[10,20,110,70])
                self.assertTrue(hit);self.assertEqual(provider.calls,1);self.assertEqual(result,again)
                self.assertFalse(result['formula_recognition_verified'])
            finally:cache.close()

    def test_page_original_model_and_region_have_distinct_keys(self):
        with tempfile.TemporaryDirectory() as folder,fitz.open() as doc:
            doc.new_page(width=200,height=100);doc.new_page(width=200,height=100)
            provider=Provider();cache=TextCache(Path(folder)/'cache.sqlite')
            try:
                keys=[]
                for page,sha,region in [(1,'a'*64,None),(2,'a'*64,None),(1,'b'*64,None),(1,'a'*64,[10,20,110,70])]:
                    result,hit=process_page(doc,page,sha,provider,cache,region);keys.append(result['cache_key'])
                    self.assertFalse(hit)
                provider.identity={'model':'new-synthetic-test'}
                result,hit=process_page(doc,1,'a'*64,provider,cache);keys.append(result['cache_key'])
                self.assertEqual(len(set(keys)),5)
            finally:cache.close()

    def test_embedded_text_does_not_run_ocr_and_reopens_from_disk(self):
        with tempfile.TemporaryDirectory() as folder,fitz.open() as doc:
            page=doc.new_page(width=400,height=200)
            page.insert_text((20,40),'An original page with enough embedded text.')
            provider=Provider();path=Path(folder)/'cache.sqlite';cache=TextCache(path)
            try:result,_=process_page(doc,1,'a'*64,provider,cache)
            finally:cache.close()
            cache=TextCache(path)
            try:self.assertEqual(cache.get(result['request']),result)
            finally:cache.close()
            self.assertEqual(provider.calls,0);self.assertEqual(result['method'],'embedded_pdf_text_unverified')

    def test_corrupt_optional_cache_is_unavailable_not_authoritative(self):
        with tempfile.TemporaryDirectory() as folder,fitz.open() as doc:
            doc.new_page(width=200,height=100);path=Path(folder)/'cache.sqlite';cache=TextCache(path)
            try:
                result,_=process_page(doc,1,'a'*64,Provider(),cache)
                with cache.db:cache.db.execute("UPDATE page_text SET payload_hash='bad'")
                with self.assertRaises(ValueError):cache.get(result['request'])
                refs=original_text_refs(path,[('a'*64,1)])
                self.assertEqual(refs['status'],'unavailable');self.assertEqual(refs['pages'],[])
            finally:cache.close()

    def test_study_context_only_reads_matching_full_page_and_keeps_uncertainty(self):
        with tempfile.TemporaryDirectory() as folder,fitz.open() as doc:
            doc.new_page(width=200,height=100);path=Path(folder)/'cache.sqlite';cache=TextCache(path)
            try:
                process_page(doc,1,'a'*64,Provider(),cache,[10,20,110,70])
                self.assertEqual(original_text_refs(path,[('a'*64,1)])['pages'],[])
                process_page(doc,1,'a'*64,Provider(),cache)
                refs=original_text_refs(path,[('a'*64,1),('b'*64,1),('a'*64,2)])
                self.assertEqual(len(refs['pages']),1)
                self.assertFalse(refs['pages'][0]['accuracy_verified'])
                self.assertFalse(refs['pages'][0]['question_attribution_verified'])
            finally:cache.close()


if __name__=='__main__':unittest.main(verbosity=2)
