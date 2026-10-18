import json
from pathlib import Path
import tempfile
import unittest
from coordinate_stream import CoordinateStream
from edit_spatial_evidence import contact_location
from verify_coordinate_alignment import point


class SpatialChecks(unittest.TestCase):
    def locate(self,changes=None,context=None):
        with tempfile.TemporaryDirectory() as folder:
            raw=Path(folder)/'raw.jsonl'
            points=[dict(point(i,1000+i),action=1 if i==1 else 3) for i in (1,2)]
            raw.write_text(''.join(json.dumps(p)+'\n' for p in points))
            store=CoordinateStream(folder,raw,'synthetic');store.ingest()
            event={'start_seq':1,'end_seq':2,'points':2,'tool_type':1,'pointer_id':0,
                   'window_id':2,'display_id':0,'context':{'page_id':'p'}}
            if context is not None:event['context']=context
            try:
                for i in (1,2):
                    p={'session_id':'synthetic','seq':i,'status':'candidate','pdf_xy':[i,2],
                       'page_uuid':'p','source_pdf_sha256':'hash','pdf_page_1based':1,
                       'reason':None,'provisional_live_revision':False}
                    if changes:changes(i,p)
                    if not p:continue
                    with store.db:store.db.execute('INSERT INTO coordinates VALUES (?,?,?,?)',
                        (i,json.dumps(p),p['status'],p['reason']))
                return contact_location(store,event)
            finally:store.close()

    def test_complete_matching_page_candidates(self):
        result=self.locate()
        self.assertEqual(result['scope'],'within_bound_page_candidate')
        self.assertEqual(result['end_pdf_candidate'],[2,2])
        self.assertFalse(result['page_position_verified'])

    def test_all_outside_is_outside_but_partial_outside_is_not(self):
        def outside(i,p):p.update(status='unresolved',reason='outside_bound_page',pdf_xy=None)
        self.assertEqual(self.locate(outside)['scope'],'outside_bound_page')
        self.assertEqual(self.locate(lambda i,p:outside(i,p) if i==1 else None)['scope'],'partial_or_unknown')

    def test_missing_provisional_wrong_session_or_page_rejected(self):
        for change in (lambda i,p:p.clear() if i==1 else None,
                       lambda i,p:p.update(provisional_live_revision=True),
                       lambda i,p:p.update(session_id='wrong'),
                       lambda i,p:p.update(page_uuid='wrong')):
            self.assertEqual(self.locate(change)['scope'],'unknown')

    def test_final_coordinate_page_must_belong_to_visible_set(self):
        context={'page_id':None,'page_ids':['p','q']}
        self.assertEqual(self.locate(context=context)['scope'],'within_bound_page_candidate')
        result=self.locate(lambda i,p:p.update(page_uuid='r'),context=context)
        self.assertEqual(result['reason'],'tool_coordinate_page_mismatch')
        result=self.locate(lambda i,p:p.update(page_uuid='q') if i==2 else None,context=context)
        self.assertEqual(result['reason'],'ambiguous_page_identity')


if __name__=='__main__':unittest.main(verbosity=2)
