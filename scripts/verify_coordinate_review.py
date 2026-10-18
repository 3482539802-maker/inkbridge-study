"""Context rendering must never invent a page or join missing evidence."""
from copy import deepcopy
import hashlib,json,tempfile,unittest
from pathlib import Path

from coordinate_review import split_runs,attach_groups,build_session


def located(seq,status='candidate',page=1):
    return {'seq':seq,'pdf_xy':[seq+.5,2.0],'source_pdf_sha256':'a'*64,
        'pdf_page_1based':page,'page_uuid':'uuid'+str(page),'status':status}


class CoordinateReviewChecks(unittest.TestCase):
    def test_unknown_points_never_have_a_path_or_bridge(self):
        rows=[located(1),located(2),{'seq':3,'pdf_xy':None},located(4),located(5)]
        result=split_runs(rows)
        self.assertEqual([r['sequences'] for r in result],[[1,2],[4,5]])
        self.assertEqual(sum(len(r['xy']) for r in result),4)
        self.assertEqual(split_runs([{'seq':1,'pdf_xy':None}]),[])

    def test_gap_status_or_page_change_breaks_lines(self):
        rows=[located(1),located(3),located(4,'extrapolated_candidate'),located(5,page=2)]
        result=split_runs(rows)
        self.assertEqual([r['sequences'] for r in result],[[1],[3],[4],[5]])
        self.assertEqual(result[2]['status'],'extrapolated_candidate')
        self.assertNotEqual(result[2]['page_key'],result[3]['page_key'])

    def test_group_requires_exact_source_hash_and_contact_bounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw=Path(temporary)/'raw.jsonl'
            data=b'{"type":"point","seq":1,"tool_type":1}\n'
            raw.write_bytes(data);sha=hashlib.sha256(data).hexdigest()
            sample={'id':'g','number':1,'input_mode':'screen','predictions':[],
                'source':{'contacts':[{'start_seq':1,'end_seq':1,'point_count':1,
                    'selected_raw_records_sha256':sha}]}}
            packet={'manifest':{'session_id':'s','raw_sha256':sha},'samples':[sample]}
            session={'id':'s','contacts':[{'start_seq':1,'end_seq':1}],'groups':[]}
            attach_groups(session,packet,raw)
            self.assertEqual(session['groups'][0]['starts'],[1])
            wrong=deepcopy(packet);wrong['samples'][0]['source']['contacts'][0]['selected_raw_records_sha256']='b'*64
            with self.assertRaises(ValueError):attach_groups(session,wrong,raw)
            wrong=deepcopy(session);wrong['contacts'][0]['end_seq']=2
            with self.assertRaises(ValueError):attach_groups(wrong,packet,raw)

    def test_changed_final_export_refused_before_original_render(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);directory=root/'output/pipeline/cmc/session-s';directory.mkdir(parents=True)
            raw=root/'output/ink/sessions/s.jsonl';raw.parent.mkdir(parents=True);raw.write_bytes(b'raw\n')
            (directory/'points.jsonl').write_bytes(b'changed coordinates\n')
            (directory/'job.json').write_text(json.dumps({'final_complete':True,'geometry_closed':True}))
            (directory/'final-audit.json').write_text(json.dumps({'session_id':'s',
                'raw_sha256':hashlib.sha256(raw.read_bytes()).hexdigest(),
                'coordinate_export_sha256':'a'*64,'raw_index_coordinates_export_equal':True}))
            with self.assertRaisesRegex(ValueError,'Final raw/export evidence mismatch'):
                build_session(root,'s',{}, {})


if __name__=='__main__':unittest.main(verbosity=2)
