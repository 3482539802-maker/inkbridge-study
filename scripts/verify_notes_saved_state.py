import copy
import json
from pathlib import Path
import tempfile
import unittest
from notes_saved_state import SavedNotes,export_pair


def data(sha,points=None,count=1):
    binding={'source_hinote_sha256':sha,'note_id':'note','title':'Notes <test>',
             'source_pdf_sha256':'pdfhash','pages':{'page':1}}
    stroke={'points_native':points or [[1,2],[3,4]],'width_native':2,'pen_type':2,
            'style_sha256':'style','layer_id':'layer','degenerate_point_path':False}
    native={'source_hinote_sha256':sha,'pages':[{'page_uuid':'page','background_index':0,
        'native_size_candidate':[1000,1200],'strokes':[copy.deepcopy(stroke) for _ in range(count)]}]}
    return binding,native


class SavedChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=SavedNotes(self.root/'notes.sqlite')
    def tearDown(self):self.store.close();self.temp.cleanup()
    def add(self,sha,**kw):return self.store.import_decoded(*data(sha,**kw),self.root/(sha+'.hinote'))

    def test_idempotent_restart_and_duplicate_geometry_separate_ids(self):
        self.add('before',count=2);self.add('before',count=2)
        self.store.close();self.store=SavedNotes(self.root/'notes.sqlite')
        strokes=self.store.page('before','page')['strokes']
        self.assertEqual(len(strokes),2)
        self.assertNotEqual(strokes[0]['local_snapshot_object_id'],strokes[1]['local_snapshot_object_id'])
        self.assertEqual(strokes[0]['content_sha256'],strokes[1]['content_sha256'])
        self.assertFalse(self.store.metadata('before')['live_state_verified'])

    def test_snapshot_absence_does_not_reconstruct_delete_or_undo(self):
        self.add('before',count=2);self.add('after',count=1)
        result=self.store.compare('before','after','synthetic')
        self.assertEqual(result['pages'][0]['before_unmatched_indices'],[1])
        self.assertFalse(result['operation_history_reconstructed'])
        self.assertEqual(len(result['pages'][0]['unchanged_path_pairs']),1)

    def test_wrong_hash_source_and_page_identity_rejected(self):
        self.add('same');binding,native=data('same');binding['source_pdf_sha256']='other'
        with self.assertRaises(ValueError):self.store.import_decoded(binding,native,'source')
        for kind in ('hash','page'):
            binding,native=data('invalid')
            if kind=='hash':native['source_hinote_sha256']='wrong'
            else:native['pages'][0]['page_uuid']='../escape'
            with self.assertRaises(ValueError):self.store.import_decoded(binding,native,'source')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0],1)

    def test_failed_import_rolls_back_snapshot_and_objects(self):
        binding,native=data('bad',count=2)
        native['pages'][0]['strokes'][1]['points_native']=[[float('nan'),2],[3,4]]
        with self.assertRaises(ValueError):self.store.import_decoded(binding,native,'source')
        for table in ('snapshots','pages','objects'):
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_export_is_saved_snapshot_and_preserves_source_identity(self):
        self.add('before');self.add('after',points=[[4,6],[6,8]])
        result=self.store.compare('before','after')
        manifest=export_pair(self.store,result,self.root/'view')
        self.assertEqual(manifest['after']['snapshot_sha256'],'after')
        self.assertEqual(len(result['pages'][0]['translated_path_candidates']),1)
        page=json.loads((self.root/'view/page.json').read_bytes())
        self.assertEqual(page['strokes'][0]['points_native'],[[4,6],[6,8]])
        html=(self.root/'view/report.html').read_text(encoding='utf-8')
        self.assertIn('Notes &lt;test&gt;',html)


if __name__=='__main__':unittest.main(verbosity=2)
